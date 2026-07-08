import csv
import hashlib
import json
import os
from datetime import datetime
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# R3 (industrial-grade audit): session audit trail — operator/software-version/
# calibration snapshot, previously captured nowhere at all. See
# write_session_metadata()'s own docstring for the full rationale.
# ---------------------------------------------------------------------------
_app_version_cache: Optional[str] = None


def get_app_version() -> str:
    """Best-effort short git commit hash identifying the exact code that produced
    a session — cached after the first call (it never changes mid-run). Falls
    back to "unknown" if this isn't a git checkout (e.g. a packaged/frozen build)
    or git isn't on PATH; must never raise or block startup over this."""
    global _app_version_cache
    if _app_version_cache is not None:
        return _app_version_cache
    try:
        import subprocess
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3, cwd=repo_root)
        if result.returncode == 0 and result.stdout.strip():
            _app_version_cache = result.stdout.strip()
            return _app_version_cache
    except Exception:
        pass
    _app_version_cache = "unknown"
    return _app_version_cache


def write_session_metadata(csv_path: str, config: Any) -> None:
    """Write a companion <csv_path>.meta.json capturing the audit-trail context
    that used to exist nowhere: which operator ran this session, which exact
    software version produced it, and which calibration values (harness
    resistance, product measured_params) were in effect at the time. Without
    this, a graded result could never be traced back to who tested it or which
    calibration snapshot graded it — and since config.json/battery_profiles.json
    are NOT versioned, a later recalibration would make an old result
    unreconstructable even from the archived CSV alone.

    Written at start_logging() time (not stop_logging()) so it's still captured
    even if the session crashes mid-test — a crash is exactly when this context
    matters most for a post-incident investigation.

    Best-effort and non-fatal: a metadata write failure must never block a test
    from starting. `config` is duck-typed (ConfigManager or anything with the
    same .battery/.system attribute shape) so this has no import-time dependency
    on aset_batt.core.config.
    """
    try:
        battery = getattr(config, "battery", None)
        system = getattr(config, "system", None)
        operator = (getattr(system, "operator_name", "") or "").strip()
        if not operator:
            try:
                import getpass
                operator = getpass.getuser()
            except Exception:
                operator = "unknown"

        product_name = getattr(battery, "product_name", "") or ""
        measured_params = {}
        if product_name:
            try:
                from aset_batt.core import battery_profiles
                measured_params = battery_profiles.get_measured_params(product_name)
            except Exception:
                pass

        meta = {
            "operator": operator,
            "app_version": get_app_version(),
            "written_at": datetime.now().isoformat(timespec="seconds"),
            "battery_type": getattr(battery, "battery_type", ""),
            "product_name": product_name,
            "rated_capacity_ah": getattr(battery, "rated_capacity", None),
            "cells_series": getattr(battery, "cells_series", None),
            "cells_parallel": getattr(battery, "cells_parallel", None),
            "harness_resistance_ohm": getattr(battery, "harness_resistance_ohm", None),
            "measured_params": measured_params,
        }
        with open(csv_path + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not write session metadata for {csv_path}: {e}")

# ---------------------------------------------------------------------------
# Cloud-push helpers (used by cloud_push.py)
# ---------------------------------------------------------------------------

_CHANNELS = ["Voltage_V", "Current_A", "SoC_pct", "Resistance_mOhm", "Temperature_C"]

_MODE_LABEL = {
    "cc-cv charge":               "CC-CV Charge",
    "constant current discharge":  "CC Discharge",
    "hppc pulse test":             "HPPC",
}


def _tail_csv_rows(csv_path: str, limit: int = 20000) -> list:
    """Return up to *limit* rows from *csv_path* as a list of dicts."""
    rows = []
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows[-limit:] if len(rows) > limit else rows
    except Exception as e:
        logger.warning("_tail_csv_rows failed: %s", e)
        return []


def _extract_series(rows: list, keys: list = None) -> dict:
    """Extract columns from rows into dict of float lists."""
    if keys is None:
        keys = ["Elapsed_s"] + _CHANNELS
    series: dict = {k: [] for k in keys}
    for row in rows:
        for k in keys:
            try:
                series[k].append(float(row.get(k, "nan")))
            except (ValueError, TypeError):
                series[k].append(float("nan"))
    return series


def _compute_summary(rows: list) -> dict:
    """Compute simple summary stats from CSV rows."""
    if not rows:
        return {"row_count": 0}
    last = rows[-1]
    def _f(key):
        try:
            return float(last.get(key, "nan"))
        except (ValueError, TypeError):
            return None

    # test phase — ดึงจาก Mode column ของแถวล่าสุดที่ไม่ว่าง
    test_phase = None
    for r in reversed(rows):
        raw = r.get("Mode", "").strip()
        if raw:
            test_phase = _MODE_LABEL.get(raw.lower(), raw)
            break

    v_vals = []
    i_vals = []
    elapsed = 0.0
    for r in rows:
        try:
            v_vals.append(float(r.get("Voltage_V", "nan")))
        except (ValueError, TypeError):
            pass
        try:
            i_vals.append(float(r.get("Current_A", "nan")))
        except (ValueError, TypeError):
            pass
        try:
            elapsed = float(r.get("Elapsed_s", 0))
        except (ValueError, TypeError):
            pass

    avg_v = sum(v_vals) / len(v_vals) if v_vals else None
    avg_i = sum(i_vals) / len(i_vals) if i_vals else None
    capacity_ah = abs(avg_i * elapsed / 3600.0) if avg_i and elapsed else None
    energy_wh = abs(avg_i * avg_v * elapsed / 3600.0) if avg_i and avg_v and elapsed else None

    return {
        "row_count": len(rows),
        "elapsed_s": elapsed,
        "avg_voltage_v": avg_v,
        "avg_current_a": avg_i,
        "capacity_ah": capacity_ah,
        "energy_wh": energy_wh,
        "test_phase": test_phase,
        "latest": {
            "Voltage_V": _f("Voltage_V"),
            "Current_A": _f("Current_A"),
            "SoC_pct": _f("SoC_pct"),
            "Resistance_mOhm": _f("Resistance_mOhm"),
            "Temperature_C": _f("Temperature_C"),
            # Missing column (older CSVs predating this field) defaults to calibrated —
            # they were all logged before the "still just the pre-fit guess" distinction
            # existed, i.e. real per-sample Rin the whole way through.
            "Rin_Calibrated": last.get("Rin_Calibrated", "1").strip() != "0",
        },
    }


def _run_analysis(config_manager, csv_path: str) -> dict:
    """Run unified analysis on *csv_path*; returns a result dict."""
    try:
        from aset_batt.acquisition.analysis import analyze_csv_mp, profile_from_config
        profile = profile_from_config(config_manager)
        result = analyze_csv_mp(csv_path, profile)
        # strip large numpy arrays (ica/dtv) — ไม่ต้องการบน cloud
        clean = {k: v for k, v in result.items() if k not in ("ica", "dtv")}
        clean["success"] = True
        return clean
    except Exception as e:
        return {"success": False, "error": str(e)}

class DataHandler:
    def __init__(self):
        self.is_recording = False
        self.csv_file = None
        self.csv_writer = None
        self.current_path: str = ""   # path ของ session ปัจจุบัน
        self._last_flush = 0.0        # perf_counter of the last disk flush — see log_row

    @staticmethod
    def make_session_path(sessions_dir: str = "sessions", label: str = "") -> str:
        """สร้าง path สำหรับ session ใหม่.

        ไม่มี label → sessions/test_20260625_143022.csv (เหมือนเดิม)
        มี label  → sessions/test_HPPC_20260625_143022.csv (บอกชนิดเทสต์ในชื่อไฟล์)

        label ถูก sanitize เหลือ [A-Za-z0-9] เท่านั้น เพื่อไม่ให้กระทบการ parse
        timestamp (\\d{8}_\\d{6}) ใน _format_session_time/_detect_session_type."""
        os.makedirs(sessions_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in (label or "") if c.isalnum())
        prefix = f"test_{safe}_" if safe else "test_"
        return os.path.join(sessions_dir, f"{prefix}{ts}.csv")

    def start_logging(self, filepath: str):
        """เริ่มบันทึก CSV — คืน (True, "") หรือ (False, error_message)"""
        try:
            self.csv_file = open(filepath, 'a', newline='', encoding='utf-8-sig')
            self.csv_writer = csv.writer(self.csv_file)
            # เขียน header เฉพาะเมื่อไฟล์ใหม่ (ขนาด 0)
            if os.path.getsize(filepath) == 0:
                self.csv_writer.writerow([
                    "Timestamp", "Elapsed_s",
                    "Voltage_V", "Current_A",
                    "SoC_pct", "Resistance_mOhm", "Temperature_C",
                    "Rin_Calibrated",
                ])
            self.current_path = filepath
            self.is_recording = True
            return True, "Success"
        except Exception as e:
            return False, str(e)

    def stop_logging(self):
        self.is_recording = False
        if self.csv_file:
            try:
                self.csv_file.close()
            except Exception:
                pass
            self.csv_file = None
            # R4 (industrial-grade audit): a SHA-256 sidecar (<path>.sha256) lets
            # anyone later verify a session CSV hasn't been edited since the test
            # completed — there was previously no way to prove (or disprove) that at
            # all. Best-effort: a hashing failure must never prevent the session
            # from being considered stopped, so this is deliberately outside the
            # try/except above (csv_file is already closed and cleared either way).
            if self.current_path:
                try:
                    self._write_integrity_sidecar(self.current_path)
                except Exception as e:
                    logger.error(f"Could not write integrity sidecar for "
                                f"{self.current_path}: {e}")

    @staticmethod
    def _hash_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()

    @classmethod
    def _write_integrity_sidecar(cls, csv_path: str) -> None:
        digest = cls._hash_file(csv_path)
        with open(csv_path + ".sha256", "w", encoding="utf-8") as f:
            f.write(f"{digest}  {os.path.basename(csv_path)}\n")

    @classmethod
    def verify_integrity(cls, csv_path: str) -> Optional[bool]:
        """Check a session CSV against its .sha256 sidecar (written by
        stop_logging()). Returns True if it matches (untouched since the test
        completed), False if it doesn't (modified, corrupted, or truncated), or
        None if no sidecar exists — e.g. a session logged before this feature
        existed, one that's still actively recording, or one that never reached a
        clean stop_logging() call (e.g. a crash mid-test)."""
        sidecar_path = csv_path + ".sha256"
        if not os.path.exists(sidecar_path) or not os.path.exists(csv_path):
            return None
        try:
            with open(sidecar_path, "r", encoding="utf-8") as f:
                expected = f.read().split()[0]
        except Exception:
            return None
        try:
            return cls._hash_file(csv_path) == expected
        except Exception:
            return None

    def log_row(self, elapsed_s: float, v: float, i_net: float,
                soc: float, resistance_mohm: float, temp_c: float,
                rin_calibrated: bool = True):
        """
        บันทึก 1 แถวข้อมูล

        Args:
            elapsed_s      : วินาทีที่ผ่านไปนับจากเริ่ม test (ไม่ใช่ unix timestamp)
            v              : Voltage (V)
            i_net          : Net current (A)
            soc            : State of Charge (%)
            resistance_mohm: Internal resistance (mΩ) — still shown live even before a
                             real HPPC fit (see rin_calibrated), so the operator keeps
                             seeing a continuous trend instead of a gap.
            temp_c         : Temperature (°C)
            rin_calibrated : False = resistance_mohm is still _ekf_rc_defaults()'s
                             uncalibrated placeholder guess, not a real per-pulse fit —
                             the UI marks it "estimated" instead of hiding it.
        """
        if self.is_recording and self.csv_writer:
            try:
                self.csv_writer.writerow([
                    datetime.now().strftime("%H:%M:%S"),
                    f"{elapsed_s:.1f}",
                    f"{v:.4f}",
                    f"{i_net:.4f}",
                    f"{soc:.2f}",
                    f"{resistance_mohm:.2f}",
                    f"{temp_c:.2f}",
                    "1" if rin_calibrated else "0",
                ])
                # flush() forces a real disk write (or, on this repo's OneDrive-synced
                # project folder, a sync-agent wakeup) every call — at the monitor
                # loop's ~10 Hz during CHARGE that's 10 forced writes/sec, a plausible
                # source of periodic stutters. Throttled to ~1/s: worst case loses <1s
                # of rows on a hard crash, which cloud push (5s interval, see
                # cloud_push_interval) already tolerates just as well.
                import time
                now = time.perf_counter()
                if now - self._last_flush >= 1.0:
                    self._last_flush = now
                    self.csv_file.flush()
            except Exception as e:
                logger.error(f"CSV write error: {e}")

    @staticmethod
    def load_profile_csv(filepath: str, default_dt: float):
        """
        โหลด current profile จาก CSV

        รูปแบบที่รองรับ:
          - 2 คอลัมน์: current (A), duration (s)
          - 1 คอลัมน์: current (A) — ใช้ default_dt เป็น duration

        คืน: (data_list, None) หรือ (None, error_message)
        """
        data = []
        try:
            with open(filepath, 'r') as f:
                reader = csv.reader(f)
                for row in reader:
                    if not row:
                        continue
                    try:
                        if len(row) >= 2:
                            data.append((float(row[0]), float(row[1])))
                        elif len(row) == 1:
                            data.append((float(row[0]), float(default_dt)))
                    except ValueError:
                        continue
            return data, None
        except Exception as e:
            return None, str(e)