from pathlib import Path
import tempfile
import unittest
from unittest import mock

import db
import report


class PollAgeReportTests(unittest.TestCase):
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
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init()

    def test_uses_latest_snapshot_timestamp(self):
        with db.connect() as con:
            con.executemany(
                "INSERT INTO snapshots (ts, soc) VALUES (?, ?)",
                [(1700000000, 60), (1700000600, 61)])
        report.generate()
        self.assertIn('id="poll-age" data-ts="1700000600"',
                      self.output.read_text())

    def test_no_readings_does_not_claim_a_recent_poll(self):
        report.generate()
        self.assertIn('id="poll-age" data-ts=""',
                      self.output.read_text())


if __name__ == "__main__":
    unittest.main()
