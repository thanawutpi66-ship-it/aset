"""Insert the approved detailed Chapter 5 into the approved 00_00 base report.

Only Chapter 5 is taken from the detailed source.  Every other body element,
cover, section, header, footer, and style definition remains from 00_00.
"""

from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path
import re

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor


BASE = Path(r"C:\Users\boatl\Downloads\A19_capstone_design_2026_00_00 (แก้แล้ว).docx")
CHAPTER_SOURCE = Path(r"C:\Users\boatl\Downloads\A19_capstone_design_ch5_tables_detailed.docx")
OUTPUT = Path(r"C:\Users\boatl\Projects\ASET_BATT\output\docx\A19_capstone_design_00_00_ch5_detailed.docx")


def find_one(doc: Document, starts_with: str):
    matches = [p for p in doc.paragraphs if p.text.strip().startswith(starts_with)]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one paragraph beginning {starts_with!r}; found {len(matches)}")
    return matches[0]


def chapter_elements(doc: Document):
    ch5 = find_one(doc, "บทที่ 5 ")
    ch6 = find_one(doc, "บทที่ 6 ")
    children = list(doc.element.body)
    start = children.index(ch5._element)
    end = children.index(ch6._element)
    elements = children[start:end]
    drawings = sum(len(el.findall('.//' + qn('a:blip'))) for el in elements)
    if drawings:
        raise RuntimeError("Chapter 5 contains drawings; relationship copying is required")
    return [deepcopy(el) for el in elements]


def tables_between_chapters(doc: Document):
    ch5 = find_one(doc, "บทที่ 5 ")
    ch6 = find_one(doc, "บทที่ 6 ")
    children = list(doc.element.body)
    start = children.index(ch5._element)
    end = children.index(ch6._element)
    tables = []
    for element in children[start:end]:
        if element.tag != qn("w:tbl"):
            continue
        tables.append(next(table for table in doc.tables if table._tbl == element))
    return tables


def apply_base_table_style(table) -> None:
    """Normalize typography while preserving the source table's grid lines."""
    for row_index, row in enumerate(table.rows):
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.style = "Normal"
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(0)
                for run in paragraph.runs:
                    # The imported detail table carried a 7.5 pt direct format.
                    # 9 pt keeps it legible while retaining the approved
                    # portrait report geometry.
                    run.font.size = Pt(8.5 if row_index == 0 else 9)
                    if row_index == 0:
                        run.bold = True
                        run.font.color.rgb = RGBColor(255, 255, 255)
    # Repeat the header when Word carries the table onto another page.
    header_props = table.rows[0]._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    header_props.append(header)


def clone_heading_format(target, template) -> None:
    """Apply the approved 00_00 heading geometry and direct run format."""
    target_ppr = target._p.get_or_add_pPr()
    template_ppr = template._p.pPr
    if template_ppr is not None:
        target_ppr.getparent().replace(target_ppr, deepcopy(template_ppr))
    if not template.runs:
        return
    template_rpr = template.runs[0]._r.rPr
    if template_rpr is None:
        return
    for run in target.runs:
        run_rpr = run._r.get_or_add_rPr()
        run_rpr.getparent().replace(run_rpr, deepcopy(template_rpr))


def normalize_chapter5_headings(base: Document) -> None:
    base_ch4 = find_one(base, "บทที่ 4 ")
    base_41 = find_one(base, "4.1 ")
    ch5 = find_one(base, "บทที่ 5 ")
    clone_heading_format(ch5, base_ch4)
    children = list(base.element.body)
    start = children.index(ch5._element)
    ch6 = find_one(base, "บทที่ 6 ")
    end = children.index(ch6._element)
    for element in children[start + 1 : end]:
        if element.tag != qn("w:p"):
            continue
        paragraph = next((p for p in base.paragraphs if p._p == element), None)
        if paragraph is not None and re.match(r"^5\.[1-5]\s", paragraph.text.strip()):
            clone_heading_format(paragraph, base_41)


def request_field_update(doc: Document) -> None:
    settings = doc.settings.element
    update = settings.find(qn("w:updateFields"))
    if update is None:
        update = OxmlElement("w:updateFields")
        settings.append(update)
    update.set(qn("w:val"), "true")


def main() -> None:
    if not BASE.exists() or not CHAPTER_SOURCE.exists():
        raise FileNotFoundError("Missing approved base or detailed Chapter 5 source")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BASE, OUTPUT)
    base = Document(OUTPUT)
    detail = Document(CHAPTER_SOURCE)

    ch6 = find_one(base, "บทที่ 6 ")
    anchor = ch6._element
    for element in chapter_elements(detail):
        anchor.addprevious(element)

    normalize_chapter5_headings(base)

    # The detailed source has eight Chapter 5 tables.  Only Table 5.2 needs a
    # direct font normalization because it has seven narrow data columns.
    inserted_tables = tables_between_chapters(base)
    if len(inserted_tables) != 8 or (len(inserted_tables[2].rows), len(inserted_tables[2].columns)) != (16, 7):
        raise RuntimeError("Detailed Table 5.2 was not inserted as 16 rows x 7 columns")
    apply_base_table_style(inserted_tables[2])

    request_field_update(base)
    base.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
