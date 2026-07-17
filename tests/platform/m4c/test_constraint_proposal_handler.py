"""Task 11b — ``constraint_proposer@1`` (extraction proposer + compile oracle).

Drives the REAL M2 ``ExtractionProposer`` through the 11a model-bridge adapter with
a REPLAY cassette (``FakeModelBridge``); the deterministic compile oracle
(``parse_assert``) — not the LLM — decides which proposals survive. The resulting
``constraint_proposal[constraint-proposal@1]`` is only a DRAFT.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from gameforge.apps.worker.agent_runners import M2ConstraintProposalAgentRunner
from gameforge.contracts.agent_io import ConstraintProposal
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    ConstraintProposalProposePayloadV1,
    PreparedRunResult,
    PromptGoalBindingV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.workflow import ConstraintProposalV1
from gameforge.platform.run_handlers.constraint_proposal import (
    ConstraintExtractionExecutionConfig,
    ConstraintProposalHandler,
    ConstraintProposalOutcomeV1,
)
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
    store.register(DOC_ID, b"Side quests reward at most 80 gold.")
    store.register(GOAL_ID, b"Extract a deterministic gold reward cap.")
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
        version_tuple=VersionTuple(doc_version="v1", tool_version="handler@1"),
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
    assert primary.lineage == tuple(sorted((DOC_ID, GOAL_ID)))
    # exactly ONE ordered LLM call went through the bridge.
    assert len(bridge.requests) == 1
    assert bridge.requests[0].idempotency_scope == "run:run:1:attempt:1"
    assert bridge.requests[0].idempotency_key == "model:1"
    assert bridge.requests[0].source_artifact_ids == tuple(sorted((DOC_ID, GOAL_ID)))
    assert "Authoring goal:\nExtract a deterministic gold reward cap." in (
        bridge.requests[0].model_request.messages[-1].content
    )

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


def test_constraint_proposal_rejects_mismatched_profile_binding_before_model_call() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_PROPOSALS,))
    context = _context(bridge)
    context = replace(
        context,
        payload=context.payload.model_copy(
            update={
                "resolved_profiles": (
                    resolved_binding(
                        "/params/extraction_policy",
                        profile_id="other-extraction",
                        version=9,
                        kind="constraint_extraction",
                    ),
                )
            }
        ),
    )

    with pytest.raises(IntegrityViolation, match="exact Run binding"):
        _handler(store)(context)

    assert bridge.requests == []
    assert store.put_count == 0


def test_extraction_accepts_a_valid_source_larger_than_one_mib() -> None:
    store = _store()
    store.register(DOC_ID, b"A" * (1024 * 1024 + 1))
    bridge = FakeModelBridge(responses=(_PROPOSALS,))

    outcome = _handler(store)(_context(bridge))

    assert isinstance(outcome, PreparedRunResult)
    assert len(bridge.requests) == 1
    assert len(bridge.requests[0].model_request.messages[-1].content.encode("utf-8")) > 1024 * 1024


def test_extraction_prompt_profile_cap_rejects_before_bridge_or_object_write() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_PROPOSALS,))
    handler = ConstraintProposalHandler(
        blobs=store,
        store=store,
        agent_runner=M2ConstraintProposalAgentRunner(),
        execution_config_resolver=lambda _profile: ConstraintExtractionExecutionConfig(
            max_prompt_message_bytes=1
        ),
    )

    with pytest.raises(IntegrityViolation, match="profile byte limit"):
        handler(_context(bridge))

    assert bridge.requests == []
    assert store.put_count == 0


def test_constraint_proposal_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _handler(store_a)(_context(FakeModelBridge(responses=(_PROPOSALS,))))
    out_b = _handler(store_b)(_context(FakeModelBridge(responses=(_PROPOSALS,))))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]


@pytest.mark.parametrize(
    "response",
    (
        "not valid JSON",
        json.dumps(
            [
                {
                    "proposed_id": "C_rejected",
                    "kind": "numeric",
                    "assert_expr": "__import__('os').system('x')",
                    "rationale": "not in the deterministic DSL",
                }
            ]
        ),
    ),
)
def test_constraint_proposal_fallback_or_fully_dropped_response_creates_no_draft(
    response: str,
) -> None:
    store = _store()

    with pytest.raises(ValueError, match="constraint extraction"):
        _handler(store)(_context(FakeModelBridge(responses=(response,))))

    assert store.put_count == 0


def test_constraint_handler_rejects_an_empty_runner_outcome_before_sealing() -> None:
    class EmptyRunner:
        def run(self, request):
            del request
            return ConstraintProposalOutcomeV1(proposals=(), dropped=1)

    store = _store()
    handler = ConstraintProposalHandler(blobs=store, store=store, agent_runner=EmptyRunner())

    with pytest.raises(ValueError, match="no compile-valid constraints"):
        handler(_context(FakeModelBridge(responses=())))

    assert store.put_count == 0


def _accepted_proposal(*, assert_expr: str = "reward_gold <= 80") -> ConstraintProposal:
    return ConstraintProposal(
        proposed_id="C_cap",
        kind="numeric",
        assert_expr=assert_expr,
        rationale="bounded reward",
    )


class _StaticRunner:
    def __init__(self, outcome: ConstraintProposalOutcomeV1) -> None:
        self.outcome = outcome
        self.calls = 0

    def run(self, request):
        del request
        self.calls += 1
        return self.outcome


def test_handler_independently_compiles_runner_claims_before_writing() -> None:
    store = _store()
    runner = _StaticRunner(
        ConstraintProposalOutcomeV1(
            proposals=(_accepted_proposal(assert_expr="__import__('os').system('x')"),),
        )
    )

    with pytest.raises(ValueError, match="assert does not compile"):
        ConstraintProposalHandler(blobs=store, store=store, agent_runner=runner)(
            _context(FakeModelBridge())
        )

    assert runner.calls == 1
    assert store.put_count == 0


def test_source_byte_budget_rejects_before_agent_or_blob_write() -> None:
    store = _store()
    runner = _StaticRunner(ConstraintProposalOutcomeV1(proposals=(_accepted_proposal(),)))
    handler = ConstraintProposalHandler(
        blobs=store,
        store=store,
        agent_runner=runner,
        execution_config_resolver=lambda _profile: ConstraintExtractionExecutionConfig(
            max_source_artifact_count=64,
            max_source_artifact_bytes=4,
            max_total_input_bytes=64,
            max_proposal_count=8,
            max_output_bytes=1024,
        ),
    )

    with pytest.raises(IntegrityViolation, match="per-source byte budget"):
        handler(_context(FakeModelBridge()))

    assert runner.calls == 0
    assert store.put_count == 0


def test_proposal_and_output_budgets_reject_before_blob_write() -> None:
    store = _store()
    runner = _StaticRunner(
        ConstraintProposalOutcomeV1(
            proposals=(_accepted_proposal(),),
            dropped=1,
        )
    )
    count_bounded = ConstraintProposalHandler(
        blobs=store,
        store=store,
        agent_runner=runner,
        execution_config_resolver=lambda _profile: ConstraintExtractionExecutionConfig(
            max_proposal_count=1
        ),
    )

    with pytest.raises(ValueError, match="proposal count"):
        count_bounded(_context(FakeModelBridge()))

    assert store.put_count == 0

    output_bounded = ConstraintProposalHandler(
        blobs=store,
        store=store,
        agent_runner=_StaticRunner(ConstraintProposalOutcomeV1(proposals=(_accepted_proposal(),))),
        execution_config_resolver=lambda _profile: ConstraintExtractionExecutionConfig(
            max_output_bytes=1
        ),
    )
    with pytest.raises(IntegrityViolation, match="output exceeds"):
        output_bounded(_context(FakeModelBridge()))

    assert store.put_count == 0


def test_source_payload_is_read_once_and_hash_binding_uses_cached_bytes() -> None:
    class CountingStore(FakeArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.reads: dict[str, int] = {}

        def read_bytes(self, artifact_id: str) -> bytes:
            self.reads[artifact_id] = self.reads.get(artifact_id, 0) + 1
            return super().read_bytes(artifact_id)

    store = CountingStore()
    store.register(DOC_ID, b"Side quests reward at most 80 gold.")
    store.register(GOAL_ID, b"Extract a deterministic gold reward cap.")
    runner = _StaticRunner(ConstraintProposalOutcomeV1(proposals=(_accepted_proposal(),)))

    outcome = ConstraintProposalHandler(
        blobs=store,
        store=store,
        agent_runner=runner,
    )(_context(FakeModelBridge()))

    proposal = ConstraintProposalV1.model_validate(
        json.loads(store.read_prepared(outcome.artifacts[0].object_ref))
    )
    assert store.reads == {DOC_ID: 1, GOAL_ID: 1}
    assert (
        proposal.source_bindings[0].provenance_hash
        == hashlib.sha256(b"Side quests reward at most 80 gold.").hexdigest()
    )
