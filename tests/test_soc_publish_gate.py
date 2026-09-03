"""Regression coverage for the SoC display/logging validity gate."""
import math
import time
from unittest.mock import MagicMock

from aset_batt.app.auto_controller import AutoController
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


def _estimator():
    return StateEstimator(5.3, BatteryModel("LeadAcid", 2.0, 6, 1))


def test_internal_50_percent_seed_is_not_publishable_until_an_anchor():
    estimator = _estimator()

    assert estimator.soc == 50.0
    assert estimator.soc_is_initialized is False

    # Running the estimator does not turn its arbitrary numerical seed into
    # evidence; only a physical OCV/endpoint anchor can do that.
    estimator.update(12.3, 0.0, dt=1.0, temp=25.0)
    assert estimator.soc_is_initialized is False

    estimator.sync_with_ocv(12.6, temp=25.0)
    assert estimator.soc_is_initialized is True

    estimator.invalidate_soc()
    assert estimator.soc_is_initialized is False


def test_unanchored_soc_is_logged_as_nan_not_the_internal_seed():
    estimator = _estimator()
    controller = AutoController.__new__(AutoController)
    controller.estimator = estimator
    controller.data = MagicMock()
    controller._start_mono = None
    controller._start_time = time.time()
    controller.hw = MagicMock(current_temp=25.0,
                              last_voltage_source="psu",
                              last_current_source="load")

    controller._log_sample(12.3, 0.0, mode="OCV")

    args = controller.data.log_row.call_args.args
    assert math.isnan(args[3])  # SoC_pct positional argument
