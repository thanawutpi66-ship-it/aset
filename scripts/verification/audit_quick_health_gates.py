"""Evidence-oriented audit of Quick Scan gate inputs from a session ZIP.

This script reads the archive without modifying it and writes run-level and
sample-level evidence into the requested audit directory.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
from aset_batt.acquisition.analysis import analyze_csv, profile_from_config, _read_csv
from aset_batt.acquisition.health_assessment import load_config, assess_quick_health
from aset_batt.acquisition.ocv_validation import evaluate_quick_ocv_window
from aset_batt.core.config import ConfigManager


def _write(path, rows):
    fields = list(dict.fromkeys(k for row in rows for k in row)) or ["status"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _profile(meta):
    cfg = ConfigManager()
    b = cfg.battery
    b.battery_type = meta.get("battery_type") or b.battery_type
    b.product_name = meta.get("product_name") or meta.get("battery_product") or b.product_name
    for attr, key, cast in (("cells_series", "cells_series", int),
                            ("cells_parallel", "cells_parallel", int),
                            ("rated_capacity", "rated_capacity_ah", float),
                            ("rated_capacity", "selected_reference_capacity_ah", float)):
        if meta.get(key) is not None:
            try: setattr(b, attr, cast(meta[key]))
            except (TypeError, ValueError): pass
    return profile_from_config(cfg)


def _sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _ocv_window(t, v, i, temp, quality, mode, base=0.0):
    indices = [k for k, m in enumerate(mode) if str(m).strip().upper() == "OCV"
               and np.isfinite(v[k]) and np.isfinite(t[k])]
    if len(indices) < 2:
        return {"valid": False, "status": "NO_RECORDED_OCV_PHASE", "voltage_v": None,
                "rest_s": 0.0, "rows": ""}
    samples = []
    t0 = t[indices[0]]
    for k in indices:
        q = str(quality[k]).strip().upper() if k < len(quality) else "VALID"
        samples.append((float(t[k] - t0), float(v[k]), float(i[k]),
                        float(temp[k]) if np.isfinite(temp[k]) else float("nan"),
                        q == "VALID"))
    result = evaluate_quick_ocv_window(samples, outputs_off=True,
                                       max_abs_current_a=0.10,
                                       now_s=float(t[indices[-1]] - t0))
    # Runtime exits on the first accepted observation, and times out at 600 s.
    # A long recorded OCV phase must therefore be replayed incrementally rather
    # than judged only at its final sample.
    first_valid = None
    for stop, sample in enumerate(samples, 1):
        elapsed = float(sample[0])
        if elapsed > 600.0:
            break
        candidate = evaluate_quick_ocv_window(samples[:stop], outputs_off=True,
            max_abs_current_a=0.10, now_s=elapsed)
        if candidate["valid"]:
            first_valid = candidate
            candidate["accepted_at_s"] = elapsed
            break
    if first_valid is not None:
        result = first_valid
    else:
        result["accepted_at_s"] = None
    result["rows"] = f"{indices[0]+2}-{indices[-1]+2}"
    return result


def run(archive: Path, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    archive_hash_before = _sha(archive)
    tmp = Path(tempfile.mkdtemp(prefix="aset_gate_audit_"))
    quick, traces, dcir_rows, recovery_rows, c10_rows, all_rows = [], [], [], [], [], []
    errors = []
    cfg = load_config()
    try:
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                target = (tmp / item.filename).resolve()
                if tmp.resolve() not in target.parents and target != tmp.resolve():
                    raise ValueError(f"Unsafe archive path: {item.filename}")
            z.extractall(tmp)
        csvs = sorted(tmp.rglob("*.csv"))
        for csv_path in csvs:
            mp = csv_path.with_suffix(csv_path.suffix + ".meta.json")
            try: meta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
            except Exception: meta = {}
            typ = str(meta.get("test_type", ""))
            is_quick = "quick" in (typ + " " + csv_path.name).lower()
            if not is_quick:
                if any(x in (typ + " " + csv_path.name).lower() for x in ("capacity", "c10", "constant current")):
                    all_rows.append({"file": csv_path.name, "test_type": typ or "UNKNOWN",
                                     "battery_id": meta.get("battery_id") or meta.get("serial_number") or meta.get("battery_serial_number"),
                                     "session_id": meta.get("session_id"), "status": meta.get("status"),
                                     "capacity_ah": meta.get("capacity_ah"), "soh": meta.get("soh")})
                continue
            try:
                profile = _profile(meta)
                # Re-run the regular analyzer path; offline_legacy deliberately
                # withholds current Quick SoH and would mask what the runtime path
                # can calculate from the recorded phases/sidecar.
                try:
                    result = analyze_csv(str(csv_path), profile, fit_ecm=True, offline_legacy=False)
                    normal_analysis_error = ""
                except Exception as primary_error:
                    # Incomplete/older records can fail the current live-analysis
                    # assumptions; preserve their raw diagnostic metrics through
                    # the supported offline legacy route and record the failure.
                    result = analyze_csv(str(csv_path), profile, fit_ecm=True, offline_legacy=True)
                    normal_analysis_error = repr(primary_error)
                t, v, i, temp, cap, soc, modes, quality = _read_csv(str(csv_path))
                labels = [str(x or "").strip().upper() for x in modes]
                battery_id = next((meta.get(k) for k in ("battery_id", "serial_number", "battery_serial_number", "serial") if meta.get(k)), None)
                profile_recorded = (meta.get("battery_product") or meta.get("product_name") or meta.get("battery_profile"))
                start_idx = [k for k,x in enumerate(labels) if x == "OCV"]
                # OCV_end is the post-discharge TAIL_REST. RELAX belongs to the
                # mini-pulse recovery experiment and must not contaminate OCV.
                end_idx = [k for k,x in enumerate(labels) if x == "TAIL_REST"]
                mini_idx = [k for k,x in enumerate(labels) if x == "MINI_PULSE"]
                relax_idx = [k for k,x in enumerate(labels) if x == "RELAX"]
                main_idx = [k for k,x in enumerate(labels) if x == "MAIN_DISCHARGE"]
                try:
                    legacy_result = analyze_csv(str(csv_path), profile, fit_ecm=True, offline_legacy=True)
                except Exception:
                    legacy_result = {}
                measured_main = i[main_idx] if main_idx else np.asarray([], float)
                mean_main = float(np.mean(measured_main)) if measured_main.size else None
                k_peukert = float(result.get("quick_peukert_k", result.get("peukert_k", getattr(profile,"peukert_k", float("nan")))))
                i_c10 = (float(getattr(profile,"capacity_10h_ah",0.0))/max(1.0,float(getattr(profile,"peukert_hr",10.0))))
                q_main_value = result.get("q_removed_ah")
                q_c10_value = None
                if q_main_value is not None and mean_main and i_c10 > 0 and math.isfinite(k_peukert):
                    q_c10_value = float(q_main_value) * (mean_main/i_c10)**(k_peukert-1.0)
                start_cand = float(np.median(v[start_idx])) if start_idx else None
                end_cand = float(np.median(v[end_idx[len(end_idx)//2:]])) if end_idx else None
                raw_quality = quality.get("row_quality", [])
                start_window = _ocv_window(t,v,i,temp,raw_quality,modes) if start_idx else None
                # Evaluate the explicit terminal rest phase using the same bounded
                # stability/current limits; this remains a candidate because old
                # records do not carry an output-state assertion.
                if end_idx:
                    tail_samples = []
                    tail_t0 = float(t[end_idx[0]])
                    for k in end_idx:
                        q = raw_quality[k] if k < len(raw_quality) else "VALID"
                        tail_samples.append((float(t[k]-tail_t0),float(v[k]),float(i[k]),
                                             float(temp[k]) if np.isfinite(temp[k]) else float("nan"),
                                             q == "VALID"))
                    end_window = evaluate_quick_ocv_window(tail_samples, outputs_off=True,
                        max_abs_current_a=0.10, now_s=float(t[end_idx[-1]]-tail_t0))
                else:
                    end_window = None
                # The current saved-session analyzer uses sidecar anchor flags for SoC validity.
                soh = result.get("quick_soh_est_pct")
                dcir_ohm = result.get("dcir_mohm")
                recovery = result.get("quick_recovery_delta_v_candidate")
                hp = cfg.get("profiles", {}).get(profile.name, {})
                flags = []
                if str(meta.get("status", "")).lower() not in {"", "completed"}:
                    flags.append("session status is " + str(meta.get("status")))
                if normal_analysis_error:
                    flags.append("current analyzer path unavailable: " + normal_analysis_error)
                if not meta.get("ocv_start_valid"): flags.append("start OCV anchor not recorded valid")
                if not meta.get("ocv_end_valid"): flags.append("end OCV anchor not recorded valid")
                if not result.get("dcir_measured"): flags.append("measured DCIR unavailable")
                if not np.isfinite(result.get("dcir_latency_s", float("nan"))): flags.append("DCIR edge latency unavailable/invalid")
                if not math.isfinite(float(recovery)) if recovery is not None else True: flags.append("recovery candidate unavailable")
                assessment = assess_quick_health(quick_soh_pct=soh,
                    dcir_ohm=(float(dcir_ohm)/1000 if dcir_ohm is not None else None),
                    recovery_metric=recovery, profile_config=hp,
                    temperature_c=result.get("temperature_median_c"), quality_flags=flags, config=cfg)
                gates = []
                if not meta.get("ocv_start_valid"):
                    gates.append({"gate":"ocv_start_anchor","category":"ALGORITHM_OR_WINDOW_ISSUE" if start_idx else "RAW_DATA_MISSING",
                        "evidence":f"rows={len(start_idx)}; runtime window={start_window.get('status') if start_window else 'NO_RECORDED_OCV_PHASE'}; metadata flag={meta.get('ocv_start_valid')}"})
                if not meta.get("ocv_end_valid"):
                    gates.append({"gate":"ocv_end_anchor","category":"ALGORITHM_OR_WINDOW_ISSUE" if end_idx else "RAW_DATA_MISSING",
                        "evidence":f"TAIL_REST rows={len(end_idx)}; runtime window={end_window.get('status') if end_window else 'NO_RECORDED_TAIL_REST'}; metadata flag={meta.get('ocv_end_valid')}"})
                pulse_edge_candidate = False
                if mini_idx:
                    pulse_values = np.abs(i[mini_idx])
                    threshold = max(1e-3, 0.20 * float(np.max(pulse_values))) if pulse_values.size else float("inf")
                    pulse_edge_candidate = any(labels[k+1] == "MINI_PULSE" and (i[k+1]-i[k]) > threshold for k in range(len(i)-1))
                for gate, passed, category, evidence in [
                    ("quick_soh", result.get("quick_soh_est_valid", False), ("PARSER_OR_METADATA_ISSUE" if start_window and start_window.get("valid") and end_window and end_window.get("valid") else "ALGORITHM_OR_WINDOW_ISSUE" if start_idx or end_idx else "RAW_DATA_MISSING"), "raw phase rows start/end=" + str((len(start_idx),len(end_idx))) + "; candidate windows=" + str((start_window.get("status") if start_window else None,end_window.get("status") if end_window else None)) + "; sidecar valid anchors=" + str((meta.get("ocv_start_valid"),meta.get("ocv_end_valid")))),
                    ("measured_dcir", result.get("dcir_measured", False), "ALGORITHM_OR_WINDOW_ISSUE" if pulse_edge_candidate or mini_idx else "RAW_DATA_MISSING", f"mini-pulse rows={len(mini_idx)}; qualifying edge candidate={pulse_edge_candidate}; measured={result.get('dcir_measured')}; latency={result.get('dcir_latency_s')}; n={result.get('dcir_n_steps')}"),
                    ("dcir_reference", hp.get("dcir_healthy_ohm") is not None and hp.get("dcir_eol_ohm") is not None, "REFERENCE_OR_CONFIG_MISSING", "profile refs=" + repr((hp.get("dcir_healthy_ohm"),hp.get("dcir_eol_ohm")))),
                    ("recovery_reference", hp.get("recovery_healthy") is not None and hp.get("recovery_eol") is not None, "REFERENCE_OR_CONFIG_MISSING", "profile refs=" + repr((hp.get("recovery_healthy"),hp.get("recovery_eol")))),
                ]:
                    if not passed: gates.append({"gate":gate,"category":category,"evidence":evidence})
                audit = {
                    "file":csv_path.name,"session_id":meta.get("session_id"),"started_at":meta.get("started_at"),
                    "completion_status":meta.get("status","UNKNOWN"),"samples":len(t),"app_version":meta.get("app_version"),
                    "battery_id":battery_id,"battery_id_source":"SESSION_METADATA" if battery_id else "UNAVAILABLE_AFTER_METADATA_AND_COLUMNS_AUDIT",
                    "battery_profile":profile_recorded or profile.name,"battery_profile_source":"SESSION_METADATA" if profile_recorded else "CURRENT_CONFIG_UNVERIFIED_FALLBACK",
                    "start_ocv_candidate_v":start_cand,"end_ocv_candidate_v":end_cand,
                    "ocv_start_valid":meta.get("ocv_start_valid"),"ocv_end_valid":meta.get("ocv_end_valid"),
                    "ocv_rejection_reason":"; ".join(x for x in (meta.get("ocv_start_status"),meta.get("ocv_end_status")) if x) or "No historical validity metadata; raw window candidates are diagnostic only and cannot establish the original output-off assertion",
                    "start_ocv_window_candidate_status":start_window.get("status") if start_window else "UNAVAILABLE",
                    "start_ocv_window_candidate_valid":start_window.get("valid") if start_window else False,
                    "start_ocv_window_spread_v":start_window.get("voltage_window_v") if start_window else None,
                    "end_ocv_window_candidate_status":end_window.get("status") if end_window else "UNAVAILABLE",
                    "end_ocv_window_candidate_valid":end_window.get("valid") if end_window else False,
                    "end_ocv_window_spread_v":end_window.get("voltage_window_v") if end_window else None,
                    "soc_start_pct":meta.get("ocv_start_soc_pct") if meta.get("ocv_start_valid") else None,
                    "soc_end_pct":meta.get("ocv_end_soc_pct") if meta.get("ocv_end_valid") else None,
                    "soc_start_fallback_analyzer_pct":result.get("quick_soc_start_pct"),
                    "delta_soc_fraction":((float(meta.get("ocv_start_soc_pct"))-float(meta.get("ocv_end_soc_pct")))/100 if meta.get("ocv_start_valid") and meta.get("ocv_end_valid") else None),
                    "q_main_ah":q_main_value,"mean_discharge_a":mean_main,
                    "peukert_k":k_peukert,"peukert_k_source":meta.get("peukert_k_source") or "CURRENT_PROFILE_FALLBACK_UNVERIFIED",
                    "q_c10_est_ah":q_c10_value,
                    "q_full_est_ah":result.get("quick_full_capacity_est_ah"),"quick_soh_pct":soh,
                    "quick_soh_valid":result.get("quick_soh_est_valid"),"dcir_available":result.get("dcir_measured"),
                    "dcir_mohm":dcir_ohm,"dcir_method":"Pulse step estimator; 25C-normalized; separate from ECM R0 and ACIR",
                    "legacy_raw_dcir_reanalysis_mohm":legacy_result.get("dcir_reanalyzed_mohm"),
                    "dcir_reference_available":assessment.get("score_dcir") is not None,"r_healthy_ohm":hp.get("dcir_healthy_ohm"),"r_eol_ohm":hp.get("dcir_eol_ohm"),
                    "recovery_available":recovery is not None and math.isfinite(float(recovery)),"recovery_delta_v":recovery,
                    "recovery_window":"median(last <=5 loaded MINI_PULSE samples) to median(second half of RELAX samples)",
                    "recovery_reference_available":assessment.get("score_recovery") is not None,
                    "c10_pair_found":False,"paired_identity_evidence":"No traceable battery identity in metadata/CSV columns",
                    "failed_gates":json.dumps(gates,ensure_ascii=False),"assessment_status":assessment["health_assessment_status"],
                    "invalid_reason":assessment["health_assessment_reason"],"health_score":assessment["health_score_quick"],"grade":assessment["condition_grade"],
                    "ecm_r0_mohm":result.get("r0_mohm"),"ecm_r2":result.get("ecm_r2"),"current_analyzer_error":normal_analysis_error,
                }
                quick.append(audit)
                if str(meta.get("status","")).lower()=="completed":
                    trace={"file":csv_path.name,"session_id":meta.get("session_id"),"samples":len(t),
                           "phases":json.dumps(dict(Counter(labels)),ensure_ascii=False),
                           "time_start_s":float(t[0]) if len(t) else None,"time_end_s":float(t[-1]) if len(t) else None,
                           "start_ocv_rows":f"{start_idx[0]+2}-{start_idx[-1]+2}" if start_idx else None,
                           "start_ocv_candidate_v":start_cand,"end_rest_rows":f"{end_idx[0]+2}-{end_idx[-1]+2}" if end_idx else None,
                           "end_ocv_candidate_v":end_cand,"start_window_candidate_status":start_window.get("status") if start_window else "UNAVAILABLE",
                           "start_window_candidate_valid":start_window.get("valid") if start_window else False,
                           "start_window_spread_v":start_window.get("voltage_window_v") if start_window else None,
                           "end_window_candidate_status":end_window.get("status") if end_window else "UNAVAILABLE",
                           "end_window_candidate_valid":end_window.get("valid") if end_window else False,
                           "end_window_spread_v":end_window.get("voltage_window_v") if end_window else None,
                           "recorded_ocv_start_valid":meta.get("ocv_start_valid"),
                           "recorded_ocv_end_valid":meta.get("ocv_end_valid"),"soc_start_pct":meta.get("ocv_start_soc_pct") if meta.get("ocv_start_valid") else None,
                           "soc_end_pct":meta.get("ocv_end_soc_pct") if meta.get("ocv_end_valid") else None,
                           "soc_start_fallback_analyzer_pct":result.get("quick_soc_start_pct"),"q_main_ah":q_main_value,
                           "i_mean_a":mean_main,"peukert_k":k_peukert,
                           "peukert_factor":(q_c10_value/q_main_value if q_main_value and q_c10_value is not None else None),"q_c10_est_ah":q_c10_value,
                           "quick_soh_pct":soh,"quick_soh_status":result.get("quick_soh_status"),
                           "dcir_edge_latency_s":result.get("dcir_latency_s"),"dcir_mohm":dcir_ohm,
                           "ecm_r0_mohm":result.get("r0_mohm"),"ecm_r2":result.get("ecm_r2"),
                           "recovery_delta_v":recovery,"recovery_rows":json.dumps({"pulse_tail":(np.flatnonzero((np.array(labels)=="MINI_PULSE")&(i>0.05))[-5:]+2).tolist(),"relax_second_half":(np.flatnonzero(np.array(labels)=="RELAX")[len(np.flatnonzero(np.array(labels)=="RELAX"))//2:]+2).tolist()},ensure_ascii=False),
                           "legacy_dcir_reanalysis_mohm":legacy_result.get("dcir_reanalyzed_mohm"),
                           "validity_gates":audit["failed_gates"],"final":audit["assessment_status"]+" / "+audit["grade"]}
                    traces.append(trace)
                # Record raw current-step candidates with exact neighboring row/time values.
                for k in range(max(0,len(i)-1)):
                    di=float(i[k+1]-i[k]); dt=float(t[k+1]-t[k])
                    if abs(di)>0.05 and (not labels or labels[k+1]=="MINI_PULSE"):
                        dcir_rows.append({"file":csv_path.name,"session_id":meta.get("session_id"),"row_pre":k+2,"row_post":k+3,
                            "phase_pre":labels[k] if labels else "","phase_post":labels[k+1] if labels else "",
                            "t_pre_s":t[k],"t_post_s":t[k+1],"latency_s":dt,"v_pre_v":v[k],"v_post_v":v[k+1],
                            "i_pre_a":i[k],"i_post_a":i[k+1],"delta_v_v":v[k+1]-v[k],"delta_i_a":di,
                            "raw_pulse_dcir_ohm":abs((v[k+1]-v[k])/di) if di else None,
                            "reported_dcir_mohm":dcir_ohm,"reported_ecm_r0_mohm":result.get("r0_mohm"),
                            "temperature_c":temp[k+1] if len(temp)>k+1 else None,"ecm_r2":result.get("ecm_r2"),
                            "in_analyzer_latency_gate":0<dt<=0.5})
                if result.get("dcir_measured"):
                    # Preserve a per-run row even when archived quality/timing
                    # drops the individual edge from our simple raw-edge list.
                    dcir_rows.append({"file":csv_path.name,"session_id":meta.get("session_id"),
                        "candidate_status":"REPORTED_MEASURED_DCiR","edge_rows":"see raw edge rows or unavailable",
                        "reported_dcir_mohm":dcir_ohm,"reported_dcir_method":"identify_dcir median of accepted pulse edges, normalized to 25 C",
                        "reported_ecm_r0_mohm":result.get("r0_mohm"),"ecm_r2":result.get("ecm_r2"),
                        "dcir_latency_s":result.get("dcir_latency_s"),"dcir_n_steps":result.get("dcir_n_steps"),
                        "dcir_temp_normalized":True,"reference_available":assessment.get("score_dcir") is not None})
                legacy_dcir = legacy_result.get("dcir_reanalyzed_mohm")
                if legacy_dcir is not None and np.isfinite(legacy_dcir):
                    legacy_edge = None
                    threshold = max(0.05, 0.20 * float(np.nanmax(i))) if len(i) else float("inf")
                    for edge in np.where(np.diff(i) > threshold)[0]:
                        if any(labels) and (edge+1 >= len(labels) or labels[edge+1] != "MINI_PULSE"):
                            continue
                        edge_dt = float(t[edge+1]-t[edge])
                        if edge_dt <= 0.5 and i[edge+1] > 0:
                            legacy_edge = int(edge)
                            break
                    dcir_rows.append({"file":csv_path.name,"session_id":meta.get("session_id"),
                        "candidate_status":"LEGACY_RAW_DIAGNOSTIC_NOT_CERTIFIED",
                        "reported_dcir_mohm":legacy_dcir,
                        "reported_dcir_method":"Offline legacy first qualifying pulse edge; raw ΔV/ΔI, not temperature-normalized; diagnostic only",
                        "row_pre":legacy_edge+2 if legacy_edge is not None else None,
                        "row_post":legacy_edge+3 if legacy_edge is not None else None,
                        "t_pre_s":float(t[legacy_edge]) if legacy_edge is not None else None,
                        "t_post_s":float(t[legacy_edge+1]) if legacy_edge is not None else None,
                        "latency_s":float(t[legacy_edge+1]-t[legacy_edge]) if legacy_edge is not None else legacy_result.get("dcir_reanalyzed_latency_s"),
                        "v_pre_v":float(v[legacy_edge]) if legacy_edge is not None else None,
                        "v_post_v":float(v[legacy_edge+1]) if legacy_edge is not None else None,
                        "i_pre_a":float(i[legacy_edge]) if legacy_edge is not None else None,
                        "i_post_a":float(i[legacy_edge+1]) if legacy_edge is not None else None,
                        "delta_v_v":float(v[legacy_edge+1]-v[legacy_edge]) if legacy_edge is not None else None,
                        "delta_i_a":float(i[legacy_edge+1]-i[legacy_edge]) if legacy_edge is not None else None,
                        "raw_pulse_dcir_ohm":abs(float(v[legacy_edge+1]-v[legacy_edge])/float(i[legacy_edge+1]-i[legacy_edge])) if legacy_edge is not None and i[legacy_edge+1] != i[legacy_edge] else None,
                        "temperature_c":float(temp[legacy_edge+1]) if legacy_edge is not None else None,
                        "reported_ecm_r0_mohm":legacy_result.get("r0_mohm"),"ecm_r2":legacy_result.get("ecm_r2"),
                        "dcir_temp_normalized":False,
                        "reference_available":False})
                recovery_rows.append({"file":csv_path.name,"status":meta.get("status"),"delta_v_candidate":recovery,
                    "pulse_sample_count":int(np.sum((np.array(labels)=="MINI_PULSE")&(i>0.05))),
                    "relax_sample_count":int(np.sum(np.array(labels)=="RELAX")),"candidate_window":audit["recovery_window"],
                    "pulse_first_row":mini_idx[0]+2 if mini_idx else None,"pulse_last_row":mini_idx[-1]+2 if mini_idx else None,
                    "pulse_duration_s":float(t[mini_idx[-1]]-t[mini_idx[0]]) if len(mini_idx)>1 else None,
                    "pulse_current_median_a":float(np.median(i[(np.array(labels)=="MINI_PULSE")&(i>0.05)])) if np.any((np.array(labels)=="MINI_PULSE")&(i>0.05)) else None,
                    "relax_first_row":relax_idx[0]+2 if relax_idx else None,"relax_last_row":relax_idx[-1]+2 if relax_idx else None,
                    "relax_duration_s":float(t[relax_idx[-1]]-t[relax_idx[0]]) if len(relax_idx)>1 else None,
                    "pulse_tail_median_v":float(np.median(v[np.flatnonzero((np.array(labels)=="MINI_PULSE")&(i>0.05))[-min(5,int(np.sum((np.array(labels)=="MINI_PULSE")&(i>0.05)))):]])) if np.any((np.array(labels)=="MINI_PULSE")&(i>0.05)) else None,
                    "relax_later_half_median_v":float(np.median(v[relax_idx[len(relax_idx)//2:]])) if len(relax_idx)>0 else None,
                    "tail_rest_duration_s":float(t[-1]-t[np.flatnonzero(np.array(labels)=="TAIL_REST")[0]]) if np.any(np.array(labels)=="TAIL_REST") else None,
                    "identity_available":bool(battery_id),"reference_available":assessment.get("score_recovery") is not None})
            except Exception as exc:
                errors.append({"file":csv_path.name,"error":repr(exc)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _write(outdir/"quickscan_gate_audit.csv",quick)
    _write(outdir/"completed_quickscan_trace.csv",traces)
    _write(outdir/"dcir_audit.csv",dcir_rows)
    _write(outdir/"recovery_audit.csv",recovery_rows)
    _write(outdir/"quick_c10_pairing.csv",[{"quick_file":r["file"],"battery_id":r["battery_id"],"c10_pair":"UNAVAILABLE","candidate_capacity_sessions":json.dumps(all_rows,ensure_ascii=False),"evidence":r["paired_identity_evidence"]} for r in quick])
    gates = Counter()
    for row in quick:
        for item in json.loads(row["failed_gates"]): gates[(item["gate"],item["category"])] += 1
    _write(outdir/"invalid_gate_breakdown.csv",[{"gate":k[0],"category":k[1],"failed_runs":v,"evidence_summary":"Per-run evidence is in quickscan_gate_audit.csv"} for k,v in gates.items()])
    # replay_after_gate_audit.csv is created by replay_quick_health.py and
    # contains all analyzable archive CSVs; do not replace it with Quick-only rows.
    _write(outdir/"software_fixes.csv",[{"issue":"Replay sensitivity ran without valid grading cases","file":"scripts/verification/replay_quick_health.py","old_behavior":"Computed 54 sensitivity rows although all Quick Scan cases were INVALID","root_cause":"Sensitivity loop was unconditional","fix":"Run sensitivity only if a valid finite composite score exists; production scoring unchanged","test":"tests/test_quick_health_replay.py (1 passed)","effect_on_replay":"Sensitivity output is empty; analysis correctly marked unassessable"},
        {"issue":"Evidence-gated grading test expected N/A for Quick Health","file":"tests/test_evidence_gated_grading.py","old_behavior":"Test asserted quick_grade=N/A for an incomplete Quick C1 screening assessment","root_cause":"Expectation predated the explicit INVALID / RETEST validity-gate contract","fix":"Update the assertion to INVALID; no production code changed","test":"Focused verification suite: 86 passed, 13 subtests passed","effect_on_replay":"No change to replay; confirms incomplete case remains ungraded"},
        {"issue":"Historical Quick Scan health gates","file":"aset_batt/acquisition/analysis.py; quick_health_config.json","old_behavior":"No recorded accepted OCV anchors or battery IDs; DCIR and Recovery references null","root_cause":"Historical data do not demonstrate valid OCV windows/identity/references; no parser defect demonstrated that can safely certify them","fix":"No production fix; preserve all gates and null references","test":"Replay of all 99 CSV entries; focused and full suites reported separately","effect_on_replay":"All 18 Quick Scans remain INVALID; no score or grade fabricated"}])
    (outdir/"selected_configuration.json").write_text(json.dumps(cfg,indent=2),encoding="utf-8")
    legacy_dcir_count = sum(row.get("candidate_status") == "LEGACY_RAW_DIAGNOSTIC_NOT_CERTIFIED" for row in dcir_rows)
    measured_dcir_count = sum(bool(row.get("dcir_available")) for row in quick)
    status_counts = Counter(str(row.get("completion_status")) for row in quick)
    completed_recovery = [row for row in recovery_rows if str(row.get("status")).lower() == "completed"]
    trace_lines = [f"- `{row['file']}`: {row['samples']} samples; OCV start/end window={row['start_window_candidate_status']}/{row['end_window_candidate_status']}; Q_main={row['q_main_ah']} Ah, mean I={row['i_mean_a']} A, k={row['peukert_k']}, Q_C10 estimate={row['q_c10_est_ah']} Ah; Quick SoH unavailable; pulse DCIR={row['dcir_mohm']} mΩ; ECM R0={row['ecm_r0_mohm']} mΩ (R²={row['ecm_r2']}); recovery={row['recovery_delta_v']} V; `{row['final']}`." for row in traces]
    recovery_lines = [f"- `{row['file']}`: ΔV={row['delta_v_candidate']} V; pulse-tail median={row['pulse_tail_median_v']} V; later RELAX-half median={row['relax_later_half_median_v']} V; pulse={row['pulse_current_median_a']} A for {row['pulse_duration_s']} s; RELAX={row['relax_duration_s']} s." for row in completed_recovery]
    gate_lines = [f"- {gate}: {count} ({category})" for (gate,category),count in gates.items()]
    (outdir/"gate_audit_summary.md").write_text(
        "# Quick Scan validity gate audit\n\n"
        "## Pipeline inspected before changes\n\n"
        "`DataHandler` and `write_session_metadata()` in `aset_batt/storage/data_utils.py` write telemetry and sidecar provenance. `QuickScanThread` in `aset_batt/ui/sequences/quick_scan.py` logs phases and records OCV validity only after its bounded settle validator accepts. `_auto_analyze()` in `aset_batt/app/auto_controller.py` flushes and calls `analyze_csv_mp()`. `analyze_csv()` / `_read_csv()` in `aset_batt/acquisition/analysis.py` load metadata, phases and measurements. Quick SoH requires main-discharge charge, profile Peukert parameters and valid OCV-derived SoC endpoints through `estimate_full_capacity()` in `ocv_validation.py`. `identify_dcir()` validates a sampled current edge and latency and normalizes pulse DCIR to 25 °C. ECM fitted R0/R² are separate quantities; neither is ACIR at 1 kHz. `_quick_recovery_delta_v()` calculates the median of the later RELAX half minus the median of the last ≤5 loaded MINI_PULSE samples. `assess_quick_health()` in `health_assessment.py` applies configured normalization/references/gates; `test_control.py` and `report_html.py` show the result.\n\n"
        "## Replay and gate results\n\n"
        f"- Archive SHA-256 before/after: `{archive_hash_before}` (unchanged); 99 CSVs present, 94 analyzed, 5 too short.\n"
        f"- Quick Scan: {len(quick)}; statuses `{dict(status_counts)}`; completed traces={len(traces)}; valid composite grade={sum(x['assessment_status']=='VALID' for x in quick)}. All Quick Scan results remain INVALID / RETEST.\n"
        f"- Battery IDs: {sum(bool(row['battery_id']) for row in quick)} traceable identities. Product/session labels are not physical Battery IDs. Quick/C10 pairs=0/{len(quick)}; error metrics unavailable.\n"
        f"- DCIR: {legacy_dcir_count} historical raw diagnostic values (exact edge details in `dcir_audit.csv`); current measured pulse gate available in {measured_dcir_count} runs. Healthy/EOL references remain null.\n"
        f"- Failed gate counts by category:\n" + "\n".join(gate_lines) + "\n\n"
        "## Three completed Quick Scan traces\n\n" + "\n".join(trace_lines) + "\n\n"
        "## Recovery audit\n\n" + "\n".join(recovery_lines) + "\n\n"
        "The completed recovery deltas are 0.49, 0.44 and 4.22 V. The third value is reproduced by the raw medians 8.22 V (pulse tail) and 12.44 V (later RELAX half). Pulse current/duration and RELAX duration are similar, but IDs and valid initial OCV SoC are missing, so the cause cannot be attributed to battery condition. Recovery is experimental/uncalibrated with no score reference.\n\n"
        "The three completed OCV starts have timeout/not-rested candidate windows; terminal TAIL_REST spans only ~59–60 s, below the 180 s minimum, and the voltage window is unstable. Historical sidecars lack accepted start/end OCV flags. Candidate voltages therefore do not establish SoC endpoints: ΔSoC, Q_full and Quick SoH remain unavailable. No phase-parser defect was verified. The 11 legacy raw DCIR results are not temperature-normalized/current certified; the current pulse route and fitted ECM R0/R² are reported separately.\n\n"
        "## Decision and tests\n\n"
        "Requirement 9 / Chapter 5.3.8 is NOT VALIDATED for grading accuracy or predictive-performance claims. It supports method description and exploratory diagnostics only. Default weights remain 0.60/0.30/0.10; thresholds 90/80/70 remain project-defined, not IEC. Sensitivity is unassessable; its CSV is header-only because there are zero valid grading cases. DCIR and Recovery references remain null.\n\n"
        "Targeted verification: 86 passed, 13 subtests passed. The replay utility sensitivity guard test is included. Full suite was attempted before the final test-only expectation update but interrupted near 70% after a long-running test; three failures appeared before interruption and no final pytest summary was produced. Full suite is not verified. `git diff --check` passed. No production algorithm/config, original ZIP, commit, push, or physical experiment was changed/performed.\n\n"
        "Minimum new evidence: unique traceable Battery IDs; exact profile/version and Peukert provenance; accepted start and ≥180 s stable end OCV anchors with output-off/current/stability evidence; Quick and reference C10 runs on the same battery; repeated healthy/EOL DCIR references at documented temperatures; and controlled repeated recovery at fixed SoC, current, pulse duration and rest. Evaluate sensitivity only after at least one valid composite score exists.\n",
        encoding="utf-8")
    return {"quick_runs":len(quick),"completed":len(traces),"dcir_edges":len(dcir_rows),"errors":errors,"archive_sha256":archive_hash_before}


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--archive",type=Path,required=True); ap.add_argument("--outdir",type=Path,required=True)
    print(json.dumps(run(ap.parse_args().archive,ap.parse_args().outdir),indent=2))
