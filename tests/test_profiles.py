"""
Tests สำหรับ battery_profiles registry (ฐานข้อมูลโปรไฟล์แบตเตอรี่ JSON)
- เคมีโหลดจาก registry ได้ครบ + ค่า OCV/charge ตรงตามที่ออกแบบ
- battery_type ที่ไม่รู้จัก fallback เป็น Li-ion (เหมือน else-branch เดิม)
- BatteryModel ที่ refactor แล้วยังคืน OCV เดิมเป๊ะ (กัน regression)
"""
import unittest

import battery_profiles
from battery_model import BatteryModel


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


class TestModelMatchesProfile(unittest.TestCase):
    """BatteryModel หลัง refactor ต้องคืน OCV เดิม (ค่าที่ test อื่นผูกไว้)"""

    def test_lifepo4_ocv_unchanged(self):
        m = BatteryModel("LiFePO4")
        self.assertAlmostEqual(m.get_ocv_from_soc(0), 2.50, places=2)
        self.assertAlmostEqual(m.get_ocv_from_soc(50), 3.278, places=3)
        self.assertAlmostEqual(m.get_ocv_from_soc(100), 3.40, places=2)

    def test_lead_acid_pack_ocv_unchanged(self):
        m = BatteryModel("LeadAcid", 2.0, series_cells=6)
        self.assertAlmostEqual(m.get_ocv_from_soc(100), 12.78, delta=0.1)

    def test_model_exposes_charge_profile(self):
        m = BatteryModel("LeadAcid", 2.0, series_cells=6)
        self.assertEqual(m.charge_profile.strategy, "three_stage")


if __name__ == "__main__":
    unittest.main()
