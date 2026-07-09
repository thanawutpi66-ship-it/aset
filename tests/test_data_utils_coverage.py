import pytest
import os
import tempfile
import time
from unittest.mock import patch, MagicMock
from aset_batt.storage.data_utils import DataHandler, write_session_metadata

@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d



def test_data_handler_start_stop_logging(temp_dir):
    handler = DataHandler()
    
    filepath = os.path.join(temp_dir, "test.csv")
    ok, msg = handler.start_logging(filepath)
    assert ok is True
    assert handler.is_recording is True
    
    handler.log_row(time.time(), 12.0, 5.0, 25.0, "STAGE", "NOTE", 0.01)
    
    handler.stop_logging()
    assert handler.is_recording is False
    assert os.path.exists(filepath)

def test_read_write_metadata(temp_dir):
    filepath = os.path.join(temp_dir, "test_meta.csv")
    
    class DummyBattery:
        product_name = "TEST_PRODUCT"
        battery_type = "LFP"
        rated_capacity = 100.0
        cells_series = 1
        cells_parallel = 1
        harness_resistance_ohm = 0.01

    class DummySystem:
        operator_name = "test_operator"

    class DummyConfig:
        def __init__(self):
            self.battery = DummyBattery()
            self.system = DummySystem()
            
    write_session_metadata(filepath, DummyConfig())
    
    expected_json = filepath + ".meta.json"
    assert os.path.exists(expected_json)
