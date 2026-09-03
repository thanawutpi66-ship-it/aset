import csv
import json
import math
import sys
from pathlib import Path


def load(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        out.append({
            "t": float(r["Elapsed_s"]),
            "v": float(r["Voltage_V"]),
            "i": float(r["Current_A"]),
            "soc": float(r["SoC_pct"]),
            "rin": float(r["Resistance_mOhm"]),
            "temp": float(r["Temperature_C"]),
            "cal": int(r.get("Rin_Calibrated", "0") or 0),
        })
    return out


def integrate(rows, sign):
    ah = wh = 0.0
    for a, b in zip(rows, rows[1:]):
        dt = max(0.0, b["t"] - a["t"])
        ia = max(a["i"], 0.0) if sign == "positive" else max(-a["i"], 0.0)
        ib = max(b["i"], 0.0) if sign == "positive" else max(-b["i"], 0.0)
        ah += 0.5 * (ia + ib) * dt / 3600.0
        wh += 0.5 * (ia * a["v"] + ib * b["v"]) * dt / 3600.0
    return ah, wh


def segments(rows, threshold=0.2):
    def cls(i):
        return "discharge" if i > threshold else "charge" if i < -threshold else "rest"
    result = []
    start = 0
    state = cls(rows[0]["i"])
    for idx in range(1, len(rows)):
        s = cls(rows[idx]["i"])
        if s != state:
            chunk = rows[start:idx]
            result.append((state, chunk))
            start, state = idx, s
    result.append((state, rows[start:]))
    return result


def summarize(path):
    rows = load(path)
    pos_ah, pos_wh = integrate(rows, "positive")
    neg_ah, neg_wh = integrate(rows, "negative")
    active = [r for r in rows if abs(r["i"]) > 0.2]
    segs = []
    for state, chunk in segments(rows):
        if len(chunk) < 2:
            continue
        ah, wh = integrate(chunk, "positive" if state == "discharge" else "negative")
        segs.append({
            "state": state,
            "t0": round(chunk[0]["t"], 3),
            "t1": round(chunk[-1]["t"], 3),
            "duration_s": round(chunk[-1]["t"] - chunk[0]["t"], 3),
            "n": len(chunk),
            "i_mean": round(sum(r["i"] for r in chunk) / len(chunk), 5),
            "v_start": chunk[0]["v"],
            "v_end": chunk[-1]["v"],
            "v_min": min(r["v"] for r in chunk),
            "v_max": max(r["v"] for r in chunk),
            "soc_start": chunk[0]["soc"],
            "soc_end": chunk[-1]["soc"],
            "ah_abs": round(ah, 6),
            "wh_abs": round(wh, 6),
        })
    return {
        "path": str(path),
        "rows": len(rows),
        "elapsed_s": rows[-1]["t"] - rows[0]["t"],
        "v_start": rows[0]["v"],
        "v_end": rows[-1]["v"],
        "v_min": min(r["v"] for r in rows),
        "v_max": max(r["v"] for r in rows),
        "soc_start": rows[0]["soc"],
        "soc_end": rows[-1]["soc"],
        "i_active_mean": sum(r["i"] for r in active) / len(active) if active else math.nan,
        "i_min": min(r["i"] for r in rows),
        "i_max": max(r["i"] for r in rows),
        "positive_ah": pos_ah,
        "negative_ah": neg_ah,
        "positive_wh": pos_wh,
        "negative_wh": neg_wh,
        "rin_calibrated_rows": sum(r["cal"] for r in rows),
        "segments": segs,
    }


print(json.dumps([summarize(p) for p in sys.argv[1:]], ensure_ascii=False, indent=2))
