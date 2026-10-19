import csv
from contextlib import closing
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import api
import db
from dashboard_auth import DashboardAuth


class ChargeCsvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "tracker.db"
        with closing(sqlite3.connect(self.database)) as con:
            con.executescript(db.SCHEMA)
        for target, value in [
            ("api.DB_PATH", self.database),
            ("api.auth", DashboardAuth(None, lambda: 1700000000)),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = api.app.test_client()

    def charge(self, timestamp, price=None, label="Test charger"):
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute(
                "INSERT INTO charges (start_ts, end_ts, charge_type, label,"
                " minutes, kwh_added, user_cost_per_kwh)"
                " VALUES (?, ?, 'DC', ?, 30, 10, ?)",
                (timestamp, timestamp + 1800, label, price))

    def rows(self, response):
        return list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))

    def test_exports_all_sessions_in_order_with_blank_and_free_prices(self):
        label = 'Test, "caf\u00e9"\ncharger'
        for i in reversed(range(21)):
            self.charge(1700000000 + i * 3600, 0 if i else None, label)
        response = self.client.get("/api/charges.csv")
        self.assertEqual(response.status_code, 200)
        rows = self.rows(response)
        self.assertEqual(len(rows), 21)
        self.assertLess(rows[0]["start_utc"], rows[-1]["start_utc"])
        self.assertTrue(rows[0]["start_utc"].endswith("+00:00"))
        self.assertEqual(rows[0]["label"], label)
        self.assertEqual(rows[0]["user_cost_per_kwh"], "")
        self.assertEqual(float(rows[1]["user_cost_per_kwh"]), 0)

    def test_empty_history_is_a_header_only_download(self):
        response = self.client.get("/api/charges.csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows(response), [])
        self.assertIn("start_utc,end_utc", response.get_data(as_text=True))
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_external_labels_cannot_start_spreadsheet_formulas(self):
        for i, label in enumerate(["=1+1", " +1+1", "-1+1", "@SUM(1)", "\tname"]):
            self.charge(1700000000 + i, label=label)
        rows = self.rows(self.client.get("/api/charges.csv"))
        self.assertTrue(all(row["label"].startswith("'") for row in rows))

    def test_configured_login_requires_a_token_or_session_cookie(self):
        token = "synthetic-csv-token-0123456789"
        api.auth = DashboardAuth(token, lambda: 1700000000)
        self.assertEqual(self.client.get("/api/charges.csv").status_code, 401)
        response = self.client.get(
            "/api/charges.csv", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.client.post("/login", data={"password": token})
        self.assertEqual(self.client.get("/api/charges.csv").status_code, 200)

    def test_missing_database_is_an_error_not_a_new_empty_export(self):
        missing = self.database.with_name("missing.db")
        with mock.patch("api.DB_PATH", missing), self.assertLogs(
                api.app.logger, level="ERROR"):
            response = self.client.get("/api/charges.csv")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(missing.exists())


class CsvLinkTests(unittest.TestCase):
    def test_static_demo_does_not_advertise_a_missing_api(self):
        import report
        charges = [{"start_ts": 1700000000, "end_ts": 1700001800, "kwh_added": 10}]
        with mock.patch("report._charge_row_html", return_value=""), mock.patch(
                "report._charge_month_summary", return_value=""):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIn("/api/charges.csv", report._charges_html(charges))
            with mock.patch.dict(os.environ, {"WATTSON_DEMO": "1"}):
                self.assertNotIn("/api/charges.csv", report._charges_html(charges))


if __name__ == "__main__":
    unittest.main()
