"""Task 11b — ``generation_proposer@1`` (content generator + deterministic gate).

Drives the REAL M2 ``ContentGenerator`` + ``gate_proposal`` through the 11a
model-bridge adapter with a REPLAY cassette. The LLM only PROPOSES ops; the
deterministic gate (spine graph checker + economy sim over the applied preview)
decides pass/reject. Gate PASS yields the primary patch + preview + config exports
+ gate evidence; gate REJECT yields an evidence-only failure with NO config export
and NO workflow subject.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

import gameforge.apps.worker.agent_runners as agent_runners_mod
from gameforge.apps.worker.agent_runners import M2GenerationAgentRunner
from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.config_export import ConfigExportFileV1, ConfigExportPackageV1
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import Finding, PatchV2, TypedOp
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    GenerationProposePayloadV1,
    PreparedRunFailure,
    PreparedRunResult,
    PromptGoalBindingV1,
    RefReadBindingV1,
    ResolvedArtifactRequirementV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.platform.run_handlers.generation import (
    GenerationGateOutcomeV1,
    GenerationExecutionConfig,
    GenerationProposalHandler,
    config_export_profile_binding,
)
from gameforge.platform.run_handlers.readers import load_snapshot
from gameforge.platform.publication.payload_schema import decode_and_validate_artifact_payload
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    FakeModelBridge,
    build_context,
    execution_plan,
    resolved_binding,
    resolved_policy_snapshot,
    snapshot_bytes,
)

GENERATION_KIND = RunKindRef(kind="generation.propose", version=1)
MODEL_REF = "anthropic/claude-opus-4-8/m2a@1"
SNAPSHOT_ID = "artifact:snapshot"
CONSTRAINT_ID = "artifact:constraints"
GOAL_ID = "artifact:goal"
_HEX = "a" * 64

# A base with a real gold faucet (mob drops gold) but no dangling references.
_BENIGN_OPS = json.dumps(
    [{"op": "set_entity_attr", "target": "mob.gold_max", "new_value": 120, "op_id": "g0"}]
)
# Introduces a NEW dangling reference (src endpoint missing) -> gate must reject.
_BAD_OPS = json.dumps(
    [
        {
            "op": "add_relation",
            "target": "bad",
            "new_value": {"type": "DROPS_FROM", "src_id": "ghost:missing", "dst_id": "gold"},
            "op_id": "g0",
        }
    ]
)
_MALFORMED_OPS = json.dumps([{"op": "run_shell", "target": "gold", "new_value": 1, "op_id": "g0"}])
_INAPPLICABLE_OPS = json.dumps(
    [
        {
            "op": "set_entity_attr",
            "target": "mob.gold_max",
            "old_value": 999,
            "new_value": 120,
            "op_id": "g0",
        }
    ]
)


def _base_snapshot() -> bytes:
    gold = Entity(id="gold", type=NodeType.CURRENCY, attrs={})
    mob = Entity(
        id="mob",
        type=NodeType.MONSTER,
        attrs={"gold_min": 60, "gold_max": 140, "kills_per_tick": 10},
    )
    drop = Relation(id="drop", type=EdgeType.DROPS_FROM, src_id="mob", dst_id="gold")
    return snapshot_bytes([gold, mob], [drop])


def _navigation_snapshot(*, talk_step_count: int) -> bytes:
    quest = Entity(id="quest:nav", type=NodeType.QUEST, attrs={})
    giver = Entity(id="npc:giver", type=NodeType.NPC, attrs={})
    base_step = Entity(
        id="step:base",
        type=NodeType.QUEST_STEP,
        attrs={"kind": "kill", "target": "mob"},
    )
    steps = [
        Entity(
            id=f"step:talk:{index}",
            type=NodeType.QUEST_STEP,
            attrs={"kind": "talk", "target": giver.id},
        )
        for index in range(talk_step_count)
    ]
    relations = [
        Relation(
            id="quest:giver",
            type=EdgeType.STARTS_AT,
            src_id=quest.id,
            dst_id=giver.id,
        ),
        Relation(
            id="quest:base",
            type=EdgeType.HAS_STEP,
            src_id=quest.id,
            dst_id=base_step.id,
        ),
        *(
            Relation(
                id=f"quest:talk:{index}",
                type=EdgeType.HAS_STEP,
                src_id=quest.id,
                dst_id=step.id,
            )
            for index, step in enumerate(steps)
        ),
    ]
    return snapshot_bytes([quest, giver, base_step, *steps], relations)


def _simulation_finding(snapshot_id: str, observed: float = 4.0) -> Finding:
    return Finding(
        id=f"sim:inflation:{snapshot_id[:12]}",
        source="sim",
        producer_id="economy_sim",
        producer_run_id="sim:run",
        oracle_type="simulation",
        defect_class="inflation_rate",
        severity="major",
        snapshot_id=snapshot_id,
        evidence={"invariant": "inflation_rate", "observed": observed},
        minimal_repro={"invariant": "inflation_rate"},
        status="confirmed",
        message="inflation exceeds the profile threshold",
    )


class _FakeConfigExporter:
    """Return a minimal valid config-export package bound to the export profile."""

    def export(
        self,
        *,
        export_profile,
        export_profile_binding,
        run_kind,
        llm_execution_mode,
        preview_snapshot_id,
        preview_payload,
        constraint_snapshot_artifact_id,
        constraints,
    ) -> ConfigExportPackageV1:
        del export_profile_binding, run_kind, llm_execution_mode, preview_payload, constraints
        content = json.dumps({"preview": preview_snapshot_id}, sort_keys=True).encode("utf-8")
        return ConfigExportPackageV1(
            export_profile=export_profile,
            target_environment_profile=ProfileRefV1(profile_id="aureus-env", version=1),
            env_contract_version="env@1",
            source_preview_artifact_id="preview:local",
            constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
            format_schema_id="aureus-csv@1",
            files=(
                ConfigExportFileV1(
                    relative_path="config/outpost.json",
                    media_type="application/json",
                    content_sha256=sha256_lowerhex(content),
                    size_bytes=len(content),
                    content_bytes=content,
                ),
            ),
        )


class _SemanticCollisionChecker:
    """Emit the same class/entity on a different relation after the patch."""

    id = "semantic-collision"

    def __init__(self, base_snapshot_id: str) -> None:
        self._base_snapshot_id = base_snapshot_id

    def check(self, snapshot, nav=None):
        del nav
        relation_id = (
            "relation:baseline"
            if snapshot.snapshot_id == self._base_snapshot_id
            else "relation:introduced"
        )
        return [
            Finding(
                id=f"finding:{relation_id}",
                source="checker",
                producer_id=self.id,
                producer_run_id="run:checker",
                oracle_type="deterministic",
                defect_class="semantic_collision",
                severity="major",
                snapshot_id=snapshot.snapshot_id,
                entities=["mob"],
                # Graph-style checkers may encode the exact edge only in evidence.
                relations=[],
                evidence={"relation_id": relation_id},
                minimal_repro={"entity_id": "mob"},
                status="confirmed",
                message=relation_id,
            )
        ]


def _payload() -> GenerationProposePayloadV1:
    return GenerationProposePayloadV1(
        base_snapshot_artifact_id=SNAPSHOT_ID,
        constraint_snapshot_artifact_id=CONSTRAINT_ID,
        findings=(),
        objective_goal=PromptGoalBindingV1(source_artifact_id=GOAL_ID, expected_payload_hash=_HEX),
        domain_scope=DomainScope(domain_ids=("content",)),
        target=RefReadBindingV1(ref_name="patch:main", expected_ref=None),
        generation_policy=ProfileRefV1(profile_id="gen", version=1),
        candidate_export_profiles=(ProfileRefV1(profile_id="csv", version=1),),
    )


def _store() -> FakeArtifactStore:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _base_snapshot())
    store.register(CONSTRAINT_ID, {"dsl_grammar_version": "dsl@1", "constraints": []})
    store.register(GOAL_ID, b"tune the outpost faucet")
    return store


def _execution_config(
    *, max_prompt_message_bytes: int = 16 * 1024 * 1024
) -> GenerationExecutionConfig:
    return GenerationExecutionConfig(
        max_prompt_message_bytes=max_prompt_message_bytes,
        max_constraint_count=256,
        max_work_units=2_000_000,
        gate_simulation_seed=0,
        gate_simulation_population=30,
        gate_simulation_horizon_steps=120,
        max_simulation_work_units=2_000_000,
    )


def _handler(
    store: FakeArtifactStore,
    *,
    execution_config: GenerationExecutionConfig | None = None,
) -> GenerationProposalHandler:
    return GenerationProposalHandler(
        blobs=store,
        store=store,
        agent_runner=M2GenerationAgentRunner(checker_factory=lambda snap, cons: [GraphChecker()]),
        config_exporter=_FakeConfigExporter(),
        execution_config_resolver=lambda profile: execution_config or _execution_config(),
    )


def _context(
    bridge: FakeModelBridge,
    *,
    payload: GenerationProposePayloadV1 | None = None,
):
    actual_payload = payload or _payload()
    requirements = tuple(
        ResolvedArtifactRequirementV1(
            requirement_id=f"profile-gate:{rule}",
            outcome_rule_id=rule,
            artifact_kind=kind,
            payload_schema_id=schema,
            producer_profile_field_path="/params/generation_policy",
            ordinal=1,
        )
        for rule, kind, schema in (
            ("checker", "checker_run", "checker-report@1"),
            ("simulation", "simulation_run", "simulation-result@1"),
            ("review", "review_report", "review@1"),
        )
    )
    return build_context(
        params=actual_payload,
        kind=GENERATION_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/generation_policy", profile_id="gen", version=1, kind="generation"
            ),
            *(
                resolved_binding(
                    f"/params/candidate_export_profiles/{index}",
                    profile_id=profile.profile_id,
                    version=profile.version,
                    kind="config_export",
                )
                for index, profile in enumerate(actual_payload.candidate_export_profiles)
            ),
        ),
        resolved_policy_snapshots=(
            resolved_policy_snapshot("generation-gate", "/params/generation_policy", requirements),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"generation": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
        version_tuple=VersionTuple(
            doc_version="goal@1",
            constraint_snapshot_id="constraint:snapshot:1",
            tool_version="handler@1",
        ),
    )


def _kinds(outcome) -> list[str]:
    return [artifact.kind for artifact in outcome.artifacts]


def test_config_export_preflight_rejects_a_missing_exact_collection_binding() -> None:
    context = _context(FakeModelBridge(responses=(_BENIGN_OPS,)))
    context = replace(
        context,
        payload=context.payload.model_copy(
            update={
                "resolved_profiles": tuple(
                    binding
                    for binding in context.payload.resolved_profiles
                    if binding.expected_profile_kind != "config_export"
                )
            }
        ),
    )

    with pytest.raises(IntegrityViolation, match="exact Run binding"):
        config_export_profile_binding(
            context, index=0, profile=_payload().candidate_export_profiles[0]
        )


@pytest.mark.parametrize(
    "field_path",
    ("/params/generation_policy", "/params/candidate_export_profiles/0"),
)
def test_generation_rejects_mismatched_profile_binding_before_model_call(
    field_path: str,
) -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_BENIGN_OPS,))
    context = _context(bridge)
    bindings = tuple(
        resolved_binding(
            binding.field_path,
            profile_id=(
                "other" if binding.field_path == field_path else binding.profile.profile_id
            ),
            version=(9 if binding.field_path == field_path else binding.profile.version),
            kind=binding.expected_profile_kind,
        )
        for binding in context.payload.resolved_profiles
    )
    context = replace(
        context,
        payload=context.payload.model_copy(update={"resolved_profiles": bindings}),
    )

    with pytest.raises(IntegrityViolation, match="exact Run binding"):
        _handler(store)(context)

    assert bridge.requests == []
    assert store.put_count == 0


def test_generation_gate_pass_emits_patch_preview_config_and_gate_evidence() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_BENIGN_OPS,))
    outcome = _handler(store)(_context(bridge))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "generation_gate_passed"
    # exactly one LLM proposal call went through the bridge.
    assert len(bridge.requests) == 1
    assert bridge.requests[0].source_artifact_ids == tuple(sorted((GOAL_ID, SNAPSHOT_ID)))
    assert "Design goal: tune the outpost faucet" in (
        bridge.requests[0].model_request.messages[-1].content
    )

    kinds = _kinds(outcome)
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "patch"
    assert primary.payload_schema_id == "patch@2"
    assert kinds.count("ir_snapshot") == 1  # the non-authoritative preview
    # one config_export per candidate_export_profile, bound to /export_profile.
    assert kinds.count("config_export") == 1
    config = next(a for a in outcome.artifacts if a.kind == "config_export")
    assert config.meta["export_profile"] == {"profile_id": "csv", "version": 1}
    assert config.version_tuple.doc_version == "goal@1"
    # gate checker/sim/review evidence (one resolved requirement each).
    assert kinds.count("checker_run") == 1
    assert kinds.count("simulation_run") == 1
    assert kinds.count("review_report") == 1
    assert {
        artifact.meta["requirement_id"]
        for artifact in outcome.artifacts
        if artifact.kind in {"checker_run", "simulation_run", "review_report"}
    } == {
        "profile-gate:checker",
        "profile-gate:simulation",
        "profile-gate:review",
    }
    simulation = next(
        artifact for artifact in outcome.artifacts if artifact.kind == "simulation_run"
    )
    simulation_blob = store.read_prepared(simulation.object_ref)
    decoded_simulation = decode_and_validate_artifact_payload(
        payload_schema_id="simulation-result@1",
        blob=simulation_blob,
    )
    assert decoded_simulation["seed"] == 0
    assert decoded_simulation["replication_count"] == 30
    assert decoded_simulation["horizon_steps"] == 120
    assert simulation.version_tuple.seed == 0
    assert {
        artifact.version_tuple.seed
        for artifact in outcome.artifacts
        if artifact.kind in {"checker_run", "review_report"}
    } == {None}
    for evidence in (
        artifact
        for artifact in outcome.artifacts
        if artifact.kind in {"checker_run", "simulation_run", "review_report"}
    ):
        evidence_payload = decode_and_validate_artifact_payload(
            payload_schema_id=evidence.payload_schema_id,
            blob=store.read_prepared(evidence.object_ref),
        )
        assert evidence_payload["requirement_id"] == evidence.meta["requirement_id"]
        finding_groups = (
            "findings",
            "deterministic_findings",
            "llm_assisted_findings",
            "simulation_findings",
            "unproven_findings",
        )
        assert {
            finding["producer_run_id"]
            for field in finding_groups
            for finding in evidence_payload.get(field, [])
        } <= {"run:1"}

    # the (agent) patch's producer_run_id binds to THIS run.
    patch = PatchV2.model_validate(json.loads(store.read_prepared(primary.object_ref)))
    assert patch.produced_by == "agent"
    assert patch.producer_run_id == "run:1"
    assert patch.revision == 1
    assert primary.version_tuple.constraint_snapshot_id == "constraint:snapshot:1"
    preview = next(artifact for artifact in outcome.artifacts if artifact.kind == "ir_snapshot")
    assert preview.version_tuple.constraint_snapshot_id is None


def test_generation_prompt_profile_cap_rejects_before_bridge_or_object_write() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_BENIGN_OPS,))

    with pytest.raises(IntegrityViolation, match="profile byte limit"):
        _handler(
            store,
            execution_config=_execution_config(max_prompt_message_bytes=1),
        )(_context(bridge))

    assert bridge.requests == []
    assert store.put_count == 0


def test_generation_gate_reject_is_evidence_only_failure_without_subject() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_BAD_OPS,))
    outcome = _handler(store)(_context(bridge))

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "generation_gate_rejected"
    assert outcome.failure_class == "business_rule"


def test_generation_parse_fallback_cannot_turn_an_empty_proposal_into_gate_pass() -> None:
    store = _store()
    outcome = _handler(store)(_context(FakeModelBridge(responses=("not valid JSON",))))
    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "generation_gate_rejected"
    assert outcome.intrinsic_retry_eligible is False

    kinds = _kinds(outcome)
    # evidence-only: rejected patch + preview + gate evidence, NO config_export.
    assert kinds.count("patch") == 1
    assert kinds.count("ir_snapshot") == 1
    assert "config_export" not in kinds
    assert kinds.count("checker_run") == 1
    assert kinds.count("simulation_run") == 1
    assert kinds.count("review_report") == 1

    # the rejected Patch is retained for review and still binds producer_run_id to
    # this terminal non-success generation Run (the frozen failure-code exception).
    patch = next(a for a in outcome.artifacts if a.kind == "patch")
    payload = PatchV2.model_validate(json.loads(store.read_prepared(patch.object_ref)))
    assert payload.produced_by == "agent"
    assert payload.producer_run_id == "run:1"
    checker = next(artifact for artifact in outcome.artifacts if artifact.kind == "checker_run")
    checker_payload = json.loads(store.read_prepared(checker.object_ref))
    rejection = next(
        finding
        for finding in checker_payload["findings"]
        if finding["defect_class"] == "invalid_generation_proposal"
    )
    assert rejection["evidence"] == {"reason_code": "model_response_unparseable"}


@pytest.mark.parametrize(
    ("response", "reason_code", "expected_op_count"),
    (
        (_MALFORMED_OPS, "malformed_ops", 0),
        (_INAPPLICABLE_OPS, "inapplicable_ops", 1),
    ),
)
def test_generation_structural_rejection_retains_typed_evidence_only_outcome(
    response: str,
    reason_code: str,
    expected_op_count: int,
) -> None:
    store = _store()

    outcome = _handler(store)(_context(FakeModelBridge(responses=(response,))))

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "generation_gate_rejected"
    assert "config_export" not in _kinds(outcome)
    patch_artifact = next(artifact for artifact in outcome.artifacts if artifact.kind == "patch")
    patch = PatchV2.model_validate(json.loads(store.read_prepared(patch_artifact.object_ref)))
    assert len(patch.ops) == expected_op_count
    assert patch.target_snapshot_id == next(
        artifact.version_tuple.ir_snapshot_id
        for artifact in outcome.artifacts
        if artifact.kind == "ir_snapshot"
    )
    assert patch.target_snapshot_id == patch.base_snapshot_id
    checker = next(artifact for artifact in outcome.artifacts if artifact.kind == "checker_run")
    checker_payload = json.loads(store.read_prepared(checker.object_ref))
    rejection = next(
        finding
        for finding in checker_payload["findings"]
        if finding["defect_class"] == "invalid_generation_proposal"
    )
    assert rejection["evidence"] == {"reason_code": reason_code}
    review = next(artifact for artifact in outcome.artifacts if artifact.kind == "review_report")
    review_payload = json.loads(store.read_prepared(review.object_ref))
    assert any(
        finding["evidence"] == {"reason_code": reason_code}
        for finding in review_payload["deterministic_findings"]
    )


def test_generation_gate_does_not_mask_a_new_relation_with_same_class_and_entity() -> None:
    store = _store()

    def checker_factory(snapshot, constraints):
        del constraints
        return [_SemanticCollisionChecker(snapshot.snapshot_id)]

    handler = GenerationProposalHandler(
        blobs=store,
        store=store,
        agent_runner=M2GenerationAgentRunner(checker_factory=checker_factory),
        config_exporter=_FakeConfigExporter(),
        execution_config_resolver=lambda profile: _execution_config(),
    )

    outcome = handler(_context(FakeModelBridge(responses=(_BENIGN_OPS,))))

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "generation_gate_rejected"


def test_generation_gate_rejects_new_non_collapse_simulation_invariant(monkeypatch) -> None:
    store = _store()
    exact_base_id = load_snapshot(store, SNAPSHOT_ID).snapshot_id

    def findings(_result, snapshot_id, model):
        del model
        return [] if snapshot_id == exact_base_id else [_simulation_finding(snapshot_id)]

    monkeypatch.setattr(agent_runners_mod, "to_findings", findings)

    outcome = _handler(store)(_context(FakeModelBridge(responses=(_BENIGN_OPS,))))

    assert isinstance(outcome, PreparedRunFailure)
    simulation = next(a for a in outcome.artifacts if a.kind == "simulation_run")
    payload = json.loads(store.read_prepared(simulation.object_ref))
    assert [item["defect_class"] for item in payload["findings"]] == ["inflation_rate"]


def test_generation_gate_allows_existing_simulation_predicate_observation_change(
    monkeypatch,
) -> None:
    store = _store()
    exact_base_id = load_snapshot(store, SNAPSHOT_ID).snapshot_id

    def findings(_result, snapshot_id, model):
        del model
        observed = 4.0 if snapshot_id == exact_base_id else 5.0
        return [_simulation_finding(snapshot_id, observed)]

    monkeypatch.setattr(agent_runners_mod, "to_findings", findings)

    outcome = _handler(store)(_context(FakeModelBridge(responses=(_BENIGN_OPS,))))

    assert isinstance(outcome, PreparedRunResult)


def test_generation_gate_rejects_new_navigation_proof_obligation() -> None:
    store = _store()
    store.register(SNAPSHOT_ID, _navigation_snapshot(talk_step_count=0))
    response = json.dumps(
        [
            {
                "op": "add_entity",
                "target": "step:talk:new",
                "new_value": {
                    "type": "QUEST_STEP",
                    "attrs": {"kind": "talk", "target": "npc:giver"},
                },
                "op_id": "add-talk",
            },
            {
                "op": "add_relation",
                "target": "quest:talk:new",
                "new_value": {
                    "type": "HAS_STEP",
                    "src_id": "quest:nav",
                    "dst_id": "step:talk:new",
                },
                "op_id": "bind-talk",
            },
        ]
    )

    outcome = _handler(store)(_context(FakeModelBridge(responses=(response,))))

    assert isinstance(outcome, PreparedRunFailure)
    checker = next(a for a in outcome.artifacts if a.kind == "checker_run")
    payload = json.loads(store.read_prepared(checker.object_ref))
    assert any(
        item["defect_class"] == "unreachable_target" and item["status"] == "unproven"
        for item in payload["findings"]
    )


def test_generation_gate_removing_one_of_two_navigation_obligations_is_not_new() -> None:
    store = _store()
    store.register(SNAPSHOT_ID, _navigation_snapshot(talk_step_count=2))
    response = json.dumps(
        [
            {
                "op": "delete_relation",
                "target": "quest:talk:1",
                "op_id": "remove-one-talk-obligation",
            }
        ]
    )

    outcome = _handler(store)(_context(FakeModelBridge(responses=(response,))))

    assert isinstance(outcome, PreparedRunResult)


def test_generation_candidate_expansion_rejects_before_candidate_checker_or_simulation(
    monkeypatch,
) -> None:
    class RecordingChecker:
        calls: list[int] = []

        def check(self, snapshot, nav=None):
            del nav
            self.calls.append(len(snapshot.entities))
            return []

    simulation_calls: list[str] = []
    original_run = agent_runners_mod.EconomySimulator.run

    def recording_run(self, model, seed, n_agents, n_ticks):
        simulation_calls.append("run")
        return original_run(self, model, seed=seed, n_agents=n_agents, n_ticks=n_ticks)

    monkeypatch.setattr(agent_runners_mod.EconomySimulator, "run", recording_run)
    config = GenerationExecutionConfig(
        max_constraint_count=256,
        # Base is 2^2 + 2 + 1 = 7; adding one entity raises it to 13.
        max_work_units=7,
        gate_simulation_seed=0,
        gate_simulation_population=30,
        gate_simulation_horizon_steps=120,
        max_simulation_work_units=2_000_000,
    )
    store = _store()
    handler = GenerationProposalHandler(
        blobs=store,
        store=store,
        agent_runner=M2GenerationAgentRunner(
            checker_factory=lambda _snapshot, _constraints: [RecordingChecker()]
        ),
        config_exporter=_FakeConfigExporter(),
        execution_config_resolver=lambda _profile: config,
    )
    response = json.dumps(
        [
            {
                "op": "add_entity",
                "target": "item:expanded",
                "new_value": {"type": "ITEM"},
                "op_id": "expand",
            }
        ]
    )

    outcome = handler(_context(FakeModelBridge(responses=(response,))))

    assert isinstance(outcome, PreparedRunFailure)
    assert RecordingChecker.calls == [2]
    assert simulation_calls == ["run"]
    checker = next(a for a in outcome.artifacts if a.kind == "checker_run")
    payload = json.loads(store.read_prepared(checker.object_ref))
    assert any(
        item["evidence"] == {"reason_code": "candidate_work_budget_exceeded"}
        for item in payload["findings"]
    )


def test_generation_aggregate_output_budget_rejects_before_any_object_write() -> None:
    config = GenerationExecutionConfig(
        max_constraint_count=256,
        max_work_units=2_000_000,
        gate_simulation_seed=0,
        gate_simulation_population=30,
        gate_simulation_horizon_steps=120,
        max_simulation_work_units=2_000_000,
        max_total_prepared_artifact_bytes=1,
    )
    store = _store()

    with pytest.raises(IntegrityViolation, match="aggregate byte bound"):
        _handler(store, execution_config=config)(
            _context(FakeModelBridge(responses=(_BENIGN_OPS,)))
        )

    assert store.put_count == 0


def test_generation_rejects_runner_preview_not_replayed_from_exact_ops_before_write() -> None:
    class FabricatedPreviewRunner:
        def run(self, request):
            return GenerationGateOutcomeV1(
                ops=(
                    TypedOp(
                        op_id="fabricated",
                        op="set_entity_attr",
                        target="mob.gold_max",
                        new_value=999,
                    ),
                ),
                passed=True,
                preview_payload=request.snapshot.content_payload,
                preview_snapshot_id=request.snapshot.snapshot_id,
                checker_evidence=(),
                simulation_evidence=(),
                review_evidence=(),
            )

    store = _store()
    handler = GenerationProposalHandler(
        blobs=store,
        store=store,
        agent_runner=FabricatedPreviewRunner(),
        config_exporter=_FakeConfigExporter(),
        execution_config_resolver=lambda _profile: _execution_config(),
    )

    with pytest.raises(ValueError, match="differs from exact-base replay"):
        handler(_context(FakeModelBridge()))

    assert store.put_count == 0


def test_generation_export_profile_count_rejects_before_model_or_object_write() -> None:
    config = GenerationExecutionConfig(
        max_constraint_count=256,
        max_work_units=2_000_000,
        gate_simulation_seed=0,
        gate_simulation_population=30,
        gate_simulation_horizon_steps=120,
        max_simulation_work_units=2_000_000,
        max_candidate_export_profiles=1,
    )
    payload = _payload().model_copy(
        update={
            "candidate_export_profiles": (
                ProfileRefV1(profile_id="csv-a", version=1),
                ProfileRefV1(profile_id="csv-b", version=1),
            )
        }
    )
    bridge = FakeModelBridge(responses=(_BENIGN_OPS,))
    store = _store()

    with pytest.raises(ValueError, match="profile count budget"):
        _handler(store, execution_config=config)(_context(bridge, payload=payload))

    assert bridge.requests == []
    assert store.put_count == 0


def test_generation_gate_pass_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _handler(store_a)(_context(FakeModelBridge(responses=(_BENIGN_OPS,))))
    out_b = _handler(store_b)(_context(FakeModelBridge(responses=(_BENIGN_OPS,))))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]


def test_generation_checker_budget_rejects_before_model_or_gate_execution() -> None:
    store = _store()
    store.register(
        CONSTRAINT_ID,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [
                {
                    "id": f"C_{index:03d}",
                    "dsl_grammar_version": "dsl@1",
                    "kind": "numeric",
                    "oracle": "deterministic",
                    "predicates": [],
                    "assert": "reward_gold <= 80",
                    "severity": "major",
                }
                for index in range(257)
            ],
        },
    )
    bridge = FakeModelBridge(responses=(_BENIGN_OPS,))

    with pytest.raises(ValueError, match="profile work budget"):
        _handler(store)(_context(bridge))

    assert bridge.requests == []
    assert store.put_count == 0


def test_generation_gate_uses_exact_profile_simulation_budget(monkeypatch) -> None:
    calls: list[tuple[int, int, int]] = []
    original = agent_runners_mod.EconomySimulator.run

    def recording_run(self, model, seed, n_agents, n_ticks):
        calls.append((seed, n_agents, n_ticks))
        return original(self, model, seed=seed, n_agents=n_agents, n_ticks=n_ticks)

    monkeypatch.setattr(agent_runners_mod.EconomySimulator, "run", recording_run)
    config = GenerationExecutionConfig(
        max_constraint_count=256,
        max_work_units=2_000_000,
        gate_simulation_seed=0,
        gate_simulation_population=7,
        gate_simulation_horizon_steps=9,
        max_simulation_work_units=2_000_000,
    )
    store = _store()

    outcome = _handler(store, execution_config=config)(
        _context(FakeModelBridge(responses=(_BENIGN_OPS,)))
    )

    assert isinstance(outcome, PreparedRunResult)
    assert calls == [(0, 7, 9), (0, 7, 9)]
    simulation = next(a for a in outcome.artifacts if a.kind == "simulation_run")
    payload = json.loads(store.read_prepared(simulation.object_ref))
    assert (payload["seed"], payload["replication_count"], payload["horizon_steps"]) == (
        0,
        7,
        9,
    )


def test_generation_rejects_profile_seed_different_from_producer_facts() -> None:
    config = GenerationExecutionConfig(
        max_constraint_count=256,
        max_work_units=2_000_000,
        gate_simulation_seed=1,
        gate_simulation_population=30,
        gate_simulation_horizon_steps=120,
        max_simulation_work_units=2_000_000,
    )
    bridge = FakeModelBridge(responses=(_BENIGN_OPS,))
    store = _store()

    with pytest.raises(ValueError, match="seed differs"):
        _handler(store, execution_config=config)(_context(bridge))

    assert bridge.requests == []
