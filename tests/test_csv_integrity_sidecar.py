"""Industrial-grade audit follow-up R4.

Session CSVs used to carry no integrity guard at all — appended to with a
plain `open(path, 'a')`, no checksum/hash. If it ever became necessary to
prove a result file hadn't been edited after the test completed (e.g. a
dispute over a graded batch), there was no way to do that. DataHandler.
stop_logging() now writes a SHA-256 sidecar (<path>.sha256); verify_integrity()
checks a CSV against it later.
"""
import hashlib
import os
import shutil
import tempfile
import unittest

from aset_batt.storage.data_utils import DataHandler


class TestIntegritySidecarWrittenOnStop(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aset_csv_integrity_test_")
        self.csv_path = os.path.join(self.tmpdir, "session.csv")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_sidecar_created_on_stop_logging(self):
        dh = DataHandler()
        dh.start_logging(self.csv_path)
        dh.log_row(10.0, 12.5, 1.0, 90.0, 30.0, 25.0)
        dh.stop_logging()

        sidecar = self.csv_path + ".sha256"
        self.assertTrue(os.path.exists(sidecar))
        with open(sidecar, encoding="utf-8") as f:
            content = f.read()
        self.assertIn(os.path.basename(self.csv_path), content)

    def test_sidecar_matches_a_real_sha256_of_the_file(self):
        dh = DataHandler()
        dh.start_logging(self.csv_path)
        dh.log_row(10.0, 12.5, 1.0, 90.0, 30.0, 25.0)
        dh.stop_logging()

        with open(self.csv_path, "rb") as f:
            expected = hashlib.sha256(f.read()).hexdigest()
        with open(self.csv_path + ".sha256", encoding="utf-8") as f:
            recorded = f.read().split()[0]
        self.assertEqual(recorded, expected)

    def test_no_sidecar_written_if_logging_never_started(self):
        dh = DataHandler()
        dh.stop_logging()   # never started — must not raise, must not write anything
        self.assertFalse(os.path.exists(self.csv_path + ".sha256"))


class TestVerifyIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aset_csv_integrity_test_")
        self.csv_path = os.path.join(self.tmpdir, "session.csv")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _log_a_session(self):
        dh = DataHandler()
        dh.start_logging(self.csv_path)
        dh.log_row(10.0, 12.5, 1.0, 90.0, 30.0, 25.0)
        dh.log_row(20.0, 12.4, 1.0, 89.0, 30.0, 25.1)
        dh.stop_logging()

    def test_untouched_file_verifies_true(self):
        self._log_a_session()
        self.assertTrue(DataHandler.verify_integrity(self.csv_path))

    def test_modified_file_verifies_false(self):
        self._log_a_session()
        with open(self.csv_path, "a", encoding="utf-8-sig") as f:
            f.write("99:99:99,999.9,0.0,0.0,0.0,0.0,0.0,1\n")   # tampered row appended
        self.assertFalse(DataHandler.verify_integrity(self.csv_path))

    def test_missing_sidecar_returns_none(self):
        self._log_a_session()
        os.remove(self.csv_path + ".sha256")
        self.assertIsNone(DataHandler.verify_integrity(self.csv_path))

    def test_missing_csv_returns_none(self):
        self._log_a_session()
        os.remove(self.csv_path)
        self.assertIsNone(DataHandler.verify_integrity(self.csv_path))

    def test_a_session_still_being_recorded_has_no_sidecar_yet(self):
        dh = DataHandler()
        dh.start_logging(self.csv_path)
        dh.log_row(10.0, 12.5, 1.0, 90.0, 30.0, 25.0)
        try:
            self.assertIsNone(DataHandler.verify_integrity(self.csv_path))
        finally:
            dh.stop_logging()


if __name__ == "__main__":
    unittest.main()
