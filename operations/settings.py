# reads one client's settings file

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from whatsapp import PHONE, PROVIDERS

# values used when the file does not mention a setting
DEFAULTS = {
    "timezone": "Asia/Kolkata",
    "setup_min_seconds": 300,
    "gap_seconds": 5,
    "drift": {"window": 10, "watch_pct": 5, "act_pct": 10},
    "weekly_off": [],            # for example ["Sunday"]
    "failure_alarm_codes": [],   # empty = every alarm counts
    "maintenance_jobs": [],
    "last_done": {},
    "stop_reasons": ["LOADING", "NO_MATERIAL", "TOOL_CHANGE", "NO_OPERATOR",
                     "QUALITY_CHECK", "SETUP", "MAINTENANCE", "OTHER"],
    "alerts": {
        "alarm_minutes": 5,
        "idle_minutes": 15,
        "silent_minutes": 2,
        "drift_levels": ["ACT"],
        "maintenance_levels": ["OVERDUE", "DUE SOON"],
    },
    # the end-of-shift message. No recipients = no message.
    # Each recipient: {"name": "Owner", "phone": "+919812345678", "lang": "mr"}
    "whatsapp": {
        "provider": "file",        # "file" writes to output/messages/, "meta" or "wasender" sends
        "recipients": [],
        "seconds_between": 6,      # WasenderAPI's account protection allows 1 message per 5 s
        "max_attempts": 6,
        # Meta's official API: the number to send from (API Setup in the Meta app) and the
        # approved template. The token stays in the WHATSAPP_TOKEN environment variable.
        "meta_phone_number_id": "",
        "meta_template": "shift_report",
        "meta_api_version": "v27.0",
    },
}

MESSAGE_LANGUAGES = ("en", "mr", "hi")

# no default. It changes the OEE number, so the file must say it
REQUIRED = ["client", "shifts", "setup_is_planned"]


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def minutes(text):
    # "07:30" -> 450
    t = datetime.strptime(text, "%H:%M")
    return t.hour * 60 + t.minute


def shift_minutes(shift):
    # start and end of a shift, in minutes from the midnight it starts after
    start, end = minutes(shift["start"]), minutes(shift["end"])
    return start, end + 1440 if end <= start else end


def break_minutes(shift):
    # the breaks of a shift, on the same scale as shift_minutes
    start, _ = shift_minutes(shift)
    out = []
    for b in shift["breaks"]:
        begin = minutes(b["start"])
        if begin < start:
            begin += 1440
        finish = minutes(b["end"])
        while finish <= begin:
            finish += 1440
        out.append((begin, finish))
    return sorted(out)


def load_settings(path):

    with open(path, encoding="utf-8") as f:
        given = json.load(f)

    # a misspelt name would be ignored silently, so stop on it
    known = set(DEFAULTS) | set(REQUIRED)
    unknown = set(given) - known
    if unknown:
        raise ValueError(f"unknown setting(s): {sorted(unknown)}")

    for name in REQUIRED:
        if name not in given:
            raise ValueError(f"settings file must have: {name}")

    # file values on top of the defaults
    s = {**DEFAULTS, **given}
    s["drift"] = {**DEFAULTS["drift"], **given.get("drift", {})}
    s["alerts"] = {**DEFAULTS["alerts"], **given.get("alerts", {})}
    s["whatsapp"] = {**DEFAULTS["whatsapp"], **given.get("whatsapp", {})}

    check(s)
    return s


def check(s):

    ZoneInfo(s["timezone"])

    if not isinstance(s["setup_is_planned"], bool):
        raise ValueError("setup_is_planned must be true or false")

    if not s["shifts"]:
        raise ValueError("shifts cannot be empty")
    for sh in s["shifts"]:
        # breaks has no default. It changes the OEE number, so the file must say it
        for key in ("name", "start", "end", "breaks"):
            if key not in sh:
                raise ValueError(f"every shift needs: {key}")
        for key in ("start", "end"):
            datetime.strptime(sh[key], "%H:%M")

        begin, finish = shift_minutes(sh)
        for b in sh["breaks"]:
            if "start" not in b or "end" not in b:
                raise ValueError("every break needs: start, end")
            datetime.strptime(b["start"], "%H:%M")
            datetime.strptime(b["end"], "%H:%M")
        for b_start, b_end in break_minutes(sh):
            if b_start < begin or b_end > finish:
                raise ValueError(f"{sh['name']}: a break is outside the shift")

    wrong = set(s["weekly_off"]) - set(WEEKDAYS)
    if wrong:
        raise ValueError(f"weekly_off: not a day: {sorted(wrong)}")

    if s["setup_min_seconds"] <= 0 or s["gap_seconds"] <= 0:
        raise ValueError("setup_min_seconds and gap_seconds must be above 0")

    reasons = s["stop_reasons"]
    if not reasons or not all(isinstance(r, str) and r.strip() for r in reasons):
        raise ValueError("stop_reasons must be a list of names")
    if len(set(reasons)) != len(reasons):
        raise ValueError("stop_reasons has a name twice")

    d = s["drift"]
    if d["window"] < 2 or not (0 < d["watch_pct"] < d["act_pct"]):
        raise ValueError("drift: window 2 or more, and 0 < watch_pct < act_pct")

    al = s["alerts"]
    unknown = set(al) - set(DEFAULTS["alerts"])
    if unknown:
        raise ValueError(f"unknown alert setting(s): {sorted(unknown)}")
    for key in ("alarm_minutes", "idle_minutes", "silent_minutes"):
        if al[key] <= 0:
            raise ValueError(f"alerts: {key} must be above 0")
    if not set(al["drift_levels"]) <= {"WATCH", "ACT"}:
        raise ValueError("alerts: drift_levels can only hold WATCH and ACT")
    if not set(al["maintenance_levels"]) <= {"OVERDUE", "DUE SOON", "NO RECORD"}:
        raise ValueError("alerts: maintenance_levels can only hold OVERDUE, DUE SOON, NO RECORD")

    w = s["whatsapp"]
    unknown = set(w) - set(DEFAULTS["whatsapp"])
    if unknown:
        raise ValueError(f"unknown whatsapp setting(s): {sorted(unknown)}")
    if w["provider"] not in PROVIDERS:
        raise ValueError(f"whatsapp: provider must be one of {', '.join(PROVIDERS)}")
    if w["seconds_between"] < 0 or w["max_attempts"] < 1:
        raise ValueError("whatsapp: seconds_between 0 or more, max_attempts 1 or more")
    if w["provider"] == "meta" and not str(w["meta_phone_number_id"]).isdigit():
        raise ValueError("whatsapp: meta_phone_number_id must be the number ID from API Setup (digits only)")
    if not re.fullmatch(r"[a-z0-9_]+", w["meta_template"]):
        raise ValueError("whatsapp: meta_template is written in small letters, digits and _")
    if not re.fullmatch(r"v\d+\.\d+", w["meta_api_version"]):
        raise ValueError("whatsapp: meta_api_version looks like v27.0")
    phones = []
    for r in w["recipients"]:
        r.setdefault("lang", "en")
        if not r.get("name") or not isinstance(r.get("phone"), str):
            raise ValueError("whatsapp: every recipient needs a name and a phone")
        if not PHONE.match(r["phone"]):
            raise ValueError(f"whatsapp: {r['name']}: phone must look like +919812345678")
        if r["lang"] not in MESSAGE_LANGUAGES:
            raise ValueError(f"whatsapp: {r['name']}: lang must be one of {', '.join(MESSAGE_LANGUAGES)}")
        phones.append(r["phone"])
    if len(set(phones)) != len(phones):
        raise ValueError("whatsapp: a phone number is listed twice")

    for j in s["maintenance_jobs"]:
        if "job" not in j or j.get("every_h", 0) <= 0:
            raise ValueError("every maintenance job needs a name and every_h above 0")


if __name__ == "__main__":

    s = load_settings("settings/example_client.json")

    print("client:           ", s["client"])
    print("time zone:        ", s["timezone"])
    print("shifts:           ", [f"{x['name']} {x['start']}-{x['end']}" for x in s["shifts"]])
    print("setup is planned: ", s["setup_is_planned"])
    print("setup min seconds:", s["setup_min_seconds"])
    print("gap seconds:      ", s["gap_seconds"])
    print("drift:            ", s["drift"])
    print("failure alarms:   ", s["failure_alarm_codes"] or "all alarms count")
    print("maintenance jobs: ", [j["job"] for j in s["maintenance_jobs"]])