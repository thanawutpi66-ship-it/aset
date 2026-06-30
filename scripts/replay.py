"""
Replay / ablation harness — validate SoC-estimation accuracy offline.

Records once, replays unlimited times: read a logged CSV (Elapsed_s, Voltage_V,
Current_A discharge-positive, Temperature_C), feed it row-by-row through the
StateEstimator under many on/off component configurations, and score each against
a ground-truth SoC curve. This turns "we improved the estimator" into measured
RMSE / max-error / capacity-error numbers (the research framework's R5–R9).

GROUND TRUTH (R5): for a *slow* constant-current discharge from full, the simple
current integral is essentially the true SoC (Rin/Peukert effects are negligible at
≤0.1C). Cap_true = ∫|I|dt over the discharge; SoC_true(t) = 100·(1 − Ah(t)/Cap_true).
Use a slow full-discharge log for a fair ruler, or pass --cap-true / --soc-start.

USAGE:
    python scripts/replay.py session.csv --battery LeadAcid --rated 5.3 --cells 6
    python scripts/replay.py session.csv --battery LiFePO4 --rated 50 --cells 8 --csv-out out.csv
"""
import argparse
import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator


# ---- ablation configurations (R7 matrix) -------------------------------------
# Each maps to StateEstimator flags. 'mode' B0 is handled specially (pure OCV lookup).
CONFIGS = [
    ("B0 OCV-only",   dict(mode="ocv")),
    ("B1 Coulomb",    dict(use_ekf=False, use_ocv=False, use_peukert=False,
                          use_eta=False, use_temp=False, alpha=1.0)),
    ("B2 CC+OCV",     dict(use_ekf=False, use_ocv=True, use_peukert=False,
                          use_eta=False, use_temp=False, alpha=0.05)),
    ("+Peukert",      dict(use_ekf=False, use_ocv=True, use_peukert=True,
                          use_eta=False, use_temp=False, alpha=0.05)),
    ("+Eta",          dict(use_ekf=False, use_ocv=True, use_peukert=False,
                          use_eta=True, use_temp=False, alpha=0.05)),
    ("+Temp",         dict(use_ekf=False, use_ocv=True, use_peukert=False,
                          use_eta=False, use_temp=True, alpha=0.05)),
    ("Full (EMA)",    dict(use_ekf=False, use_ocv=True, use_peukert=True,
                          use_eta=True, use_temp=True, alpha=0.05)),
    ("EKF (full)",    dict(use_ekf=True, use_ocv=True, use_peukert=True,
                          use_eta=True, use_temp=True)),
    ("EKF adaptive-R", dict(use_ekf=True, use_ocv=True, use_peukert=True,
                            use_eta=True, use_temp=True, adaptive_r=True)),
]


def load_csv(path):
    """Return lists (t_s, v, i_dis_positive, temp). Tolerant of column names."""
    t, v, i, tc = [], [], [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.lower(): c for c in reader.fieldnames}

        def pick(*names, default=None):
            for n in names:
                if n in cols:
                    return cols[n]
            return default

        c_t = pick("elapsed_s", "elapsed", "time", "time_s")
        c_v = pick("voltage_v", "voltage", "v")
        c_i = pick("current_a", "current", "i")
        c_tc = pick("temperature_c", "temp", "temperature")
        for row in reader:
            try:
                vv = float(row[c_v]); ii = float(row[c_i])
            except (TypeError, ValueError):
                continue
            tt = float(row[c_t]) if c_t and row.get(c_t) else (t[-1] + 1.0 if t else 0.0)
            temp = 25.0
            if c_tc and row.get(c_tc):
                try:
                    temp = float(row[c_tc])
                except ValueError:
                    pass
            t.append(tt); v.append(vv); i.append(ii); tc.append(temp)
    return t, v, i, tc


def ground_truth(t, i, cap_true=None, soc_start=100.0):
    """SoC_true(t) from current integration (trapezoidal). Returns (soc_true, cap)."""
    ah = [0.0]
    for k in range(1, len(t)):
        dt = max(0.0, t[k] - t[k - 1])
        ah.append(ah[-1] + 0.5 * (i[k] + i[k - 1]) * dt / 3600.0)
    cap = cap_true if cap_true else (max(ah) - min(ah))
    if cap <= 1e-6:
        cap = 1.0
    soc_true = [max(0.0, min(100.0, soc_start - a / cap * 100.0)) for a in ah]
    return soc_true, cap


def run_config(name, cfg, t, v, i, tc, battery, rated, cells, soc_start):
    model = BatteryModel(battery_type=battery, series_cells=cells)
    est = StateEstimator(rated_capacity=rated, battery_model=model)

    if cfg.get("mode") == "ocv":
        # B0: pure OCV→SoC lookup each sample (no coulomb counting)
        return [model.get_soc_from_ocv(vv, tcc) for vv, tcc in zip(v, tc)]

    for k, val in cfg.items():
        setattr(est, k, val)
    if cfg.get("adaptive_r"):
        est.use_ekf = True
    est.set_initial_soc(soc_start)
    if cfg.get("adaptive_r"):
        est._ensure_ekf().adaptive_r = True

    out = []
    for k in range(len(t)):
        dt = (t[k] - t[k - 1]) if k > 0 else (t[1] - t[0] if len(t) > 1 else 1.0)
        dt = max(1e-3, dt)
        st = est.update(v[k], i[k], dt=dt, temp=tc[k])
        out.append(st["soc"])
    return out


def metrics(soc_est, soc_true):
    n = min(len(soc_est), len(soc_true))
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    errs = [soc_est[k] - soc_true[k] for k in range(n)]
    rmse = math.sqrt(sum(e * e for e in errs) / n)
    maxe = max(abs(e) for e in errs)
    end_e = abs(errs[-1])
    return rmse, maxe, end_e


def main():
    ap = argparse.ArgumentParser(description="SoC estimator replay / ablation study")
    ap.add_argument("csv", help="logged session CSV")
    ap.add_argument("--battery", default="LeadAcid", help="chemistry (LeadAcid/LiFePO4/...)")
    ap.add_argument("--rated", type=float, required=True, help="rated capacity (Ah)")
    ap.add_argument("--cells", type=int, default=6, help="series cells")
    ap.add_argument("--cap-true", type=float, default=None, help="override true capacity (Ah)")
    ap.add_argument("--soc-start", type=float, default=100.0, help="SoC at start (%%)")
    ap.add_argument("--keep-threshold", type=float, default=1.0,
                    help="keep a component if it cuts RMSE by >= this (abs %%)")
    ap.add_argument("--csv-out", default=None, help="write per-sample SoC curves to CSV")
    args = ap.parse_args()

    t, v, i, tc = load_csv(args.csv)
    if len(t) < 5:
        print(f"ERROR: too few samples ({len(t)}) in {args.csv}")
        sys.exit(1)
    soc_true, cap = ground_truth(t, i, args.cap_true, args.soc_start)

    print(f"\nReplay: {os.path.basename(args.csv)}  |  {len(t)} samples  |  "
          f"{t[-1]/3600:.2f} h  |  {args.battery} {args.cells}S {args.rated}Ah")
    print(f"Ground-truth capacity (integrated): {cap:.3f} Ah  "
          f"(SoH vs rated = {cap/args.rated*100:.1f}%)\n")

    header = f"{'Config':<16}{'SoC RMSE %':>12}{'Max |err| %':>13}{'End err %':>11}"
    print(header); print("-" * len(header))

    results = {}
    curves = {}
    for name, cfg in CONFIGS:
        try:
            est_soc = run_config(name, dict(cfg), t, v, i, tc,
                                 args.battery, args.rated, args.cells, args.soc_start)
            rmse, maxe, ende = metrics(est_soc, soc_true)
            results[name] = rmse
            curves[name] = est_soc
            print(f"{name:<16}{rmse:>12.2f}{maxe:>13.2f}{ende:>11.2f}")
        except Exception as e:
            print(f"{name:<16}{'ERROR: ' + str(e):>36}")

    # keep/cut verdict (R9): compare each component vs the B2 core baseline
    base = results.get("B2 CC+OCV")
    if base is not None:
        print("\nKeep/Cut vs B2 core (Occam — keep only if it cuts RMSE >= "
              f"{args.keep_threshold:.1f}%):")
        for name in ("+Peukert", "+Eta", "+Temp", "Full (EMA)", "EKF (full)",
                     "EKF adaptive-R"):
            if name in results:
                gain = base - results[name]
                verdict = "KEEP" if gain >= args.keep_threshold else "cut"
                print(f"  {name:<16} ΔRMSE = {gain:+.2f}%   -> {verdict}")

    if args.csv_out:
        with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            names = list(curves.keys())
            w.writerow(["Elapsed_s", "SoC_true"] + names)
            for k in range(len(t)):
                w.writerow([f"{t[k]:.1f}", f"{soc_true[k]:.2f}"] +
                           [f"{curves[n][k]:.2f}" for n in names])
        print(f"\nPer-sample curves -> {args.csv_out}")


if __name__ == "__main__":
    main()
