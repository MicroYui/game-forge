"""Clean Aureus base snapshot(s) for GameForge-Bench (M3a Task 6).

Loads the real `scenarios/defects/clean` CSV workbook — the M0b/M1 baseline
that `tests/apps/test_m1_acceptance.py` proves oracle-FP=0 against — into an
in-memory IR `Snapshot`. Injectors (`bench/inject.py`) mutate copies of it; the
`clean` samples themselves are the false-positive denominator (a checker that
flags a clean base is an oracle-FP). Lives in the package (not the test tree)
so `bench/corpus.py` can build the ≥500-sample corpus without importing tests.
"""
from __future__ import annotations

import json
from pathlib import Path

from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.spine.ingestion.csv_format import read_workbook
from gameforge.spine.ingestion.format_schema import FormatSchema
from gameforge.spine.ir.snapshot import Snapshot

_CLEAN_DIR = Path(__file__).resolve().parents[2] / "scenarios" / "defects" / "clean"


def clean_base() -> Snapshot:
    """A fresh clean Aureus outpost `Snapshot` (content-addressed, immutable by
    convention — a fresh object per call, cheap, no shared mutable state)."""
    with open(_CLEAN_DIR / "format_schema.json", encoding="utf-8") as fh:
        schema = FormatSchema.model_validate(json.load(fh))
    workbook = read_workbook(str(_CLEAN_DIR), schema)
    return AureusCsvAdapter().to_ir(workbook, file_ref=str(_CLEAN_DIR))
