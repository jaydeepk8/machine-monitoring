# runs the whole system again and again, so nobody has to type commands

import argparse
import os
import time
from datetime import datetime

import alerts
import shift_message
import shift_report
from run_all import LIVE_DB, NoReadings, run
from settings import load_settings
from store import clock_mode, live_now


def one_line(tables):
    parts = int(tables["parts"]["parts_added"].sum())
    all_row = tables["oee"][tables["oee"]["machine"] == "ALL"].iloc[0]
    oee = all_row["oee_pct"]
    return f"parts {parts:>4}   availability {all_row['availability_pct']:>5}%   OEE {oee:>5}%"


def show(result):
    opened, cleared = result
    for c in opened:
        print(f"          ALERT    {c['rule']:<12}{c['message']}")
    for c in cleared:
        print(f"          cleared  {c['rule']:<12}{c['message']}")


if __name__ == "__main__":

    p = argparse.ArgumentParser()
    p.add_argument("--settings", default="settings/example_client.json")
    p.add_argument("--data", default="data/cnc_sim_data.csv")
    p.add_argument("--rejects", default=None)
    p.add_argument("--source", choices=["csv", "live"], default="live")
    p.add_argument("--every", type=float, default=10, help="seconds between runs")
    p.add_argument("--times", type=int, default=0, help="stop after this many runs (0 = never)")
    p.add_argument("--fresh", action="store_true", help="delete old alerts and shift reports before starting")
    a = p.parse_args()

    settings = load_settings(a.settings)

    # a fresh checkout has no output folder yet
    os.makedirs("output", exist_ok=True)

    if a.fresh:
        alerts.reset()
        shift_report.reset()

    print(f"running every {a.every:g} seconds from the {a.source} source. Ctrl+C to stop.")

    count = 0
    try:
        while True:
            now = datetime.now().strftime("%H:%M:%S")
            try:
                tables = run(a.settings, a.data, a.rejects, a.source)
                print(f"{now}  {one_line(tables)}")

                # alerts only make sense on live readings
                if a.source == "live":
                    show(alerts.check(settings))

                # a finished shift gets its report once
                # live readings stop when the machines do, so the clock decides
                real = a.source == "live" and clock_mode(LIVE_DB) == "real"
                for w in shift_report.check(settings, now=live_now(LIVE_DB) if real else None):
                    print(f"          REPORT   {w['shift']} {w['date']} saved to {w['file']}, "
                          f"{w['messages']} message(s) queued")
            except NoReadings as e:
                print(f"{now}  not saved: {e}")
                # nothing to calculate, which is exactly when silence must be reported
                if a.source == "live":
                    try:
                        show(alerts.check(settings, silent_only=True))
                    except Exception as e2:
                        print(f"{now}  alerts not checked: {type(e2).__name__}: {e2}")
            except Exception as e:
                # keep going. A bad moment must not stop the loop.
                print(f"{now}  not saved: {type(e).__name__}: {e}")

            # messages wait in the outbox until they go, even while the internet is down
            if settings["whatsapp"]["recipients"]:
                try:
                    for d in shift_message.deliver(settings):
                        print(f"          WHATSAPP {d['status']:<7}{d['shift']} {d['date']} to {d['name']} {d['error']}")
                except Exception as e:
                    print(f"{now}  messages not sent: {type(e).__name__}: {e}")

            count += 1
            if a.times and count >= a.times:
                break
            time.sleep(a.every)
    except KeyboardInterrupt:
        print("stopped")