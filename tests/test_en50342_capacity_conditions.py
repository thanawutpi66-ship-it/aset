"""Tests for the EN 50342-1 (SLI lead-acid) capacity-test condition checker.

Context: the rig's capacity workflow was branded "IEC 61960" — a secondary
LITHIUM standard — even for VRLA batteries. Beyond the label, nothing verified
whether a run actually satisfied the lead-acid standard's Cn-test conditions
(discharge at the In = Cn/n reference rate, 1.75 V/cell end voltage, from a
fully charged and rested battery), so any C-rate/cutoff combination could
masquerade as a standard measurement. en50342_capacity_conditions() makes the
verdict explicit: a clean run reports a direct Ce-vs-Cn result (Peukert is a
no-op at the reference rate), and any violated condition is named.
"""
import unittest

from aset_batt.ui.sequences import en50342_capacity_conditions


class TestApplicability(unittest.TestCase):
    def test_not_applicable_to_lithium(self):
        applicable, violations = en50342_capacity_conditions(
            "LiPO", 0.2, 16.5, 6, False, False)
        self.assertFalse(applicable)
        self.assertEqual(violations, [])

    def test_applicable_to_lead_acid_and_aliases(self):
        for chem in ("LeadAcid", "AGM", "VRLA", "SLA"):
            applicable, _ = en50342_capacity_conditions(
                chem, 0.1, 10.5, 6, False, False)
            self.assertTrue(applicable, chem)


class TestStandardConditions(unittest.TestCase):
    def test_clean_i10_run_has_no_violations(self):
        # C10-rated pack: reference rate = 0.1C; 10.5 V / 6 cells = 1.75 V/cell.
        applicable, violations = en50342_capacity_conditions(
            "LeadAcid", 0.1, 10.5, 6, skip_charge=False, skip_rest=False)
        self.assertTrue(applicable)
        self.assertEqual(violations, [])

    def test_wrong_rate_is_named(self):
        # The real 2026-07-08 IEC run discharged at 0.5C — 5x the reference
        # rate; its capacity number only meant anything through the Peukert
        # model, never as a direct standard Ce.
        _, violations = en50342_capacity_conditions(
            "LeadAcid", 0.5, 10.5, 6, False, False)
        self.assertTrue(any("reference rate" in v for v in violations))

    def test_wrong_end_voltage_is_named(self):
        _, violations = en50342_capacity_conditions(
            "LeadAcid", 0.1, 11.4, 6, False, False)   # 1.90 V/cell cutoff
        self.assertTrue(any("end voltage" in v for v in violations))

    def test_skipped_charge_and_rest_are_named(self):
        # The real 2026-07-08 run auto-skipped CHARGE on a surface-charge OCV
        # misread — exactly the condition that made its "SoH 95.66%" an
        # artifact. A standard verdict must name that skip.
        _, violations = en50342_capacity_conditions(
            "LeadAcid", 0.1, 10.5, 6, skip_charge=True, skip_rest=True)
        self.assertTrue(any("fully charged" in v for v in violations))
        self.assertTrue(any("rested" in v for v in violations))

    def test_rate_tolerance_accepts_near_reference(self):
        # ±15% band: 0.108C on a C10 rating still counts as I10.
        _, violations = en50342_capacity_conditions(
            "LeadAcid", 0.108, 10.5, 6, False, False)
        self.assertFalse(any("reference rate" in v for v in violations))


if __name__ == "__main__":
    unittest.main()
