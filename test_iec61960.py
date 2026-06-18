#!/usr/bin/env python3
"""
Test script สำหรับ IEC 61960 LiPO Battery Testing Standard
"""
from iec61960_standard import IEC61960Standard, TestType
from battery_model import BatteryModel
import json

def main():
    print("🔬 IEC 61960 LiPO Battery Testing Standard Test")
    print("=" * 60)

    # สร้าง IEC 61960 standard สำหรับ LiPO 2Ah
    iec_standard = IEC61960Standard(battery_capacity_ah=2.0)

    # แสดง available tests
    print("📋 Available IEC 61960 Tests:")
    for test_id in iec_standard.get_available_tests():
        profile = iec_standard.get_test_profile(test_id)
        print(f"  • {test_id}: {profile.name}")
        print(f"    Duration: {profile.duration_hours:.1f}h, Temp: {profile.temperature}°C")
        if profile.discharge_rate:
            print(f"    C-Rate: {profile.discharge_rate.value}C")
        print()

    # ทดสอบ Battery Model กับ LiPO
    print("🔋 LiPO Battery Model Test:")
    model = BatteryModel('LiPO', nominal_voltage=3.7)

    # ทดสอบ OCV ที่อุณหภูมิต่างๆ
    print("📊 OCV vs Temperature (SoC = 50%):")
    for temp in [-10, 0, 25, 40, 60]:
        ocv = model.get_ocv_from_soc(50.0, temp)
        print(".1f")

    print()
    print("⚡ Voltage Prediction Test (SoC=80%, Current=2A):")
    for temp in [0, 25, 45]:
        voltage = model.get_voltage_from_state(80.0, 2.0, temp)
        print(".2f")

    # ทดสอบ capacity calculation
    print()
    print("📏 Capacity Calculation Test:")
    # จำลอง discharge data
    voltage_data = [4.2, 4.1, 4.0, 3.9, 3.8, 3.7, 3.6, 3.5, 3.4, 3.3, 3.2, 3.1, 3.0]
    current_data = [-2.0] * len(voltage_data)  # 2A discharge
    time_data = [i * 3600 for i in range(len(voltage_data))]  # 1 hour intervals

    capacity_results = iec_standard.calculate_capacity(voltage_data, current_data, time_data)
    print(".3f")
    print(".3f")
    print(".1f")
    print(".3f")

    # ทดสอบ energy density
    print()
    print("🔋 Energy Density Calculation:")
    energy_results = iec_standard.calculate_energy_density(capacity_results['capacity_ah'], 100.0)
    print(".1f")
    print(".1f")

    # ทดสอบ DCIR measurement
    print()
    print("Ω DCIR Measurement Test:")
    dcir_results = model.measure_iec61960_dcir(3.8, 3.75, 1.0, 25.0)
    print(".1f")
    print(".1f")

    # ทดสอบ cycle life assessment
    print()
    print("🔄 Cycle Life Assessment Test:")
    capacity_history = [2.0, 1.98, 1.95, 1.92, 1.88, 1.85, 1.82, 1.78, 1.75, 1.72]  # 10 cycles
    cycle_numbers = list(range(1, len(capacity_history) + 1))

    cycle_results = iec_standard.assess_cycle_life(capacity_history)
    print(f"  Cycles to 80% capacity: {cycle_results['cycles_to_80_percent']}")
    print(".1f")
    print(".1f")

    # สร้าง test report
    print()
    print("📄 IEC 61960 Test Report Sample:")
    sample_results = {
        "capacity_ah": 1.95,
        "energy_wh": 7.02,
        "dcir_mohm": 45.2,
        "iec61960_compliant": True
    }
    report = iec_standard.generate_test_report("capacity_02c", sample_results)
    print(report)

    print()
    print("✅ IEC 61960 LiPO Battery Testing Standard พร้อมใช้งาน!")
    print("🎯 ระบบรองรับการทดสอบตามมาตรฐานสากลสำหรับ LiPO batteries")

if __name__ == "__main__":
    main()