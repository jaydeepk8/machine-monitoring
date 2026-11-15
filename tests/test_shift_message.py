# the end-of-shift WhatsApp message: what it says, that it is queued once with its report,
# and that sending survives a bad network. Nothing here reaches WhatsApp.

import io
import json
import re
import sqlite3
import sys
import urllib.error
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "assistant"))

import shift_message  # noqa: E402
import shift_report  # noqa: E402
from fixture import build  # noqa: E402
from settings import load_settings  # noqa: E402
from shift_message import (BODY_LIMIT, TEMPLATES, compose, deliver, fill, load_context,  # noqa: E402
                           latest_shift, retry_failed, template_values)
from store import query  # noqa: E402
from whatsapp import (FileSender, MetaSender, SendError, Sent, WasenderSender, make_sender,  # noqa: E402
                      read_meta_reply, read_reply)

EXAMPLE = ROOT / "settings" / "example_client.json"
PEOPLE = [{"name": "Owner", "phone": "+919800000001", "lang": "mr"},
          {"name": "Supervisor", "phone": "+919800000002", "lang": "en"}]


def settings_with(tmp_path, **whatsapp):
    s = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    if whatsapp:
        s["whatsapp"] = whatsapp
    path = tmp_path / "client.json"
    path.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    return load_settings(str(path))


@pytest.fixture
def db(tmp_path):
    return str(build(tmp_path / "results.db"))


@pytest.fixture
def day(db, tmp_path):
    s = settings_with(tmp_path)
    return s, latest_shift(db), load_context(db)


def numbers(text):
    return {float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)}


# ---- what the message says

def test_the_message_says_how_much_and_why(day):
    s, rows, context = day
    text = compose(s, rows, context, "en")
    assert text.startswith("Shift 1, 02-11-2026, 07:00-15:00\nExample Client")
    assert "Parts made: 430" in text
    # best first
    assert text.index("V62: OEE 73.0%") < text.index("V60: OEE 58.7%") < text.index("V61: OEE 49.8%")
    assert "Lowest OEE: V61. It stood still 198.9 min, mostly LOADING (125.1 min)." in text
    assert "Longest stop: V60, SETUP 28.2 min at 08:50." in text
    assert "- V61 program 5315: latest parts 19.6% slower than ideal" in text
    # overdue comes before due soon
    assert text.index("V61 Spindle grease: 24.0 h overdue") < text.index("V60 Spindle grease: due in 11.1 h")
    assert "Quality was not measured" in text


def test_every_number_comes_from_the_tables(db, day):
    s, rows, context = day
    source = " ".join(
        query(db, f"SELECT * FROM {t}").to_string() for t in ("shift_summary", "drift", "maintenance", "coverage")
    ) + " " + rows.iloc[0]["date"].replace("-", " ")
    # the one sum: parts of all machines, as the shift report has always added them
    source += f" {int(rows['parts'].sum())}"
    for lang in ("en", "mr", "hi"):
        text = compose(s, rows, context, lang)
        # 02-11-2026 is the date written the Indian way
        assert numbers(text) - numbers(source) <= {2.0, 11.0, 2026.0}, lang


def test_the_three_languages_carry_the_same_numbers(day):
    s, rows, context = day
    en, mr, hi = (numbers(compose(s, rows, context, lang)) for lang in ("en", "mr", "hi"))
    assert en == mr == hi


def summary_rows(**changes):
    base = {
        "machine": "M1", "shift": "S", "date": "2026-11-02", "window": "07:00-15:00", "parts": 10,
        "availability_pct": 90.0, "performance_pct": 70.0, "oee_pct": 63.0, "downtime_min": 5.0,
        "top_reasons": json.dumps([{"reason": "LOADING", "minutes": 5.0}]),
        "longest_stop": json.dumps({"reason": "LOADING", "minutes": 2.0, "started_ist": "09:00"}),
        "notes": json.dumps([]),
    }
    return pd.DataFrame([{**base, **changes}])


def test_slow_cycles_are_named_when_they_cost_more_than_stops(tmp_path):
    s = settings_with(tmp_path)
    text = compose(s, summary_rows(), {}, "en")
    # one machine: no "lowest", just the reason
    assert "M1: It ran slower than ideal (performance 70.0%)." in text


def test_stops_with_no_reason_are_pointed_out(tmp_path):
    s = settings_with(tmp_path)
    rows = summary_rows(availability_pct=50.0, top_reasons=json.dumps([{"reason": "UNKNOWN", "minutes": 4.0}]))
    text = compose(s, rows, {}, "en")
    assert "mostly UNKNOWN (no reason tapped) (4.0 min)" in text
    assert "- M1: 4.0 min of stops had no reason tapped" in text
    assert "Quality was not measured" not in text


def test_missing_readings_and_other_notes_are_said(tmp_path):
    s = settings_with(tmp_path)
    rows = summary_rows(oee_pct=None, performance_pct=None,
                        notes=json.dumps(["ideal time missing for program 7"]))
    context = {"coverage": pd.DataFrame([{"machine": "M1", "missing_min": 12.5}])}
    text = compose(s, rows, context, "en")
    assert "M1: OEE not available, 10 parts" in text
    assert "- M1: readings missing for 12.5 min today, so its numbers may be low" in text
    assert "- M1: ideal time missing for program 7" in text


# ---- settings

@pytest.mark.parametrize("recipient, error", [
    ({"name": "A", "phone": "9800000001"}, "phone must look like"),
    ({"name": "A", "phone": "+91 98000 00001"}, "phone must look like"),
    ({"name": "A", "phone": "+919800000001", "lang": "fr"}, "lang must be one of"),
    ({"phone": "+919800000001"}, "needs a name"),
])
def test_a_wrong_recipient_is_refused(tmp_path, recipient, error):
    with pytest.raises(ValueError, match=error):
        settings_with(tmp_path, recipients=[recipient])


def test_other_whatsapp_mistakes_are_refused(tmp_path):
    with pytest.raises(ValueError, match="listed twice"):
        settings_with(tmp_path, recipients=[PEOPLE[0], {**PEOPLE[1], "phone": PEOPLE[0]["phone"]}])
    with pytest.raises(ValueError, match="provider"):
        settings_with(tmp_path, provider="twilio")
    with pytest.raises(ValueError, match="unknown whatsapp"):
        settings_with(tmp_path, recipents=[])


def test_no_recipients_by_default(tmp_path):
    s = settings_with(tmp_path)
    assert s["whatsapp"]["recipients"] == [] and s["whatsapp"]["provider"] == "file"


# ---- queued with the report, once

def report(s, db, tmp_path):
    return shift_report.check(s, db, str(tmp_path / "reports"), now=pd.Timestamp("2026-11-03", tz="UTC"))


def outbox(db):
    return query(db, "SELECT * FROM outbox ORDER BY name")


def test_each_recipient_gets_one_message_in_their_language(db, tmp_path):
    s = settings_with(tmp_path, recipients=PEOPLE)
    written = report(s, db, tmp_path)
    assert written[0]["messages"] == 2
    box = outbox(db)
    assert list(box["name"]) == ["Owner", "Supervisor"]
    assert set(box["status"]) == {"PENDING"}
    assert "सगळ्यात कमी OEE: V61" in box.iloc[0]["text"]
    assert "Lowest OEE: V61" in box.iloc[1]["text"]

    # the report is not written again, so neither are the messages
    assert report(s, db, tmp_path) == []
    assert len(outbox(db)) == 2


def test_reset_lets_a_test_day_be_sent_again(db, tmp_path):
    s = settings_with(tmp_path, recipients=PEOPLE)
    report(s, db, tmp_path)
    shift_report.reset(db)
    shift_message.reset(db)
    assert report(s, db, tmp_path)[0]["messages"] == 2


def test_no_recipients_no_outbox(db, tmp_path):
    report(settings_with(tmp_path), db, tmp_path)
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert "outbox" not in tables


# ---- sending

class FakeSender:
    def __init__(self, *results):
        self.results = list(results)
        self.sent = []

    def send(self, phone, text, label="", **extra):
        self.sent.append(phone)
        self.extra = getattr(self, "extra", []) + [extra]
        return self.results.pop(0)


@pytest.fixture
def queued(db, tmp_path):
    s = settings_with(tmp_path, recipients=PEOPLE, max_attempts=3)
    report(s, db, tmp_path)
    return s, db


T0 = pd.Timestamp("2030-01-01T00:00:00Z")


def test_waiting_messages_are_sent_one_by_one(queued):
    s, db = queued
    pauses = []
    sender = FakeSender(Sent(ok=True, message_id="11"), Sent(ok=True, message_id="12"))
    done = deliver(s, db, sender, now=T0, sleep=pauses.append)
    assert [d["status"] for d in done] == ["SENT", "SENT"]
    assert pauses == [6]
    assert list(outbox(db)["message_id"]) == ["11", "12"]
    # nothing left to send
    assert deliver(s, db, FakeSender(), now=T0) == []


def test_a_network_failure_is_tried_again_later(queued):
    s, db = queued
    sender = FakeSender(Sent(ok=False, error="network: down", retry=True), Sent(ok=True))
    deliver(s, db, sender, now=T0, sleep=lambda _: None)
    box = outbox(db).set_index("name")
    assert box.loc["Owner", "status"] == "PENDING"
    assert box.loc["Owner", "next_try_at"] == "2030-01-01T00:01:00Z"
    assert box.loc["Owner", "last_error"] == "network: down"

    # not before its time
    assert deliver(s, db, FakeSender(), now=T0 + pd.Timedelta(seconds=30)) == []
    later = deliver(s, db, FakeSender(Sent(ok=True)), now=T0 + pd.Timedelta(minutes=1))
    assert [d["status"] for d in later] == ["SENT"]


def test_the_service_saying_wait_is_respected(queued):
    s, db = queued
    deliver(s, db, FakeSender(Sent(ok=False, retry=True, retry_after=600), Sent(ok=True)), now=T0,
            sleep=lambda _: None)
    assert outbox(db).set_index("name").loc["Owner", "next_try_at"] == "2030-01-01T00:10:00Z"


def test_a_message_that_cannot_go_is_marked_failed(queued):
    s, db = queued
    deliver(s, db, FakeSender(Sent(ok=False, error="HTTP 401: bad key"), Sent(ok=True)), now=T0,
            sleep=lambda _: None)
    box = outbox(db).set_index("name")
    assert box.loc["Owner", "status"] == "FAILED"
    assert box.loc["Supervisor", "status"] == "SENT"

    assert retry_failed(db) == 1
    done = deliver(s, db, FakeSender(Sent(ok=True)), now=pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=1))
    assert [(d["name"], d["status"]) for d in done] == [("Owner", "SENT")]


def test_retries_stop_after_the_last_attempt(queued):
    s, db = queued
    now = T0
    for _ in range(3):
        deliver(s, db, FakeSender(Sent(ok=False, retry=True), Sent(ok=True)), now=now, sleep=lambda _: None)
        now += pd.Timedelta(hours=1)
    owner = outbox(db).set_index("name").loc["Owner"]
    assert owner["status"] == "FAILED" and owner["attempts"] == 3


def test_a_crash_while_sending_keeps_what_already_went(queued):
    s, db = queued

    class Breaks(FakeSender):
        def send(self, phone, text, label="", **extra):
            if self.sent:
                raise RuntimeError("power cut")
            return super().send(phone, text, label, **extra)

    with pytest.raises(RuntimeError):
        deliver(s, db, Breaks(Sent(ok=True)), now=T0, sleep=lambda _: None)
    box = outbox(db).set_index("name")
    assert box.loc["Owner", "status"] == "SENT"
    assert box.loc["Supervisor", "status"] == "PENDING"


def test_the_file_sender_writes_the_message(tmp_path):
    r = FileSender(tmp_path / "messages").send("+919800000001", "नमस्कार", "2026-11-02_Shift 1_Owner")
    assert r.ok
    assert Path(r.message_id).name == "2026-11-02_Shift_1_Owner.txt"
    assert "नमस्कार" in Path(r.message_id).read_text(encoding="utf-8")


# ---- WasenderAPI, without the network

class Reply:
    def __init__(self, status, body, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    def read(self):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(result):
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if isinstance(result, Exception):
            raise result
        return result

    return opener, calls


def http_error(code, body, headers=None):
    return urllib.error.HTTPError("https://x", code, "error", headers or {}, io.BytesIO(json.dumps(body).encode()))


def test_wasender_gets_the_documented_request():
    opener, calls = opener_returning(Reply(200, {"success": True, "data": {"msgId": 100000, "status": "in_progress"}}))
    r = WasenderSender(api_key="k123", opener=opener).send("+919800000001", "नमस्कार")
    assert r.ok and r.message_id == "100000"
    req = calls[0]
    assert req.full_url == "https://www.wasenderapi.com/api/send-message"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer k123"
    assert json.loads(req.data.decode("utf-8")) == {"to": "+919800000001", "text": "नमस्कार"}


def test_wasender_busy_means_try_again():
    opener, _ = opener_returning(http_error(429, {"message": "You can only send 1 message every 1 minute.",
                                                  "retry_after": 60}))
    r = WasenderSender(api_key="k", opener=opener).send("+919800000001", "hi")
    assert not r.ok and r.retry and r.retry_after == 60


def test_wasender_bad_key_is_not_retried_and_the_key_is_not_logged():
    opener, _ = opener_returning(http_error(401, {"success": False, "message": "Invalid API key"}))
    r = WasenderSender(api_key="secret-key", opener=opener).send("+919800000001", "hi")
    assert not r.ok and not r.retry
    assert "Invalid API key" in r.error and "secret-key" not in r.error


def test_wasender_no_internet_means_try_again():
    opener, _ = opener_returning(urllib.error.URLError("getaddrinfo failed"))
    r = WasenderSender(api_key="k", opener=opener).send("+919800000001", "hi")
    assert not r.ok and r.retry and "getaddrinfo" in r.error


def test_wasender_odd_replies():
    assert read_reply(500, b"<html>").retry
    assert read_reply(400, b'{"message": "Session is not connected"}').retry
    v = read_reply(422, b'{"message": "Validation failed", "errors": {"to": ["The to field is required."]}}')
    assert not v.retry and "to field" in v.error


def test_wasender_needs_a_key(monkeypatch):
    monkeypatch.delenv("WASENDER_API_KEY", raising=False)
    with pytest.raises(SendError, match="WASENDER_API_KEY"):
        WasenderSender()


def test_a_missing_key_leaves_the_messages_waiting(queued, monkeypatch):
    s, db = queued
    s["whatsapp"]["provider"] = "wasender"
    monkeypatch.delenv("WASENDER_API_KEY", raising=False)
    with pytest.raises(SendError):
        deliver(s, db, now=T0)
    assert set(outbox(db)["status"]) == {"PENDING"}


# ---- Meta's official API, without the network

def test_the_template_values_are_one_line_each_and_fill_the_template(day):
    s, rows, context = day
    for lang in ("en", "mr", "hi"):
        values = template_values(s, rows, context, lang)
        assert len(values) == 6
        assert all(v and "\n" not in v and "\t" not in v and "     " not in v for v in values)
        body = fill(TEMPLATES[lang], values)
        assert "{{" not in body and len(body) <= BODY_LIMIT
    en = fill(TEMPLATES["en"], template_values(s, rows, context, "en"))
    assert "Parts made: 430" in en
    assert "Why: Lowest OEE: V61. It stood still 198.9 min" in en
    assert "Quality was not measured" in en


def test_the_templates_obey_metas_rules():
    for lang, body in TEMPLATES.items():
        # a value may not be the first or last thing, and the six are numbered in order
        assert not body.startswith("{{") and not body.rstrip(".").endswith("}}"), lang
        assert [int(n) for n in re.findall(r"{{(\d+)}}", body)] == [1, 2, 3, 4, 5, 6], lang
        assert len(body) < BODY_LIMIT


def test_the_template_and_the_text_carry_the_same_numbers(day):
    s, rows, context = day
    for lang in ("en", "mr", "hi"):
        text = compose(s, rows, context, lang)
        body = fill(TEMPLATES[lang], template_values(s, rows, context, lang))
        assert numbers(body) == numbers(text), lang


def test_a_long_list_is_cut_to_fit_and_says_how_much_was_left_out(tmp_path):
    s = settings_with(tmp_path)
    notes = [f"ideal time missing for program {n}" for n in range(1000, 1060)]
    rows = summary_rows(notes=json.dumps(notes))
    values = template_values(s, rows, {}, "en")
    assert len(fill(TEMPLATES["en"], values)) <= BODY_LIMIT
    assert re.search(r"and \d+ more$", values[5])


def test_nothing_to_look_at_still_fills_the_value(tmp_path):
    s = settings_with(tmp_path)
    rows = summary_rows(oee_pct=None, availability_pct=None, performance_pct=None, longest_stop=None)
    values = template_values(s, rows, {}, "mr")
    assert values[4] == "काही नाही" and values[5] == "काही नाही"


def test_the_outbox_keeps_the_template_values(db, tmp_path):
    s = settings_with(tmp_path, recipients=PEOPLE)
    report(s, db, tmp_path)
    box = outbox(db).set_index("name")
    assert json.loads(box.loc["Owner", "params"])[2] == "430"
    sender = FakeSender(Sent(ok=True), Sent(ok=True))
    deliver(s, db, sender, now=T0, sleep=lambda _: None)
    assert sender.extra[0]["lang"] == "mr" and sender.extra[0]["params"][2] == "430"


def test_an_old_outbox_gets_the_new_column(db):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE outbox (date TEXT, shift TEXT, name TEXT, phone TEXT, lang TEXT, text TEXT, "
                "status TEXT, attempts INTEGER, next_try_at TEXT, last_error TEXT, message_id TEXT, "
                "queued_at TEXT, sent_at TEXT, PRIMARY KEY (date, shift, phone))")
    shift_message.open_outbox(con)
    assert "params" in {r[1] for r in con.execute("PRAGMA table_info(outbox)")}
    con.close()


def meta(opener):
    return MetaSender("123456789012345", "shift_report", "v27.0", token="tok-secret", opener=opener)


ACCEPTED = {"messaging_product": "whatsapp", "contacts": [{"input": "+919800000001", "wa_id": "919800000001"}],
            "messages": [{"id": "wamid.HBgM"}]}


def test_meta_gets_the_documented_template_request():
    opener, calls = opener_returning(Reply(200, ACCEPTED))
    r = meta(opener).send("+919800000001", "free text is not used", lang="mr", params=["a", "b", 430])
    assert r.ok and r.message_id == "wamid.HBgM"
    req = calls[0]
    assert req.full_url == "https://graph.facebook.com/v27.0/123456789012345/messages"
    assert req.get_header("Authorization") == "Bearer tok-secret"
    assert json.loads(req.data.decode("utf-8")) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": "+919800000001",
        "type": "template",
        "template": {"name": "shift_report", "language": {"code": "mr"},
                     "components": [{"type": "body", "parameters": [
                         {"type": "text", "text": "a"}, {"type": "text", "text": "b"},
                         {"type": "text", "text": "430"}]}]},
    }


def test_meta_test_message_is_hello_world():
    opener, calls = opener_returning(Reply(200, ACCEPTED))
    assert meta(opener).test("+919800000001", "ignored").ok
    body = json.loads(calls[0].data.decode("utf-8"))
    assert body["template"] == {"name": "hello_world", "language": {"code": "en_US"}}


def meta_error(code, message="x", details=""):
    return json.dumps({"error": {"message": f"(#{code}) {message}", "type": "OAuthException", "code": code,
                                 "error_data": {"messaging_product": "whatsapp", "details": details}}}).encode()


def test_meta_busy_or_temporary_faults_are_tried_again():
    for code in (130429, 131056, 131000, 2, 80007):
        assert read_meta_reply(400, meta_error(code)).retry, code
    day = read_meta_reply(400, meta_error(131049))
    assert day.retry and day.retry_after == 24 * 3600
    assert read_meta_reply(503, b"").retry


def test_meta_setup_mistakes_need_a_person_and_say_what_to_do():
    expired = read_meta_reply(401, meta_error(190, "Access token has expired"))
    assert not expired.retry and "expired" in expired.error
    template = read_meta_reply(404, meta_error(132001, "Template name does not exist in the translation"))
    assert not template.retry and "not approved yet" in template.error
    allowed = read_meta_reply(400, meta_error(131030, "Recipient phone number not in allowed list"))
    assert not allowed.retry and "add this phone as a recipient" in allowed.error


def test_meta_never_logs_the_token():
    opener, _ = opener_returning(http_error(401, json.loads(meta_error(190))))
    r = meta(opener).send("+919800000001", "", params=["1"])
    assert not r.ok and "tok-secret" not in r.error


def test_meta_no_internet_means_try_again():
    opener, _ = opener_returning(urllib.error.URLError("getaddrinfo failed"))
    r = meta(opener).send("+919800000001", "", params=["1"])
    assert r.retry and not r.ok


def test_meta_without_values_does_not_send():
    opener, calls = opener_returning(Reply(200, ACCEPTED))
    assert not meta(opener).send("+919800000001", "text only").ok
    assert calls == []


def test_meta_needs_a_token_and_a_number(monkeypatch, tmp_path):
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False)
    with pytest.raises(SendError, match="WHATSAPP_TOKEN"):
        MetaSender("123", "shift_report")
    with pytest.raises(ValueError, match="meta_phone_number_id"):
        settings_with(tmp_path, provider="meta")
    with pytest.raises(ValueError, match="meta_template"):
        settings_with(tmp_path, meta_template="Shift Report")
    s = settings_with(tmp_path, provider="meta", meta_phone_number_id="123456789012345")
    monkeypatch.setenv("WHATSAPP_TOKEN", "t")
    sender = make_sender(s)
    assert isinstance(sender, MetaSender) and sender.url.endswith("/v27.0/123456789012345/messages")
