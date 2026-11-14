# the only things the AI can look at: fixed, read-only lookups on the results database
# every number the AI says must come from one of these. The code does the maths, never the model.

import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "operations"))

import pandas as pd  # noqa: E402

from downtime import downtime_by_reason  # noqa: E402

RESULTS_DB = str(ROOT / "output" / "results.db")
ZONE = "Asia/Kolkata"

# what the codes in the data mean, given with every downtime answer
REASON_NOTES = {
    "UNKNOWN": "the operator did not tap a reason for these stops",
    "NOT_RESPONDING": "the machine did not answer while the monitoring box and network were fine",
    "PLANNED_STOP": "breaks and time outside the shifts. Not counted as a loss",
}

MAINTENANCE_NOTES = {
    "OK": "not due yet",
    "DUE SOON": "less than 10% of the interval is left",
    "OVERDUE": "the interval has passed",
    "NO RECORD": "there is no record of when this job was last done",
    "NO CUTTING HOURS": "this machine does not report cutting hours",
}

DRIFT_NOTES = {
    "OK": "cycle times are close to ideal",
    "WATCH": "the last parts are getting slower than ideal",
    "ACT": "the last parts are much slower than ideal. Worth checking tools and settings",
    "TOO FEW PARTS": "not enough parts in this run to judge",
    "NO DATA": "the machine does not report cycle times",
}


class ToolError(Exception):
    # a question the data cannot answer as asked, such as a machine that does not exist
    pass


def nullable(kind, description):
    return {"type": [kind, "null"], "description": description}


MACHINE = nullable("string", "Machine code such as V60, exactly as the shop names it. null for every machine.")


def tool(name, description, properties=None):
    properties = properties or {}
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool("get_overview",
         "The working day this data covers, the list of machines, and for every machine and the whole shop: "
         "what it is doing now, parts made, run time, availability, performance, quality and OEE. Start here."),
    tool("get_downtime",
         "Time lost to stops, by reason, with stop counts, share of downtime and the longest single stop. "
         "For one machine, or totals for the whole shop when machine is null.",
         {"machine": MACHINE}),
    tool("get_shift_summary",
         "Per-shift numbers for each machine: parts, run time, planned time, OEE, number of stops, "
         "downtime, top reasons and the longest stop.",
         {"machine": MACHINE, "shift": nullable("string", "Shift name, or null for every shift.")}),
    tool("get_parts",
         "Parts made by program, with average cycle time and how much slower than ideal the parts were on average.",
         {"machine": MACHINE, "program": nullable("integer", "Program number such as 5315, or null for every program.")}),
    tool("get_drift",
         "Whether machines are getting slower part after part, for each program run.",
         {"machine": MACHINE}),
    tool("get_maintenance",
         "Which maintenance jobs are due, by cutting hours, with hours and days left.",
         {"machine": MACHINE}),
    tool("get_failures",
         "Alarm failures: count, time between failures (MTBF) and time to repair (MTTR).",
         {"machine": MACHINE}),
    tool("get_alerts",
         "Alerts raised by the monitoring system, such as long alarms, long idle time or silent machines.",
         {"status": {"type": "string", "enum": ["open", "all"], "description": "open for current alerts only"}}),
    tool("get_data_coverage",
         "Whether any readings are missing, which would make other numbers less certain.",
         {"machine": MACHINE}),
]


# ---------------------------------------------------------------------------

def clean(value):
    # numbers as plain floats or ints, blanks as None, so the JSON stays readable
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
        if isinstance(value, float) and math.isnan(value):
            return None
    return value


def rows(df, columns=None):
    df = df if columns is None else df[columns]
    return [{k: clean(v) for k, v in r.items()} for r in df.to_dict("records")]


def ist(text):
    if text is None or (isinstance(text, float) and math.isnan(text)):
        return None
    return pd.to_datetime(text, utc=True).tz_convert(ZONE).strftime("%H:%M")


class Shop:
    # one open results database. Each lookup reads it fresh.

    def __init__(self, db=RESULTS_DB):
        if not Path(db).exists():
            raise FileNotFoundError(f"no results database at {db}. Run run_all.py first.")
        self.db = db

    def read(self, sql, values=()):
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            return pd.read_sql_query(sql, con, params=values)
        except Exception:
            # a table that does not exist yet, such as alerts before the first live run
            return pd.DataFrame()
        finally:
            con.close()

    def machines(self):
        oee = self.read("SELECT machine FROM oee")
        return [m for m in oee["machine"] if m != "ALL"] if not oee.empty else []

    def check(self, machine):
        if machine is None:
            return None
        known = self.machines()
        for m in known:
            if m.lower() == str(machine).strip().lower():
                return m
        raise ToolError(f"there is no machine called {machine}. The machines are: {', '.join(known)}")

    def period(self):
        e = self.read("SELECT MIN(started_at) AS a, MAX(ended_at) AS b FROM events")
        if e.empty or e["a"].iloc[0] is None:
            return None
        start = pd.to_datetime(e["a"].iloc[0], utc=True).tz_convert(ZONE)
        end = pd.to_datetime(e["b"].iloc[0], utc=True).tz_convert(ZONE) + pd.Timedelta(seconds=1)
        return {"date": start.strftime("%Y-%m-%d"), "from": start.strftime("%H:%M"), "to": end.strftime("%H:%M"),
                "note": "This is the only working day in the data. Earlier days are not available."}

    def events(self, machine=None):
        e = self.read("SELECT * FROM events")
        if machine is not None and not e.empty:
            e = e[e["machine"] == machine]
        for col in ("started_at", "ended_at"):
            if col in e:
                e[col] = pd.to_datetime(e[col], utc=True)
        return e

    # ---- the lookups

    def get_overview(self):
        oee = self.read("SELECT * FROM oee")
        parts = self.read("SELECT machine, SUM(parts_added) AS parts FROM parts GROUP BY machine")
        shifts = self.read("SELECT machine, SUM(run_min) AS run_min, SUM(planned_min) AS planned_min FROM shift_summary GROUP BY machine")
        made = dict(zip(parts["machine"], parts["parts"])) if not parts.empty else {}
        run = shifts.set_index("machine") if not shifts.empty else pd.DataFrame()

        e = self.events()
        latest = {}
        if not e.empty:
            for _, r in e.sort_values(["machine", "started_at"]).groupby("machine").tail(1).iterrows():
                latest[r["machine"]] = {"state": r["state"], "since": r["started_at"].tz_convert(ZONE).strftime("%H:%M")}

        machines = []
        for _, r in oee[oee["machine"] != "ALL"].iterrows():
            m = r["machine"]
            machines.append({
                "machine": m,
                "state_at_end_of_data": latest.get(m),
                "parts": int(made.get(m, 0)),
                "run_min": clean(run["run_min"].get(m)) if not run.empty else None,
                "planned_min": clean(run["planned_min"].get(m)) if not run.empty else None,
                "availability_pct": clean(r["availability_pct"]),
                "performance_pct": clean(r["performance_pct"]),
                "quality_pct": clean(r["quality_pct"]),
                "oee_pct": clean(r["oee_pct"]),
                "note": r["note"] or None,
            })

        shop = oee[oee["machine"] == "ALL"]
        shop_row = None
        if not shop.empty:
            s = shop.iloc[0]
            shop_row = {
                "parts": int(sum(made.values())),
                "availability_pct": clean(s["availability_pct"]),
                "performance_pct": clean(s["performance_pct"]),
                "quality_pct": clean(s["quality_pct"]),
                "oee_pct": clean(s["oee_pct"]),
                "note": s["note"] or None,
            }

        ranked = sorted((m for m in machines if m["oee_pct"] is not None), key=lambda m: m["oee_pct"], reverse=True)
        return {
            "period": self.period(),
            "machine_count": len(machines),
            "machines": machines,
            "shop": shop_row,
            "highest_oee": ranked[0]["machine"] if ranked else None,
            "lowest_oee": ranked[-1]["machine"] if ranked else None,
            "notes": [
                "quality_pct is 100 when no rejects were entered. 'quality not measured' means rejects were not entered.",
                "OEE = availability x performance x quality.",
            ],
        }

    def get_downtime(self, machine=None):
        machine = self.check(machine)
        e = self.events(machine)
        if e.empty:
            return {"period": self.period(), "machine": machine or "all", "reasons": [], "total_downtime_min": 0}

        table = downtime_by_reason(e).reset_index()
        reasons = rows(table)

        stops = e[e["state"].isin(["IDLE", "SETUP", "NOT_RESPONDING"])].copy()
        longest = None
        if not stops.empty:
            big = stops.loc[stops["duration_s"].idxmax()]
            reason = big["reason"] if isinstance(big["reason"], str) else "UNKNOWN"
            longest = {"machine": big["machine"], "reason": reason, "minutes": round(big["duration_s"] / 60, 1),
                       "started": big["started_at"].tz_convert(ZONE).strftime("%H:%M")}

        per_machine = None
        if machine is None:
            per_machine = [
                {"machine": m, "downtime_min": round(g["duration_s"].sum() / 60, 1), "stops": int(len(g))}
                for m, g in stops.groupby("machine")
            ]

        used = {r["reason"] for r in reasons}
        return {
            "period": self.period(),
            "machine": machine or "all machines",
            "total_downtime_min": round(stops["duration_s"].sum() / 60, 1),
            "total_stops": int(len(stops)),
            "reasons": reasons,
            "longest_stop": longest,
            "by_machine": per_machine,
            "notes": [f"{k}: {v}" for k, v in REASON_NOTES.items() if k in used]
                     + ["minutes are the time lost to each reason. share_pct is that reason's part of all downtime."],
        }

    def get_shift_summary(self, machine=None, shift=None):
        machine = self.check(machine)
        s = self.read("SELECT * FROM shift_summary")
        if machine is not None and not s.empty:
            s = s[s["machine"] == machine]
        if shift is not None and not s.empty:
            names = sorted(set(s["shift"]))
            hit = s[s["shift"].str.lower() == str(shift).strip().lower()]
            if hit.empty:
                raise ToolError(f"there is no shift called {shift}. The shifts are: {', '.join(names)}")
            s = hit
        out = []
        for r in rows(s):
            for col in ("top_reasons", "longest_stop", "notes"):
                r[col] = json.loads(r[col]) if isinstance(r[col], str) else r[col]
            out.append(r)
        return {"period": self.period(), "shifts": out}

    def get_parts(self, machine=None, program=None):
        machine = self.check(machine)
        p = self.read("SELECT * FROM parts")
        if machine is not None and not p.empty:
            p = p[p["machine"] == machine]
        if program is not None and not p.empty:
            p = p[p["program_number"] == program]
            if p.empty:
                return {"period": self.period(), "machine": machine or "all", "program": program,
                        "programs": [], "note": f"no parts of program {program} were made"}
        if p.empty:
            return {"period": self.period(), "programs": [], "note": "no part data"}

        g = p.groupby(["machine", "program_number"]).agg(
            parts=("parts_added", "sum"),
            avg_cycle_s=("cycle_s", "mean"),
            ideal_cycle_s=("ideal_s", "first"),
            avg_slower_than_ideal_pct=("slower_pct", "mean"),
        ).round(1).reset_index().rename(columns={"program_number": "program"})
        g["program"] = g["program"].astype(int)
        return {
            "period": self.period(),
            "total_parts": int(g["parts"].sum()),
            "programs": rows(g),
            "notes": ["avg_slower_than_ideal_pct is how much longer the parts took than the ideal cycle, on average."],
        }

    def get_drift(self, machine=None):
        machine = self.check(machine)
        d = self.read("SELECT * FROM drift")
        if machine is not None and not d.empty:
            d = d[d["machine"] == machine]
        runs = []
        for r in rows(d):
            r["started"] = ist(r.pop("started_at"))
            r["now_slower_than_ideal_pct"] = r.pop("now_pct")
            r["start_slower_than_ideal_pct"] = r.pop("start_pct")
            r["meaning"] = DRIFT_NOTES.get(r["status"])
            runs.append(r)
        return {"period": self.period(), "runs": runs,
                "notes": ["Each run is one program on one machine. Percentages are the average of the last 10 parts."]}

    def get_maintenance(self, machine=None):
        machine = self.check(machine)
        m = self.read("SELECT * FROM maintenance")
        if machine is not None and not m.empty:
            m = m[m["machine"] == machine]
        jobs = []
        for r in rows(m):
            left = r.pop("left_h")
            r["hours_left"] = left if left is not None and left > 0 else (0 if left is not None else None)
            r["hours_overdue"] = round(-left, 1) if left is not None and left < 0 else 0 if left is not None else None
            r["cutting_hours_since_last_done"] = r.pop("used_h")
            r["meaning"] = MAINTENANCE_NOTES.get(r["status"])
            jobs.append(r)
        return {"period": self.period(), "jobs": jobs,
                "notes": ["Due times count cutting hours, not calendar days. days_left uses the recent cutting rate."]}

    def get_failures(self, machine=None):
        machine = self.check(machine)
        f = self.read("SELECT * FROM mtbf_mttr")
        if machine is not None and not f.empty:
            f = f[f["machine"] == machine]
        return {"period": self.period(), "machines": rows(f),
                "notes": ["A failure is one alarm period. mtbf_min = run time between failures. mttr_min = time to repair.",
                          "Blank MTBF and MTTR mean there were no failures, not missing data."]}

    def get_alerts(self, status="open"):
        a = self.read("SELECT * FROM alerts")
        if a.empty:
            return {"alerts": [], "note": "no alerts have been recorded for this data"}
        if status == "open":
            a = a[a["status"] == "OPEN"]
        out = []
        for r in rows(a, ["status", "rule", "machine", "message", "opened_at", "cleared_at"]):
            r["opened"] = ist(r.pop("opened_at"))
            r["cleared"] = ist(r.pop("cleared_at"))
            out.append(r)
        return {"alerts": out}

    def get_data_coverage(self, machine=None):
        machine = self.check(machine)
        c = self.read("SELECT * FROM coverage")
        if machine is not None and not c.empty:
            c = c[c["machine"] == machine]
        return {"period": self.period(), "machines": rows(c),
                "notes": ["coverage_pct is the share of working time with readings. 100 means nothing is missing."]}


def run_tool(shop, name, arguments):
    # returns (text for the model, is_error)
    known = {t["name"] for t in TOOLS}
    if name not in known:
        return f"there is no lookup called {name}", True
    try:
        result = getattr(shop, name)(**(arguments or {}))
    except ToolError as e:
        return str(e), True
    except TypeError as e:
        return f"wrong arguments for {name}: {e}", True
    return json.dumps(result, ensure_ascii=False), False
