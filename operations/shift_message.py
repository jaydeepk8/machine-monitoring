# the end-of-shift WhatsApp message: short, in the reader's language, and saying why,
# not only how much. Every number is copied from the result tables. The only sum is the
# shop's total parts, added the same way the shift report adds them.
#
#   python operations/shift_message.py --show                 the latest shift's message
#   python operations/shift_message.py --show --lang mr
#   python operations/shift_message.py --send                 send the messages that are waiting
#   python operations/shift_message.py --retry-failed         try the failed ones again
#   python operations/shift_message.py --test +919812345678   send one test message
#   python operations/shift_message.py --template             the Meta template to submit, per language
#
# A message is queued in the outbox when its shift report is written, in the same step,
# so a restart cannot lose it or queue it twice. auto_run.py sends what is waiting.
# The Marathi and Hindi wording is a first draft and needs checking by a native speaker.

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

import pandas as pd

from settings import load_settings
from store import query
from whatsapp import SendError, make_sender

RESULTS_DB = "output/results.db"

PHRASES = {
    "en": {
        "parts": "Parts made: {n}",
        "machine": "{m}: OEE {oee}, {parts} parts",
        "lowest": "Lowest OEE: {m}.",
        "stood": "It stood still {down} min, mostly {reason} ({minutes} min).",
        "slow": "It ran slower than ideal (performance {perf}).",
        "longest": "Longest stop: {m}, {reason} {minutes} min at {at}.",
        "look": "To look at:",
        "drift": "{m} program {p}: latest parts {pct}% slower than ideal",
        "OVERDUE": "{m} {job}: {h} h overdue",
        "DUE SOON": "{m} {job}: due in {h} h",
        "NO RECORD": "{m} {job}: no service record",
        "untapped": "{m}: {minutes} min of stops had no reason tapped",
        "missing": "{m}: readings missing for {minutes} min today, so its numbers may be low",
        "quality": "Quality was not measured: no rejects were entered.",
        "no_reason": "UNKNOWN (no reason tapped)",
        "not_available": "not available",
        "test": "Test message from the machine monitoring system. Shift messages will come to this number.",
        "none": "nothing",
        "more": "and {n} more",
    },
    "mr": {
        "parts": "बनलेले पार्ट्स: {n}",
        "machine": "{m}: OEE {oee}, {parts} पार्ट्स",
        "lowest": "सगळ्यात कमी OEE: {m}.",
        "stood": "हे मशीन {down} मिनिटे बंद होते, जास्त करून {reason} मुळे ({minutes} मिनिटे).",
        "slow": "हे मशीन आदर्श वेळेपेक्षा हळू चालले (performance {perf}).",
        "longest": "सगळ्यात मोठा थांबा: {m}, {reason} {minutes} मिनिटे, {at} वाजता.",
        "look": "लक्ष द्या:",
        "drift": "{m} प्रोग्राम {p}: शेवटचे पार्ट्स आदर्शपेक्षा {pct}% हळू",
        "OVERDUE": "{m} {job}: {h} तास उशीर झाला आहे",
        "DUE SOON": "{m} {job}: {h} तासांत करायचे आहे",
        "NO RECORD": "{m} {job}: सर्व्हिसची नोंद नाही",
        "untapped": "{m}: {minutes} मिनिटांच्या थांब्यांचे कारण टाकले नाही",
        "missing": "{m}: आज {minutes} मिनिटांचा डेटा मिळाला नाही, त्यामुळे आकडे कमी दिसू शकतात",
        "quality": "क्वालिटी मोजली नाही: रिजेक्ट्स टाकले नाहीत.",
        "no_reason": "UNKNOWN (कारण टाकले नाही)",
        "not_available": "उपलब्ध नाही",
        "test": "मशीन मॉनिटरिंग सिस्टमकडून टेस्ट मेसेज. शिफ्टचे मेसेज या नंबरवर येतील.",
        "none": "काही नाही",
        "more": "आणखी {n}",
    },
    "hi": {
        "parts": "बने पार्ट्स: {n}",
        "machine": "{m}: OEE {oee}, {parts} पार्ट्स",
        "lowest": "सबसे कम OEE: {m}.",
        "stood": "यह मशीन {down} मिनट बंद रही, ज़्यादातर {reason} की वजह से ({minutes} मिनट).",
        "slow": "यह मशीन आदर्श समय से धीमी चली (performance {perf}).",
        "longest": "सबसे लंबा स्टॉप: {m}, {reason} {minutes} मिनट, {at} बजे.",
        "look": "ध्यान दें:",
        "drift": "{m} प्रोग्राम {p}: आखिरी पार्ट्स आदर्श से {pct}% धीमे",
        "OVERDUE": "{m} {job}: {h} घंटे देर हो चुकी है",
        "DUE SOON": "{m} {job}: {h} घंटे में करना है",
        "NO RECORD": "{m} {job}: सर्विस का रिकॉर्ड नहीं",
        "untapped": "{m}: {minutes} मिनट के स्टॉप का कारण नहीं डाला गया",
        "missing": "{m}: आज {minutes} मिनट का डेटा नहीं मिला, इसलिए आंकड़े कम दिख सकते हैं",
        "quality": "क्वालिटी नहीं मापी गई: रिजेक्ट दर्ज नहीं किए गए.",
        "no_reason": "UNKNOWN (कारण नहीं डाला गया)",
        "not_available": "उपलब्ध नहीं",
        "test": "मशीन मॉनिटरिंग सिस्टम से टेस्ट मैसेज. शिफ्ट के मैसेज इसी नंबर पर आएंगे.",
        "none": "कुछ नहीं",
        "more": "और {n}",
    },
}

QUALITY_NOTE = "quality not measured"

# Meta's official API only starts a chat with an approved template. The values put into a
# template cannot hold line breaks, so the layout lives in the template and each value is one
# line. Submit these in WhatsApp Manager as a Utility template, one per language
# (python operations/shift_message.py --template prints them with sample values).
# A template must not start or end with a value, hence the fixed first and last lines.
TEMPLATES = {
    "en": ("Shift report: {{1}}\nShop: {{2}}\n\nParts made: {{3}}\nOEE: {{4}}\n\n"
           "Why: {{5}}\n\nTo look at: {{6}}\n\nSent by the machine monitoring system."),
    "mr": ("शिफ्ट रिपोर्ट: {{1}}\nशॉप: {{2}}\n\nबनलेले पार्ट्स: {{3}}\nOEE: {{4}}\n\n"
           "कारण: {{5}}\n\nलक्ष द्या: {{6}}\n\nमशीन मॉनिटरिंग सिस्टमकडून पाठवले."),
    "hi": ("शिफ्ट रिपोर्ट: {{1}}\nशॉप: {{2}}\n\nबने पार्ट्स: {{3}}\nOEE: {{4}}\n\n"
           "कारण: {{5}}\n\nध्यान दें: {{6}}\n\nमशीन मॉनिटरिंग सिस्टम से भेजा गया."),
}
# Meta's limit for a template body, with the values filled in
BODY_LIMIT = 1024


# ---- writing the message

def blank(x):
    return x is None or (isinstance(x, float) and pd.isna(x))


def pct(value, t):
    return t["not_available"] if blank(value) else f"{value}%"


def reason_text(code, t):
    # codes stay as the operator sees them on the panel. UNKNOWN means nobody tapped one.
    return t["no_reason"] if code == "UNKNOWN" else code


def as_list(value):
    # shift_summary keeps these as JSON text
    if isinstance(value, str):
        return json.loads(value)
    return value if value is not None else []


def load_context(results_db):
    # what the day's other tables say at the moment the shift is reported
    context = {}
    for name in ("drift", "maintenance", "coverage"):
        try:
            context[name] = query(results_db, f'SELECT * FROM "{name}"')
        except Exception:
            context[name] = pd.DataFrame()
    return context


def to_look_at(settings, rows, context, t):
    machines = set(rows["machine"])
    items = []

    d = context.get("drift", pd.DataFrame())
    if not d.empty:
        # the program each machine was running last
        latest = d.sort_values(["machine", "started_at"]).groupby("machine").tail(1)
        for _, r in latest.iterrows():
            if r["machine"] in machines and r["status"] in settings["alerts"]["drift_levels"]:
                items.append(t["drift"].format(m=r["machine"], p=int(r["program"]), pct=r["now_pct"]))

    m = context.get("maintenance", pd.DataFrame())
    if not m.empty:
        due = m[m["machine"].isin(machines) & m["status"].isin(settings["alerts"]["maintenance_levels"])]
        # overdue first, then the soonest
        due = due.sort_values("left_h", na_position="last")
        for _, r in due.iterrows():
            h = "" if blank(r["left_h"]) else f"{abs(r['left_h']):.1f}"
            items.append(t[r["status"]].format(m=r["machine"], job=r["job"], h=h))

    for _, r in rows.iterrows():
        for top in as_list(r["top_reasons"]):
            if top["reason"] == "UNKNOWN":
                items.append(t["untapped"].format(m=r["machine"], minutes=top["minutes"]))

    c = context.get("coverage", pd.DataFrame())
    if not c.empty:
        for _, r in c[c["machine"].isin(machines)].iterrows():
            if not blank(r["missing_min"]) and r["missing_min"] > 0:
                items.append(t["missing"].format(m=r["machine"], minutes=r["missing_min"]))

    # other notes from the calculations, for example a missing ideal time, stay in English
    for _, r in rows.iterrows():
        for note in as_list(r["notes"]):
            if note != QUALITY_NOTE:
                items.append(f"{r['machine']}: {note}")

    return items


def why_lowest(rows, t):
    rated = rows[rows["oee_pct"].notna()]
    if rated.empty:
        return None
    r = rated.loc[rated["oee_pct"].idxmin()]
    label = t["lowest"].format(m=r["machine"]) if len(rated) > 1 else f"{r['machine']}:"

    top = as_list(r["top_reasons"])
    avail, perf = r["availability_pct"], r["performance_pct"]
    # the bigger of the two losses is the reason. Stops show in availability, slow cycles in performance.
    if not blank(avail) and (blank(perf) or avail <= perf) and top and r["downtime_min"] > 0:
        why = t["stood"].format(down=r["downtime_min"], reason=reason_text(top[0]["reason"], t),
                                minutes=top[0]["minutes"])
    elif not blank(perf):
        why = t["slow"].format(perf=pct(perf, t))
    else:
        return None
    return f"{label} {why}"


def longest_stop(rows, t):
    best = None
    for _, r in rows.iterrows():
        stop = r["longest_stop"]
        stop = json.loads(stop) if isinstance(stop, str) else stop
        if stop and (best is None or stop["minutes"] > best[1]["minutes"]):
            best = (r["machine"], stop)
    if best is None:
        return None
    m, stop = best
    return t["longest"].format(m=m, reason=reason_text(stop["reason"], t), minutes=stop["minutes"],
                               at=stop["started_ist"])


def facts(settings, rows, context, lang="en"):
    # what one shift's message says, before it is laid out as text or as template values
    # rows = the shift_summary rows of one shift, one per machine
    t = PHRASES[lang]
    first = rows.iloc[0]
    day = datetime.strptime(first["date"], "%Y-%m-%d").strftime("%d-%m-%Y")
    order = rows.sort_values("oee_pct", ascending=False, na_position="last")
    return {
        "shift": f"{first['shift']}, {day}, {first['window']}",
        "client": settings["client"],
        "parts": int(rows["parts"].sum()),
        "machines": [t["machine"].format(m=r["machine"], oee=pct(r["oee_pct"], t), parts=int(r["parts"]))
                     for _, r in order.iterrows()],
        "why": [x for x in (why_lowest(rows, t), longest_stop(rows, t)) if x],
        "look": to_look_at(settings, rows, context, t),
        "quality": all(QUALITY_NOTE in as_list(n) for n in rows["notes"]),
    }


def compose(settings, rows, context, lang="en"):
    # the message as free text, for the file and WasenderAPI senders
    t = PHRASES[lang]
    f = facts(settings, rows, context, lang)

    lines = [f["shift"], f["client"], "", t["parts"].format(n=f["parts"])] + f["machines"]
    if f["why"]:
        lines += [""] + f["why"]
    if f["look"]:
        lines += ["", t["look"]] + [f"- {i}" for i in f["look"]]
    if f["quality"]:
        lines += ["", t["quality"]]
    return "\n".join(lines)


def one_line(text):
    # template values may not hold line breaks, tabs or long runs of spaces
    return " ".join(str(text).split())


def fill(template, values):
    out = template
    for i, v in enumerate(values, 1):
        out = out.replace("{{%d}}" % i, v)
    return out


def template_values(settings, rows, context, lang="en"):
    # the six values for the Meta template, kept inside its 1024 character limit
    t = PHRASES[lang]
    f = facts(settings, rows, context, lang)
    look = f["look"] + ([t["quality"]] if f["quality"] else [])

    def values(shown):
        rest = len(look) - len(shown)
        items = shown + ([t["more"].format(n=rest)] if rest else [])
        return [one_line(v) for v in (
            f["shift"], f["client"], f["parts"], "; ".join(f["machines"]),
            " ".join(f["why"]) or t["none"], "; ".join(items) or t["none"])]

    # too long: leave out the last things to look at, and say how many were left out
    shown = list(look)
    while len(fill(TEMPLATES[lang], values(shown))) > BODY_LIMIT and shown:
        shown.pop()
    out = values(shown)
    over = len(fill(TEMPLATES[lang], out)) - BODY_LIMIT
    if over > 0:
        # a shop with very many machines: shorten the OEE line
        out[3] = out[3][: max(len(out[3]) - over - 3, 1)] + "..."
    return out


# ---- the outbox

def utc_text(t=None):
    t = t or datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def open_outbox(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS outbox (
            date TEXT, shift TEXT, name TEXT, phone TEXT, lang TEXT, text TEXT,
            status TEXT, attempts INTEGER, next_try_at TEXT, last_error TEXT,
            message_id TEXT, queued_at TEXT, sent_at TEXT, params TEXT,
            PRIMARY KEY (date, shift, phone)
        )""")
    # an outbox made before template values were kept
    if "params" not in {row[1] for row in con.execute("PRAGMA table_info(outbox)")}:
        con.execute("ALTER TABLE outbox ADD COLUMN params TEXT")


def queue(con, settings, rows, context):
    # called by shift_report inside its own transaction, so the report and its messages
    # are saved together
    recipients = settings["whatsapp"]["recipients"]
    if not recipients:
        return 0
    open_outbox(con)
    first = rows.iloc[0]
    now = utc_text()
    added = 0
    for r in recipients:
        cur = con.execute(
            "INSERT OR IGNORE INTO outbox (date, shift, name, phone, lang, text, status, attempts, "
            "next_try_at, queued_at, params) VALUES (?,?,?,?,?,?,'PENDING',0,?,?,?)",
            (first["date"], first["shift"], r["name"], r["phone"], r["lang"],
             compose(settings, rows, context, r["lang"]), now, now,
             json.dumps(template_values(settings, rows, context, r["lang"]), ensure_ascii=False)),
        )
        added += cur.rowcount
    return added


def deliver(settings, results_db=RESULTS_DB, sender=None, now=None, sleep=time.sleep):
    # sends every waiting message whose time has come. Returns what happened to each.
    w = settings["whatsapp"]
    con = sqlite3.connect(results_db, timeout=30)
    done = []
    try:
        open_outbox(con)
        con.commit()
        now = now or datetime.now(timezone.utc)
        due = con.execute(
            "SELECT date, shift, name, phone, text, attempts, lang, params FROM outbox "
            "WHERE status='PENDING' AND next_try_at <= ? ORDER BY queued_at, name",
            (utc_text(now),),
        ).fetchall()
        if not due:
            return done
        sender = sender or make_sender(settings)

        for i, (date, shift, name, phone, text, attempts, lang, params) in enumerate(due):
            if i:
                # one at a time, spaced out, so the number does not look like a spammer
                sleep(w["seconds_between"])
            result = sender.send(phone, text, f"{date}_{shift}_{name}",
                                 lang=lang, params=json.loads(params) if params else None)
            attempts += 1
            key = (date, shift, phone)

            if result.ok:
                con.execute("UPDATE outbox SET status='SENT', attempts=?, sent_at=?, message_id=?, last_error=NULL "
                            "WHERE date=? AND shift=? AND phone=?", (attempts, utc_text(), result.message_id, *key))
                status = "SENT"
            elif result.retry and attempts < w["max_attempts"]:
                # wait longer after each failure: 1, 2, 4 ... minutes, at most 30
                wait = max(result.retry_after, min(60 * 2 ** (attempts - 1), 1800))
                later = utc_text(now + pd.Timedelta(seconds=wait))
                con.execute("UPDATE outbox SET attempts=?, next_try_at=?, last_error=? "
                            "WHERE date=? AND shift=? AND phone=?", (attempts, later, result.error, *key))
                status = "RETRY"
            else:
                con.execute("UPDATE outbox SET status='FAILED', attempts=?, last_error=? "
                            "WHERE date=? AND shift=? AND phone=?", (attempts, result.error, *key))
                status = "FAILED"
            # saved after every message, so a crash never sends one twice
            con.commit()
            done.append({"date": date, "shift": shift, "name": name, "status": status, "error": result.error})
    finally:
        con.close()
    return done


def retry_failed(results_db=RESULTS_DB):
    con = sqlite3.connect(results_db, timeout=30)
    try:
        open_outbox(con)
        n = con.execute("UPDATE outbox SET status='PENDING', attempts=0, next_try_at=? "
                        "WHERE status='FAILED'", (utc_text(),)).rowcount
        con.commit()
        return n
    finally:
        con.close()


def reset(results_db=RESULTS_DB):
    # forget every queued and sent message, so a test day can be sent again
    con = sqlite3.connect(results_db, timeout=30)
    try:
        con.execute("DROP TABLE IF EXISTS outbox")
        con.commit()
    finally:
        con.close()


def latest_shift(results_db):
    summary = query(results_db, "SELECT * FROM shift_summary")
    if summary.empty:
        return None
    last = summary.sort_values(["date", "window"]).iloc[-1]
    return summary[(summary["date"] == last["date"]) & (summary["shift"] == last["shift"])]


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--settings", default="settings/example_client.json")
    p.add_argument("--db", default=RESULTS_DB)
    p.add_argument("--show", action="store_true", help="print the latest shift's message, send nothing")
    p.add_argument("--lang", choices=list(PHRASES), default="en")
    p.add_argument("--send", action="store_true", help="send the messages that are waiting")
    p.add_argument("--retry-failed", action="store_true", help="put failed messages back in the queue")
    p.add_argument("--test", metavar="PHONE", help="send one test message to this number")
    p.add_argument("--template", action="store_true", help="print the Meta templates with sample values")
    a = p.parse_args()
    s = load_settings(a.settings)
    # Marathi and Hindi must print even when the output goes to a file on Windows
    sys.stdout.reconfigure(encoding="utf-8")

    if a.show:
        rows = latest_shift(a.db)
        print(compose(s, rows, load_context(a.db), a.lang) if rows is not None else "no shift results yet")

    if a.template:
        rows = latest_shift(a.db)
        if rows is None:
            raise SystemExit("no shift results to take sample values from. Run run_all.py first.")
        context = load_context(a.db)
        for lang in PHRASES:
            print(f"==== name {s['whatsapp']['meta_template']}, category Utility, language {lang}")
            print(TEMPLATES[lang])
            print()
            for i, v in enumerate(template_values(s, rows, context, lang), 1):
                print("sample {{%d}}: %s" % (i, v))
            print()

    if a.test:
        try:
            r = make_sender(s).test(a.test, PHRASES[a.lang]["test"])
        except SendError as e:
            raise SystemExit(f"cannot send: {e}")
        print(f"sent ({r.message_id})" if r.ok else f"not sent: {r.error}")

    if a.retry_failed:
        print(f"{retry_failed(a.db)} failed message(s) queued again")

    if a.send or a.retry_failed:
        try:
            for d in deliver(s, a.db):
                print(f"{d['status']:<7}{d['shift']} {d['date']} to {d['name']}  {d['error']}")
        except SendError as e:
            raise SystemExit(f"cannot send: {e}")

    if not (a.show or a.test or a.template):
        try:
            box = query(a.db, "SELECT date, shift, name, status, attempts, last_error FROM outbox ORDER BY queued_at")
            print(box.to_string(index=False) if not box.empty else "outbox is empty")
        except Exception:
            print("outbox is empty")
