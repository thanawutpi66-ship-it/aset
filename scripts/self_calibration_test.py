"""
self_calibration_test.py — validate the whole R0/DCIR measurement chain against
a precisely known resistance, instead of a real (uncertain) battery.

The PSW 80-40.5 can emulate a fixed internal source resistance on its CV output
(V = V_set - I*ohms) — see HardwareController.set_psu_resistance_emulation().
Dialing in a known ohms value turns the PSU into a "battery" with an exactly
known R0 and (since it's a pure resistor, not a real cell) essentially zero
RC polarization — R1/C1 from the ECM fit should come out ~0 and R0/DCIR should
land on the dialed-in value. This exercises the SAME analyze_csv()/
identify_ecm_fit()/identify_dcir() pipeline used on real battery data, so a
mismatch here means the measurement chain itself (harness correction, sense
wiring, the fit) has a real problem — independent of any specific battery.

Hardware-only (needs the real PSU + Load + the rig's actual wiring, per
docs/rig_investigation_findings.md / pel3111_psw_hardware_reference.md).

Usage:
    python scripts/self_calibration_test.py --ohms 0.050
    python scripts/self_calibration_test.py --ohms 0.100 --voltage 12.6 --pulse-current 2.0
    python scripts/self_calibration_test.py --psu MOCK::PSU::INSTR --load MOCK::LOAD::INSTR
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aset_batt.hardware.hardware_driver import HardwareController
from aset_batt.storage.data_utils import DataHandler
from aset_batt.acquisition.analysis import analyze_csv, profile_from_config
from aset_batt.core.config import ConfigManager


def _config_ports():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    psu_port = load_port = None
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        psu_port = c.get("hardware", {}).get("psu_port")
        load_port = c.get("hardware", {}).get("load_port")
    return psu_port, load_port


def main():
    ap = argparse.ArgumentParser(
        description="Self-calibration: emulate a known resistance on the PSW and "
                    "check the analysis pipeline measures it back correctly.")
    ap.add_argument("--psu", help="VISA address ของ PSU (default จาก config.json)")
    ap.add_argument("--load", help="VISA address ของ e-load (default จาก config.json)")
    ap.add_argument("--ohms", type=float, default=0.050,
                    help="ความต้านทานที่จะจำลอง (Ohm, 0-1.975 สำหรับ PSW 80-40.5) — ค่า reference ที่ใช้เทียบ")
    ap.add_argument("--voltage", type=float, default=12.6,
                    help="PSU CV setpoint (V) — จำลองเป็น OCV ของ 'แบต' นี้")
    ap.add_argument("--pulse-current", type=float, default=1.0, help="กระแส pulse (A)")
    ap.add_argument("--pulse-duration", type=float, default=30.0, help="ความยาว pulse (วิ)")
    ap.add_argument("--sample-period", type=float, default=0.2,
                    help="คาบสุ่มตัวอย่าง (วิ) — ค่า default 0.2s (~5Hz) ตรงกับที่ identify_ecm_fit ออกแบบไว้")
    ap.add_argument("--csv-out", default=None, help="path เก็บ CSV (default: sessions/selfcal_TIMESTAMP.csv)")
    args = ap.parse_args()

    cfg_psu, cfg_load = _config_ports()
    psu_port = args.psu or cfg_psu
    load_port = args.load or cfg_load
    if not psu_port or not load_port:
        print("ไม่พบ VISA address ของ PSU/Load — ใส่ --psu/--load หรือตั้งใน config.json")
        sys.exit(1)
    if not (0.0 <= args.ohms <= 1.975):
        print(f"--ohms {args.ohms} เกินช่วงของ PSW 80-40.5 (0.000-1.975Ohm)")
        sys.exit(1)

    hw = HardwareController()
    print(f"Connecting PSU={psu_port}  Load={load_port} ...")
    hw.connect_instruments(psu_port, load_port)
    print("PSU/Load connected OK")

    data = DataHandler()
    csv_path = args.csv_out or os.path.join(
        "sessions", f"selfcal_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)

    try:
        err = hw.set_psu_resistance_emulation(args.ohms)
        if err:
            print(f"[WARN] SCPI error setting resistance emulation: {err}")
        hw.set_psu(True, str(args.voltage), str(args.pulse_current + 0.5))
        print(f"PSU CV={args.voltage}V, R_emulated={args.ohms}Ohm, output ON — settling 3s...")
        time.sleep(3.0)

        ok, msg = data.start_logging(csv_path)
        if not ok:
            print(f"CSV logging failed: {msg}"); sys.exit(1)

        # ── Rest sample (a few seconds at zero load current) ──────────────
        t0 = time.perf_counter()
        for _ in range(5):
            v, i_psu = hw.read_measurements(prefer_load_v=False)
            now = time.perf_counter() - t0
            data.log_row(now, v, -i_psu, 50.0, args.ohms * 1000.0, hw.current_temp)
            time.sleep(args.sample_period)

        # ── Pulse leg — the Load pulls a known current from the emulated source ──
        print(f"Pulsing {args.pulse_current}A for {args.pulse_duration}s "
              f"(sampling every {args.sample_period}s)...")
        hw.set_load(True, str(args.pulse_current))
        t_pulse_end = time.perf_counter() + args.pulse_duration
        while time.perf_counter() < t_pulse_end:
            v, i_load = hw.read_measurements(prefer_load_v=True)
            now = time.perf_counter() - t0
            data.log_row(now, v, i_load, 50.0, args.ohms * 1000.0, hw.current_temp)
            time.sleep(args.sample_period)
        hw.load_off()
        print("Pulse done.")

    except KeyboardInterrupt:
        print("\nหยุดโดยผู้ใช้")
    finally:
        data.stop_logging()
        # Always leave the PSU in a normal, safe state — a stale non-zero
        # resistance emulation left on for a REAL battery test afterwards would
        # silently corrupt every CV/OCV reading.
        try:
            hw.load_off()
        except Exception:
            pass
        try:
            hw.psu_off()
        except Exception:
            pass
        try:
            hw.set_psu_resistance_emulation(0.000)
        except Exception:
            pass

    print(f"\nCSV: {csv_path}")
    cfg = ConfigManager()
    profile = profile_from_config(cfg)
    try:
        res = analyze_csv(csv_path, profile, force_hppc=True)
    except Exception as e:
        print(f"Analysis failed: {e}")
        sys.exit(1)

    r0_mohm = res.get("r0_mohm", res.get("ri_mohm", float("nan")))
    dcir_mohm = res.get("dcir_mohm", float("nan"))
    known_mohm = args.ohms * 1000.0
    print("\n=== ผลเทียบกับค่าอ้างอิงที่รู้แน่ชัด ===")
    print(f"  ความต้านทานที่ตั้งจริง (known)  : {known_mohm:.2f} mOhm")
    print(f"  DCIR (multi-step)              : {dcir_mohm:.2f} mOhm  "
          f"({100*(dcir_mohm-known_mohm)/known_mohm:+.1f}% จากค่าจริง)" if known_mohm else "")
    print(f"  ECM R0 (t=0 extrapolation)     : {r0_mohm:.2f} mOhm  "
          f"({100*(r0_mohm-known_mohm)/known_mohm:+.1f}% จากค่าจริง)" if known_mohm else "")
    r1_mohm = res.get("r1_mohm")
    if r1_mohm is not None:
        print(f"  ECM R1 (ควรใกล้ 0 — ตัวต้านทานล้วนไม่มี RC dynamics): {r1_mohm:.2f} mOhm")
    print(f"  ECM R^2 fit quality             : {res.get('ecm_r2', float('nan')):.4f}")
    for w in res.get("quality_warnings", []):
        print(f"  [WARN] {w}")


if __name__ == "__main__":
    main()
