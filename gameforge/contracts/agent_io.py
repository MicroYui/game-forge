"""Agent-role I/O contracts (PRD §7.5) — 6 roles, fields once (不简化只延后).

M2a implements extraction/triage/repair/consistency/generation; playtest I/O is
defined here now, implemented in M2b.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.findings import Finding, Patch
from gameforge.contracts.versions import (
    AGENT_IO_SCHEMA_VERSION,
    M2_AGENT_IO_SCHEMA_VERSION as M2_AGENT_IO_SCHEMA_VERSION,
)

AgentRole = Literal[
    "extraction", "triage", "repair", "consistency", "generation", "playtest"
]
NarrativeDefectClass = Literal[
    "character_violation",
    "spoiler",
    "faction_violation",
    "uniqueness_violation",
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgentNodeResult(BaseModel):
    agent_io_schema_version: str = AGENT_IO_SCHEMA_VERSION
    role: AgentRole
    fallback_taken: bool = False
    model_run_id: str
    request_hashes: list[str] = Field(default_factory=list)  # traces every LLM call
    produced: dict[str, Any] = Field(default_factory=dict)


# --- Extraction Proposer ---
class DesignDocInput(BaseModel):
    doc_text: str
    doc_version: str


class ConstraintProposal(BaseModel):
    proposed_id: str
    kind: str
    assert_expr: str
    rationale: str
    needs_human_authoring: bool = True  # LLM proposes; human authors authoritative


class EntityConstraintProposals(BaseModel):
    proposals: list[ConstraintProposal] = Field(default_factory=list)


# --- Defect Triager ---
class FindingsInput(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


class TriagedCluster(BaseModel):
    cluster_id: str
    finding_ids: list[str]
    priority: Literal["p0", "p1", "p2", "p3"]
    suspected_root_cause: str


class TriagedFindings(BaseModel):
    clusters: list[TriagedCluster] = Field(default_factory=list)


# --- Repair Drafter ---
class FindingContextInput(BaseModel):
    finding: Finding
    snapshot_id: str


class PatchDraft(BaseModel):
    patch: Patch
    search_steps: int = 0
    passed_verification: bool = False


# --- Consistency Assistant ---
class NarrativeConstraintInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    constraint_id: NonEmptyText
    entity_ids: list[NonEmptyText]
    statement: NonEmptyText

    @field_validator("entity_ids")
    @classmethod
    def validate_entity_ids(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("entity_ids must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("entity_ids must not contain duplicates")
        return value


class DialogueNarrativeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialogue: NonEmptyText
    narrative_constraints: list[NarrativeConstraintInput] = Field(default_factory=list)
    narrative_constraint_ids: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_constraint_channels(self) -> DialogueNarrativeInput:
        structured_ids = [item.constraint_id for item in self.narrative_constraints]
        if len(structured_ids) != len(set(structured_ids)):
            raise ValueError("narrative_constraints contain duplicate constraint IDs")
        if len(self.narrative_constraint_ids) != len(set(self.narrative_constraint_ids)):
            raise ValueError("narrative_constraint_ids contain duplicate IDs")
        if self.narrative_constraints and self.narrative_constraint_ids:
            raise ValueError(
                "narrative_constraints and narrative_constraint_ids cannot both be populated"
            )
        return self


class ConsistencyHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    defect_class: NarrativeDefectClass
    entity_ids: list[NonEmptyText]
    constraint_ids: list[NonEmptyText]
    span: NonEmptyText
    rationale: NonEmptyText
    is_suggestion: Literal[True] = True

    @field_validator("entity_ids", "constraint_ids")
    @classmethod
    def validate_grounding_ids(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("grounding ID lists must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("grounding ID lists must not contain duplicates")
        return value


class ConsistencyHints(BaseModel):
    hints: list[ConsistencyHint] = Field(default_factory=list)


# --- Content Generator ---
class DesignGoalInput(BaseModel):
    goal: str
    grounding_snapshot_id: str


class ContentProposal(BaseModel):
    proposed_ops: list[dict[str, Any]] = Field(default_factory=list)
    passed_gate: bool = False  # must pass checker+sim gate before candidacy


# --- Playtest Agent (I/O defined @M2a, impl @M2b) ---
class PlaytestInput(BaseModel):
    scenario: str
    seed: int


class PlaytestReport(BaseModel):
    action_trace: list[dict[str, Any]] = Field(default_factory=list)
    defect_findings: list[Finding] = Field(default_factory=list)
    completed: bool = False
