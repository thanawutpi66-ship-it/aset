"""
Tests สำหรับ charge_controller — state machine การชาร์จหลายเคมี
- decide() (pure): bulk→absorption→float (lead-acid), cc→cv→done (lithium)
- ChargeParams.from_config: คำนวณ setpoint ระดับแพ็คถูกต้อง (YTZ7V)
- run(): ขับ loop กับ fake hardware จนเข้า float / done
"""
import types
import unittest

import aset_batt.core.charge_controller as cc
from aset_batt.core.charge_controller import ChargeController, ChargeParams, decide
from aset_batt.core.battery_model import BatteryModel


def _cfg(series, cap):
    return types.SimpleNamespace(
        battery=types.SimpleNamespace(cells_series=series, rated_capacity=cap)
    )


class TestChargeParams(unittest.TestCase):
    def test_lead_acid_ytz7v_setpoints(self):
        m = BatteryModel("LeadAcid", 2.0, series_cells=6)
        p = ChargeParams.from_config(m.charge_profile, 6, 7.0)
        self.assertEqual(p.strategy, "three_stage")
        self.assertAlmostEqual(p.bulk_current_a, 0.7, places=3)    # 0.10C × 7Ah
        self.assertAlmostEqual(p.absorption_v, 14.4, places=2)     # 2.40 × 6S
        self.assertAlmostEqual(p.float_v, 13.65, places=2)         # 2.275 × 6S
        self.assertAlmostEqual(p.tail_current_a, 0.21, places=3)   # 0.03C × 7Ah

    def test_lithium_4s_setpoints(self):
        m = BatteryModel("LiFePO4", 3.2, series_cells=4)
        p = ChargeParams.from_config(m.charge_profile, 4, 7.0)
        self.assertEqual(p.strategy, "cc_cv")
        self.assertAlmostEqual(p.cv_v, 14.6, places=2)             # 3.65 × 4S


class TestDecideThreeStage(unittest.TestCase):
    def setUp(self):
        m = BatteryModel("LeadAcid", 2.0, series_cells=6)
        self.p = ChargeParams.from_config(m.charge_profile, 6, 7.0)

    def test_bulk_stays_while_low_voltage(self):
        d = decide(self.p, cc.BULK, v_pack=12.5, i_charge=0.7, t_in_stage=10)
        self.assertEqual(d.stage, cc.BULK)
        self.assertAlmostEqual(d.set_current, 0.7, places=3)

    def test_bulk_to_absorption_on_voltage(self):
        # v แตะ absorption - margin (14.4-0.3=14.1)
        d = decide(self.p, cc.BULK, v_pack=14.2, i_charge=0.7, t_in_stage=10)
        self.assertEqual(d.stage, cc.ABSORPTION)

    def test_absorption_to_float_on_tail_current(self):
        d = decide(self.p, cc.ABSORPTION, v_pack=14.4, i_charge=0.10, t_in_stage=60)
        self.assertEqual(d.stage, cc.FLOAT)
        self.assertAlmostEqual(d.set_voltage, 13.65, places=2)

    def test_absorption_holds_while_current_high(self):
        d = decide(self.p, cc.ABSORPTION, v_pack=14.4, i_charge=0.5, t_in_stage=60)
        self.assertEqual(d.stage, cc.ABSORPTION)

    def test_float_is_done(self):
        d = decide(self.p, cc.FLOAT, v_pack=13.65, i_charge=0.02, t_in_stage=5)
        self.assertTrue(d.done)
        self.assertTrue(d.output_on)   # lead-acid เลี้ยง float ต่อ ไม่ตัด

    def test_bulk_timeout_forces_absorption(self):
        d = decide(self.p, cc.BULK, v_pack=12.0, i_charge=0.7,
                   t_in_stage=self.p.stage_timeout_s + 1)
        self.assertEqual(d.stage, cc.ABSORPTION)


class TestDecideCCCV(unittest.TestCase):
    def setUp(self):
        m = BatteryModel("LiFePO4", 3.2, series_cells=4)
        self.p = ChargeParams.from_config(m.charge_profile, 4, 7.0)

    def test_cc_to_cv(self):
        d = decide(self.p, cc.CC, v_pack=14.5, i_charge=3.5, t_in_stage=10)
        self.assertEqual(d.stage, cc.CV)

    def test_cv_to_done_cuts_output(self):
        d = decide(self.p, cc.CV, v_pack=14.6, i_charge=0.3, t_in_stage=30)
        self.assertEqual(d.stage, cc.DONE)
        self.assertTrue(d.done)
        self.assertFalse(d.output_on)   # lithium ตัดไฟเมื่อเต็ม (ไม่ float)


class _FakeHW:
    """จำลองแบตที่ตอบสนองการชาร์จ: แรงดันไต่ขึ้นจน CV แล้วกระแส taper"""
    def __init__(self, target_v, bulk_i):
        self.is_connected = True
        self.v = target_v - 2.0
        self.i = bulk_i
        self._target = target_v
        self._bulk = bulk_i
        self.off = False

    def read_vi(self):
        return (self.v, self.i, 0.0)

    def set_psu_cccv(self, volt, curr):
        if self.v < self._target - 1e-6:
            self.v = min(self._target, self.v + 0.5)   # bulk: ไต่ขึ้น
            self.i = self._bulk
        else:
            self.i = max(0.0, self.i - 0.2)            # CV: taper

    def psu_off(self):
        self.off = True


class TestRunLoop(unittest.TestCase):
    def test_lead_acid_run_reaches_float(self):
        m = BatteryModel("LeadAcid", 2.0, series_cells=6)
        ctrl = ChargeController(_FakeHW(14.4, 0.7), _cfg(6, 7.0), m,
                                poll_interval_s=0)
        final = ctrl.run(float_hold_s=0)
        self.assertEqual(final, cc.FLOAT)

    def test_lithium_run_reaches_done_and_cuts(self):
        m = BatteryModel("LiFePO4", 3.2, series_cells=4)
        hw = _FakeHW(14.6, 3.5)
        ctrl = ChargeController(hw, _cfg(4, 7.0), m, poll_interval_s=0)
        final = ctrl.run()
        self.assertEqual(final, cc.DONE)
        self.assertTrue(hw.off)   # PSU ถูกปิดเมื่อจบ


if __name__ == "__main__":
    unittest.main()
