import pytest
from unittest.mock import patch, MagicMock
from aset_batt.hardware.pel_batt_test import PelBattTest, integrate_capacity, soh_from_capacity, DischargeResult

def test_integrate_capacity():
    t = [0.0, 1.0, 2.0]
    v = [12.0, 12.0, 12.0]
    i = [10.0, 10.0, 10.0]
    cap, wh = integrate_capacity(t, v, i)
    assert cap > 0
    assert wh > 0
    
    cap0, wh0 = integrate_capacity([], [], [])
    assert cap0 == 0.0

def test_soh_from_capacity():
    soh = soh_from_capacity(50.0, 100.0)
    assert soh == 50.0
    
    import math
    soh0 = soh_from_capacity(0.0, 0.0)
    assert math.isnan(soh0)

def test_pel_batt_test():
    with patch('aset_batt.hardware.pel_batt_test.time.sleep') as mock_sleep, \
         patch('aset_batt.hardware.pel_batt_test.time.time') as mock_time:
         
        mock_time.side_effect = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
         
        driver = MagicMock()
        pbt = PelBattTest(driver, rated_capacity_ah=100.0)
        
        # Mock _read_vi
        driver.query.side_effect = ["12.0", "10.0"]
        v, i = pbt._read_vi()
        assert v == 12.0
        
        # Test safe_off
        pbt.safe_off()
        driver.write.assert_called()
        
        # Test run_pc_discharge
        driver.query.side_effect = ["12.0", "10.0", "12.0", "10.0", "10.0", "10.0", "9.0", "10.0"]
        res = pbt.run_pc_discharge(10.0, 10.5)
        assert isinstance(res, DischargeResult)
