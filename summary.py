"""Morning summary: last night's idle drain + recent efficiency.

Stdout is relayed to the owner by the scheduled job.
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import FLAG_REAUTH
from db import connect
from analyze import drain_baseline
import json


def main() -> None:
    if FLAG_REAUTH.exists():
        print("WATTSON: session expired - re-link your "
              "Rivian account (run auth.py).")
        return

    now = int(time.time())
    with connect() as con:
        drains = [dict(r) for r in con.execute(
            "SELECT * FROM drains ORDER BY sleep_ts DESC LIMIT 7")]
        trips = [dict(r) for r in con.execute(
            "SELECT * FROM trips WHERE start_ts > ? ORDER BY start_ts DESC",
            (now - 7 * 86400,))]

    last_night = None
    # "Last night": the drain event overlapping the overnight window
    # (yesterday 6pm -> today 8am). A run that started in the afternoon
    # and ran through the night counts; a daytime-only run does not.
    today_8am = datetime.now(LOCAL_TZ).replace(hour=8, minute=0,
                                              second=0, microsecond=0)
    yest_6pm = today_8am - timedelta(hours=14)
    cands = [d for d in drains
             if d["sleep_ts"] < today_8am.timestamp()
             and d["wake_ts"] > yest_6pm.timestamp()]
    if cands:
        last_night = max(cands, key=lambda d: d["sleep_ts"])

    first_night = None
    if drains:
        first_night = min(drains, key=lambda d: d["sleep_ts"])

    test = {}
    test_path = Path(__file__).resolve().parent / "proximity_test.json"
    if test_path.exists():
        test = json.loads(test_path.read_text())

    def proximity_label(d):
        day = datetime.fromtimestamp(d["sleep_ts"], tz=LOCAL_TZ)
        date_s = day.strftime("%Y-%m-%d")
        if day.hour <= 8:
            # a drain ending in the morning belongs to the previous evening
            date_s = (day - timedelta(days=1)).strftime("%Y-%m-%d")
        for n in test.get("nights", []):
            if n["date"] == date_s:
                return (f"Proximity unlock {n['proximity'].upper()} "
                        f"({n['role']} night).")
        return ""

    median_w, abnormal = drain_baseline(drains)

    lines = ["RIVIAN TRACKER - morning summary"]
    if last_night:
        d = last_night
        rs, re_ = d.get("range_start"), d.get("range_end")
        mi_lost = (f"{rs - re_:.0f} mi of range "
                   f"({rs:.0f} -> {re_:.0f} mi)" if rs and re_ else None)
        steady = (f", steady {d['steady_watts']:.0f} W after cooldown"
                  if d.get("steady_watts") is not None else "")
        wakes = int(d.get("wake_count") or 0)
        lines.append(
            f"Last night: {d['kwh_lost']:.2f} kWh lost over {d['hours']:.1f}h "
            f"({d['soc_start']:.0f}% -> {d['soc_end']:.0f}%)"
            + (f", {mi_lost}" if mi_lost else "") +
            f", avg draw {d['watts_avg']:.0f} W{steady}.")
        lines.append(f"Observed wake-ups overnight: {wakes} "
                     f"(brief, low-energy wake-ups may not register).")
        plabel = proximity_label(d)
        if plabel:
            lines.append(plabel)
        if d["sleep_ts"] in abnormal:
            lines.append("Note: last night's drain was unusually high "
                         f"vs your baseline ({median_w:.0f} W).")
        if first_night and first_night["sleep_ts"] != d["sleep_ts"]:
            f = first_night
            lines.append(
                f"Vs first recorded night: {f['kwh_lost']:.2f} kWh over "
                f"{f['hours']:.1f}h, avg draw {f['watts_avg']:.0f} W.")
    else:
        lines.append("Last night: no full sleep period recorded yet.")

    if trips:
        mi = sum(t["miles"] for t in trips)
        kwh = sum(t["kwh_used"] for t in trips)
        lines.append(f"Last 7 days: {len(trips)} trips, {mi:.0f} mi, "
                     f"{mi / kwh:.2f} mi/kWh average.")
    else:
        lines.append("No trips recorded in the last 7 days.")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
