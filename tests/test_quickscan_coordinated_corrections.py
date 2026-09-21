"""Regression coverage for rate basis, OCV acceptance and Quick Scan estimates."""
import unittest

import numpy as np

from aset_batt.acquisition.analysis import (
    analyze_series, identify_dcir, profile_from_config, _main_discharge_capacity,
    _integration_gap_metrics, _quick_discharge_interval,
    _quick_mean_discharge_current,
    _quick_start_soc_from_metadata,
)
from aset_batt.acquisition.models import BatteryProfile
from aset_batt.acquisition.ocv_validation import (
    OCV_MAX_ABS_CURRENT_A, evaluate_ocv_window, evaluate_quick_ocv_window,
    estimate_full_capacity,
)
from aset_batt.core import battery_profiles
from aset_batt.core.battery_model import BatteryModel


def _profile():
    return BatteryProfile(
        name="LeadAcid", chemistry="LeadAcid", nominal_v=12.0, series=6,
        capacity_ah=5.0, max_charge_v=14.7, cutoff_v=10.5,
        max_charge_a=5.0, max_discharge_a=5.0, ovp=15.0, uvp=10.0,
        otp_warn=45.0, otp_crit=55.0, internal_r=0.03,
        peukert_k=1.1, peukert_hr=10.0,
        capacity_10h_ah=5.0, capacity_rating_validated=True,
    )


def _quick_record(q_mini, q_main, *, i_mini=5.302, i_main=5.301,
                 boundary_dt=0.01):
    """Build realistic Quick telemetry at each phase's configured cadence."""
    rows = [(0.0, 0.0, 12.87, "OCV")]
    now = 0.0

    def append(dt, current, voltage, phase):
        nonlocal now
        now += dt
        rows.append((now, current, voltage, phase))

    append(boundary_dt, i_mini, 12.86, "MINI_PULSE")
    mini_hold = q_mini * 3600.0 / i_mini - boundary_dt
    n_mini = max(1, int(np.ceil(mini_hold / 0.20)))
    for _ in range(n_mini):
        append(mini_hold / n_mini, i_mini, 12.8, "MINI_PULSE")
    append(boundary_dt, 0.0, 12.8, "RELAX")
    for _ in range(18):
        append(5.0, 0.0, 12.8, "RELAX")
    append(boundary_dt, i_main, 12.7, "MAIN_DISCHARGE")
    main_hold = q_main * 3600.0 / i_main - 2.0 * boundary_dt
    n_main = max(1, int(np.ceil(main_hold / 12.0)))
    for j in range(1, n_main + 1):
        append(main_hold / n_main, i_main,
               12.7 - 2.2 * j / n_main, "MAIN_DISCHARGE")
    append(boundary_dt, i_main, 10.4, "NEAR_CUTOFF")
    append(boundary_dt, 0.0, 10.4, "TAIL_REST")
    t, i, v, modes = map(list, zip(*rows))
    return (np.asarray(t), np.asarray(i), np.asarray(v),
            np.full(len(rows), 25.0), modes)


class TestYTZ6VRateBasis(unittest.TestCase):
    def test_explicit_c10_and_c20_ratings(self):
        p = battery_profiles.get_product("YTZ6V (12V 5.3Ah VRLA)")
        self.assertEqual(p.capacity_10h_ah, 5.0)
        self.assertEqual(p.capacity_20h_ah, 5.3)
        self.assertEqual(p.rated_capacity_ah, 5.0)
        self.assertEqual(p.product_display_capacity_ah, 5.3)
        self.assertEqual(p.cca_a, 90.0)
        self.assertEqual(p.peukert_k, 1.16)
        self.assertEqual(p.rated_capacity_ah / 10.0, 0.5)
        self.assertEqual(p.capacity_20h_ah / 20.0, 0.265)
        from aset_batt.ui.sequences.quick_scan import quick_scan_1c_current
        self.assertEqual(quick_scan_1c_current(p.rated_capacity_ah,
                                               p.max_cont_discharge_a), 5.0)

    def test_ftz6v_rate_basis_is_not_assumed(self):
        p = battery_profiles.get_product("FB FTZ6V (12V 5.3Ah VRLA AGM)")
        self.assertEqual(p.capacity_rating_basis, "UNKNOWN")
        self.assertEqual(p.capacity_10h_ah, 0.0)

    def test_live_quick_profile_uses_product_peukert_override(self):
        from aset_batt.core.config import ConfigManager

        product = battery_profiles.get_product("YTZ6V (12V 5.3Ah VRLA)")
        chemistry = battery_profiles.get_chemistry("LeadAcid")
        self.assertEqual(product.peukert_k, 1.16)
        self.assertEqual(chemistry.peukert_k, 1.10)
        config = ConfigManager()
        config.battery.battery_type = "LeadAcid"
        config.battery.product_name = "YTZ6V (12V 5.3Ah VRLA)"
        profile = profile_from_config(config)
        self.assertEqual(profile.peukert_k, 1.16)


class TestOcvValidity(unittest.TestCase):
    def _samples(self, current=0.0, drift=0.0):
        return [(i * 5.0, 12.4 + drift * i, current, 25.0, True)
                for i in range(61)]

    def test_zero_current_stable_rest_passes(self):
        result = evaluate_ocv_window(self._samples(), outputs_off=True,
                                     min_rest_s=300, window_s=60,
                                     max_spread_v=0.010)
        self.assertTrue(result["valid"])
        self.assertEqual(result["status"], "VALID_OCV")

    def test_current_unstable_and_short_rest_fail(self):
        args = dict(outputs_off=True, min_rest_s=300, window_s=60,
                    max_spread_v=0.010)
        self.assertEqual(evaluate_ocv_window(self._samples(OCV_MAX_ABS_CURRENT_A + .01),
                                              **args)["status"], "CURRENT_NOT_ZERO")
        self.assertEqual(evaluate_ocv_window(self._samples(drift=.001),
                                              **args)["status"], "VOLTAGE_UNSTABLE")
        self.assertEqual(evaluate_ocv_window(self._samples()[:10],
                                              **args)["status"], "NOT_RESTED")

    def test_ocv_curve_regression_and_pack_scaling(self):
        model = BatteryModel("LeadAcid", series_cells=6)
        expected = [(12.90, 100.0), (12.80, 91.9), (12.71, 84.7),
                    (12.70, 83.9), (12.60, 76.4), (12.50, 69.0),
                    (12.40, 59.6), (12.20, 47.7), (12.00, 32.7),
                    # Source table's 15%=11.754 V and 20%=11.808 V imply
                    # 19.26% at 11.800 V (not the earlier 17.7% estimate).
                    (11.80, 19.26), (10.50, 0.0)]
        for voltage, soc in expected:
            self.assertAlmostEqual(model.get_soc_from_ocv(voltage, 25.0), soc, delta=0.15)

    def test_quick_ocv_uses_shared_180_to_600_second_policy(self):
        samples = [(float(i), 12.5, 0.0, 25.0, True) for i in range(181)]
        at_180 = evaluate_quick_ocv_window(samples, outputs_off=True, now_s=180.0)
        self.assertTrue(at_180["valid"])
        self.assertEqual(at_180["voltage_v"], 12.5)
        unstable = [(float(i), 12.5 + i * 0.0002, 0.0, 25.0, True)
                    for i in range(601)]
        at_600 = evaluate_quick_ocv_window(unstable, outputs_off=True, now_s=600.0)
        self.assertFalse(at_600["valid"])
        self.assertEqual(at_600["status"], "OCV_TIMEOUT")

    def test_quick_ocv_current_spread_and_minimum_rest_gates(self):
        stable = [(float(i), 12.5, 0.0, 25.0, True) for i in range(181)]
        current = [(t, v, 0.101, temp, valid)
                   for t, v, _, temp, valid in stable]
        self.assertEqual(evaluate_quick_ocv_window(
            current, outputs_off=True, now_s=180.0)["status"],
            "CURRENT_NOT_ZERO")
        unstable = [(float(i), 12.5 + i * 0.0002, 0.0, 25.0, True)
                    for i in range(181)]
        self.assertEqual(evaluate_quick_ocv_window(
            unstable, outputs_off=True, now_s=180.0)["status"],
            "VOLTAGE_UNSTABLE")
        self.assertEqual(evaluate_quick_ocv_window(
            stable[:180], outputs_off=True, now_s=179.0)["status"],
            "NOT_RESTED")

    def test_stable_end_ocv_is_range_checked_before_soc_lookup(self):
        from pathlib import Path

        model = BatteryModel("LeadAcid", series_cells=6)
        quick_source = Path("aset_batt/ui/sequences/quick_scan.py").read_text(
            encoding="utf-8")
        self.assertLess(quick_source.index("ocv_out_of_range_mv("),
                        quick_source.index("end_soc = ("))
        for voltage, expected_valid in ((12.5, True), (5.0, False), (13.15, False)):
            samples = [(float(i), voltage, 0.0, 25.0, True)
                       for i in range(181)]
            end = evaluate_quick_ocv_window(
                samples, outputs_off=True, now_s=180.0)
            self.assertTrue(end["valid"])
            oor_mv = model.ocv_out_of_range_mv(
                end["voltage_v"], end["temperature_c"])
            if oor_mv != 0.0:
                end["valid"] = False
                end["status"] = "OUT_OF_RANGE"
            self.assertEqual(end["valid"], expected_valid)
            if not end["valid"]:
                unavailable = estimate_full_capacity(
                    2.0, 90.0, None, start_valid=True, end_valid=False)
                self.assertIsNone(unavailable["capacity_ah"])
                self.assertEqual(unavailable["status"], "END_OCV_NOT_VALID")


class TestQuickDcirAndCapacity(unittest.TestCase):
    def _record(self, latency, with_main=True):
        t = np.array([0.0, 0.1, 0.2, 0.3, 0.3 + latency, 0.4 + latency,
                      0.5 + latency, 0.6 + latency, 0.7 + latency,
                      0.8 + latency], float)
        i = np.array([0.0, 0.0, 0.0, 0.0, 1.0, 1.0,
                      0.0, 0.0, 0.0, 0.0], float)
        v = np.array([12.6, 12.6, 12.6, 12.6, 12.55, 12.54,
                      12.60, 12.60, 12.60, 12.60], float)
        modes = ["OCV"] * 4 + ["MINI_PULSE"] * 2 + ["RELAX"] * 2 + ["TAIL_REST"] * 2
        if with_main:
            t = np.append(t, [0.9 + latency, 1.0 + latency, 1.1 + latency])
            i = np.append(i, [0.0, 1.0, 1.0])
            v = np.append(v, [12.60, 12.4, 12.3])
            modes += ["TAIL_REST", "MAIN_DISCHARGE", "NEAR_CUTOFF"]
        return t, i, v, np.full(i.size, 25.0), modes

    def test_latency_boundary_cases(self):
        for latency in (0.286, 0.346, 0.500):
            with self.subTest(latency=latency):
                t, i, v, temp, modes = self._record(latency)
                result = identify_dcir(i, v, temp, _profile(), time_s=t,
                                       modes=modes, quick_scan=True)
                self.assertTrue(result[3])
                self.assertEqual(result[2], 1)
        for latency in (0.501, 1.374):
            with self.subTest(latency=latency):
                t, i, v, temp, modes = self._record(latency)
                result = identify_dcir(i, v, temp, _profile(), time_s=t,
                                       modes=modes, quick_scan=True)
                self.assertFalse(result[3])

    def test_quick_dcir_uses_only_mini_pulse_step_on(self):
        t, i, v, temp, modes = self._record(0.286)
        # Main-discharge edge and release cannot inflate the Quick DCIR sample count.
        result = identify_dcir(i, v, temp, _profile(), time_s=t,
                               modes=modes, quick_scan=True)
        self.assertEqual(result[2], 1)
        self.assertAlmostEqual(result[0], 0.05, places=6)

    def test_main_and_near_cutoff_intervals_count_once(self):
        t = np.array([0.0, 10.0, 20.0, 30.0])
        i = np.array([1.0, 1.0, 1.0, 1.0])
        modes = ["MAIN_DISCHARGE", "MAIN_DISCHARGE", "NEAR_CUTOFF", "NEAR_CUTOFF"]
        q, basis = _main_discharge_capacity(t, i, 99.0, modes)
        self.assertEqual(basis, "MAIN_DISCHARGE")
        self.assertAlmostEqual(q, 30.0 / 3600.0)

    def test_quick_interval_includes_mini_and_main_once(self):
        q_mini, q_main = 0.04405, 2.69893
        t, i, _, _, modes = _quick_record(q_mini, q_main)
        q = _quick_discharge_interval(t, i, 0.0, modes)
        self.assertAlmostEqual(q, 2.74298, places=5)
        q_main_only, _ = _main_discharge_capacity(t, i, 0.0, modes)
        self.assertAlmostEqual(q_main_only,
                               q_main - 5.301 * 0.01 / 3600.0, places=7)

    def test_quick_mean_uses_same_timestamped_phase_boundaries_as_charge(self):
        t = np.array([0.0, 0.1, 0.2, 0.3, 0.4])
        i = np.array([0.0, 4.0, 0.0, 2.0, 0.0])
        modes = ["OCV", "MINI_PULSE", "RELAX", "MAIN_DISCHARGE", "TAIL_REST"]
        q = _quick_discharge_interval(t, i, 0.0, modes)
        mean_i = _quick_mean_discharge_current(t, i, modes)
        self.assertAlmostEqual(q * 3600.0, 0.6)
        self.assertAlmostEqual(mean_i, 1.5)

    def test_quick_gap_boundaries_follow_phase_cadence(self):
        for phases, limit in ((["MINI_PULSE"] * 2, 0.25),
                              (["NEAR_CUTOFF"] * 2, 0.25),
                              (["MAIN_DISCHARGE"] * 2, 12.5)):
            with self.subTest(phase=phases[0]):
                accepted = _integration_gap_metrics(
                    [0.0, limit], None, phases, quick_scan=True)
                rejected = _integration_gap_metrics(
                    [0.0, limit + 0.001], None, phases, quick_scan=True)
                self.assertFalse(accepted["excluded_interval_mask"][0])
                self.assertEqual(accepted["integration_quality_status"], "VALID")
                self.assertTrue(rejected["excluded_interval_mask"][0])
                self.assertEqual(rejected["excluded_gap_count"], 1)
                self.assertAlmostEqual(rejected["largest_dt_s"], limit + 0.001)
                self.assertAlmostEqual(rejected["excluded_gap_duration_s"], limit + 0.001)
                self.assertAlmostEqual(rejected["integration_span_s"], limit + 0.001)
                self.assertEqual(rejected["integration_quality_status"], "EXCESSIVE_GAPS")

    def test_thirty_second_quick_gap_still_obeys_stricter_phase_limit(self):
        result = _integration_gap_metrics(
            [0.0, 30.0], None, ["MINI_PULSE", "MINI_PULSE"], quick_scan=True)
        self.assertTrue(result["excluded_interval_mask"][0])
        self.assertEqual(result["excluded_gap_duration_s"], 30.0)
        self.assertEqual(result["integration_quality_status"], "EXCESSIVE_GAPS")

    def test_q_and_mean_use_identical_accepted_mixed_phase_edges(self):
        t = np.array([0.0, 0.2, 0.35, 0.60, 20.0, 20.15, 20.4])
        i = np.array([0.0, 4.0, 0.0, 0.0, 2.0, 2.0, 0.0])
        modes = ["MINI_PULSE", "MINI_PULSE", "RELAX", "MAIN_DISCHARGE",
                 "MAIN_DISCHARGE", "NEAR_CUTOFF", "TAIL_REST"]
        quality = _integration_gap_metrics(t, None, modes, quick_scan=True)
        active = {"MINI_PULSE", "MAIN_DISCHARGE", "NEAR_CUTOFF"}
        accepted_edges = [idx for idx, (a, b) in enumerate(zip(modes[:-1], modes[1:]))
                          if (a in active or b in active)
                          and not quality["excluded_interval_mask"][idx]]
        self.assertEqual(accepted_edges, [0, 1, 2, 4, 5])
        self.assertEqual(quality["excluded_gap_count"], 1)
        self.assertAlmostEqual(quality["largest_dt_s"], 19.4)
        self.assertAlmostEqual(quality["excluded_gap_duration_s"], 19.4)
        self.assertAlmostEqual(quality["integration_span_s"], 20.4)
        self.assertEqual(quality["integration_quality_status"], "EXCESSIVE_GAPS")
        q = _quick_discharge_interval(t, i, 0.0, modes)
        mean_i = _quick_mean_discharge_current(t, i, modes)
        self.assertAlmostEqual(q * 3600.0, 1.25)
        self.assertAlmostEqual(mean_i, 1.25)

    def test_invalid_peukert_exponent_cannot_create_quick_soh(self):
        t, i, v, temp, modes = _quick_record(0.0015, 1.0)
        for invalid_k in (None, float("nan"), float("inf")):
            with self.subTest(k=invalid_k):
                profile = _profile()
                profile.peukert_k = invalid_k
                result = analyze_series(
                    t, i, v, temp, np.zeros_like(i), profile, False,
                    fit_ecm=False, modes=modes, quick_scan=True,
                    soc_start=80.0, soc_end=30.0,
                    ocv_start_valid=True, ocv_end_valid=True)
                self.assertEqual(result["quick_capacity_est_status"],
                                 "PEUKERT_INPUT_INVALID")
                self.assertIsNone(result["quick_full_capacity_est_ah"])
                self.assertFalse(result["quick_soh_est_valid"])

    def test_soc_start_prefers_valid_initial_ocv_anchor_before_pulse(self):
        self.assertEqual(_quick_start_soc_from_metadata(
            80.0, quick_scan=True,
            session_meta={"ocv_start_valid": True, "ocv_start_soc_pct": 97.53}),
            97.53)
        self.assertEqual(_quick_start_soc_from_metadata(
            80.0, quick_scan=True,
            session_meta={"ocv_start_valid": False, "ocv_start_soc_pct": None}),
            80.0)

    def test_historical_numerical_replay_k_1_10_with_synthetic_valid_anchors(self):
        profile = _profile()
        q_mini, q_main = 0.04405, 2.69893
        t, i, v, temp, modes = _quick_record(q_mini, q_main)
        result = analyze_series(t, i, v, temp, np.zeros(i.size),
                                profile, False, soc_start=90.0, soc_end=10.0,
                                ocv_start_valid=True, ocv_end_valid=True,
                                fit_ecm=False, modes=modes, quick_scan=True)
        kp = (5.301 / 0.500) ** 0.10
        q_c10 = 2.74298 * kp
        self.assertAlmostEqual(result["q_interval_removed_ah"], 2.74298, places=4)
        self.assertAlmostEqual(result["quick_mean_discharge_a"], 5.301, places=3)
        self.assertAlmostEqual(result["peukert_factor"], kp, places=4)
        self.assertAlmostEqual(result["q_c10_interval_equivalent_ah"], q_c10, places=4)
        self.assertAlmostEqual(result["quick_full_capacity_est_ah"], q_c10 / 0.80, places=4)
        self.assertAlmostEqual(result["quick_soh_est_pct"], 100.0 * q_c10 / 0.80 / 5.0,
                               places=3)
        self.assertAlmostEqual(result["quick_soh_est_pct"], 86.8363, places=3)

    def test_partial_soc_normalization_and_missing_end(self):
        result = estimate_full_capacity(3.8166, 84.7, 5.0,
                                        start_valid=True, end_valid=True)
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["capacity_ah"], 4.7887, places=3)
        missing = estimate_full_capacity(3.8166, 84.7, None,
                                         start_valid=True, end_valid=False)
        self.assertIsNone(missing["capacity_ah"])
        self.assertEqual(missing["status"], "END_OCV_NOT_VALID")

    def _analyze_partial_quick(self, start_valid, end_valid):
        profile = _profile()
        profile.capacity_rating_validated = True
        t, i, v, temp, modes = _quick_record(
            0.0015, 2.5, i_mini=1.0, i_main=5.0, boundary_dt=0.0001)
        return analyze_series(
            t, i, v, temp, np.zeros_like(i), profile, False,
            soc_start=80.0, soc_end=30.0,
            ocv_start_valid=start_valid, ocv_end_valid=end_valid,
            fit_ecm=False, modes=modes, quick_scan=True)

    def test_quick_soh_requires_both_valid_ocv_anchors(self):
        invalid = self._analyze_partial_quick(True, False)
        self.assertIsNone(invalid["quick_capacity_est_ah"])
        self.assertEqual(invalid["quick_capacity_est_status"], "END_OCV_NOT_VALID")
        self.assertFalse(invalid["quick_soh_est_valid"])
        self.assertEqual(invalid["quick_grade"], "N/A")
        self.assertEqual(invalid["grade"], "N/A")
        self.assertEqual(invalid["capacity_grade"], "N/A")
        self.assertEqual(invalid["capacity_assessment_status"], "NOT_AVAILABLE")
        self.assertIsNotNone(invalid["q_interval_removed_ah"])
        self.assertIsNotNone(invalid["q_c10_interval_equivalent_ah"])
        self.assertTrue(invalid["dcir_measured"])

        valid = self._analyze_partial_quick(True, True)
        self.assertAlmostEqual(valid["q_removed_ah"], 2.5, places=6)
        self.assertAlmostEqual(valid["quick_capacity_est_ah"],
                               valid["q_c10_interval_equivalent_ah"] / 0.5, places=6)
        self.assertAlmostEqual(valid["quick_soh_est_pct"],
                               100.0 * valid["quick_capacity_est_ah"] / 5.0, places=6)
        self.assertEqual(valid["quick_scan_reference_capacity_ah"], 5.0)
        self.assertEqual(valid["peukert_reference_rate_hr"], 10.0)
        self.assertNotEqual(valid["grade"], "A")  # verified C10 evidence is still absent

    def test_stale_mini_pulse_is_fallback_not_measured(self):
        t, i, v, temp, modes = self._record(0.501)
        result = analyze_series(
            t, i, v, temp, np.zeros_like(i), _profile(), False,
            fit_ecm=False, modes=modes, quick_scan=True,
            soc_start=80.0, soc_end=30.0,
            ocv_start_valid=True, ocv_end_valid=True)
        self.assertFalse(result["dcir_measured"])
        self.assertEqual(result["dcir_source"], "PROFILE_FALLBACK")
        self.assertEqual(result["cca_proxy_source"], "PROFILE_FALLBACK")


if __name__ == "__main__":
    unittest.main()
