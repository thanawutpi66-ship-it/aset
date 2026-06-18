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
import os
import time
import urllib.request
import urllib.error

from config import config_manager
from web_server import _tail_csv_rows, _compute_summary, _run_analysis, _extract_series, _CHANNELS


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


def push(url, token, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/api/ingest", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Ingest-Token": token},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


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
