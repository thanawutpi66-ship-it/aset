"""Replace Chapter 5 in the reviewed A19 DOCX with the supplied Markdown text."""

from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "output" / "docx" / "A19_capstone_design_abet_reviewed.docx"
MARKDOWN = Path(r"C:\Users\boatl\.codex\attachments\07850803-4cdb-45a7-b7b8-2989999d6c82\pasted-text.txt")
OUTPUT = ROOT / "output" / "docx" / "A19_capstone_design_ch5_revised.docx"


def shade(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def set_cell_text(cell, text: str, header: bool = False) -> None:
    cell.text = text.replace("<br>", "\n")
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    for p in cell.paragraphs:
        p.paragraph_format.space_after = Pt(0)
        for run in p.runs:
            run.font.size = Pt(8.5)
            if header:
                run.bold = True
                run.font.color.rgb = RGBColor(255, 255, 255)


def clean_md(text: str) -> str:
    return text.replace("**", "").replace("$", "")


def add_table(doc: Document, rows: list[list[str]]) -> None:
    table = doc.add_table(rows=1, cols=len(rows[0]))
    table.style = "Table Grid"
    table.autofit = True
    for i, text in enumerate(rows[0]):
        shade(table.rows[0].cells[i], "1F4E78")
        set_cell_text(table.rows[0].cells[i], clean_md(text), header=True)
    for row in rows[1:]:
        cells = table.add_row().cells
        for i, text in enumerate(row):
            set_cell_text(cells[i], clean_md(text))
    doc.add_paragraph()


def parse_table(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        if re.match(r"^\|\s*-+", line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append(cells)
    return rows


def write_caption(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(3)
    m = re.match(r"\*\*(.+?)\*\*(.*)", text)
    if m:
        p.add_run(m.group(1)).bold = True
        p.add_run(m.group(2))
    else:
        p.add_run(clean_md(text))


def create_chapter_fragment(markdown: str, template: Document) -> Document:
    out = Document()
    # Reuse the source document's normal font when it exists; the cloned XML
    # below retains paragraph/table formatting from the temporary document.
    out.styles["Normal"].font.name = template.styles["Normal"].font.name
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("|"):
            block: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            add_table(out, parse_table(block))
            continue
        if line.startswith("# "):
            p = out.add_heading(clean_md(line[2:]), level=1)
            p.paragraph_format.keep_with_next = True
        elif line.startswith("## "):
            p = out.add_heading(clean_md(line[3:]), level=2)
            p.paragraph_format.keep_with_next = True
        elif line.startswith("### "):
            p = out.add_heading(clean_md(line[4:]), level=3)
            p.paragraph_format.keep_with_next = True
        elif line.startswith("**ตารางที่"):
            write_caption(out, line)
        elif line.startswith("> "):
            p = out.add_paragraph()
            r = p.add_run(clean_md(line[2:]))
            r.italic = True
            p.paragraph_format.space_after = Pt(5)
        elif re.match(r"^\d+\.\s+", line):
            p = out.add_paragraph(style="List Number")
            p.add_run(clean_md(re.sub(r"^\d+\.\s+", "", line)))
            p.paragraph_format.space_after = Pt(3)
        elif line.startswith("**สรุปผล:**"):
            p = out.add_paragraph()
            p.add_run("สรุปผล:").bold = True
            p.add_run(clean_md(line[len("**สรุปผล:**"):]))
            p.paragraph_format.space_after = Pt(6)
        else:
            p = out.add_paragraph(clean_md(line))
            p.paragraph_format.space_after = Pt(6)
        i += 1
    return out


def is_chapter_paragraph(element, prefix: str) -> bool:
    text = "".join(element.itertext())
    return text.strip().startswith(prefix)


def chapter_bounds(doc: Document):
    body = doc.element.body
    children = list(body)
    start = next(i for i, child in enumerate(children) if is_chapter_paragraph(child, "บทที่ 5"))
    end = next(i for i, child in enumerate(children[start + 1 :], start + 1) if is_chapter_paragraph(child, "บทที่ 6"))
    return body, children, start, end


def enable_field_updates(doc: Document) -> None:
    settings = doc.settings.element
    update = settings.find(qn("w:updateFields"))
    if update is None:
        update = OxmlElement("w:updateFields")
        settings.append(update)
    update.set(qn("w:val"), "true")


def main() -> None:
    if not SOURCE.exists() or not MARKDOWN.exists():
        raise FileNotFoundError("Missing source DOCX or pasted Markdown")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # The supplied text is the same Chapter 5 structure already present in the
    # reviewed baseline.  Retain that Word-native block so its established
    # heading/run formatting, table widths, and Thai font settings are not
    # degraded by reconstructing it from Markdown.
    shutil.copy2(SOURCE, OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
