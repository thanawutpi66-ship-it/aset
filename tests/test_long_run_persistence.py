import csv
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from aset_batt.storage.data_utils import (
    DataHandler, StorageError, write_session_metadata,
)


class _VirtualClock:
    def __init__(self, now=0.0):
        self.value = now

    def __call__(self):
        return self.value


class TestLongRunPersistence(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="aset_longrun_")
        self.path = os.path.join(self.tempdir.name, "session.csv")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_csv_write_failure_is_not_silently_ignored(self):
        handler = DataHandler(throttle_redundant_rows=False)
        ok, message = handler.start_logging(self.path, "test")
        self.assertTrue(ok, message)
        handler.csv_file.close()
        with self.assertRaises(StorageError):
            handler.log_row(0.0, 12.0, 1.0, 50.0, 10.0, 25.0, mode="REST")

    def test_flush_failure_is_reported_to_caller(self):
        handler = DataHandler()
        ok, message = handler.start_logging(self.path, "test")
        self.assertTrue(ok, message)
        handler.csv_file.close()
        with self.assertRaises(StorageError):
            handler.flush()

    def test_open_permission_failure_is_returned_without_leaking_handle(self):
        handler = DataHandler()
        with patch("builtins.open", side_effect=PermissionError("denied")):
            ok, message = handler.start_logging(self.path, "mock")
        self.assertFalse(ok)
        self.assertIn("denied", message)
        self.assertFalse(handler.is_recording)
        self.assertIsNone(handler.csv_file)

    def test_writer_disk_full_and_removed_file_are_reported(self):
        handler = DataHandler(throttle_redundant_rows=False)
        ok, message = handler.start_logging(self.path, "mock")
        self.assertTrue(ok, message)
        write_session_metadata(self.path, session_id=handler.session_id, test_type="mock")

        class _FullDiskWriter:
            def writerow(self, _row):
                raise OSError("disk full")

        handler.csv_writer = _FullDiskWriter()
        with self.assertRaisesRegex(StorageError, "disk full"):
            handler.log_row(0.1, 12.0, 1.0, 50.0, 10.0, 25.0, mode="REST")
        handler.csv_writer = None
        handler.stop_logging("fault", "simulated disk full")

        # Windows permits deleting an open file, but a subsequent buffered write
        # still has platform-dependent semantics. Closing it deterministically
        # models the resulting invalid handle without touching real storage.
        handler = DataHandler(throttle_redundant_rows=False)
        ok, message = handler.start_logging(self.path, "mock")
        self.assertTrue(ok, message)
        handler.csv_file.close()
        with self.assertRaises(StorageError):
            handler.log_row(0.1, 12.0, 1.0, 50.0, 10.0, 25.0, mode="REST")

    def test_metadata_checkpoint_failure_is_a_storage_fault(self):
        clock = _VirtualClock(0.0)
        handler = DataHandler(throttle_redundant_rows=False)
        handler._clock = clock
        ok, message = handler.start_logging(self.path, "mock")
        self.assertTrue(ok, message)
        write_session_metadata(self.path, session_id=handler.session_id, test_type="mock")
        clock.value = 30.0
        with patch("aset_batt.storage.data_utils.checkpoint_session_metadata",
                   side_effect=StorageError("permission denied for sidecar")):
            with self.assertRaisesRegex(StorageError, "checkpoint"):
                handler.log_row(30.0, 12.0, 1.0, 50.0, 10.0, 25.0,
                                mode="REST", phase="REST")
        handler.stop_logging("fault", "metadata checkpoint failed")

    def test_removed_session_path_is_reported_on_next_write(self):
        handler = DataHandler(throttle_redundant_rows=False)
        ok, message = handler.start_logging(self.path, "mock")
        self.assertTrue(ok, message)
        handler.csv_file.close()
        os.unlink(self.path)
        with self.assertRaises(StorageError):
            handler.log_row(0.1, 12.0, 1.0, 50.0, 10.0, 25.0, mode="REST")

    def test_periodic_checkpoint_is_atomic_and_contains_recovery_state(self):
        clock = _VirtualClock(100.0)
        handler = DataHandler(throttle_redundant_rows=False)
        handler._clock = clock
        ok, message = handler.start_logging(self.path, "mock")
        self.assertTrue(ok, message)
        write_session_metadata(self.path, session_id=handler.session_id,
                               test_type="mock", extra={"protocol": {"id": "test"}})
        clock.value += 30.1
        handler.log_row(30.1, 12.1, 1.0, 50.0, 10.0, 25.0,
                        mode="MAIN_DISCHARGE", phase="MAIN_DISCHARGE")
        with open(self.path + ".meta.json", encoding="utf-8") as stream:
            meta = json.load(stream)
        checkpoint = meta["checkpoint"]
        self.assertEqual(checkpoint["status"], "running")
        self.assertEqual(checkpoint["rows_written"], 1)
        self.assertEqual(checkpoint["current_phase"], "MAIN_DISCHARGE")
        self.assertEqual(checkpoint["elapsed_engineering_s"], 30.1)
        self.assertTrue(checkpoint["last_successful_flush_timestamp"])
        self.assertEqual(meta["session_id"], handler.session_id)
        handler.stop_logging("completed", "test complete")

    def test_flush_policy_tracks_virtual_one_second_intervals(self):
        clock = _VirtualClock()
        handler = DataHandler(throttle_redundant_rows=False)
        handler._clock = clock
        ok, message = handler.start_logging(self.path, "mock")
        self.assertTrue(ok, message)
        write_session_metadata(self.path, session_id=handler.session_id, test_type="mock")
        for index in range(30):
            handler.log_row(index / 10.0, 12.0, 1.0, 50.0, 10.0, 25.0,
                            mode="MAIN", phase="MAIN")
            clock.value += 0.1
        # One header flush plus successful row flushes at virtual 1 s and 2 s.
        self.assertEqual(handler.flush_count, 3)
        handler.stop_logging("completed", "normal")

    def test_clean_stop_records_terminal_checkpoint_and_closes_file(self):
        handler = DataHandler(throttle_redundant_rows=False)
        ok, message = handler.start_logging(self.path, "mock")
        self.assertTrue(ok, message)
        write_session_metadata(self.path, session_id=handler.session_id, test_type="mock")
        handler.log_row(2.0, 12.0, 1.0, 50.0, 10.0, 25.0,
                        mode="PULSE", phase="PULSE")
        handler.stop_logging("completed", "normal")
        self.assertIsNone(handler.csv_file)
        with open(self.path + ".meta.json", encoding="utf-8") as stream:
            meta = json.load(stream)
        self.assertEqual(meta["status"], "completed")
        self.assertEqual(meta["checkpoint"]["status"], "completed")
        self.assertEqual(meta["checkpoint"]["rows_written"], 1)
        self.assertEqual(meta["checkpoint"]["current_phase"], "PULSE")
        with open(self.path, encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
