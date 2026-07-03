"""Checker interface (contract §6 / PRD §7.3).

A Checker is a stateless computation: IR snapshot -> [Finding]. Structural
properties are decided by graph algorithms; given correct constraints the
reported structural defects are true (algorithmic soundness). A `NavProvider`
may be injected so reachability uses the real navigation graph.
"""

from __future__ import annotations

from typing import Protocol

from gameforge.contracts.findings import Finding
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider


class Checker(Protocol):
    id: str

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]: ...
