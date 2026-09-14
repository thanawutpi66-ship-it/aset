"""Regression tests for the canonical, auditable session CSV schema."""
import csv
import os
import shutil
import tempfile
import unittest
import json

from aset_batt.storage.data_utils import (
    DataHandler, SESSION_COLUMNS, SESSION_SCHEMA_VERSION, write_session_metadata,
)


class TestSessionSchema(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aset_session_schema_")
        self.path = os.path.join(self.dir, "session.csv")
        self.writer = DataHandler(throttle_redundant_rows=False)
        ok, message = self.writer.start_logging(self.path, test_type="QuickScan")
        self.assertTrue(ok, message)

    def tearDown(self):
        self.writer.stop_logging()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _rows(self):
        self.writer.csv_file.flush()
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def test_every_new_session_has_one_canonical_schema_and_provenance(self):
        self.writer.log_row(
            0.100, 12.55, 5.0, 99.8, 42.1, 26.5,
            rin_calibrated=True, mode="MINI_PULSE", phase="MINI_PULSE",
            capacity_ah=0.00014, voltage_source="eload",
            current_source="eload",
        )
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            self.assertEqual(next(csv.reader(f)), SESSION_COLUMNS)

        row = self._rows()[0]
        self.assertEqual(row["Schema_Version"], SESSION_SCHEMA_VERSION)
        self.assertEqual(row["Test_Type"], "QuickScan")
        self.assertEqual(row["Mode"], "MINI_PULSE")
        self.assertEqual(row["Phase"], "MINI_PULSE")
        self.assertEqual(row["Voltage_Source"], "eload")
        self.assertEqual(row["Current_Source"], "eload")
        self.assertEqual(row["Sample_Quality"], "VALID")
        self.assertEqual(row["Step_Index"], "1")
        self.assertTrue(row["Session_ID"])

    def test_late_high_rate_sample_is_retained_and_flagged_as_gap(self):
        self.writer.log_row(0.000, 12.6, 5.0, 100.0, 42.0, 26.0,
                            mode="MINI_PULSE", expected_dt_s=0.1)
        self.writer.log_row(0.500, 12.5, 5.0, 99.9, 42.0, 26.0,
                            mode="MINI_PULSE", expected_dt_s=0.1)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["Sample_Quality"], "GAP")
        self.assertIn("sample_gap_s=0.500", rows[1]["Sample_Note"])

    def test_invalid_voltage_or_missing_phase_is_retained_but_not_valid(self):
        self.writer.log_row(0.0, 0.0, 0.0, 100.0, 42.0, 26.0)
        row = self._rows()[0]
        self.assertEqual(row["Sample_Quality"], "INVALID")
        self.assertIn("missing_phase", row["Sample_Note"])
        self.assertIn("invalid_voltage", row["Sample_Note"])

    def test_metadata_carries_the_same_session_identity_and_test_type(self):
        write_session_metadata(
            self.path, session_id=self.writer.session_id,
            test_type=self.writer.test_type,
            extra={"profile": "Lead-Acid 12V (6S, 7Ah)", "sample_hz_target": 10.0},
        )
        with open(self.path + ".meta.json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["schema_version"], SESSION_SCHEMA_VERSION)
        self.assertEqual(meta["session_id"], self.writer.session_id)
        self.assertEqual(meta["test_type"], "QuickScan")
        self.assertEqual(meta["profile"], "Lead-Acid 12V (6S, 7Ah)")

    def test_terminal_outcome_and_protocol_are_preserved_in_sidecar(self):
        """A partial/safety-tripped record must never be indistinguishable from
        a completed test when it is inspected later for grading or a report."""
        write_session_metadata(
            self.path, session_id=self.writer.session_id,
            test_type=self.writer.test_type,
            extra={"protocol": {
                "id": "quick-scan-v2",
                "phases": ["OCV_SETTLE", "MINI_PULSE", "MAIN_DISCHARGE"],
            }},
        )
        self.writer.log_row(0.1, 12.5, 5.0, 99.0, 42.0, 26.0)
        self.writer.stop_logging("safety_tripped", "under-voltage interlock")

        with open(self.path + ".meta.json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["status"], "safety_tripped")
        self.assertEqual(meta["end_reason"], "under-voltage interlock")
        self.assertIn("started_at", meta)
        self.assertIn("ended_at", meta)
        self.assertTrue(meta["sha256"])
        self.assertEqual(meta["protocol"]["id"], "quick-scan-v2")
        self.assertIn("sampling_summary", meta)
        self.assertEqual(meta["sampling_summary"]["quality_counts"]["INVALID"], 1)

    def test_flush_makes_pending_rows_visible_without_closing_session(self):
        self.writer.log_row(0.1, 12.5, 5.0, 99.0, 42.0, 26.0)
        self.writer.flush()
        self.assertEqual(len(self._rows()), 1)
        self.assertTrue(self.writer.is_recording)

    def test_session_paths_are_unique_within_one_second(self):
        first = DataHandler.make_session_path(self.dir, label="HPPC")
        second = DataHandler.make_session_path(self.dir, label="HPPC")
        self.assertNotEqual(first, second)
        self.assertIn("test_HPPC_", os.path.basename(first))


if __name__ == "__main__":
    unittest.main()
