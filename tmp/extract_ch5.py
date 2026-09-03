import sys
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph


def iter_blocks(doc):
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, doc)
        elif child.tag.endswith("}tbl"):
            yield Table(child, doc)


sys.stdout.reconfigure(encoding="utf-8")
doc = Document(Path(sys.argv[1]))
chapter = sys.argv[2] if len(sys.argv) > 2 else "5"
next_chapter = str(int(chapter) + 1)
inside = False
for block in iter_blocks(doc):
    if isinstance(block, Paragraph):
        text = " ".join(block.text.split())
        if text.startswith(f"บทที่ {chapter}") or text.lower().startswith(f"chapter {chapter}"):
            inside = True
        if inside and (text.startswith(f"บทที่ {next_chapter}") or text.lower().startswith(f"chapter {next_chapter}")):
            break
        if inside and text:
            print(f"P[{block.style.name}] {text}")
    elif inside:
        print(f"TABLE {len(block.rows)}x{len(block.columns)}")
        for row in block.rows:
            print(" | ".join(" ".join(cell.text.split()) for cell in row.cells))
