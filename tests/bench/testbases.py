"""Shared NON-test helper for `tests/bench/`: a small, complete Aureus
`Snapshot` built from the real `scenarios/defects/clean` CSV workbook (M0b/M1
baseline — the same fixture `tests/apps/test_m1_acceptance.py` proves
oracle-FP=0 against), for `gameforge.bench.inject` injectors to mutate.

NOT a test module itself (no `test_*` functions — pytest never collects it);
imported by `tests/bench/test_inject_*.py`.
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
    """Build the clean Aureus outpost scenario as an in-memory IR `Snapshot`.

    A fresh `Snapshot` per call — `Snapshot` is immutable/content-addressed
    (contract §2.4), so repeated calls are cheap and never share mutable
    state across tests/injectors.
    """
    workbook_dir = str(_CLEAN_DIR)
    with open(_CLEAN_DIR / "format_schema.json", encoding="utf-8") as fh:
        schema = FormatSchema.model_validate(json.load(fh))
    workbook = read_workbook(workbook_dir, schema)
    return AureusCsvAdapter().to_ir(workbook, file_ref=workbook_dir)
