"""Tire pressure: awake-only Parallax grab, storage, low detection.

Design contract:
- A brief open-read-close Parallax fetch runs only when the poll flow has
  already found the car awake, at most once per hour. We never wake the
  car for tire pressure.
- FAIL-SOFT is a hard requirement: maybe_fetch_tires() never raises. Any
  failure (timeout, websocket error, car fell asleep mid-fetch) means we
  keep the previous reading and the normal poll is untouched.
- Detection levels (0 ok / 1 warn / 2 crit) are stored per tire in
  tire_alert_state; the dashboard Tires tile reads them to color each
  cell. Wattson has no notification delivery: the tile is the alert.
"""

import asyncio
import statistics
import time

import governor
from config import TIRE_TARGET_PSI

TIRES_RVM = "dynamics.tires.state"
BAR_TO_PSI = 14.5038
GRAB_TIMEOUT_S = 25          # hard cap on the websocket wait
MIN_GAP_S = 3600             # at most one grab per hour
# Warning zones relative to the configured target (config.TIRE_TARGET_PSI,
# env RIVIAN_TIRE_TARGET_PSI, default 42 PSI).
LOW_WARN_PCT = 0.10          # yellow at 10% below target (37.8 PSI @ 42)
LOW_CRIT_PCT = 0.20          # red at 20% below target (33.6 PSI @ 42; the
                             # tire-industry flat/inspect line, stricter
                             # than the 25% US TPMS legal floor)
LOW_SIBLING_DELTA_PSI = 4.0  # below the median of the other three tires
OVER_WARN_PCT = 0.15         # yellow at 15% above target (48.3 PSI @ 42).
                             # Yellow only: no red tier for over-inflation,
                             # since a red line would be the tire's sidewall
                             # max, which varies by tire and is not set here.

POSITIONS = ("fl", "fr", "rl", "rr")
_LIB_KEY = {
    "fl": "FrontLeft",
    "fr": "FrontRight",
    "rl": "RearLeft",
    "rr": "RearRight",
}


def init_tire_tables(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS tire_readings (
               ts INTEGER PRIMARY KEY,
               fl REAL, fr REAL, rl REAL, rr REAL,
               fl_status TEXT, fr_status TEXT, rl_status TEXT, rr_status TEXT
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS tire_alert_state (
               tire TEXT PRIMARY KEY,
               low INTEGER NOT NULL DEFAULT 0
           )"""
    )
    con.commit()


def latest_reading(con):
    """Latest tire_readings row as a dict, or None."""
    cur = con.execute(
        "SELECT ts, fl, fr, rl, rr, fl_status, fr_status, rl_status, rr_status"
        " FROM tire_readings ORDER BY ts DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        return None
    keys = ("ts", "fl", "fr", "rl", "rr",
            "fl_status", "fr_status", "rl_status", "rr_status")
    return dict(zip(keys, row))


async def _grab_async(vehicle_id: str, user_session_token: str):
    """Open a Parallax subscription for tires only, wait for one message,
    close. Returns the decoded tire dict or None. Never raises."""
    import aiohttp
    from rivian import Rivian
    from rivian.parallax import decode_parallax_message

    got: dict = {}
    done = asyncio.Event()

    def _cb(msg) -> None:
        try:
            node = msg["payload"]["data"]["parallaxMessages"]
            items = node if isinstance(node, list) else [node]
            for item in items:
                if item.get("rvm") == TIRES_RVM and item.get("payload"):
                    decoded = decode_parallax_message(TIRES_RVM, item["payload"])
                    if decoded:
                        got.update(decoded)
                        done.set()
        except Exception:
            pass

    client = None
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            client = Rivian(session=session)
            with governor.slot("tires", "csrf"):
                await client.create_csrf_token()
            # Same rehydration the regular fetch uses (see riv._fetch).
            client._user_session_token = user_session_token
            unsub = None
            try:
                with governor.slot("tires", "subscribe"):
                    unsub = await client.subscribe_for_parallax_messages(
                        vehicle_id, _cb, rvms=[TIRES_RVM]
                    )
                if unsub is not None:
                    await asyncio.wait_for(done.wait(), timeout=GRAB_TIMEOUT_S)
            finally:
                if unsub is not None:
                    try:
                        await unsub()
                    except Exception:
                        pass
                try:
                    await client.close()
                except Exception:
                    pass
    except Exception:
        return None
    return got or None


def _psi(bar):
    return None if bar is None else bar * BAR_TO_PSI


def _low_level(status, psi, sibling_psis) -> int:
    """0 = fine, 1 = warn (yellow), 2 = crit (red)."""
    if status is not None and str(status).upper() != "OK":
        return 2
    if psi is not None:
        if psi < TIRE_TARGET_PSI * (1 - LOW_CRIT_PCT):
            return 2
        if psi < TIRE_TARGET_PSI * (1 - LOW_WARN_PCT):
            return 1
        if len(sibling_psis) >= 2:
            if psi < statistics.median(sibling_psis) - LOW_SIBLING_DELTA_PSI:
                return 1
        if psi > TIRE_TARGET_PSI * (1 + OVER_WARN_PCT):
            return 1
    return 0


def store_and_evaluate(con, decoded: dict, now: int | None = None) -> dict:
    """Store one decoded reading and update each tire's warning level.

    Pressures are stored as reported (bar); conversion to PSI happens in
    _low_level and at display time. Returns a small summary dict for
    logging. Raises only on DB errors; callers treat the whole grab path
    as fail-soft.
    """
    now = int(time.time()) if now is None else now
    init_tire_tables(con)
    row = {}
    for pos in POSITIONS:
        key = _LIB_KEY[pos]
        row[pos] = decoded.get(f"tirePressure{key}")
        row[f"{pos}_status"] = decoded.get(f"tirePressureStatus{key}")

    # Guard against empty messages: a decoded message with no pressures
    # at all is not a reading. Storing it would zero every warning level
    # and, via latest_reading, block retry grabs for an hour, so keep
    # the previous reading untouched instead (summary has stored=False).
    if all(row[pos] is None for pos in POSITIONS):
        return {"ts": now, "psis": {pos: None for pos in POSITIONS},
                "levels": {}, "stored": False}
    # Partial message: keep the previous value for any corner that did
    # not report, rather than overwriting a good reading with a blank.
    prev = latest_reading(con)
    if prev is not None:
        for pos in POSITIONS:
            if row[pos] is None:
                row[pos] = prev[pos]
                row[f"{pos}_status"] = prev[f"{pos}_status"]

    con.execute(
        "INSERT OR REPLACE INTO tire_readings"
        " (ts, fl, fr, rl, rr, fl_status, fr_status, rl_status, rr_status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now, row["fl"], row["fr"], row["rl"], row["rr"],
         row["fl_status"], row["fr_status"], row["rl_status"], row["rr_status"]),
    )

    psis = {pos: _psi(row[pos]) for pos in POSITIONS}
    levels = {}
    for pos in POSITIONS:
        siblings = [psis[p] for p in POSITIONS if p != pos and psis[p] is not None]
        level = _low_level(row[f"{pos}_status"], psis[pos], siblings)
        levels[pos] = level
        con.execute(
            "INSERT INTO tire_alert_state (tire, low) VALUES (?, ?)"
            " ON CONFLICT(tire) DO UPDATE SET low=excluded.low",
            (pos, level),
        )

    con.commit()
    return {"ts": now, "psis": psis, "levels": levels, "stored": True}


def maybe_fetch_tires(con, tokens: dict) -> None:
    """Fail-soft hourly tire grab. Call after a poll found the car awake.

    `con` is a sqlite3 connection to the tracker DB. `tokens` is the same
    decrypted token dict riv.fetch_state uses. NEVER raises.
    """
    try:
        init_tire_tables(con)
        last = latest_reading(con)
        now = int(time.time())
        if last is not None and now - last["ts"] < MIN_GAP_S:
            return
        vehicle_id = tokens.get("vehicle_id") or tokens.get("vin")
        decoded = asyncio.run(
            _grab_async(vehicle_id, tokens["user_session_token"])
        )
        if not decoded:
            return
        summary = store_and_evaluate(con, decoded, now=now)
        if not summary.get("stored"):
            return
        psis = {k: (None if v is None else round(v, 1))
                for k, v in summary["psis"].items()}
        print(f"tires: {psis} levels={summary['levels']}")
    except Exception:
        # Fail-soft: a tire grab must never break or slow the poll.
        pass
