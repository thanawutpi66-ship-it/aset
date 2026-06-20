"""
cloud_push.py — ส่งข้อมูลเทสต์ล่าสุดจากเครื่องแล็บขึ้น ASET Cloud Dashboard

อ่าน battery_data.csv → คำนวณ summary + AI analysis (reuse โค้ดเดิม) →
downsample series → POST ขึ้น cloud (auth ด้วย token)

การใช้งาน:
  set CLOUD_DASHBOARD_URL=https://your-app.herokuapp.com
  set INGEST_TOKEN=xxxxxxxx
  python cloud_push.py                 # ส่งครั้งเดียว
  python cloud_push.py --interval 30   # ส่งทุก 30 วินาที (รันค้างไว้ระหว่างเทสต์)
"""
import argparse
import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error

from aset_batt.core.config import config_manager
# Local web dashboard helpers copied here so cloud_push works without the local web server
import csv
from typing import Any, Dict, List

_CHANNELS = ["Voltage_V", "Current_A", "SoC_pct", "Resistance_mOhm", "Temperature_C"]

def _tail_csv_rows(csv_path: str, limit: int = 200) -> List[Dict[str, str]]:
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows[-limit:]


def _extract_series(rows: List[Dict[str, str]], keys: List[str]) -> Dict[str, List[float]]:
    out: Dict[str, List[float]] = {k: [] for k in keys}
    for r in rows:
        for k in keys:
            try:
                out[k].append(float(r.get(k, "")))
            except Exception:
                pass
    return out


def _stats(vals: List[float]):
    if not vals:
        return {"min": None, "max": None, "avg": None}
    return {"min": min(vals), "max": max(vals), "avg": sum(vals) / len(vals)}


def _compute_summary(rows: List[Dict[str, str]]):
    series = _extract_series(rows, keys=_CHANNELS + ["Elapsed_s"])
    elapsed = series.get("Elapsed_s", [])
    current = series.get("Current_A", [])
    voltage = series.get("Voltage_V", [])

    capacity_ah = 0.0
    energy_wh = 0.0
    n = min(len(elapsed), len(current), len(voltage))
    for k in range(1, n):
        dt_h = (elapsed[k] - elapsed[k - 1]) / 3600.0
        i_mid = (current[k] + current[k - 1]) / 2.0
        v_mid = (voltage[k] + voltage[k - 1]) / 2.0
        if i_mid > 0:
            capacity_ah += i_mid * dt_h
            energy_wh += i_mid * v_mid * dt_h

    latest = rows[-1] if rows else {}
    return {
        "row_count": len(rows),
        "elapsed_s": elapsed[-1] if elapsed else 0.0,
        "latest": {k: latest.get(k) for k in _CHANNELS},
        "stats": {k: _stats(series.get(k, [])) for k in _CHANNELS},
        "capacity_ah": capacity_ah,
        "energy_wh": energy_wh,
    }


def _run_analysis(config: Any, csv_path: str) -> Dict[str, Any]:
    """Run BatteryAnalyzer directly (was previously provided by web_server)."""
    from aset_batt.core.analysis_module import BatteryAnalyzer
    rated = getattr(config.battery, "rated_capacity", 2.0)
    base_r0 = 25.0
    try:
        from aset_batt.core.battery_model import BatteryModel
        bm = BatteryModel(
            config.battery.battery_type, config.battery.nominal_voltage,
            config.battery.cells_series, config.battery.cells_parallel,
        )
        base_r0 = bm.base_r0_mohm_pack
    except Exception:
        pass

    analyzer = BatteryAnalyzer(rated_capacity_ah=rated, base_r0_mohm=base_r0)
    return analyzer.analyze(csv_path).to_dict()

logger = logging.getLogger(__name__)


def resolve_token(explicit: str = "") -> str:
    """หา ingest token: arg ตรง > env INGEST_TOKEN > ไฟล์ cloud_token.txt (gitignored)"""
    if explicit:
        return explicit.strip()
    env = os.environ.get("INGEST_TOKEN", "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for path in ("cloud_token.txt", os.path.join(here, "cloud_token.txt")):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
        except Exception:
            pass
    return ""


def _downsample(rows, max_points):
    """ลดจำนวนจุดของ series ให้ไม่เกิน max_points (stride sampling)"""
    keys = ["Elapsed_s"] + _CHANNELS
    series = _extract_series(rows, keys=keys)
    n = len(series.get("Elapsed_s", []))
    if n <= max_points:
        return series
    stride = (n + max_points - 1) // max_points
    return {k: v[::stride] for k, v in series.items()}


def build_payload(csv_path, max_points):
    rows = _tail_csv_rows(csv_path, limit=20000)
    summary = _compute_summary(rows)
    summary["csv_path"] = csv_path
    try:
        analysis = _run_analysis(config_manager, csv_path)
    except Exception as e:
        analysis = {"success": False, "error": str(e)}

    bat = config_manager.battery
    battery_desc = f"{bat.cells_series}S{bat.cells_parallel}P {bat.battery_type} {bat.rated_capacity}Ah"
    return {
        "meta": {
            "battery": battery_desc,
            "csv_name": os.path.basename(csv_path),
            "pushed_at": time.time(),
        },
        "summary": summary,
        "analysis": analysis,
        "series": _downsample(rows, max_points),
    }


def push(url, token, payload, timeout=120):
    # timeout เผื่อ cloud cold-start (เช่น Azure B1 ที่ไม่ได้เปิด Always On)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/api/ingest", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Ingest-Token": token},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


class CloudPusher:
    """Background daemon ที่ push CSV ล่าสุดขึ้น cloud เป็นช่วง ๆ (auto-push)

    ให้แอปแล็บสร้าง+start ตอนเริ่ม แล้ว stop ตอนปิด — push ทุก `interval` วินาที
    (เห็นผลสด ๆ ระหว่างเทสต์ + snapshot สุดท้ายค้างไว้บน dashboard)
    """

    def __init__(self, url: str, token: str = "", csv_path: str = "",
                 interval: float = 30.0, max_points: int = 400):
        self.url = (url or "").strip()
        self.token = resolve_token(token)
        self.csv_path = csv_path or config_manager.system.csv_filepath
        self.interval = max(5.0, float(interval))
        self.max_points = max_points
        self._running = False
        self._thread = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def start(self):
        if self._running:
            return
        if not self.enabled:
            logger.warning("CloudPusher ปิด (ขาด url หรือ token) — ตั้ง env INGEST_TOKEN "
                           "หรือไฟล์ cloud_token.txt เพื่อเปิด auto-push")
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("CloudPusher started -> %s (ทุก %.0fs)", self.url, self.interval)

    def stop(self):
        self._running = False

    def push_once(self) -> bool:
        """push หนึ่งครั้ง (best-effort — ไม่ throw)"""
        try:
            payload = build_payload(self.csv_path, self.max_points)
            status, _ = push(self.url, self.token, payload)
            logger.debug("cloud push -> HTTP %s (rows=%s)",
                         status, payload["summary"].get("row_count"))
            return True
        except Exception as e:
            logger.warning("cloud push ล้มเหลว: %s", e)
            return False

    def _loop(self):
        while self._running:
            self.push_once()
            # sleep เป็นช่วงสั้น ๆ เพื่อให้ stop ได้เร็ว
            slept = 0.0
            while self._running and slept < self.interval:
                time.sleep(0.5)
                slept += 0.5


def main():
    p = argparse.ArgumentParser(description="Push test data to ASET Cloud Dashboard")
    p.add_argument("--url", default=os.environ.get("CLOUD_DASHBOARD_URL", ""),
                   help="cloud base URL (หรือ env CLOUD_DASHBOARD_URL)")
    p.add_argument("--token", default=os.environ.get("INGEST_TOKEN", ""),
                   help="ingest token (หรือ env INGEST_TOKEN)")
    p.add_argument("--csv", default=config_manager.system.csv_filepath)
    p.add_argument("--interval", type=float, default=0.0,
                   help="วินาทีต่อรอบ (0 = ส่งครั้งเดียว)")
    p.add_argument("--max-points", type=int, default=400)
    args = p.parse_args()

    if not args.url or not args.token:
        raise SystemExit("ต้องระบุ --url และ --token (หรือ env CLOUD_DASHBOARD_URL / INGEST_TOKEN)")

    def once():
        payload = build_payload(args.csv, args.max_points)
        status, body = push(args.url, args.token, payload)
        rows = payload["summary"].get("row_count", 0)
        grade = payload["analysis"].get("grade", "?")
        print(f"[{time.strftime('%H:%M:%S')}] pushed rows={rows} grade={grade} -> HTTP {status}")

    if args.interval <= 0:
        once()
        return
    print(f"pushing every {args.interval}s to {args.url} (Ctrl+C to stop)")
    while True:
        try:
            once()
        except urllib.error.URLError as e:
            print(f"push failed: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
