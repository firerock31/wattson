"""Regression tests for the phantom-drain display rules (Oct 2026):
sub-step periods print no rate, a parked gain never displays as a
negative drop, missing SoC cannot crash the render, the median label
lives in the axis gutter, and stub labels survive thin mode."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import report


def period(**over):
    base = {
        "id": 1, "label": "Oct 21", "day": "2026-10-21", "hours": 3.2,
        "kwh": 0.0, "watts": 0.0, "pct_day": 0.0, "soc_start": 54.7,
        "soc_end": 54.7, "soc_drop": 0.0, "measured": True,
        "plugged": False, "approx": True, "substep": True,
        "reports": 2, "no_data": False, "window": "w", "t1": 0, "t2": 1,
        "wake_count": 0,
    }
    base.update(over)
    return base


def drain_row(**over):
    base = {
        "sleep_ts": 1700000000, "wake_ts": 1700010800, "hours": 3.0,
        "soc_start": 50.0, "soc_end": 50.1, "kwh_lost": 0.0,
        "watts_avg": 0.0, "pct_per_day": 0.0, "plugged_in": 0,
        "approx": 1, "measured": 1, "open_span": 0,
        "anchor_start_ts": 1700000000, "anchor_end_ts": 1700010800,
    }
    base.update(over)
    return base


class IdleHistoryTests(unittest.TestCase):
    def test_upward_recalibration_clamps_drop_to_zero(self):
        idles, _ = report._idle_history([drain_row()], [])
        self.assertEqual(idles[0]["soc_drop"], 0.0)
        self.assertTrue(idles[0]["substep"])

    def test_missing_soc_keeps_drop_none(self):
        idles, _ = report._idle_history(
            [drain_row(soc_start=None, soc_end=None)], [])
        self.assertIsNone(idles[0]["soc_drop"])
        html = report._idle_table(idles, set())
        self.assertIn("0.00 kWh &bull; -", html)


class IdleTableTests(unittest.TestCase):
    def test_single_report_is_singular(self):
        html = report._idle_table([period(reports=1)], set())
        self.assertIn("&middot; 1 report</td>", html)
        self.assertNotIn("1 reports", html)

    def test_multiple_reports_are_plural(self):
        html = report._idle_table([period(reports=3)], set())
        self.assertIn("&middot; 3 reports</td>", html)


class BarsTests(unittest.TestCase):
    def test_median_label_sits_in_the_gutter(self):
        svg = report._bars([("a", 10.0, "#f5a623"), ("b", 20.0, "#f5a623")],
                           y_unit="W", ref_value=15.0)
        m = re.search(r'<text x="([\d.]+)"[^>]*>median</text>', svg)
        self.assertIsNotNone(m)
        # x0 is 50, so the end-anchored label ends at 46 and its ~39px
        # of 12px text stays inside the viewBox.
        self.assertEqual(float(m.group(1)), 46.0)

    def test_thin_mode_keeps_stub_labels(self):
        items = [(f"p{i}", 10.0, "#f5a623") for i in range(17)]
        labels = [f"{i} W" for i in range(17)]
        labels[16] = "&le;0.1%"
        svg = report._bars(items, labels=labels, thin=True, y_unit="W",
                           stubs={16})
        self.assertIn("&le;0.1%", svg)
        self.assertNotIn(">5 W<", svg)
        self.assertIn('fill="none"', svg)


class HeatStripTests(unittest.TestCase):
    def test_substep_cell_is_flat_and_honest(self):
        nights = [
            period(id=1, day="2026-10-20", label="Oct 20", watts=44.0,
                   kwh=1.1, substep=False, approx=False, soc_drop=1.3),
            period(id=2, day="2026-10-21", label="Oct 21", watts=17.0,
                   kwh=0.09, substep=True),
        ]
        svg = report._heat_strip(nights, set())
        self.assertIn("Oct 21: below one gauge step over 3.2 h", svg)
        self.assertNotIn("17 W, 0.09 kWh", svg)
        m = re.search(r'fill="([^"]+)"><title>Oct 21: below', svg)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "#2b2b2d")


def snap(ts, soc=None, cloud_online=None, charger_state=None):
    return {"ts": ts, "soc": soc, "cloud_online": cloud_online,
            "charger_state": charger_state}


class OpenParkEstimateTests(unittest.TestCase):
    T0 = 1700000000

    def estimate(self, soc_start, snaps):
        return report._open_park_estimate(
            {"sleep_ts": self.T0, "soc_start": soc_start}, snaps)

    def test_empty_window(self):
        est = self.estimate(60.0, [])
        self.assertIsNone(est["soc_now"])
        self.assertEqual(est["elapsed_h"], 0.0)
        self.assertIsNone(est["drop"])
        self.assertIsNone(est["pct_day"])

    def test_missing_soc_start(self):
        est = self.estimate(None, [snap(self.T0 + 10800, 59.0)])
        self.assertIsNone(est["drop"])
        self.assertIsNone(est["pct_day"])

    def test_plugged_charger_state_withholds_rate(self):
        snaps = [snap(self.T0, 60.0, charger_state="charging_active"),
                 snap(self.T0 + 10800, 59.0,
                      charger_state="charging_active")]
        est = self.estimate(60.0, snaps)
        self.assertTrue(est["plugged"])
        self.assertIsNone(est["pct_day"])

    def test_gain_means_charged(self):
        snaps = [snap(self.T0, 50.0), snap(self.T0 + 10800, 50.3)]
        est = self.estimate(50.0, snaps)
        self.assertTrue(est["plugged"])
        self.assertIsNone(est["drop"])

    def test_tiny_gain_clamps_to_zero(self):
        snaps = [snap(self.T0, 50.0), snap(self.T0 + 10800, 50.1)]
        est = self.estimate(50.0, snaps)
        self.assertEqual(est["drop"], 0.0)
        self.assertTrue(est["approx"])

    def test_exact_0_3_drop_withholds_rate(self):
        # 50.1 -> 49.8 is exactly 0.3% in decimal, but the float
        # subtraction overshoots (0.30000000000000426); without the
        # epsilon it would compute a rate instead of withholding it.
        snaps = [snap(self.T0, 50.1), snap(self.T0 + 10800, 49.8)]
        est = self.estimate(50.1, snaps)
        self.assertTrue(est["approx"])
        self.assertIsNone(est["pct_day"])

    def test_short_park_withholds_rate(self):
        snaps = [snap(self.T0, 60.0), snap(self.T0 + 3600, 59.0)]
        est = self.estimate(60.0, snaps)
        self.assertEqual(est["elapsed_h"], 1.0)
        self.assertIsNone(est["pct_day"])

    def test_long_park_computes_rate(self):
        snaps = [snap(self.T0, 60.0), snap(self.T0 + 10800, 59.0)]
        est = self.estimate(60.0, snaps)
        self.assertFalse(est["approx"])
        self.assertEqual(est["pct_day"], 8.0)


class SleepShareTests(unittest.TestCase):
    def test_empty_window_returns_nones(self):
        share = report._sleep_share([], 1000, 2000, 3.0)
        self.assertEqual(share, {"quiet_h": None, "online_h": None,
                                 "unknown_h": None})

    def test_zero_hours_returns_nones(self):
        snaps = [snap(1000, cloud_online=1)]
        share = report._sleep_share(snaps, 1000, 1000, 0.0)
        self.assertEqual(share, {"quiet_h": None, "online_h": None,
                                 "unknown_h": None})

    def test_mixed_online_counts(self):
        snaps = ([snap(1000 + i * 60, cloud_online=1) for i in range(6)]
                 + [snap(1000 + 360 + i * 60, cloud_online=0)
                    for i in range(2)]
                 + [snap(1000 + 480 + i * 60, cloud_online=None)
                    for i in range(2)])
        share = report._sleep_share(snaps, 1000, 2000, 10.0)
        self.assertEqual(share, {"quiet_h": 2.0, "online_h": 6.0,
                                 "unknown_h": 2.0})


class SleepLabelTests(unittest.TestCase):
    def test_awake_label(self):
        html = report._idle_table(
            [period(sleep={"quiet_h": 1.0, "online_h": 2.5,
                           "unknown_h": 0.5})], set())
        self.assertIn("awake ~2.5 h", html)

    def test_asleep_throughout(self):
        html = report._idle_table(
            [period(sleep={"quiet_h": 2.8, "online_h": 0.0,
                           "unknown_h": 0.2})], set())
        self.assertIn("asleep throughout", html)

    def test_mostly_unknown_omits_asleep(self):
        html = report._idle_table(
            [period(sleep={"quiet_h": 1.0, "online_h": 0.0,
                           "unknown_h": 2.0})], set())
        self.assertNotIn("asleep throughout", html)
        self.assertNotIn("awake ~", html)

    def test_missing_sleep_key_renders(self):
        html = report._idle_table([period()], set())
        self.assertNotIn("awake ~", html)
        self.assertNotIn("asleep throughout", html)


class OpenParkBannerTests(unittest.TestCase):
    # Own setUp (mirrors GenerateDrainTests): the banner test needs a
    # full generate() render, and keeping it in its own class keeps this
    # file mergeable with main, which appends its own tests to
    # GenerateDrainTests.
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
            ("report.GAS_PRICE_FALLBACK", 4.5),
            ("gasprice.get_gas_price", lambda day: 4.5),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init()

    def render(self):
        report.generate()
        return self.output.read_text()

    def test_parked_now_banner_shows_awake_half_hour(self):
        # online_h of exactly 0.5 must print "0.5", not "0" (the old
        # :.0f format rounded half-to-even down to zero).
        with db.connect() as con:
            con.execute(
                "INSERT INTO drains (sleep_ts, wake_ts, hours, soc_start,"
                " soc_end, kwh_lost, watts_avg, pct_per_day, plugged_in,"
                " approx, measured, open_span, anchor_start_ts,"
                " anchor_end_ts) VALUES (?, ?, 10, 60.0, 59.0, 0.88, 44.0,"
                " 1.0, 0, 0, 1, 0, ?, ?)",
                (1700000000, 1700036000, 1700000000, 1700036000))
        open_park = {
            "since_ts": 1700000000, "soc": 60.0, "soc_now": 59.7,
            "elapsed_h": 3.0, "drop": 0.3, "approx": True, "plugged": False,
            "pct_day": None,
            "sleep": {"quiet_h": 2.5, "online_h": 0.5, "unknown_h": 0.0},
        }
        with mock.patch("report._idle_history",
                         return_value=([], open_park)):
            page = self.render()
        self.assertIn("Parked now", page)
        self.assertIn("awake ~0.5 h of it", page)


class GenerateDrainTests(unittest.TestCase):
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
            ("report.GAS_PRICE_FALLBACK", 4.5),
            ("gasprice.get_gas_price", lambda day: 4.5),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init()

    def drain(self, sleep_ts, hours, soc_start, soc_end, kwh=0.0,
              watts=0.0, pct=0.0, approx=1):
        wake_ts = sleep_ts + int(hours * 3600)
        with db.connect() as con:
            con.execute(
                "INSERT INTO drains (sleep_ts, wake_ts, hours, soc_start,"
                " soc_end, kwh_lost, watts_avg, pct_per_day, plugged_in,"
                " approx, measured, open_span, anchor_start_ts,"
                " anchor_end_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?,"
                " 1, 0, ?, ?)",
                (sleep_ts, wake_ts, hours, soc_start, soc_end, kwh,
                 watts, pct, approx, sleep_ts, wake_ts))

    def render(self):
        report.generate()
        return self.output.read_text()

    def test_latest_park_with_upward_recalibration(self):
        self.drain(1700000000, 10, 60.0, 59.0, kwh=0.88, watts=44.0,
                   pct=1.0, approx=0)
        self.drain(1700100000, 5, 50.0, 50.1)
        page = self.render()
        self.assertNotIn("-0.1", page)
        self.assertIn("below one gauge step", page)
        self.assertIn("0.0<small> %</small>", page)

    def test_measured_park_with_missing_soc_does_not_crash(self):
        self.drain(1700000000, 10, 60.0, 59.0, kwh=0.88, watts=44.0,
                   pct=1.0, approx=0)
        self.drain(1700100000, 5, None, None)
        page = self.render()
        self.assertIn("0.00 kWh &bull; -", page)
        self.assertIn("1.0<small> %</small>", page)


if __name__ == "__main__":
    unittest.main()
