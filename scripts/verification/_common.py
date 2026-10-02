"""Shared read-only CSV and JSON helpers for verification CLIs."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any


def read_csv(path: str | Path) -> tuple[list[dict[str, str]], list[str]]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"CSV does not exist: {source}")
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {source}")
        return list(reader), list(reader.fieldnames)


def read_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"JSON does not exist: {source}")
    value = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {source}")
    return value


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def write_result(result: dict[str, Any], json_out: str | None = None) -> None:
    rendered = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
    print(rendered)
    if json_out:
        Path(json_out).write_text(rendered + "\n", encoding="utf-8")


def meta_path_for(csv_path: str | Path) -> Path:
    return Path(str(csv_path) + ".meta.json")


def load_sidecar(csv_path: str | Path, explicit: str | None = None) -> dict[str, Any]:
    path = Path(explicit) if explicit else meta_path_for(csv_path)
    if not path.is_file():
        return {}
    return read_json(path)
