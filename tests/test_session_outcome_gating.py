"""A partial run remains useful diagnostic data, never an accepted grade."""
import json
import os
import tempfile
import unittest

from aset_batt.acquisition.analysis import _apply_session_outcome


class TestSessionOutcomeGating(unittest.TestCase):
    def test_safety_tripped_session_is_withheld_from_overall_grading(self):
        with tempfile.TemporaryDirectory(prefix="aset_outcome_") as directory:
            csv_path = os.path.join(directory, "session.csv")
            with open(csv_path + ".meta.json", "w", encoding="utf-8") as f:
                json.dump({"status": "safety_tripped",
                           "end_reason": "under-voltage interlock"}, f)
            result = _apply_session_outcome(csv_path, {
                "grade": "A", "overall_grade": "A", "gradeable": True,
                "overall_gradeable": True, "quality_warnings": [],
            })
        self.assertEqual(result["grade"], "REVIEW")
        self.assertFalse(result["overall_gradeable"])
        self.assertEqual(result["session_outcome"], "safety_tripped")
        self.assertIn("under-voltage interlock", result["quality_warnings"][0])

    def test_completed_session_preserves_analysis_grade(self):
        with tempfile.TemporaryDirectory(prefix="aset_outcome_") as directory:
            csv_path = os.path.join(directory, "session.csv")
            with open(csv_path + ".meta.json", "w", encoding="utf-8") as f:
                json.dump({"status": "completed", "end_reason": "cut-off"}, f)
            result = _apply_session_outcome(csv_path, {
                "grade": "A", "overall_grade": "A", "gradeable": True,
                "overall_gradeable": True, "quality_warnings": [],
            })
        self.assertEqual(result["grade"], "A")
        self.assertTrue(result["session_complete"])


if __name__ == "__main__":
    unittest.main()
