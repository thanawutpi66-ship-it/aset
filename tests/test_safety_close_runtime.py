from unittest.mock import MagicMock

from aset_batt.app.auto_controller import AutoController


def test_shutdown_attempts_ssr_load_and_psu_independently():
    controller = AutoController.__new__(AutoController)
    controller.hw = MagicMock()
    controller.hw.set_ssr.side_effect = OSError("ssr unavailable")
    controller.hw.load_off.side_effect = OSError("pel unavailable")
    controller.hw.psu_off.return_value = True
    controller._emergency_shutdown()
    controller.hw.set_ssr.assert_called_once_with(False)
    controller.hw.load_off.assert_called_once()
    controller.hw.psu_off.assert_called_once()


def test_shutdown_does_not_claim_physical_off_on_failure():
    controller = AutoController.__new__(AutoController)
    controller.hw = MagicMock()
    controller.hw.set_ssr.return_value = False
    controller.hw.load_off.return_value = False
    controller.hw.psu_off.return_value = False
    controller._emergency_shutdown()
    assert controller.hw.set_ssr.call_count == 1
    assert controller.hw.load_off.call_count == 1
    assert controller.hw.psu_off.call_count == 1
