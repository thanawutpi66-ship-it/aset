import unittest
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


class TestBatteryModel(unittest.TestCase):
    def setUp(self):
        # default = single cell (per-cell domain)
        self.model = BatteryModel(battery_type="LiFePO4")

    def test_ocv_lookup(self):
        # rested OCV ต่อเซลล์ (ตารางใหม่: plateau ~3.28V, 100% ~3.40V)
        self.assertAlmostEqual(self.model.get_ocv_from_soc(0), 2.50, places=2)
        self.assertAlmostEqual(self.model.get_ocv_from_soc(50), 3.28, places=2)
        self.assertAlmostEqual(self.model.get_ocv_from_soc(100), 3.40, places=2)

    def test_ocv_interpolation(self):
        ocv_at_25 = self.model.get_ocv_from_soc(25)
        self.assertTrue(2.50 < ocv_at_25 < 3.30)

    def test_reverse_lookup(self):
        ocv = self.model.get_ocv_from_soc(50)
        soc_back = self.model.get_soc_from_ocv(ocv)
        self.assertAlmostEqual(soc_back, 50.0, delta=2.0)

    def test_rin_increases_at_low_temperature(self):
        # Arrhenius: ความต้านทานต้องสูงขึ้นเมื่อเย็นลง (กันบั๊ก temp_coeff กลับเครื่องหมาย)
        r_cold = self.model._calculate_base_rin(50.0, -10.0)
        r_warm = self.model._calculate_base_rin(50.0, 40.0)
        self.assertGreater(r_cold, r_warm)


class TestPackScaling(unittest.TestCase):
    """8S pack: แรงดัน/ความต้านทานต้องคูณตามจำนวน series"""

    def setUp(self):
        self.cell = BatteryModel(battery_type="LiFePO4")
        self.pack = BatteryModel(battery_type="LiFePO4", series_cells=8, parallel_cells=1)

    def test_ocv_scales_with_series(self):
        self.assertAlmostEqual(
            self.pack.get_ocv_from_soc(50),
            8 * self.cell.get_ocv_from_soc(50),
            places=3,
        )

    def test_pack_ocv_in_expected_range(self):
        # 8S LFP เต็ม ~ 8 × 3.40 = 27.2V
        self.assertAlmostEqual(self.pack.get_ocv_from_soc(100), 27.2, delta=0.3)

    def test_soc_from_pack_voltage_roundtrip(self):
        v_pack = self.pack.get_ocv_from_soc(50)
        self.assertAlmostEqual(self.pack.get_soc_from_ocv(v_pack), 50.0, delta=2.0)

    def test_resistance_scales_with_series(self):
        r_pack = self.pack._calculate_base_rin(50.0, 25.0)
        r_cell = self.cell._calculate_base_rin(50.0, 25.0)
        self.assertAlmostEqual(r_pack, 8 * r_cell, places=4)


class TestStateEstimator(unittest.TestCase):
    def setUp(self):
        self.estimator = StateEstimator(
            rated_capacity=50.0, battery_model=BatteryModel(battery_type="LiFePO4")
        )

    def test_initialization(self):
        # 50% SoC ต่อเซลล์ ≈ 3.28V (ตารางใหม่)
        self.estimator.init_from_voltage(3.28)
        self.assertAlmostEqual(self.estimator.soc, 50.0, delta=3.0)

    def test_coulomb_counting(self):
        self.estimator.set_initial_soc(50.0)
        # Discharge 1A for 1 hour (current > 0 = discharge)
        result = self.estimator.update(3.0, 1.0, 3600)
        self.assertTrue(
            48.0 < result["soc"] <= 50.1,
            f"SoC {result['soc']} not in expected range",
        )


if __name__ == "__main__":
    unittest.main()
