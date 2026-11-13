"""Send-to-car navigation: place search, saved places, trip planning,
sending, and confirmation by watching the car's own route report.

How this works (reverse engineered from the Rivian app's traffic by
walthowd/rivian-gen2-key-poc, MIT; reimplemented here on our existing
session plumbing):

- Everything rides the normal signed-in session. No paired key, no
  Bluetooth. Requests go to Rivian's cloud; the cloud wakes the car
  (requiresWakeup) and delivers.
- One destination: ParseAndShareLocationToVehicle takes a LINK, the
  same share-sheet trick the app uses. A telenav.id link when we have
  a Google place id, else a Google Maps link with the coordinates.
  Search results with no place id are sent as a trip instead: a
  bare-coordinate link does not become a destination in the car.
- A trip: PlanTrip2 asks Rivian's planner for a preview (arrival
  charge, charging stops it would add); sendTripToVehicle sends the
  waypoints and the car plans the route and charging stops itself.
- The cloud answers "success" even when the car never got the send,
  so confirmation means watching the Parallax topic
  navigation.navigation_service.trip_info until the car itself
  reports the route. A send the car missed while falling asleep is
  re-sent once.
"""
import asyncio
import base64
import json

import governor
import math
import threading
import time
import urllib.parse
import urllib.request
import uuid

TRIP_INFO_RVM = "navigation.navigation_service.trip_info"
TARGET_SOC = 20.0  # arrival charge the car's planner aims for

# Charging networks the app plans with (ids as the app sends them).
NETWORKS = ("10001", "10025", "10027", "10030", "10031", "10032",
            "10056", "10045", "10050", "10002", "10033")

_UA = "Wattson/1.0 (open-source Rivian energy tracker)"
_last_search = 0.0
_search_lock = threading.Lock()

SAVED_Q = (
    "query savedLocations($vehicleId: String!, $userId: String!) { mapSearchSavedLocations("
    "vehicleId: $vehicleId, userId: $userId) { name userGivenName locationType address coordinate "
    "{ latitude longitude } id } }"
)
SHARE_MUT = (
    "mutation ParseAndShareLocationToVehicle($locationUrl: String!, $vehicleId: String!, "
    "$requiresWakeup: Boolean) { parseAndShareLocationToVehicle(str: $locationUrl "
    "vehicleId: $vehicleId requiresWakeup: $requiresWakeup) { publishResponse { result } } }"
)
SEND_TRIP_MUT = (
    "mutation sendTripToVehicle($attrs: SendTripToVehicleInput!) { sendTripToVehicle(attrs: $attrs) }"
)
PLAN_Q = (
    "query PlanTrip2($waypoints: [RequestWaypointInput!]!, $vehicle: String!, $startingSoc: Float, "
    "$startingRangeMeters: Float, $targetArrivalSocPercent: Float, $driveMode: DriveMode, "
    "$networkPreferences: [NetworkPreference!], $hasAdapter: Boolean, $routePreferences: RoutePreferences, "
    "$departureTime: UnixTimestamp) { planTrip2(waypoints: $waypoints vehicle: $vehicle "
    "startingSoc: $startingSoc startingRangeMeters: $startingRangeMeters "
    "targetArrivalSocPercent: $targetArrivalSocPercent driveMode: $driveMode "
    "networkPreferences: $networkPreferences hasAdapter: $hasAdapter routePreferences: $routePreferences "
    "departureTime: $departureTime) { plans { planIdentifierMetadata { abrpRouteId legs { abrpLegId "
    "routeRequest } } summary { arrivalSOCPercent destinationReachable totalChargeDurationSeconds "
    "totalDriveDistanceMeters totalTripDurationSeconds } waypoints { arrivalSOCPercent arrivalTimeUTC "
    "charger { chargeDurationSeconds entityId name } isSystemAdded latitude longitude waypointType } } "
    "status } }"
)


# ---------------------------------------------------------------------------
# Session plumbing (mirrors riv.py: fresh client, rehydrated user session)
# ---------------------------------------------------------------------------

_ids_cache: dict = {}


async def _open_client():
    import aiohttp
    from rivian import Rivian
    import rivian.rivian as _rivmod
    _rivmod.TEMPLATE_MAP["gnssLocation"] = "{ latitude longitude timeStamp }"
    from crypto import unseal
    from config import TOKENS_PATH

    tokens = unseal(TOKENS_PATH.read_bytes())
    session = aiohttp.ClientSession(trust_env=True)
    client = Rivian(session=session)
    with governor.slot("nav", "csrf", priority=True):
        await client.create_csrf_token()
    # Rehydrate the persisted user session onto the fresh app session
    # (same approach as riv._fetch).
    client._user_session_token = tokens["user_session_token"]
    client._nav_session = session  # ours to close in _close(); see below
    return client, tokens


async def _close(client) -> None:
    try:
        await client.close()
    except Exception:
        pass
    try:
        await client._nav_session.close()
    except Exception:
        pass


async def _ids(client, tokens) -> tuple[str, str]:
    """(user_id, vehicle_id) for our vehicle, cached per process."""
    if _ids_cache:
        return _ids_cache["user_id"], _ids_cache["vehicle_id"]
    with governor.slot("nav", "userInfo", priority=True):
        resp = await client.get_user_information()
    payload = await resp.json()
    user = (payload.get("data") or {}).get("currentUser") or {}
    vehicle_id = None
    for v in user.get("vehicles") or []:
        if v.get("vin") == tokens.get("vin"):
            vehicle_id = v.get("id")
            break
    if not user.get("id") or not vehicle_id:
        raise RuntimeError("Could not resolve Rivian user/vehicle ids")
    _ids_cache.update(user_id=user["id"], vehicle_id=vehicle_id)
    return user["id"], vehicle_id


async def _gql(client, operation, query, variables):
    import rivian.rivian as _rivmod
    headers = dict(_rivmod.BASE_HEADERS)
    headers.update({"Csrf-Token": client._csrf_token,
                    "A-Sess": client._app_session_token,
                    "U-Sess": client._user_session_token})
    body = {"operationName": operation, "query": query, "variables": variables}
    with governor.slot("nav", operation, priority=True):
        resp = await client._Rivian__graphql_query(
            headers, _rivmod.GRAPHQL_GATEWAY, body)
    return (await resp.json()).get("data") or {}


# ---------------------------------------------------------------------------
# Places: saved (from the Rivian account) and search (OpenStreetMap)
# ---------------------------------------------------------------------------

def _norm_saved(loc) -> dict | None:
    coord = loc.get("coordinate") or {}
    lat, lon = coord.get("latitude"), coord.get("longitude")
    if lat is None or lon is None:
        return None
    return {
        "name": loc.get("userGivenName") or loc.get("name") or "Saved place",
        "address": loc.get("address") or "",
        "lat": lat, "lon": lon,
        "place_id": loc.get("id") or None,
        "type": loc.get("locationType") or "",
        "url": None,
    }


async def _saved_places_async() -> list[dict]:
    client, tokens = await _open_client()
    try:
        user_id, vehicle_id = await _ids(client, tokens)
        data = await _gql(client, "savedLocations", SAVED_Q,
                          {"userId": user_id, "vehicleId": vehicle_id})
        out = []
        for loc in data.get("mapSearchSavedLocations") or []:
            place = _norm_saved(loc)
            if place:
                out.append(place)
        return out
    finally:
        await _close(client)


def saved_places() -> list[dict]:
    return asyncio.run(_saved_places_async())


def search_places(query: str, near_lat=None, near_lon=None) -> list[dict]:
    """Forward search via Nominatim (same service and etiquette as
    geocode.py: descriptive UA, at most 1 request/second)."""
    global _last_search
    with _search_lock:
        wait = _last_search + 1.1 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_search = time.monotonic()
    params = {"q": query, "format": "jsonv2", "addressdetails": 1,
              "limit": 6, "countrycodes": "us"}
    if near_lat is not None and near_lon is not None:
        params["viewbox"] = (f"{near_lon - 1.6},{near_lat + 1.1},"
                             f"{near_lon + 1.6},{near_lat - 1.1}")
        params["bounded"] = 0
    req = urllib.request.Request(
        "https://nominatim.openstreetmap.org/search?"
        + urllib.parse.urlencode(params),
        headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    out = []
    seen = set()
    for r in rows:
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (round(lat, 4), round(lon, 4))
        if key in seen:
            continue
        seen.add(key)
        display = r.get("display_name") or ""
        # Address results carry no name from Nominatim, so the old
        # fallback used only the house number, which is what the car
        # then displayed. Use the full address as the name instead,
        # so the car shows the address.
        name = (r.get("name") or "").strip() or display
        out.append({"name": name, "address": display, "lat": lat,
                    "lon": lon, "place_id": None, "type": "", "url": None})
    return out


# ---------------------------------------------------------------------------
# Sending: share link (one destination) and trip (car plans charging)
# ---------------------------------------------------------------------------

def share_url(place: dict) -> str:
    if place.get("url"):
        return place["url"]
    coords = f"{place['lat']},{place['lon']}"
    if not place.get("place_id"):
        return "https://www.google.com/maps/search/?" + urllib.parse.urlencode(
            {"api": 1, "query": coords})
    query = {"geo_coordinates": coords, "entity_id": place["place_id"],
             "name": place.get("name") or "", "nav_coordinates": coords,
             "address": place.get("address") or "", "source_type": "telenav"}
    return "https://telenav.id/entity?" + urllib.parse.urlencode(
        {k: v for k, v in query.items() if v})


async def _share_async(client, vehicle_id, place) -> bool:
    data = await _gql(client, "ParseAndShareLocationToVehicle", SHARE_MUT,
                      {"locationUrl": share_url(place),
                       "vehicleId": vehicle_id, "requiresWakeup": True})
    node = data.get("parseAndShareLocationToVehicle") or {}
    return ((node.get("publishResponse") or {}).get("result")) == 0


def _trip_waypoints(origin, stops):
    def waypoint(stop):
        address = {"address": stop.get("address") or "",
                   "addressSourceId": stop.get("place_id") or "",
                   "latitude": stop["lat"], "longitude": stop["lon"],
                   "name": stop.get("name") or ""}
        if stop.get("place_id"):
            address["addressSourceType"] = "google"
        return {"address": address}
    waypoints = [{"address": {"address": "", "addressSourceId": "",
                              "latitude": origin["lat"],
                              "longitude": origin["lon"],
                              "name": "Vehicle location"}}]
    waypoints += [waypoint(s) for s in stops]
    waypoints[-1]["minArrivalSoC"] = TARGET_SOC
    return waypoints


async def _send_trip_async(client, vehicle_id, origin, stops) -> bool:
    attrs = {
        "preferences": {"avoidAdapterRequired": False,
                        "networks": [{"networkId": n} for n in NETWORKS]},
        "requiresWakeup": True,
        "routePreferences": {"avoidFerries": False, "avoidHighways": False,
                             "avoidTolls": False},
        "vehicleId": vehicle_id,
        "waypoints": _trip_waypoints(origin, stops),
    }
    data = await _gql(client, "sendTripToVehicle", SEND_TRIP_MUT,
                      {"attrs": attrs})
    return bool(data.get("sendTripToVehicle"))


async def _plan_async(client, vehicle_id, origin, stops, soc) -> dict:
    def waypoint(stop, **extra):
        return {**extra, "entityId": stop.get("place_id") or "",
                "latitude": stop["lat"], "longitude": stop["lon"],
                "waypointType": "OTHER"}
    variables = {
        "departureTime": str(int(time.time() * 1000)),
        "driveMode": "EVERYDAY", "hasAdapter": True,
        "networkPreferences": [{"networkId": n, "preference": 1}
                               for n in NETWORKS],
        "routePreferences": {"avoidFerries": False, "avoidHighways": False,
                             "avoidTolls": False},
        "startingRangeMeters": 0, "startingSoc": soc,
        "targetArrivalSocPercent": TARGET_SOC,
        "vehicle": vehicle_id,
        "waypoints": [waypoint(origin, bearingDegrees=0)]
                     + [waypoint(s) for s in stops],
    }
    data = await _gql(client, "PlanTrip2", PLAN_Q, variables)
    result = data.get("planTrip2") or {}
    plans = result.get("plans") or []
    if not plans:
        return {"ok": False, "error": "Rivian's planner returned no plan."}
    plan = plans[0]
    summary = plan.get("summary") or {}
    chargers = []
    arrivals = []
    for wp in plan.get("waypoints") or []:
        arrivals.append(wp.get("arrivalSOCPercent"))
        charger = wp.get("charger")
        if wp.get("isSystemAdded") and charger:
            chargers.append({
                "name": charger.get("name") or "Charger",
                "minutes": round((charger.get("chargeDurationSeconds") or 0) / 60),
            })
    return {
        "ok": True,
        "reachable": bool(summary.get("destinationReachable")),
        "arrival_soc": summary.get("arrivalSOCPercent"),
        "final_arrival_soc": arrivals[-1] if arrivals else None,
        "distance_mi": (summary.get("totalDriveDistanceMeters") or 0) / 1609.344,
        "drive_minutes": round((summary.get("totalTripDurationSeconds") or 0) / 60),
        "charge_minutes": round((summary.get("totalChargeDurationSeconds") or 0) / 60),
        "chargers": chargers,
    }


def plan(place: dict, round_trip: bool, home: dict | None,
         origin: dict, soc: float) -> dict:
    stops = [place] + ([home] if round_trip and home else [])
    async def run():
        client, tokens = await _open_client()
        try:
            _, vehicle_id = await _ids(client, tokens)
            return await _plan_async(client, vehicle_id, origin, stops, soc)
        finally:
            await _close(client)
    return asyncio.run(run())


# ---------------------------------------------------------------------------
# trip_info: decode the car's own route report (protobuf, layout per the
# POC's byte-verified notes: legs at 3, destinations at 3.3.1, chargers
# at 3.3.2; destination = {1: {1: lat, 2: lng}, 4: name, 5: place id})
# ---------------------------------------------------------------------------

def _group(data: bytes) -> dict:
    from rivian.parallax import _decode_protobuf_fields
    grouped: dict[int, list] = {}
    for num, _wire, value in _decode_protobuf_fields(data):
        grouped.setdefault(num, []).append(value)
    return grouped


def parse_trip_info(data: bytes) -> dict | None:
    if not data:
        return None
    destinations, chargers = [], []
    for leg in _group(data).get(3, []):
        if not isinstance(leg, bytes):
            continue
        for wrapper in _group(leg).get(3, []):
            if not isinstance(wrapper, bytes):
                continue
            wfields = _group(wrapper)
            for dest in wfields.get(1, []):
                if not isinstance(dest, bytes):
                    continue
                df = _group(dest)
                name = df.get(4, [b""])[0]
                name = name.decode("utf-8", "replace") if isinstance(name, bytes) else ""
                if not name:
                    continue
                lat = lon = None
                coords = df.get(1, [None])[0]
                if isinstance(coords, bytes):
                    cf = _group(coords)
                    lat = cf.get(1, [None])[0]
                    lon = cf.get(2, [None])[0]
                pid = df.get(5, [None])[0]
                place = {"name": name, "lat": lat, "lon": lon,
                         "place_id": pid.decode("utf-8", "replace")
                         if isinstance(pid, bytes) else None}
                if not destinations or destinations[-1]["name"] != name:
                    destinations.append(place)
            for chg in wfields.get(2, []):
                if not isinstance(chg, bytes):
                    continue
                cf = _group(chg)
                name = cf.get(2, [b""])[0]
                name = name.decode("utf-8", "replace") if isinstance(name, bytes) else ""
                seconds = cf.get(4, [0])[0]
                if name:
                    chargers.append({"name": name,
                                     "minutes": round((seconds or 0) / 60)})
    if not destinations:
        return None
    return {"destinations": destinations, "chargers": chargers}


class _RouteWatch:
    """Collects the latest trip_info payload from a Parallax subscription."""

    def __init__(self):
        self.route: bytes | None = None
        self.changed = asyncio.Event()

    def cb(self, msg) -> None:
        try:
            node = msg["payload"]["data"]["parallaxMessages"]
            items = node if isinstance(node, list) else [node]
            for item in items:
                if item.get("rvm") == TRIP_INFO_RVM:
                    self.route = base64.b64decode(item.get("payload") or "")
                    self.changed.set()
        except Exception:
            pass

    async def wait_for(self, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            self.changed.clear()
            if self.route is not None and predicate(self.route):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                return False


def _round4(value):
    """Coordinates to about 11 m. Protobuf float noise between two
    reports of the same route must not change its fingerprint."""
    return round(value, 4) if isinstance(value, (int, float)) else value


def _route_fingerprint(route: dict | None):
    """Parsed-plan fingerprint for send confirmation: destinations
    (name, coordinates to 4 places, place id) and charging stops
    (names only). A re-report of the same plan fingerprints equal
    even when the raw report bytes differ (traffic refresh, wake
    cycles), which is what keeps a re-report from confirming a
    send the car may never have received. Charger minutes are
    deliberately excluded: a re-plan that only changes how long
    the car charges is still the same plan, and counting it as
    changed would let a re-planned stale route confirm a send."""
    if not route:
        return None
    dests = tuple((d.get("name"), _round4(d.get("lat")),
                   _round4(d.get("lon")), d.get("place_id"))
                  for d in route.get("destinations") or [])
    chgs = tuple(c.get("name")
                 for c in route.get("chargers") or [])
    return (dests, chgs)


def _names(route: dict | None) -> set:
    if not route:
        return set()
    return {d["name"].strip().lower() for d in route["destinations"]}


class _Confirmer:
    """Decides whether a route report confirms the send.

    Built from the route captured before the send. When no route
    was captured (the car reported nothing during the baseline
    wait), the first later report that does not name the sent
    place becomes the baseline instead of confirming anything: a
    waking car re-reports the route it was already on, and that
    stale route must not pass as confirmation. A report naming
    the sent place still confirms in that case; the car was
    either already going there or the send landed, and both are
    the state the user asked for.
    """

    def __init__(self, baseline: dict | None, want: str,
                 home: str = "", want_coords=None):
        self._want = want
        self._home = home
        # Coordinates of the sent destination, when known (trips and
        # named shares carry them; link sends do not). A route report
        # to these coordinates confirms even when the car names the
        # place differently from the sent name (e.g. the planner
        # resolves an address waypoint to its business name).
        self._want_coords = want_coords
        self._have_baseline = baseline is not None
        self._names = _names(baseline)
        self._plan = _route_fingerprint(baseline)

    @staticmethod
    def _name_match(a: str, b: str) -> bool:
        return bool(a and b and (a in b or b in a))

    def _matches(self, name: str) -> bool:
        return self._name_match(self._want, name)

    def _matches_any(self, names) -> bool:
        return any(self._matches(n) for n in names if n)

    def _coords_match(self, route) -> bool:
        # A destination at the sent place's expected position within
        # a tenth of a mile counts as the same place. Link sends
        # carry no coordinates, so this stays off for them and name
        # matching is unchanged there. Only the expected position
        # counts: a foreign multi-stop route passing near the sent
        # place on its way elsewhere must not confirm. One-way
        # sends are single-stop, so the final destination is the
        # sent place. Round-trip sends put the place first and home
        # last, so the first stop is the sent place and the route
        # must end at home: a single reported leg is the car still
        # driving to the place, but any destination after home
        # means the car's actual final destination is elsewhere.
        if not self._want_coords:
            return False
        dests = route.get("destinations") or []
        if not dests:
            return False
        wlat, wlon = self._want_coords

        def close(d):
            lat, lon = d.get("lat"), d.get("lon")
            return lat is not None and lon is not None and \
                haversine_mi(wlat, wlon, lat, lon) <= 0.1

        if self._home:
            if not close(dests[0]):
                return False
            if len(dests) == 1:
                return True
            return len(dests) == 2 and self._name_match(
                self._home,
                (dests[1].get("name") or "").strip().lower())
        return close(dests[-1])

    def _home_ok(self, names) -> bool:
        # A round trip counts as confirmed only by a route that
        # also brings the car home. Without a baseline, a
        # pre-existing one-way route to the sent place would
        # otherwise pass for the round trip that was sent.
        return not self._home or any(
            self._name_match(self._home, n) for n in names if n)

    def check(self, raw: bytes) -> bool:
        route = parse_trip_info(raw)
        if not route:
            return False
        names = _names(route)
        if not self._have_baseline:
            # Name matching only here. A coords match without a
            # baseline cannot distinguish "our send landed" from
            # "the car was already going nearby", so a stale nearby
            # route must adopt as the baseline below, not confirm.
            if self._matches_any(names) and self._home_ok(names):
                return True
            self._names = names
            self._plan = _route_fingerprint(route)
            self._have_baseline = True
            return False
        # A destination that was not in the baseline confirms
        # only when it is the place that was sent. Any other
        # transition (the driver picked somewhere else, a
        # cloud-side waypoint change) is not our send landing.
        new = names - self._names
        if new and any(self._matches(n) for n in new):
            return True
        # A route to the sent coordinates confirms even under a
        # different name, provided the plan itself changed: an
        # identical re-report still must not confirm a send.
        if self._coords_match(route) and \
                _route_fingerprint(route) != self._plan:
            return True
        # Same destination as the baseline counts only when the
        # plan itself changed (see _route_fingerprint) and the
        # report is about the place we sent. A re-send that
        # reproduces the identical plan stays unconfirmed (the
        # job re-sends once, then fails): a false negative here
        # is safer than a false positive.
        return bool(self._want and
                    _route_fingerprint(route) != self._plan and
                    self._matches_any(names))


async def _send_and_confirm_async(place, mode, round_trip, home, origin,
                                  job):
    """Send once, watch for the car's own confirmation, re-send once if
    the car never reports the route (the send can be lost while the car
    is falling asleep; the cloud's 'success' proves nothing)."""
    client, tokens = await _open_client()
    try:
        _, vehicle_id = await _ids(client, tokens)
        watch = _RouteWatch()
        with governor.slot("nav", "subscribe", priority=True):
            unsub = await client.subscribe_for_parallax_messages(
                vehicle_id, watch.cb, rvms=[TRIP_INFO_RVM])
        if unsub is None:
            raise RuntimeError("Could not subscribe to the car's route reports")
        try:
            # The subscription starts with the current route; grab it as
            # the baseline so a stale route can't pass as confirmation.
            await watch.wait_for(lambda r: True, 10)
            baseline = parse_trip_info(watch.route) if watch.route else None
            confirmer = _Confirmer(
                baseline, (place.get("name") or "").strip().lower(),
                (home.get("name") or "").strip().lower()
                if round_trip and home else "",
                (place.get("lat"), place.get("lon"))
                if place.get("lat") is not None
                and place.get("lon") is not None else None)

            stops = [place] + ([home] if round_trip and home else [])
            for attempt in (1, 2):
                if mode == "trip":
                    ok = await _send_trip_async(client, vehicle_id, origin, stops)
                else:
                    ok = await _share_async(client, vehicle_id, place)
                if not ok:
                    raise RuntimeError("Rivian's cloud did not accept the send")
                # Rivian accepted the send into its cloud. That is
                # not proof the car received it (only the car's own
                # route report proves that), but it is the honest
                # "sent" state: the cloud holds it until the car
                # wakes or reboots and picks it up.
                job["cloud_accepted"] = True
                if await watch.wait_for(confirmer.check, 50):
                    return parse_trip_info(watch.route)
            return None
        finally:
            try:
                await unsub()
            except Exception:
                pass
    finally:
        await _close(client)


# ---------------------------------------------------------------------------
# Send jobs: the HTTP handler returns immediately; a thread runs the
# send + confirmation watch and the dashboard polls the job state.
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_STALE_S = 600  # a send still running past this is wedged, not slow


def _reap_stale_locked() -> None:
    """Fail any send job stuck in 'sending' past the stale limit.
    The caller holds _JOBS_LOCK. A wedged job (a network wait that
    never returns) must not block every later send until the
    process restarts. Jobs without a timestamp count as fresh."""
    now = time.time()
    for j in _JOBS.values():
        if j.get("state") == "sending" \
                and now - j.get("ts", now) > _JOB_STALE_S:
            j["state"] = "failed"
            j["error"] = "The send timed out."


async def _current_route_async(timeout: float = 15):
    client, tokens = await _open_client()
    try:
        _, vehicle_id = await _ids(client, tokens)
        watch = _RouteWatch()
        with governor.slot("nav", "subscribe", priority=True):
            unsub = await client.subscribe_for_parallax_messages(
                vehicle_id, watch.cb, rvms=[TRIP_INFO_RVM])
        if unsub is None:
            return None
        try:
            await watch.wait_for(lambda r: True, timeout)
            return parse_trip_info(watch.route) if watch.route else None
        finally:
            try:
                await unsub()
            except Exception:
                pass
    finally:
        await _close(client)


def active_trip_conflict(place: dict, timeout: float = 6) -> str | None:
    """Name of the car's current active destination when it differs
    from the place about to be sent, else None.

    Read-only: subscribes to the car's route report and unsubscribes.
    Fail-open: any error, or a place without coordinates, means no
    conflict (the send proceeds and the job's own confirmation still
    applies). A destination counts as the same place when its name
    matches (either-direction substring) or its coordinates are
    within half a mile of the sent place.
    """
    lat = place.get("lat")
    lon = place.get("lon")
    if lat is None or lon is None:
        return None
    try:
        route = asyncio.run(_current_route_async(timeout))
    except Exception:
        return None
    if not route:
        return None
    want = (place.get("name") or "").strip().lower()
    dests = route.get("destinations") or []
    for dest in dests:
        name = (dest.get("name") or "").strip().lower()
        if want and name and _Confirmer._name_match(want, name):
            return None
        dlat, dlon = dest.get("lat"), dest.get("lon")
        if dlat is not None and dlon is not None:
            try:
                if haversine_mi(lat, lon, dlat, dlon) < 0.5:
                    return None
            except (TypeError, ValueError):
                pass
    if dests:
        return (dests[0].get("name") or "that destination").strip()
    return None


def start_send_job(place: dict, mode: str, round_trip: bool,
                   home: dict | None, origin: dict) -> str | None:
    """Start a send job and return its id, or None when another
    send is already in flight. The busy check and the job insert
    happen under the same lock, so two sends starting at the same
    moment cannot both get through."""
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "state": "sending", "route": None, "error": None,
           "place_name": place.get("name") or "destination", "mode": mode,
           "cloud_accepted": False,
           "ts": time.time()}
    with _JOBS_LOCK:
        _reap_stale_locked()
        if any(j.get("state") == "sending" for j in _JOBS.values()):
            return None
        _JOBS[job_id] = job
        while len(_JOBS) > 20:
            _JOBS.pop(next(iter(_JOBS)))

    def run():
        try:
            route = asyncio.run(_send_and_confirm_async(
                place, mode, round_trip, home, origin, job))
            with _JOBS_LOCK:
                if route:
                    job["state"] = "confirmed"
                    job["route"] = route
                elif job.get("cloud_accepted"):
                    # Accepted by Rivian's cloud but the car never
                    # reported a route in the window. That is not a
                    # failure: the cloud holds the send until the
                    # car wakes. Report it as sent, not failed.
                    job["state"] = "sent"
                    job["error"] = None
                else:
                    job["state"] = "failed"
                    job["error"] = ("The car never confirmed the route. "
                                    "It may not have received the send.")
        except Exception as exc:  # surfaced to the dashboard as-is
            with _JOBS_LOCK:
                if job.get("cloud_accepted"):
                    # An accepted send does not un-happen because a
                    # later attempt failed: the cloud still holds
                    # it, so the job is sent, not failed. Failed
                    # now means nothing was ever accepted.
                    job["state"] = "sent"
                    job["error"] = None
                else:
                    job["state"] = "failed"
                    job["error"] = str(exc) or exc.__class__.__name__

    threading.Thread(target=run, daemon=True).start()
    return job_id


def job_status(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def haversine_mi(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
