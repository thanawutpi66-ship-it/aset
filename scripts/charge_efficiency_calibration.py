"""
charge_efficiency_calibration.py — measure REAL charging coulombic efficiency
(η) against the model's assumed values, instead of guessing a correction.

Why this exists: real sessions (test_HPPC_20260708_152502, test_20260709_154818)
showed net charge efficiency of only 44-54% for a healthy YTZ6V lead-acid pack —
much lower than the model's SoC-banded η (0.97 <75%, 0.92 75-90%, 0.75 >90%,
see StateEstimator._coulomb_eta) would predict for a normal bulk+absorption
charge. Two explanations are equally plausible from the CSV data alone: (a) the
OCV-derived starting SoC has several points of error, or (b) the model's η
over-credits Ah delivered during a long constant-voltage absorption tail (most
of which is gassing, not stored charge). Guessing a correction either way risks
making live SoC read consistently low or high — this script measures it
directly instead.

Method: PAUSE the charge at several points, let the pack rest long enough for
surface charge to bleed off (same ΔV/Δt settle criterion the app's
calibrate_from_ocv_stable() uses), read the OCV, convert to SoC via the
chemistry's own OCV curve (ground truth, independent of any η assumption), and
record (Ah put in since the last checkpoint) alongside (ΔSoC the OCV says
actually happened). At the end, the measured η per SoC band is printed next to
the model's assumed value — a real number to update battery_profiles.py's
charge model with, not a guess.

Hardware-only (needs the real PSU + Load on the rig), per
docs/rig_investigation_findings.md.

Usage:
    python scripts/charge_efficiency_calibration.py --rated-ah 5.3 --cells 6
    python scripts/charge_efficiency_calibration.py --checkpoints 8 --rest-min 12
    python scripts/charge_efficiency_calibration.py --psu MOCK::PSU::INSTR --load MOCK::LOAD::INSTR
"""
import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aset_batt.hardware.hardware_driver import HardwareController
from aset_batt.core.battery_model import BatteryModel
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


def wait_for_settle(hw, min_rest_s, window_s, dv_thresh_v, timeout_s, on_progress=None):
    """Same ΔV/Δt settle criterion as AutoController.calibrate_from_ocv_stable():
    read every 5s until max(V)-min(V) < dv_thresh over the trailing window AND
    at least min_rest_s has elapsed. Returns (settled: bool, ocv: float)."""
    readings = []
    t0 = time.perf_counter()
    v = None
    while True:
        elapsed = time.perf_counter() - t0
        if elapsed > timeout_s:
            return False, v
        v, _ = hw.read_measurements(prefer_load_v=False)
        now = time.perf_counter()
        readings.append((now, v))
        cutoff = now - window_s
        readings[:] = [(t, val) for t, val in readings if t >= cutoff]
        in_win = [val for _, val in readings]
        dv = (max(in_win) - min(in_win)) if len(in_win) >= 2 else float("nan")
        settled = elapsed >= min_rest_s and len(in_win) >= 3 and dv == dv and dv < dv_thresh_v
        if on_progress:
            on_progress(elapsed, v, dv)
        if settled:
            return True, v
        time.sleep(5.0)


def main():
    ap = argparse.ArgumentParser(
        description="Measure real charging coulombic efficiency by pausing a charge "
                    "at several checkpoints, resting to true OCV, and comparing Ah-in "
                    "vs OCV-derived SoC change.")
    ap.add_argument("--psu", help="VISA address ของ PSU (default จาก config.json)")
    ap.add_argument("--load", help="VISA address ของ e-load (default จาก config.json, unused but connected for read_measurements fallback)")
    ap.add_argument("--rated-ah", type=float, default=5.3, help="Rated capacity (Ah)")
    ap.add_argument("--cells", type=int, default=6, help="Series cell count")
    ap.add_argument("--chemistry", default="LeadAcid", help="Chemistry name (battery_profiles.py)")
    ap.add_argument("--charge-v", type=float, default=14.4, help="CV charge setpoint (V)")
    ap.add_argument("--charge-a", type=float, default=1.0, help="CC current limit (A, ~0.2C default)")
    ap.add_argument("--checkpoints", type=int, default=6,
                    help="Number of pause-and-rest checkpoints across the charge")
    ap.add_argument("--checkpoint-ah", type=float, default=None,
                    help="Ah between checkpoints (default: rated_ah / checkpoints)")
    ap.add_argument("--rest-min", type=float, default=10.0,
                    help="Minimum rest minutes at each checkpoint before trusting OCV")
    ap.add_argument("--settle-window-s", type=float, default=60.0)
    ap.add_argument("--settle-dv-mv", type=float, default=10.0,
                    help="Max mV spread over the settle window to call it rested")
    ap.add_argument("--settle-timeout-min", type=float, default=30.0,
                    help="Give up waiting for settle after this long (still records the reading)")
    ap.add_argument("--csv-out", default=None,
                    help="Checkpoint data CSV path (default: sessions/eta_calibration_TIMESTAMP.csv)")
    args = ap.parse_args()

    cfg_psu, cfg_load = _config_ports()
    psu_port = args.psu or cfg_psu
    load_port = args.load or cfg_load
    if not psu_port or not load_port:
        print("ไม่พบ VISA address ของ PSU/Load — ใส่ --psu/--load หรือตั้งใน config.json")
        sys.exit(1)

    model = BatteryModel(args.chemistry, args.rated_ah, args.cells, 1)
    checkpoint_ah = args.checkpoint_ah or (args.rated_ah / args.checkpoints)

    csv_path = args.csv_out or os.path.join(
        "sessions", f"eta_calibration_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    csv_f = open(csv_path, "w", newline="", encoding="utf-8")
    writer = csv.writer(csv_f)
    writer.writerow(["checkpoint", "ah_in_since_last", "ah_in_cumulative",
                     "ocv_v", "soc_ocv_pct", "delta_soc_ocv_pct",
                     "nominal_delta_soc_pct_if_eta_1", "measured_eta",
                     "model_eta_assumed", "settled"])

    hw = HardwareController()
    print(f"Connecting PSU={psu_port}  Load={load_port} ...")
    hw.connect_instruments(psu_port, load_port)
    print("PSU/Load connected OK")

    results = []
    try:
        hw.load_off()
        print("\n=== ขั้นแรก: อ่าน OCV เริ่มต้น (ต้องพักมาก่อนแล้ว) ===")
        v0, _ = hw.read_measurements(prefer_load_v=False)
        soc0 = model.get_soc_from_ocv(v0, hw.current_temp)
        print(f"  starting OCV={v0:.3f} V -> SoC={soc0:.1f}% (assumed already rested — "
              f"ถ้าไม่แน่ใจ ให้พักก่อนรันสคริปต์นี้)")
        soc_prev, ah_cum = soc0, 0.0

        for cp in range(1, args.checkpoints + 1):
            print(f"\n=== Checkpoint {cp}/{args.checkpoints}: ชาร์จ {checkpoint_ah:.3f} Ah "
                  f"(CC {args.charge_a} A / CV {args.charge_v} V) ===")
            hw.set_psu(True, str(args.charge_v), str(args.charge_a))
            t0 = time.perf_counter()
            last_t = t0
            ah_this = 0.0
            last_i = 0.0
            while ah_this < checkpoint_ah:
                v, i_psu = hw.read_measurements(prefer_load_v=False)
                now = time.perf_counter()
                dt = now - last_t
                last_t = now
                i_chg = max(0.0, i_psu)   # charge current, PSU convention
                ah_this += 0.5 * (i_chg + last_i) * dt / 3600.0
                last_i = i_chg
                if int(now - t0) % 30 < 1:
                    print(f"    {ah_this:.3f}/{checkpoint_ah:.3f} Ah  V={v:.3f}  I={i_chg:.3f} A", end="\r")
                time.sleep(1.0)
                if v >= args.charge_v - 0.02 and i_chg < 0.05 * args.charge_a:
                    print("\n    tapered to near-zero current — checkpoint reached early (fully charged)")
                    break
            hw.psu_off()
            print(f"\n  charged {ah_this:.3f} Ah this checkpoint ({ah_cum + ah_this:.3f} Ah cumulative)")
            ah_cum += ah_this

            print(f"  resting >= {args.rest_min:.0f} min for OCV to settle...")

            def _progress(elapsed, v, dv):
                dv_str = f"{dv*1000:.1f} mV" if dv == dv else "—"
                print(f"    rest {elapsed:5.0f}s  V={v:.3f}  spread(60s)={dv_str}", end="\r")

            settled, v_ocv = wait_for_settle(
                hw, args.rest_min * 60.0, args.settle_window_s,
                args.settle_dv_mv / 1000.0, args.settle_timeout_min * 60.0,
                on_progress=_progress)
            print()
            soc_ocv = model.get_soc_from_ocv(v_ocv, hw.current_temp)
            delta_soc = soc_ocv - soc_prev
            nominal_delta = 100.0 * ah_this / args.rated_ah
            measured_eta = delta_soc / nominal_delta if nominal_delta > 1e-6 else float("nan")
            model_eta = _model_eta_for_soc(soc_prev)

            print(f"  OCV={v_ocv:.3f} V ({'settled' if settled else 'TIMED OUT — reading may still be surface-charged'})"
                  f" -> SoC={soc_ocv:.1f}%  (Δ{delta_soc:+.1f} pp vs nominal Δ{nominal_delta:+.1f} pp"
                  f" if η=1.0 -> measured η={measured_eta:.2f}, model assumes η={model_eta:.2f})")

            writer.writerow([cp, f"{ah_this:.4f}", f"{ah_cum:.4f}", f"{v_ocv:.4f}",
                             f"{soc_ocv:.2f}", f"{delta_soc:.2f}", f"{nominal_delta:.2f}",
                             f"{measured_eta:.4f}" if measured_eta == measured_eta else "",
                             f"{model_eta:.4f}", int(settled)])
            csv_f.flush()
            results.append((soc_prev, soc_ocv, measured_eta, model_eta))
            soc_prev = soc_ocv

            if soc_ocv >= 99.5:
                print("\n  reached ~100% OCV — stopping (further charging is pure absorption/gassing)")
                break

    except KeyboardInterrupt:
        print("\nหยุดโดยผู้ใช้")
    finally:
        csv_f.close()
        try:
            hw.psu_off()
        except Exception:
            pass
        try:
            hw.load_off()
        except Exception:
            pass

    print(f"\nCSV: {csv_path}")
    print("\n=== สรุปเทียบ η ที่วัดได้จริง vs ที่โมเดลสมมติ (per SoC band) ===")
    _print_band_summary(results)


def _model_eta_for_soc(soc: float) -> float:
    """Mirrors StateEstimator._coulomb_eta's lead-acid bands — kept in sync
    manually since this script has no live estimator instance to ask."""
    if soc < 75.0:
        return 0.97
    if soc < 90.0:
        return 0.92
    return 0.75


def _print_band_summary(results):
    if not results:
        print("  (no checkpoints completed)")
        return
    bands = {"<75%": [], "75-90%": [], ">90%": []}
    for soc_start, soc_end, eta, model_eta in results:
        if eta != eta:
            continue
        key = "<75%" if soc_start < 75.0 else ("75-90%" if soc_start < 90.0 else ">90%")
        bands[key].append(eta)
    for band, vals in bands.items():
        if not vals:
            print(f"  {band:8s}: no checkpoints in this band")
            continue
        avg = sum(vals) / len(vals)
        model = _model_eta_for_soc({"<75%": 50.0, "75-90%": 80.0, ">90%": 95.0}[band])
        flag = "  <-- update battery_profiles.py" if abs(avg - model) > 0.05 else "  (close to model)"
        print(f"  {band:8s}: measured η = {avg:.2f} (n={len(vals)})  vs model η = {model:.2f}{flag}")


if __name__ == "__main__":
    main()
