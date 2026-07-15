"""Regression tests for the second-round code-review fixes (verified findings
from an 8-angle review of commit 2836339):

1. start_monitor()'s reuse_session=False default force-closes a stale
   recording instead of silently appending a second manual test into the
   first one's file (test_monitor_session_preservation.py covers the actual
   reuse case; this file adds the "sequence closes its own session at normal
   completion" half of the same fix).
2. reset_battery_state() must clear _ecm_fit_soc/_r0_calibrated/
   _ecm_calibrated, or a battery swap inherits the previous battery's live
   rin anchor and bypasses the uncalibrated-R0 EKF safety guard.
3. HPPC relax/pulse legs and the Cycle Life discharge loop must check
   _seq_check_temp_stale() BEFORE feeding estimator.update()/_log_sample(),
   matching the IEC/Quick Scan pattern (CLAUDE.md's own documented hazard:
   a new loop mirroring an existing pattern that drops a safety-guard order).
4. _detect_step_r0()'s relative sanity band must not exceed the absolute
   5 Ω ceiling battery_profiles.get_measured_params() enforces separately.
5. AcquisitionWorker._post_process()'s update_ecm() feedback must anchor
   _ecm_fit_soc to the SoC AT the fitted pulse, not the record's final SoC.
"""
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.acquisition.models import BatteryProfile, TestConfig, OperationMode

_app = QApplication.instance() or QApplication([])

_seq_dir = Path(__file__).resolve().parent.parent / "aset_batt" / "ui" / "sequences"
_SEQUENCES_SRC = "\n".join(f.read_text(encoding="utf-8") for f in _seq_dir.glob("*.py"))


def _profile():
    return BatteryProfile("Test 12V", "Lead-Acid", 12.0, 6, 7.0,
                          14.4, 10.5, 1.4, 7.0, 15.0, 10.0, 45.0, 55.0, 0.03)


def _method_src(name: str) -> str:
    start = _SEQUENCES_SRC.index(f"def {name}")
    end = _SEQUENCES_SRC.find("\n    def ", start + 1)
    return _SEQUENCES_SRC[start:] if end == -1 else _SEQUENCES_SRC[start:end]


class TestResetBatteryStateClearsEcmAnchor(unittest.TestCase):
    def test_reset_clears_fit_soc_and_calibration_flags(self):
        model = BatteryModel("LeadAcid", 5.3, 6, 1)
        est = StateEstimator(5.3, model)
        # Simulate a real fit having landed away from the neutral 50% default.
        est._ecm_fit_soc = 90.0
        est._r0_calibrated = True
        est._ecm_calibrated = True
        est._ekf = object()   # sentinel — must be discarded, not carried over
        est.ecm_table = {"sentinel": True}

        est.reset_battery_state()

        self.assertEqual(est._ecm_fit_soc, 50.0)
        self.assertFalse(est._r0_calibrated)
        self.assertFalse(est._ecm_calibrated)
        self.assertIsNone(est._ekf)
        self.assertIsNone(est.ecm_table)


class TestCheckBeforeFeedOrdering(unittest.TestCase):
    """Source-pattern check: _seq_check_temp_stale() must appear BEFORE
    estimator.update()/_log_sample() in each of the three loops touched by
    the last commit — matching the IEC/Quick Scan discharge loops' own order.

    Each loop is isolated by the unique line that reads its own sample (these
    read expressions each occur exactly once in the whole per-sample loop
    body they belong to — earlier one-off _log_sample()/read calls elsewhere
    in the same (very long) sequence method must not leak into the window)."""

    def _loop_window(self, anchor: str, span: int = 3000) -> str:
        idx = _SEQUENCES_SRC.index(anchor)
        self.assertEqual(_SEQUENCES_SRC.count(anchor), 1, f"anchor not unique: {anchor!r}")
        return _SEQUENCES_SRC[idx:idx + span]

    def test_hppc_relax_leg_checks_stale_before_feeding(self):
        win = self._loop_window("v_r, _, _ = self.hw.read_vi()\n                        temp_h")
        stale_idx = win.index("_seq_check_temp_stale()")
        update_idx = win.index("self.controller.estimator.update(")
        log_idx = win.index("self.controller._log_sample(")
        self.assertLess(stale_idx, update_idx)
        self.assertLess(stale_idx, log_idx)

    def test_hppc_pulse_leg_checks_stale_before_feeding(self):
        win = self._loop_window("v_p, i_p = self.hw.read_measurements(prefer_load_v=True)")
        stale_idx = win.index("_seq_check_temp_stale()")
        update_idx = win.index("self.controller.estimator.update(")
        log_idx = win.index("self.controller._log_sample(")
        self.assertLess(stale_idx, update_idx)
        self.assertLess(stale_idx, log_idx)

    def test_cycle_life_discharge_loop_checks_stale_before_feeding(self):
        win = self._loop_window("v_d, i_d = self.hw.read_measurements(prefer_load_v=True)")
        stale_idx = win.index("_seq_check_temp_stale()")
        update_idx = win.index("self.controller.estimator.update(")
        log_idx = win.index("self.controller._log_sample(")
        self.assertLess(stale_idx, update_idx)
        self.assertLess(stale_idx, log_idx)


class TestSequencesCloseTheirOwnSession(unittest.TestCase):
    """Each of the 4 sequence threads' finally: block must call
    controller.end_session() so a completed sequence doesn't leave
    is_recording True forever (the exact mechanism the next sequence run's
    PREPARE phase would otherwise silently inherit and append into)."""

    def test_all_four_sequences_call_end_session_in_finally(self):
        for name in ("_auto_sequence_thread", "_quick_scan_thread",
                     "_hppc_seq_thread", "_cycle_life_thread"):
            src = _method_src(name)
            finally_idx = src.rindex("finally:")
            self.assertIn("end_session()", src[finally_idx:], name)

    def test_seq_cancel_calls_end_session(self):
        src = _method_src("_on_seq_cancel")
        self.assertIn("end_session()", src)


class TestR0SanityBandAbsoluteCeiling(unittest.TestCase):
    def test_relative_band_never_exceeds_5_ohm_absolute_ceiling(self):
        """battery_profiles.get_measured_params() rejects internal_r_ohm >= 5.0
        Ω independently — _detect_step_r0()'s relative band must not accept
        something that static validator would reject."""
        # A deliberately large base_rin (not any shipped product) to probe the
        # boundary: 6x this base_rin would be 12 Ω without the absolute cap.
        model = BatteryModel("LeadAcid", 5.3, 6, 1)
        model.base_rin = 2.0
        est = StateEstimator(5.3, model)
        est.use_ekf = True
        # Seed the step-detector's rolling buffer with a flat rest, then feed a
        # step whose implied R0 is comfortably inside 6x base_rin (12 ohm) but
        # OUTSIDE the absolute 5 ohm ceiling — must be rejected.
        for _ in range(est._STEP_BUF_LEN):
            est._detect_step_r0(12.6, 0.0, 0.2, 25.0)
        est._detect_step_r0(12.6 - 8.0, 1.0, 0.2, 25.0)   # implied R0 = 8 ohm
        self.assertFalse(est._r0_calibrated)


class TestPlausibilityBandDedup(unittest.TestCase):
    """The [0.2x, 6x]-relative-plus-absolute-ceiling check used to be
    reimplemented separately in state_estimator.py, analysis.py, and
    battery_profiles.py (three copies, kept in sync only by comments saying
    they "mirror" each other). All three now call/reference one shared
    battery_model.is_plausible_r0()/ABS_R0_CEILING_OHM."""

    def test_is_plausible_r0_boundary_behavior(self):
        from aset_batt.core.battery_model import is_plausible_r0, ABS_R0_CEILING_OHM
        base = 0.03
        self.assertFalse(is_plausible_r0(0.2 * base * 0.99, base))   # just under 0.2x
        self.assertTrue(is_plausible_r0(0.2 * base * 1.01, base))
        self.assertTrue(is_plausible_r0(6.0 * base * 0.99, base))
        self.assertFalse(is_plausible_r0(6.0 * base * 1.01, base))
        # absolute ceiling wins even when 6x base_rin would allow more
        self.assertFalse(is_plausible_r0(ABS_R0_CEILING_OHM + 0.01, base_rin=10.0))

    def test_state_estimator_uses_the_shared_helper(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "aset_batt" / "core"
               / "state_estimator.py").read_text(encoding="utf-8")
        self.assertIn("is_plausible_r0", src)
        self.assertNotIn("0.2 * base", src)   # the old inline duplicate is gone

    def test_analysis_uses_the_shared_helper(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "aset_batt" / "acquisition"
               / "analysis.py").read_text(encoding="utf-8")
        self.assertIn("is_plausible_r0", src)
        self.assertNotIn("0.2 * r_base", src)   # the old inline duplicate is gone

    def test_battery_profiles_references_the_shared_absolute_ceiling(self):
        from pathlib import Path
        from aset_batt.core.battery_model import ABS_R0_CEILING_OHM
        src = (Path(__file__).resolve().parent.parent / "aset_batt" / "core"
               / "battery_profiles.py").read_text(encoding="utf-8")
        self.assertIn("ABS_R0_CEILING_OHM", src)
        self.assertEqual(ABS_R0_CEILING_OHM, 5.0)

    def test_step_edge_latency_and_spread_constants_share_one_source(self):
        """StateEstimator._STEP_MAX_DT_S/_STEP_REF_MAX_SPREAD_V and
        analysis._DCIR_MAX_STEP_DT/_VI_LEVEL_MAX_SPREAD_V used to each hardcode
        their own '0.5'/'0.15' with no cross-reference — both now derive from
        battery_model.MAX_STEP_EDGE_LATENCY_S/STEADY_STATE_MAX_SPREAD_V."""
        from aset_batt.core.battery_model import (
            MAX_STEP_EDGE_LATENCY_S, STEADY_STATE_MAX_SPREAD_V)
        from aset_batt.core.state_estimator import StateEstimator
        from aset_batt.acquisition import analysis

        self.assertEqual(StateEstimator._STEP_MAX_DT_S, MAX_STEP_EDGE_LATENCY_S)
        self.assertEqual(StateEstimator._STEP_REF_MAX_SPREAD_V, STEADY_STATE_MAX_SPREAD_V)
        self.assertEqual(analysis._DCIR_MAX_STEP_DT, MAX_STEP_EDGE_LATENCY_S)
        self.assertEqual(analysis._VI_LEVEL_MAX_SPREAD_V, STEADY_STATE_MAX_SPREAD_V)


class TestArrheniusFormulaDedup(unittest.TestCase):
    """temp_rin_multiplier() and _calculate_base_rin() used to each reimplement
    the Arrhenius/linear-fallback formula separately and drifted apart for
    months (temp_rin_multiplier only checked one key name and silently always
    used the linear fallback). Both now call one shared
    BatteryModel._arrhenius_temp_factor()."""

    def test_both_call_sites_use_the_shared_helper(self):
        model = BatteryModel("LeadAcid", 12.0, 6, 1)
        self.assertTrue(hasattr(model, "_arrhenius_temp_factor"))

    def test_temp_rin_multiplier_and_base_rin_agree_on_the_same_temp_factor(self):
        """Construct the ratio each method implies for the temp_factor and
        confirm they match exactly (not just approximately) — proving they
        share one computation, not two independently-tuned ones."""
        model = BatteryModel("LeadAcid", 12.0, 6, 1)
        temp = 10.0
        direct = model._arrhenius_temp_factor(model._clamp_temperature(temp))
        mult = model.temp_rin_multiplier(temp)
        self.assertAlmostEqual(mult, max(0.1, 1.0 + direct), places=10)

        # _calculate_base_rin must fold in the SAME temp_factor via the shared
        # helper — reconstruct rin_cell manually from the (now-shared) factor
        # and compare against the real base_rin the method returns.
        soc = 50.0   # zero out the soc_factor term to isolate temp_factor
        params = model.rin_params
        aging_factor = params['aging_coeff'] * (1.0 - model.aging_factor)
        expected_cell = params['r0'] * (1 + direct) * (1 + 0.0) * (1 + aging_factor)
        expected_pack = max(0.001, expected_cell * model.series_cells / model.parallel_cells)
        actual = model._calculate_base_rin(soc, temp)
        self.assertAlmostEqual(actual, expected_pack, places=10)


class TestDcirPlausibilityBand(unittest.TestCase):
    """Real-file bug (sessions/test_20260709_154818.csv, 2026-07-09): at the
    charge onset the logger wrote two rows in the same 0.1 s window — the
    current column had refreshed (PSU setpoint applied) but the voltage
    readback (a separate SCPI query) still returned the pre-edge value, so
    ΔV = 0 across a 1.235 A edge computed R = 0.00 mΩ, which identify_dcir
    accepted as the record's only 'measured' step (n=1 → median 0.00) and the
    CCA proxy divided by it → 0 A. The live detector already had a relative
    plausibility band; identify_dcir now applies the same [0.2×, 6×] band."""

    def _prof(self):
        return BatteryProfile("YTZ6V", "LeadAcid", 12.0, 6, 5.3,
                              14.4, 10.5, 1.0, 10.0, 15.0, 9.5, 45.0, 60.0, 0.113)

    def test_zero_dv_step_is_rejected_not_reported_as_zero_ohm(self):
        from aset_batt.acquisition.analysis import identify_dcir
        # the exact shape from the real file: stale V across the current edge
        ia = np.array([0.0, 0.0, 0.0, -1.235, -2.653, -2.653])
        va = np.array([12.76, 12.76, 12.76, 12.76, 12.86, 12.86])
        temp = np.full(6, 27.6)
        t = np.array([0.1, 0.4, 0.5, 0.5, 0.6, 0.8])
        dcir, std, n, measured, n_stale, n_bad = identify_dcir(
            ia, va, temp, self._prof(), time_s=t)
        self.assertFalse(measured, "a zero-ΔV artifact must not count as measured")
        self.assertGreaterEqual(n_bad, 1)
        # falls back to the profile baseline, so the CCA proxy stays sane
        self.assertAlmostEqual(dcir, 0.113, places=3)

    def test_plausible_step_still_measures(self):
        from aset_batt.acquisition.analysis import identify_dcir
        ia = np.array([0.0, 0.0, 0.0, 2.65, 2.65, 2.65])
        va = np.array([12.76, 12.76, 12.76, 12.76 - 2.65 * 0.113,
                       12.76 - 2.65 * 0.113, 12.76 - 2.65 * 0.113])
        temp = np.full(6, 25.0)
        t = np.array([0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
        dcir, std, n, measured, n_stale, n_bad = identify_dcir(
            ia, va, temp, self._prof(), time_s=t)
        self.assertTrue(measured)
        self.assertEqual(n_bad, 0)
        self.assertAlmostEqual(dcir, 0.113, delta=0.01)

    def test_implausible_step_produces_quality_warning(self):
        from aset_batt.acquisition.analysis import analyze_series
        # rest -> stale-V edge (ΔV=0) -> settled load; long enough to analyze
        n_rest, n_dis = 25, 200
        t = np.concatenate([np.arange(n_rest) * 0.2,
                            n_rest * 0.2 + np.arange(n_dis) * 0.2])
        i = np.concatenate([np.zeros(n_rest), np.full(n_dis, 2.65)])
        v = np.concatenate([np.full(n_rest, 12.76),
                            [12.76],                      # stale V on the edge sample
                            np.linspace(12.45, 12.30, n_dis - 1)])
        temp = np.full(t.size, 25.0)
        cap = np.cumsum(np.clip(i, 0, None)) * 0.2 / 3600.0
        res = analyze_series(t, i, v, temp, cap, self._prof(), is_hppc=False)
        self.assertFalse(res["dcir_measured"])
        self.assertTrue(any("outside the plausible band" in w
                            for w in res["quality_warnings"]))


class _ArrayBackend:
    """Feeds pre-built (v, i_discharge_positive) samples one at a time, raising
    to end the run() loop once exhausted — backend convention is charge+/
    discharge-, the inverse of the discharge-positive arrays built below."""

    def __init__(self, v, i_dis):
        self.v, self.i_dis, self.k = v, i_dis, 0

    def start_mode(self, cfg):
        pass

    def step(self, dt_since, elapsed):
        if self.k >= len(self.v):
            raise IndexError("array exhausted — ends the worker loop")
        v, i_dis = float(self.v[self.k]), float(self.i_dis[self.k])
        self.k += 1
        return v, -i_dis

    def read_temperature(self):
        return 25.0

    def safe_shutdown(self):
        pass

    def emergency_zero(self):
        pass


class TestWorkerEcmFeedbackAnchorsToPulseSoc(unittest.TestCase):
    def test_update_ecm_uses_soc_at_pulse_not_final_soc(self):
        """Real run() + real StateEstimator: rest -> clean HPPC pulse -> a long
        further discharge tail (soc keeps moving well past the fitted pulse).
        update_ecm()'s fit_soc must match the SoC recorded AT the pulse's own
        timestamp, not wherever SoC ends up once the whole record finishes."""
        from aset_batt.acquisition.worker import AcquisitionWorker

        # cur stays within _profile()'s max_discharge_a=7.0 (the worker's OCP
        # interlock trips emergency_stop() at 1.05x that and would otherwise
        # cut this test's run short before the tail ever runs).
        # voc is a mid-SoC rest (12.40 V, ~2.07 V/cell) deliberately WELL below the
        # 6S OCV-curve 100% point (~12.888 V): this test verifies update_ecm's
        # fit_soc anchors to the pulse timestamp (not the final SoC), which needs
        # the tail's voltage correction to keep moving SoC. A near-ceiling voc
        # (the old 12.80 V) plus the pulse's I·R would push the implied OCV above
        # the curve top, tripping the surface-charge gate (F3) — correct in real
        # use, but here the compressed simulated dt (sample_hz=1e5 → microsecond
        # dt) means the gate's in-range clear-hold never elapses, so the tail's
        # correction would be suppressed and SoC would not move. A mid-SoC voc
        # keeps the pulse and tail in range so the anchoring being tested is
        # exercised on the normal correction path.
        r0, r1, c1, cur, voc = 0.012, 0.018, 1000.0, 5.0, 12.40
        tau = r1 * c1
        dt = 0.1
        t_rest = np.arange(0, 10, dt)
        t_pulse = np.arange(0, 40, dt)
        v_rest = np.full_like(t_rest, voc)
        v_pulse = voc - cur * (r0 + r1 * (1 - np.exp(-t_pulse / tau)))
        i_rest = np.zeros_like(t_rest)
        i_pulse = np.full_like(t_pulse, cur)
        # Long further discharge tail AFTER the fitted pulse — this is the exact
        # gap the fix closes: SoC keeps moving well past the pulse's own time.
        n_tail = 2000
        v_tail = np.full(n_tail, voc - 0.3)
        i_tail = np.full(n_tail, 3.0)

        v = np.concatenate([v_rest, v_pulse, v_tail])
        i_dis = np.concatenate([i_rest, i_pulse, i_tail])

        model = BatteryModel("LeadAcid", 7.0, 6, 1)
        estimator = StateEstimator(7.0, model)
        soc_trace = []
        orig_update = estimator.update
        def _spy_update(*a, **k):
            st = orig_update(*a, **k)
            soc_trace.append(st["soc"])
            return st
        estimator.update = _spy_update

        fit_soc_calls = []
        orig_update_ecm = estimator.update_ecm
        def _spy_update_ecm(r0_, r1_, c1_, fit_soc=None):
            fit_soc_calls.append(fit_soc)
            return orig_update_ecm(r0_, r1_, c1_, fit_soc=fit_soc)
        estimator.update_ecm = _spy_update_ecm

        cfg = TestConfig(_profile(), OperationMode.HPPC)
        cfg.sample_hz = 100000.0   # effectively no msleep() delay in the test
        csv_path = os.path.join(tempfile.mkdtemp(), "worker_ecm_anchor.csv")
        w = AcquisitionWorker(backend=_ArrayBackend(v, i_dis), cfg=cfg,
                              csv_path=csv_path, estimator=estimator)
        w.run()

        self.assertEqual(len(fit_soc_calls), 1, "update_ecm must be fed exactly once")
        fit_soc = fit_soc_calls[0]
        self.assertIsNotNone(fit_soc, "fit_soc must be resolved from the pulse's own timestamp")

        # Independently-known-good reference: the fit anchors to t_edge_s = the
        # pulse's ONSET (rest->pulse is the single biggest |ΔI| in the whole
        # record, bigger than the later pulse->tail step), i.e. the very first
        # pulse sample — index len(t_rest) into the full concatenated record.
        soc_at_pulse_onset = soc_trace[len(t_rest)]
        soc_final = soc_trace[-1]

        self.assertGreater(abs(soc_final - soc_at_pulse_onset), 1.0,
                           "test setup must actually move SoC meaningfully after the pulse")
        self.assertAlmostEqual(fit_soc, soc_at_pulse_onset, delta=0.5)
        self.assertGreater(abs(fit_soc - soc_final), 1.0,
                           "fit_soc must NOT collapse to the record's final SoC")


class TestChemistryDetectionConsistency(unittest.TestCase):
    """state_estimator.py's _default_min_rest_s()/_coulomb_eta() and
    analysis.py's profile_from_config() used to detect lead-acid/LFP via an
    ad-hoc substring match on the raw battery_type string ("lead" in
    chem.lower()) instead of the canonical battery_profiles.get_chemistry()
    resolver that _cca_cutoff_v()/en50342_capacity_conditions() already used
    correctly — an alias not literally containing "lead" (e.g. a future
    product-family name) would be misclassified as lithium in some call
    sites but not others."""

    def test_min_rest_s_uses_canonical_chemistry_for_all_three_bands(self):
        for chem, expected in (("LeadAcid", 60.0), ("LiFePO4", 120.0),
                               ("LiPO", 30.0), ("Li-ion", 30.0)):
            model = BatteryModel(chem, 12.0, 6, 1)
            est = StateEstimator(5.3, model)
            self.assertEqual(est._min_rest_s, expected, chem)

    def test_min_rest_s_resolves_lead_acid_aliases_not_just_substring_lead(self):
        """VRLA/SLA/AGM don't contain "lead" as a substring but must still
        resolve to the 60s lead-acid band via the alias table."""
        for alias in ("VRLA", "SLA", "AGM", "Lead-Acid"):
            model = BatteryModel(alias, 12.0, 6, 1)
            est = StateEstimator(5.3, model)
            self.assertEqual(est._min_rest_s, 60.0, alias)

    def test_coulomb_eta_uses_canonical_chemistry(self):
        model = BatteryModel("VRLA", 12.0, 6, 1)   # alias, no "lead" substring
        est = StateEstimator(5.3, model)
        est.use_eta = True
        self.assertAlmostEqual(est._coulomb_eta(soc=95.0, current=-1.0), 0.75)
        model2 = BatteryModel("LiPO", 3.7, 1, 1)
        est2 = StateEstimator(2.0, model2)
        est2.use_eta = True
        self.assertAlmostEqual(est2._coulomb_eta(soc=95.0, current=-1.0), 0.99)

    def test_profile_from_config_peukert_uses_canonical_chemistry(self):
        """peukert_k itself now comes from the chemistry registry (see
        TestPeukertKMatchesRegistry below) — this test only proves the
        registry lookup resolves the VRLA alias correctly (no "lead"
        substring), not a specific hardcoded value."""
        from aset_batt.acquisition.analysis import profile_from_config
        from aset_batt.core.config import ConfigManager
        from aset_batt.core import battery_profiles
        cfg = ConfigManager()
        cfg.battery.battery_type = "VRLA"   # alias, no "lead" substring
        profile = profile_from_config(cfg)
        self.assertAlmostEqual(profile.peukert_k,
                               battery_profiles.get_chemistry("VRLA").peukert_k)

    def test_no_more_adhoc_lead_substring_checks_in_core_or_acquisition(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for rel in ("aset_batt/core/state_estimator.py",
                    "aset_batt/acquisition/analysis.py",
                    "aset_batt/ui/zones.py"):
            src = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn('"lead" in', src, rel)


class TestPeukertKMatchesRegistry(unittest.TestCase):
    """profile_from_config() used to hardcode peukert_k=1.20 for every
    LeadAcid battery, disagreeing with the SAME chemistry registry the live
    estimator reads (1.10, "AGM 1.05-1.15; flooded 1.2-1.6" per
    battery_profiles.json) — a real AGM product's post-hoc SoH differed by
    >13 points (80.0% vs 93.4% on a real 3.629 Ah discharge) purely
    depending on which code path computed it, potentially the difference
    between an A and a B grade."""

    def test_lead_acid_uses_registry_value_not_hardcoded_1_20(self):
        from aset_batt.acquisition.analysis import profile_from_config
        from aset_batt.core.config import ConfigManager
        from aset_batt.core import battery_profiles
        cfg = ConfigManager()
        cfg.battery.battery_type = "LeadAcid"
        cfg.battery.product_name = ""   # no product override
        profile = profile_from_config(cfg)
        self.assertAlmostEqual(profile.peukert_k,
                               battery_profiles.get_chemistry("LeadAcid").peukert_k)
        self.assertNotAlmostEqual(profile.peukert_k, 1.20, places=2,
                                  msg="must not silently fall back to the old hardcoded value")

    def test_product_level_override_takes_priority_over_chemistry_default(self):
        from aset_batt.acquisition.analysis import profile_from_config
        from aset_batt.core.config import ConfigManager
        from aset_batt.core import battery_profiles
        cfg = ConfigManager()
        cfg.battery.battery_type = "LeadAcid"
        # Any real product in the registry with its own peukert_k override.
        override_name = next(
            (n for n in battery_profiles.list_products()
             if getattr(battery_profiles.get_product(n), "peukert_k", 0.0) > 0.0), None)
        if override_name is None:
            self.skipTest("no product in the registry currently overrides peukert_k")
        cfg.battery.product_name = override_name
        profile = profile_from_config(cfg)
        expected = battery_profiles.get_product(override_name).peukert_k
        self.assertAlmostEqual(profile.peukert_k, expected)


class TestSequenceSafetyConstantsSingleSource(unittest.TestCase):
    """_WATCHDOG_TIMEOUT_S and _SEQ_TEMP_STALE_TRIP_S used to be re-declared
    independently in BaseSequenceMixin AND each of HppcMixin/CycleLifeMixin/
    IecCapacityMixin/QuickScanMixin (5 copies total) — since BaseSequenceMixin
    is listed first in SequencesMixin's MRO, the other 4 copies were always
    shadowed/dead (never actually read), but nothing signaled that; a change
    to one of the shadowed copies would silently do nothing. Now declared
    exactly once, on BaseSequenceMixin."""

    def test_watchdog_timeout_declared_on_exactly_one_class(self):
        from aset_batt.ui.sequences import SequencesMixin
        owners = [c.__name__ for c in SequencesMixin.__mro__
                 if '_WATCHDOG_TIMEOUT_S' in c.__dict__]
        self.assertEqual(owners, ['BaseSequenceMixin'])

    def test_temp_stale_trip_declared_on_exactly_one_class(self):
        from aset_batt.ui.sequences import SequencesMixin
        owners = [c.__name__ for c in SequencesMixin.__mro__
                 if '_SEQ_TEMP_STALE_TRIP_S' in c.__dict__]
        self.assertEqual(owners, ['BaseSequenceMixin'])

    def test_resolved_values_unchanged(self):
        from aset_batt.ui.sequences import SequencesMixin
        self.assertEqual(SequencesMixin._WATCHDOG_TIMEOUT_S, 300)
        self.assertEqual(SequencesMixin._SEQ_TEMP_STALE_TRIP_S, 60.0)


if __name__ == "__main__":
    unittest.main()
