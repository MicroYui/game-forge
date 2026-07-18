"""Finding / Patch standard data formats (contract §6).

The most膨胀-prone producer↔consumer interface — every field defined once, now.
Implementation of producers (checkers M1, sim M1, playtest M2, repair M2) is
phased; the schema is NOT cut (不简化只延后).
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gameforge.contracts.canonical import typed_canonical_json
from gameforge.contracts.versions import (
    FINDING_PAYLOAD_SCHEMA_VERSION,
    FINDING_REVISION_SCHEMA_VERSION,
    FINDING_SCHEMA_VERSION,
    PATCH_SCHEMA_VERSION,
    PATCH_SCHEMA_VERSION_V2,
)

Severity = Literal["critical", "major", "minor"]
FindingSource = Literal["checker", "sim", "playtest", "llm"]
OracleType = Literal["deterministic", "llm-assisted", "simulation"]
FindingStatus = Literal["confirmed", "unproven", "dismissed", "fixed", "accepted_risk"]


class Finding(BaseModel):
    id: str
    finding_schema_version: Literal["finding@1"] = FINDING_SCHEMA_VERSION
    source: FindingSource
    producer_id: str
    producer_run_id: str
    oracle_type: OracleType
    defect_class: str
    severity: Severity
    snapshot_id: str
    entities: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    constraint_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    minimal_repro: dict[str, Any] = Field(default_factory=dict)
    status: FindingStatus
    confidence: float | None = None
    message: str
    created_at: str | None = None


TypedOpKind = Literal[
    "add_entity",
    "delete_entity",
    "set_entity_attr",
    "add_relation",
    "delete_relation",
    "set_relation_attr",
    "replace_subgraph",
]


class TypedOp(BaseModel):
    op_id: str
    op: TypedOpKind
    target: str  # entity_id / relation_id / path
    old_value: Any | None = None  # optimistic concurrency: apply only if still == old_value
    new_value: Any | None = None
    source_ref: dict[str, Any] | None = None


class Patch(BaseModel):
    id: str
    patch_schema_version: Literal["patch@1"] = PATCH_SCHEMA_VERSION
    base_snapshot_id: str
    target_snapshot_id: str
    expected_to_fix: list[str] = Field(default_factory=list)
    preconditions: list[dict[str, Any]] = Field(default_factory=list)
    side_effect_risk: str
    ops: list[TypedOp]
    produced_by: Literal["agent", "human"]
    producer_run_id: str
    rationale: str
    validation_status: str | None = None
    regression_status: str | None = None
    approval_status: str | None = None
    created_at: str | None = None


# Explicit legacy names let M4 code accept the old wire shapes without
# pretending that their mutable status fields are M4 workflow authority.
LegacyFindingV1 = Finding
PatchV1 = Patch

FindingPayloadSchemaVersion = Literal["finding-payload@1"]
FindingRevisionSchemaVersion = Literal["finding-revision@1"]
PatchV2SchemaVersion = Literal["patch@2"]


class FindingPayloadV1(BaseModel):
    """Identity-free semantic payload embedded by an immutable finding revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    payload_schema_version: FindingPayloadSchemaVersion = FINDING_PAYLOAD_SCHEMA_VERSION
    source: FindingSource
    producer_id: str
    producer_run_id: str
    oracle_type: OracleType
    defect_class: str
    severity: Severity
    snapshot_id: str
    entities: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    constraint_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    minimal_repro: dict[str, Any] = Field(default_factory=dict)
    status: FindingStatus
    confidence: float | None = None
    message: str


class FindingRevisionV1(BaseModel):
    """Stable finding series identity plus one immutable semantic revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    revision_schema_version: FindingRevisionSchemaVersion = FINDING_REVISION_SCHEMA_VERSION
    finding_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    supersedes_revision: int | None = Field(default=None, ge=1)
    created_at: str
    payload: FindingPayloadV1

    @model_validator(mode="after")
    def _validate_revision_chain(self) -> FindingRevisionV1:
        if self.revision == 1 and self.supersedes_revision is not None:
            raise ValueError("finding revision 1 cannot supersede another revision")
        if self.supersedes_revision is not None and self.supersedes_revision >= self.revision:
            raise ValueError("supersedes_revision must precede revision")
        return self


class FindingDigestPayloadV1(BaseModel):
    """The exact finding digest payload; persistence time is deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    revision_schema_version: FindingRevisionSchemaVersion = FINDING_REVISION_SCHEMA_VERSION
    finding_id: str
    revision: int
    supersedes_revision: int | None = None
    payload: FindingPayloadV1


def finding_revision_digest(revision: FindingRevisionV1 | Mapping[str, Any]) -> str:
    parsed = (
        revision
        if isinstance(revision, FindingRevisionV1)
        else FindingRevisionV1.model_validate(revision)
    )
    digest_payload = FindingDigestPayloadV1(
        finding_id=parsed.finding_id,
        revision=parsed.revision,
        supersedes_revision=parsed.supersedes_revision,
        payload=parsed.payload,
    )
    digest_input = (
        b"gameforge.finding-revision@1"
        + b"\x00"
        + typed_canonical_json(digest_payload.model_dump(mode="python")).encode("utf-8")
    )
    return hashlib.sha256(digest_input).hexdigest()


class PatchV2(BaseModel):
    """Immutable Patch payload; mutable workflow state lives in ApprovalItem."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    patch_schema_version: PatchV2SchemaVersion = PATCH_SCHEMA_VERSION_V2
    revision: int = Field(ge=1)
    supersedes_artifact_id: str | None = None
    base_snapshot_id: str = Field(min_length=1)
    target_snapshot_id: str = Field(min_length=1)
    expected_to_fix: list[str] = Field(default_factory=list)
    preconditions: list[dict[str, Any]] = Field(default_factory=list)
    side_effect_risk: str
    ops: list[TypedOp]
    produced_by: Literal["agent", "human"]
    producer_run_id: str | None = None
    rationale: str

    @model_validator(mode="after")
    def _validate_revision_and_producer(self) -> PatchV2:
        if self.revision == 1 and self.supersedes_artifact_id is not None:
            raise ValueError("patch revision 1 cannot supersede another artifact")
        if self.revision > 1 and not self.supersedes_artifact_id:
            raise ValueError("later patch revisions require supersedes_artifact_id")
        if self.produced_by == "agent" and not self.producer_run_id:
            raise ValueError("agent-produced patch requires producer_run_id")
        if self.produced_by == "human" and self.producer_run_id is not None:
            raise ValueError("human-produced patch must not carry producer_run_id")
        return self


class PatchView(BaseModel):
    """Read projection; statuses are derived from evidence and ApprovalItem."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    patch: PatchV2
    validation_status: str
    regression_status: str
    approval_status: str
    workflow_revision: int = Field(ge=1)


def parse_finding(payload: Mapping[str, Any]) -> LegacyFindingV1 | FindingRevisionV1:
    if "revision_schema_version" in payload:
        return FindingRevisionV1.model_validate(payload)
    return LegacyFindingV1.model_validate(payload)


def parse_patch(payload: Mapping[str, Any]) -> PatchV1 | PatchV2:
    if payload.get("patch_schema_version") == "patch@2":
        return PatchV2.model_validate(payload)
    return PatchV1.model_validate(payload)
