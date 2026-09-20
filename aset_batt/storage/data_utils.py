import csv
import hashlib
import json
import os
import math
import uuid
import tempfile
import time
from datetime import datetime
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)


class StorageError(OSError):
    """A session could not be durably recorded; acquisition must stop safely."""


# All new sessions use this schema, whether they are written by AutoController
# sequences or the high-rate AcquisitionWorker.  Keep the original analysis
# columns first so existing CSV readers remain compatible.
SESSION_SCHEMA_VERSION = "2.3"
SESSION_COLUMNS = [
    "Timestamp", "Timestamp_ISO", "Elapsed_s", "Voltage_V", "Current_A", "SoC_pct",
    "Resistance_mOhm", "Temperature_C", "Rin_Calibrated", "Capacity_Ah",
    "Mode", "Schema_Version", "Session_ID", "Test_Type", "Phase",
    "Step_Index", "Voltage_Source", "Current_Source", "Sample_Quality",
    "Sample_Note", "Temperature_Status", "Temperature_Age_s", "Temperature_Source",
]


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
    except Exception as e:
        import logging
        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
    _app_version_cache = "unknown"
    return _app_version_cache


def write_session_metadata(csv_path: str, config: Any = None, *,
                           session_id: str = "", test_type: str = "",
                           extra: Optional[dict] = None, hardware: Any = None) -> None:
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
        peukert_snapshot = {}
        if battery is not None:
            try:
                from aset_batt.core import battery_profiles
                peukert_snapshot = battery_profiles.resolve_peukert_parameters(
                    product_name, getattr(battery, "battery_type", ""))
            except Exception as exc:
                logger.warning("Peukert metadata unavailable: %s", exc)
        measured_params = {}
        if product_name:
            try:
                from aset_batt.core import battery_profiles
                measured_params = battery_profiles.get_measured_params(product_name)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

        now = datetime.now().astimezone().isoformat(timespec="milliseconds")
        validation_campaign = None
        try:
            from aset_batt.core.validation_campaign import normalize_campaign
            candidate = normalize_campaign(getattr(system, "validation_campaign", None))
            if candidate["enabled"]:
                validation_campaign = candidate
        except Exception:
            validation_campaign = None

        meta = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": session_id,
            "test_type": test_type,
            "operator": operator,
            "app_version": get_app_version(),
            # A CSV is valid evidence only when its exact procedure and outcome
            # can be reconstructed.  The procedure snapshot is supplied as
            # ``extra[\"protocol\"]`` by each sequence; these fields express the
            # lifecycle common to every session, including a run interrupted by
            # a safety trip or by the operator.
            "status": "running",
            "started_at": now,
            "written_at": now,
            "battery_type": getattr(battery, "battery_type", ""),
            "product_name": product_name,
            "rated_capacity_ah": getattr(battery, "rated_capacity", None),
            "cells_series": getattr(battery, "cells_series", None),
            "cells_parallel": getattr(battery, "cells_parallel", None),
            "harness_resistance_ohm": getattr(battery, "harness_resistance_ohm", None),
            "measured_params": measured_params,
        }
        if peukert_snapshot:
            reference_capacity = (peukert_snapshot.get("peukert_reference_capacity_ah")
                                  or getattr(battery, "rated_capacity", None))
            reference_hr = peukert_snapshot["peukert_reference_hr"]
            reference_current = peukert_snapshot["peukert_reference_current_a"]
            if reference_current is None and reference_capacity and reference_hr > 0.0:
                reference_current = float(reference_capacity) / reference_hr
            meta.update({
                "peukert_k": peukert_snapshot["peukert_k"],
                "peukert_k_source": peukert_snapshot["peukert_k_source"],
                "peukert_reference_hr": peukert_snapshot["peukert_reference_hr"],
                "peukert_reference_current_a": reference_current,
                "peukert_reference_capacity_ah": reference_capacity,
                "peukert_formula_version": "peukert-power-law-v1",
            })
        hwcfg = getattr(config, "hardware", None)
        identity = {}
        try:
            candidate_identity = hardware.instrument_identity() if hardware is not None else {}
            if isinstance(candidate_identity, dict):
                identity = candidate_identity
            else:
                hardware = None
        except Exception:
            hardware = None
        def _number(value, default=0.0):
            try:
                result = float(value)
                return result if math.isfinite(result) else default
            except (TypeError, ValueError):
                return default
        cal = {
            "source": "CONFIGURED_OFFSET_CORRECTION",
            "applied": hardware is not None,
            "psu_voltage_offset_v": _number(getattr(hardware, "_psu_voltage_offset", getattr(hwcfg, "psu_v_offset", 0.0))),
            "psu_current_offset_a": _number(getattr(hardware, "_psu_configured_current_offset", getattr(hwcfg, "psu_i_offset", 0.0))),
            "load_voltage_offset_v": _number(getattr(hardware, "_load_voltage_offset", getattr(hwcfg, "load_v_offset", 0.0))),
            "load_current_offset_a": _number(getattr(hardware, "_load_current_offset", getattr(hwcfg, "load_i_offset", 0.0))),
        }
        if hardware is not None:
            cal.update({
                "psu_runtime_zero_offset_a": _number(getattr(hardware, "_psu_runtime_zero_offset", 0.0)),
                "psu_effective_current_offset_a": _number(getattr(hardware, "_psu_current_offset", cal["psu_current_offset_a"]), cal["psu_current_offset_a"]),
            })
        instruments = identity
        cal["version"] = hashlib.sha256(json.dumps(cal, sort_keys=True).encode()).hexdigest()[:12]
        cal["recorded_at"] = now
        meta["calibration"] = cal
        meta["instruments"] = instruments
        meta["temperature_source"] = "MLX90614 via ESP32" if hardware is not None else "unknown"
        meta["measurement_sources"] = {"voltage": "active instrument SCPI readback",
                                        "current": "active instrument SCPI readback"}
        meta["measurement_timestamp_basis"] = "HOST_READ_RETURN"
        meta["temperature_timestamp_basis"] = "HOST_SERIAL_PARSE_ARRIVAL"
        meta["voltage_current_alignment"] = "combined response when instrument supports it; otherwise sequential SCPI queries"
        if system is not None:
            meta["safety_limits"] = dict(getattr(system, "safety_limits", {}) or {})
        resolver = getattr(config, "effective_safety_limits", None)
        if callable(resolver):
            try:
                meta["effective_safety_limits"] = resolver()
            except Exception as exc:
                logger.warning("Effective safety snapshot unavailable: %s", exc)
        if product_name:
            try:
                from aset_batt.core import battery_profiles
                product = battery_profiles.get_product(product_name)
                if product:
                    meta.update({
                        "battery_profile": product.chemistry,
                        "battery_product": product_name,
                        "rated_capacity_basis": product.capacity_rating_basis,
                        "product_display_capacity_ah": product.product_display_capacity_ah or None,
                        "capacity_10h_ah": product.capacity_10h_ah or None,
                        "capacity_20h_ah": product.capacity_20h_ah or None,
                        "selected_reference_capacity_ah": battery.rated_capacity,
                        "selected_reference_rate_hr": product.peukert_hr or None,
                        "profile_version": "battery-profiles-v2",
                        "capacity_basis_version": "ytz6v-c10-c20-v1",
                    })
            except Exception as exc:
                logger.warning("Product rating metadata unavailable: %s", exc)
        if validation_campaign is not None:
            meta["validation_campaign"] = validation_campaign
        if extra:
            meta.update(extra)
        with open(csv_path + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Could not write session metadata for {csv_path}: {e}")


def finalize_session_metadata(csv_path: str, outcome: str = "completed",
                              reason: str = "", checkpoint: dict | None = None) -> None:
    """Record the immutable end-of-session evidence after the CSV is closed.

    A row trace by itself cannot distinguish a completed capacity test from an
    operator cancellation or a safety trip.  Keeping the terminal outcome in
    the sidecar follows the same audit principle as commercial cyclers: later
    analysis can show partial data, but must not present it as a completed test.
    This is best-effort and intentionally never prevents a hardware shutdown.
    """
    if not csv_path:
        return
    try:
        path = csv_path + ".meta.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            meta = {"schema_version": SESSION_SCHEMA_VERSION}
        meta.update({
            "status": outcome,
            "ended_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "end_reason": reason or outcome,
        })
        if checkpoint is not None:
            meta["checkpoint"] = dict(checkpoint)
        # Preserve the achieved timing by phase, beside the sequence's target
        # rate in ``protocol``.  A nominal 10 Hz setting is not evidence that
        # the instruments actually delivered 10 Hz during a pulse.
        meta["sampling_summary"] = _sampling_summary(csv_path)
        campaign = meta.get("validation_campaign")
        if isinstance(campaign, dict):
            temperatures = _csv_temperature_values(csv_path)
            try:
                from aset_batt.core.validation_campaign import ambient_summary
                meta["validation_evidence"] = {
                    **(meta.get("validation_evidence") or {}),
                    "ambient": ambient_summary(temperatures, campaign),
                    "sampling": meta["sampling_summary"],
                }
            except Exception as exc:
                logger.warning("Could not finalize validation evidence: %s", exc)
        if os.path.exists(csv_path):
            meta["sha256"] = DataHandler._hash_file(csv_path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Could not finalize session metadata for %s: %s", csv_path, e)


def _csv_temperature_values(csv_path: str) -> list[float]:
    values: list[float] = []
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    temp = float(row.get("Temperature_C", "nan"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(temp):
                    values.append(temp)
    except OSError:
        pass
    return values


def record_session_event(csv_path: str, name: str, payload: dict) -> None:
    """Append an auditable event to an active/finished session sidecar.

    The helper is intentionally best-effort: an event log must never delay a
    safety shutdown or telemetry loop.
    """
    if not csv_path:
        return
    path = csv_path + ".meta.json"
    try:
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return
    events = list(meta.get("events") or [])
    events.append({"name": str(name), "recorded_at": datetime.now().isoformat(timespec="seconds"),
                   "payload": dict(payload)})
    meta["events"] = events
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.warning("Could not record session event %s: %s", name, exc)


def update_session_metadata(csv_path: str, updates: dict) -> None:
    """Best-effort additive metadata update for measurements discovered mid-run."""
    if not csv_path:
        return
    path = csv_path + ".meta.json"
    try:
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
        meta.update(dict(updates))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2, ensure_ascii=False)
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("Could not update session metadata %s: %s", path, exc)


def checkpoint_session_metadata(csv_path: str, checkpoint: dict) -> None:
    """Atomically persist bounded recovery state; failures are acquisition faults."""
    path = csv_path + ".meta.json"
    tmp_path = None
    try:
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
        meta["checkpoint"] = dict(checkpoint)
        fd, tmp_path = tempfile.mkstemp(prefix=".session-meta-", suffix=".tmp",
                                        dir=os.path.dirname(os.path.abspath(path)))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception as exc:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise StorageError(f"session metadata checkpoint failed: {exc}") from exc


def _sampling_summary(csv_path: str) -> dict:
    """Return compact, per-phase timing evidence for a completed CSV.

    This intentionally uses only the standard library: it executes once after
    the file is closed, so it cannot disturb a time-critical acquisition loop.
    """
    # Timing compliance must be calculated from usable samples only.  Keeping
    # INVALID/GAP counts separately exposes an acquisition fault without
    # allowing malformed rows to make a phase appear better timed than it was.
    phases: dict[str, list[float]] = {}
    quality: dict[str, int] = {}
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                label = (row.get("Phase") or row.get("Mode") or "UNLABELLED").strip()
                try:
                    elapsed = float(row.get("Elapsed_s", "nan"))
                except (TypeError, ValueError):
                    continue
                q = (row.get("Sample_Quality") or "UNKNOWN").strip().upper()
                if math.isfinite(elapsed) and q == "VALID":
                    phases.setdefault(label, []).append(elapsed)
                quality[q] = quality.get(q, 0) + 1
    except OSError:
        return {}

    by_phase = {}
    for label, values in phases.items():
        values.sort()
        dt = [b - a for a, b in zip(values, values[1:]) if b >= a]
        if dt:
            dt.sort()
            by_phase[label] = {
                "samples": len(values),
                "median_dt_s": round(dt[len(dt) // 2], 4),
                "p95_dt_s": round(dt[min(len(dt) - 1, math.ceil(len(dt) * 0.95) - 1)], 4),
                "max_dt_s": round(dt[-1], 4),
                "dt_over_0p5s": sum(1 for value in dt if value > 0.5),
            }
        else:
            by_phase[label] = {"samples": len(values)}
    return {"by_phase": by_phase, "quality_counts": quality}

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


def _tail_csv_rows_incremental(csv_path: str, cache: dict, limit: int = 20000) -> list:
    """Like _tail_csv_rows but reuses *cache* (caller-owned, e.g. one dict per
    CloudPusher instance) across calls so only lines appended since the last
    call are read+parsed — the whole CSV is not re-opened and re-parsed from
    scratch every cycle. Cost of a repeat call is O(new rows), not O(total rows).

    Falls back to a full _tail_csv_rows() read (and repopulates the cache) on
    the first call for a path, if the file shrank (truncated/rotated — a new
    test session reusing the same path), or on any parse error.
    """
    # Binary-mode I/O throughout: os.path.getsize() and f.seek()/f.tell() must
    # agree on the same byte-offset units, which text-mode "utf-8-sig" doesn't
    # cleanly guarantee (BOM stripping makes tell()'s opaque offsets risky to
    # mix with getsize()). Decoding is done manually on each raw chunk instead.
    try:
        if cache.get("path") != csv_path:
            cache.clear()
            cache["path"] = csv_path
        size = os.path.getsize(csv_path)
        pos = cache.get("pos")
        if pos is None or size < pos:
            rows = _tail_csv_rows(csv_path, limit=limit)
            with open(csv_path, "rb") as f:
                header_line = f.readline().decode("utf-8-sig")
            cache["fieldnames"] = next(csv.reader([header_line])) if header_line.strip() else []
            cache["rows"] = rows
            cache["pos"] = size
            return cache["rows"]
        if size == pos:
            return cache.get("rows", [])
        with open(csv_path, "rb") as f:
            f.seek(pos)
            chunk = f.read()
        # Only consume complete lines — the writer may be mid-writerow() on the
        # tail of the file; hold back any trailing partial line for next time.
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return cache.get("rows", [])
        complete = chunk[:last_nl + 1]
        cache["pos"] = pos + len(complete)
        fieldnames = cache.get("fieldnames", [])
        rows = cache.setdefault("rows", [])
        for parsed in csv.reader(complete.decode("utf-8", errors="replace").splitlines()):
            if parsed:
                rows.append(dict(zip(fieldnames, parsed)))
        if len(rows) > limit:
            del rows[:len(rows) - limit]
        return rows
    except Exception as e:
        logger.warning("_tail_csv_rows_incremental failed, falling back to full read: %s", e)
        cache.clear()
        return _tail_csv_rows(csv_path, limit=limit)


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
    legacy_energy_wh = abs(avg_i * avg_v * elapsed / 3600.0) if avg_i and avg_v and elapsed else None
    energy_in_wh = energy_out_wh = 0.0
    energy_duration = 0.0
    times, powers, qualities = [], [], []
    for row in rows:
        try:
            tt = float(row.get("Elapsed_s", "nan"))
            vv = float(row.get("Voltage_V", "nan"))
            ii = float(row.get("Current_A", "nan"))
        except (ValueError, TypeError):
            continue
        times.append(tt); powers.append(vv * ii)
        qualities.append(str(row.get("Sample_Quality", "VALID")).upper())
    dts = [times[n] - times[n - 1] for n in range(1, len(times))
           if math.isfinite(times[n] - times[n - 1]) and times[n] > times[n - 1]]
    max_gap = 2.5 * (sorted(dts)[len(dts) // 2] if dts else float("inf"))
    for n in range(len(times) - 1):
        dt = times[n + 1] - times[n]
        if (dt <= 0 or dt > max_gap or "INVALID" in (qualities[n], qualities[n + 1])
                or "GAP" in (qualities[n], qualities[n + 1])
                or not math.isfinite(powers[n]) or not math.isfinite(powers[n + 1])):
            continue
        energy_out_wh += 0.5 * (max(0.0, powers[n]) + max(0.0, powers[n + 1])) * dt / 3600.0
        energy_in_wh += 0.5 * (max(0.0, -powers[n]) + max(0.0, -powers[n + 1])) * dt / 3600.0
        energy_duration += dt
    energy_wh = energy_in_wh + energy_out_wh if energy_duration > 0 else None

    return {
        "row_count": len(rows),
        "elapsed_s": elapsed,
        "avg_voltage_v": avg_v,
        "avg_current_a": avg_i,
        "capacity_ah": capacity_ah,
        "energy_wh": energy_wh,
        "energy_in_wh": energy_in_wh if energy_duration > 0 else None,
        "energy_out_wh": energy_out_wh if energy_duration > 0 else None,
        "energy_integration_method": "TRAPEZOIDAL_MEASURED_VI",
        "energy_legacy_average_product_wh": legacy_energy_wh,
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
    def __init__(self, throttle_redundant_rows: bool = True):
        self.is_recording = False
        self.csv_file = None
        self.csv_writer = None
        self.current_path: str = ""   # path ของ session ปัจจุบัน
        self._last_flush = 0.0        # perf_counter of the last disk flush — see log_row
        # Redundant-row throttle state — see log_row. Reset in start_logging so a
        # new session's first row always writes.
        self._last_row_vals = None
        self._last_row_elapsed = -1e9
        self._throttle_redundant_rows = throttle_redundant_rows
        self.session_id: str = ""
        self.test_type: str = ""
        self._step_index = 0
        self.last_valid_timestamp = ""
        self.last_phase = ""
        self.last_flush_timestamp = ""
        self.last_elapsed_s: float | None = None
        self._last_checkpoint = 0.0
        self._clock = time.perf_counter  # private deterministic test seam
        self.flush_count = 0

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
        # The timestamp remains parseable for old session views; the short token
        # prevents a second test launched in the same second from appending into
        # another session's CSV with a different schema/provenance.
        token = uuid.uuid4().hex[:8]
        return os.path.join(sessions_dir, f"{prefix}{ts}_{token}.csv")

    def start_logging(self, filepath: str, test_type: str = ""):
        """เริ่มบันทึก CSV — คืน (True, "") หรือ (False, error_message)"""
        try:
            self.flush_count = 0
            self.csv_file = open(filepath, 'a', newline='', encoding='utf-8-sig')
            self.csv_writer = csv.writer(self.csv_file)
            # เขียน header เฉพาะเมื่อไฟล์ใหม่ (ขนาด 0)
            if os.path.getsize(filepath) == 0:
                self.csv_writer.writerow(SESSION_COLUMNS)
                self.csv_file.flush()  # FIX: Prevent 0-byte file on early crash
                self.flush_count += 1
            self.current_path = filepath
            self.is_recording = True
            self._last_row_vals = None       # new session — first row always writes
            self._last_row_elapsed = -1e9
            self._step_index = 0
            self.last_valid_timestamp = ""
            self.last_phase = ""
            self.last_elapsed_s = None
            self.last_flush_timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            self._last_checkpoint = self._clock()
            self._last_flush = self._last_checkpoint
            self.session_id = uuid.uuid4().hex
            self.test_type = test_type
            return True, "Success"
        except Exception as e:
            if self.csv_file is not None:
                try:
                    self.csv_file.close()
                except Exception:
                    pass
            self.csv_file = None
            self.csv_writer = None
            self.is_recording = False
            return False, str(e)

    def flush(self) -> None:
        """Flush buffered rows without ending the session.

        Sequence analysis runs before the UI closes the session.  This makes
        the final pulse/cut-off samples visible to the analysis rather than
        depending on the one-second periodic flush cadence.
        """
        if self.csv_file:
            try:
                self.csv_file.flush()
                self.flush_count += 1
                self._last_flush = self._clock()
                self.last_flush_timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            except Exception as exc:
                raise StorageError(f"CSV flush failed: {exc}") from exc

    def stop_logging(self, outcome: str = "completed", reason: str = ""):
        """Close the CSV and mark its terminal outcome in the sidecar metadata."""
        was_recording = self.is_recording
        self.is_recording = False
        if self.csv_file:
            try:
                self.csv_file.close()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
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
        # Do not create a metadata file for a handler that never started.  A
        # normal completed/aborted session has already received its start
        # snapshot before this point.
        if was_recording and self.current_path:
            finalize_session_metadata(self.current_path, outcome, reason, {
                "session_id": self.session_id,
                "status": outcome,
                "current_phase": self.last_phase,
                "last_valid_data_timestamp": self.last_valid_timestamp,
                "elapsed_engineering_s": self.last_elapsed_s,
                "rows_written": self._step_index,
                "last_successful_flush_timestamp": self.last_flush_timestamp,
                "test_type": self.test_type,
            })

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
                rin_calibrated: bool = True, mode: str = "",
                capacity_ah: Optional[float] = None, phase: str = "",
                voltage_source: str = "unknown", current_source: str = "unknown",
                sample_quality: str = "VALID", sample_note: str = "",
                expected_dt_s: Optional[float] = None,
                temperature_status: str = "NOT_AVAILABLE",
                temperature_age_s: Optional[float] = None,
                temperature_source: str = "unknown"):
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
            mode           : Backward-compatible phase label (e.g. "MAIN_DISCHARGE").
            capacity_ah    : Cumulative signed-out capacity when measured by a worker.
            phase/source/quality: Per-row provenance required for later validation.
            expected_dt_s  : If supplied, a late sample is kept but marked GAP.
        """
        if not self.is_recording or not self.csv_writer or not self.csv_file:
            raise StorageError("CSV session is not open for writing")
        if self.is_recording and self.csv_writer:
            try:
                phase = phase or mode
                mode = mode or phase
                # Do not silently promote a missing phase or an impossible
                # instrument reading into valid experimental evidence.  The row
                # is retained for audit/replay, but downstream grading can see
                # that it must not be used as a measurement anchor.
                invalid_notes = []
                if not str(phase or "").strip():
                    invalid_notes.append("missing_phase")
                if not math.isfinite(v) or v <= 0.0:
                    invalid_notes.append("invalid_voltage")
                if not math.isfinite(i_net):
                    invalid_notes.append("invalid_current")
                if invalid_notes:
                    sample_quality = "INVALID"
                    sample_note = (sample_note + "; " if sample_note else "") + "; ".join(invalid_notes)
                if self._last_row_elapsed > -1e8 and elapsed_s <= self._last_row_elapsed:
                    sample_quality = "INVALID"
                    sample_note = (sample_note + "; " if sample_note else "") + "nonpositive_elapsed_interval"
                if (expected_dt_s and self._last_row_elapsed > -1e8
                        and elapsed_s - self._last_row_elapsed > expected_dt_s * 2.5):
                    gap = elapsed_s - self._last_row_elapsed
                    if sample_quality == "VALID":
                        sample_quality = "GAP"
                    sample_note = (sample_note + "; " if sample_note else "") + \
                        f"sample_gap_s={gap:.3f}"
                capacity_text = "" if capacity_ah is None or not math.isfinite(capacity_ah) \
                    else f"{capacity_ah:.5f}"
                # Redundant-row throttle: the monitor loop polls at ~10 Hz but the
                # instruments update slower, so long steady phases (a 4 h charge)
                # produced thousands of rows whose every measured value was identical
                # to the previous row (a real 4.8 h session: 3,542 of them) — zero
                # information, real disk/OneDrive churn. Skip a row ONLY when all
                # measured values match the previous row AND it's been under 0.25 s
                # since the last write. 0.25 s (not 1 s) is a hard ceiling from
                # identify_dcir's _DCIR_MAX_STEP_DT=0.5 s staleness gate: the recorded
                # gap from the last pre-edge row to the first post-edge row is
                # throttle-interval + one real sample period (~0.2 s), and 0.25+0.2
                # stays inside the gate where 1.0 s would get every real current-step
                # edge dropped as stale. A row with ANY changed value always writes
                # immediately, so edges themselves are never delayed.
                row_vals = (f"{v:.4f}", f"{i_net:.4f}", f"{soc:.2f}",
                            f"{resistance_mohm:.2f}", f"{temp_c:.2f}",
                            "1" if rin_calibrated else "0", capacity_text, mode,
                            phase, voltage_source, current_source, sample_quality,
                            sample_note, temperature_status,
                            "" if temperature_age_s is None else f"{temperature_age_s:.3f}",
                            temperature_source)
                if (self._throttle_redundant_rows and row_vals == self._last_row_vals
                        and elapsed_s - self._last_row_elapsed < 0.25):
                    return
                self._last_row_vals = row_vals
                self._last_row_elapsed = elapsed_s
                self._step_index += 1
                now_wall = datetime.now().astimezone()
                timestamp_iso = now_wall.isoformat(timespec="milliseconds")
                self.csv_writer.writerow([
                    # Full date, not just HH:MM:SS — a 4-5 h session crossing
                    # midnight otherwise wraps 23:59→00:00 with nothing to
                    # disambiguate the day during a post-hoc audit.
                    now_wall.strftime("%Y-%m-%d %H:%M:%S"),
                    timestamp_iso,
                    # 1 ms resolution, not 0.1 s: at ~10 Hz the old %.1f quantisation
                    # gave thousands of duplicate timestamps per session (5,988 in a
                    # real file), corrupting every dt-based consumer (identify_dcir's
                    # staleness gate, ECM fit time axis, replay dt=0 divisions).
                    f"{elapsed_s:.3f}",
                    f"{v:.4f}", f"{i_net:.4f}", f"{soc:.2f}",
                    f"{resistance_mohm:.2f}", f"{temp_c:.2f}",
                    "1" if rin_calibrated else "0", capacity_text, mode,
                    SESSION_SCHEMA_VERSION, self.session_id, self.test_type,
                    phase, str(self._step_index), voltage_source,
                    current_source, sample_quality, sample_note,
                    temperature_status,
                    "" if temperature_age_s is None else f"{temperature_age_s:.3f}",
                    temperature_source,
                ])
                self.last_valid_timestamp = timestamp_iso if sample_quality == "VALID" else self.last_valid_timestamp
                self.last_phase = phase
                self.last_elapsed_s = elapsed_s
                # flush() forces a real disk write (or, on this repo's OneDrive-synced
                # project folder, a sync-agent wakeup) every call — at the monitor
                # loop's ~10 Hz during CHARGE that's 10 forced writes/sec, a plausible
                # source of periodic stutters. Throttled to ~1/s: worst case loses <1s
                # of rows on a hard crash, which cloud push (5s interval, see
                # cloud_push_interval) already tolerates just as well.
                import time
                now = self._clock()
                if now - self._last_flush >= 1.0 - 1e-9:
                    try:
                        self.csv_file.flush()
                        self.flush_count += 1
                        self._last_flush = now
                        self.last_flush_timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
                    except Exception as exc:
                        raise StorageError(f"CSV flush failed: {exc}") from exc
                try:
                    if now - self._last_checkpoint >= 30.0 - 1e-9:
                        checkpoint_session_metadata(self.current_path, {
                            "session_id": self.session_id,
                            "status": "running",
                            "current_phase": self.last_phase,
                            "last_valid_data_timestamp": self.last_valid_timestamp,
                            "elapsed_engineering_s": elapsed_s,
                            "rows_written": self._step_index,
                            "last_successful_flush_timestamp": self.last_flush_timestamp,
                            "test_type": self.test_type,
                        })
                        self._last_checkpoint = now
                except Exception as exc:
                    raise StorageError(f"CSV durability/checkpoint failed: {exc}") from exc
            except Exception as e:
                logger.error(f"CSV write error: {e}")
                if isinstance(e, StorageError):
                    raise
                raise StorageError(f"CSV write failed: {e}") from e

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
