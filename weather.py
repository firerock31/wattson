"""Ambient temperature for trips via Open-Meteo (free, no API key).

The R2's unofficial API reports no usable temperature fields (cabin temps
are null), so trip temps come from historical weather at the trip's start
location and hour. Results are cached in the weather_cache table: one API
call covers a whole (day, ~11 km cell), so ongoing cost is ~1 call per
driving day, and failed cells back off for an hour.
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
_API = "https://api.open-meteo.com/v1/forecast"
_NEG_TTL = 3600  # seconds to wait before retrying a failed (day, cell)
_neg = {}  # (day, lat_r, lon_r) -> timestamp of last failure


def _fetch_day(day, lat, lon):
    """Hour -> temp (°F) for one local day at one rounded location."""
    q = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "start_date": day,
        "end_date": day,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": "America/Los_Angeles",
    })
    req = urllib.request.Request(
        _API + "?" + q, headers={"User-Agent": "rivian-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    hourly = data.get("hourly") or {}
    out = {}
    for t, v in zip(hourly.get("time") or [], hourly.get("temperature_2m") or []):
        if v is not None:
            out[int(t[11:13])] = float(v)  # t like "2026-09-27T14:00"
    return out


def temp_for_trip(con, start_ts, lat, lon):
    """Ambient °F at the trip's start hour. None when unknown.

    `con` is an open db connection (used for the cache); the caller owns
    the commit.
    """
    if lat is None or lon is None:
        return None
    local = datetime.fromtimestamp(start_ts, LOCAL_TZ)
    day = local.strftime("%Y-%m-%d")
    hour = local.hour
    lat_r = round(lat, 1)
    lon_r = round(lon, 1)
    row = con.execute(
        "SELECT temp_f FROM weather_cache"
        " WHERE day=? AND hour=? AND lat_r=? AND lon_r=?",
        (day, hour, lat_r, lon_r)).fetchone()
    if row:
        return row["temp_f"]
    key = (day, lat_r, lon_r)
    if _neg.get(key, 0) > time.time() - _NEG_TTL:
        return None
    try:
        temps = _fetch_day(day, lat_r, lon_r)
    except Exception:
        _neg[key] = time.time()
        return None
    if not temps:
        _neg[key] = time.time()
        return None
    con.executemany(
        "INSERT OR REPLACE INTO weather_cache"
        " (day, hour, lat_r, lon_r, temp_f) VALUES (?,?,?,?,?)",
        [(day, h, lat_r, lon_r, v) for h, v in temps.items()])
    return temps.get(hour)
