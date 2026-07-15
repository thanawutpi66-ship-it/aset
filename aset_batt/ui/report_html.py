"""
HTML formatters for analysis results shown in the GUI: the rich analytics
table and the compact inline sequence-result card. Pure functions of the
analysis dict — no widget state — extracted from isa101_views.py.

Reads aset_batt.ui.theme's palette constants live (via theme.X, not a
one-time import), so the HTML always reflects whichever theme is active when
each function is called — no special import ordering needed.
"""

import math

from aset_batt.ui import theme


def format_seq_result(res: dict) -> str:
    """Format an analyze_csv result dict into a short HTML string for the
    inline result card."""
    grade   = res.get("grade", "?")
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
    lines = [
        f"<b>Grade: {grade}</b>   SoH: {soh_str}   Cap: {cap_str}",
        f"DCIR: {dcir_str}   Confidence: {conf*100:.0f}%",
    ]
    if ecm and not math.isnan(r0):
        lines.append(
            f"ECM — R0: {r0:.1f} mΩ  R1: {r1:.1f} mΩ  τ: {tau:.1f}s  R²: {r2:.3f}"
        )
    return "<br>".join(lines)


def build_results_html(results: dict) -> str:
    """Rich HTML table for the analytics results pane."""
    grade = results["grade"]
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
    parts.append(row(
        "Grade",
        f'<span style="color:{gc};font-size:14px">{grade}</span>',
        f'conf {conf * 100:.0f}%'
    ))
    parts.append(row("State of Health", soh_txt, "%"))
    cap_sub = ""
    if cap_norm and abs(cap_norm - cap_ah) > 1e-4:
        k = results.get("peukert_k", 1.1)
        i_avg = results.get("mean_discharge_a", 0)
        cap_sub = f"rate-norm. {cap_norm:.3f} Ah @ k={k:.2f}, Ī={i_avg:.1f} A"
    parts.append(row("Capacity", f"{cap_ah:.3f}", "Ah", cap_sub))
    parts.append(row("Rested OCV", f"{ocv:.3f}", "V"))

    # ── DCIR ──
    parts.append(hdr("Resistance &amp; Cranking  (DCIR @ ~250 ms, norm. 25 °C)"))
    meas_hint = "" if results.get("dcir_measured", True) else "no usable current step → profile baseline"
    step_sub = f"n={nstep} step{'s' if nstep != 1 else ''}" + (
        f"  {meas_hint}" if meas_hint else ""
    )
    label = "DCIR" if nstep > 0 else "R_base"
    parts.append(row(label, f"{dcir:.2f} ± {dstd:.2f}", "mΩ", step_sub))
    parts.append(row("Voltage sag (load)", f"{results.get('voltage_sag_v', 0.0):.3f}", "V"))
    parts.append(row("CCA proxy", f"{results.get('cca_est_a', 0.0):.0f}", "A",
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
            parts.append(row(
                f"Pulse {p['idx']}  ({p['i_pulse_a']:.2f} A, {p['duration_s']:.0f} s)",
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
