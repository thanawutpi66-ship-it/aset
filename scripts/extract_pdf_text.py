"""Extract each input PDF with page boundaries for rubric/template review."""

from __future__ import annotations

import sys
from pathlib import Path

from pypdf import PdfReader


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    for raw_path in sys.argv[1:]:
        path = Path(raw_path)
        reader = PdfReader(path)
        print(f"\n===== {path.name} | pages={len(reader.pages)} =====")
        for number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            print(f"\n--- PAGE {number} ---\n{text}")


if __name__ == "__main__":
    main()
