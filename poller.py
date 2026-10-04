"""Cron entrypoint: poll Rivian, store snapshot, derive stats, rebuild report.

Two modes:
- default: a single poll (manual runs, backfills).
- --burst: loop for --minutes (default 48), polling every --interval
  seconds (default 120, with jitter). One burst produces ~24 snapshots,
  so an hourly restart gets 2-minute data density. Each
  snapshot commits immediately, so a worker killed mid-burst loses only
  the polls it never ran; derive + report run once at the end.

The old adaptive tier picker below is a leftover from the minute-cron
era: with an hourly cadence the elapsed time always exceeds every tier,
so each single run polls exactly once. The tier logic now only guards
against duplicate runs (e.g. a manual run landing inside the same hour
as the scheduled one). Trip detection at this cadence relies on odometer
jumps between polls; drives are bracketed, not second-by-second.
"""
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (BURST_HEARTBEAT_PATH, BURST_PID_PATH, FLAG_REAUTH,
                    HOME_LAT, HOME_LON,
                    POLL_INTERVAL_ASLEEP, POLL_INTERVAL_DRIVING,
                    POLL_INTERVAL_PARKED, SPEED_DRIVE_EPS, STATE_PATH,
                    TOKENS_PATH, WATCH_PATH)
from db import init, insert_snapshot, insert_raw_snapshot
from riv import NeedsReauth, fetch_state
from analyze import DRIVE_GEARS, haversine_mi


MOVE_GPS_MI = 0.06  # ~100 m; parked GPS jitter stays well under this


def _require_home() -> None:
    """Fail fast if home coordinates aren't configured.

    They live in the .env file (see config.py), never
    in source. Without them home detection silently degrades, which is
    worse than refusing to run.
    """
    if math.isnan(HOME_LAT) or math.isnan(HOME_LON):
        raise SystemExit(
            "RIVIAN_HOME_LAT/RIVIAN_HOME_LON not set: add them to "
            ".env and restart.")


def _burst_interval(last_snap: dict | None, prev_snap: dict | None,
                    base: float) -> int:
    """Burst cadence: base seconds normally, 30 seconds while driving.

    Movement signals (the R2's gear/speed/power fields are always null,
    so these are what we have): the odometer increased between polls, or
    GPS moved more than ~100 m. Detection lags reality by up to one poll.
    """
    if prev_snap and last_snap:
        odo_now = last_snap.get("odometer")
        odo_prev = prev_snap.get("odometer")
        if odo_now and odo_prev and odo_now > odo_prev:
            return POLL_INTERVAL_DRIVING
        la1, lo1 = last_snap.get("lat"), last_snap.get("lon")
        la0, lo0 = prev_snap.get("lat"), prev_snap.get("lon")
        if la1 and lo1 and la0 and lo0:
            try:
                if haversine_mi((la1, lo1), (la0, lo0)) > MOVE_GPS_MI:
                    return POLL_INTERVAL_DRIVING
            except Exception:
                pass
    return int(base)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state))


def is_asleep(power: str | None, gear: str | None = None,
              speed: float | None = None) -> bool:
    # The R2 reports power_state=None while in deep sleep; treat that as
    # asleep unless the gear selector shows the car is actually moving.
    if (gear or "").upper() in DRIVE_GEARS:
        return False
    # GPS speed is the reliable "is it moving" signal: gearStatus has been
    # observed null mid-drive, but the phone app shows speed, so the API has it.
    if speed is not None and speed > SPEED_DRIVE_EPS:
        return False
    return not power or "sleep" in power.lower()


def poll_interval(power: str | None, gear: str | None,
                  speed: float | None = None) -> int:
    """Adaptive tier: fast while driving (car is awake anyway, so no extra
    drain), moderate while parked-but-awake, heartbeat only when asleep."""
    if (gear or "").upper() in DRIVE_GEARS:
        return POLL_INTERVAL_DRIVING
    if speed is not None and speed > SPEED_DRIVE_EPS:
        return POLL_INTERVAL_DRIVING
    if is_asleep(power, gear, speed):
        return POLL_INTERVAL_ASLEEP
    return POLL_INTERVAL_PARKED


def main() -> None:
    init()
    _require_home()
    if FLAG_REAUTH.exists():
        return  # waiting on the owner to re-link; don't spam errors
    if not TOKENS_PATH.exists():
        return  # not linked yet

    state = load_state()
    now = int(time.time())
    last_poll = state.get("last_poll", 0)
    interval = poll_interval(state.get("last_power"), state.get("last_gear"),
                             state.get("last_speed"))
    # The cloud cache can report stale gear/power while the odometer keeps
    # moving (seen in testing: gear=None mid-drive). An odometer increase
    # between the last two polls means the car is driving: poll fast.
    prev_odo = state.get("prev_odo")
    last_odo = state.get("last_odo")
    if (prev_odo is not None and last_odo is not None
            and last_odo > prev_odo):
        interval = min(interval, POLL_INTERVAL_DRIVING)
    # Drive watch: the owner said they're about to drive. Capture at the
    # watch's interval (default 1-min driving resolution) even if the cached
    # state still says asleep, so the trip bracket starts tight. Expires on
    # its own via "until".
    try:
        watch = json.loads(WATCH_PATH.read_text())
        if now < watch.get("until", 0):
            interval = min(interval,
                           int(watch.get("interval", POLL_INTERVAL_DRIVING)))
    except Exception:
        pass
    if now - last_poll < interval:
        return

    try:
        snap, raw = fetch_state()
    except NeedsReauth:
        FLAG_REAUTH.touch()
        print("Session expired - owner needs to re-link (NEEDS_REAUTH).")
        return
    except Exception:
        traceback.print_exc()
        return

    insert_snapshot(snap)
    insert_raw_snapshot(snap["ts"], raw)

    import analyze
    trips, drains, charges, errands = analyze.derive()

    import report
    report.generate()

    save_state({"last_poll": now, "last_power": snap.get("power_state"),
                "last_gear": snap.get("gear"),
                "last_speed": snap.get("speed"),
                "prev_odo": state.get("last_odo"),
                "last_odo": snap.get("odometer"),
                "trips": len(trips), "drains": len(drains),
                "last_soc": snap.get("soc")})
    print(f"ok soc={snap.get('soc')} power={snap.get('power_state')} "
          f"trips={len(trips)} drains={len(drains)} charges={len(charges)} "
          f"errand_days={len(errands)}")


def burst_main(minutes: float = 48, interval: float = 120,
               dry_run: bool = False) -> None:
    """One burst, many polls: loop for ``minutes``, polling every
    ``interval`` seconds with jitter, dropping to 30 seconds while the car
    is moving (odometer up or GPS moved ~100 m between polls).

    Each snapshot commits immediately, so a worker killed mid-burst loses
    only the polls it never ran. Derive + report refresh after every poll
    (so the dashboard's "Updated" time tracks the latest poll) and once
    more at the end. Exits quietly if the previous burst looks still alive
    (heartbeat file under 200 s old), so a slow worker can't stack bursts.
    A lone single poll never counts: only burst mode writes the heartbeat.
    When the loop ends cleanly, the daemon re-execs itself for another
    cycle (no gap, fresh process); the supervisor remains the backstop.
    """
    import random

    init()
    _require_home()
    if FLAG_REAUTH.exists():
        return  # waiting on the owner to re-link; don't spam errors
    if not TOKENS_PATH.exists():
        return  # not linked yet

    now = int(time.time())
    try:
        beat = int(BURST_HEARTBEAT_PATH.read_text().strip())
    except Exception:
        beat = 0
    if now - beat < 200:
        print(f"burst: previous burst still polling "
              f"(heartbeat {now - beat}s ago); exiting")
        return

    import faulthandler
    faulthandler.enable()  # traceback to stderr on segfault/abort
    BURST_PID_PATH.write_text(str(os.getpid()))
    print(f"burst: pid={os.getpid()} starting {minutes}-minute cycle",
          flush=True)

    def touch_heartbeat() -> None:
        try:
            BURST_HEARTBEAT_PATH.write_text(str(int(time.time())))
        except Exception:
            pass

    deadline = time.time() + minutes * 60
    next_at = time.time()
    polls = 0
    last_snap: dict | None = None
    prev_snap: dict | None = None
    last_interval = int(interval)

    def finish_burst(tag: str) -> None:
        """Derive stats, rebuild the dashboard, clear the heartbeat.

        Runs on clean completion AND on SIGTERM, so a daemon that gets
        killed still leaves fresh derived data and a clear log line
        instead of dying silently mid-burst.
        """
        print(f"burst: entering finish ({tag})", flush=True)
        try:
            BURST_HEARTBEAT_PATH.unlink(missing_ok=True)
            BURST_PID_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        if dry_run:
            print(f"burst dry-run ok polls={polls}")
            return
        try:
            trips, drains, charges, errands = _derive_and_report()
        except Exception:
            traceback.print_exc()
            trips, drains, charges, errands = [], [], [], []
        if last_snap is not None:
            try:
                state = load_state()
                save_state({"last_poll": int(time.time()),
                            "last_power": last_snap.get("power_state"),
                            "last_gear": last_snap.get("gear"),
                            "last_speed": last_snap.get("speed"),
                            "prev_odo": state.get("last_odo"),
                            "last_odo": last_snap.get("odometer"),
                            "trips": len(trips), "drains": len(drains),
                            "last_soc": last_snap.get("soc")})
            except Exception:
                traceback.print_exc()
        print(f"{tag} polls={polls} trips={len(trips)} drains={len(drains)} "
              f"charges={len(charges)} errand_days={len(errands)}",
              flush=True)

    import signal

    def _on_term(signum, frame):
        print(f"burst: received signal {signum} after {polls} polls; "
              f"running derive+report before exit", flush=True)
        try:
            finish_burst("burst terminated")
        finally:
            raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    while next_at < deadline:
        wait = next_at - time.time()
        if wait > 0:
            time.sleep(wait)
        if FLAG_REAUTH.exists():
            break
        try:
            if dry_run:
                polls += 1
                print(f"burst poll {polls} (dry-run)", flush=True)
            else:
                snap, raw = fetch_state()
                insert_snapshot(snap)
                insert_raw_snapshot(snap["ts"], raw)
                last_snap = snap
                polls += 1
                print(f"burst poll {polls} soc={snap.get('soc')} "
                      f"power={snap.get('power_state')}", flush=True)
            touch_heartbeat()
            # Refresh derived stats + dashboard every poll so the page's
            # "Updated" time tracks the latest poll instead of lagging to
            # the burst end. Failures here must never kill the poll loop.
            try:
                _derive_and_report()
            except Exception:
                traceback.print_exc()
        except NeedsReauth:
            FLAG_REAUTH.touch()
            print("Session expired - owner needs to re-link (NEEDS_REAUTH).")
            break
        except Exception:
            traceback.print_exc()
        # Adaptive cadence: 30-sec while the car is moving (odo up or GPS
        # moved ~100m+), base interval otherwise. Log only on change.
        cur = _burst_interval(last_snap, prev_snap, interval)
        prev_snap = last_snap
        if cur != last_interval:
            print(f"burst: cadence {last_interval}s -> {cur}s", flush=True)
            last_interval = cur
        jitter = (random.uniform(-10, 20) if cur <= 60
                  else random.uniform(-20, 40))
        next_at += cur + jitter

    finish_burst("burst ok")
    if not dry_run:
        # Self-renew: re-exec into a fresh process so polling continues
        # with no gap. finish_burst cleared the heartbeat, so the new
        # image passes the overlap guard. The supervisor stays as the
        # backstop if renewal ever fails.
        try:
            script = os.path.abspath(__file__)
            argv = [sys.executable, script] + sys.argv[1:]
            print(f"burst: renewing for another {minutes} minutes",
                  flush=True)
            os.execv(sys.executable, argv)
        except Exception:
            traceback.print_exc()


def _derive_and_report():
    """Run analyze.derive() + report.generate().

    Shared by the burst loop (every poll), burst finish, and --derive-only.
    """
    import analyze
    trips, drains, charges, errands = analyze.derive()
    import report
    report.generate()
    return trips, drains, charges, errands


def parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="Rivian tracker poller")
    p.add_argument("--burst", action="store_true",
                   help="loop polls for --minutes instead of a single poll")
    p.add_argument("--minutes", type=float, default=48,
                   help="burst loop duration in minutes")
    p.add_argument("--interval", type=float, default=120,
                   help="target seconds between poll starts in burst mode")
    p.add_argument("--dry-run", action="store_true",
                   help="burst loop timing test: no API calls, no DB writes")
    p.add_argument("--derive-only", action="store_true",
                   help="run derive+report once and exit (no polling)")
    return p.parse_args(argv)


def derive_only() -> None:
    """Refresh derived tables and rebuild the dashboard without polling."""
    init()
    _require_home()
    trips, drains, charges, errands = _derive_and_report()
    print(f"derive-only ok trips={len(trips)} drains={len(drains)} "
          f"charges={len(charges)} errand_days={len(errands)}", flush=True)


if __name__ == "__main__":
    args = parse_args()
    if args.derive_only:
        derive_only()
    elif args.burst:
        burst_main(args.minutes, args.interval, args.dry_run)
    else:
        main()
