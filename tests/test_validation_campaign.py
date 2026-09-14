import math
import unittest

from aset_batt.core.validation_campaign import (
    ambient_summary,
    campaign_is_complete,
    ekf_metrics,
    normalize_campaign,
    reference_soc_from_capacity,
    replicate_statistics,
    ssr_interruption_evidence,
    validation_verdict,
)


class TestValidationCampaign(unittest.TestCase):
    def setUp(self):
        self.campaign = normalize_campaign({
            "enabled": True, "campaign_id": "VRLA-SEP", "specimen_id": "B006",
            "expected_condition": "Normal", "run_index": 2,
        })

    def test_legacy_campaign_stays_disabled(self):
        self.assertFalse(normalize_campaign(None)["enabled"])
        self.assertFalse(campaign_is_complete(normalize_campaign({"enabled": True})))
        self.assertTrue(campaign_is_complete(self.campaign))

    def test_reference_soc_uses_capacity_not_estimator_signal(self):
        self.assertEqual(reference_soc_from_capacity([0.0, 2.65, 5.3], 5.3),
                         [100.0, 50.0, 0.0])
        with self.assertRaises(ValueError):
            reference_soc_from_capacity([0.0], 0.0)

    def test_replicates_use_small_sample_confidence_interval(self):
        stats = replicate_statistics([10.0, 11.0, 12.0])
        self.assertEqual(stats["n"], 3)
        self.assertAlmostEqual(stats["mean"], 11.0)
        self.assertAlmostEqual(stats["std_dev"], 1.0)
        self.assertAlmostEqual(stats["ci95_half_width"], 4.303 / math.sqrt(3), places=5)

    def test_ekf_metrics_report_sustained_convergence(self):
        result = ekf_metrics([70, 76, 78, 79], [80, 80, 80, 80],
                             [0, 30, 60, 120], convergence_pct=5, sustain_s=60)
        self.assertAlmostEqual(result["mae_pct"], 4.25)
        self.assertEqual(result["convergence_s"], 30.0)
        self.assertEqual(result["max_error_pct"], 10.0)

    def test_ssr_result_is_an_upper_bound_not_switch_time(self):
        evidence = ssr_interruption_evidence(10.0, [(9.9, 1.0), (10.1, 0.2), (10.4, 0.01)])
        self.assertTrue(evidence["available"])
        self.assertAlmostEqual(evidence["upper_bound_s"], 0.4)
        self.assertIn("not relay switching time", evidence["claim"])

    def test_verdict_requires_ambient_c10_and_clean_telemetry(self):
        ambient = ambient_summary([24.5, 25.3], self.campaign)
        self.assertTrue(validation_verdict(
            self.campaign, ambient, {"quality_counts": {"VALID": 20}}, True)["validation_ready"])
        verdict = validation_verdict(self.campaign, ambient,
                                     {"quality_counts": {"INVALID": 1}}, True)
        self.assertFalse(verdict["validation_ready"])
        self.assertIn("invalid telemetry samples present", verdict["reasons"])


if __name__ == "__main__":
    unittest.main()
