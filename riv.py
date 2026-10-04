"""Thin wrapper around rivian-python-client: token persistence + state fetch.

Session tokens are stored encrypted (see crypto.py). The Rivian password is
NEVER stored - it is used once during `auth.py` and discarded.
"""
import asyncio
import time
from datetime import datetime

import aiohttp
import rivian.rivian as _rivmod
from rivian import Rivian

from config import PROPS, TOKENS_PATH
from crypto import unseal

# The bundled client's gnssLocation template includes an `isAuthorized`
# subfield that Rivian's current schema rejects. Patch it once at import.
_rivmod.TEMPLATE_MAP["gnssLocation"] = "{ latitude longitude timeStamp }"


class NeedsReauth(Exception):
    """Raised when Rivian rejects our session tokens."""


def _val(state: dict, name: str):
    node = state.get(name)
    if isinstance(node, dict):
        return node.get("value")
    return node


def _is_unauthenticated(exc: BaseException) -> bool:
    blob = repr(exc)
    for attr in ("args",):
        try:
            blob += repr(getattr(exc, attr, ""))
        except Exception:
            pass
    return "UNAUTHENTICATED" in blob or "UNAUTHORIZED" in blob


async def _fetch(vehicle_id: str, user_session_token: str) -> dict:
    # trust_env=True so standard HTTPS_PROXY environment variables are honored.
    async with aiohttp.ClientSession(trust_env=True) as session:
        client = Rivian(session=session)
        await client.create_csrf_token()
        # Rehydrate the persisted user session onto a fresh app session.
        client._user_session_token = user_session_token
        try:
            resp = await client.get_vehicle_state(vehicle_id, PROPS)
        except Exception as exc:
            if _is_unauthenticated(exc):
                raise NeedsReauth() from exc
            raise
        data = await resp.json()
        return data["data"]["vehicleState"]


def fetch_state() -> tuple[dict, dict]:
    """Return (normalized snapshot, raw API payload) from the Rivian API."""
    tokens = unseal(TOKENS_PATH.read_bytes())
    vehicle_id = tokens.get("vehicle_id") or tokens["vin"]
    raw = asyncio.run(_fetch(vehicle_id, tokens["user_session_token"]))

    _locnode = raw.get("gnssLocation") or {}
    # gnssLocation is NOT wrapped in {value, timeStamp} like other fields;
    # latitude/longitude sit directly on the node. Handle both shapes.
    _locval = _locnode.get("value")
    loc = _locval if isinstance(_locval, dict) else _locnode
    geo = raw.get("geoLocation")
    geo_label = geo.get("value") if isinstance(geo, dict) else geo
    # distanceToEmpty is reported in kilometers; store miles.
    rng_km = _val(raw, "distanceToEmpty")
    # cloudConnection is the sleep signal: the API serves cached state, so a
    # poll succeeding while isOnline=false means the car itself is quiet.
    # powerState has been null on every poll so far, so this replaces it.
    cc = raw.get("cloudConnection") or {}
    cc_online = cc.get("isOnline")
    cc_sync = cc.get("lastSync")
    try:
        cc_sync_ts = int(datetime.fromisoformat(
            str(cc_sync).replace("Z", "+00:00")).timestamp()) \
            if cc_sync else None
    except Exception:
        cc_sync_ts = None

    snap = {
        "ts": int(time.time()),
        "soc": _val(raw, "batteryLevel"),
        "capacity_kwh": _val(raw, "batteryCapacity"),
        "range_mi": rng_km / 1.609344 if rng_km is not None else None,
        "charger_state": _val(raw, "chargerState"),
        "charge_port": _val(raw, "chargePortState"),
        # NOTE: no live power field; "powerKW" is rejected by the API schema.
        # Charge power is derived from the SoC curve in analyze.py instead.
        "power_state": _val(raw, "powerState"),
        "gear": _val(raw, "gearStatus"),
        "lat": loc.get("latitude"),
        "lon": loc.get("longitude"),
        "speed": _val(raw, "gnssSpeed"),  # unit TBD from diagnostic drive
        "geo_label": geo_label,
        "odometer": _val(raw, "vehicleMileage"),
        "cloud_online": (1 if cc_online else 0) if cc_online is not None else None,
        "cloud_sync_ts": cc_sync_ts,
    }
    return snap, raw
