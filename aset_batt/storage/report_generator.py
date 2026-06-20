"""
PDF Report Generator — รายงานผลทดสอบแบตเตอรี่ (สำหรับงานคัดแยก / เล่ม capstone)

ใช้ reportlab สร้าง PDF: ข้อมูลแบต, สถานะ SoC/SoH/Rin, ผล AI grade, และกราฟ V/I
จาก CSV (ถ้ามี). ออกแบบให้ทนทาน — ส่วนไหนข้อมูลไม่พอก็ข้าม ไม่ทำให้ทั้งรายงานล่ม
"""
import os
import logging
import tempfile
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

    # --- Battery info ---
    b = config.battery
    story.append(Paragraph("Battery Under Test", h2))
    story.append(_info_table([
        ["Chemistry", str(b.battery_type)],
        ["Configuration", f"{b.cells_series}S{b.cells_parallel}P"],
        ["Nominal Voltage", f"{b.pack_nominal_voltage:.2f} V (pack)"],
        ["Rated Capacity", f"{b.rated_capacity:.2f} Ah"],
        ["Mass", f"{getattr(b, 'mass_grams', 0):.0f} g"],
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
    if analysis is not None and getattr(analysis, "success", False):
        f = analysis.features
        story.append(Paragraph("AI Grading Result", h2))
        grade_tbl = _info_table([
            ["Grade", f"{analysis.grade}  ({analysis.confidence * 100:.0f}% conf, {analysis.method})"],
            ["SoH", f"{f.soh_pct:.1f} %"],
            ["Capacity", f"{f.capacity_ah:.3f} Ah"],
            ["Energy", f"{f.energy_wh:.2f} Wh"],
            ["R0 (ohmic)", f"{f.r0_mohm:.2f} mΩ"],
            ["Rp (polarization)", f"{f.rp_mohm:.2f} mΩ"],
            ["Avg Temperature", f"{f.avg_temp_c:.1f} °C"],
        ])
        story.append(grade_tbl)
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
        "IEC 61960 reference</font>", normal))

    doc.build(story)

    if plot_path and os.path.exists(plot_path):
        try:
            os.remove(plot_path)
        except OSError:
            pass

    logger.info(f"PDF report written: {path}")
    return path
