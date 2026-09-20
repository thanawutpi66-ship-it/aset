"""
HTML formatters for analysis results shown in the GUI: the rich analytics
table and the compact inline sequence-result card. Pure functions of the
analysis dict — no widget state — extracted from isa101_views.py.

Reads aset_batt.ui.theme's palette constants live (via theme.X, not a
one-time import), so the HTML always reflects whichever theme is active when
each function is called — no special import ordering needed.
"""

import math
from html import escape

from aset_batt.ui import theme


def format_seq_result(res: dict) -> str:
    """Format an analyze_csv result dict into a short HTML string for the
    inline result card."""
    grade   = res.get("overall_grade", res.get("grade", "?"))
    quick_grade = res.get("quick_grade", "REVIEW")
    capacity_grade = res.get("capacity_grade", "REVIEW")
    electrical_grade = res.get("electrical_grade", "REVIEW")
    soh     = res.get("soh", float("nan"))
    cap     = res.get("capacity_ah", float("nan"))
    dcir    = res.get("dcir_mohm", float("nan"))
    conf    = res.get("confidence", 0.0)
    ecm     = res.get("ecm_identified", False)
    r0      = res.get("r0_mohm", float("nan"))
    r1      = res.get("r1_mohm", float("nan"))
    tau     = res.get("tau_s", float("nan"))
    r2      = res.get("ecm_r2", float("nan"))
    soh_str = f"{soh:.1f}%" if not math.isnan(soh) else "N/A"
    cap_str = f"{cap:.2f} Ah" if not math.isnan(cap) else "N/A"
    dcir_str = f"{dcir:.1f} mΩ" if not math.isnan(dcir) else "N/A"
    qsoh = res.get("quick_soh_est_pct", float("nan"))
    qsoh_str = f"{qsoh:.1f}%" if math.isfinite(qsoh) else "N/A"
    qcap = res.get("quick_capacity_est_ah")
    qcap_str = f"{qcap:.2f} Ah" if qcap is not None else "N/A"
    dcir_label = ("Measured DCIR" if res.get("dcir_measured")
                  else "Profile Resistance (Fallback)")
    lines = [
        f"<b>Quick Screening Grade: {quick_grade}</b>   Quick SoH Estimate: {qsoh_str}",
        f"Verified Overall: {grade}   Verified C10 SoH: {soh_str}   Measured Removed Charge: {cap_str}",
        f"OCV-normalized Estimated Full Capacity: {qcap_str}",
        f"Capacity: {capacity_grade}   Electrical: {electrical_grade}",
        f"{dcir_label}: {dcir_str}   Confidence: {conf*100:.0f}%",
    ]
    if ecm and not math.isnan(r0):
        lines.append(
            f"ECM — R0: {r0:.1f} mΩ  R1: {r1:.1f} mΩ  τ: {tau:.1f}s  R²: {r2:.3f}"
        )
    return "<br>".join(lines)


def build_results_html(results: dict) -> str:
    """Rich HTML table for the analytics results pane."""
    if results.get("analysis_layer") == "OFFLINE_CURRENT_REANALYSIS":
        return _build_offline_legacy_results_html(results)
    grade = results.get("overall_grade", results["grade"])
    gc = {"A": theme.OK, "B": theme.INFO, "C": theme.WARN, "REJECT": theme.CRIT, "REVIEW": theme.NEUTRAL}.get(grade, theme.NEUTRAL)
    soh = results["soh"]
    soh_txt = "N/A" if soh != soh else f"{soh:.1f}"
    conf = results.get("confidence", 1.0)
    dcir = results.get("dcir_mohm", results.get("ri_mohm", 0.0))
    dstd = results.get("dcir_std_mohm", 0.0)
    nstep = results.get("dcir_n_steps", 0)
    ocv = results.get("ocv_v", 0.0)
    cap_ah = results["capacity_ah"]
    cap_norm = results.get("capacity_norm_ah")
    soh_est = results.get("soh_est", float("nan"))
    soh_basis = results.get("soh_basis", "")
    capacity_grade = results.get("capacity_grade", "REVIEW")
    electrical_grade = results.get("electrical_grade", "REVIEW")
    capacity_basis = results.get("capacity_basis", "")
    quick_grade = results.get("quick_grade", "REVIEW")
    quick_basis = results.get("quick_grade_basis", "")
    warns = results.get("quality_warnings", [])

    def hdr(text):
        return (
            f'<tr><td colspan="2" style="background:{theme.PANEL2};padding:5px 8px;'
            f'font-weight:bold;color:{theme.TEXT};font-size:11px;'
            f'border-top:2px solid {theme.BORDER};border-bottom:1px solid {theme.BORDER}">'
            f'{text}</td></tr>'
        )

    def row(label, value, unit="", sub=""):
        sub_html = (
            f'<br><span style="font-size:9px;color:{theme.MUTED}">{sub}</span>'
        ) if sub else ""
        return (
            f'<tr>'
            f'<td style="padding:4px 8px 4px 14px;color:{theme.MUTED};font-size:11px;vertical-align:top">'
            f'{label}</td>'
            f'<td style="padding:4px 8px;color:{theme.INFO};font-family:Consolas,monospace;'
            f'font-size:12px;font-weight:bold;vertical-align:top">'
            f'{value}'
            f'<span style="color:{theme.MUTED};font-size:10px;font-weight:normal"> {unit}</span>'
            f'{sub_html}</td>'
            f'</tr>'
        )

    parts = [
        '<table width="100%" cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;">'
    ]

    # ── Summary ──
    parts.append(hdr("Summary"))
    parts.append(row("Quick Scan Grade", quick_grade, "",
                     quick_basis))
    parts.append(row(
        "Verified Overall Grade",
        f'<span style="color:{gc};font-size:14px">{grade}</span>',
        (f'conf {conf * 100:.0f}% — requires valid capacity + electrical evidence')
    ))
    parts.append(row("Verified C10 SoH", soh_txt, "%", soh_basis))
    if soh_est == soh_est and abs(soh_est - soh) > 1e-4:
        parts.append(row("Quick SoH Estimate", f"{soh_est:.1f}", "%",
                         "OCV-interval and Peukert normalized; screening only"))
    if results.get("quick_soh_est_pct") is not None or results.get("quick_peukert_k") is not None:
        parts.append(row(
            "Quick Peukert basis",
            f"k={results.get('quick_peukert_k', results.get('peukert_k', 'N/A'))}",
            "",
            f"source={results.get('peukert_k_source', 'UNKNOWN')}; "
            f"Iref={results.get('peukert_reference_current_a', results.get('reference_current_c10_a', 'N/A'))} A; "
            f"Kp={results.get('peukert_factor', 'N/A')}"))
    parts.append(row("Capacity Grade", capacity_grade, "",
                     "valid only for a phase-labelled full C10 discharge to cut-off"))
    parts.append(row("Electrical Grade", electrical_grade, "",
                     "valid only when DCIR/ECM pulse quality passes"))
    cap_sub = ""
    if cap_norm and abs(cap_norm - cap_ah) > 1e-4:
        k = results.get("peukert_k", 1.1)
        i_avg = results.get("mean_discharge_a", 0)
        cap_sub = f"rate-norm. {cap_norm:.3f} Ah @ k={k:.2f}, Ī={i_avg:.1f} A"
    cap_provenance = f"source: {capacity_basis}" if capacity_basis else ""
    cap_sub = "; ".join(x for x in (cap_sub, cap_provenance) if x)
    parts.append(row("Charge Removed", f"{cap_ah:.3f}", "Ah", cap_sub))
    quick_cap = results.get("quick_capacity_est_ah")
    parts.append(row("OCV-normalized Estimated Full Capacity",
                     f"{quick_cap:.3f}" if quick_cap is not None else "N/A", "Ah",
                     results.get("quick_capacity_est_status", "screening estimate")))
    parts.append(row("Rested OCV", f"{ocv:.3f}", "V"))

    # ── DCIR ──
    parts.append(hdr("Resistance &amp; Cranking  (DCIR @ ~250 ms, norm. 25 °C)"))
    meas_hint = "" if results.get("dcir_measured", True) else "no usable current step → profile baseline"
    step_sub = f"n={nstep} step{'s' if nstep != 1 else ''}" + (
        f"  {meas_hint}" if meas_hint else ""
    )
    label = "Measured DCIR" if results.get("dcir_measured", False) else "Profile Resistance (Fallback)"
    parts.append(row(label, f"{dcir:.2f} ± {dstd:.2f}", "mΩ", step_sub))
    parts.append(row("Voltage sag (load)", f"{results.get('voltage_sag_v', 0.0):.3f}", "V"))
    parts.append(row("CCA Proxy", f"{results.get('cca_est_a', 0.0):.0f}", "A",
                     "(OCV − cutoff) / DCIR"))
    slope = results.get("dcir_slope_mohm")
    if slope is not None and slope == slope and results.get("dcir_slope_r2", 0) >= 0.9:
        parts.append(row("DCIR (V–I slope)", f"{slope:.2f}", "mΩ",
                         f"R² {results['dcir_slope_r2']:.3f}, OCV-cancelled"))

    # ── R@fixed timepoints (FreedomCAR/SAE J537-style: 0.1s~ohmic, 1s~+charge-
    # transfer, 10s~+diffusion, closest to sustained-load/cranking resistance) ──
    tps = results.get("dcir_timepoints_mohm") or {}
    if tps:
        parts.append(hdr("R @ fixed post-edge timepoints (norm. 25 °C)"))
        for tp in sorted(tps):
            d = tps[tp]
            parts.append(row(f"R @ {tp:g}s", f"{d['r_mohm']:.2f} ± {d['std_mohm']:.2f}", "mΩ",
                             f"n={d['n_steps']} step{'s' if d['n_steps'] != 1 else ''}"))

    # ── ECM (HPPC only) ──
    if results.get("ecm_identified"):
        r2 = results.get("ecm_r2", 0.0)
        parts.append(hdr(f"1-RC Thévenin ECM  (HPPC, R² {r2:.3f})"))
        r0_method = results.get("r0_method", "load_on_ecm_fit")
        if r0_method == "release_edge_rc_compensated":
            n_release = results.get("r0_release_n", 0)
            parts.append(row("R₀  (release-edge, RC-comp.)",
                             f"{results['r0_mohm']:.2f}", "mΩ",
                             f"median of {n_release} release edge(s); ACIR-comparable"))
            parts.append(row("R₀  (load-on fit, audit)",
                             f"{results.get('r0_fit_mohm', float('nan')):.2f}", "mΩ"))
        else:
            parts.append(row("R₀  (ohmic, t=0 extrap.)", f"{results['r0_mohm']:.2f}", "mΩ"))
        parts.append(row("R₁  (polarisation)", f"{results['r1_mohm']:.2f}", "mΩ"))
        parts.append(row("C₁", f"{results['c1_farad']:.0f}", "F"))
        parts.append(row("τ  (R₁·C₁)", f"{results['tau_s']:.1f}", "s"))
        parts.append(row("Total (R₀+R₁)", f"{results['ri_mohm']:.2f}", "mΩ"))
        # FreedomCAR-style DC resistance at defined pulse timepoints (G5) — read
        # off the fitted model so they're comparable across rigs/labs regardless
        # of sample rate. R@10s is the closest surrogate to a cranking/high-rate pull.
        r01 = results.get("r_at_0p1s_mohm", float("nan"))
        r1s = results.get("r_at_1s_mohm", float("nan"))
        r10 = results.get("r_at_10s_mohm", float("nan"))
        if not math.isnan(r01):
            parts.append(row("DCR @ 0.1 / 1 / 10 s",
                             f"{r01:.1f} / {r1s:.1f} / {r10:.1f}", "mΩ",
                             "FreedomCAR timepoints (R@10s ≈ cranking)"))

    # ── Per-pulse breakdown (HPPC only) — the aggregated ECM above fits ONE
    # pulse; this table exposes the pulse-to-pulse trend the single fit hides
    # (a real run's rest anchor drifted 190 mV and every anchor-referenced R0
    # "declined" 27-37% purely from that — see identify_hppc_pulses). ──
    pulses = results.get("hppc_pulses") or []
    if pulses:
        drift = results.get("hppc_anchor_drift_v", float("nan"))
        cv = results.get("hppc_r0_cv_pct", float("nan"))
        sub = []
        if drift == drift:
            sub.append(f"anchor drift {drift * 1e3:+.0f} mV")
        if cv == cv:
            sub.append(f"R₀ CV {cv:.0f}%")
        parts.append(hdr(f"Per-pulse breakdown  ({len(pulses)} pulses"
                         + (", " + ", ".join(sub) if sub else "") + ")"))
        for p in pulses:
            stale = "  ⚠ edge stale" if p.get("edge_stale") else ""
            r0f = p.get("r0_fit_mohm", float("nan"))
            r0e = p.get("r0_edge_mohm", float("nan"))
            tau = p.get("tau_fit_s", float("nan"))
            r2p = p.get("fit_r2", float("nan"))
            fit_txt = (f"R₀ {r0f:.1f} mΩ  τ {tau:.1f} s  R² {r2p:.3f}"
                       if r0f == r0f else "fit failed")
            # G6 (regen pulse) / G1-G2 (SoC-sweep) support: tag the pulse's leg
            # when it's a charge-direction regen pulse (discharge is the
            # default, unlabeled, to keep the common case terse), and show the
            # SoC level it fired at when the CSV carried a SoC_pct column.
            leg_txt = "" if p.get("leg", "discharge") == "discharge" else "  [REGEN]"
            soc_p = p.get("soc_pct", float("nan"))
            soc_txt = f"  SoC {soc_p:.0f}%" if soc_p == soc_p else ""
            parts.append(row(
                f"Pulse {p['idx']}{leg_txt}{soc_txt}  ({p['i_pulse_a']:.2f} A, {p['duration_s']:.0f} s)",
                fit_txt, "",
                f"anchor {p['anchor_v']:.3f} V, edge R₀ {r0e:.1f} mΩ"
                f" @{p['edge_dt_s']:.1f}s{stale}"))

    # ── Quality flags ──
    if warns:
        parts.append(hdr("⚠ Data Quality Flags"))
        for w in warns:
            parts.append(
                f'<tr><td colspan="2" style="padding:3px 14px;color:{theme.CRIT};font-size:11px">'
                f'• {w}</td></tr>'
            )

    parts.append('</table>')
    return "".join(parts)


def _build_offline_legacy_results_html(results: dict) -> str:
    """Display historical provenance separately from offline raw reanalysis."""
    hist = {k: v for k, v in (results.get("historical_result") or {}).items()
            if v is not None}

    def show(value, fmt="{}"):
        if value is None:
            return "N/A"
        try:
            if isinstance(value, float) and not math.isfinite(value):
                return "N/A"
            return fmt.format(value)
        except (TypeError, ValueError):
            return str(value)

    rows = [
        ("Compatibility", results.get("compatibility_status", "LEGACY_LIMITED")),
        ("Dataset status", results.get("dataset_status", "LEGACY_LIMITED")),
        ("Integrity / hash", results.get("integrity_status", "INTEGRITY_HASH_UNAVAILABLE")),
        ("Current reanalysis status", results.get("current_reanalysis_status", "UNAVAILABLE")),
        ("Electrical status", results.get("electrical_status", "UNAVAILABLE")),
        ("Capacity status", results.get("capacity_basis_status", "UNAVAILABLE")),
        ("Phase detection", results.get("phase_detection_source", "UNAVAILABLE")),
        ("Measured DCIR", show(results.get("dcir_reanalyzed_mohm"), "{:.2f}") + " mΩ"),
        ("DCIR latency", show(results.get("dcir_reanalyzed_latency_s"), "{:.3f}") + " s"),
        ("Charge removed", show(results.get("charge_removed_ah"), "{:.3f}") + " Ah"),
        ("C10-equivalent interval charge", show(results.get("c10_equivalent_interval_charge_ah"), "{:.3f}") + " Ah"),
        ("Peukert exponent k", show(results.get("peukert_k"))),
        ("Peukert source", results.get("peukert_k_source", "LEGACY_UNKNOWN")),
        ("Peukert reference current", show(results.get("peukert_reference_current_a"), "{:.4f}") + " A"),
        ("Peukert factor Kp", show(results.get("peukert_factor"), "{:.4f}")),
        ("Reanalysis basis", results.get("peukert_reanalysis_basis", "UNKNOWN")),
        ("Historical Peukert k", show(results.get("historical_peukert_k"))),
        ("Historical Peukert source", results.get("historical_peukert_k_source", "LEGACY_UNKNOWN")),
        ("Recorded start voltage", show(results.get("recorded_rest_voltage_v"), "{:.3f}") + " V"),
        ("Validated start OCV", results.get("start_ocv_status", "UNAVAILABLE")),
        ("Validated end OCV", results.get("end_ocv_status", "UNAVAILABLE")),
        ("Current Quick SoH", "N/A"),
        ("Reason", results.get("quick_soh_current_method_reason", "Insufficient current-method evidence")),
    ]
    out = ['<table width="100%" cellspacing="0" cellpadding="4" style="border-collapse:collapse;font-family:Segoe UI,Arial">']
    info = results.get("file_information") or {}
    out.append('<tr><th colspan="2" align="left">FILE INFORMATION</th></tr>')
    for key, label in (("filename", "Filename"), ("session_id", "Session ID"),
                       ("test_type", "Test Type"), ("acquisition_date", "Acquisition Date"),
                       ("app_version", "App Version"), ("analysis_version", "Analysis Version"),
                       ("size_bytes", "Size (bytes)")):
        if info.get(key) is not None:
            out.append(f'<tr><td>{label}</td><td>{escape(str(info[key]))}</td></tr>')
    if hist:
        out.append('<tr><th colspan="2" align="left">HISTORICAL RESULT</th></tr>')
        labels = {"analysis_version": "Historical Algorithm", "capacity_basis_version": "Historical capacity basis version",
                  "rated_capacity_ah": "Historical rated capacity", "capacity_ah": "Historical capacity",
                  "soh": "Historical SoH", "quick_soh_est_pct": "Historical Quick SoH", "grade": "Historical grade"}
        for key, label in labels.items():
            if hist.get(key) is not None:
                out.append(f'<tr><td>{label}</td><td>{escape(str(hist[key]))}</td></tr>')
    out.append(f'<tr><th colspan="2" align="left">CURRENT REANALYSIS · {escape(str(results.get("dataset_status", "LEGACY_LIMITED")))}</th></tr>')
    out.extend(f'<tr><td>{escape(str(label))}</td><td>{escape(str(value))}</td></tr>' for label, value in rows)
    out.append('<tr><th colspan="2" align="left">EVIDENCE / NOTES</th></tr>')
    notes = list(results.get("dataset_notes") or [])
    if results.get("quick_soh_current_method_reason"):
        notes.append(results["quick_soh_current_method_reason"])
    out.extend(f'<tr><td colspan="2">{escape(str(note))}</td></tr>' for note in notes)
    out.append('</table>')
    return ''.join(out)
