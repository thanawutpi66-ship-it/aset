"""
Regression tests สำหรับ current sign convention

ทั้งระบบใช้ convention: **discharge = บวก** (ให้ตรงกับ StateEstimator,
CSV/dashboard และ generate_sample_data) — กันบั๊กเก่าที่ live loop เคยใช้
psu_i - load_i (charge=บวก) ทำให้ SoC ขยับผิดทางขณะ discharge
"""
import unittest

from aset_batt.hardware.mock_hardware import MockHardwareController
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.core.battery_model import BatteryModel


class TestCurrentSignConvention(unittest.TestCase):
    def test_discharge_current_is_positive(self):
        hw = MockHardwareController()
        hw.set_load(True, 2.0)  # ดึง 2A ออก = discharge
        # Operational contract: discharge reads use prefer_load_v=True (PSU is OFF,
        # current comes from the e-load). prefer_load_v=False is the charge/idle path
        # which reads −i_psu (= 0 here) — both mock and real HAL behave this way.
        _v, i_net = hw.read_measurements(prefer_load_v=True)
        self.assertGreater(
            i_net, 0,
            "discharge ต้องให้กระแสเป็นบวก (ตรงกับ convention ของ StateEstimator)"
        )

    def test_rest_current_near_zero(self):
        hw = MockHardwareController()
        hw.set_load(False)  # ไม่มี load
        _v, i_net = hw.read_measurements()
        self.assertAlmostEqual(i_net, 0.0, delta=0.6)

    def test_discharge_decreases_soc(self):
        """discharge (กระแสบวก) ต้องทำให้ SoC ลดลง ไม่ใช่เพิ่ม"""
        est = StateEstimator(rated_capacity=50.0,
                             battery_model=BatteryModel(battery_type="LiFePO4"))
        est.set_initial_soc(80.0)
        # discharge 10A เป็นเวลา 1 ชั่วโมง (ค่าบวก = discharge)
        result = est.update(voltage=3.2, current=10.0, dt=3600, temp=25.0)
        self.assertLess(
            result["soc"], 80.0,
            "SoC ต้องลดลงเมื่อ discharge (กระแสบวก)"
        )


if __name__ == "__main__":
    unittest.main()
