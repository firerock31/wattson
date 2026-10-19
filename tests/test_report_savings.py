from pathlib import Path
import tempfile
import unittest
from unittest import mock

import db
import report


class SavingsCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.output = root / "dashboard.html"
        for target, value in [
            ("db.DB_PATH", root / "tracker.db"),
            ("report.REPORT_PATH", self.output),
            ("report.TOKENS_PATH", root / "missing-tokens"),
            ("report._fmt_time", lambda ts: "10:00 am"),
            ("report.GAS_COMPARISON_MPG", 25.5),
            ("report.GAS_PRICE_FALLBACK", 4.5),
            ("gasprice.get_gas_price", lambda day: 4.5),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init()

    def charge(self, timestamp, charge_type, price, label="Test charger"):
        with db.connect() as con:
            con.execute(
                "INSERT INTO charges (start_ts, end_ts, minutes, soc_start,"
                " soc_end, kwh_added, charge_type, user_cost_per_kwh, label)"
                " VALUES (?, ?, 30, 20, 40, 10, ?, ?, ?)",
                (timestamp, timestamp + 1800, charge_type, price, label))

    def render(self):
        report.generate()
        return self.output.read_text()

    def test_free_dc_is_included_but_unpriced_dc_is_disclosed(self):
        self.charge(1700000000, "AC", None, "HOME")
        self.charge(1700010000, "DC", 0)
        self.charge(1700020000, "DC", None)
        page = self.render()
        self.assertIn("1 home session<br>1 DC session", page)
        self.assertIn("1 unpriced DC session excluded", page)
        self.assertIn("25.5 MPG; $4.50/gal", page)

    def test_all_unpriced_sessions_explain_how_to_include_them(self):
        self.charge(1700000000, "DC", None)
        self.charge(1700010000, "DC", None)
        page = self.render()
        self.assertIn("2 unpriced DC sessions excluded", page)
        self.assertIn("Enter a DC price in the charging log", page)
        self.assertNotIn("No charges recorded yet", page)


if __name__ == "__main__":
    unittest.main()
