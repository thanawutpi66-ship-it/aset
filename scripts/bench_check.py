"""
bench_check.py — ตรวจ "ความถูกต้องบนเครื่องจริง" ก่อนเชื่อผลการวัด/คัดเกรด

รันบน **เครื่องเทสที่ต่อ GW Instek PSW (PSU) + PEL-3111 (load) จริง** เท่านั้น
(ไม่เกี่ยวกับ unit tests / simulation — สคริปต์นี้คุยกับฮาร์ดแวร์ตรงผ่าน PyVISA)

ตอบ checklist ที่ค้างจากการวิเคราะห์:
  1) PSU ตอน OUTPUT OFF อ่าน MEAS:VOLT? ได้แรงดันแบตจริง หรือ 0?  ← ความเสี่ยงอันดับ 1
  2) Load ตอน INPUT OFF อ่าน MEAS:VOLT? ได้แรงดันแบตไหม (โค้ดใหม่อ่าน V จาก load ตอน discharge)
  3) Load step คมพอไหม — กระแสขึ้นถึง 95% ภายในกี่ sample (กระทบความแม่นของ DCIR)
  4) DCIR ก้อนเดิมซ้ำ N รอบ → mean ± std (ความ repeatable จริงที่ ~5 Hz)

ความปลอดภัย: ใช้กระแสน้อย, ถามยืนยันก่อนจ่ายโหลด, และ finally ปิด output/input + ปลดโหลดเสมอ

การใช้งาน (ต่อแบต 12V ที่ขั้วร่วม PSU+Load แล้ว):
    python scripts/bench_check.py                 # อ่าน ports จาก config.json
    python scripts/bench_check.py --load-current 1.5 --repeats 5
    python scripts/bench_check.py --psu MOCK::PSU::INSTR --load MOCK::LOAD::INSTR   # override
    python scripts/bench_check.py --yes           # ข้ามการถามยืนยัน (ใช้เมื่อมั่นใจ setup)
"""
import argparse
import json
import os
import statistics
import sys
import time

try:
    import pyvisa
except Exception:
    print("ต้องติดตั้ง pyvisa ก่อน:  pip install pyvisa")
    sys.exit(1)


def _load_ports():
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config.json")
    psu = load = None
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            hw = json.load(f).get("hardware", {})
        psu, load = hw.get("psu_port"), hw.get("load_port")
    return psu, load


def _open(rm, addr):
    inst = rm.open_resource(addr)
    # ตั้งค่าเดียวกับ HardwareController.connect_instruments (ปรับตามรุ่นถ้าจำเป็น)
    try:
        inst.baud_rate = 9600
        inst.read_termination = "\n"
        inst.write_termination = "\n"
    except Exception:
        pass
    inst.timeout = 5000
    return inst


def _q(inst, cmd):
    """query ที่ทน error: คืน string (strip) หรือ '<ERR>'"""
    try:
        return inst.query(cmd).strip()
    except Exception as e:
        return f"<ERR {e}>"


def _qf(inst, cmd):
    try:
        return float(inst.query(cmd).strip())
    except Exception:
        return float("nan")


def check1_psu_voltage_when_off(psu, load):
    print("\n[1] PSU อ่านแรงดันตอน OUTPUT OFF ได้ค่าจริงหรือ 0?")
    psu.write(":OUTP OFF"); load.write(":INP OFF"); time.sleep(0.3)
    v = _qf(psu, "MEAS:VOLT?")
    print(f"    PSU MEAS:VOLT? (output OFF) = {v:.4f} V")
    if v != v:        # NaN
        print("    ⚠ อ่านไม่ได้ (timeout/คำสั่งผิด) — เช็คสาย/คำสั่ง SCPI ของรุ่นนี้")
    elif abs(v) < 0.5:
        print("    ❌ ได้ ~0V → PSU ไม่ sense แรงดันแบตตอน OFF → ต้องอ่าน V จาก LOAD (โค้ดใหม่ทำแล้ว)")
    else:
        print("    ✅ ได้แรงดัน ~แบต → PSU sense ตอน OFF ได้ (read_vi เดิมใช้ได้)")
    return v


def check2_load_voltage_when_off(psu, load):
    print("\n[2] LOAD อ่านแรงดันตอน INPUT OFF ได้ไหม (โค้ดใหม่ใช้ค่านี้ตอน discharge)")
    load.write(":INP OFF"); time.sleep(0.3)
    v = _qf(load, "MEAS:VOLT?")
    print(f"    LOAD MEAS:VOLT? (input OFF) = {v:.4f} V")
    if v != v:
        print("    ⚠ อ่านไม่ได้ — เช็คว่า PEL-3111 รองรับ MEAS:VOLT? และคำสั่งถูกต้อง")
    elif abs(v) < 0.5:
        print("    ⚠ ได้ ~0V ตอน INP OFF — load อาจอ่าน V เฉพาะตอน INP ON (ยังโอเค: discharge เปิด load อยู่)")
    else:
        print("    ✅ load อ่านแรงดันแบตได้แม้ INP OFF → เป็นแหล่ง V ที่เชื่อถือได้")
    return v


def check3_step_sharpness(psu, load, i_target):
    print(f"\n[3] Load step คมพอไหม — สั่ง {i_target:.2f} A แล้วดูกระแสขึ้นถึง 95% กี่ sample")
    psu.write(":OUTP OFF")
    load.write(":MODE CC"); load.write(f":CURR {i_target}")
    samples = []
    load.write(":INP ON")
    t0 = time.perf_counter()
    for _ in range(20):                    # ~20 readback ติดกันให้เร็วที่สุด
        i = _qf(load, "MEAS:CURR?")
        samples.append((time.perf_counter() - t0, i))
        if time.perf_counter() - t0 > 3.0:
            break
    load.write(":INP OFF")
    n_to_95 = next((k for k, (_, i) in enumerate(samples)
                    if i >= 0.95 * i_target), None)
    dt = (samples[-1][0] / max(1, len(samples) - 1)) if len(samples) > 1 else float("nan")
    print(f"    เก็บได้ {len(samples)} sample, เฉลี่ย ~{dt*1000:.0f} ms/sample (= {1/dt:.1f} Hz)" if dt == dt else "    ")
    if n_to_95 is None:
        print(f"    ⚠ กระแสไม่ถึง 95% ใน {len(samples)} sample — เช็ค slew/การต่อสาย")
    else:
        print(f"    กระแสถึง 95% ที่ sample ที่ {n_to_95} (~{samples[n_to_95][0]*1000:.0f} ms)")
        if n_to_95 <= 1:
            print("    ✅ คม (≤1 sample) → step detection / DCIR เชื่อถือได้")
        else:
            print("    ⚠ ramp หลาย sample → ตั้ง load current slew ให้เร็วขึ้น ไม่งั้น DCIR ต่ำกว่าจริง")
    return samples


def check4_dcir_repeatability(psu, load, i_target, repeats):
    print(f"\n[4] DCIR ซ้ำ {repeats} รอบ ที่ {i_target:.2f} A (อ่าน V จาก LOAD)")
    psu.write(":OUTP OFF"); load.write(":MODE CC"); load.write(f":CURR {i_target}")
    vals = []
    for k in range(repeats):
        load.write(":INP OFF"); time.sleep(2.0)              # พักให้คืนแรงดัน
        v_oc = _qf(load, "MEAS:VOLT?")
        if v_oc != v_oc or abs(v_oc) < 0.5:                   # load อ่าน V ตอน off ไม่ได้ → ใช้ PSU
            v_oc = _qf(psu, "MEAS:VOLT?")
        load.write(":INP ON"); time.sleep(0.25)               # ~250 ms readback point
        v_ld = _qf(load, "MEAS:VOLT?"); i_ld = _qf(load, "MEAS:CURR?")
        load.write(":INP OFF")
        if i_ld and i_ld == i_ld and abs(i_ld) > 0.05:
            dcir = abs((v_oc - v_ld) / i_ld) * 1000.0
            vals.append(dcir)
            print(f"    รอบ {k+1}: OCV={v_oc:.3f} V  Vload={v_ld:.3f} V  I={i_ld:.3f} A  → DCIR={dcir:.1f} mΩ")
        else:
            print(f"    รอบ {k+1}: อ่านกระแสไม่ได้ ({i_ld}) — ข้าม")
    if len(vals) >= 2:
        m, s = statistics.mean(vals), statistics.pstdev(vals)
        print(f"    → DCIR = {m:.1f} ± {s:.1f} mΩ  (CoV {100*s/m:.1f}%)")
        print("    ✅ repeatable" if s/m < 0.1 else "    ⚠ กระจายสูง — เช็คการสัมผัสขั้ว/slew")
    return vals


def main():
    ap = argparse.ArgumentParser(description="Bench validation for the GW Instek rig")
    ap.add_argument("--psu", help="VISA address ของ PSU (ค่าเริ่มต้นจาก config.json)")
    ap.add_argument("--load", help="VISA address ของ DC load (ค่าเริ่มต้นจาก config.json)")
    ap.add_argument("--load-current", type=float, default=1.5, help="กระแสทดสอบ (A) — เริ่มน้อยๆ")
    ap.add_argument("--repeats", type=int, default=5, help="จำนวนรอบวัด DCIR")
    ap.add_argument("--yes", action="store_true", help="ข้ามการถามยืนยันก่อนจ่ายโหลด")
    args = ap.parse_args()

    psu_addr, load_addr = args.psu, args.load
    if not psu_addr or not load_addr:
        c_psu, c_load = _load_ports()
        psu_addr = psu_addr or c_psu
        load_addr = load_addr or c_load
    if not psu_addr or not load_addr:
        print("ไม่พบ VISA address — ระบุ --psu/--load หรือใส่ใน config.json")
        sys.exit(1)
    print(f"PSU = {psu_addr}\nLOAD = {load_addr}")

    rm = pyvisa.ResourceManager()
    psu = _open(rm, psu_addr)
    load = _open(rm, load_addr)
    print(f"PSU  *IDN? : {_q(psu, '*IDN?')}")
    print(f"LOAD *IDN? : {_q(load, '*IDN?')}")

    try:
        check1_psu_voltage_when_off(psu, load)
        check2_load_voltage_when_off(psu, load)
        if not args.yes:
            ans = input(f"\nต่อแบต 12V พร้อมหรือยัง? จะจ่ายโหลด {args.load_current} A — พิมพ์ 'y' เพื่อทำต่อ: ")
            if ans.strip().lower() != "y":
                print("ข้ามขั้นที่จ่ายโหลด (1–2 ทำเสร็จแล้ว)")
                return
        check3_step_sharpness(psu, load, args.load_current)
        check4_dcir_repeatability(psu, load, args.load_current, args.repeats)
    finally:
        # คืนสถานะปลอดภัยเสมอ
        for cmd, inst in ((":INP OFF", load), (":OUTP OFF", psu)):
            try:
                inst.write(cmd)
            except Exception:
                pass
        print("\nปิด output/load เรียบร้อย — จบการตรวจ")


if __name__ == "__main__":
    main()
