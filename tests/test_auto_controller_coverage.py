import pytest
from unittest.mock import MagicMock, patch
from aset_batt.app.auto_controller import AutoController

@pytest.fixture
def mock_app_deps():
    root = MagicMock()
    hw = MagicMock()
    data = MagicMock()
    estimator = MagicMock()
    config = MagicMock()
    
    hw.is_connected = True
    config.config = {"General": {"data_dir": "test_dir"}}
    
    with patch('aset_batt.app.auto_controller.ServiceLocator') as mock_sl:
        mock_sl.get_global.return_value.get_service.return_value = MagicMock()
        controller = AutoController(root, hw, data, estimator, config)
        controller._event_system = MagicMock()
        yield controller

def test_controller_init(mock_app_deps):
    assert mock_app_deps.monitor_running is False
    assert mock_app_deps.is_profile_running is False
    assert mock_app_deps.is_charging is False
    assert mock_app_deps.safety_triggered is False

def test_emergency_shutdown(mock_app_deps):
    mock_app_deps.is_profile_running = True
    with patch.object(mock_app_deps, 'stop_profile'):
        mock_app_deps._emergency_shutdown()
    
    mock_app_deps.hw.psu_off.assert_called_once()
    mock_app_deps.hw.load_off.assert_called_once()

def test_start_stop_monitor(mock_app_deps):
    with patch('threading.Thread') as mock_thread:
        mock_app_deps.data.start_logging.return_value = (True, "")
        mock_app_deps.start_monitor()
        assert mock_app_deps.monitor_running is True
        
        mock_app_deps.monitor_running = True
        mock_app_deps.stop_monitor()
        assert mock_app_deps.monitor_running is False

def test_start_stop_profile(mock_app_deps):
    mock_app_deps.current_profile = MagicMock()
    mock_app_deps.profile_data = [MagicMock()]
    with patch('threading.Thread') as mock_thread:
        mock_app_deps.start_profile()
        assert mock_app_deps.is_profile_running is True
        
        mock_app_deps.stop_profile()
        assert mock_app_deps.is_profile_running is False

def test_ensure_logging(mock_app_deps):
    mock_app_deps.data.is_recording = False
    mock_app_deps.current_profile = MagicMock()
    mock_app_deps.current_profile.name = "TestProfile"
    
    mock_app_deps.data.start_logging.return_value = (True, "")
    mock_app_deps._ensure_logging("LABEL")
    mock_app_deps.data.start_logging.assert_called_once()
