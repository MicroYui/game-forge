"""Task 11b — ``constraint_proposer@1`` (extraction proposer + compile oracle).

Drives the REAL M2 ``ExtractionProposer`` through the 11a model-bridge adapter with
a REPLAY cassette (``FakeModelBridge``); the deterministic compile oracle
(``parse_assert``) — not the LLM — decides which proposals survive. The resulting
``constraint_proposal[constraint-proposal@1]`` is only a DRAFT.
"""

from __future__ import annotations

import json

from gameforge.apps.worker.agent_runners import M2ConstraintProposalAgentRunner
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    ConstraintProposalProposePayloadV1,
    PreparedRunResult,
    PromptGoalBindingV1,
)
from gameforge.contracts.workflow import ConstraintProposalV1
from gameforge.platform.run_handlers.constraint_proposal import ConstraintProposalHandler
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    FakeModelBridge,
    build_context,
    execution_plan,
    resolved_binding,
)

CONSTRAINT_KIND = RunKindRef(kind="constraint_proposal.propose", version=1)
MODEL_REF = "anthropic/claude-opus-4-8/m2a@1"
DOC_ID = "artifact:doc"
GOAL_ID = "artifact:goal"
_HEX = "a" * 64

_PROPOSALS = json.dumps(
    [
        {
            "proposed_id": "C_cap",
            "kind": "numeric",
            "assert_expr": "reward_gold <= 80",
            "rationale": "side-quest reward cap",
        },
        {
            "proposed_id": "C_bad",
            "kind": "numeric",
            "assert_expr": "__import__('os').system('x')",
            "rationale": "not compilable",
        },
    ]
)


def _payload() -> ConstraintProposalProposePayloadV1:
    return ConstraintProposalProposePayloadV1(
        source_artifact_ids=(DOC_ID,),
        base_constraint_snapshot_artifact_id=None,
        domain_scope=DomainScope(domain_ids=("economy",)),
        authoring_goal=PromptGoalBindingV1(source_artifact_id=GOAL_ID, expected_payload_hash=_HEX),
        dsl_grammar_version="dsl@1",
        extraction_policy=ProfileRefV1(profile_id="extract", version=1),
    )


def _store() -> FakeArtifactStore:
    store = FakeArtifactStore()
    store.register(DOC_ID, {"doc_text": "Side quests reward at most 80 gold.", "doc_version": "v1"})
    return store


def _handler(store: FakeArtifactStore) -> ConstraintProposalHandler:
    return ConstraintProposalHandler(
        blobs=store, store=store, agent_runner=M2ConstraintProposalAgentRunner()
    )


def _context(bridge: FakeModelBridge):
    return build_context(
        params=_payload(),
        kind=CONSTRAINT_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/extraction_policy",
                profile_id="extract",
                version=1,
                kind="constraint_extraction",
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"extraction": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )


def test_constraint_proposal_drafted_is_agent_produced_draft() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_PROPOSALS,))
    outcome = _handler(store)(_context(bridge))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "constraint_proposal_drafted"
    assert outcome.findings == ()
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "constraint_proposal"
    assert primary.payload_schema_id == "constraint-proposal@1"
    # exactly ONE ordered LLM call went through the bridge.
    assert len(bridge.requests) == 1
    assert bridge.requests[0].idempotency_key == "run:1:1:model:1"

    proposal = ConstraintProposalV1.model_validate(
        json.loads(store.read_prepared(primary.object_ref))
    )
    # It is a DRAFT: agent-produced, so producer_run_id binds to THIS run.
    assert proposal.produced_by == "agent"
    assert proposal.producer_run_id == "run:1"
    assert proposal.revision == 1
    assert proposal.supersedes_artifact_id is None
    # only the compile-verified constraint survives the deterministic oracle.
    assert [c.id for c in proposal.constraints] == ["C_cap"]
    assert proposal.constraints[0].assert_ == "reward_gold <= 80"
    assert primary.meta["dropped_proposal_count"] == 1


def test_constraint_proposal_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _handler(store_a)(_context(FakeModelBridge(responses=(_PROPOSALS,))))
    out_b = _handler(store_b)(_context(FakeModelBridge(responses=(_PROPOSALS,))))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]
