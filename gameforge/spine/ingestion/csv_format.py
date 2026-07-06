"""CSV workbook read/write with typed coercion (spine ingestion, Task 7).

Deterministic, stdlib-only round trip between on-disk CSV sheets and typed
in-memory workbook dicts, one dict-of-rows per SheetSchema. Depends only on
gameforge.contracts (the decimal-normalize float rule) and the stdlib
csv/json modules — no DB, no LLM, no runtime imports (spine boundary,
contract §1).

Round-trip determinism is the headline property this exists for:
`read_workbook(dir, schema)` after `write_workbook(dir, schema, x)` must equal
`x` at field level (feeds Task 8's lossless round-trip test), including the
canonical float rule so `1.10 == 1.1` never drifts across a write→read cycle.

Coercion rules (mirrored between encode/decode so they invert each other):
  - `int_list` / `str_list`: `;`-joined on write, split on `;` on read; the
    empty string is the empty list in both directions (independent of
    `required`, since `[]` is a valid list value, not a missing one).
  - other types on a non-required column: the empty string decodes to `None`
    (a missing optional cell), and `None` encodes back to the empty string.
    An empty cell on a REQUIRED column instead raises `ValueError` for every
    scalar type (`int`/`float`/`bool`/`json`), rather than silently
    coercing — validation of required-ness is SchemaRegistry's job, but a
    missing required cell should never masquerade as a valid value.
  - `int` / `float` / `bool` / `json` / `str`: coerced via the obvious
    stdlib call; `float` goes through `Decimal(str(v)).normalize()` (same
    rule as `contracts.canonical`) so equal-value floats serialize
    identically; `json` goes through plain `json.dumps(sort_keys=True,
    separators=(",", ":"))` — deterministic like `contracts.canonical_json`,
    but NOT that function, since `canonical_json` is a content-addressed-
    hashing helper that tags floats as strings and drops `None`-valued dict
    keys, both of which would break this format's lossless round-trip.
"""

from __future__ import annotations

import csv
import json
import os
from decimal import Decimal
from typing import Any

from gameforge.spine.ingestion.format_schema import ColumnSchema, FormatSchema

_LIST_TYPES = {"int_list", "str_list"}


def _decode_cell(raw: str, column: ColumnSchema) -> Any:
    if column.type in _LIST_TYPES:
        if raw == "":
            return []
        parts = raw.split(";")
        return [int(p) for p in parts] if column.type == "int_list" else parts
    if raw == "" and not column.required:
        return None
    if column.type == "int":
        return int(raw)
    if column.type == "float":
        return float(raw)
    if column.type == "bool":
        if raw == "":
            raise ValueError(f"required bool column {column.name!r} has an empty cell")
        return raw.strip().lower() == "true"
    if column.type == "json":
        return json.loads(raw)
    return raw  # "str"


def _encode_cell(value: Any, column: ColumnSchema) -> str:
    if value is None:
        return ""
    if column.type in _LIST_TYPES:
        return ";".join(str(v) for v in value)
    if column.type == "json":
        # Plain deterministic JSON, NOT canonical_json: canonical_json is a
        # content-addressed-hashing helper (contracts/canonical.py) that tags
        # floats as strings ("f:0.5") and drops None-valued dict keys — both
        # lossy transforms that break this format's round-trip guarantee.
        # Sorted keys + fixed separators still give us deterministic bytes
        # across writes without touching real JSON types.
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if column.type == "float":
        return format(Decimal(str(value)).normalize(), "f")
    if column.type == "bool":
        return "true" if value else "false"
    return str(value)  # "int" / "str"


def read_workbook(dir_path: str, schema: FormatSchema) -> dict[str, list[dict]]:
    """Read one CSV per sheet under `dir_path`, coercing cells per schema.

    A sheet whose `<name>.csv` file is missing reads as `[]` (not an error) —
    ingestion of a partial workbook directory is a normal/expected case.
    """
    workbook: dict[str, list[dict]] = {}
    for sheet in schema.sheets:
        path = os.path.join(dir_path, f"{sheet.name}.csv")
        if not os.path.isfile(path):
            workbook[sheet.name] = []
            continue
        rows: list[dict] = []
        with open(path, newline="", encoding="utf-8") as f:
            for raw_row in csv.DictReader(f):
                rows.append(
                    {col.name: _decode_cell(raw_row.get(col.name, ""), col) for col in sheet.columns}
                )
        workbook[sheet.name] = rows
    return workbook


def write_workbook(dir_path: str, schema: FormatSchema, workbook: dict[str, list[dict]]) -> None:
    """Write one CSV per sheet under `dir_path`, in schema column/row order.

    Uses `lineterminator="\\n"` (with the file opened `newline=""`, per the
    stdlib csv recipe) so output is byte-identical across platforms — no CRLF
    drift from the default `\\r\\n` writer behavior.
    """
    os.makedirs(dir_path, exist_ok=True)
    for sheet in schema.sheets:
        path = os.path.join(dir_path, f"{sheet.name}.csv")
        rows = workbook.get(sheet.name, [])
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow([col.name for col in sheet.columns])
            for row in rows:
                writer.writerow([_encode_cell(row.get(col.name), col) for col in sheet.columns])
