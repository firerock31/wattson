"""Derive trips, idle-drain events, charging sessions, and errand rollups.

Trips: consecutive snapshots while driving (gear or GPS movement). Only
drives of TRIP_MIN_SECONDS or longer get individual rows; shorter drives
are noise (SoC resolution makes their Wh/mi junk) and roll into a per-day
"errands" aggregate instead.

Charges: parked stretches where SoC climbs. Classified DC fast vs AC by
peak power.

Drains: long parked gaps (sleep) with measurable SoC loss.
"""
import math
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import weather
from config import (DC_FAST_MIN_KW, USABLE_KWH,
                    ODOMETER_METERS_PER_UNIT, TRIP_MIN_SECONDS)
from db import connect, init, rebuild_derived

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

DRIVE_GEARS = {"D", "DRIVE", "R", "REVERSE"}
PARKED_GEARS = {"P", "PARK", "N", "NEUTRAL", ""}
TRIP_END_GAP = 5 * 60       # seconds of stillness that closes a trip.
# 5 min splits quick errands into their own trips; red lights and brief
# traffic halts still merge.
DRAIN_MIN_GAP = 2 * 3600    # seconds of parked time that counts as a drain event
CHARGE_MIN_SOC_DELTA = 0.3  # % SoC gain that counts as a charge session


def haversine_mi(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))


def _moving(s, prev) -> bool:
    if (s.get("gear") or "").upper() in DRIVE_GEARS:
        return True
    if prev and s.get("lat") and prev.get("lat"):
        dt = s["ts"] - prev["ts"]
        if 0 < dt < 900 and haversine_mi((prev["lat"], prev["lon"]),
                                         (s["lat"], s["lon"])) > GPS_MOVE_MI:
            return True
    return False


def _odo_jump_mi(s, prev) -> float:
    """Odometer increase in miles between two snapshots; 0 if unknown."""
    a = (prev or {}).get("odometer")
    b = (s or {}).get("odometer")
    if a is None or b is None or b < a:
        return 0.0
    return (b - a) * ODOMETER_METERS_PER_UNIT / 1609.344


# A drive that started between polls (e.g. while the car was on the slow
# asleep tier) leaves no in-drive snapshots, but the odometer jump is
# unambiguous. Anything above this still counts as a real drive.
ODO_JUMP_MIN_MI = 0.1

# GPS motion between two polls counts when the fix moved more than this.
GPS_MOVE_MI = 0.02
# The R2's odometer only reports whole kilometers, so a single 1000 m tick
# can be a 50 m driveway shuffle across the rounding boundary, not a real
# 0.62 mi drive. At or below one quantum the GPS path is the honest measure.
ODO_QUANTUM_M = 1000.0


def _capacity(snaps) -> float:
    # Energy math always uses Rivian's rated usable capacity. The API's
    # batteryCapacity (91.4) is not the usable figure, so snapshot values
    # are kept as raw data only (pack-health card) and never used here.
    return USABLE_KWH


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=LOCAL_TZ).strftime("%Y-%m-%d")


def _gps_path_mi(cur: list[dict]) -> float:
    miles = 0.0
    for a, b in zip(cur, cur[1:]):
        if (b["ts"] - a["ts"]) < 900 and a.get("lat") and b.get("lat"):
            miles += haversine_mi((a["lat"], a["lon"]), (b["lat"], b["lon"]))
    return miles


def _relocated(snaps: list[dict], i: int, need: int = 2, window: int = 3) -> bool:
    """Did the car genuinely relocate to snaps[i]'s position, as opposed to
    a GPS wander blip? True when the new position persists: at least `need`
    of the next `window` polls stay within GPS_MOVE_MI of it. A wandering
    fix snaps back (or jumps elsewhere); a real move stays put."""
    base = snaps[i]
    if not base.get("lat"):
        return False
    b = (base["lat"], base["lon"])
    later = snaps[i + 1:i + 1 + window]
    hits = sum(1 for s in later
               if s.get("lat")
               and haversine_mi(b, (s["lat"], s["lon"])) <= GPS_MOVE_MI)
    return hits >= min(need, len(later))


def _trip_miles(cur: list[dict]) -> float:
    # Prefer the odometer: exact, immune to GPS gaps and sampling error.
    # But it only reports whole kilometers: a single 1000 m tick can be a
    # driveway shuffle across the rounding boundary, so at or below one
    # quantum the GPS path is the honest measure when it shows motion.
    odo = [s["odometer"] for s in cur if s.get("odometer") is not None]
    odo_m = ((odo[-1] - odo[0]) * ODOMETER_METERS_PER_UNIT
             if len(odo) >= 2 and odo[-1] >= odo[0] else 0.0)
    gps_mi = _gps_path_mi(cur)
    if odo_m > ODO_QUANTUM_M:
        return odo_m / 1609.344
    if gps_mi >= GPS_MOVE_MI:
        return gps_mi
    return odo_m / 1609.344


def _endpoint_label(cur: list[dict], which: str) -> str | None:
    """Label for a trip endpoint. Asleep snapshots carry no GNSS coords,
    so walk inward from the endpoint to the nearest snapshot that has a
    usable position (e.g. the first in-drive fix after departure)."""
    import geocode
    seq = cur if which == "start" else cur[::-1]
    for s in seq:
        if which == "end" and s.get("_bracket"):
            continue  # pre-drive snapshot is not the arrival point
        lbl = geocode.label(s.get("lat"), s.get("lon"), s.get("geo_label"))
        if lbl:
            return lbl
    return None


def derive() -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    with connect() as con:
        snaps = [dict(r) for r in
                 con.execute("SELECT * FROM snapshots ORDER BY ts")]
    trips, drains, charges = [], [], []
    errands = defaultdict(lambda: {"drives": 0, "miles": 0.0, "kwh": 0.0})

    # ---- trips ----
    cur: list[dict] = []
    cur_end = -1
    prev = None
    for idx, s in enumerate(snaps):
        # Odometer fallback: bracket an unseen drive with the previous
        # snapshot so distance, energy, and endpoints stay exact. Duration
        # is then an upper bound (the drive happened somewhere inside the
        # gap), not the true drive time.
        odo_jump = _odo_jump_mi(s, prev) > ODO_JUMP_MIN_MI
        if _moving(s, prev) or odo_jump:
            if cur and s["ts"] - cur[-1]["ts"] > TRIP_END_GAP:
                _close_trip(cur, trips, errands, snaps, cur_end)
                cur = []
            if not cur and prev is not None:
                # Bracket the drive with the last pre-drive snapshot: the car
                # was parked there, so this is the true departure point
                # (usually home), not just the first poll that already shows
                # movement. _bracket_kind records how the drive was seen:
                # "odo" (odometer jump: the whole drive happened between
                # polls, so the bracket window is the honest measure) vs
                # "seen" (movement observed: timing/energy are measured from
                # the first in-motion snapshot; the bracket only corrects
                # the origin label, coords, and map path).
                cur.append({**prev, "_bracket": True,
                            "_bracket_kind": "odo" if odo_jump else "seen"})
            cur.append(s)
            cur_end = idx
        else:
            if cur and s["ts"] - cur[-1]["ts"] > TRIP_END_GAP:
                _close_trip(cur, trips, errands, snaps, cur_end)
                cur = []
        prev = s
    if cur:
        _close_trip(cur, trips, errands, snaps, cur_end)

    # ---- parked runs (shared by drains + charges) ----
    # The R2 reports gear=None while asleep; treat that as parked too.
    runs: list[list[dict]] = []
    cur: list[dict] = []
    prev = None
    for s in snaps:
        parked = (s.get("gear") or "").upper() in PARKED_GEARS
        if parked and not _moving(s, prev):
            if cur and _odo_jump_mi(s, prev) > ODO_JUMP_MIN_MI:
                # The car moved between polls without us seeing it drive:
                # close the previous parked run and start a fresh one here,
                # so driving energy never pollutes a drain measurement.
                runs.append(cur)
                cur = []
            cur.append(s)
        else:
            if cur:
                runs.append(cur)
                cur = []
        prev = s
    if cur:
        runs.append(cur)

    for run in runs:
        _drain_from_run(run, drains)
        _charges_from_run(run, charges)

    errand_rows = [{"day": day, "drives": e["drives"],
                    "miles": round(e["miles"], 2),
                    "kwh_used": round(e["kwh"], 3)}
                   for day, e in sorted(errands.items()) if e["drives"]]

    # Ambient temperature at each trip's start (cached in weather_cache,
    # so this costs ~1 API call per driving day, not per poll cycle).
    init()  # ensure weather_cache exists before the fill below
    with connect() as con:
        for t in trips:
            t["temp_f"] = weather.temp_for_trip(
                con, t["start_ts"], t.get("start_lat"), t.get("start_lon"))

    rebuild_derived(trips, drains, charges, errand_rows)
    return trips, drains, charges, errand_rows


def _via_point(cur: list[dict], origin: dict) -> dict:
    """For round trips (ends where it started), the interesting bit is where
    he went: the farthest interior GPS point from the origin, with a real
    street label. Returns keys via_lat/via_lon/via_label (Nones when n/a)."""
    import geocode
    out = {"via_lat": None, "via_lon": None, "via_label": None}
    o = (origin.get("lat"), origin.get("lon"))
    last = cur[-1]
    if (not o[0] or o[1] is None or not last.get("lat")
            or len(cur) < 3
            or haversine_mi(o, (last["lat"], last["lon"])) >= 0.5):
        return out
    best, bestd = None, 0.3
    for s in cur[1:-1]:
        if s.get("_bracket") or s.get("lat") is None:
            continue
        d = haversine_mi(o, (s["lat"], s["lon"]))
        if d > bestd:
            best, bestd = s, d
    if best is None:
        return out
    out.update(via_lat=best["lat"], via_lon=best["lon"],
               via_label=geocode.label(best["lat"], best["lon"], None))
    return out


def _close_trip(cur: list[dict], trips: list[dict], errands: dict,
                snaps: list[dict] | None = None,
                end_idx: int | None = None) -> None:
    import json
    miles = _trip_miles(cur)
    if miles < GPS_MOVE_MI:
        return  # didn't actually go anywhere
    if miles < 0.1:
        # Sub-quantum micro-drive (the 1 km odometer can't see it): only
        # real if the car actually relocated, not a GPS wander blip.
        if snaps is None or end_idx is None or not _relocated(snaps, end_idx):
            return
    # The opening bracket is the parked departure point. For odometer-jump
    # trips the drive happened entirely between polls, so the bracket window
    # is the honest measure (start time, duration, energy all include it).
    # For observed movement, timing and energy are measured from the first
    # in-motion snapshot; the bracket only corrects the origin label,
    # coords, and map path.
    drive = [s for s in cur if not s.get("_bracket")] or cur
    if cur[0].get("_bracket_kind", "odo") == "odo":
        first = cur[0]
    else:
        first = drive[0]
    last = cur[-1]
    duration = last["ts"] - first["ts"]
    cap = _capacity(cur)
    kwh = max(0.0, ((first.get("soc") or 0) - (last.get("soc") or 0)) / 100.0 * cap)
    if duration < TRIP_MIN_SECONDS:
        # Noise: roll into the day's errands, no individual row.
        e = errands[_day(first["ts"])]
        e["drives"] += 1
        e["miles"] += miles
        e["kwh"] += kwh
        return
    # A trip whose opening snapshot is a pre-drive bracket separated from the
    # first in-motion snapshot by a wide gap started somewhere inside that
    # gap: the bracketed duration is an upper bound, not the true drive time.
    bracketed = bool(cur[0].get("_bracket")
                     and (drive[0]["ts"] - cur[0]["ts"]) > 300)
    origin = cur[0]  # departure point: the bracket, or first in-motion fix
    trips.append({
        "start_ts": first["ts"], "end_ts": last["ts"],
        "miles": round(miles, 2), "kwh_used": round(kwh, 3),
        "wh_per_mi": round(kwh * 1000 / miles, 1) if miles else None,
        # Odometer miles over wall-clock duration. For bracketed trips the
        # duration is an upper bound, so this is a lower bound on speed.
        "avg_mph": round(miles / (duration / 3600.0), 1)
        if duration > 0 else None,
        "bracketed": bracketed,
        "start_lat": origin.get("lat"), "start_lon": origin.get("lon"),
        "end_lat": last.get("lat"), "end_lon": last.get("lon"),
        "start_label": _endpoint_label(cur, "start"),
        "end_label": _endpoint_label(cur, "end"),
        **_via_point(cur, origin),
        # True GPS path (bracket included) for the map, not just endpoints.
        "path": json.dumps([[s["lat"], s["lon"]] for s in cur
                            if s.get("lat") is not None
                            and s.get("lon") is not None]),
    })


def _drain_from_run(run: list[dict], drains: list[dict]) -> None:
    a, b = run[0], run[-1]
    hours = (b["ts"] - a["ts"]) / 3600.0
    if hours * 3600 < DRAIN_MIN_GAP or a.get("soc") is None or b.get("soc") is None:
        return
    if (b["soc"] or 0) > (a["soc"] or 0):
        return  # it charged while parked, not a drain
    cap = _capacity(run)
    kwh = max(0.0, (a["soc"] - b["soc"]) / 100.0 * cap)
    # Split the first hour after parking (cooldown: car still awake, thermal
    # systems settling, higher draw) from steady-state deep sleep. The split
    # point is the snapshot nearest the 1-hour mark.
    cd_kwh, cd_watts, st_kwh, st_watts, st_hours = kwh, None, None, None, None
    if hours > 1.0 and len(run) >= 3:
        target = a["ts"] + 3600
        mid = min(run[1:-1], key=lambda s: abs(s["ts"] - target))
        if mid["ts"] > a["ts"] and mid["ts"] < b["ts"] \
                and mid.get("soc") is not None:
            cd_hours = (mid["ts"] - a["ts"]) / 3600.0
            st_hours = (b["ts"] - mid["ts"]) / 3600.0
            cd_kwh = max(0.0, (a["soc"] - mid["soc"]) / 100.0 * cap)
            st_kwh = max(0.0, (mid["soc"] - b["soc"]) / 100.0 * cap)
            cd_watts = round(cd_kwh * 1000 / cd_hours, 1) if cd_hours else None
            st_watts = round(st_kwh * 1000 / st_hours, 1) if st_hours else None
    # Observed wake-ups from the SoC curve. The R2 always reports
    # power_state=None, so power transitions are blind; instead count
    # episodes of consecutive SoC drops between nearby snapshots. Slow
    # steady drain (flat pairs between drops) does not count, only bursts.
    # Tiny unlock cycles that burn <0.15% may still slip through.
    wakes = 0
    ep_drop = 0.0
    in_ep = False
    for p, s_ in zip(run, run[1:]):
        if p.get("soc") is None or s_.get("soc") is None:
            continue
        if s_["ts"] - p["ts"] > 2700:  # >45 min gap: do not bridge
            if in_ep and ep_drop >= 0.15:
                wakes += 1
            in_ep, ep_drop = False, 0.0
            continue
        if p["soc"] - s_["soc"] >= 0.1:
            in_ep = True
            ep_drop += p["soc"] - s_["soc"]
        else:
            if in_ep and ep_drop >= 0.15:
                wakes += 1
            in_ep, ep_drop = False, 0.0
    if in_ep and ep_drop >= 0.15:
        wakes += 1
    drains.append({
        "sleep_ts": a["ts"], "wake_ts": b["ts"],
        "wake_count": wakes,
        "hours": round(hours, 2),
        "soc_start": a["soc"], "soc_end": b["soc"],
        "kwh_lost": round(kwh, 3),
        "watts_avg": round(kwh * 1000 / hours, 1) if hours else 0,
        "cd_kwh": round(cd_kwh, 3),
        "cd_watts": cd_watts,
        "steady_kwh": round(st_kwh, 3) if st_kwh is not None else None,
        "steady_watts": st_watts,
        "steady_hours": round(st_hours, 2) if st_hours else None,
        "range_start": a.get("range_mi"), "range_end": b.get("range_mi"),
    })


def _charges_from_run(run: list[dict], charges: list[dict]) -> None:
    # Split the parked run into monotonically increasing SoC segments;
    # each one with meaningful gain is a charge session. The charger's own
    # charging_complete state also ends a session: without it, the flat
    # post-charge tail (car sitting at target SoC, still plugged in)
    # stretches the session to the last snapshot.
    seg: list[dict] = []
    for s in run:
        if s.get("soc") is None:
            continue
        if seg and s["soc"] < seg[-1]["soc"] - 0.05:
            _close_charge(seg, charges)
            seg = []
        seg.append(s)
        if s.get("charger_state") == "charging_complete":
            _close_charge(seg, charges)
            seg = []
    if seg:
        _close_charge(seg, charges)


def _close_charge(seg: list[dict], charges: list[dict]) -> None:
    gain = (seg[-1]["soc"] or 0) - (seg[0]["soc"] or 0)
    if gain < CHARGE_MIN_SOC_DELTA:
        return
    cap = _capacity(seg)
    a, b = seg[0], seg[-1]
    hours = (b["ts"] - a["ts"]) / 3600.0
    kwh_added = gain / 100.0 * cap
    # No live power field in the API (powerKW is rejected), so average power
    # over the session stands in for peak. Fine for DC vs AC classification:
    # a DC session averages far above what any AC wall charger can sustain.
    avg_kw = kwh_added / hours if hours > 0 else 0
    r0, r1 = a.get("range_mi"), b.get("range_mi")
    charges.append({
        "start_ts": a["ts"], "end_ts": b["ts"],
        "minutes": round((b["ts"] - a["ts"]) / 60.0, 1),
        "soc_start": a["soc"], "soc_end": b["soc"],
        "kwh_added": round(kwh_added, 2),
        "miles_added": round(r1 - r0, 1) if r0 is not None and r1 is not None else None,
        "avg_kw": round(avg_kw, 1) if avg_kw else None,
        "charge_type": "DC fast" if avg_kw >= DC_FAST_MIN_KW else "AC",
        "lat": a.get("lat"), "lon": a.get("lon"),
        "label": _endpoint_label(seg, "start"),
    })


def drain_baseline(drains: list[dict]):
    """Median idle draw and the set of abnormally high nights.

    Uses steady-state watts (post cooldown hour) when available, since the
    first hour after parking runs hotter and isn't comparable across nights.
    Needs a few nights before the baseline means anything.
    """
    def _w(d):
        return d.get("steady_watts") or d.get("watts_avg")
    watts = sorted(w for w in (_w(d) for d in drains) if w)
    if len(watts) < 3:
        return None, set()
    median = watts[len(watts) // 2]
    abnormal = {d["sleep_ts"] for d in drains
                if (_w(d) or 0) > median * 1.6}
    return median, abnormal


# ---------------------------------------------------------------- 24h activity

# Activity-bar states for the daily strip:
# moving = orange, charging = purple, parked = green (awake, responding),
# sleep = dark green (deep sleep: polls succeed but the car sends no updates),
# unknown = dark gray (poll gap/outage, or before we stored the cloud state).
ACT_MOVING = "moving"
ACT_CHARGING = "charging"
ACT_PARKED = "parked"
ACT_SLEEP = "sleep"
ACT_UNKNOWN = "unknown"

ACTIVITY_GAP_S = 15 * 60         # a poll gap longer than this is unknown...
ACTIVITY_BRIDGE_MAX_S = 6 * 60 * 60  # cap for awake-parked gaps and the tail


def _overlaps(a1, a2, b1, b2):
    return a1 < b2 and a2 > b1


def _gps_moving(s, prev):
    """GPS-only movement between two polls (no gear, no odometer)."""
    if prev and s.get("lat") and prev.get("lat"):
        dt = s["ts"] - prev["ts"]
        if 0 < dt < 900 and haversine_mi((prev["lat"], prev["lon"]),
                                         (s["lat"], s["lon"])) > GPS_MOVE_MI:
            return True
    return False


def activity_segments(now=None):
    """Classify every poll interval into a car state, merged into per-day
    segments for the 24h activity bar.

    Each interval [prev.ts, s.ts) is:
      moving   if the odometer jumped, or GPS movement confirmed by an
               adjacent interval (a lone GPS blip is a wandering fix),
      charging if it overlaps a detected charge session,
      unknown  if the poll gap exceeds ACTIVITY_GAP_S (outage, not sleep),
               except gaps whose endpoints agree the car sat still\n               (sleep bridges any length; awake-parked up to 6h),
      parked   if the car answered the cloud (online: awake, not moving),
      sleep    if polls succeeded but the car went quiet (offline: deep sleep),
      unknown  if cloud_online was never recorded (before we stored it).

    Adjacent same-state intervals merge; segments split at local midnights.
    Returns [{day, start_ts, end_ts, state}], oldest first.
    """
    from datetime import timedelta

    if now is None:
        now = datetime.now(tz=LOCAL_TZ).timestamp()
    with connect() as con:
        snaps = [dict(r) for r in con.execute(
            "SELECT ts, lat, lon, odometer, cloud_online, soc"
            " FROM snapshots ORDER BY ts")]
        charges = [dict(r) for r in con.execute(
            "SELECT start_ts, end_ts FROM charges ORDER BY start_ts")]

    # Raw per-interval signals.
    steps = []
    step_end_idx = []  # snaps index of each interval's end snapshot
    prev = None
    for idx, s in enumerate(snaps):
        if prev is not None and s["ts"] > prev["ts"]:
            steps.append({
                "a": prev["ts"], "b": s["ts"],
                "odo_jump": _odo_jump_mi(s, prev) > ODO_JUMP_MIN_MI,
                "gps_move": _gps_moving(s, prev),
                "cloud": prev.get("cloud_online"),
                "cloud_next": s.get("cloud_online"),
                "soc_delta": abs((s.get("soc") or 0) - (prev.get("soc") or 0)),
            })
            step_end_idx.append(idx)
        prev = s
    n_real = len(steps)
    tail = None
    if snaps:
        last = snaps[-1]
        if now > last["ts"]:
            tail = {"a": last["ts"], "b": now, "odo_jump": False,
                    "gps_move": False, "cloud": last.get("cloud_online"),
                    "cloud_next": last.get("cloud_online"), "soc_delta": 0.0, "is_tail": True}
            steps.append(tail)

    # Confirm movement: the odometer never lies, but a single GPS blip is
    # usually a wandering fix on a parked car, so GPS movement only counts
    # when an adjacent interval agrees or the new position persists (a real
    # sub-interval drive stays put; wander snaps back). The tail extends
    # the last confirmed state to now.
    confirmed = []
    for i in range(n_real):
        st = steps[i]
        if st["odo_jump"]:
            confirmed.append(True)
        elif not st["gps_move"]:
            confirmed.append(False)
        else:
            pm = i > 0 and (steps[i - 1]["gps_move"]
                            or steps[i - 1]["odo_jump"])
            nm = (i < n_real - 1 and (steps[i + 1]["gps_move"]
                                      or steps[i + 1]["odo_jump"]))
            if pm or nm:
                confirmed.append(True)
            else:
                confirmed.append(_relocated(snaps, step_end_idx[i]))
    if tail is not None:
        confirmed.append(confirmed[-1] if confirmed else False)

    classified = []
    for i, st in enumerate(steps):
        a, b = st["a"], st["b"]
        if confirmed[i]:
            classified.append((a, b, ACT_MOVING))
        elif any(_overlaps(a, b, c["start_ts"], c["end_ts"])
                 for c in charges):
            classified.append((a, b, ACT_CHARGING))
        elif b - a > ACTIVITY_GAP_S:
            state = ACT_UNKNOWN
            # Bridge a gap when both endpoint polls agree the car just sat
            # there: same cloud state, no odometer jump, no meaningful SoC
            # change. Sleep bridges any length (both ends asleep means the
            # middle was asleep); awake-parked bridges up to 6h; the tail
            # keeps the cap so a dead poller reads as unknown, not sleep.
            co = st["cloud"]
            agree = (co in (0, 1)
                     and co == st["cloud_next"]
                     and st["soc_delta"] < 1.0)
            if agree:
                if co == 0 and not st.get("is_tail"):
                    state = ACT_SLEEP
                elif b - a <= ACTIVITY_BRIDGE_MAX_S:
                    state = ACT_PARKED if co == 1 else ACT_SLEEP
            classified.append((a, b, state))
        elif st["cloud"] == 1:
            classified.append((a, b, ACT_PARKED))
        elif st["cloud"] == 0:
            classified.append((a, b, ACT_SLEEP))
        else:
            classified.append((a, b, ACT_UNKNOWN))

    # Merge adjacent same-state intervals.
    merged = []
    for a, b, st in classified:
        if merged and merged[-1][2] == st and merged[-1][1] >= a:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b, st])

    # Split at local midnights.
    out = []
    for a, b, st in merged:
        cur = a
        while cur < b:
            d = datetime.fromtimestamp(cur, tz=LOCAL_TZ).date()
            nxt = datetime.combine(
                d + timedelta(days=1), datetime.min.time(),
                tzinfo=LOCAL_TZ).timestamp()
            end = min(b, nxt)
            out.append({"day": d.strftime("%Y-%m-%d"),
                        "start_ts": int(cur), "end_ts": int(end),
                        "state": st})
            cur = end
    return out


if __name__ == "__main__":
    t, d, c, e = derive()
    print(f"{len(t)} trips, {len(d)} drain events, {len(c)} charges, "
          f"{len(e)} errand days")
