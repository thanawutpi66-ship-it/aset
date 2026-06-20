"""
Tests สำหรับการแก้ความถูกต้องของ estimator/model:
- coulombic efficiency ใช้กับ charge เท่านั้น
- อุณหภูมิถูก forward เข้า estimate_rin (เดิมส่ง measured_dcir ผิดตำแหน่ง)
- plateau guard: ocv_slope ต่ำบน plateau ของ LFP
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


class TestEstimatorCorrectness(unittest.TestCase):
    def test_coulombic_efficiency_charge_only(self):
        # discharge: นับเต็ม (ah = +10.0)
        e_dis = StateEstimator(50.0, BatteryModel("LiFePO4"))
        e_dis.set_initial_soc(50.0)
        e_dis.update(3.3, 10.0, dt=3600)  # discharge 10A 1h
        self.assertAlmostEqual(e_dis.ah_accumulated, 10.0, places=3)

        # charge: ถูกหักด้วย efficiency 0.99 (ah = -9.9 ไม่ใช่ -10)
        e_chg = StateEstimator(50.0, BatteryModel("LiFePO4"))
        e_chg.set_initial_soc(50.0)
        e_chg.update(3.3, -10.0, dt=3600)  # charge 10A 1h
        self.assertAlmostEqual(e_chg.ah_accumulated, -9.9, places=3)

    def test_temperature_forwarded_to_rin(self):
        # อุณหภูมิถูกส่งเข้า estimate_rin จริง -> Rin เย็น > Rin ร้อน (Arrhenius)
        model = BatteryModel("LiFePO4", series_cells=8)
        e = StateEstimator(50.0, model)
        e.set_initial_soc(50.0)
        r_cold = e.update(25.6, 5.0, dt=1.0, temp=-10.0)["rin"]
        e.set_initial_soc(50.0)
        r_warm = e.update(25.6, 5.0, dt=1.0, temp=40.0)["rin"]
        self.assertGreater(r_cold, r_warm)

    def test_plateau_guard_slope(self):
        model = BatteryModel("LiFePO4", series_cells=8)
        e = StateEstimator(50.0, model)
        slope_plateau = model.ocv_slope(50.0)   # กลาง plateau -> flat
        slope_knee = model.ocv_slope(5.0)        # ใกล้ knee -> ชัน
        self.assertLess(slope_plateau, e.min_ocv_slope)
        self.assertGreater(slope_knee, e.min_ocv_slope)


if __name__ == "__main__":
    unittest.main()
