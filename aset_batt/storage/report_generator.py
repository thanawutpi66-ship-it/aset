"""
PDF Report Generator — รายงานผลทดสอบแบตเตอรี่ (สำหรับงานคัดแยก / เล่ม capstone)

ใช้ reportlab สร้าง PDF: ข้อมูลแบต, สถานะ SoC/SoH/Rin, ผล AI grade, และกราฟ V/I
จาก CSV (ถ้ามี). ออกแบบให้ทนทาน — ส่วนไหนข้อมูลไม่พอก็ข้าม ไม่ทำให้ทั้งรายงานล่ม
"""
import os
import logging
import tempfile
import json
import math
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

logger = logging.getLogger(__name__)

_PRIMARY = colors.HexColor("#005a9e")


def _fmt(value, decimals=2, suffix=""):
    try:
        number = float(value)
        return f"{number:.{decimals}f}{suffix}" if math.isfinite(number) else "N/A"
    except (TypeError, ValueError):
        return "N/A"


def _info_table(rows):
    t = Table(rows, colWidths=[55 * mm, 110 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#374151")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#e5e7eb")),
    ]))
    return t


def _render_csv_plot(csv_path):
    """render กราฟ V/I vs time จาก CSV → ไฟล์ PNG ชั่วคราว (คืน path หรือ None)"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import csv as _csv

        t, v, i = [], [], []
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                try:
                    t.append(float(row["Elapsed_s"]))
                    v.append(float(row["Voltage_V"]))
                    i.append(float(row["Current_A"]))
                except (KeyError, ValueError):
                    continue
        if len(t) < 2:
            return None

        fig, ax1 = plt.subplots(figsize=(7, 3.2), dpi=120)
        ax1.plot(t, v, color="#005a9e", linewidth=1.3, label="Voltage")
        ax1.set_xlabel("Time (s)")
        ax1.set_ylabel("Voltage (V)", color="#005a9e")
        ax2 = ax1.twinx()
        ax2.plot(t, i, color="#d83b01", linewidth=1.0, label="Current")
        ax2.set_ylabel("Current (A)", color="#d83b01")
        ax1.grid(True, alpha=0.3)
        fig.tight_layout()
        tmp = os.path.join(tempfile.gettempdir(),
                           f"aset_report_plot_{os.getpid()}.png")
        fig.savefig(tmp)
        plt.close(fig)
        return tmp
    except Exception as e:
        logger.warning(f"render csv plot ไม่สำเร็จ: {e}")
        return None


def generate_pdf_report(path, config, estimator=None, analysis=None, csv_path=None):
    """สร้างไฟล์ PDF รายงานผลทดสอบ

    path      : ปลายทาง .pdf
    config    : ConfigManager (อ่าน battery/system)
    estimator : StateEstimator (อ่าน SoC/SoH/Rin ปัจจุบัน) — optional
    analysis  : AnalysisResult จาก AI grader — optional
    csv_path  : ไฟล์ข้อมูลดิบสำหรับ plot — optional
    """
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], textColor=_PRIMARY, fontSize=20)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=_PRIMARY)
    normal = styles["Normal"]

    doc = SimpleDocTemplate(path, pagesize=A4,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    story = []

    story.append(Paragraph("ASET Battery Test Report", h1))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal))
    story.append(Spacer(1, 8 * mm))

    session_meta = {}
    if csv_path and os.path.exists(csv_path + ".meta.json"):
        try:
            with open(csv_path + ".meta.json", encoding="utf-8") as handle:
                session_meta = json.load(handle)
        except (OSError, ValueError):
            pass

    # --- Battery info ---
    b = config.battery
    story.append(Paragraph("Battery Under Test", h2))
    story.append(_info_table([
        ["Chemistry", str(b.battery_type)],
        ["Configuration", f"{b.cells_series}S{b.cells_parallel}P"],
        ["Nominal Voltage", f"{b.pack_nominal_voltage:.2f} V (pack)"],
        ["Selected reference capacity", f"{b.rated_capacity:.2f} Ah"],
        ["Capacity rating basis", (f"C10 {session_meta.get('capacity_10h_ah', 'N/A')} Ah; "
                                    f"C20 {session_meta.get('capacity_20h_ah', 'N/A')} Ah; "
                                    f"basis version {session_meta.get('capacity_basis_version', 'legacy / not recorded')}")],
        ["Peukert k / source", f"{session_meta.get('peukert_k', 'N/A')} / {session_meta.get('peukert_k_source', 'LEGACY_UNKNOWN')}"],
        ["Peukert reference", f"{session_meta.get('peukert_reference_hr', 'N/A')} h; {session_meta.get('peukert_reference_current_a', 'N/A')} A"],
        ["Quick Peukert factor", _fmt(session_meta.get('peukert_factor'), 4)],
        ["Mass", f"{getattr(b, 'mass_grams', 0):.0f} g"],
    ]))
    story.append(Spacer(1, 6 * mm))

    # Validation evidence is opt-in session context, never inferred from a
    # routine test.  Keep it compact in the submission PDF; raw residuals and
    # traces remain in the linked CSV/session sidecar.
    if session_meta.get("test_type") == "CoulombEfficiency":
        story.append(Paragraph("Coulombic Efficiency", h2))
        story.append(_info_table([
            ["Q charged (Qin)", _fmt(session_meta.get("Qin_Ah"), 3, " Ah")],
            ["Q discharged (Qout)", _fmt(session_meta.get("Qout_Ah"), 3, " Ah")],
            ["Coulombic Efficiency", _fmt(session_meta.get("eta_coulomb_pct"), 2, " %")],
            ["Result status", str(session_meta.get("eta_status", "UNKNOWN"))],
            ["Reference discharge", _fmt(session_meta.get("reference_discharge_current_a"), 3, " A (C10)")],
            ["Charge / discharge duration", f"{_fmt(session_meta.get('charge_duration_s', 0) / 3600, 2, ' h')} / {_fmt(session_meta.get('discharge_duration_s', 0) / 3600, 2, ' h')}"],
            ["Interpretation", "Coulombic efficiency is not capacity SoH."],
        ]))
        story.append(Spacer(1, 6 * mm))
    campaign = session_meta.get("validation_campaign") or {}
    evidence = session_meta.get("validation_evidence") or {}
    if campaign.get("enabled"):
        ambient = evidence.get("ambient") or {}
        story.append(Paragraph("Validation Campaign Evidence", h2))
        ambient_text = (f"{ambient.get('min_c', 'N/A')}–{ambient.get('max_c', 'N/A')} °C "
                        f"({'in band' if ambient.get('in_band') else 'out of band / incomplete'})")
        story.append(_info_table([
            ["Campaign / specimen", f"{campaign.get('campaign_id', 'N/A')} / {campaign.get('specimen_id', 'N/A')}"],
            ["Expected condition / run", f"{campaign.get('expected_condition', 'N/A')} / {campaign.get('run_index', 'N/A')}"],
            ["Preconditioning", str(campaign.get('preconditioning', 'N/A'))],
            ["Ambient evidence", ambient_text],
            ["Interpretation", "Campaign label is not ground truth; compare measured C10 SoH and repeatability."],
        ]))
        story.append(Spacer(1, 6 * mm))

    # --- Live state ---
    if estimator is not None:
        try:
            s = estimator.get_state()
            story.append(Paragraph("Measured State", h2))
            story.append(_info_table([
                ["State of Charge", f"{s.get('soc', 0):.1f} %"],
                ["State of Health", f"{s.get('soh', 0):.1f} %"],
                ["Internal Resistance", f"{s.get('rin', 0) * 1000:.2f} mΩ"],
                ["Charge Throughput", f"{s.get('ah_accumulated', 0):.3f} Ah"],
            ]))
            story.append(Spacer(1, 6 * mm))
        except Exception as e:
            logger.warning(f"estimator state ไม่พร้อม: {e}")

    # --- AI grade ---
    # Supports two formats:
    #   dict  — keys: grade, soh, capacity_ah, dcir_mohm, r0_mohm, r1_mohm, c1_farad,
    #                  tau_s, ecm_identified, ecm_r2, confidence, quality_warnings
    #   legacy object — has .success attr (old AnalysisResult)
    if analysis is not None:
        if isinstance(analysis, dict):
            _grade = analysis.get("grade", "?")
            _quick_grade = analysis.get("quick_grade", "REVIEW")
            _conf = analysis.get("confidence", 0.0)
            _soh = analysis.get("soh", float("nan"))
            _soh_est = analysis.get("soh_est", _soh)
            _cap = analysis.get("capacity_ah", float("nan"))
            _dcir = analysis.get("dcir_mohm", 0.0)
            _r0 = analysis.get("r0_mohm", 0.0)
            _r1 = analysis.get("r1_mohm", 0.0)
            _tau = analysis.get("tau_s", 0.0)
            _ecm_id = analysis.get("ecm_identified", False)
            _ecm_r2 = analysis.get("ecm_r2", 0.0)
            _ecm_rmse = analysis.get("ecm_rmse_mv", float("nan"))
            _warnings = analysis.get("quality_warnings") or []
            _show = True
        elif getattr(analysis, "success", False):
            f = analysis.features
            _grade = analysis.grade
            _quick_grade = "N/A"
            _conf = analysis.confidence
            _soh = getattr(f, "soh_pct", float("nan"))
            _soh_est = _soh
            _cap = getattr(f, "capacity_ah", 0.0)
            _dcir = getattr(f, "r0_mohm", 0.0) + getattr(f, "rp_mohm", 0.0)
            _r0 = getattr(f, "r0_mohm", 0.0)
            _r1 = getattr(f, "rp_mohm", 0.0)
            _tau = 0.0
            _ecm_id = False
            _ecm_r2 = 0.0
            _ecm_rmse = float("nan")
            _warnings = []
            _show = True
        else:
            _show = False

        if _show:
            story.append(Paragraph("AI Grading Result", h2))
            grade_rows = [
                ["Quick Screening Grade", str(_quick_grade)],
                ["Quick SoH Estimate", _fmt(analysis.get("quick_soh_est_pct"), 1, " %")],
                ["Verified Grade", f"{_grade}  ({_conf * 100:.0f}% confidence)"],
                ["Measured Removed Charge", _fmt(analysis.get("q_removed_ah", _cap), 3, " Ah")],
                ["OCV-normalized Estimated Full Capacity", _fmt(analysis.get("quick_capacity_est_ah"), 3, " Ah")],
                ["Measured DCIR" if analysis.get("dcir_measured") else "Profile Resistance (Fallback)", f"{_dcir:.2f} mΩ"],
                ["CCA Proxy", f"{analysis.get('cca_est_a', 0.0):.0f} A"],
            ]
            if _ecm_id:
                grade_rows.extend([
                    ["ECM R0", f"{_r0:.2f} mΩ"],
                    ["ECM R1", f"{_r1:.2f} mΩ"],
                    ["ECM τ (time constant)", f"{_tau:.2f} s"],
                ])
                grade_rows.append(["ECM R²", f"{_ecm_r2:.4f}"])
                if _ecm_rmse == _ecm_rmse:
                    grade_rows.append(["ECM voltage RMSE", f"{_ecm_rmse:.2f} mV"])
            if _warnings:
                grade_rows.append(["Warnings", "; ".join(str(w) for w in _warnings)])
            story.append(_info_table(grade_rows))

            # ECM summary note when equivalent-circuit model was identified
            if _ecm_id:
                ecm_style = ParagraphStyle(
                    "ecm_note",
                    parent=styles["Normal"],
                    fontSize=9,
                    textColor=colors.HexColor("#374151"),
                    backColor=colors.HexColor("#f0f9ff"),
                    borderColor=colors.HexColor("#93c5fd"),
                    borderWidth=0.5,
                    borderPadding=4,
                )
                story.append(Spacer(1, 3 * mm))
                story.append(Paragraph(
                    f"ECM: 1-RC model identified — "
                    f"R0 = {_r0:.1f} mΩ  "
                    f"R1 = {_r1:.1f} mΩ  "
                    f"τ = {_tau:.1f} s  "
                    f"R² = {_ecm_r2:.3f}  "
                    + (f"RMSE = {_ecm_rmse:.2f} mV" if _ecm_rmse == _ecm_rmse else ""),
                    ecm_style,
                ))
            story.append(Spacer(1, 6 * mm))

    # --- CSV plot ---
    plot_path = None
    if csv_path and os.path.exists(csv_path):
        plot_path = _render_csv_plot(csv_path)
        if plot_path:
            story.append(Paragraph("Voltage / Current Profile", h2))
            story.append(Image(plot_path, width=165 * mm, height=75 * mm))

    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        "<font color='#9ca3af' size='8'>Generated by ASET Universal Battery Tester · "
        "chemistry-specific method recorded in the session protocol</font>", normal))

    doc.build(story)

    if plot_path and os.path.exists(plot_path):
        try:
            os.remove(plot_path)
        except OSError:
            pass

    logger.info(f"PDF report written: {path}")
    return path
