import unittest
import os
from data_utils import DataHandler

class TestDataHandler(unittest.TestCase):
    def setUp(self):
        self.dh = DataHandler()

    def test_start_logging(self):
        filepath = 'test_data.csv'
        success, msg = self.dh.start_logging(filepath)
        self.assertTrue(success)
        self.assertTrue(os.path.exists(filepath))
        self.dh.stop_logging()
        os.remove(filepath)

    def test_log_row(self):
        filepath = 'test_data.csv'
        self.dh.start_logging(filepath)
        self.dh.log_row(10.0, 3.7, 0.5, 95.0, 0.1, 25.0)
        self.dh.stop_logging()
        with open(filepath, 'r') as f:
            lines = f.readlines()
            self.assertEqual(len(lines), 2)  # header + data
        os.remove(filepath)

    def tearDown(self):
        if self.dh.is_recording:
            self.dh.stop_logging()