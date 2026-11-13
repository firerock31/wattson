"""Shared traffic governor for outbound Rivian cloud calls.

Every part of the tracker that talks to Rivian's cloud passes through
`slot()` first, so the
account's traffic looks like one calm client instead of five scripts:

- calls are serialized (never two governed requests in flight at once),
- consecutive calls are spaced at least MIN_GAP_S apart,
- a rate-limit signal from Rivian pauses ALL governed traffic on a
  5 / 15 / 60 minute cooldown ladder (a server-named Retry-After wins),
- callers arriving during a cooldown fail fast with CoolingDown,
- every call and cooldown event is logged to the traffic_log table.

State is one small JSON file guarded by an flock, because the callers run
in separate processes (burst daemon, API service, one-off scripts). No
daemon of our own to babysit. The governor FAILS OPEN: a missing or
corrupt state file, or any logging failure, never blocks car data.

Kill switch: RIVIAN_GOVERNOR=off disables gating (calls pass through,
still logged). Auth failures are never treated as rate limits.
"""
import fcntl
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from config import BASE, DB_PATH

STATE_PATH = Path(os.environ.get(
    "RIVIAN_GOVERNOR_STATE", BASE / "governor_state.json"))
LOCK_PATH = STATE_PATH.with_suffix(STATE_PATH.suffix + ".lock")

MIN_GAP_S = 3.0
LADDER_S = (300.0, 900.0, 3600.0)
RESET_AFTER_S = 3600.0

_now = time.time
_sleep = time.sleep
_local = threading.local()


def _fmt_until(until: float) -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(
            until, tz=ZoneInfo("America/Los_Angeles")).strftime("%-I:%M %p")
    except Exception:
        return time.strftime("%H:%M UTC", time.gmtime(until))


class CoolingDown(Exception):
    """Raised when a governed call arrives during an active cooldown.
    The message is user-facing: it surfaces verbatim in the nav send
    job's error text on the dashboard."""

    def __init__(self, until: float):
        self.until = until
        super().__init__(
            "Rivian asked us to slow down, so tracker traffic is paused. "
            f"Try again after {_fmt_until(until)}.")


def _enabled() -> bool:
    return os.environ.get("RIVIAN_GOVERNOR", "on").strip().lower() != "off"


# ---------------------------------------------------------------- state

def _read_state(fp) -> dict:
    try:
        fp.seek(0)
        raw = fp.read().strip()
        if not raw:
            return {}
        state = json.loads(raw)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}  # corrupt state fails open


def _write_state(fp, state: dict) -> None:
    fp.seek(0)
    fp.truncate()
    fp.write(json.dumps(state))
    fp.flush()


@contextmanager
def _locked_state():
    """Yield the state dict with the state file exclusively locked.

    The lock is held for the whole with-block; slot() keeps it across the
    governed call itself, which is what serializes calls across processes.
    """
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fp = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX)
        if STATE_PATH.exists():
            fp = open(STATE_PATH, "r+")
        else:
            fp = open(STATE_PATH, "w+")
        try:
            yield fp, _read_state(fp)
        finally:
            fp.close()
    finally:
        fcntl.flock(lock_fp, fcntl.LOCK_UN)
        lock_fp.close()


# ---------------------------------------------------------------- logging

_log_ready = False


def _log(caller: str, kind: str, outcome: str, detail: str = "") -> None:
    """Append one row to traffic_log. Never raises."""
    global _log_ready
    try:
        con = sqlite3.connect(DB_PATH, timeout=2)
        try:
            con.execute("PRAGMA busy_timeout = 2000")
            if not _log_ready:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS traffic_log("
                    "ts INTEGER, caller TEXT, kind TEXT, outcome TEXT,"
                    " detail TEXT)")
                _log_ready = True
            con.execute(
                "INSERT INTO traffic_log(ts, caller, kind, outcome, detail)"
                " VALUES (?, ?, ?, ?, ?)",
                (int(_now()), caller, kind, outcome, detail[:200]))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


# ---------------------------------------------------------------- signals

def _retry_after_s(exc) -> float | None:
    for holder in (exc, getattr(exc, "response", None)):
        headers = getattr(holder, "headers", None)
        if headers:
            try:
                val = headers.get("Retry-After") or headers.get("retry-after")
                if val is not None:
                    return float(val)
            except Exception:
                pass
    return None


def _looks_rate_limited(exc) -> tuple[bool, float | None]:
    """(is_rate_limit, retry_after_s). Auth failures never count."""
    if exc is None:
        return False, None
    blob = repr(exc)
    try:
        blob += repr(getattr(exc, "args", ""))
    except Exception:
        pass
    if "UNAUTHENTICATED" in blob or "UNAUTHORIZED" in blob:
        return False, None
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    if status == 429:
        return True, _retry_after_s(exc)
    low = blob.lower()
    if "429" in blob or "too many" in low or "rate limit" in low \
            or "rate_limit" in low or "throttl" in low:
        return True, _retry_after_s(exc)
    return False, None


def _apply_cooldown(state: dict, retry_after_s: float | None) -> float:
    now = _now()
    last_limit = float(state.get("last_limit_ts") or 0)
    if last_limit and now - last_limit <= RESET_AFTER_S:
        step = min(int(state.get("ladder_step") or 0) + 1, len(LADDER_S) - 1)
    else:
        step = 0
    wait = LADDER_S[step]
    if retry_after_s:
        wait = max(wait, float(retry_after_s))
    until = now + wait
    state["ladder_step"] = step
    state["last_limit_ts"] = now
    state["cooldown_until"] = until
    return until


def note_rate_limit(retry_after_s: float | None = None,
                    source: str = "") -> float:
    """Record a rate-limit signal seen outside an exception (e.g. a 429
    response status). Returns the cooldown end time."""
    with _locked_state() as (fp, state):
        until = _apply_cooldown(state, retry_after_s)
        _write_state(fp, state)
    _log(source or "governor", "cooldown", "start", f"until {until:.0f}")
    return until


def cooldown_until() -> float | None:
    """Current cooldown end, or None. Lock-free best-effort read, for the
    dashboard note; never raises."""
    try:
        state = json.loads(STATE_PATH.read_text())
        until = float(state.get("cooldown_until") or 0)
        return until if until > _now() else None
    except Exception:
        return None


# ---------------------------------------------------------------- the slot

@contextmanager
def slot(caller: str, kind: str, priority: bool = False):
    """Govern one outbound Rivian call.

    Usage:
        with governor.slot("poller", "vehicleState"):
            resp = await client.get_vehicle_state(...)

    The state lock is held across the call: that is the serialization.
    `priority` marks user-initiated traffic (nav sends, live polls); it is
    recorded in the log. Priority never overrides a hard cooldown.

    Raises CoolingDown if a cooldown is active. A rate-limit exception
    inside the block starts (or extends) the cooldown before propagating.
    """
    if getattr(_local, "depth", 0) > 0:
        # Nested governed call in the same process: the outer slot already
        # holds the lock. Count it and proceed.
        _log(caller, kind, "nested")
        yield
        return
    if not _enabled():
        try:
            yield
            _log(caller, kind, "pass")
        except BaseException as exc:
            _log(caller, kind, "error", repr(exc)[:120])
            raise
        return
    with _locked_state() as (fp, state):
        now = _now()
        until = float(state.get("cooldown_until") or 0)
        if until > now:
            _log(caller, kind, "blocked", f"until {until:.0f}")
            raise CoolingDown(until)
        last = float(state.get("last_call_ts") or 0)
        wait = last + MIN_GAP_S - now
        if wait > 0:
            _sleep(wait)
            now = _now()
        state["last_call_ts"] = now
        _write_state(fp, state)
        _local.depth = getattr(_local, "depth", 0) + 1
        try:
            yield
        except BaseException as exc:
            limited, retry_after = _looks_rate_limited(exc)
            if limited:
                new_until = _apply_cooldown(state, retry_after)
                _write_state(fp, state)
                _log(caller, kind, "rate_limited", f"until {new_until:.0f}")
                _log(caller, "cooldown", "start", f"until {new_until:.0f}")
            else:
                _log(caller, kind, "error", repr(exc)[:120])
            raise
        else:
            _log(caller, kind, "ok", "priority" if priority else "")
        finally:
            _local.depth -= 1
