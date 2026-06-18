"""
Test: IEC 61960 Clause 6.4 two-pulse DCIR
R = (V1 - V2) / (I2 - I1)
"""
import unittest

from iec61960_standard import IEC61960Standard


class TestDCIRTwoPulse(unittest.TestCase):
    def setUp(self):
        self.s = IEC61960Standard(50.0, "LiFePO4", 25.6)

    def test_two_pulse_formula(self):
        # (3.2-3.0)/(15-10) = 0.04 ohm = 40 mΩ
        r = self.s.calculate_dcir_two_pulse(v1=3.2, i1=10.0, v2=3.0, i2=15.0)
        self.assertTrue(r["valid_measurement"])
        self.assertAlmostEqual(r["dcir_mohm"], 40.0, places=1)
        self.assertTrue(r["iec61960_compliant"])
        # ACIR ควรน้อยกว่า DCIR
        self.assertLess(r["acir_mohm"], r["dcir_mohm"])

    def test_equal_currents_invalid(self):
        r = self.s.calculate_dcir_two_pulse(3.2, 10.0, 3.1, 10.0)
        self.assertFalse(r["valid_measurement"])


if __name__ == "__main__":
    unittest.main()
