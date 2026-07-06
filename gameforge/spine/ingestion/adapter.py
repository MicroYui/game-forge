"""Adapter Protocol — pluggable config-format <-> Spec-IR translation (contract §12A.1).

Every format-specific adapter (`AureusCsvAdapter` now; other config formats /
open-source-game adapters land in M1) implements this Protocol so the
ingestion pipeline can `to_ir`/`from_ir` a workbook without caring which
concrete format produced it. Deliberately a structural (duck-typed) Protocol
rather than an ABC: adapters share no common state or behavior to inherit —
only the shape of `to_ir`/`from_ir` — so a Protocol is the honest interface
(mirrors `spine.ir.store.NavProvider`'s use of the same pattern).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from gameforge.spine.ir.snapshot import Snapshot


@runtime_checkable
class Adapter(Protocol):
    """Round-trips a typed workbook (`dict[sheet_name, list[row_dict]]`) <-> `Snapshot`."""

    format_id: str

    def to_ir(self, workbook: dict[str, list[dict]], file_ref: str) -> Snapshot: ...

    def from_ir(self, snapshot: Snapshot) -> dict[str, list[dict]]: ...
