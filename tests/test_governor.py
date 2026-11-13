"""Unit tests for governor.py with a fake clock. Run:
    python -m unittest test_governor -v
Needs config.py importable (run from the tracker code dir or staging).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import governor


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class RateLimited(Exception):
    status = 429


class Unauth(Exception):
    pass


class GovernorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = FakeClock()
        self.logs = []
        governor.STATE_PATH = Path(self.tmp) / "governor_state.json"
        governor.LOCK_PATH = Path(self.tmp) / "governor_state.json.lock"
        governor._now = self.clock.now
        governor._sleep = self.clock.sleep
        governor._log = lambda c, k, o, d="": self.logs.append((c, k, o, d))
        os.environ.pop("RIVIAN_GOVERNOR", None)

    def state(self):
        p = governor.STATE_PATH
        return json.loads(p.read_text()) if p.exists() else {}

    def test_first_call_passes_and_logs_ok(self):
        with governor.slot("poller", "vehicleState"):
            pass
        self.assertIn(("poller", "vehicleState", "ok", ""), self.logs)
        self.assertEqual(self.state()["last_call_ts"], self.clock.t)

    def test_spacing_enforced(self):
        with governor.slot("poller", "vehicleState"):
            pass
        self.clock.t += 1.0  # only 1s later
        with governor.slot("poller", "vehicleState"):
            pass
        self.assertAlmostEqual(sum(self.clock.slept), 2.0, places=5)

    def test_rate_limit_starts_cooldown_and_blocks(self):
        with self.assertRaises(RateLimited):
            with governor.slot("poller", "vehicleState"):
                raise RateLimited("429")
        until = governor.cooldown_until()
        self.assertAlmostEqual(until, self.clock.t + 300, places=5)
        with self.assertRaises(governor.CoolingDown):
            with governor.slot("tires", "subscribe"):
                pass
        self.assertIn(("tires", "subscribe", "blocked",
                       f"until {until:.0f}"), self.logs)

    def test_ladder_escalates_then_caps(self):
        for expected in (300, 900, 3600, 3600):
            with self.assertRaises(RateLimited):
                with governor.slot("poller", "vehicleState"):
                    raise RateLimited("429")
            self.assertAlmostEqual(
                governor.cooldown_until(), self.clock.t + expected, places=4)
            # Jump to the moment the cooldown expires. The gap since the
            # last limit equals the ladder step (<= 3600), so the ladder
            # does not reset between iterations.
            self.clock.t = governor.cooldown_until()

    def test_ladder_resets_after_clean_hour(self):
        with self.assertRaises(RateLimited):
            with governor.slot("poller", "vehicleState"):
                raise RateLimited("429")
        self.clock.t += 3600 + 301 + 10  # cooldown over AND reset window over
        with self.assertRaises(RateLimited):
            with governor.slot("poller", "vehicleState"):
                raise RateLimited("429")
        self.assertAlmostEqual(
            governor.cooldown_until(), self.clock.t + 300, places=4)

    def test_retry_after_wins_over_ladder(self):
        until = governor.note_rate_limit(1200, source="riv")
        self.assertAlmostEqual(until, self.clock.t + 1200, places=5)
        self.assertAlmostEqual(governor.cooldown_until(), until, places=5)

    def test_unauthenticated_is_not_a_rate_limit(self):
        with self.assertRaises(Unauth):
            with governor.slot("poller", "vehicleState"):
                raise Unauth("UNAUTHENTICATED: session expired")
        self.assertIsNone(governor.cooldown_until())

    def test_kill_switch_passes_but_logs(self):
        governor.note_rate_limit(None, source="test")
        os.environ["RIVIAN_GOVERNOR"] = "off"
        with governor.slot("poller", "vehicleState"):
            pass
        self.assertIn(("poller", "vehicleState", "pass", ""), self.logs)

    def test_corrupt_state_fails_open(self):
        governor.STATE_PATH.write_text("{not json")
        with governor.slot("poller", "vehicleState"):
            pass  # must not raise

    def test_error_without_rate_limit_no_cooldown(self):
        with self.assertRaises(ValueError):
            with governor.slot("poller", "vehicleState"):
                raise ValueError("boom")
        self.assertIsNone(governor.cooldown_until())
        self.assertTrue(any(o == "error" for _, _, o, _ in self.logs))

    def test_cooldown_until_none_when_expired(self):
        governor.note_rate_limit(None, source="test")
        self.clock.t += 301
        self.assertIsNone(governor.cooldown_until())


if __name__ == "__main__":
    unittest.main()
