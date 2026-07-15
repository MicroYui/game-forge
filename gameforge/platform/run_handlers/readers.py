"""Default input-artifact readers for the deterministic Run handlers.

Each handler is constructed with an :class:`ArtifactBlobReader`; these functions
turn the exact stored bytes of an input Artifact into the typed spine objects the
checkers / simulator need. They are the *default* (composition-root) loaders — a
handler may inject an alternative loader in a test or specialised wiring.

Formats are fail-closed and match the canonical on-store shapes:

* ``ir_snapshot``       → ``{meta_schema_version, entities, relations}`` (the
  content-addressed IR payload), parsed via the shared
  :func:`snapshot_from_canonical_view`.
* ``constraint_snapshot`` → ``{dsl_grammar_version, constraints:[Constraint,...]}``.
"""

from __future__ import annotations

from typing import Callable

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider

from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.run_handlers.base import ArtifactBlobReader, load_json_blob

SnapshotLoader = Callable[[ArtifactBlobReader, str], Snapshot]
ConstraintLoader = Callable[[ArtifactBlobReader, str], list[Constraint]]
NavLoader = Callable[[ArtifactBlobReader, str], "NavProvider | None"]


def load_snapshot(blobs: ArtifactBlobReader, artifact_id: str) -> Snapshot:
    """Parse the canonical IR payload of an ``ir_snapshot`` Artifact into a Snapshot."""

    view = load_json_blob(blobs, artifact_id)
    if not isinstance(view, dict):
        raise IntegrityViolation("ir_snapshot payload must be a canonical IR object")
    return snapshot_from_canonical_view(view)


def load_constraints(blobs: ArtifactBlobReader, artifact_id: str) -> list[Constraint]:
    """Parse a ``constraint_snapshot`` Artifact into typed ``Constraint``s."""

    payload = load_json_blob(blobs, artifact_id)
    if not isinstance(payload, dict) or set(payload) != {"dsl_grammar_version", "constraints"}:
        raise IntegrityViolation("constraint snapshot payload has the wrong shape")
    raw = payload["constraints"]
    if not isinstance(raw, list):
        raise IntegrityViolation("constraint snapshot constraints must be a list")
    return [Constraint.model_validate(item) for item in raw]


def load_nav(blobs: ArtifactBlobReader, artifact_id: str) -> NavProvider | None:
    """No navigation ground truth travels with an M4 ``ir_snapshot`` Artifact.

    Reachability checks that require spatial ground truth (e.g. the graph
    ``unreachable_target`` class) degrade to *not reported* rather than a false
    pass; every non-spatial defect class is still decided. A specialised wiring
    may inject a real nav loader.
    """

    return None


__all__ = [
    "ConstraintLoader",
    "NavLoader",
    "SnapshotLoader",
    "load_constraints",
    "load_nav",
    "load_snapshot",
]
