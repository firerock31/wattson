"""Derive trips, idle-drain events, charging sessions, and errand rollups.

Trips: consecutive snapshots while driving (gear or GPS movement). Only
drives of TRIP_MIN_SECONDS or longer get individual rows; shorter drives
are noise (SoC resolution makes their Wh/mi junk) and roll into a per-day
"errands" aggregate instead.

Charges: parked stretches where SoC climbs. Classified DC fast vs AC by
peak power.

Drains: long parked gaps (sleep) with measurable SoC loss.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

import weather
from config import (DC_FAST_MIN_KW, USABLE_KWH, CHARGE_STALE_SOC_MINUTES,
                    CHARGE_STALE_SKIP_SOC,
                    ODOMETER_METERS_PER_UNIT, TRIP_MIN_SECONDS,
                    LOCAL_TZ)
from db import connect, init, rebuild_derived

DRIVE_GEARS = {"D", "DRIVE", "R", "REVERSE"}
PARKED_GEARS = {"P", "PARK", "N", "NEUTRAL", ""}
TRIP_END_GAP = 5 * 60       # seconds of stillness that closes a trip.
# 5 min splits quick errands into their own trips; red lights and brief
# traffic halts still merge.
DRAIN_MIN_GAP = 2 * 3600    # seconds of parked time that counts as a drain event
CHARGE_MIN_SOC_DELTA = 0.3  # % SoC gain that counts as a charge session
# API SoC resolution is 0.1%: single-quantum ticks during sleep are noise
# (BMS re-quantization on wake), not energy flow. The stale tail below is the
# longest suffix staying within one quantum of its max.
SOC_QUANTUM = 0.1


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

    fresh, fresh_first = _fresh_sync_points(snaps)
    for run in runs:
        _charges_from_run(run, charges)
    _drains_from_trips(snaps, trips, runs, fresh, fresh_first, drains)

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
    # Distinct drive modes used during the trip, in order of first appearance
    _modes = []
    for s in cur:
        m = s.get("drive_mode")
        if m and m not in _modes:
            _modes.append(m)
    _drive_mode = " + ".join(m.title() for m in _modes) if _modes else None
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
        "drive_mode": _drive_mode,
        **_via_point(cur, origin),
        # True GPS path (bracket included) for the map, not just endpoints.
        "path": json.dumps([[s["lat"], s["lon"]] for s in cur
                            if s.get("lat") is not None
                            and s.get("lon") is not None]),
    })


def _fresh_sync_points(snaps: list[dict]) -> tuple[list[tuple[int, dict]],
                                                  list[tuple[int, dict]]]:
    """Unique cloud-synced data points: (last_points, first_points).

    Rivian's cloud replays the last synced vehicle state while the car
    sleeps, so most snapshots are echoes of one sync. A snapshot carries
    new information only when its cloud_sync_ts advances. Those sync
    points are the only honest anchors for measuring drain.

    Each sync contributes two edges, because the value under one sync
    keeps moving while the car is awake (state updates keep arriving
    under the same sync timestamp):

    - last_points: the LAST poll seen under each sync, its settled end
      state. A span's START anchor wants this: the car as it was left
      parked. (For a sync that spans a sleep, the replayed value never
      changes, so last == first.)
    - first_points: the FIRST poll seen under each sync, the car's
      state at the moment the sync advanced. A span's END anchor must
      use this edge: the last poll under the wake sync can already
      include the next drive, or several short ones (say a wake sync's
      value falls 72.4 -> 71.5 across errands before the sync advances
      again), and booking that drop as parked drain is exactly the bug
      this split exists to prevent.
    """
    first_by_sync: dict[int, dict] = {}
    last_by_sync: dict[int, dict] = {}
    for s in snaps:
        st = s.get("cloud_sync_ts")
        if st is None or s.get("soc") is None:
            continue
        if st not in first_by_sync:
            first_by_sync[st] = s  # first poll under the sync wins
        last_by_sync[st] = s  # last poll under the sync wins
    return sorted(last_by_sync.items()), sorted(first_by_sync.items())



def _drains_from_trips(snaps: list[dict], trips: list[dict],
                       runs: list[list[dict]],
                       fresh: list[tuple[int, dict]],
                       fresh_first: list[tuple[int, dict]],
                       drains: list[dict]) -> None:
    """Build drain spans from drive boundaries.

    A span runs from one drive's end to the next drive's start. Parked
    runs alone cannot define spans: the car can wake mid-park (app
    check, software update, preconditioning) without driving, and a
    wake must not close the span; only a drive does. The trailing span
    is open when the car currently sits in a parked run.
    """
    if not trips or not snaps:
        return
    currently_parked = bool(runs) and runs[-1][-1]["ts"] == snaps[-1]["ts"]
    spans = []
    for i, tr in enumerate(trips):
        t0 = tr["end_ts"]
        if i + 1 < len(trips):
            spans.append((t0, trips[i + 1]["start_ts"], False))
        elif currently_parked:
            spans.append((t0, snaps[-1]["ts"], True))
    for t0, t1, is_open in spans:
        span_snaps = [s for s in snaps if t0 <= s["ts"] <= t1]
        _drain_span(t0, t1, span_snaps, fresh, fresh_first, is_open, drains)



def _drain_span(t0: int, t1: int, span_snaps: list[dict],
                fresh: list[tuple[int, dict]],
                fresh_first: list[tuple[int, dict]], is_open: bool,
                drains: list[dict]) -> None:
    """One parked span (last drive ended -> next drive started).

    Drain is measured between fresh cloud-sync anchors only: the car's
    last report at/just after parking (start anchor) and its first
    report at/just before the next drive (end anchor). Poll timestamps
    inside the span are mostly stale replays and are never used as
    measurement endpoints; fresh syncs that happen mid-park (the car
    woke but did not drive) are not endpoints either. Spans where the
    car was plugged in are kept but carry no rate: the car tops itself
    off, so "drain" is not measurable. The still-open span (car parked
    now) has no end anchor by definition and is flagged open_span.
    """
    park_hours = (t1 - t0) / 3600.0
    if park_hours * 3600 < DRAIN_MIN_GAP:
        return
    plugged = any((s.get("charger_state") or "") in
                  ("charging_active", "charging_complete",
                   "charging_scheduled", "charging_user_stopped")
                  for s in span_snaps)
    row = {
        "sleep_ts": t0, "wake_ts": t1, "wake_count": 0,
        "hours": round(park_hours, 2),
        "soc_start": None, "soc_end": None,
        "kwh_lost": None, "watts_avg": None,
        "cd_kwh": None, "cd_watts": None,
        "steady_kwh": None, "steady_watts": None, "steady_hours": None,
        "range_start": None, "range_end": None,
        "anchor_start_ts": None, "anchor_end_ts": None,
        "pct_per_day": None,
        "plugged_in": 1 if plugged else 0,
        "approx": 0, "measured": 0, "open_span": 1 if is_open else 0,
    }
    # Start anchor: last fresh sync in [park - 2h, park + 15min].
    start = None
    for st, s in fresh:
        if st > t0 + 900:
            break
        if st >= t0 - 2 * 3600:
            start = (st, s)
    if start is not None:
        row["anchor_start_ts"] = start[0]
        row["soc_start"] = start[1].get("soc")
        row["range_start"] = start[1].get("range_mi")
    if is_open:
        drains.append(row)
        return
    # End anchor: first fresh sync at/just before the next drive starts,
    # read at its FIRST poll (fresh_first). Later polls under the wake
    # sync can already include that drive (the value keeps updating under
    # one sync timestamp while the car is awake), which would book
    # driving energy as parked drain.
    end = None
    if start is not None:
        for st, s in fresh_first:
            if st > t1 + 6 * 3600:
                break
            if st >= t1 - 900 and st > start[0]:
                end = (st, s)
                break
    if end is not None:
        row["anchor_end_ts"] = end[0]
        row["soc_end"] = end[1].get("soc")
        row["range_end"] = end[1].get("range_mi")
    if plugged or start is None or end is None:
        drains.append(row)
        return
    hours = (end[0] - start[0]) / 3600.0
    if hours <= 0:
        drains.append(row)
        return
    d_soc = (start[1].get("soc") or 0) - (end[1].get("soc") or 0)
    if d_soc < -0.15:
        # Battery gained real charge while parked (past one gauge step
        # of wake recalibration): it was plugged in, even if no charging
        # state was caught between polls. Not a drain span.
        row["plugged_in"] = 1
        drains.append(row)
        return
    if d_soc < 0:
        # Sub-step upward recalibration: call it zero, at resolution edge.
        d_soc = 0.0
        row["approx"] = 1
    cap = _capacity(span_snaps)
    kwh = d_soc / 100.0 * cap
    row["hours"] = round(hours, 2)
    row["kwh_lost"] = round(kwh, 3)
    row["watts_avg"] = round(kwh * 1000 / hours, 1)
    row["pct_per_day"] = round(d_soc / (hours / 24.0), 2)
    if d_soc <= 0.3:
        # Drop is within a few gauge steps (0.1% each): the rate is real
        # but coarse. Report marks these with a ~.
        row["approx"] = 1
    row["measured"] = 1
    drains.append(row)




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


def _trim_stale_tail(seg: list[dict]) -> list[dict]:
    """Cut a phantom flat tail off a charge segment.

    The API's "charging" state goes stale when the car sleeps mid-session,
    so SoC is the ground truth: a real session keeps SoC rising. If the
    segment ends with a flat tail (no rise beyond one SoC quantum) lasting
    at least CHARGE_STALE_SOC_MINUTES, the tail is stale API state; end the
    session at the last snapshot where SoC genuinely increased.

    Skipped when the segment ends at/above CHARGE_STALE_SKIP_SOC (trickle/
    cell balancing near full) and when the charger explicitly reported
    charging_complete (fresh, authoritative state; trust it).

    Pure function of the snapshots, so re-derives are idempotent: once a
    session is closed at the trim point it stays closed.
    """
    if len(seg) < 2:
        return seg
    if seg[-1].get("charger_state") == "charging_complete":
        return seg
    if (seg[-1].get("soc") or 0) >= CHARGE_STALE_SKIP_SOC:
        return seg
    # Longest suffix whose values stay within one quantum of the suffix max.
    # Segments are monotonic (drops > 0.05 split them), so the suffix max is
    # the last value; the update below is just belt-and-braces.
    suffix_max = seg[-1]["soc"]
    tail_start = len(seg) - 1
    # EPS: SoC arrives as float32 artifacts (e.g. 32.60000228881836), so a
    # true 0.1% step can read as 0.1000023. Keep the tolerance well under
    # one quantum (0.1) but above float noise.
    while (tail_start > 0
           and suffix_max - seg[tail_start - 1]["soc"] <= SOC_QUANTUM + 1e-3):
        tail_start -= 1
        if seg[tail_start]["soc"] > suffix_max:
            suffix_max = seg[tail_start]["soc"]
    if tail_start == 0:
        return seg  # whole segment flat; the gain check below drops it
    if seg[-1]["ts"] - seg[tail_start]["ts"] >= CHARGE_STALE_SOC_MINUTES * 60:
        # End at the last snapshot where SoC genuinely increased: tail_start
        # is the first flat snapshot, so the increase is embodied in it.
        return seg[:tail_start + 1]
    return seg


def _close_charge(seg: list[dict], charges: list[dict]) -> None:
    seg = _trim_stale_tail(seg)
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
    """Median idle draw and the set of abnormally high periods.

    Uses anchor-measured average watts only; plugged-in and unmeasured
    periods never enter the baseline. Needs a few periods before the
    baseline means anything.
    """
    def _w(d):
        return d.get("watts_avg") if d.get("measured") else None
    watts = sorted(w for w in (_w(d) for d in drains) if w)
    if len(watts) < 3:
        return None, set()
    median = watts[len(watts) // 2]
    abnormal = {d["sleep_ts"] for d in drains
                if (_w(d) or 0) > median * 1.6}
    return median, abnormal


# ---------------------------------------------------------------- 24h activity

# Activity-bar states for the daily strip (colors decided with Andy):
# moving = orange, charging = purple, parked = green (awake, responding),
# sleep = dark green (deep sleep: polls succeed but the car sends no updates),
# unknown = dark gray (poll gap/outage, or before we stored the cloud state).
ACT_MOVING = "moving"
ACT_CHARGING = "charging"
ACT_PARKED = "parked"
ACT_SLEEP = "sleep"
ACT_UNKNOWN = "unknown"
ACT_CLIMATE = "climate"

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


CLIMATE_MIN_S = 10 * 60       # 10 min minimum sustained
CLIMATE_MIN_RATE_PCT_HR = 0.4  # SoC %/hr above wind-down trickle
CLIMATE_MIN_DROP_PCT = 0.3     # SoC % loss; a single gauge step (0.1%)
                               # must not trigger a state change on its own


def _detect_climate_segments(merged, snaps):
    """Reclassify parked+awake segments as climate-running.

    A PARKED segment becomes CLIMATE when the battery drops at least
    CLIMATE_MIN_DROP_PCT over at least CLIMATE_MIN_S, at a rate of at
    least CLIMATE_MIN_RATE_PCT_HR. SoC is read only from snapshots
    inside the segment itself, so a drive's drop before the park (or a
    charge's gain after it) can never leak in. Drops and rates are
    rounded to 4 decimals first: SoC moves in 0.1% steps, and the raw
    float subtraction of two such readings can land a hair either side
    of a threshold (0.1% over 10 min is 0.60%/hr, but 0.3% over 45 min
    computes as 0.39999999999999997).

    This is INFERRED, not measured: there is no direct climate signal
    in the data. Anything that pulls >=0.4%/hr from a parked, awake
    car for 10+ minutes reads as climate: cabin HVAC, but also BMS
    thermal conditioning, an OTA update, or a 120V outlet load. And
    the check is whole-segment: a park that mixes idle time with a
    short climate run averages the two, so a long idle stretch can
    hide a real climate run and a short one can paint the whole park
    as climate. Treat the label as "sustained high parked drain",
    not proof the HVAC was on.
    """
    from bisect import bisect_left, bisect_right

    soc_pts = sorted((s["ts"], s["soc"]) for s in snaps
                     if s.get("soc") is not None)
    if not soc_pts:
        return merged
    times = [t for t, _ in soc_pts]
    out = []
    for a, b, st in merged:
        if st == ACT_PARKED and (b - a) >= CLIMATE_MIN_S:
            lo = bisect_left(times, a)        # first snap at or after a
            hi = bisect_right(times, b) - 1   # last snap at or before b
            if lo <= hi and times[lo] < b and times[hi] > a:
                ta, soc_a = soc_pts[lo]
                tb, soc_b = soc_pts[hi]
                if (tb - ta) >= CLIMATE_MIN_S:
                    drop = round(soc_a - soc_b, 4)
                    if drop >= CLIMATE_MIN_DROP_PCT:
                        rate = round(drop / ((tb - ta) / 3600.0), 4)
                        if rate >= CLIMATE_MIN_RATE_PCT_HR:
                            out.append([a, b, ACT_CLIMATE])
                            continue
        out.append([a, b, st])
    return out


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

    # Reclassify PARKED segments as CLIMATE when the car sat parked+awake
    # with the battery actively dropping (climate running, e.g. dog mode).
    # Threshold: 10+ min sustained, SoC drop rate above wind-down trickle.
    # Wind-down after parking barely moves SoC; climate pulls ~1%/hr.
    merged = _detect_climate_segments(merged, snaps)

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

