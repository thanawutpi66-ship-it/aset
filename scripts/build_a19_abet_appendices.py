"""Append evidence-based mandatory appendices to the A19 capstone report.

The script intentionally does not replace unresolved validation placeholders in
Chapter 5.  It only adds material traceable to the existing report and the
provided ABET report template.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "output" / "docx" / "A19_capstone_design_merged.docx"
OUTPUT = ROOT / "output" / "docx" / "A19_capstone_design_abet_reviewed.docx"


def shade(cell, fill: str) -> None:
    props = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    props.append(shd)


def set_cell_text(cell, text: str, *, header: bool = False) -> None:
    cell.text = text
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    for paragraph in cell.paragraphs:
        paragraph.paragraph_format.space_after = Pt(0)
        for run in paragraph.runs:
            run.font.size = Pt(8.5)
            if header:
                run.bold = True
                run.font.color.rgb = RGBColor(255, 255, 255)


def add_table(doc: Document, headers: list[str], rows: list[list[str]]) -> None:
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.autofit = True
    for cell, label in zip(table.rows[0].cells, headers):
        shade(cell, "1F4E78")
        set_cell_text(cell, label, header=True)
    for values in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, values):
            set_cell_text(cell, value)
    doc.add_paragraph()


def add_appendix_heading(doc: Document, thai: str, english: str) -> None:
    heading = doc.add_heading(f"{thai} / {english}", level=1)
    heading.paragraph_format.keep_with_next = True


def add_text(doc: Document, text: str) -> None:
    paragraph = doc.add_paragraph(text)
    paragraph.paragraph_format.space_after = Pt(6)


def enable_field_updates(doc: Document) -> None:
    settings = doc.settings.element
    update = settings.find(qn("w:updateFields"))
    if update is None:
        update = OxmlElement("w:updateFields")
        settings.append(update)
    update.set(qn("w:val"), "true")


def main() -> None:
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, OUTPUT)
    doc = Document(OUTPUT)

    page = doc.add_paragraph()
    page.add_run().add_break(WD_BREAK.PAGE)
    add_appendix_heading(doc, "ภาคผนวก", "Appendices")
    add_text(
        doc,
        "ภาคผนวกนี้รวบรวมหลักฐานประกอบที่อ้างอิงได้จากเนื้อหารายงาน เพื่อให้การตรวจสอบข้อกำหนดทางวิศวกรรมและผลลัพธ์การเรียนรู้เป็นระบบเดียวกัน.",
    )

    add_appendix_heading(doc, "ภาคผนวก ก การคำนวณโดยละเอียด", "Appendix A: Detailed Calculations")
    add_text(
        doc,
        "ตารางต่อไปนี้เป็นดัชนีการคำนวณที่ใช้ในการออกแบบและการตั้งค่าการทดสอบ โดยค่าที่แสดงเป็นค่าซึ่งระบุไว้แล้วในบทที่ 4 ของรายงาน.",
    )
    add_table(doc, ["Calculation", "Inputs and method", "Report evidence"], [
        ["DCIR by two-pulse method", "R₀ = ΔV / ΔI = 0.150 V / 5 A = 30 mΩ", "Section 4.3 Engineering Analysis"],
        ["Peukert normalized capacity", "Legacy example: 5.3 Ah at 0.2C (1.06 A), not YTZ6V basis; corrected YTZ6V reference is C10 5.0 Ah at 0.500 A", "Section 4.3 Engineering Analysis"],
        ["Sampling interval", "Target sampling rate 10 Hz; nominal interval 100 ms; time-gap gate 0.5 s", "Sections 4.3 and 5.1"],
    ])

    add_appendix_heading(doc, "ภาคผนวก ข แบบวิศวกรรม", "Appendix B: Engineering Drawings")
    add_text(
        doc,
        "แบบและแผนผังที่ใช้ตรวจสอบการติดตั้งจริงอยู่ในบทที่ 4.4 เพื่อคงการอ้างอิงเดียวกับรายละเอียดการออกแบบและการเดินสายของระบบ.",
    )
    add_table(doc, ["Drawing / diagram", "Purpose", "Report evidence"], [
        ["4-Wire Kelvin system wiring", "Shows separate force and sense paths for voltage measurement.", "Figure 4.1; Section 4.4"],
        ["As-built system configuration", "Shows the PSU, e-load, ESP32 controller, protection and sensing connections.", "Figure 4.2; Section 4.4"],
    ])

    add_appendix_heading(doc, "ภาคผนวก ค เอกสารข้อมูลจำเพาะ", "Appendix C: Datasheets and Specifications")
    add_text(
        doc,
        "ตารางนี้เป็นดัชนีอุปกรณ์และข้อมูลจำเพาะที่รายงานใช้ในการออกแบบ. เอกสารผู้ผลิตฉบับเต็มควรเก็บแนบในแฟ้มส่งมอบโครงงานเมื่อมีต้นฉบับที่ตรวจสอบได้.",
    )
    add_table(doc, ["Equipment", "Model / role", "Report evidence"], [
        ["DC Power Supply", "GW Instek PSW80-40.5; controlled charging source", "Sections 4.4 and 4.6"],
        ["Electronic Load", "ITECH PEL-3111; controlled discharge load", "Sections 4.4 and 4.6"],
        ["Controller", "ESP32; SCPI command, acquisition and safety logic", "Sections 4.4–4.5"],
        ["Temperature sensor", "MLX90614 infrared sensor; thermal monitoring", "Sections 4.4 and 5.1"],
        ["Protection devices", "SSR and circuit breaker; controlled isolation and protection", "Sections 4.4–4.5"],
    ])

    add_appendix_heading(doc, "ภาคผนวก ง เอกสารการบริหารโครงงาน", "Appendix D: Project Management Documents")
    add_text(
        doc,
        "หลักฐานการวางแผนและการทำงานร่วมกันในรายงานถูกจัดทำเป็นดัชนีไว้ด้านล่าง เพื่อใช้ติดตามเอกสารการประชุม ตารางงาน และหลักฐานการมีส่วนร่วมของสมาชิกในแฟ้มโครงงาน.",
    )
    add_table(doc, ["Project-management evidence", "Location in report", "Use"], [
        ["Team organization and member responsibilities", "Sections 7.1–7.2", "Defines roles, ownership and the work plan."],
        ["Schedule and milestones", "Section 1.7", "Tracks planned delivery and decision points."],
        ["Progress monitoring and communication", "Section 7.3", "Records the monitoring approach and coordination."],
        ["Lessons learned and improvement", "Section 7.4", "Captures improvements for the remaining work."],
        ["Individual contribution records", "Project evidence file", "Attach signed task/time records when submitting the final project package."],
    ])

    add_appendix_heading(doc, "ภาคผนวก จ การเชื่อมโยงผลลัพธ์การเรียนรู้ ABET", "Appendix E: ABET Student Outcome Mapping")
    add_text(
        doc,
        "ตารางนี้ระบุตำแหน่งหลักฐานเฉพาะในรายงานตาม 7 Student Outcomes และ 23 Performance Indicators. ตัวชี้วัดการสื่อสารด้วยวาจาประเมินใน Final Defense นอกเหนือจากรายงานฉบับเขียน.",
    )
    rows = [
        ["SO1 Problem Solving", "PI1", "Shows understanding of the problem", "1.3 Problem Statement"],
        ["SO1 Problem Solving", "PI2", "Chooses an appropriate model / solution procedure", "4.1–4.2 Design Methodology and Engineering Analysis"],
        ["SO1 Problem Solving", "PI3", "Solution is appropriate within constraints", "4.6 Final Specifications; 5.4 Compliance Matrix"],
        ["SO2 Design", "PI1", "Produces a clear needs statement and specifications", "2.1–2.2 Customer Needs and Engineering Requirements"],
        ["SO2 Design", "PI2", "Identifies constraints and establishes criteria", "2.4–2.5 Design Constraints and Criteria Summary"],
        ["SO2 Design", "PI3", "Carries the design through to a justified solution", "3.4 Selected Concept"],
        ["SO3 Communication", "PI1", "Uses technical writing style", "Whole report (written style)"],
        ["SO3 Communication", "PI2", "Uses appropriate graphics", "4.4 Engineering Drawings; figures and tables throughout"],
        ["SO3 Communication", "PI3", "Demonstrates correct mechanics and grammar", "Whole report"],
        ["SO3 Communication", "PI4", "Communicates orally with clear body language", "Final Defense (outside report)"],
        ["SO4 Ethics", "PI1", "Knows the code of ethics and evaluates ethical dimensions", "6.5 Ethical Considerations"],
        ["SO4 Ethics", "PI2", "Evaluates economic impact", "6.1 Economic Impact"],
        ["SO4 Ethics", "PI3", "Identifies environmental and social issues", "6.2 Environmental Impact; 6.4 Social Impact"],
        ["SO5 Teamwork", "PI1", "Defines roles, including leadership", "7.1 Team Organization"],
        ["SO5 Teamwork", "PI2", "Establishes goals and a work plan", "7.2 Work Plan; 1.7 Schedule"],
        ["SO5 Teamwork", "PI3", "Creates a collaborative, dependable environment", "7.3–7.4; Appendix D"],
        ["SO6 Experimentation", "PI1", "Applies good laboratory practice and operates instrumentation", "5.2 Test Setup and Procedures"],
        ["SO6 Experimentation", "PI2", "Selects appropriate data, equipment and protocols", "5.1 Verification and Validation Plan"],
        ["SO6 Experimentation", "PI3", "Analyzes, verifies and validates results", "5.3–5.5 Test Results, Compliance and Discussion"],
        ["SO7 Lifelong Learning", "PI1", "Recognizes the need for continued education", "8.3–8.4 Recommendations and Future Work"],
        ["SO7 Lifelong Learning", "PI2", "Finds information without guidance", "1.1 Background and Motivation"],
        ["SO7 Lifelong Learning", "PI3", "Identifies current issues and evaluates alternatives", "3.1 Alternative Concepts"],
        ["SO7 Lifelong Learning", "PI4", "Selects and compares tools and techniques", "4.5 Implementation and Integration"],
    ]
    add_table(doc, ["Student Outcome", "PI", "Performance indicator", "Evidence (chapter / section)"], rows)

    enable_field_updates(doc)
    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
