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
from flask import Flask, request, redirect, jsonify, Response
import sqlite3
import math
import time

from config import DB_PATH
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
            for row in con.execute(
                    "SELECT start_ts, end_ts, charge_type, label, minutes,"
                    " kwh_added, miles_added, avg_kw, soc_start, soc_end,"
                    " user_cost_per_kwh FROM charges ORDER BY start_ts"):
                writer.writerow([
                    datetime.fromtimestamp(ts, timezone.utc).isoformat()
                    for ts in row[:2]
                ] + [_csv_cell(value) for value in row[2:]])
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
    charge_id = request.form.get("charge_id")
    start_ts = request.form.get("start_ts")
    raw_cost = request.form.get("cost_per_kwh")
    try:
        cid = int(charge_id)
    except (ValueError, TypeError):
        return _err_page("Invalid charge id.", 400)
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
        with sqlite3.connect(str(DB_PATH), timeout=30) as con:
            cur = con.execute(
                "UPDATE charges SET user_cost_per_kwh = ? WHERE id = ?",
                (cost, cid))
            if cur.rowcount == 0 and start_ts:
                # The dashboard HTML may predate a background re-derive,
                # which reassigns charge IDs. Fall back to the session's
                # start timestamp, which is stable across re-derives.
                try:
                    sts = int(start_ts)
                except (ValueError, TypeError):
                    sts = None
                if sts is not None:
                    cur = con.execute(
                        "UPDATE charges SET user_cost_per_kwh = ?"
                        " WHERE start_ts = ?",
                        (cost, sts))
            if cur.rowcount == 0:
                return _err_page("Charge not found. The charging log may have"
                                 " refreshed; please reload the page and try"
                                 " again.", 404)
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


if __name__ == "__main__":
    app.run(host=WATTSON_API_HOST, port=5000, threaded=True)
