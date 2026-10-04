"""Reverse geocoding for trip endpoints: coordinates -> street name.

Results are cached forever in the `places` table (rounded to ~100 m), so we
only ever hit the free Nominatim service for genuinely new locations. Most
drivers make only a handful of such lookups a week.
"""
import json
import time
import urllib.parse
import urllib.request

from db import get_place_entry, set_place

ZOOM = 16  # street-level detail
_UA = "Wattson/1.0"
RETRY_FAILED_AFTER = 3600  # seconds before retrying a failed lookup
_last_call = 0.0


def _throttle(min_gap: float = 1.1) -> None:
    """Nominatim policy: at most 1 request per second."""
    global _last_call
    wait = _last_call + min_gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _round(v: float) -> int:
    return int(round(v * 1000))


def _lookup(lat: float, lon: float) -> str | None:
    params = urllib.parse.urlencode({
        "lat": lat, "lon": lon, "format": "jsonv2", "zoom": ZOOM,
    })
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/reverse?{params}",
        headers={"User-Agent": _UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    addr = data.get("address") or {}
    road = addr.get("road")
    area = (addr.get("suburb") or addr.get("neighbourhood")
            or addr.get("city") or addr.get("town") or addr.get("village"))
    if road and area:
        return f"{road}, {area}"
    return road or area or data.get("display_name")


def label(lat: float | None, lon: float | None,
          geo_label: str | None = None) -> str | None:
    """Best human label for a point: API geo label (HOME) wins, else street.

    The API reports "UNKNOWN" for any saved-location miss; treat that as no
    label so we fall through to a real reverse-geocode instead of stamping
    UNKNOWN on the trip.
    """
    if geo_label and geo_label.upper() != "UNKNOWN":
        return geo_label
    if lat is None or lon is None:
        # Nothing to reverse-geocode: keep the API's own label (usually
        # UNKNOWN) rather than pretending a lookup happened.
        return geo_label
    lat_r, lon_r = _round(lat), _round(lon)
    hit = get_place_entry(lat_r, lon_r)
    if hit is not None:
        cached, ts = hit
        if cached or time.time() - ts < RETRY_FAILED_AFTER:
            return cached or None
    _throttle()
    name = _lookup(lat, lon)
    set_place(lat_r, lon_r, name, int(time.time()))
    return name
