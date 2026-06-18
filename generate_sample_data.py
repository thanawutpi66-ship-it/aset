"""
generate_sample_data.py — สร้างข้อมูล discharge จำลองที่สมจริงลง battery_data.csv

ใช้ BatteryModel ของโปรเจกต์เองในการคำนวณ OCV / internal resistance
เพื่อให้ค่าบนแดชบอร์ดดู "ครบถ้วนเหมาะสม" (V, I, SoC, R, T) สำหรับเดโม/แชร์ให้เพื่อนดู

การใช้งาน:
    python generate_sample_data.py
    python generate_sample_data.py --current 12 --dt 20 --out battery_data.csv
"""
import argparse
import csv
import math
import random
from datetime import datetime, timedelta

from battery_model import BatteryModel
from config import config_manager

HEADER = ["Timestamp", "Elapsed_s", "Voltage_V", "Current_A",
          "SoC_pct", "Resistance_mOhm", "Temperature_C"]


def generate(out_path: str, current_a: float, dt_s: float,
             ambient_c: float, seed: int) -> int:
    rng = random.Random(seed)
    bat = config_manager.battery
    model = BatteryModel(battery_type=bat.battery_type,
                         nominal_voltage=bat.nominal_voltage,
                         series_cells=bat.cells_series,
                         parallel_cells=bat.cells_parallel)
    capacity_ah = bat.rated_capacity          # pack total (Ah)
    v_cutoff = bat.pack_min_voltage           # pack-level cutoff (8S → 20V)

    soc = 100.0          # %
    elapsed = 0.0        # s
    temp = ambient_c     # °C
    start = datetime.now()

    rows = []
    max_seconds = 8 * 3600
    while soc > 1.0 and elapsed <= max_seconds:
        # OCV และ internal resistance จากโมเดลจริง
        ocv = model.get_ocv_from_soc(soc, temp)
        rin = model.estimate_rin(voltage=ocv, current=current_a,
                                 soc=soc, temp=temp)          # Ohm
        # แรงดันขั้ว = OCV - I*Rin (+ noise วัดเล็กน้อย)
        voltage = ocv - current_a * rin + rng.uniform(-0.004, 0.004)

        # อุณหภูมิ: ค่อยๆ สูงขึ้นระหว่าง discharge + ผันผวนเล็กน้อย
        temp = ambient_c + 11.0 * (1.0 - soc / 100.0) + rng.uniform(-0.2, 0.2)

        rows.append([
            (start + timedelta(seconds=elapsed)).strftime("%H:%M:%S"),
            f"{elapsed:.1f}",
            f"{voltage:.4f}",
            f"{current_a:.4f}",
            f"{soc:.2f}",
            f"{rin * 1000.0:.2f}",
            f"{temp:.2f}",
        ])

        if voltage <= v_cutoff:
            break

        # coulomb counting: ลด SoC ตามประจุที่จ่ายออก
        removed_ah = current_a * (dt_s / 3600.0)
        soc -= removed_ah / capacity_ah * 100.0
        elapsed += dt_s

    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)

    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description="สร้าง discharge data จำลองลง CSV")
    p.add_argument("--out", default="battery_data.csv", help="ไฟล์ปลายทาง")
    p.add_argument("--current", type=float, default=10.0, help="กระแส discharge (A)")
    p.add_argument("--dt", type=float, default=20.0, help="ช่วงเวลาเก็บข้อมูล (s)")
    p.add_argument("--ambient", type=float, default=25.0, help="อุณหภูมิเริ่มต้น (°C)")
    p.add_argument("--seed", type=int, default=7, help="random seed")
    args = p.parse_args()

    n = generate(args.out, args.current, args.dt, args.ambient, args.seed)
    print(f"เขียน {n} แถวลง {args.out} "
          f"(I={args.current}A, dt={args.dt}s, ambient={args.ambient}C)")


if __name__ == "__main__":
    main()
