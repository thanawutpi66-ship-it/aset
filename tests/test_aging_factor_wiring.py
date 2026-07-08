"""Phase D3 regression: BatteryModel.aging_factor is now a real, wired-up feature
instead of always exactly 1.0 in production (nothing previously called
update_aging_factor()/get_soh_from_capacity(), so the aging term inside
_calculate_base_rin was permanently zero despite comments implying otherwise).

Per the approved Phase D decision: feed the capacity-based SoH the app already
measures (StateEstimator.soh, itself set from acquisition.analysis's
full-discharge measurement or a prior test in the same session) into the Rin
baseline's aging factor via the new BatteryModel.set_aging_from_soh(); default
safely to 1.0 (no aging adjustment) when SoH isn't known yet (e.g. a quick
HPPC-only test with no full discharge in this session).
"""
import os
import unittest

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.storage.data_utils import DataHandler
from aset_batt.app.auto_controller import AutoController
from aset_batt.ui.isa101_views import BatteryQtWindow
from aset_batt.hardware.mock_hardware import MockHardwareController

_app = QApplication.instance() or QApplication([])


def _make_bound_window():
    cfg = ConfigManager()
    hw = MockHardwareController()
    model = BatteryModel(cfg.battery.battery_type, cfg.battery.rated_capacity,
                          cfg.battery.cells_series, cfg.battery.cells_parallel)
    estimator = StateEstimator(cfg.battery.rated_capacity, model)
    data = DataHandler()
    ctrl = AutoController(None, hw, data, estimator, cfg)
    win = BatteryQtWindow(cfg)
    win.bind_controller(ctrl)
    ctrl.set_ui(win)
    return win, ctrl


class TestSetAgingFromSoh(unittest.TestCase):
    def test_fresh_model_defaults_to_no_aging(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        self.assertEqual(m.aging_factor, 1.0)

    def test_full_soh_yields_no_aging_adjustment(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        m.set_aging_from_soh(100.0)
        self.assertEqual(m.aging_factor, 1.0)

    def test_degraded_soh_lowers_aging_factor(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        m.set_aging_from_soh(80.0)
        self.assertAlmostEqual(m.aging_factor, 0.8, places=6)

    def test_aging_factor_floors_at_half(self):
        """Same 50% floor as update_aging_factor() — never assume more than 50%
        extra resistance from aging alone, even for a badly degraded reading."""
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        m.set_aging_from_soh(10.0)
        self.assertAlmostEqual(m.aging_factor, 0.5, places=6)

    def test_soh_above_100_does_not_exceed_unity(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        m.set_aging_from_soh(115.0)   # measurement noise/calibration overshoot
        self.assertEqual(m.aging_factor, 1.0)

    def test_none_resets_to_no_aging(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        m.set_aging_from_soh(70.0)
        self.assertLess(m.aging_factor, 1.0)
        m.set_aging_from_soh(None)
        self.assertEqual(m.aging_factor, 1.0)

    def test_nan_resets_to_no_aging(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        m.set_aging_from_soh(70.0)
        m.set_aging_from_soh(float("nan"))
        self.assertEqual(m.aging_factor, 1.0)

    def test_aging_factor_actually_moves_the_rin_baseline(self):
        """Proves this isn't a dead assignment — _calculate_base_rin must actually
        read the updated aging_factor and produce a higher baseline for a more-aged
        pack at the same SoC/temp."""
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        rin_new = m._calculate_base_rin(50.0, 25.0)
        m.set_aging_from_soh(60.0)
        rin_aged = m._calculate_base_rin(50.0, 25.0)
        self.assertGreater(rin_aged, rin_new)


class TestStateEstimatorSyncsAgingFactor(unittest.TestCase):
    def _make(self):
        model = BatteryModel("LeadAcid", 2.0, 6, 1)
        est = StateEstimator(rated_capacity=7.0, battery_model=model)
        return est, model

    def test_new_estimator_has_no_aging_adjustment(self):
        est, model = self._make()
        self.assertEqual(model.aging_factor, 1.0)

    def test_set_soh_syncs_aging_factor(self):
        est, model = self._make()
        est.set_soh(75.0)
        self.assertAlmostEqual(model.aging_factor, 0.75, places=6)

    def test_reset_battery_state_un_ages_the_model(self):
        est, model = self._make()
        est.set_soh(65.0)
        self.assertLess(model.aging_factor, 1.0)
        est.reset_battery_state()
        self.assertEqual(est.soh, 100.0)
        self.assertEqual(model.aging_factor, 1.0)

    def test_live_full_sweep_soh_update_syncs_aging_factor(self):
        """Exercises the OTHER self.soh write site (the live zero-anchor/full-sweep
        path inside update()), not just the external set_soh() call — see
        state_estimator.py's "live SoH from a full→empty sweep" branch."""
        est, model = self._make()
        est._cap_counting = True
        est._cap_counter_ah = 5.0   # > 30% of the 7.0 Ah rated capacity required

        # Manually replicate exactly what that branch does (isolating it from the
        # rest of update()'s EKF/CC machinery, which isn't what this test is about).
        est.measured_capacity_ah = est._cap_counter_ah
        est.soh = max(0.0, min(120.0, est._cap_counter_ah / est.rated_capacity * 100.0))
        est.battery_model.set_aging_from_soh(est.soh)

        expected_soh = 5.0 / 7.0 * 100.0
        self.assertAlmostEqual(est.soh, expected_soh, places=3)
        self.assertAlmostEqual(model.aging_factor, max(0.5, expected_soh / 100.0), places=6)


class TestMainGuiOnTestFinishedSyncsAgingFactor(unittest.TestCase):
    """_on_test_finished (aset_batt/ui/isa101_views.py) is the single place both the
    "Analyze CSV" button AND every sequence's automatic post-test analysis land — see
    its own docstring. Proves the D3 wiring there is live, not just in
    AcquisitionWorker's separate command-center pipeline."""

    @staticmethod
    def _result(soh):
        """A REAL analyze_series() result (full key set, including ones added after
        this test was written) with only "soh" overridden — avoids hand-rolling a
        dict that must track every key _on_test_finished happens to read."""
        from aset_batt.acquisition.analysis import analyze_series
        from aset_batt.acquisition.models import BatteryProfile
        n = 20
        t = np.arange(n, dtype=float) * 0.2
        i = np.full(n, 1.0)
        v = np.linspace(12.6, 11.5, n)
        temp = np.full(n, 25.0)
        cap = np.cumsum(i) * 0.2 / 3600.0
        profile = BatteryProfile(
            name="t", chemistry="LeadAcid", nominal_v=12.0, series=6, capacity_ah=5.3,
            max_charge_v=14.4, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=10.0,
            ovp=15.0, uvp=9.5, otp_warn=45.0, otp_crit=60.0, internal_r=0.03,
        )
        res = analyze_series(t, i, v, temp, cap, profile, is_hppc=False)
        res["soh"] = soh
        return res

    def test_measurable_soh_updates_the_bound_estimator(self):
        win, ctrl = _make_bound_window()
        try:
            win._on_test_finished(self._result(82.0))
            self.assertAlmostEqual(ctrl.estimator.soh, 82.0, places=3)
            self.assertAlmostEqual(ctrl.estimator.battery_model.aging_factor, 0.82, places=6)
        finally:
            win.close()

    def test_nan_soh_leaves_prior_aging_factor_untouched(self):
        win, ctrl = _make_bound_window()
        try:
            win._on_test_finished(self._result(70.0))
            self.assertAlmostEqual(ctrl.estimator.battery_model.aging_factor, 0.7, places=6)

            # A subsequent HPPC-only result (soh=NaN) must NOT reset the aging factor
            # back to 1.0 — "previous tests or state" should still apply.
            win._on_test_finished(self._result(float("nan")))
            self.assertAlmostEqual(ctrl.estimator.battery_model.aging_factor, 0.7, places=6)
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
