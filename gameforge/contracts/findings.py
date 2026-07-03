"""Finding / Patch standard data formats (contract §6).

The most膨胀-prone producer↔consumer interface — every field defined once, now.
Implementation of producers (checkers M1, sim M1, playtest M2, repair M2) is
phased; the schema is NOT cut (不简化只延后).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from gameforge.contracts.versions import FINDING_SCHEMA_VERSION, PATCH_SCHEMA_VERSION

Severity = Literal["critical", "major", "minor"]
FindingSource = Literal["checker", "sim", "playtest", "llm"]
OracleType = Literal["deterministic", "llm-assisted", "simulation"]
FindingStatus = Literal["confirmed", "unproven", "dismissed", "fixed", "accepted_risk"]


class Finding(BaseModel):
    id: str
    finding_schema_version: str = FINDING_SCHEMA_VERSION
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
    "add_entity", "delete_entity", "set_entity_attr",
    "add_relation", "delete_relation", "set_relation_attr", "replace_subgraph",
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
    patch_schema_version: str = PATCH_SCHEMA_VERSION
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
