"""Version tuple / artifact lineage / audit trail schemas (contract §5).

VersionTuple carries all fields across milestones — fields not produced until
M1/M2 (constraint_snapshot_id, prompt_version, model_snapshot,
agent_graph_version, cassette_id) are schema-present now with default None
(不简化只延后). Implementation of producers is phased; the schema is not cut.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from gameforge.contracts.versions import AUDIT_SCHEMA_VERSION, LINEAGE_SCHEMA_VERSION

ArtifactKind = Literal["ir_snapshot", "config_export", "checker_run", "playtest_trace", "patch"]


class VersionTuple(BaseModel):
    doc_version: str | None = None
    ir_snapshot_id: str | None = None
    constraint_snapshot_id: str | None = None
    prompt_version: str | None = None
    model_snapshot: str | None = None
    agent_graph_version: str | None = None
    tool_version: str | None = None
    env_contract_version: str | None = None
    seed: int | None = None
    cassette_id: str | None = None


class Artifact(BaseModel):
    artifact_id: str
    lineage_schema_version: str = LINEAGE_SCHEMA_VERSION
    kind: ArtifactKind
    version_tuple: VersionTuple
    lineage: list[str] = Field(default_factory=list)
    payload_hash: str | None = None
    created_at: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class AuditRecord(BaseModel):
    audit_schema_version: str = AUDIT_SCHEMA_VERSION
    seq: int
    actor: str
    action: str
    artifact_id: str | None = None
    ts: str
    content_hash: str
    prev_hash: str | None = None
