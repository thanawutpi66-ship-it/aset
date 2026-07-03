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
from aset_batt.storage.data_utils import _tail_csv_rows, _compute_summary, _run_analysis, _extract_series, _CHANNELS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level test-phase state — updated by GUI via set_cloud_meta()
# ---------------------------------------------------------------------------
_meta_override: dict = {}


def set_cloud_meta(phase: str | None = None,
                   test_mode: str | None = None,
                   workflow: str | None = None,
                   elapsed_s: int | None = None,
                   total_s: int | None = None) -> None:
    """อัปเดต phase/test_mode/ETA ปัจจุบัน — ถูก merge เข้า meta ใน push ถัดไป."""
    if phase is not None:
        _meta_override["phase"] = phase
    if test_mode is not None:
        _meta_override["test_mode"] = test_mode
    if workflow is not None:
        _meta_override["workflow"] = workflow
    if elapsed_s is not None:
        _meta_override["elapsed_s"] = elapsed_s
    if total_s is not None:
        _meta_override["total_s"] = total_s


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


def build_payload(csv_path, max_points, cached_analysis=None, config=None):
    """Build push payload. Pass cached_analysis to skip the expensive ECM fitting."""
    cfg = config if config is not None else config_manager
    rows = _tail_csv_rows(csv_path, limit=20000)
    summary = _compute_summary(rows)
    summary["csv_path"] = csv_path

    if cached_analysis is None:
        try:
            cached_analysis = _run_analysis(cfg, csv_path)
        except Exception as e:
            cached_analysis = {"success": False, "error": str(e)}

    bat = cfg.battery
    prod = getattr(bat, "product_name", "") or ""
    battery_desc = prod if prod else (
        f"{bat.cells_series}S{bat.cells_parallel}P {bat.battery_type} {bat.rated_capacity}Ah"
    )
    return {
        "meta": {
            "battery": battery_desc,
            "csv_name": os.path.basename(csv_path),
            "pushed_at": time.time(),
            **_meta_override,   # phase, test_mode, workflow (set by GUI)
        },
        "summary": summary,
        "analysis": cached_analysis,
        "series": _downsample(rows, max_points),
    }


class _NumpySafeEncoder(json.JSONEncoder):
    """JSON encoder ที่รองรับ numpy scalar/array โดยไม่ต้อง import numpy at module level."""
    def default(self, obj):
        t = type(obj).__name__
        # numpy scalars
        if hasattr(obj, "item"):
            try:
                return obj.item()
            except Exception:
                pass
        # numpy arrays
        if hasattr(obj, "tolist"):
            return obj.tolist()
        return super().default(obj)


def push(url, token, payload, timeout=120):
    # timeout เผื่อ cloud cold-start (เช่น Azure B1 ที่ไม่ได้เปิด Always On)
    data = json.dumps(payload, cls=_NumpySafeEncoder).encode("utf-8")
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
                 interval: float = 5.0, max_points: int = 400,
                 analysis_interval: float = 60.0,
                 data_handler=None, config=None):
        self._config = config if config is not None else config_manager
        self.url = (url or "").strip()
        self.token = resolve_token(token)
        self.csv_path = csv_path or self._config.system.csv_filepath
        self._data_handler = data_handler
        self.interval = max(3.0, float(interval))
        self.analysis_interval = max(self.interval, float(analysis_interval))
        self.max_points = max_points
        self._running = False
        self._thread = None
        self._cached_analysis: dict = {}
        self._last_analysis_t: float = 0.0

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
        """push หนึ่งครั้ง — ใช้ cached analysis ถ้ายังไม่ถึงเวลา refresh (best-effort)"""
        try:
            active = (self._data_handler.current_path
                      if self._data_handler and getattr(self._data_handler, "current_path", "")
                      else self.csv_path)

            now = time.time()
            if now - self._last_analysis_t >= self.analysis_interval:
                try:
                    self._cached_analysis = _run_analysis(self._config, active)
                    self._last_analysis_t = now
                    logger.debug("cloud push: analysis refreshed")
                except Exception as e:
                    logger.warning("cloud push: analysis failed — using cache: %s", e)

            payload = build_payload(active, self.max_points,
                                    cached_analysis=self._cached_analysis or None,
                                    config=self._config)
            status, _ = push(self.url, self.token, payload)
            logger.debug("cloud push -> HTTP %s (rows=%s, analysis_age=%.0fs)",
                         status, payload["summary"].get("row_count"),
                         now - self._last_analysis_t)
            return True
        except Exception as e:
            logger.warning("cloud push ล้มเหลว: %s", e)
            return False

    def _poll_and_analyze(self) -> None:
        """Poll cloud for pending re-analysis requests; run them and push results back."""
        try:
            req = urllib.request.Request(
                self.url.rstrip("/") + "/api/pending-analyses",
                headers={"X-Ingest-Token": self.token},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            pending = data.get("pending", [])
            if not pending:
                return
            for item in pending:
                sidx = item.get("idx")
                csv_path = item.get("csv_path", "")
                if not csv_path or not os.path.exists(csv_path):
                    csv_path = (self._data_handler.current_path
                               if self._data_handler and getattr(self._data_handler, "current_path", "")
                               else self.csv_path)
                if not csv_path or not os.path.exists(csv_path):
                    logger.warning("cloud analyze: ไม่พบ CSV สำหรับ session %s", sidx)
                    continue
                logger.info("cloud analyze: session %s — รัน analysis (csv=%s)", sidx, os.path.basename(csv_path))
                try:
                    analysis = _run_analysis(self._config, csv_path)
                    self._cached_analysis = analysis
                    self._last_analysis_t = time.time()
                except Exception as e:
                    analysis = {"success": False, "error": str(e)}
                try:
                    body = json.dumps({"analysis": analysis}, cls=_NumpySafeEncoder).encode("utf-8")
                    req2 = urllib.request.Request(
                        self.url.rstrip("/") + f"/api/update-analysis/{sidx}",
                        data=body, method="POST",
                        headers={"Content-Type": "application/json", "X-Ingest-Token": self.token},
                    )
                    with urllib.request.urlopen(req2, timeout=30) as resp2:
                        logger.info("cloud analyze: session %s → HTTP %s", sidx, resp2.status)
                except Exception as e:
                    logger.warning("cloud analyze: push session %s ล้มเหลว: %s", sidx, e)
        except Exception as e:
            logger.debug("cloud analyze poll: %s", e)

    def _loop(self):
        while self._running:
            self.push_once()
            self._poll_and_analyze()
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
