"""Tests for the climate-segment inference (Wattson PR #25): a parked+awake
segment becomes CLIMATE only on a sustained, multi-step SoC drop measured
from snapshots inside the segment itself, and the activity bar renders it.
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import analyze
import db
import report

T = 1700000000
LA = ZoneInfo("America/Los_Angeles")


def snap(ts, soc):
    return {"ts": ts, "soc": soc}


def detect(merged, snaps):
    return analyze._detect_climate_segments(merged, snaps)


class DetectClimateTests(unittest.TestCase):
    def test_sustained_drop_reclassifies(self):
        out = detect([[T, T + 3600, "parked"]],
                     [snap(T, 80.0), snap(T + 3600, 79.0)])
        self.assertEqual(out[0][2], "climate")

    def test_short_segment_stays_parked(self):
        out = detect([[T, T + 540, "parked"]],
                     [snap(T, 80.0), snap(T + 540, 79.0)])
        self.assertEqual(out[0][2], "parked")

    def test_slow_drop_stays_parked(self):
        out = detect([[T, T + 3600, "parked"]],
                     [snap(T, 80.0), snap(T + 3600, 79.8)])
        self.assertEqual(out[0][2], "parked")

    def test_single_gauge_step_stays_parked(self):
        # One 0.1% gauge tick over 10 min is 0.60%/hr, above the rate bar;
        # the minimum-drop floor keeps it from painting the park climate.
        out = detect([[T, T + 600, "parked"]],
                     [snap(T, 80.0), snap(T + 600, 79.9)])
        self.assertEqual(out[0][2], "parked")

    def test_exact_minimum_drop_over_45_min(self):
        # 0.3% over exactly 45 min is exactly 0.4%/hr; the raw float
        # division computes 0.39999999999999997, so both gates need the
        # rounding to pass.
        out = detect([[T, T + 2700, "parked"]],
                     [snap(T, 80.0), snap(T + 2700, 79.7)])
        self.assertEqual(out[0][2], "climate")

    def test_non_parked_segments_untouched(self):
        merged = [[T, T + 3600, "moving"], [T + 3600, T + 7200, "sleep"],
                  [T + 7200, T + 10800, "charging"]]
        snaps = [snap(T, 80.0), snap(T + 3600, 79.0),
                 snap(T + 7200, 78.0), snap(T + 10800, 77.0)]
        out = detect(merged, snaps)
        self.assertEqual([s[2] for s in out],
                         ["moving", "sleep", "charging"])

    def test_park_start_soc_none_uses_in_segment_snaps(self):
        # The park-start poll has no SoC; the drop must be measured from
        # snapshots inside the park, not walked back into the drive.
        snaps = [snap(T, 80.0), {"ts": T + 300, "soc": None},
                 snap(T + 1200, 77.0)]
        merged = [[T, T + 300, "moving"], [T + 300, T + 1200, "parked"]]
        out = detect(merged, snaps)
        self.assertEqual(out[1][2], "parked")

    def test_no_soc_inside_segment_stays_parked(self):
        snaps = [snap(T - 3600, 80.0), snap(T + 7200, 77.0)]
        out = detect([[T, T + 3600, "parked"]], snaps)
        self.assertEqual(out[0][2], "parked")

    def test_single_soc_snapshot_inside_stays_parked(self):
        snaps = [snap(T + 1800, 79.0)]
        out = detect([[T, T + 3600, "parked"]], snaps)
        self.assertEqual(out[0][2], "parked")

    def test_mixed_segment_averages_whole_segment(self):
        # Whole-segment averaging is a known limitation (see docstring):
        # 20 min of climate (-1.0%) + 2 h idle averages 0.43%/hr and
        # paints the entire park climate.
        snaps = []
        for m in range(0, 141, 2):
            if m <= 20:
                soc = round(80.0 - 1.0 * (m / 20), 1)
            else:
                soc = 79.0
            snaps.append(snap(T + m * 60, soc))
        out = detect([[T, T + 8400, "parked"]], snaps)
        self.assertEqual(out[0][2], "climate")
        # And 3 h idle + 20 min of climate averages 0.30%/hr, hiding it.
        snaps = []
        for m in range(0, 201, 2):
            if m <= 180:
                soc = 80.0
            else:
                soc = round(80.0 - 1.0 * ((m - 180) / 20), 1)
            snaps.append(snap(T + m * 60, soc))
        out = detect([[T, T + 12000, "parked"]], snaps)
        self.assertEqual(out[0][2], "parked")


class ActivitySegmentsMidnightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target, value in [
            ("db.DB_PATH", root / "tracker.db"),
            ("report.TOKENS_PATH", root / "missing-tokens"),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init()

    def test_climate_segment_spanning_midnight_stays_climate(self):
        # 23:30 -> 00:30 local, cloud online, parked, soc 80.0 -> 79.0.
        start = int(datetime(2026, 11, 5, 23, 30,
                             tzinfo=LA).timestamp())
        with db.connect() as con:
            for i in range(31):
                ts = start + i * 120
                soc = round(80.0 - 1.0 * (i / 30), 1)
                con.execute(
                    "INSERT INTO snapshots (ts, lat, lon, odometer,"
                    " cloud_online, soc) VALUES (?, 34.0, -118.4,"
                    " 10000.0, 1, ?)", (ts, soc))
        segs = analyze.activity_segments(now=start + 30 * 120)
        by_day = {}
        for s in segs:
            by_day.setdefault(s["day"], set()).add(s["state"])
        self.assertIn("climate", by_day.get("2026-11-05", set()))
        self.assertIn("climate", by_day.get("2026-11-06", set()))


class ActivityBarTests(unittest.TestCase):
    def test_climate_segment_renders_st_climate(self):
        day_start = int(datetime(2026, 11, 5, 0, 0,
                                 tzinfo=LA).timestamp())
        segs = [{"day": "2026-11-05", "start_ts": day_start + 3600,
                 "end_ts": day_start + 7200, "state": "climate"},
                {"day": "2026-11-05", "start_ts": day_start + 7200,
                 "end_ts": day_start + 10800, "state": "parked"}]
        html = report._activity_bar(segs, day_start, None)
        self.assertIn("st-climate", html)
        self.assertIn("climate", html)
        self.assertIn("st-parked", html)

    def test_unknown_state_falls_back_gracefully(self):
        day_start = int(datetime(2026, 11, 5, 0, 0,
                                 tzinfo=LA).timestamp())
        segs = [{"day": "2026-11-05", "start_ts": day_start + 3600,
                 "end_ts": day_start + 7200, "state": "mystery"}]
        html = report._activity_bar(segs, day_start, None)
        self.assertIn("st-unknown", html)
        self.assertIn("mystery", html)

    def test_legend_includes_climate(self):
        self.assertIn('<i class="st-climate"></i>climate',
                      report.HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
