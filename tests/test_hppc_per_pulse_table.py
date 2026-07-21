"""Per-pulse HPPC breakdown (identify_hppc_pulses + report table).

The aggregated ECM fit reports ONE pulse and one whole-record OCV anchor, so a
systematic rest-anchor drift across the pulse train was invisible in the report:
the real run sessions/test_HPPC_20260708_152502.csv rested 190 mV above
equilibrium at pulse 1 and relaxed monotonically across its 5 pulses
(anchor 13.340→13.150 V) — every anchor-referenced R0 estimator "declined"
27-37% over 15 minutes purely from that moving anchor (detrended repeatability
was ~5%). These tests pin the per-pulse table that makes the trend visible,
using both a synthetic record with injected drift and the real CSV's exact
numbers.
"""
import os
import unittest

import numpy as np

from aset_batt.acquisition.analysis import (
    identify_hppc_pulses, _hppc_pulse_summary, analyze_csv,
    _HPPC_ANCHOR_DRIFT_WARN_V,
)
from aset_batt.acquisition.models import BatteryProfile

_REAL_CSV = os.path.join(os.path.dirname(__file__), "..", "sessions",
                         "test_HPPC_20260708_152502.csv")


def _make_profile(**overrides):
    kwargs = dict(
        name="FB FTZ6V (12V 5.3Ah VRLA AGM)", chemistry="LeadAcid",
        nominal_v=12.0, series=6, capacity_ah=5.3,
        max_charge_v=14.7, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=5.3,
        ovp=15.0, uvp=10.0, otp_warn=45.0, otp_crit=55.0, internal_r=0.030,
    )
    kwargs.update(overrides)
    return BatteryProfile(**kwargs)


def _synthetic_hppc(n_pulses=5, anchor0=12.80, drift_per_pulse=0.0,
                    r0=0.030, r1=0.060, tau=5.0, i_pulse=5.3,
                    pulse_s=30.0, rest_s=60.0, hz=10.0):
    """Rest→pulse train with a 1-RC response and an optional linear rest-anchor
    drift (models surface charge dissipating between pulses). Returns
    (t, i, v, temp)."""
    dt = 1.0 / hz
    t, i, v = [], [], []
    tt = 0.0
    for p in range(n_pulses):
        anchor = anchor0 + p * drift_per_pulse
        for _ in range(int(rest_s * hz)):
            t.append(tt); i.append(0.0); v.append(anchor)
            tt += dt
        for k in range(int(pulse_s * hz)):
            ts = k * dt
            vk = anchor - i_pulse * (r0 + r1 * (1.0 - np.exp(-ts / tau)))
            t.append(tt); i.append(i_pulse); v.append(vk)
            tt += dt
    # trailing rest so the last pulse has a clean off-edge
    anchor = anchor0 + n_pulses * drift_per_pulse
    for _ in range(int(rest_s * hz)):
        t.append(tt); i.append(0.0); v.append(anchor)
        tt += dt
    n = len(t)
    return (np.asarray(t), np.asarray(i), np.asarray(v), np.full(n, 25.0))


class TestSyntheticDriftDecomposition(unittest.TestCase):
    def test_five_pulses_found_with_correct_anchors(self):
        drift = -0.040   # V per pulse → −160 mV across 5 pulses
        t, i, v, temp = _synthetic_hppc(drift_per_pulse=drift)
        pulses = identify_hppc_pulses(t, i, v, temp, _make_profile())
        self.assertEqual(len(pulses), 5)
        for k, p in enumerate(pulses):
            self.assertAlmostEqual(p["anchor_v"], 12.80 + k * drift, places=3)
            self.assertAlmostEqual(p["i_pulse_a"], 5.3, places=2)
            self.assertFalse(p["edge_stale"], "10 Hz edge must not be stale")
            # per-pulse fit must recover the injected parameters despite the
            # drifting anchor (each pulse fits with its OWN anchor)
            self.assertAlmostEqual(p["r0_fit_mohm"], 30.0, delta=6.0)
            self.assertAlmostEqual(p["tau_fit_s"], 5.0, delta=1.5)

    def test_drift_summary_warns_and_r0_stays_flat(self):
        t, i, v, temp = _synthetic_hppc(drift_per_pulse=-0.040)
        pulses = identify_hppc_pulses(t, i, v, temp, _make_profile())
        warnings: list = []
        drift_v, cv, warnings = _hppc_pulse_summary(pulses, warnings)
        self.assertAlmostEqual(drift_v, -0.160, places=3)
        self.assertGreater(abs(drift_v), _HPPC_ANCHOR_DRIFT_WARN_V)
        self.assertTrue(any("anchor drifted" in w for w in warnings))
        # with each pulse fit against its OWN anchor, fit R0 must NOT inherit
        # the drift trend (this is the whole point of the per-pulse fit)
        r0s = [p["r0_fit_mohm"] for p in pulses]
        self.assertLess(cv, 10.0,
                        f"per-pulse fit R0 should stay flat, got {r0s}")

    def test_no_drift_no_warning(self):
        t, i, v, temp = _synthetic_hppc(drift_per_pulse=0.0)
        pulses = identify_hppc_pulses(t, i, v, temp, _make_profile())
        warnings: list = []
        drift_v, cv, warnings = _hppc_pulse_summary(pulses, warnings)
        self.assertAlmostEqual(drift_v, 0.0, places=3)
        self.assertEqual(warnings, [])

    def test_single_pulse_returns_empty(self):
        t, i, v, temp = _synthetic_hppc(n_pulses=1)
        pulses = identify_hppc_pulses(t, i, v, temp, _make_profile())
        self.assertEqual(pulses, [])


class TestRealCsvPerPulse(unittest.TestCase):
    """Replays the actual corrupted run. Exact numbers from the Phase-A deep
    analysis (2026-07-13): anchors 13.340→13.150 V (−190 mV), edge latency
    1.3–3.1 s (all 5 edges stale vs the 0.5 s gate — the run predates the 5 Hz
    relax-leg pacing fix and the USB-selective-suspend fix)."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(_REAL_CSV):
            raise unittest.SkipTest("real HPPC CSV not present")
        cls.res = analyze_csv(_REAL_CSV, _make_profile(), force_hppc=True)

    def test_five_pulses_with_documented_anchor_drift(self):
        pulses = self.res["hppc_pulses"]
        self.assertEqual(len(pulses), 5)
        self.assertAlmostEqual(pulses[0]["anchor_v"], 13.340, places=3)
        self.assertAlmostEqual(pulses[-1]["anchor_v"], 13.150, places=3)
        self.assertAlmostEqual(self.res["hppc_anchor_drift_v"], -0.190, places=3)

    def test_all_edges_flagged_stale(self):
        # 1.3-3.1 s post-edge latency on every pulse — the edge R0 numbers are
        # RC-contaminated and the table must say so.
        for p in self.res["hppc_pulses"]:
            self.assertTrue(p["edge_stale"],
                            f"pulse {p['idx']} edge_dt={p['edge_dt_s']}s")
            self.assertGreater(p["edge_dt_s"], 0.5)

    def test_drift_warning_present(self):
        self.assertTrue(any("anchor drifted" in w
                            for w in self.res["quality_warnings"]))

    def test_report_renders_per_pulse_table(self):
        from aset_batt.ui import theme
        theme.set_theme("light")
        from aset_batt.ui.report_html import build_results_html
        html = build_results_html(self.res)
        self.assertIn("Per-pulse breakdown", html)
        self.assertIn("anchor drift", html)
        self.assertIn("Pulse 5", html)


class TestNonHppcRecordHasNoPulseTable(unittest.TestCase):
    def test_constant_discharge_yields_empty(self):
        hz, dur = 10.0, 120.0
        n = int(hz * dur)
        t = np.arange(n) / hz
        i = np.concatenate([np.zeros(50), np.full(n - 50, 1.06)])
        v = np.concatenate([np.full(50, 12.8), np.linspace(12.6, 11.0, n - 50)])
        pulses = identify_hppc_pulses(t, i, v, np.full(n, 25.0), _make_profile())
        self.assertEqual(pulses, [])


if __name__ == "__main__":
    unittest.main()
