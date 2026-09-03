"""Single entry point for the user-facing experiment-report package.

The two files intentionally share one config, analysis result, and CSV session:
the DOCX remains editable for Chapter 5 while the PDF is the fixed submission
copy.  Keeping orchestration here prevents their UI actions from drifting into
two unrelated report workflows.
"""

from __future__ import annotations

import os
from pathlib import Path


def _base_path(path: str) -> Path:
    output = Path(path).expanduser().resolve()
    if output.suffix.lower() in {".docx", ".pdf"}:
        output = output.with_suffix("")
    return output


def generate_report_package(path: str, config, estimator=None, analysis=None,
                            csv_path: str | None = None) -> dict:
    """Create matching editable Word and submission PDF reports.

    Returns paths and non-fatal warnings.  PDF generation remains available if
    Word support is accidentally absent, but a clear warning is returned rather
    than exposing the raw ``No module named docx`` traceback to the operator.
    """
    base = _base_path(path)
    base.parent.mkdir(parents=True, exist_ok=True)
    docx_path = str(base.with_suffix(".docx"))
    pdf_path = str(base.with_suffix(".pdf"))
    warnings: list[str] = []
    word_created = False

    try:
        import docx  # noqa: F401 - preflight gives an operator-facing error.
        from aset_batt.storage.word_report import generate_word_report
        generate_word_report(docx_path, config, estimator, analysis=analysis,
                             csv_path=csv_path)
        word_created = True
    except ModuleNotFoundError as exc:
        if exc.name in {"docx", "python-docx"}:
            warnings.append(
                "Word report was not created because python-docx is missing. "
                "Install the project requirements, then export again.")
        else:
            raise

    from aset_batt.storage.report_generator import generate_pdf_report
    generate_pdf_report(pdf_path, config, estimator, analysis=analysis,
                        csv_path=csv_path)

    return {
        "docx_path": docx_path if word_created else None,
        "pdf_path": pdf_path,
        "warnings": warnings,
    }
