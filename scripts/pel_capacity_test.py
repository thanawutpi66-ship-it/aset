"""
pel_capacity_test.py — run a capacity / SoH discharge on the PEL-3111 and print results.

Drives the e-load via PyVISA (ports + rated capacity from config.json), discharges the
connected battery at a chosen current down to a stop voltage, and reports measured
capacity, energy, and SoH. Prefers the instrument-native BATT-test datalog when
available, else PC-side coulomb counting (verified SCPI only).

Hardware-only (needs the real load). Examples:
    python scripts/pel_capacity_test.py --current 1.4 --stop 10.5      # 0.2C-ish, 12V cutoff
    python scripts/pel_capacity_test.py --current 7 --stop 10.5 --native
    python scripts/pel_capacity_test.py --load MOCK::LOAD::INSTR       # override port
"""
import argparse
import json
import os
import sys

try:
    import pyvisa
except Exception:
    print("ต้องติดตั้ง pyvisa ก่อน:  pip install pyvisa")
    sys.exit(1)

from aset_batt.hardware.pel_batt_test import PelBattTest


def _config():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    load_port = None
    rated = 7.0
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        load_port = c.get("hardware", {}).get("load_port")
        rated = float(c.get("battery", {}).get("rated_capacity", rated))
    return load_port, rated


def main():
    ap = argparse.ArgumentParser(description="PEL-3111 capacity / SoH discharge test")
    ap.add_argument("--load", help="VISA address ของ e-load (ค่าเริ่มต้นจาก config.json)")
    ap.add_argument("--rated", type=float, help="ความจุที่ rate (Ah) — ค่าเริ่มต้นจาก config.json")
    ap.add_argument("--current", type=float, required=True, help="กระแส discharge (A)")
    ap.add_argument("--stop", type=float, required=True, help="แรงดันหยุด (V, ระดับแพ็ค)")
    ap.add_argument("--native", action="store_true",
                    help="ลองใช้ BATT test + Datalog ในตัวเครื่อง (ต้อง verify SCPI)")
    ap.add_argument("--max-hours", type=float, default=8.0, help="timeout (ชม.)")
    args = ap.parse_args()

    cfg_load, cfg_rated = _config()
    load_addr = args.load or cfg_load
    rated = args.rated or cfg_rated
    if not load_addr:
        print("ไม่พบ VISA address ของ load — ใส่ --load หรือ config.json")
        sys.exit(1)

    rm = pyvisa.ResourceManager()
    load = rm.open_resource(load_addr)
    try:
        load.read_termination = "\n"; load.write_termination = "\n"; load.timeout = 5000
    except Exception:
        pass
    print(f"LOAD = {load_addr}")
    try:
        print("LOAD *IDN? :", load.query("*IDN?").strip())
    except Exception as e:
        print("อ่าน *IDN? ไม่ได้:", e)

    tester = PelBattTest(load, rated_capacity_ah=rated)
    print(f"เริ่ม discharge: I={args.current} A → stop {args.stop} V  (rated {rated} Ah)\n"
          f"กด Ctrl+C เพื่อหยุด (โหลดจะถูกปิดอัตโนมัติ)\n")

    res = None
    try:
        if args.native:
            res = tester.run_native_batt_test(args.current, args.stop,
                                              max_seconds=args.max_hours * 3600)
            if res is None:
                print("native BATT test ใช้ไม่ได้ — สลับไป PC coulomb counting")
        if res is None:
            res = tester.run_pc_discharge(args.current, args.stop,
                                          max_seconds=args.max_hours * 3600)
    except KeyboardInterrupt:
        tester.safe_off()
        print("\nหยุดโดยผู้ใช้ — ปิดโหลดแล้ว")
        return

    print("\n=== ผลการวัด ===")
    print(f"  source      : {res.source}")
    print(f"  capacity    : {res.capacity_ah:.3f} Ah")
    print(f"  energy      : {res.energy_wh:.2f} Wh")
    print(f"  SoH         : {res.soh_pct:.1f} %   (เทียบ rated {rated} Ah)")
    print(f"  duration    : {res.duration_s/60:.1f} min")
    print(f"  samples     : {res.n_samples}")
    print(f"  stop reason : {res.stopped_reason}")


if __name__ == "__main__":
    main()
