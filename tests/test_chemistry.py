"""
Tests สำหรับแนวใหม่ (แบตมอเตอร์ไซค์ 12V):
- โมเดล lead-acid (OCV sloped, reverse lookup เสถียร)
- ChemistryDetector แยก lead-acid ↔ LiFePO4 (กรณีจริงของมอเตอร์ไซค์)
"""
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.analysis_module import ChemistryDetector


class TestLeadAcidModel(unittest.TestCase):
    def setUp(self):
        self.m = BatteryModel("LeadAcid", nominal_voltage=2.0, series_cells=6)

    def test_pack_ocv_range(self):
        # AGM reference: เต็ม ~12.89V, หมด ~11.63V (6S × per-cell 2.148/1.938)
        self.assertAlmostEqual(self.m.get_ocv_from_soc(100), 12.89, delta=0.1)
        self.assertTrue(11.5 < self.m.get_ocv_from_soc(0) < 12.0)

    def test_soc_roundtrip_sloped(self):
        # เส้น sloped → reverse lookup เสถียร (ต่างจาก LFP plateau)
        for soc in (20.0, 50.0, 80.0):
            v = self.m.get_ocv_from_soc(soc)
            self.assertAlmostEqual(self.m.get_soc_from_ocv(v), soc, delta=4.0)


class TestChemistryDetector(unittest.TestCase):
    def setUp(self):
        self.d = ChemistryDetector()

    def test_detect_lead_acid(self):
        m = BatteryModel("LeadAcid", 2.0, series_cells=6)
        v, s = ChemistryDetector.features_from_model(m)
        self.assertEqual(self.d.detect(v, s).chemistry, "LeadAcid")

    def test_detect_lifepo4_motorcycle_4s(self):
        m = BatteryModel("LiFePO4", 3.2, series_cells=4)
        v, s = ChemistryDetector.features_from_model(m)
        r = self.d.detect(v, s)
        self.assertEqual(r.chemistry, "LiFePO4")
        self.assertGreaterEqual(r.confidence, 0.8)  # flat plateau ยืนยัน

    def test_lead_acid_vs_lifepo4_separated_by_ocv(self):
        la = ChemistryDetector.features_from_model(BatteryModel("LeadAcid", 2.0, series_cells=6))[0]
        lfp = ChemistryDetector.features_from_model(BatteryModel("LiFePO4", 3.2, series_cells=4))[0]
        self.assertLess(la, 13.0)
        self.assertGreater(lfp, 13.1)


if __name__ == "__main__":
    unittest.main()
