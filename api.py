"""Wattson API: charging-cost entry, history export, and dashboard login.

Run alongside the dashboard (see docker-compose.yml). The dashboard itself
is static HTML; this small Flask app lets users export sessions and record what
they paid per kWh at public DC fast chargers, which feeds the fuel-savings
math on the next dashboard regeneration. When WATTSON_DASHBOARD_TOKEN is
set it also serves the login page and the check nginx runs before every
dashboard request (see dashboard_auth.py).
"""
import os
import csv
from contextlib import closing
from datetime import datetime, timezone
import io
import ipaddress
from urllib.parse import urlparse
from flask import Flask, request, redirect, jsonify, Response
import sqlite3
import math
import time

from config import DB_PATH, PUBLIC_ORIGIN, USABLE_KWH
from db import (apply_charge_price, charge_price_info,
                reassign_charge_price)
from dashboard_auth import (SESSION_COOKIE, DashboardAuth, client_ip, is_https,
                            token_from_env)

app = Flask(__name__)

WATTSON_API_HOST = os.environ.get("WATTSON_API_HOST", "127.0.0.1")

auth = DashboardAuth(token_from_env(), time.time)

LOGIN_ERRORS = {
    "bad": "Wrong password.",
    "throttled": "Too many failed attempts. Try again in a few minutes.",
}


def _err_page(msg, code):
    """Friendly error page instead of a bare string on API failures."""
    return (
        '<html><head><meta name="viewport" content="width=device-width,'
        ' initial-scale=1"></head>'
        '<body style="background:#0b0d10;color:#fff;font-family:sans-serif;'
        'padding:40px;max-width:480px;margin:0 auto">'
        f"<p>{msg}</p>"
        '<p><a href="/#charging" style="color:#7ed321">Back to dashboard</a></p>'
        "</body></html>", code)


def _origin_triple(value):
    """(scheme, hostname, port) of an Origin/Referer value, or None.

    Ports default by scheme (443 for https, 80 for http). Anything
    without an http(s) scheme and hostname, or with an unparseable
    port, has no usable triple and can never match.
    """
    if not value:
        return None
    try:
        parts = urlparse(value)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        if scheme not in ("http", "https") or not host:
            return None
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host, port)


_TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _trusted_proxy(remote_addr):
    """True when the immediate peer is loopback or a private network.

    Only such peers may supply X-Forwarded-Proto: in production Flask
    listens on loopback behind nginx, and in Docker the peer is the
    bridge network. Anything else is an untrusted client whose
    forwarding headers must be ignored, never honored.
    """
    if not remote_addr:
        return False
    try:
        addr = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED_PROXY_NETWORKS)


def _expected_origin(request):
    """(scheme, hostname, port) the request legitimately came from.

    A configured canonical public origin wins outright: it does not
    depend on proxy headers at all. Otherwise the scheme is the
    forwarded proto, but only from a trusted proxy peer, falling back
    to the request's own scheme; the host comes from the Host header,
    which the production proxy forwards.
    """
    if PUBLIC_ORIGIN:
        triple = _origin_triple(PUBLIC_ORIGIN)
        if triple is None:
            app.logger.error("WATTSON_PUBLIC_ORIGIN is set but invalid: %r",
                             PUBLIC_ORIGIN)
            return None
        return triple
    scheme = request.scheme
    if _trusted_proxy(request.remote_addr):
        forwarded = (request.headers.get("X-Forwarded-Proto") or "")
        forwarded = forwarded.split(",")[0].strip().lower()
        if forwarded in ("http", "https"):
            scheme = forwarded
    try:
        host_parts = urlparse("//" + (request.host or ""))
        host = (host_parts.hostname or "").lower()
        port = host_parts.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host, port)


def _same_origin(request):
    """CSRF origin check for browser form POSTs.

    Compares the full origin triple (scheme, hostname, port): same
    hostname is not same origin, since cookies are not port-isolated
    and SameSite=Lax still sends cookies between ports on one host.
    An Origin header that is present must match; it is never rescued
    by a Referer. Referer is consulted only when Origin is absent
    (older browsers). Missing both fails closed.
    """
    expected = _expected_origin(request)
    if expected is None:
        return False
    if "Origin" in request.headers:
        candidate = _origin_triple(request.headers.get("Origin"))
    else:
        candidate = _origin_triple(request.headers.get("Referer"))
    return candidate is not None and candidate == expected


def _guard_charge_post(as_json=False):
    """Auth + origin checks shared by the charging-price POST endpoints.

    Returns an error response, or None when the request may proceed.
    A valid bearer token skips the origin check: bearer tokens are
    not attached automatically by browsers, so a request carrying one
    cannot be a cross-site form POST (a form cannot set an
    Authorization header, and a cross-origin fetch with one needs a
    CORS preflight this app never grants). Cookie-authenticated and
    unauthenticated requests always pass the origin check: cookies
    are ambient, and with no token configured the origin check is the
    only defense a local dashboard has. The JSON endpoint renders
    errors as JSON (as_json=True) so its AJAX caller keeps working;
    the form endpoints render the error page.
    """
    def _fail(msg, code):
        if as_json:
            return jsonify({"ok": False, "error": msg}), code
        return _err_page(msg, code)
    header = request.headers.get("Authorization")
    if auth.bearer_valid(header):
        return None
    if not _same_origin(request):
        return _fail("Could not verify this request came from the "
                     "dashboard. Please submit from the dashboard "
                     "page.", 403)
    if not auth.authorized(header, request.cookies.get(SESSION_COOKIE)):
        return _fail("Sign in to the dashboard first.", 401)
    return None


def _csv_cell(value):
    # Charger names are external text, not spreadsheet formulas.
    if isinstance(value, str) and (
            value.startswith(("\t", "\r", "\n"))
            or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


@app.route("/api/charges.csv")
def export_charges():
    if not auth.authorized(request.headers.get("Authorization"),
                           request.cookies.get(SESSION_COOKIE)):
        return "", 401
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "start_utc", "end_utc", "charge_type", "label", "minutes",
        "kwh_added", "miles_added", "avg_kw", "soc_start", "soc_end",
        "user_cost_per_kwh",
    ])
    try:
        with closing(sqlite3.connect(
                DB_PATH.resolve().as_uri() + "?mode=ro",
                uri=True, timeout=30)) as con:
            rows = con.execute(
                    "SELECT start_ts, end_ts, charge_type, label, minutes,"
                    " kwh_added, miles_added, avg_kw, soc_start, soc_end"
                    " FROM charges ORDER BY start_ts").fetchall()
            # Prices resolve through the same helper as the dashboard,
            # so the export can never disagree with it (and a price on
            # an ambiguous start is withheld here too).
            prices, _, _ = charge_price_info(
                con, [r[0] for r in rows])
            for row in rows:
                writer.writerow([
                    datetime.fromtimestamp(ts, timezone.utc).isoformat()
                    for ts in row[:2]
                ] + [_csv_cell(value) for value in row[2:]]
                  + [_csv_cell(prices.get(row[0]))])
    except sqlite3.Error:
        app.logger.exception("Charging CSV export failed")
        return _err_page("Charging history is unavailable. Please try again.",
                         503)
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = (
        'attachment; filename="wattson-charges.csv"')
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/charge-cost-form", methods=["POST"])
def set_charge_cost_form():
    guard = _guard_charge_post()
    if guard is not None:
        return guard
    # charge_id is accepted for old cached dashboards but never used
    # for identity: numeric ids are reassigned on every re-derive, and
    # trusting one is how a stale dashboard used to price the wrong
    # session. start_ts is the only key.
    request.form.get("charge_id")
    start_ts = request.form.get("start_ts")
    raw_cost = request.form.get("cost_per_kwh")
    # Empty cost clears a previously entered rate (back to unpriced).
    if raw_cost is None or str(raw_cost).strip() == "":
        cost = None
    else:
        try:
            cost = float(raw_cost)
        except (ValueError, TypeError):
            return _err_page("Enter a valid price per kWh.", 400)
        # Validate: reject NaN/Inf, allow 0.00 (free charging) through 10.00.
        if math.isnan(cost) or math.isinf(cost) or not (0.0 <= cost <= 10.0):
            return _err_page("Enter a price between 0 and 10 $/kWh.", 400)
    try:
        sts = int(start_ts)
    except (ValueError, TypeError):
        return _err_page("Invalid charging session.", 400)
    try:
        with sqlite3.connect(str(DB_PATH), timeout=30) as con:
            # Hold the write lock across the check-and-write so a
            # concurrent rebuild cannot delete the session between
            # the existence check and the upsert.
            con.execute("BEGIN IMMEDIATE")
            status = apply_charge_price(con, sts, cost)
            if status == "not_found":
                return _err_page("That charging session is no longer in "
                                 "the log. Please refresh the dashboard "
                                 "and try again.", 404)
            if status == "ambiguous":
                return _err_page("Several sessions share that start time, "
                                 "so the price was not applied to any of "
                                 "them. Please refresh and try again.", 409)
            con.commit()
    except sqlite3.OperationalError as e:
        app.logger.error("DB error: %s", e)
        return _err_page("Database is busy. Please try again in a moment.",
                         503)
    # Regenerate the dashboard so the new cost shows immediately.
    try:
        import report
        report.generate()
    except Exception:
        app.logger.exception("Dashboard regeneration failed")
    return redirect("/#charging", code=303)


@app.route("/api/charge-price-reassign", methods=["POST"])
def reassign_charge_price_form():
    """Move a saved price from an orphaned start time onto the correct
    session, or remove the orphan. Both endpoints are validated
    exactly; nothing is matched by guesswork."""
    guard = _guard_charge_post()
    if guard is not None:
        return guard
    def _as_int(value):
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    from_ts = _as_int(request.form.get("orphan_start_ts"))
    action = request.form.get("action")
    to_ts = _as_int(request.form.get("target_start_ts"))
    if from_ts is None or action not in ("move", "remove"):
        return _err_page("Invalid reassignment request.", 400)
    if action == "move" and to_ts is None:
        return _err_page("Choose a session to move the price to.", 400)
    try:
        with sqlite3.connect(str(DB_PATH), timeout=30) as con:
            con.execute("BEGIN IMMEDIATE")
            status = reassign_charge_price(con, from_ts, to_ts, action)
            if status == "orphan_not_found":
                return _err_page("That saved price is no longer there. "
                                 "Please refresh the dashboard and try "
                                 "again.", 404)
            if status == "source_attached":
                return _err_page("That saved price is attached to a "
                                 "session again. Please refresh the "
                                 "dashboard and try again.", 409)
            if status == "target_not_found":
                return _err_page("That session is no longer in the log. "
                                 "Please refresh and try again.", 404)
            if status == "target_ambiguous":
                return _err_page("Several sessions share that start time, "
                                 "so the price was not moved.", 409)
            if status == "target_priced":
                return _err_page("That session already has a price. Clear "
                                 "it first, then move this one.", 409)
            if status == "same_session":
                return _err_page("That is already the session holding this "
                                 "price.", 400)
            con.commit()
    except sqlite3.OperationalError as e:
        app.logger.error("DB error: %s", e)
        return _err_page("Database is busy. Please try again in a moment.",
                         503)
    try:
        import report
        report.generate()
    except Exception:
        app.logger.exception("Dashboard regeneration failed")
    return redirect("/#charging", code=303)


@app.route("/api/charge-cost", methods=["POST"])
def set_charge_cost():
    """JSON version of charge-cost-form for AJAX modal saves.
    Returns JSON instead of redirecting; the modal closes and the
    page reloads to show the new cost. Same start_ts-only identity
    and guard as the form endpoint; only the error rendering differs
    (JSON, so the modal keeps working)."""
    guard = _guard_charge_post(as_json=True)
    if guard is not None:
        return guard
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        # A JSON list, number, string, or boolean is not an object:
        # treat it as no usable body rather than crashing on .get().
        data = {}
    # charge_id is accepted for old cached dashboards but never used
    # for identity (see set_charge_cost_form).
    data.get("charge_id")
    start_ts = data.get("start_ts")
    cost_per_kwh = data.get("cost_per_kwh")
    raw = cost_per_kwh
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        cost = None
    else:
        # JSON booleans reach here as Python bools, and float(True)
        # is 1.0, so reject them explicitly instead of saving $1.00.
        if isinstance(raw, bool):
            return jsonify({"ok": False,
                            "error": "Enter a valid price per kWh."}), 400
        try:
            cost = float(raw)
        except (ValueError, TypeError):
            return jsonify({"ok": False,
                            "error": "Enter a valid price per kWh."}), 400
        if math.isnan(cost) or math.isinf(cost) or not (0.0 <= cost <= 10.0):
            return jsonify({"ok": False,
                            "error": "Enter a price between 0 and 10 $/kWh."}), 400
    # Same bool guard for the session key: int(True) is 1, which would
    # look up a session that does not exist instead of failing cleanly.
    if isinstance(start_ts, bool):
        return jsonify({"ok": False,
                        "error": "Invalid charging session."}), 400
    try:
        sts = int(start_ts)
    except (ValueError, TypeError):
        return jsonify({"ok": False,
                        "error": "Invalid charging session."}), 400
    try:
        with sqlite3.connect(str(DB_PATH), timeout=30) as con:
            con.execute("BEGIN IMMEDIATE")
            status = apply_charge_price(con, sts, cost)
            if status == "not_found":
                return jsonify({"ok": False, "error": "That charging session is no longer in the log. Please refresh the dashboard and try again."}), 404
            if status == "ambiguous":
                return jsonify({"ok": False, "error": "Several sessions share that start time, so the price was not applied to any of them. Please refresh and try again."}), 409
            con.commit()
    except sqlite3.OperationalError as e:
        app.logger.error("DB error: %s", e)
        return jsonify({"ok": False,
                        "error": "Database is busy. Please try again in a moment."}), 503
    # Regenerate dashboard so the reload shows the new cost immediately.
    # Cost edits are infrequent, so the few seconds this takes is worth it.
    try:
        import report
        report.generate()
    except Exception:
        app.logger.exception("Dashboard regeneration failed")
    return jsonify({"ok": True, "cost_per_kwh": cost})


def _login_page(error, code):
    message = LOGIN_ERRORS.get(error)
    err = f'<p style="color:#ff6b6b">{message}</p>' if message else ""
    return (
        '<html><head><meta name="viewport" content="width=device-width,'
        ' initial-scale=1"><title>Wattson login</title></head>'
        '<body style="background:#0b0d10;color:#fff;font-family:sans-serif;'
        'padding:40px;max-width:320px;margin:0 auto">'
        '<form method="post" action="/login"'
        ' style="display:flex;flex-direction:column;gap:12px">'
        '<h1 style="margin:0">Wattson</h1>'
        '<p style="margin:0;color:#9aa0a6">This dashboard is private.</p>'
        f"{err}"
        '<input type="password" name="password" placeholder="password"'
        ' autocomplete="current-password" autofocus'
        ' style="font-size:15px;padding:10px 12px;border:1px solid #2a2f36;'
        'border-radius:4px;background:#14181d;color:#fff">'
        '<button type="submit" style="font-size:15px;padding:10px;border:0;'
        'border-radius:4px;background:#7ed321;color:#0b0d10;'
        'font-weight:600">Sign in</button>'
        "</form></body></html>", code)


@app.route("/api/auth/check")
def auth_check():
    # nginx auth_request treats any status other than 2xx/401/403 as a
    # server error for the whole site, so this only ever says 200 or 401.
    if auth.authorized(request.headers.get("Authorization"),
                       request.cookies.get(SESSION_COOKIE)):
        return "", 200
    return "", 401


@app.route("/login", methods=["GET"])
def login_form():
    return _login_page(request.args.get("error"), 200)


@app.route("/login", methods=["POST"])
def login():
    ip = client_ip(request.headers, request.remote_addr)
    result = auth.login(ip, request.form.get("password", ""))
    if result == "bad":
        app.logger.warning("dashboard login failed from %s", ip)
        return _login_page("bad", 401)
    if result == "throttled":
        return _login_page("throttled", 429)
    # Always "/", never a caller-supplied target, so this is no open redirect.
    resp = redirect("/", code=303)
    if result == "ok":
        resp.headers["Set-Cookie"] = auth.session_cookie(
            is_https(request.headers))
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    resp = redirect("/login", code=303)
    resp.headers["Set-Cookie"] = auth.clear_cookie(is_https(request.headers))
    return resp


@app.route("/api/health")
def health():
    return jsonify({"ok": True})



# ---------------------------------------------------------------------------
# Send destination navigation (see nav.py). nginx gates /api/ when a
# dashboard token is set; these routes also check in-app (like the CSV
# export above) because they act on the car, and the API port should not
# be reachable without the token just by bypassing nginx.
# ---------------------------------------------------------------------------

@app.before_request
def _nav_gate():
    if request.path.startswith("/api/nav/"):
        # These routes read saved places and the car's live location and
        # can send it somewhere, so unlike the rest of the dashboard they
        # stay off entirely until a dashboard password is set. The
        # default no-token install gets no nav, not open nav.
        if not auth.token:
            return jsonify({"ok": False, "error": "Send destination is "
                            "off until a dashboard password is set "
                            "(WATTSON_DASHBOARD_TOKEN)."}), 403
        if not auth.authorized(request.headers.get("Authorization"),
                               request.cookies.get(SESSION_COOKIE)):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401


_saved_cache = {"ts": 0.0, "places": []}


def _car_state():
    """Latest snapshot + recent efficiency from the tracker DB."""
    car = {}
    wh_mi = None
    try:
        with closing(sqlite3.connect(str(DB_PATH), timeout=30)) as con:
            row = con.execute(
                "SELECT ts, soc, range_mi, lat, lon FROM snapshots"
                " ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                car = {"ts": row[0], "soc": row[1], "range_mi": row[2],
                       "lat": row[3], "lon": row[4]}
            agg = con.execute(
                "SELECT SUM(kwh_used), SUM(miles) FROM trips"
                " WHERE miles >= 1 AND wh_per_mi BETWEEN 100 AND 900"
                " AND start_ts > strftime('%s', 'now', '-90 days')").fetchone()
            if agg and agg[0] and agg[1]:
                wh_mi = agg[0] * 1000.0 / agg[1]
            else:
                agg = con.execute(
                    "SELECT SUM(kwh_used), SUM(miles) FROM trips"
                    " WHERE miles >= 1 AND wh_per_mi BETWEEN 100 AND 900"
                ).fetchone()
                if agg and agg[0] and agg[1]:
                    wh_mi = agg[0] * 1000.0 / agg[1]
    except sqlite3.Error:
        app.logger.exception("nav car state failed")
    return car, (round(wh_mi) if wh_mi else 380)


def _saved_places_cached():
    import time as _time
    import nav
    if _time.time() - _saved_cache["ts"] < 300 and _saved_cache["places"]:
        return _saved_cache["places"], True
    for attempt in (1, 2):
        try:
            places = nav.saved_places()
            _saved_cache.update(ts=_time.time(), places=places)
            return places, True
        except Exception:
            app.logger.exception(
                "saved places fetch failed (attempt %d)", attempt)
    return _saved_cache["places"], False


def _home_place(places):
    for p in places:
        if p.get("type") == "FAVORITE_HOME":
            return p
    try:
        from config import HOME_LAT, HOME_LON
        if HOME_LAT == HOME_LAT and HOME_LON == HOME_LON:  # not NaN
            return {"name": "Home", "address": "", "lat": HOME_LAT,
                    "lon": HOME_LON, "place_id": None, "type": "HOME",
                    "url": None}
    except Exception:
        pass
    return None


def _clean_place(data):
    """Validate a place dict from the dashboard. Returns (place, error)."""
    if not isinstance(data, dict):
        return None, "Missing place"
    url = data.get("url")
    if url:
        host = ""
        if isinstance(url, str):
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or "").lower()
        if not (isinstance(url, str) and url.startswith("https://")
                and (host in ("telenav.id", "goo.gl", "maps.app.goo.gl")
                     or host == "google.com"
                     or host.endswith(".google.com"))):
            return None, "That link is not a map link"
        return {"name": (str(data.get("name") or "Map link"))[:120],
                "address": "", "lat": None, "lon": None,
                "place_id": None, "type": "", "url": url}, None
    try:
        lat = float(data.get("lat"))
        lon = float(data.get("lon"))
    except (TypeError, ValueError):
        return None, "That place has no coordinates"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, "That place has no coordinates"
    pid = data.get("place_id")
    return {"name": (str(data.get("name") or "Destination"))[:120],
            "address": (str(data.get("address") or ""))[:240],
            "lat": lat, "lon": lon,
            "place_id": str(pid)[:80] if pid else None,
            "type": str(data.get("type") or ""), "url": None}, None


@app.route("/api/nav/state")
def nav_state():
    import nav
    car, wh_mi = _car_state()
    places, fresh = _saved_places_cached()
    out = []
    for p in places:
        q = dict(p)
        if car.get("lat") is not None and p.get("lat") is not None:
            q["distance_mi"] = round(
                nav.haversine_mi(car["lat"], car["lon"], p["lat"], p["lon"]), 1)
        out.append(q)
    return jsonify({"ok": True, "car": car, "wh_mi": wh_mi,
                    "usable_kwh": USABLE_KWH,
                    "places": out, "places_fresh": fresh})


@app.route("/api/nav/search")
def nav_search():
    import nav
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"ok": True, "results": []})
    car, _wh = _car_state()
    try:
        results = nav.search_places(q, car.get("lat"), car.get("lon"))
    except Exception:
        app.logger.exception("place search failed")
        return jsonify({"ok": False, "error": "Search failed. Try again."}), 502
    for r in results:
        if car.get("lat") is not None:
            r["distance_mi"] = round(
                nav.haversine_mi(car["lat"], car["lon"], r["lat"], r["lon"]), 1)
    results.sort(key=lambda r: r.get("distance_mi", 1e9))
    return jsonify({"ok": True, "results": results})


def _nav_post_guard():
    """POST guard: the body must be real JSON sent by our own page. A
    cross-site form post needs no CORS preflight, so without this any
    web page could make the browser POST a send to the dashboard."""
    if not request.is_json:
        return jsonify({"ok": False,
                        "error": "Expected a JSON request"}), 400
    origin = request.headers.get("Origin")
    if origin:
        from urllib.parse import urlparse
        if urlparse(origin).netloc != request.host:
            return jsonify({"ok": False,
                            "error": "Cross-origin request refused"}), 403
    return None


@app.route("/api/nav/plan", methods=["POST"])
def nav_plan():
    import nav
    guard = _nav_post_guard()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    place, err = _clean_place(data.get("place"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if place.get("lat") is None:
        return jsonify({"ok": False,
                        "error": "Planning needs a place, not a link"}), 400
    car, _wh = _car_state()
    if car.get("lat") is None or car.get("soc") is None:
        return jsonify({"ok": False, "error": "No recent car data"}), 503
    places, _fresh = _saved_places_cached()
    home = _home_place(places)
    round_trip = bool(data.get("round_trip"))
    if round_trip and home is None:
        return jsonify({"ok": False, "error": "A round trip needs a Home"
                        " place: save Home in the Rivian app, or set home"
                        " coordinates in .env."}), 400
    try:
        result = nav.plan(place, round_trip, home,
                          {"lat": car["lat"], "lon": car["lon"]}, car["soc"])
    except Exception:
        app.logger.exception("trip plan failed")
        return jsonify({"ok": False,
                        "error": "Rivian's planner did not answer."}), 502
    return jsonify(result)


@app.route("/api/nav/send", methods=["POST"])
def nav_send():
    import nav
    guard = _nav_post_guard()
    if guard:
        return guard
    data = request.get_json(silent=True) or {}
    place, err = _clean_place(data.get("place"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    round_trip = bool(data.get("round_trip"))
    # A round trip only exists in trip mode. A direct share is one-way
    # by construction and must not silently drop the return leg.
    mode = "trip" if (data.get("mode") == "trip" or round_trip) else "direct"
    # Address results have no place identity, and a bare-coordinate
    # link does not become a destination in the car: Rivian accepts
    # the share but creates no place and no route, and the parked
    # car falls back to its previous trip. Send those as a
    # structured trip instead, where the waypoint carries the name,
    # address, and coordinates directly. Saved places (place_id)
    # and pasted links (url) keep their existing paths.
    if mode == "direct" and not place.get("place_id") and not place.get("url"):
        mode = "trip"
    if mode == "trip" and place.get("lat") is None:
        return jsonify({"ok": False,
                        "error": "Trips need a place, not a link"}), 400
    if not data.get("force"):
        try:
            conflict = nav.active_trip_conflict(place)
        except Exception:
            app.logger.exception("active-trip pre-check failed")
            conflict = None
        if conflict:
            return jsonify({"ok": False, "code": "active_trip",
                            "active_trip": conflict,
                            "error": ("Car is already navigating to " + conflict +
                                      ". Send anyway?")}), 409
    car, _wh = _car_state()
    home = None
    if mode == "trip":
        # Only trip sends use the car's position (route origin)
        # and a Home place (the return leg). A direct share needs
        # neither, and must not fail or burn a Rivian call on them.
        if car.get("lat") is None:
            return jsonify({"ok": False,
                            "error": "No recent car data"}), 503
        places, _fresh = _saved_places_cached()
        home = _home_place(places)
        if round_trip and home is None:
            return jsonify({"ok": False, "error": "A round trip needs a"
                            " Home place: save Home in the Rivian app,"
                            " or set home coordinates in .env."}), 400
    job_id = nav.start_send_job(place, mode, round_trip,
                                home, {"lat": car.get("lat"), "lon": car.get("lon")})
    if job_id is None:
        return jsonify({"ok": False, "error": "A send is already in"
                        " progress. Wait for the car to confirm it."}), 409
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/nav/status")
def nav_status():
    import nav
    job = nav.job_status(request.args.get("job") or "")
    if job is None:
        return jsonify({"ok": False, "error": "Unknown job"}), 404
    return jsonify({"ok": True, "job": job})


@app.route("/api/nav/map")
def nav_map():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return "Bad coordinates", 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "Bad coordinates", 400
    try:
        zoom = int(request.args.get("z", 15))
    except (TypeError, ValueError):
        zoom = 15
    # 16 is the tile ceiling: past it the provider serves a
    # placeholder tile, which the tile cache would keep for days.
    zoom = max(3, min(16, zoom))
    try:
        import report
        img = report._render_point_map(lat, lon, z=zoom)
    except Exception:
        app.logger.exception("nav map render failed")
        img = None
    if img is None:
        return "Map unavailable", 404
    response = Response(img, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


if __name__ == "__main__":
    app.run(host=WATTSON_API_HOST, port=5000, threaded=True)
