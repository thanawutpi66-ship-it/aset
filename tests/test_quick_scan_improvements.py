"""Quick Scan accuracy + speed fix.

Real run sessions/test_QuickScan_20260712_150458.csv showed two problems:

1. Resistance results were fake: the only current edge was stale (10.36s
   latency) -> identify_dcir n_steps=0 -> dcir_mohm=30.0 was the profile
   FALLBACK, not a measurement -- yet grade=C and cca_est=189.7A were computed
   from it (confidence 0.392). All ECM fields NaN (is_hppc=False so
   identify_ecm_fit never ran).
2. It wasn't quick anymore: after the settle-everywhere fix, the sequence ran
   calibrate_from_ocv_stable() 3x (lead-acid: 300s floor, 1200s timeout each)
   plus a fixed 5-min REST -- fixed overhead 20-65 min, with settle #1 and
   settle #2 back-to-back and NOTHING disturbing the battery between them.

Fix: merge the duplicate settle into one, add a 30s mini-pulse DCIR/ECM leg
(same pattern as HPPC's own pulse leg, real-data validated R2=0.94-0.99 on
this rig/chemistry), fresh edge pair at the main discharge's load-off, and
replace the third settle with a short fixed tail rest. analyze_series() gains
a `fit_ecm` flag (NOT force_hppc, which would kill SoH) so the mini-pulse's
fit reaches the report even for a non-HPPC record, with a promotion fallback
for when the whole-record fit's edge-detector picks the wrong (bigger) edge.

These tests cover: the fit_ecm flag + promotion logic (synthetic records,
analysis.py-level) and source-pattern guards pinning that quick_scan.py's
thread actually wires the new leg in correctly (same technique
tests/test_hppc_adaptive_relax.py uses).
"""
import os
import unittest
from pathlib import Path

import numpy as np

from aset_batt.acquisition.analysis import analyze_series, analyze_csv
from aset_batt.acquisition.models import BatteryProfile

_QUICK_SCAN_PY = (Path(__file__).resolve().parent.parent / "aset_batt" / "ui"
                  / "sequences" / "quick_scan.py")
_REAL_CSV = os.path.join(os.path.dirname(__file__), "..", "sessions",
                         "test_QuickScan_20260712_150458.csv")


def _make_profile(**overrides):
    kwargs = dict(
        name="FB FTZ6V (12V 5.3Ah VRLA AGM)", chemistry="LeadAcid",
        nominal_v=12.0, series=6, capacity_ah=5.3,
        max_charge_v=14.7, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=5.3,
        ovp=15.0, uvp=10.0, otp_warn=45.0, otp_crit=55.0, internal_r=0.030,
    )
    kwargs.update(overrides)
    return BatteryProfile(**kwargs)


def _quick_scan_shaped_record(i_pulse=2.0, i_discharge=6.0,
                              r0=0.030, r1=0.050, tau=4.5,
                              anchor=12.60, cutoff_v=10.50,
                              pulse_s=30.0, relax_s=90.0,
                              discharge_s=1800.0, hz_fast=10.0, hz_slow=1.0):
    """rest -> mini-pulse (1-RC, fast/dense) -> relax -> long discharge (slow/
    sparse, OCV declining linearly toward cutoff) -> ends at cutoff, no trailing
    rest -- mirrors the real CSV's own shape (file ended under load).

    i_discharge > i_pulse on purpose: fit_model's _detect_step() picks the
    LARGEST |delta-I| edge, so the whole-record fit targets the discharge
    edge, not the pulse -- and correctly fails there (a slow multi-minute SoC
    decline doesn't match a ~5s RC exponential), exercising the promotion
    fallback rather than the aggregate fit succeeding directly.
    """
    dt_f = 1.0 / hz_fast
    t, i, v = [], [], []
    tt = 0.0
    # initial rest (dense, gives identify_hppc_pulses its anchor + fit_ecm's
    # trailing-tail seeding an equivalent to hppc.py's own pattern)
    for _ in range(int(60 * hz_fast)):
        t.append(tt); i.append(0.0); v.append(anchor)
        tt += dt_f
    # mini-pulse: clean 1-RC response, easily fit-able (R2 should be high)
    for k in range(int(pulse_s * hz_fast)):
        ts = k * dt_f
        vk = anchor - i_pulse * (r0 + r1 * (1.0 - np.exp(-ts / tau)))
        t.append(tt); i.append(i_pulse); v.append(vk)
        tt += dt_f
    v_after_pulse = v[-1]
    # relax back toward the anchor
    for k in range(int(relax_s * hz_fast)):
        ts = k * dt_f
        vk = v_after_pulse + (anchor - v_after_pulse) * (1.0 - np.exp(-ts / tau))
        t.append(tt); i.append(0.0); v.append(vk)
        tt += dt_f
    # long discharge: slow/sparse sampling, OCV declines ~linearly with a
    # small constant IR drop -- shape a 1-RC exponential cannot match.
    dt_s = 1.0 / hz_slow
    n_dis = int(discharge_s * hz_slow)
    v_start = v[-1] - i_discharge * r0    # instantaneous IR drop at the edge
    for k in range(n_dis):
        frac = k / max(1, n_dis - 1)
        vk = v_start - frac * (v_start - cutoff_v)
        t.append(tt); i.append(i_discharge); v.append(vk)
        tt += dt_s
    n = len(t)
    return (np.asarray(t), np.asarray(i), np.asarray(v), np.full(n, 25.0))


def _plain_discharge_record(i_discharge=6.0, anchor=12.60, cutoff_v=10.50,
                            discharge_s=1800.0, hz=1.0):
    """rest -> single long discharge, no pulse at all -- today's actual Quick
    Scan record shape (pre-mini-pulse-fix), used to pin fit_ecm=None's
    backward-compat behavior."""
    dt = 1.0 / hz
    t, i, v = [], [], []
    tt = 0.0
    for _ in range(int(60 * 10.0)):
        t.append(tt); i.append(0.0); v.append(anchor)
        tt += 0.1
    n_dis = int(discharge_s * hz)
    v_start = anchor - i_discharge * 0.030
    for k in range(n_dis):
        frac = k / max(1, n_dis - 1)
        vk = v_start - frac * (v_start - cutoff_v)
        t.append(tt); i.append(i_discharge); v.append(vk)
        tt += dt
    n = len(t)
    q = np.cumsum(np.clip(np.asarray(i, float), 0, None)
                  * np.diff(np.asarray(t, float), prepend=0.0)) / 3600.0
    return (np.asarray(t), np.asarray(i), np.asarray(v), np.full(n, 25.0), q)


class TestFitEcmPromotesMiniPulse(unittest.TestCase):
    def test_mini_pulse_fit_reaches_report_via_promotion(self):
        t, i, v, temp = _quick_scan_shaped_record()
        q = np.cumsum(np.clip(i, 0, None) * np.diff(t, prepend=t[0])) / 3600.0
        res = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False,
                             fit_ecm=True)
        # SoH must still be computed -- this is the whole point of fit_ecm
        # existing separately from force_hppc.
        self.assertFalse(np.isnan(res["soh"]), "fit_ecm=True must not suppress SoH")
        self.assertTrue(res["ecm_identified"],
                        "mini-pulse fit should reach the report via the promotion "
                        "fallback even though the whole-record fit targets the "
                        "(bigger, badly-fitting) discharge edge")
        # R0/R1 should be in the right ballpark of the injected values (30/50 mOhm)
        self.assertAlmostEqual(res["r0_mohm"], 30.0, delta=8.0)
        self.assertAlmostEqual(res["r1_mohm"], 50.0, delta=15.0)
        self.assertGreater(res["ecm_r2"], 0.90)
        # Grade must come from the dual-resistance path now that ECM is real.
        self.assertIn(res["grade"], ("A", "B", "C", "REJECT"))

    def test_fit_ecm_none_defaults_to_is_hppc_unchanged(self):
        """Backward-compat pin: a caller that never passes fit_ecm (every
        existing call site before this fix) must get byte-identical behavior
        to before -- is_hppc=False, fit_ecm=None -> no fit attempted at all,
        matching today's real Quick Scan CSV (all ECM fields NaN)."""
        t, i, v, temp, q = _plain_discharge_record()
        res_default = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False)
        res_explicit_none = analyze_series(t, i, v, temp, q, _make_profile(),
                                           is_hppc=False, fit_ecm=None)
        self.assertFalse(res_default["ecm_identified"])
        self.assertEqual(res_default["ecm_identified"], res_explicit_none["ecm_identified"])
        self.assertEqual(res_default["hppc_pulses"], [])
        self.assertFalse(np.isnan(res_default["soh"]))

    def test_fit_ecm_true_does_not_suppress_soh_unlike_force_hppc(self):
        """The exact bug the fit_ecm flag exists to avoid: is_hppc=True would
        zero out SoH (analyze_series only computes it when NOT is_hppc)."""
        t, i, v, temp = _quick_scan_shaped_record()
        q = np.cumsum(np.clip(i, 0, None) * np.diff(t, prepend=t[0])) / 3600.0
        res_wrong = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=True)
        res_right = analyze_series(t, i, v, temp, q, _make_profile(), is_hppc=False,
                                   fit_ecm=True)
        self.assertTrue(np.isnan(res_wrong["soh"]),
                        "sanity check: is_hppc=True really does suppress SoH")
        self.assertFalse(np.isnan(res_right["soh"]),
                         "fit_ecm=True with is_hppc=False must compute SoH")


class TestRealQuickScanCsvUnaffected(unittest.TestCase):
    """The real (pre-fix) CSV has no mini-pulse in it at all -- fit_ecm=True
    must not corrupt or change its SoH, and must gracefully find nothing to
    promote (no qualifying second edge)."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(_REAL_CSV):
            raise unittest.SkipTest("real Quick Scan CSV not present")

    def test_soh_unchanged_ecm_gracefully_absent(self):
        res_old = analyze_csv(_REAL_CSV, _make_profile())
        res_new = analyze_csv(_REAL_CSV, _make_profile(), fit_ecm=True)
        self.assertAlmostEqual(res_old["soh"], res_new["soh"], places=2)
        self.assertAlmostEqual(res_old["capacity_ah"], res_new["capacity_ah"], places=3)
        self.assertFalse(res_new["ecm_identified"],
                         "no mini-pulse in this legacy record -> nothing to promote")


class TestSourcePatternWiresMiniPulseIntoThread(unittest.TestCase):
    """Text-level guards pinning that _quick_scan_thread actually calls the
    tested machinery in the right place -- same technique
    tests/test_hppc_adaptive_relax.py uses, for the same reason: the analysis-
    level tests above are worthless if the thread quietly stops calling any
    of this."""

    def setUp(self):
        src = _QUICK_SCAN_PY.read_text(encoding="utf-8")
        start = src.index("def _quick_scan_thread")
        end = src.find("\n    def ", start + 1)
        self.qs_src = src[start:end if end != -1 else len(src)]

    def test_only_one_settle_call_left(self):
        # Count actual call sites only (self.controller.calibrate_from_ocv_stable()
        # invocations) -- not the explanatory comments that mention the name too.
        self.assertEqual(
            self.qs_src.count("self.controller.calibrate_from_ocv_stable("), 1,
            "Phase 0 and Phase 1's old post-rest settle must be merged into one call")

    def test_mini_pulse_paced_at_default_sample_hz(self):
        self.assertIn("DEFAULT_SAMPLE_HZ", self.qs_src)

    def test_mini_pulse_calls_identify_ecm_fit_and_feeds_estimator(self):
        self.assertIn("identify_ecm_fit(_fit_t, _fit_i, _fit_v, voc_for_fit)", self.qs_src)
        self.assertIn("estimator.update_ecm(", self.qs_src)

    def test_mini_pulse_has_uvp_floor_check(self):
        self.assertIn("self._uvp_floor()", self.qs_src)
        self.assertIn("quick_load_floor", self.qs_src)

    def test_mini_pulse_constants_used(self):
        self.assertIn("QUICK_MINI_PULSE_S", self.qs_src)
        self.assertIn("QUICK_MINI_RELAX_S", self.qs_src)
        self.assertIn("QUICK_TAIL_REST_S", self.qs_src)

    def test_fresh_edge_pair_at_discharge_load_off(self):
        # both a fresh pre-edge (prefer_load_v=True) and immediate post-edge
        # (prefer_load_v=False) sample must exist around the MAIN DISCHARGE's
        # own set_load(False) -- not the mini-pulse's earlier one -- the real
        # CSV was missing this edge entirely (file ended under load).
        dis_start = self.qs_src.index("Phase 2: DISCHARGE")
        idx_off = self.qs_src.index("self.hw.set_load(False)", dis_start)
        window = self.qs_src[max(0, idx_off - 600):idx_off + 400]
        self.assertIn("prefer_load_v=True", window)
        self.assertIn("prefer_load_v=False", window)

    def test_analyze_called_with_fit_ecm_true(self):
        self.assertIn("_auto_analyze(fit_ecm=True)", self.qs_src)


class TestEtaNoLongerHardcoded(unittest.TestCase):
    def test_on_quick_scan_computes_eta_from_soc(self):
        src = _QUICK_SCAN_PY.read_text(encoding="utf-8")
        start = src.index("def _on_quick_scan")
        end = src.find("\n    def ", start + 1)
        on_qs_src = src[start:end if end != -1 else len(src)]
        self.assertNotIn("eta_min=90)", on_qs_src)
        self.assertIn("soc_now", on_qs_src)


if __name__ == "__main__":
    unittest.main()
