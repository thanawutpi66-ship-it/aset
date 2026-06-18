"""
Wiring test: BatteryAnalyzer -> EventBus -> listener (ANALYSIS_COMPLETED)

ยืนยันว่า analysis_module ถูกต่อเข้ากับ event system จริง (ไม่ใช่ dead code)
"""
import csv
import os
import tempfile
import threading
import unittest

from event_system import EventBus, EventType
from analysis_module import BatteryAnalyzer, AnalysisResult


def _write_discharge_csv(path: str, rows: int = 30) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "Elapsed_s", "Voltage_V", "Current_A",
                    "SoC_pct", "Resistance_mOhm", "Temperature_C"])
        for k in range(rows):
            w.writerow(["00:00:00", k, 3.3 - 0.002 * k, 1.0,
                        100 - k, 20.0, 25.0 + 0.05 * k])


class TestAnalysisWiring(unittest.TestCase):
    def test_analyze_posts_event_with_result(self):
        bus = EventBus()
        bus.start()
        received = {}
        done = threading.Event()

        def listener(event):
            received["result"] = event.data
            done.set()

        bus.add_listener(EventType.ANALYSIS_COMPLETED, listener)

        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        _write_discharge_csv(path)
        try:
            analyzer = BatteryAnalyzer(rated_capacity_ah=2.0, event_bus=bus)
            result = analyzer.analyze(path)

            self.assertTrue(done.wait(timeout=5.0),
                            "ANALYSIS_COMPLETED event was not delivered")
            self.assertIsInstance(received["result"], AnalysisResult)
            self.assertTrue(received["result"].success)
            self.assertIs(received["result"], result)
            self.assertIn(result.grade, ("A", "B", "C", "D"))
        finally:
            bus.stop()
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
