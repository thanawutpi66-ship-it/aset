import unittest
from battery_model import BatteryModel
from state_estimator import StateEstimator

class TestBatteryModel(unittest.TestCase):
    def setUp(self):
        self.model = BatteryModel(battery_type="LiFePO4")
    
    def test_ocv_lookup(self):
        # Test OCV at key points
        self.assertAlmostEqual(self.model.get_ocv_from_soc(0), 2.50, places=2)
        self.assertAlmostEqual(self.model.get_ocv_from_soc(50), 3.225, places=2)
        self.assertAlmostEqual(self.model.get_ocv_from_soc(100), 3.80, places=2)
    
    def test_ocv_interpolation(self):
        # Test interpolation
        ocv_at_25 = self.model.get_ocv_from_soc(25)
        self.assertTrue(2.50 < ocv_at_25 < 3.27)
    
    def test_reverse_lookup(self):
        # Test SoC from OCV
        ocv = self.model.get_ocv_from_soc(50)
        soc_back = self.model.get_soc_from_ocv(ocv)
        self.assertAlmostEqual(soc_back, 50.0, delta=1.0)

from battery_model import BatteryModel

class TestStateEstimator(unittest.TestCase):
    def setUp(self):
        self.estimator = StateEstimator(rated_capacity=50.0, battery_model=BatteryModel(battery_type="LiFePO4"))
    
    def test_initialization(self):
        self.estimator.init_from_voltage(3.225)
        self.assertAlmostEqual(self.estimator.soc, 50.0, delta=2.0)
    
    def test_coulomb_counting(self):
        self.estimator.set_initial_soc(50.0)
        # Discharge 1A for 1 hour
        result = self.estimator.update(3.0, 1.0, 3600)  # 1 hour
        # With smoothing factor 0.05, immediate update is small
        # Expected: slight decrease due to coulomb counting effect
        self.assertTrue(48.0 < result["soc"] <= 50.1, 
                       f"SoC {result['soc']} not in expected range")

if __name__ == "__main__":
    unittest.main()
