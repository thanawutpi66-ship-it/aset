"""
make_training_data.py — สร้าง labeled dataset สังเคราะห์สำหรับเทรน BatteryGrader

จำลอง "เซลล์" หลายตัวที่มี SoH และ R0 (aging) ต่างกัน แต่ละตัวเป็น pulse-discharge
(สลับ rest/load → เกิด current step ให้ analyzer ฟิต 1RC ได้จริง) แล้ว label A/B/C/D
ตามกฎ (SoH + R0 ratio). เขียน CSV ต่อเซลล์ + labels.csv ให้ train_grader.py ใช้

หมายเหตุ: เป็นข้อมูล "สังเคราะห์" เพื่อ demo pipeline — ของจริงควรแทนด้วย labeled CSV
จากการวัดเซลล์จริง

  python make_training_data.py            # -> data/train/*.csv + data/train/labels.csv
  python train_grader.py data/train/labels.csv -o grader_model.joblib --rated-capacity 50
"""
import csv
import math
import os
import random

from battery_model import BatteryModel
from config import config_manager

OUT_DIR = os.path.join("data", "train")
HEADER = ["Timestamp", "Elapsed_s", "Voltage_V", "Current_A",
          "SoC_pct", "Resistance_mOhm", "Temperature_C"]


def grade_of(soh: float, r0_scale: float) -> str:
    """กฎ label (ตรงแนวกับ heuristic) — โมเดลจะเรียนรู้ขอบเขตจากหลายฟีเจอร์"""
    if soh >= 90 and r0_scale <= 1.3:
        return "A"
    if soh >= 80 and r0_scale <= 1.8:
        return "B"
    if soh >= 70 and r0_scale <= 2.5:
        return "C"
    return "D"


def simulate_cell(path, model, rated_ah, soh, r0_scale, ambient, seed):
    rng = random.Random(seed)
    eff_cap = rated_ah * soh / 100.0          # ความจุใช้งานจริง (Ah) สะท้อน SoH
    load_i = 0.2 * rated_ah                    # 0.2C
    r0 = model.base_rin * r0_scale             # pack ohmic (Ohm) สเกลตาม aging
    rp = 0.4 * r0
    tau = 5.0
    dt = 5.0

    soc = 100.0
    elapsed = 0.0
    delivered = 0.0
    rows = []
    # pattern: rest 4 samples (0A) แล้ว load 12 samples (load_i) วนไป
    while soc > 2.0 and delivered < eff_cap and elapsed < 12 * 3600:
        # rest
        for _ in range(4):
            ocv = model.get_ocv_from_soc(soc, ambient)
            v = ocv + rng.uniform(-0.005, 0.005)
            rows.append([elapsed, v, 0.0, soc, r0 * 1000.0, ambient])
            elapsed += dt
        # load (มี 1RC transient ภายใน segment → เกิด current step)
        for k in range(12):
            if soc <= 2.0 or delivered >= eff_cap:
                break
            ocv = model.get_ocv_from_soc(soc, ambient)
            t_seg = k * dt
            over = load_i * (r0 + rp * (1.0 - math.exp(-t_seg / tau)))
            v = ocv - over + rng.uniform(-0.005, 0.005)
            temp = ambient + 6.0 * (1.0 - soc / 100.0)
            rows.append([elapsed, v, load_i, soc, (r0 + rp) * 1000.0, temp])
            dah = load_i * dt / 3600.0
            delivered += dah
            soc -= dah / eff_cap * 100.0
            elapsed += dt

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow(["00:00:00", f"{r[0]:.1f}", f"{r[1]:.4f}", f"{r[2]:.4f}",
                        f"{r[3]:.2f}", f"{r[4]:.2f}", f"{r[5]:.2f}"])
    return len(rows)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    bat = config_manager.battery
    model = BatteryModel(bat.battery_type, bat.nominal_voltage,
                         bat.cells_series, bat.cells_parallel)
    rated = bat.rated_capacity

    rng = random.Random(42)
    labels = []
    n = 72
    for i in range(n):
        soh = rng.uniform(55, 100)
        r0_scale = rng.uniform(1.0, 3.0)
        ambient = rng.uniform(20, 30)
        label = grade_of(soh, r0_scale)
        name = f"cell_{i:03d}.csv"
        simulate_cell(os.path.join(OUT_DIR, name), model, rated,
                      soh, r0_scale, ambient, seed=1000 + i)
        labels.append((name, label))

    labels_path = os.path.join(OUT_DIR, "labels.csv")
    with open(labels_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["csv_path", "grade"])
        for name, label in labels:
            w.writerow([name, label])

    from collections import Counter
    dist = Counter(g for _, g in labels)
    print(f"สร้าง {n} เซลล์ -> {OUT_DIR}  (labels: {dict(sorted(dist.items()))})")
    print(f"labels file: {labels_path}")


if __name__ == "__main__":
    main()
