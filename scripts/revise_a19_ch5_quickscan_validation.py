"""Replace Chapter 5 with an evidence-based Quick Scan validation chapter."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from docx.table import Table
from docx.text.paragraph import Paragraph


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "output" / "docx" / "A19_capstone_design_00_00_ch5_detailed.docx"
OUTPUT = ROOT / "output" / "docx" / "A19_capstone_design_00_00_ch5_quickscan_validated.docx"


def iter_blocks(doc):
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, doc)
        elif child.tag.endswith("}tbl"):
            yield Table(child, doc)


def text_of(paragraph: Paragraph) -> str:
    return " ".join(paragraph.text.split())


def shade(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_repeat_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    marker = OxmlElement("w:tblHeader")
    marker.set(qn("w:val"), "true")
    tr_pr.append(marker)


def set_cell_margins(cell, top=70, start=70, bottom=70, end=70) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for side, value in {"top": top, "start": start, "bottom": bottom, "end": end}.items():
        node = tc_mar.find(qn(f"w:{side}"))
        if node is None:
            node = OxmlElement(f"w:{side}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_table_geometry(table, usable_twips: int, ratios: list[float]) -> None:
    total = sum(ratios)
    widths = [round(usable_twips * r / total) for r in ratios]
    widths[-1] += usable_twips - sum(widths)
    table.autofit = False

    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.first_child_found_in("w:tblW")
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(usable_twips))
    tbl_w.set(qn("w:type"), "dxa")

    grid_cols = table._tbl.tblGrid.gridCol_lst
    for idx, width in enumerate(widths):
        grid_cols[idx].set(qn("w:w"), str(width))
        for row in table.rows:
            tc_w = row.cells[idx]._tc.tcPr.tcW
            tc_w.set(qn("w:w"), str(width))
            tc_w.set(qn("w:type"), "dxa")


def format_table(table, ratios: list[float], doc: Document) -> None:
    table.style = "Table Grid"
    usable_twips = int(
        (doc.sections[0].page_width
         - doc.sections[0].left_margin
         - doc.sections[0].right_margin) / 635
    )
    set_table_geometry(table, usable_twips, ratios)
    set_repeat_header(table.rows[0])
    for r_idx, row in enumerate(table.rows):
        for cell in row.cells:
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            set_cell_margins(cell)
            for para in cell.paragraphs:
                para.paragraph_format.space_before = Pt(0)
                para.paragraph_format.space_after = Pt(0)
                para.paragraph_format.line_spacing = 1.0
                if r_idx == 0:
                    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in para.runs:
                    run.font.size = Pt(8.5)
                    if r_idx == 0:
                        run.font.bold = True
                        run.font.color.rgb = None
            if r_idx == 0:
                shade(cell, "1F4E78")
                for run in cell.paragraphs[0].runs:
                    run.font.color.rgb = None
                    r_pr = run._element.get_or_add_rPr()
                    color = r_pr.find(qn("w:color"))
                    if color is None:
                        color = OxmlElement("w:color")
                        r_pr.append(color)
                    color.set(qn("w:val"), "FFFFFF")


def add_paragraph(doc, text: str, style: str = "Normal", bold_prefix: str | None = None):
    p = doc.add_paragraph(style=style)
    p.paragraph_format.space_after = Pt(3)
    if bold_prefix and text.startswith(bold_prefix):
        p.add_run(bold_prefix).bold = True
        p.add_run(text[len(bold_prefix):])
    else:
        p.add_run(text)
    return p


def add_caption(doc, text: str):
    p = doc.add_paragraph(style="Normal")
    p.paragraph_format.space_before = Pt(3)
    p.paragraph_format.space_after = Pt(2)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = True
    return p


def add_table(doc, rows: list[list[str]], ratios: list[float]):
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    for r_idx, values in enumerate(rows):
        for c_idx, value in enumerate(values):
            cell = table.cell(r_idx, c_idx)
            cell.text = value
    format_table(table, ratios, doc)
    return table


def insert_before(reference, nodes) -> None:
    for node in nodes:
        reference.addprevious(node)


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = Document(SOURCE)

    chapter5 = None
    chapter6 = None
    for block in iter_blocks(doc):
        if isinstance(block, Paragraph):
            text = text_of(block)
            if text.startswith("บทที่ 5"):
                chapter5 = block._p
            elif chapter5 is not None and text.startswith("บทที่ 6"):
                chapter6 = block._p
                break
    if chapter5 is None or chapter6 is None:
        raise RuntimeError("Could not locate the Chapter 5 boundaries.")

    body_children = list(doc.element.body)
    start = body_children.index(chapter5)
    end = body_children.index(chapter6)
    for element in body_children[start:end]:
        doc.element.body.remove(element)

    new_nodes = []

    def p(text, style="Normal", bold_prefix=None):
        para = add_paragraph(doc, text, style, bold_prefix)
        new_nodes.append(para._p)
        return para

    def caption(text):
        para = add_caption(doc, text)
        new_nodes.append(para._p)
        return para

    def table(rows, ratios):
        tbl = add_table(doc, rows, ratios)
        new_nodes.append(tbl._tbl)
        return tbl

    h1 = p("บทที่ 5 การพิสูจน์ยืนยันและการตรวจสอบความถูกต้อง / Chapter 5: Verification and Validation", "Heading 1")
    h1.paragraph_format.page_break_before = True

    p("5.1 วัตถุประสงค์และขอบเขตการพิสูจน์ยืนยัน / Verification Objective and Scope", "Heading 2")
    p(
        "บทนี้พิสูจน์ยืนยันสมรรถนะของระบบ ASET Battery Tester โดยแยกการยืนยันออกเป็น "
        "(1) ความถูกต้องของข้อมูลวัดและการป้องกันความปลอดภัยของระบบ และ (2) ความน่าเชื่อถือของผล Quick Scan "
        "สำหรับแบตเตอรี่ VRLA 12 V 5.3 Ah (B007) การแยกนี้จำเป็นเพราะ Quick Scan มีการคายประจุที่ 1C "
        "เพื่อให้ได้ข้อมูลรวดเร็วกว่าการทดสอบความจุที่อัตราอ้างอิง แต่ค่าความจุจาก 1C ไม่ใช่ค่าพิกัด C10 โดยตรง"
    )
    p(
        "ดังนั้นผล Quick Scan ต้องรายงานสองสถานะ ได้แก่ “Quick Electrical Grade” ซึ่งตัดสินจากพัลส์ DCIR/ECM "
        "และ “Capacity Grade” ซึ่งยืนยันจากการชาร์จเต็ม พักตัว และคายประจุถึงจุดตัดตามเงื่อนไขอ้างอิง "
        "ห้ามใช้เกรด Quick Electrical เพียงอย่างเดียวอ้างว่าแบตเตอรี่มีความจุเต็มตามพิกัด"
    )
    caption("ตารางที่ 5.1 สถานะหลักฐานการพิสูจน์ยืนยันในบทนี้")
    table([
        ["ข้อกำหนดจากบทที่ 2", "หลักฐาน/วิธีตรวจสอบ", "สถานะ ณ ชุดข้อมูลนี้"],
        ["Sample rate 3–10 Hz", "วัด Δt แยกตาม OCV, mini-pulse และ discharge", "ผลเบื้องต้น: mini-pulse อยู่ในช่วง 4.3–5.1 Hz; ช่วง discharge บันทึกช้ากว่า จึงต้องรายงานแยกเฟส"],
        ["DCIR repeatability ≤15 %CV", "Quick Scan ซ้ำอย่างน้อย 3 รอบ หลังชาร์จเต็มและพักตัวทุกครั้ง", "มี 2 รอบ: DCIR 45.53 และ 40.59 mΩ, %CV = 8.12%; ยังไม่ครบ n=3"],
        ["SoC uncertainty ±5%", "เทียบ OCV-SoC กับ coulomb count จาก full-reference discharge", "รอยืนยัน: ยังไม่มีชุดอ้างอิงเต็ม"],
        ["UVP 10.4–10.6 V", "บันทึกแรงดันขณะสั่ง load OFF และวัดซ้ำด้วย DMM", "ยังไม่ผ่าน: ค่า minimum under load 10.18 และ 10.27 V ต่ำกว่าเป้า"],
        ["DCIR stale-step gate 0.5 s", "ตรวจจำนวน current-edge ที่ถูกตัดทิ้งและสาเหตุ", "พบ stale edge 1 จุดในแต่ละไฟล์; ต้องลด latency ก่อนใช้เป็นหลักฐาน final"],
    ], [1.25, 2.5, 2.65])

    p("5.2 ขั้นตอนทดสอบ Quick Scan ที่ใช้สำหรับการยืนยัน / Verified Quick Scan Procedure", "Heading 2")
    p(
        "ก่อนเริ่มแต่ละรอบให้ชาร์จแบตเตอรี่ตามเงื่อนไขผู้ผลิตจนสิ้นสุดการชาร์จ พักแบตเตอรี่จนแรงดันคงตัว "
        "และบันทึกอุณหภูมิห้องและอุณหภูมิแบตเตอรี่ไว้ การทดสอบซ้ำแต่ละรอบต้องเป็นวงรอบอิสระ "
        "(charge–rest–test) เพื่อไม่ให้ผลจากรอบก่อนหน้าปะปนกับรอบถัดไป"
    )
    caption("ตารางที่ 5.2 ลำดับทดสอบ Quick Scan และข้อมูลที่ต้องบันทึก")
    table([
        ["เฟส", "การทำงาน", "การบันทึกข้อมูล", "เกณฑ์คุณภาพ", "ผลลัพธ์ที่ใช้"],
        ["0: OCV settle", "ปิด PSU และ e-load; รอ OCV เสถียร", "V, T, Δt, SoC ก่อนทดสอบ", "มีช่วงพักจริงและบันทึก SoC เริ่มต้น", "Initial SoC และ OCV anchor"],
        ["1: Mini-pulse", "ดึงกระแส 1C เป็นเวลา 30 s", "V/I ที่ ≥3 Hz, edge ก่อน–หลัง pulse", "ไม่มี stale edge; ECM R² ≥0.90", "DCIR, R0, R1, C1, τ"],
        ["1b: Relax", "ปลด load และพัก 90 s", "V(t), T, Δt", "แรงดันฟื้นตัวต่อเนื่อง", "ตรวจ fit และ polarization"],
        ["2: Main discharge", "คายประจุ 1C จนถึง 10.5 V", "V/I/T, elapsed time, Mode=MAIN_DISCHARGE", "จุดตัดต้อง 10.4–10.6 V", "Ah/Wh เฉพาะ main discharge"],
        ["3: Tail rest", "ปลด load และพัก 60 s", "V(t), current=0", "บันทึก OFF edge ทันที", "หลักฐานการตัดโหลด"],
        ["4: Analysis", "วิเคราะห์และสร้างรายงาน", "metadata: profile, rated Ah, Peukert k, polarity, software version", "ข้อมูลครบและตรวจสอบย้อนกลับได้", "Quick grade และ validity flag"],
    ], [0.7, 1.4, 1.55, 1.35, 1.7])
    p(
        "การอินทิเกรตความจุต้องใช้เวลา Elapsed_s จริงและต้องนับเฉพาะช่วง Mode = MAIN_DISCHARGE "
        "โดยคำนวณแบบ trapezoidal integration; mini-pulse เป็นข้อมูลสำหรับ ECM จึงไม่ควรถูกรวมกับ capacity "
        "ของการคายประจุหลัก การบันทึก Mode และ metadata เป็นข้อกำหนดสำหรับการทดสอบรอบถัดไป"
    )

    p("5.3 ผลทดสอบเบื้องต้นจากแบตเตอรี่ B007 / Preliminary B007 Results", "Heading 2")
    p(
        "ตารางนี้สรุปจากไฟล์ Quick Scan สองรอบของ B007 โดยใช้ผล capacity, DCIR และ SoH ที่รายงานในบันทึกผลเดิม "
        "ร่วมกับการแบ่งช่วงกระแสจาก CSV ค่า “Main-discharge Ah” เป็นค่าที่อินทิเกรตเฉพาะช่วงคายประจุหลัก "
        "ส่วน “Logged Ah” คือค่าที่รายงานโดยไฟล์เดิมซึ่งรวมผลจากช่วงพัลส์/ข้อมูลที่ไม่มีคอลัมน์ Mode"
    )
    caption("ตารางที่ 5.3 ผล Quick Scan ของ B007 สองรอบ")
    table([
        ["รอบ", "วันที่", "OCV (V)", "SoC ก่อน main discharge (%)", "Main-discharge Ah", "Logged Ah", "DCIR (mΩ)", "เกรดเดิม"],
        ["1", "31 Aug 2026", "12.72", "94.32", "3.173", "3.245", "45.53", "C"],
        ["2", "01 Sep 2026", "12.89", "100.00", "3.279", "3.355", "40.59", "C"],
    ], [0.45, 0.95, 0.65, 1.1, 0.95, 0.75, 0.8, 0.55])
    caption("ตารางที่ 5.4 สถิติการทำซ้ำเบื้องต้นของ B007 (n=2)")
    table([
        ["ตัวแปร", "รอบที่ 1", "รอบที่ 2", "Mean", "Sample SD", "%CV", "ข้อสรุป"],
        ["Main-discharge capacity (Ah)", "3.173", "3.279", "3.226", "0.075", "2.33", "คงที่ดี แต่ยังไม่ครบ 3 รอบ"],
        ["Logged capacity (Ah)", "3.245", "3.355", "3.300", "0.078", "2.35", "ใช้เปรียบเทียบแนวโน้มเท่านั้น"],
        ["DCIR (mΩ)", "45.53", "40.59", "43.06", "3.50", "8.12", "ผ่านแนวโน้ม ≤15% แต่ยังเป็นผลเบื้องต้น"],
        ["Minimum voltage under load (V)", "10.18", "10.27", "10.23", "0.06", "—", "ต่ำกว่าเป้า 10.4–10.6 V"],
    ], [1.45, 0.65, 0.65, 0.65, 0.75, 0.55, 1.7])
    p(
        "ผลทั้งสองรอบแสดงว่า B007 ให้ความจุที่ 1C ซ้ำกันได้ค่อนข้างดี แต่ยังไม่สามารถสรุปว่าแบตเตอรี่ “ผ่านเกรด A "
        "ด้านความจุ” ได้จากค่า 3.245–3.355 Ah เพียงอย่างเดียว เพราะ YTZ6V มีพิกัด C10 5.0 Ah และ C20 5.3 Ah; "
        "ไม่ใช่ 1C; ค่า C20 5.3 Ah ที่ 20HR ใช้แสดงพิกัดเท่านั้น และรอบที่ 1 เริ่ม main discharge ที่ SoC 94.32% ไม่เต็ม 100%"
    )
    p(
        "หากต้องการแปลงค่าประมาณ 3.279 Ah ที่ 1C ให้เทียบเท่า C10 5.0 Ah ตามสมการ Peukert "
        "จะต้องใช้ค่า k ประมาณ 1.21 สำหรับข้อมูลรอบที่ 2 ค่า k นี้ต้องยืนยันจากการคายประจุเต็มที่อย่างน้อยสอง C-rate "
        "หรือจาก datasheet ที่ตรวจสอบได้ การตั้งค่า k = 1.28 แล้วได้ SoH 100% เป็นผลจากสมมติฐานของแบบจำลอง "
        "จึงรายงานได้เป็น estimate เท่านั้น ไม่ใช่ผลยืนยันความจุ"
    )
    p(
        "นอกจากนี้ UVP ของทั้งสองรอบตัดที่แรงดันต่ำกว่าเป้าหมาย เนื่องจากการยืนยันจุดตัดหลาย sample "
        "ขณะบันทึก main discharge ทุกประมาณ 5 s ทำให้ปลด load ช้าหลังผ่าน 10.5 V ข้อสรุปของข้อกำหนด UVP "
        "ในชุดข้อมูลนี้จึงเป็น NEEDS CORRECTION ไม่ใช่ PASS"
    )

    p("5.4 เกณฑ์การแปลผลและการตัดเกรด / Interpretation and Grading Policy", "Heading 2")
    p(
        "เพื่อไม่ให้ความต้านทานที่ดีถูกตีความเป็นความจุที่ดีโดยอัตโนมัติ ระบบควรแสดงผลดังตารางที่ 5.5 "
        "โดยเกรดสุดท้ายที่ยืนยันได้ต้องใช้ผลที่แย่กว่าระหว่าง Capacity Grade และ Electrical Grade "
        "เมื่อทั้งสองผลมีคุณภาพข้อมูลผ่านเกณฑ์"
    )
    caption("ตารางที่ 5.5 หลักการแยกเกรด Quick Scan และเกรดยืนยัน")
    table([
        ["ผลข้อมูล", "สถานะที่แสดง", "ความหมายที่อนุญาตให้สรุป"],
        ["Pulse/ECM ผ่านคุณภาพ แต่ไม่มี full-reference capacity", "Quick Electrical A/B/C (provisional)", "บอกความต้านทานและการตอบสนองต่อโหลด ณ ขณะทดสอบ; ไม่ยืนยัน capacity"],
        ["เริ่มทดสอบไม่เต็ม หรือ Peukert k ยังไม่ยืนยัน", "Capacity estimate / REVIEW", "รายงาน Ah ดิบและ SoC เริ่มต้น; ห้ามใช้เป็น Capacity Grade final"],
        ["ชาร์จเต็ม–พัก–คายถึง cutoff ที่ C10 และข้อมูลครบ", "Capacity Grade A/B/C/REJECT", "A: ≥90%, B: 80–<90%, C: 70–<80%, REJECT: <70% ของพิกัด"],
        ["Capacity และ Electrical Grade ผ่านคุณภาพทั้งคู่", "Verified Overall Grade", "ใช้เกรดที่แย่กว่าเพื่อความปลอดภัย"],
        ["stale edge, fit ไม่ผ่าน, cutoff ผิดช่วง หรือ metadata ไม่ครบ", "REVIEW / INVALID", "ต้องทดสอบซ้ำ; ไม่ออกเกรดอัตโนมัติ"],
    ], [2.1, 1.6, 3.4])

    p("5.5 แผนการยืนยันให้ได้เกรด A อย่างถูกต้อง / Plan for a Defensible Grade A", "Heading 2")
    p(
        "B007 จะได้รับเกรด A ที่พิสูจน์ได้เมื่อทำตามลำดับต่อไปนี้: (1) แก้การตัด UVP ให้เกิดในช่วง 10.4–10.6 V "
        "โดยเพิ่ม sampling หรือใช้เวลา debounce ไม่เกิน 1 s เมื่อแรงดันใกล้ cutoff, (2) บันทึกไฟล์รูปแบบใหม่ที่มี "
        "Mode และ metadata ทุกแถว, (3) ทำ Quick Scan ซ้ำให้ครบ 3 รอบ โดยชาร์จเต็มและพักแบตเตอรี่ระหว่างรอบ, "
        "(4) ทำ full-reference discharge ที่ C10 = 0.53 A ถึง 10.5 V อย่างน้อยหนึ่งรอบภายใต้ 25–28 °C, "
        "และ (5) ใช้ค่า Peukert k ที่ได้จากการทดสอบ C-rate อิสระหรือแหล่งอ้างอิงที่ตรวจสอบได้"
    )
    caption("ตารางที่ 5.6 หลักฐานที่ต้องมีเพื่อสรุปผล B007")
    table([
        ["หลักฐาน", "จำนวนขั้นต่ำ", "เกณฑ์ยอมรับ", "สถานะปัจจุบัน"],
        ["Quick Scan repeatability", "3 รอบอิสระ", "DCIR %CV ≤15%", "มี 2 รอบ; ต้องเพิ่ม 1 รอบ"],
        ["ECM fit", "ทุก Quick Scan", "R² ≥0.90 และไม่มี stale edge", "รอยืนยันจาก CSV รูปแบบใหม่"],
        ["UVP accuracy", "3 รอบ", "10.4–10.6 V ณ การตัด load", "ยังไม่ผ่านใน 2 รอบเดิม"],
        ["Capacity reference", "อย่างน้อย 1 รอบต่อแบตเตอรี่", "C10, full charge, rest, 10.5 V cutoff", "ยังไม่มี"],
        ["Grade agreement", "อย่างน้อย 3 สภาพแบตเตอรี่", "Quick grade สอดคล้องกับ reference grade", "รอยืนยัน"],
    ], [1.75, 1.15, 2.3, 2.0])
    p(
        "สรุป: B007 มีสัญญาณเชิงบวกด้านความสามารถในการทำซ้ำของ Ah และ DCIR แต่หลักฐานปัจจุบันสนับสนุนได้เพียง "
        "“ผล Quick Scan เบื้องต้น” ไม่ใช่ Verified A การแก้จุดตัด UVP, การเก็บ Mode/metadata และการทำ C10 reference "
        "จะทำให้บทสรุป A หรือไม่ A มีที่มาที่ตรวจสอบย้อนกลับได้"
    )

    insert_before(chapter6, new_nodes)
    doc.settings.element.updateFields = True
    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
