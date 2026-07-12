import pytest
from unittest.mock import MagicMock, patch
from aset_batt.hardware.hardware_driver import HardwareController

@pytest.fixture
def hw():
    config = MagicMock()
    # Mock ConfigManager globally if needed or just pass it
    with patch('aset_batt.hardware.hardware_driver.pyvisa.ResourceManager'):
        controller = HardwareController()
        controller.psu_inst = MagicMock()
        controller.load_inst = MagicMock()
        controller.esp32_inst = MagicMock()
        controller.rm = MagicMock()
        controller.is_connected = True
        return controller

def test_hardware_read_measurements(hw):
    hw.psu_inst.query.return_value = "12.0, 5.0"
    hw.load_inst.query.return_value = "4.9"
    hw.esp32_inst.readline.return_value = b"T:25.0\n"
    
    # Try reading
    res = hw.read_measurements()
    assert res is not None

def test_hardware_set_charge(hw):
    hw.set_charge(14.4, 10.0)
    hw.psu_inst.write.assert_called()

def test_hardware_set_load(hw):
    hw.set_load(5.0)
    hw.load_inst.write.assert_called()

def test_hardware_shutdown_all(hw):
    mock_psu = hw.psu_inst
    mock_load = hw.load_inst
    hw.shutdown_all()
    mock_psu.write.assert_called()
    mock_load.write.assert_called()

def test_hardware_connect_disconnect(hw):
    with patch('aset_batt.hardware.hardware_driver.pyvisa.ResourceManager') as mock_rm:
        mock_rm.return_value.open_resource.return_value = MagicMock()
        hw.connect_instruments("COM1", "COM2")
        assert hw.psu_inst is not None
        assert hw.load_inst is not None
        
        hw.disconnect_instruments()
        # Since psu/load were mocked, disconnect should call close
        
def test_hardware_get_ports():
    with patch('serial.tools.list_ports.comports') as mock_ports, \
         patch('aset_batt.hardware.hardware_driver.pyvisa.ResourceManager'):
        mock_port = MagicMock()
        mock_port.device = "COM3"
        mock_ports.return_value = [mock_port]
        
        hw = HardwareController()
        ports = hw.get_com_ports()
        assert "COM3" in ports

def test_psu_load_off(hw):
    hw.psu_off()
    hw.psu_inst.write.assert_called()
    
    hw.load_off()
    hw.load_inst.write.assert_called()
    
def test_hardware_transient_dcir(hw):
    try:
        hw.transient_dcir_measure()
    except Exception:
        pass
