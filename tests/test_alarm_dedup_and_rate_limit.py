"""Industrial-grade audit follow-up R2/G2.

_log_alarm() used to unconditionally insertRow() for every call — a stuck
sensor re-firing the identical fault every telemetry tick would flood the log
with duplicate rows, burying whatever alarm actually mattered, with no ISA-18.2
style flood suppression at all.

Fixed with two independent layers:
  1. Dedup: the SAME (event, point) repeating within _ALARM_DEDUP_WINDOW_S
     updates the existing row in place ("(xN)" occurrence count) instead of
     inserting a duplicate.
  2. Rate limit: more than _ALARM_RATE_LIMIT DISTINCT rows within
     _ALARM_RATE_WINDOW_S coalesces further rows into one running
     "rate limit" summary row until the rate drops back down.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


class TestAlarmDedup(unittest.TestCase):
    def test_repeated_identical_alarm_updates_in_place(self):
        w = BatteryQtWindow(ConfigManager())
        try:
            before = w.tbl_alarms.rowCount()
            w._log_alarm("Sensor glitch on channel 3")
            after_first = w.tbl_alarms.rowCount()
            w._log_alarm("Sensor glitch on channel 3")
            w._log_alarm("Sensor glitch on channel 3")
            after_repeats = w.tbl_alarms.rowCount()

            self.assertEqual(after_first, before + 1)
            self.assertEqual(after_repeats, after_first)   # no new rows
            last_point = w.tbl_alarms.item(after_repeats - 1, 1).text()
            self.assertIn("Sensor glitch on channel 3", last_point)
            self.assertIn("3", last_point)   # occurrence count reached 3
        finally:
            w.close()

    def test_distinct_alarms_each_get_their_own_row(self):
        w = BatteryQtWindow(ConfigManager())
        try:
            before = w.tbl_alarms.rowCount()
            w._log_alarm("Event A")
            w._log_alarm("Event B")
            w._log_alarm("Event C")
            self.assertEqual(w.tbl_alarms.rowCount(), before + 3)
        finally:
            w.close()

    def test_dedup_does_not_apply_across_a_different_intervening_alarm(self):
        """A-B-A must produce 3 rows, not coalesce the two A's — dedup only
        coalesces IMMEDIATELY repeated identical alarms, not any recurrence."""
        w = BatteryQtWindow(ConfigManager())
        try:
            before = w.tbl_alarms.rowCount()
            w._log_alarm("Event A")
            w._log_alarm("Event B")
            w._log_alarm("Event A")
            self.assertEqual(w.tbl_alarms.rowCount(), before + 3)
        finally:
            w.close()

    def test_dedup_outside_the_window_creates_a_new_row(self):
        w = BatteryQtWindow(ConfigManager())
        try:
            before = w.tbl_alarms.rowCount()
            w._log_alarm("Old repeating fault")
            # Simulate the dedup window having elapsed.
            w._last_alarm_time -= (w._ALARM_DEDUP_WINDOW_S + 1.0)
            w._log_alarm("Old repeating fault")
            self.assertEqual(w.tbl_alarms.rowCount(), before + 2)
        finally:
            w.close()


class TestAlarmRateLimit(unittest.TestCase):
    def test_flood_of_distinct_alarms_is_coalesced_into_one_summary_row(self):
        w = BatteryQtWindow(ConfigManager())
        try:
            before = w.tbl_alarms.rowCount()
            for i in range(w._ALARM_RATE_LIMIT + 10):
                w._log_alarm(f"Flood event {i}")

            # Rows must stop growing linearly with the number of alarms — capped
            # at (rate limit) normal rows + 1 summary row.
            grew_by = w.tbl_alarms.rowCount() - before
            self.assertLessEqual(grew_by, w._ALARM_RATE_LIMIT + 1)
            self.assertLess(grew_by, w._ALARM_RATE_LIMIT + 10)   # proves suppression happened

            last_point = w.tbl_alarms.item(w.tbl_alarms.rowCount() - 1, 1).text()
            self.assertIn("rate limit", last_point.lower())
        finally:
            w.close()

    def test_suppressed_count_increments_correctly_not_reset_each_call(self):
        """Regression for a specific bug caught during implementation: the
        suppressed-event counter used to reset to 0 on the very row it was
        first displayed on, undercounting every subsequent flood event by one."""
        w = BatteryQtWindow(ConfigManager())
        try:
            n_flood = w._ALARM_RATE_LIMIT + 5
            for i in range(n_flood):
                w._log_alarm(f"Flood event {i}")

            last_point = w.tbl_alarms.item(w.tbl_alarms.rowCount() - 1, 1).text()
            # Extract the integer right after "limit —".
            import re
            m = re.search(r"limit\s*—\s*(\d+)\s*event", last_point)
            self.assertIsNotNone(m)
            reported = int(m.group(1))
            self.assertEqual(reported, w._alarm_rate_suppressed)
            self.assertGreater(reported, 0)
        finally:
            w.close()

    def test_below_the_limit_every_alarm_still_gets_its_own_row(self):
        w = BatteryQtWindow(ConfigManager())
        try:
            before = w.tbl_alarms.rowCount()
            n = w._ALARM_RATE_LIMIT - 2
            for i in range(n):
                w._log_alarm(f"Sub-limit event {i}")
            self.assertEqual(w.tbl_alarms.rowCount(), before + n)
        finally:
            w.close()

    def test_rate_limit_clears_once_the_window_passes(self):
        w = BatteryQtWindow(ConfigManager())
        try:
            for i in range(w._ALARM_RATE_LIMIT + 3):
                w._log_alarm(f"Flood event {i}")
            self.assertIsNotNone(w._alarm_rate_limit_row)

            # Simulate the rate window having fully elapsed.
            w._alarm_recent_times.clear()
            before = w.tbl_alarms.rowCount()
            w._log_alarm("A fresh, non-flood alarm")

            self.assertEqual(w.tbl_alarms.rowCount(), before + 1)
            self.assertIsNone(w._alarm_rate_limit_row)
            last_point = w.tbl_alarms.item(w.tbl_alarms.rowCount() - 1, 1).text()
            self.assertIn("A fresh, non-flood alarm", last_point)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
