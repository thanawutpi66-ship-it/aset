"""Regression test: each HPPC pulse cycle must feed its own real R0/R1/C1 fit
into the live estimator (update_ecm()), not just leave it for the post-hoc
analyze_csv() pass at the very end of the sequence.

Root cause: sequences.py's HPPC pulse leg deliberately never calls
estimator.update() per-sample (R0/R1/C1/tau are meant to be fit from the whole
pulse afterwards) -- but nothing ever actually did that "afterwards" fit
against the live estimator. Confirmed on a real HPPC test's own CSV:
Rin_Calibrated was 0% for the entire ~4.8-hour file, and the live
Resistance_mOhm stayed flat at the generic uncalibrated default through all 5
real pulse cycles, despite the post-hoc analysis fitting a clean R0=25.75mOhm/
R1=68.40mOhm/R^2=0.955 from that same data.

The exact fit-and-feed code block (identify_ecm_fit -> harness correction ->
estimator.update_ecm) is exercised directly here, the same "reproduce the real
statements, not the whole multi-hour thread" pattern
tests/test_graph_feed_during_sequences.py already uses for the relax leg --
driving the entire _hppc_seq_thread (PREPARE OCV-settle, CHARGE, 30 min REST,
N pulse cycles) end-to-end would need extensive real-time mocking for no extra
coverage of the block actually being tested.
"""
import os
import re
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.acquisition.analysis import identify_ecm_fit, _correct_for_harness_r

_SEQUENCES_PY = Path(__file__).resolve().parent.parent / "aset_batt" / "ui" / "sequences" / "hppc.py"


def _synthetic_pulse(r0, r1, c1, current, voc, dt=0.2, pulse_s=30.0, rest_s=1.0,
                     noise_v=0.001, seed=0):
    """rest -> pulse, matching sequences.py's real fit-and-feed buffer shape: a
    few rest samples (i=0) immediately followed by the pulse -- identify_ecm_fit()
    needs to see the actual edge to locate the step, not just the pulse's own
    already-loaded current throughout."""
    rng = np.random.default_rng(seed)
    tau = r1 * c1
    t_rest = np.arange(-rest_s, 0.0, dt)
    t_pulse = np.arange(0.0, pulse_s, dt)
    v_rest = np.full_like(t_rest, voc)
    i_rest = np.zeros_like(t_rest)
    v_pulse = voc - current * (r0 + r1 * (1.0 - np.exp(-t_pulse / tau)))
    i_pulse = np.full_like(t_pulse, current)
    t = np.concatenate([t_rest, t_pulse])
    v = np.concatenate([v_rest, v_pulse]) + rng.normal(0, noise_v, t.size)
    i = np.concatenate([i_rest, i_pulse])
    return t.tolist(), i.tolist(), v.tolist()


class TestPerCyclePulseFeedsLiveEstimator(unittest.TestCase):
    """Reproduces the exact fit-and-feed statements sequences.py's HPPC pulse
    leg runs right after load_off(), with a synthetic pulse of known R0/R1/C1
    standing in for _fit_t/_fit_i/_fit_v collected during the real loop."""

    def _make_estimator(self, harness_r_ohm=0.0):
        cfg = ConfigManager()
        cfg.battery.harness_resistance_ohm = harness_r_ohm
        model = BatteryModel(cfg.battery.battery_type, cfg.battery.rated_capacity,
                             cfg.battery.cells_series, cfg.battery.cells_parallel)
        estimator = StateEstimator(cfg.battery.rated_capacity, model)
        return estimator, cfg

    def _run_fit_and_feed(self, estimator, cfg, fit_t, fit_i, fit_v, voc):
        """The exact logic from sequences.py's post-pulse block."""
        ecm, _reason = identify_ecm_fit(fit_t, fit_i, fit_v, voc)
        if ecm is None:
            return None
        r0 = float(ecm["R0_ohm"])
        harness_r = max(0.0, float(getattr(cfg.battery, "harness_resistance_ohm", 0.0)))
        if harness_r > 0.0:
            r0, _warn = _correct_for_harness_r(r0, harness_r, "live ECM R0", [])
        estimator.update_ecm(r0, float(ecm["R1_ohm"]), float(ecm["C1_farad"]))
        return ecm

    def test_good_pulse_feeds_update_ecm_and_flips_calibrated(self):
        estimator, cfg = self._make_estimator()
        self.assertFalse(estimator._ecm_calibrated)
        # tau = R1*C1 = 0.068*73.5 = 5s -- matches this session's real HPPC fit
        # (tau=5.0s) and, at a 30s pulse, is well-resolved (~6x tau), unlike a
        # tau comparable to/longer than the pulse itself.
        r0_true, r1_true, c1_true = 0.025, 0.068, 73.5
        voc = 13.15
        t, i, v = _synthetic_pulse(r0_true, r1_true, c1_true, 5.3, voc)
        estimator._ensure_ekf()   # update_ecm() is a no-op until the EKF exists
        ecm = self._run_fit_and_feed(estimator, cfg, t, i, v, voc)
        self.assertIsNotNone(ecm)
        self.assertTrue(estimator._ecm_calibrated)
        self.assertAlmostEqual(estimator._ekf.R0, r0_true, delta=0.15 * r0_true)
        self.assertAlmostEqual(estimator._ekf.R1, r1_true, delta=0.25 * r1_true)

    def test_harness_resistance_is_subtracted_from_the_live_r0(self):
        harness = 0.005
        estimator, cfg = self._make_estimator(harness_r_ohm=harness)
        estimator._ensure_ekf()
        # tau = R1*C1 = 0.068*73.5 = 5s -- matches this session's real HPPC fit
        # (tau=5.0s) and, at a 30s pulse, is well-resolved (~6x tau), unlike a
        # tau comparable to/longer than the pulse itself.
        r0_true, r1_true, c1_true = 0.025, 0.068, 73.5
        voc = 13.15
        t, i, v = _synthetic_pulse(r0_true, r1_true, c1_true, 5.3, voc)
        self._run_fit_and_feed(estimator, cfg, t, i, v, voc)
        # The R0 fed to the live estimator should be the harness-corrected
        # value, not the raw fit -- consistent with what analyze_csv() reports
        # in the final post-hoc grade.
        self.assertAlmostEqual(estimator._ekf.R0, r0_true - harness, delta=0.15 * r0_true)

    def test_insufficient_samples_does_not_feed_a_bad_fit(self):
        """A too-short buffer (fewer than fit_model's own 10-sample minimum)
        must be skipped (kept generic), not silently poison the live
        estimator -- e.g. a cycle cut short by a safety trip mid-pulse."""
        estimator, cfg = self._make_estimator()
        estimator._ensure_ekf()
        r0_before = estimator._ekf.R0
        t, i, v = _synthetic_pulse(0.025, 0.068, 73.5, 5.3, 13.15,
                                   pulse_s=0.4, rest_s=0.4)   # well under 10 samples total
        ecm = self._run_fit_and_feed(estimator, cfg, t, i, v, 13.15)
        self.assertIsNone(ecm)
        self.assertFalse(estimator._ecm_calibrated)
        self.assertEqual(estimator._ekf.R0, r0_before)


class TestSourcePatternWiresTheFeedIntoThePulseLoop(unittest.TestCase):
    """Cheap regression guard: confirms the fit-and-feed block actually sits
    where it needs to (inside _hppc_seq_thread, right after the pulse leg's
    load_off()) so this coverage can't silently rot if the block is moved or
    deleted without a test noticing."""

    def setUp(self):
        self.src = _SEQUENCES_PY.read_text(encoding="utf-8")
        start = self.src.index("def _hppc_seq_thread")
        end = self.src.find("\n    def ", start + 1)
        if end == -1: end = len(self.src)
        self.hppc_src = self.src[start:end]

    def test_pulse_loop_buffers_samples_for_the_fit(self):
        for name in ("_fit_t.append", "_fit_i.append", "_fit_v.append"):
            self.assertIn(name, self.hppc_src)

    def test_update_ecm_is_called_after_the_pulse_loop(self):
        self.assertIn("identify_ecm_fit", self.hppc_src)
        self.assertIn("estimator.update_ecm(", self.hppc_src)
        # Must come after load_off() (the pulse-end edge), not before/during
        # the pulse's own active-load loop.
        load_off_idx = self.hppc_src.index("self.hw.load_off()")
        update_ecm_idx = self.hppc_src.index("estimator.update_ecm(")
        self.assertGreater(update_ecm_idx, load_off_idx)


if __name__ == "__main__":
    unittest.main()
