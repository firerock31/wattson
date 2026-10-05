"""Wattson API: handles the DC charging-cost form POST from the dashboard.

Run alongside the dashboard (see docker-compose.yml). The dashboard itself
is static HTML; this small Flask app only exists so users can record what
they paid per kWh at public DC fast chargers, which feeds the fuel-savings
math on the next dashboard regeneration.
"""
import os
from flask import Flask, request, redirect, jsonify
import sqlite3
import math

from config import DB_PATH

app = Flask(__name__)

WATTSON_API_HOST = os.environ.get("WATTSON_API_HOST", "127.0.0.1")


def _err_page(msg, code):
    """Friendly error page instead of a bare string on form POST failures."""
    return (
        '<html><head><meta name="viewport" content="width=device-width,'
        ' initial-scale=1"></head>'
        '<body style="background:#0b0d10;color:#fff;font-family:sans-serif;'
        'padding:40px;max-width:480px;margin:0 auto">'
        f"<p>{msg}</p>"
        '<p><a href="/#charging" style="color:#7ed321">Back to dashboard</a></p>'
        "</body></html>", code)


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


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host=WATTSON_API_HOST, port=5000, threaded=True)
