"""
Tests สำหรับ battery_profiles registry (ฐานข้อมูลโปรไฟล์แบตเตอรี่ JSON)
- เคมีโหลดจาก registry ได้ครบ + ค่า OCV/charge ตรงตามที่ออกแบบ
- battery_type ที่ไม่รู้จัก fallback เป็น Li-ion (เหมือน else-branch เดิม)
- BatteryModel ที่ refactor แล้วยังคืน OCV เดิมเป๊ะ (กัน regression)
"""
import json
import os
import tempfile
import unittest

import aset_batt.core.battery_profiles as battery_profiles
from aset_batt.core.battery_model import BatteryModel


class TestChemistryRegistry(unittest.TestCase):
    def test_all_chemistries_present(self):
        for name in ("LiPO", "LiFePO4", "LeadAcid", "Li-ion"):
            self.assertIn(name, battery_profiles.list_chemistries())

    def test_lead_acid_three_stage_charge(self):
        c = battery_profiles.get_chemistry("LeadAcid")
        self.assertEqual(c.charge.strategy, "three_stage")
        self.assertAlmostEqual(c.charge.absorption_voltage_per_cell, 2.40, places=3)
        self.assertAlmostEqual(c.charge.float_voltage_per_cell, 2.275, places=3)

    def test_lithium_cc_cv_charge(self):
        self.assertEqual(battery_profiles.get_chemistry("LiFePO4").charge.strategy, "cc_cv")
        self.assertEqual(battery_profiles.get_chemistry("LiPO").charge.strategy, "cc_cv")

    def test_unknown_falls_back_to_liion(self):
        unknown = battery_profiles.get_chemistry("Unobtainium")
        liion = battery_profiles.get_chemistry("Li-ion")
        self.assertEqual(unknown.ocv_curve, liion.ocv_curve)

    def test_products_loaded(self):
        self.assertIn("YTZ7V (12V 7Ah VRLA)", battery_profiles.list_products())
        p = battery_profiles.get_product("YTZ7V (12V 7Ah VRLA)")
        self.assertEqual(p.chemistry, "LeadAcid")
        self.assertEqual(p.cells_series, 6)
        self.assertGreater(p.cca_a, 0)

    def test_products_carry_voltage_window(self):
        """กัน regression: สลับรุ่นต้องตั้งหน้าต่างแรงดัน+safety ให้สอดคล้องเคมีใหม่
        (ไม่งั้น pack_max/min_voltage ค้างค่ารุ่นเดิม)"""
        for name in battery_profiles.list_products():
            p = battery_profiles.get_product(name)
            self.assertGreater(p.max_voltage_per_cell, p.min_voltage_per_cell,
                               f"{name}: max ต้อง > min")
            self.assertGreater(p.safety_ovp_pack, p.safety_uvp_pack,
                               f"{name}: OVP ต้อง > UVP")
            # OVP ต้องครอบแรงดันชาร์จเต็มของแพ็ค
            pack_max = p.max_voltage_per_cell * p.cells_series
            self.assertGreaterEqual(p.safety_ovp_pack, pack_max - 1.0,
                                    f"{name}: OVP ต่ำกว่าแรงดันเต็มแพ็ค")


class TestProfileValidation(unittest.TestCase):
    """fix #6: chemistry ใน JSON ที่ ocv_curve ไม่ครบ ต้องไม่ทำให้ registry ล่ม"""

    def _load_with(self, payload):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(payload, tmp)
        tmp.close()
        orig = battery_profiles._PROFILE_FILE
        try:
            battery_profiles._PROFILE_FILE = tmp.name
            return battery_profiles._load_registry()
        finally:
            battery_profiles._PROFILE_FILE = orig
            os.unlink(tmp.name)

    def test_new_chemistry_without_ocv_is_skipped(self):
        chems, _ = self._load_with({"chemistries": {"BadChem": {"rin": {"r0": 0.01}}}})
        self.assertNotIn("BadChem", chems)   # ข้าม (ocv ว่าง)
        self.assertIn("LeadAcid", chems)      # built-in ยังอยู่

    def test_bad_override_keeps_builtin(self):
        # override LeadAcid ด้วย ocv 1 จุด → ต้องคง built-in (>=2 จุด)
        chems, _ = self._load_with(
            {"chemistries": {"LeadAcid": {"ocv_curve": [[0, 2.0]]}}})
        self.assertGreaterEqual(len(chems["LeadAcid"].ocv_curve), 2)


class TestR8RangeValidation(unittest.TestCase):
    """Industrial-grade audit R8: presence-only checks used to let an out-of-range
    r0/OCV/cells_series/rated_capacity_ah value load silently and corrupt DCIR/
    grading for every subsequent test — now range-checked, not just "key exists"."""

    # Not a TestProfileValidation subclass (that would re-run its tests too) —
    # same small helper, duplicated on purpose.
    def _load_with(self, payload):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(payload, tmp)
        tmp.close()
        orig = battery_profiles._PROFILE_FILE
        try:
            battery_profiles._PROFILE_FILE = tmp.name
            return battery_profiles._load_registry()
        finally:
            battery_profiles._PROFILE_FILE = orig
            os.unlink(tmp.name)

    def test_implausible_r0_rejected_keeps_builtin(self):
        chems, _ = self._load_with(
            {"chemistries": {"LeadAcid": {"rin": {"r0": 12.0}}}})   # 12 Ω/cell, absurd
        self.assertLess(chems["LeadAcid"].rin["r0"], 1.0)   # built-in retained

    def test_negative_r0_rejected(self):
        chems, _ = self._load_with(
            {"chemistries": {"LeadAcid": {"rin": {"r0": -0.01}}}})
        self.assertGreater(chems["LeadAcid"].rin["r0"], 0.0)

    def test_implausible_ocv_point_rejected(self):
        chems, _ = self._load_with(
            {"chemistries": {"LeadAcid": {"ocv_curve": [[0, 2.0], [100, 55.0]]}}})
        # 55 V/cell is absurd -> whole override rejected, built-in kept
        self.assertTrue(all(0.5 <= v <= 6.0 for v in chems["LeadAcid"].ocv_curve.values()))

    def test_new_chemistry_with_implausible_r0_is_skipped(self):
        chems, _ = self._load_with(
            {"chemistries": {"NewChem": {"ocv_curve": [[0, 2.0], [100, 2.2]],
                                         "rin": {"r0": 0.0}}}})
        self.assertNotIn("NewChem", chems)   # r0=0.0 fails the 0 < r0 < 1.0 check

    def test_product_with_zero_cells_series_is_skipped(self):
        _, prods = self._load_with({"products": {"BadProduct": {
            "chemistry": "LeadAcid", "nominal_voltage_per_cell": 2.0,
            "cells_series": 0, "cells_parallel": 1, "rated_capacity_ah": 5.0,
        }}})
        self.assertNotIn("BadProduct", prods)

    def test_product_with_negative_capacity_is_skipped(self):
        _, prods = self._load_with({"products": {"BadProduct": {
            "chemistry": "LeadAcid", "nominal_voltage_per_cell": 2.0,
            "cells_series": 6, "cells_parallel": 1, "rated_capacity_ah": -5.0,
        }}})
        self.assertNotIn("BadProduct", prods)

    def test_product_with_max_voltage_below_min_is_skipped(self):
        _, prods = self._load_with({"products": {"BadProduct": {
            "chemistry": "LeadAcid", "nominal_voltage_per_cell": 2.0,
            "cells_series": 6, "cells_parallel": 1, "rated_capacity_ah": 5.0,
            "max_voltage_per_cell": 1.0, "min_voltage_per_cell": 2.0,
        }}})
        self.assertNotIn("BadProduct", prods)

    def test_valid_product_still_loads(self):
        _, prods = self._load_with({"products": {"GoodProduct": {
            "chemistry": "LeadAcid", "nominal_voltage_per_cell": 2.0,
            "cells_series": 6, "cells_parallel": 1, "rated_capacity_ah": 5.3,
            "max_voltage_per_cell": 2.45, "min_voltage_per_cell": 1.75,
        }}})
        self.assertIn("GoodProduct", prods)


class TestGetMeasuredParamsValidation(unittest.TestCase):
    """R8, second independent layer: get_measured_params() re-validates on every
    read (not just at module-import time), so a bad value written later — by
    save_measured_params() or a manual edit — is still caught."""

    def _with_measured_params(self, mp: dict):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"products": {"TestProduct": {"measured_params": mp}}}, tmp)
        tmp.close()
        orig = battery_profiles._PROFILE_FILE
        try:
            battery_profiles._PROFILE_FILE = tmp.name
            return battery_profiles.get_measured_params("TestProduct")
        finally:
            battery_profiles._PROFILE_FILE = orig
            os.unlink(tmp.name)

    def test_valid_values_pass_through(self):
        mp = self._with_measured_params({"internal_r_ohm": 0.1, "r0_fraction": 0.5})
        self.assertEqual(mp["internal_r_ohm"], 0.1)

    def test_implausible_internal_r_ohm_rejected(self):
        mp = self._with_measured_params({"internal_r_ohm": 50.0, "r0_fraction": 0.5})
        self.assertEqual(mp, {})

    def test_negative_internal_r_ohm_rejected(self):
        mp = self._with_measured_params({"internal_r_ohm": -0.1})
        self.assertEqual(mp, {})

    def test_out_of_range_r0_fraction_rejected(self):
        mp = self._with_measured_params({"internal_r_ohm": 0.1, "r0_fraction": 1.5})
        self.assertEqual(mp, {})

    def test_missing_file_returns_empty(self):
        orig = battery_profiles._PROFILE_FILE
        try:
            battery_profiles._PROFILE_FILE = "does_not_exist.json"
            self.assertEqual(battery_profiles.get_measured_params("Anything"), {})
        finally:
            battery_profiles._PROFILE_FILE = orig


class TestModelMatchesProfile(unittest.TestCase):
    """BatteryModel หลัง refactor ต้องคืน OCV เดิม (ค่าที่ test อื่นผูกไว้)"""

    def test_lifepo4_ocv_unchanged(self):
        m = BatteryModel("LiFePO4")
        self.assertAlmostEqual(m.get_ocv_from_soc(0), 2.50, places=2)
        self.assertAlmostEqual(m.get_ocv_from_soc(50), 3.278, places=3)
        self.assertAlmostEqual(m.get_ocv_from_soc(100), 3.40, places=2)

    def test_lead_acid_pack_ocv_agm_reference(self):
        # AGM published reference: full-charge rested pack ~12.89V (2.148 V/cell × 6)
        m = BatteryModel("LeadAcid", 2.0, series_cells=6)
        self.assertAlmostEqual(m.get_ocv_from_soc(100), 12.89, delta=0.1)

    def test_model_exposes_charge_profile(self):
        m = BatteryModel("LeadAcid", 2.0, series_cells=6)
        self.assertEqual(m.charge_profile.strategy, "three_stage")


if __name__ == "__main__":
    unittest.main()
