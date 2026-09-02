"""Render each PDF page to a PNG for document-layout review."""

from __future__ import annotations

import sys
from pathlib import Path

import fitz
from PIL import Image, ImageDraw


def main() -> None:
    pdf_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = fitz.open(pdf_path)
    try:
        for index, page in enumerate(pdf, start=1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
            pixmap.save(out_dir / f"page-{index}.png")
    finally:
        pdf.close()

    pages = sorted(out_dir.glob("page-*.png"), key=lambda path: int(path.stem.split("-")[1]))
    contact_dir = out_dir / "contacts"
    contact_dir.mkdir(exist_ok=True)
    for start in range(0, len(pages), 4):
        group = pages[start : start + 4]
        thumbnails = []
        for path in group:
            image = Image.open(path).convert("RGB")
            image.thumbnail((560, 730))
            thumbnails.append((path, image.copy()))
            image.close()
        canvas = Image.new("RGB", (1160, 1540), "white")
        draw = ImageDraw.Draw(canvas)
        for offset, (path, image) in enumerate(thumbnails):
            col, row = offset % 2, offset // 2
            x, y = col * 580 + (560 - image.width) // 2, row * 770 + 30
            canvas.paste(image, (x, y))
            draw.text((col * 580 + 12, row * 770 + 8), path.stem, fill="black")
        canvas.save(contact_dir / f"contact-{start // 4 + 1}.png")
    print(f"pages={len(pages)}")


if __name__ == "__main__":
    main()
