"""Agent-role I/O contracts (PRD §7.5) — 6 roles, fields once (不简化只延后).

M2a implements extraction/triage/repair/consistency/generation; playtest I/O is
defined here now, implemented in M2b.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from gameforge.contracts.findings import Finding, Patch
from gameforge.contracts.versions import AGENT_IO_SCHEMA_VERSION

AgentRole = Literal[
    "extraction", "triage", "repair", "consistency", "generation", "playtest"
]


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
class DialogueNarrativeInput(BaseModel):
    dialogue: str
    narrative_constraint_ids: list[str] = Field(default_factory=list)


class ConsistencyHint(BaseModel):
    span: str
    issue: str
    is_suggestion: bool = True  # llm-assisted; human-confirmed, never authoritative


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
