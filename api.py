"""Wattson API: handles the DC charging-cost form POST from the dashboard.

Run alongside the dashboard (see docker-compose.yml). The dashboard itself
is static HTML; this small Flask app only exists so users can record what
they paid per kWh at public DC fast chargers, which feeds the fuel-savings
math on the next dashboard regeneration.
"""
from flask import Flask, request, redirect, jsonify
import sqlite3
import math

import os

from config import DB_PATH

app = Flask(__name__)

WATTSON_API_HOST = os.environ.get("WATTSON_API_HOST", "127.0.0.1")


@app.route("/api/charge-cost-form", methods=["POST"])
def set_charge_cost_form():
    charge_id = request.form.get("charge_id")
    cost_per_kwh = request.form.get("cost_per_kwh")
    try:
        cid = int(charge_id)
        cost = float(cost_per_kwh)
    except (ValueError, TypeError):
        return "Invalid values", 400
    # Validate: reject NaN/Inf, allow 0.00 (free charging) through 10.00.
    if math.isnan(cost) or math.isinf(cost) or not (0.0 <= cost <= 10.0):
        return "Invalid cost value", 400
    try:
        with sqlite3.connect(str(DB_PATH), timeout=30) as con:
            cur = con.execute(
                "UPDATE charges SET user_cost_per_kwh = ? WHERE id = ?",
                (cost, cid))
            if cur.rowcount == 0:
                return "Charge not found", 404
            con.commit()
    except sqlite3.OperationalError as e:
        app.logger.error("DB error: %s", e)
        return "Database busy", 503
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
    app.run(host=WATTSON_API_HOST, port=5000)
