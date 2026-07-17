"""Checker interface (contract §6 / PRD §7.3).

A Checker is a stateless computation: IR snapshot -> [Finding]. Structural
properties are decided by graph algorithms; given correct constraints the
reported structural defects are true (algorithmic soundness). A `NavProvider`
may be injected so reachability uses the real navigation graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gameforge.contracts.findings import Finding
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider


@dataclass(frozen=True, slots=True)
class CheckerExecutionBinding:
    """Trusted execution identity for one direct or constraint-scoped checker."""

    wrapper_id: str
    native_id: str
    constraint_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.wrapper_id, str) or not self.wrapper_id.strip():
            raise ValueError("checker execution wrapper_id must be non-empty")
        if not isinstance(self.native_id, str) or not self.native_id.strip():
            raise ValueError("checker execution native_id must be non-empty")
        if self.constraint_id is not None and (
            not isinstance(self.constraint_id, str) or not self.constraint_id.strip()
        ):
            raise ValueError("checker execution constraint_id must be non-empty when present")


class Checker(Protocol):
    id: str

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]: ...
