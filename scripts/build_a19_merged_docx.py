"""Create a consolidated A19 capstone report from the newer corrected draft.

The July draft is the formatting/data base.  This script applies only the
structural corrections confirmed by comparison with the earlier corrected
draft: restore heading styles, remove a duplicated Chapter 8, and make one
reference section at the end of the report.
"""

from __future__ import annotations

import shutil
import re
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn


SOURCE = Path(r"C:\Users\boatl\Downloads\A19_capstone_design_2026_07_24 (แก้แล้ว) (1).docx")
OUTPUT = Path(r"C:\Users\boatl\Projects\ASET_BATT\output\docx\A19_capstone_design_merged.docx")


def paragraphs_matching(doc: Document, text: str):
    return [p for p in doc.paragraphs if p.text.strip() == text]


def remove_element(element) -> None:
    element.getparent().remove(element)


def move_to_document_end(element, body) -> None:
    """Move a body element immediately before the section properties."""
    parent = element.getparent()
    parent.remove(element)
    children = list(body)
    section_properties_index = next(
        i for i, child in enumerate(children) if child.tag == qn("w:sectPr")
    )
    body.insert(section_properties_index, element)


def remove_from(element, body) -> None:
    """Remove an element and everything after it, but retain section metadata."""
    children = list(body)
    start = children.index(element)
    for child in children[start:]:
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def request_field_update(doc: Document) -> None:
    settings = doc.settings.element
    update = settings.find(qn("w:updateFields"))
    if update is None:
        update = OxmlElement("w:updateFields")
        settings.append(update)
    update.set(qn("w:val"), "true")


def accept_tracked_changes(doc: Document) -> None:
    """Keep insertions and remove deleted text so no redlines ship to the user."""
    root = doc.element
    for deletion in list(root.iter(qn("w:del"))):
        deletion.getparent().remove(deletion)
    for insertion in list(root.iter(qn("w:ins"))):
        parent = insertion.getparent()
        position = parent.index(insertion)
        for child in list(insertion):
            insertion.remove(child)
            parent.insert(position, child)
            position += 1
        parent.remove(insertion)


def remove_red_review_notes(doc: Document) -> None:
    """Remove the red, non-report reviewer note embedded in the source body."""
    root = doc.element
    paragraphs_to_remove = []
    for color in root.iter(qn("w:color")):
        if color.get(qn("w:val"), "").lower() != "ff0000":
            continue
        paragraph = color
        while paragraph is not None and paragraph.tag != qn("w:p"):
            paragraph = paragraph.getparent()
        if paragraph is not None and "***" in "".join(paragraph.itertext()):
            paragraphs_to_remove.append(paragraph)
    for paragraph in dict.fromkeys(paragraphs_to_remove):
        paragraph.getparent().remove(paragraph)


def renumber_reference(paragraph, number: int) -> None:
    """Renumber the leading bracket while retaining run-level formatting."""
    for run in paragraph.runs:
        updated, changes = re.subn(r"^\[\d+\]", f"[{number}]", run.text, count=1)
        if changes:
            run.text = updated
            return
    raise RuntimeError(f"Reference has no leading number: {paragraph.text}")


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, OUTPUT)
    doc = Document(OUTPUT)
    body = doc.element.body

    accept_tracked_changes(doc)
    remove_red_review_notes(doc)

    # Restore the hierarchy used by the original corrected draft.
    executive_summary = paragraphs_matching(doc, "บทสรุปผู้บริหาร / Executive Summary")[0]
    executive_summary.style = doc.styles["Heading 1"]

    impact_heading = paragraphs_matching(
        doc, "6.4 ผลกระทบทางสังคมและสิ่งแวดล้อม / Social and Environmental Impact"
    )[0]
    impact_heading.style = doc.styles["Heading 2"]

    # Retain the fuller first Chapter 8 and remove the later duplicated version,
    # including its redundant objective table.
    chapter_eights = paragraphs_matching(doc, "บทที่ 8 บทสรุปและข้อเสนอแนะ")
    if len(chapter_eights) != 1:
        raise RuntimeError("Expected exactly one Thai-only duplicate Chapter 8 heading")
    remove_from(chapter_eights[0]._element, body)

    # Consolidate six source references into one final heading at the end.
    title = paragraphs_matching(doc, "เอกสารอ้างอิง / References")[0]
    title.style = doc.styles["Heading 1"]
    title.paragraph_format.page_break_before = True

    entries = [p for p in doc.paragraphs if p.text.strip().startswith("[")]
    if len(entries) != 6:
        raise RuntimeError(f"Expected 6 bibliography entries, found {len(entries)}")
    secondary_titles = paragraphs_matching(doc, "เอกสารอ้างอิง")
    if len(secondary_titles) != 1:
        raise RuntimeError("Expected exactly one redundant reference heading")

    for number, entry in enumerate(entries, start=1):
        renumber_reference(entry, number)

    move_to_document_end(title._element, body)
    for entry in entries:
        move_to_document_end(entry._element, body)
    remove_element(secondary_titles[0]._element)

    request_field_update(doc)
    doc.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
