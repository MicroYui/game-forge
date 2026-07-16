"""Blob-first terminal-publication staging contracts.

Terminal publication is deliberately split into three phases:

1. a short authority snapshot produces an immutable publication draft;
2. every output blob is content-addressed and verified outside the DB UoW;
3. a fresh authority snapshot must reproduce the exact draft digest before the
   transaction binds the staged generations and commits authority.

The models in this module carry no database or ObjectStore capability.  In
particular, :class:`StagedTerminalPublication` is the only blob input accepted by
the commit surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.lineage import ObjectLocation, ObjectRef
from gameforge.contracts.lineage import object_ref_for_bytes


@dataclass(frozen=True, slots=True)
class BlobMaterial:
    """One exact immutable byte payload required by a terminal draft."""

    slot: str
    payload: bytes
    expected_ref: ObjectRef

    def __post_init__(self) -> None:
        if not self.slot:
            raise ValueError("blob material slot must be non-empty")
        if self.expected_ref != object_ref_for_bytes(self.payload):
            raise ValueError("blob material expected_ref differs from its exact payload")


@dataclass(frozen=True, slots=True)
class StagedReceipt:
    """The exact backend generation produced for one draft material."""

    slot: str
    ref: ObjectRef
    location: ObjectLocation

    def __post_init__(self) -> None:
        if not self.slot:
            raise ValueError("staged receipt slot must be non-empty")
        if self.ref.key != self.location.key:
            raise ValueError("staged receipt ref/location keys differ")


@dataclass(frozen=True, slots=True)
class TerminalPublicationDraft:
    """A complete deterministic terminal-publication projection.

    ``operations`` are private, immutable publisher operations.  They remain
    opaque here so this transport type does not depend on transaction-bound
    repository protocols.  ``projection_digest`` binds their complete canonical
    projection and is re-derived from a fresh authority snapshot before commit.
    """

    publication_kind: str
    run_id: str
    attempt_no: int | None
    occurred_at: str
    projection_digest: str
    materials: tuple[BlobMaterial, ...]
    operations: tuple[object, ...]
    operation_projection: tuple[Mapping[str, object], ...]
    result_projection: Mapping[str, object]
    result: object

    def __post_init__(self) -> None:
        if not self.publication_kind or not self.run_id or not self.occurred_at:
            raise ValueError("terminal publication draft identity must be complete")
        slots = tuple(material.slot for material in self.materials)
        if len(slots) != len(set(slots)):
            raise ValueError("terminal publication draft blob slots must be unique")
        if len(self.operations) != len(self.operation_projection):
            raise ValueError("terminal publication operation projection is incomplete")
        expected = canonical_sha256(self.canonical_projection())
        if self.projection_digest != expected:
            raise ValueError("terminal publication projection digest is not canonical")

    def canonical_projection(self) -> Mapping[str, object]:
        return {
            "publication_kind": self.publication_kind,
            "run_id": self.run_id,
            "attempt_no": self.attempt_no,
            "occurred_at": self.occurred_at,
            "materials": tuple(
                {
                    "slot": material.slot,
                    "expected_ref": material.expected_ref.model_dump(mode="json"),
                }
                for material in self.materials
            ),
            "operations": self.operation_projection,
            "result": self.result_projection,
        }


@dataclass(frozen=True, slots=True)
class StagedTerminalPublication:
    """Verified blob receipts bound to one exact publication draft digest."""

    projection_digest: str
    receipts: tuple[StagedReceipt, ...]

    def __post_init__(self) -> None:
        if len(self.projection_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.projection_digest
        ):
            raise ValueError("staged projection digest must be canonical SHA-256")
        slots = tuple(receipt.slot for receipt in self.receipts)
        if len(slots) != len(set(slots)):
            raise ValueError("staged receipt slots must be unique")


class TerminalPublicationStager(Protocol):
    """Materialize complete drafts outside every database UnitOfWork."""

    def stage(
        self, drafts: tuple[TerminalPublicationDraft, ...]
    ) -> tuple[StagedTerminalPublication, ...]: ...


__all__ = [
    "BlobMaterial",
    "StagedReceipt",
    "StagedTerminalPublication",
    "TerminalPublicationDraft",
    "TerminalPublicationStager",
]
