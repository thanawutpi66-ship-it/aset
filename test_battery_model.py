#!/usr/bin/env python3
"""
Test script สำหรับ Battery Model ที่มี temperature compensation
"""
from battery_model import BatteryModel
import numpy as np

def main():
    # ทดสอบ Battery Model ใหม่
    model = BatteryModel('LiFePO4')

    print('🔬 ASET Battery Model - Temperature Compensation Test')
    print('=' * 60)

    # ทดสอบ OCV ที่อุณหภูมิต่างๆ
    print('📊 OCV vs Temperature (SoC = 50%):')
    for temp in [-10, 0, 25, 40, 60]:
        ocv = model.get_ocv_from_soc(50.0, temp)
        print(f'  {temp:3d}°C: {ocv:.3f}V')

    print()
    print('🔋 Internal Resistance vs Temperature & SoC:')
    for temp in [0, 25, 50]:
        for soc in [20, 50, 80]:
            rin = model._calculate_base_rin(soc, temp)
            print(f'  SoC {soc:2d}%, {temp:2d}°C: {rin*1000:5.1f}mΩ')

    print()
    print('🌡️ Temperature Effects Analysis:')
    effects = model.get_temperature_effects(0)
    print(f'  Temperature: {effects["temperature"]}°C')
    print(f'  OCV Ratio: {effects["ocv_ratio"]:.3f}')
    print(f'  Rin Ratio: {effects["rin_ratio"]:.3f}')
    print(f'  Aging Factor: {effects["aging_factor"]:.3f}')

    print()
    print('⚡ Voltage Prediction Test (SoC=50%, Current=5A):')
    for temp in [-10, 0, 25, 40]:
        voltage = model.get_voltage_from_state(50.0, 5.0, temp)
        print(f'  {temp:3d}°C: {voltage:.3f}V')

    print()
    print('✅ Battery Model พร้อมใช้งานด้วย Temperature Compensation ขั้นสูง!')

if __name__ == "__main__":
    main()