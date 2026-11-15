# writes the report of a shift by itself once when the shift has ended

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd

import shift_message
from settings import load_settings
from store import query

RESULTS_DB = "output/results.db"
REPORT_DIR = "output/reports"


def open_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS shift_reports (
            date TEXT, shift TEXT, window TEXT,
            written_at TEXT, file TEXT, text TEXT,
            PRIMARY KEY (date, shift)
        )
    """)


def window_end(date, window, zone):
    # "2026-11-02", "07:00-15:00" -> end time of the shift
    start_text, end_text = window.split("-")
    start = pd.Timestamp(f"{date} {start_text}", tz=zone)
    end = pd.Timestamp(f"{date} {end_text}", tz=zone)
    if end <= start:
        end += pd.Timedelta(days=1)
    return end


def data_clock(results_db):
    # the newest second the calculations have seen
    last = query(results_db, "SELECT MAX(ended_at) AS last FROM events")["last"].iloc[0]
    return None if last is None or pd.isna(last) else pd.to_datetime(last, utc=True)


def pct(value):
    # a number that could not be worked out is shown as such, not as "None%"
    return "not available" if value is None or pd.isna(value) else f"{value}%"


def write_text(client, rows):
    first = rows.iloc[0]
    lines = [f"Shift report: {first['shift']}, {first['date']} ({first['window']} IST)", client, ""]

    lines.append(f"Parts made: {int(rows['parts'].sum())}")
    lines.append("")

    for _, r in rows.iterrows():
        lines.append(f"{r['machine']}: {r['parts']} parts | OEE {pct(r['oee_pct'])} "
                     f"(availability {pct(r['availability_pct'])}, performance {pct(r['performance_pct'])})")
        lines.append(f"  Stops: {r['stops']}, {r['downtime_min']} min down")

        top = json.loads(r["top_reasons"])
        if top:
            lines.append("  Top reasons: " + ", ".join(f"{t['reason']} {t['minutes']} min" for t in top))

        longest = json.loads(r["longest_stop"])
        if longest:
            lines.append(f"  Longest stop: {longest['reason']}, {longest['minutes']} min at {longest['started_ist']}")

        for note in json.loads(r["notes"]):
            lines.append(f"  Note: {note}")
        lines.append("")

    return "\n".join(lines)


def check(settings, results_db=RESULTS_DB, report_dir=REPORT_DIR, now=None):
    # writes the report of every finished shift that has none yet
    # now = the real clock. Without it, the newest data decides.
    summary = query(results_db, "SELECT * FROM shift_summary")
    clock = now if now is not None else data_clock(results_db)
    if summary.empty or clock is None:
        return []

    zone = settings["timezone"]
    written = []
    # read before the transaction starts. A shift that ends now is judged on today's tables.
    context = shift_message.load_context(results_db) if settings["whatsapp"]["recipients"] else {}

    con = sqlite3.connect(results_db, timeout=30)
    try:
        open_table(con)
        done = {(d, s) for d, s in con.execute("SELECT date, shift FROM shift_reports")}

        for (date, shift), rows in summary.groupby(["date", "shift"]):
            if (date, shift) in done:
                continue

            window = rows.iloc[0]["window"]
            end = window_end(date, window, zone)

            # the last second of the shift is 1 second before its end
            if clock < end.tz_convert("UTC") - pd.Timedelta(seconds=1):
                continue

            text = write_text(settings["client"], rows)
            os.makedirs(report_dir, exist_ok=True)
            path = os.path.join(report_dir, f"{date}_{shift.replace(' ', '_')}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

            con.execute(
                "INSERT INTO shift_reports VALUES (?, ?, ?, ?, ?, ?)",
                (date, shift, window, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), path, text),
            )
            # the WhatsApp messages are queued with the report, so neither is saved without the other
            queued = shift_message.queue(con, settings, rows, context)
            written.append({"date": date, "shift": shift, "file": path, "messages": queued})

        con.commit()
    finally:
        con.close()

    return written


def reset(results_db=RESULTS_DB):
    # forget all written reports (the text files stay)
    con = sqlite3.connect(results_db, timeout=30)
    try:
        open_table(con)
        con.execute("DELETE FROM shift_reports")
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--settings", default="settings/example_client.json")
    p.add_argument("--reset", action="store_true", help="forget all written reports")
    a = p.parse_args()

    if a.reset:
        reset()
        print("shift reports cleared")

    for w in check(load_settings(a.settings)):
        print(f"report written: {w['shift']} {w['date']} -> {w['file']}")

    table = query(RESULTS_DB, "SELECT date, shift, window, written_at, file FROM shift_reports")
    print(table.to_string(index=False) if not table.empty else "no shift reports yet")