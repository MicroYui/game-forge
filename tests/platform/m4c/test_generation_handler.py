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

from gameforge.apps.worker.agent_runners import M2GenerationAgentRunner
from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.config_export import ConfigExportFileV1, ConfigExportPackageV1
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    GenerationProposePayloadV1,
    PreparedRunFailure,
    PreparedRunResult,
    PromptGoalBindingV1,
    RefReadBindingV1,
)
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.platform.run_handlers.generation import GenerationProposalHandler
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    FakeModelBridge,
    build_context,
    execution_plan,
    resolved_binding,
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


def _base_snapshot() -> bytes:
    gold = Entity(id="gold", type=NodeType.CURRENCY, attrs={})
    mob = Entity(
        id="mob",
        type=NodeType.MONSTER,
        attrs={"gold_min": 60, "gold_max": 140, "kills_per_tick": 10},
    )
    drop = Relation(id="drop", type=EdgeType.DROPS_FROM, src_id="mob", dst_id="gold")
    return snapshot_bytes([gold, mob], [drop])


class _FakeConfigExporter:
    """Return a minimal valid config-export package bound to the export profile."""

    def export(
        self,
        *,
        export_profile,
        preview_snapshot_id,
        preview_payload,
        constraint_snapshot_artifact_id,
        constraints,
    ) -> ConfigExportPackageV1:
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
    store.register(GOAL_ID, {"goal": "tune the outpost faucet", "grounding_snapshot_id": ""})
    return store


def _handler(store: FakeArtifactStore) -> GenerationProposalHandler:
    return GenerationProposalHandler(
        blobs=store,
        store=store,
        agent_runner=M2GenerationAgentRunner(checker_factory=lambda snap, cons: [GraphChecker()]),
        config_exporter=_FakeConfigExporter(),
    )


def _context(bridge: FakeModelBridge):
    return build_context(
        params=_payload(),
        kind=GENERATION_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/generation_policy", profile_id="gen", version=1, kind="generation"
            ),
            resolved_binding(
                "/params/candidate_export_profiles",
                profile_id="csv",
                version=1,
                kind="config_export",
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"generation": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )


def _kinds(outcome) -> list[str]:
    return [artifact.kind for artifact in outcome.artifacts]


def test_generation_gate_pass_emits_patch_preview_config_and_gate_evidence() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_BENIGN_OPS,))
    outcome = _handler(store)(_context(bridge))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "generation_gate_passed"
    # exactly one LLM proposal call went through the bridge.
    assert len(bridge.requests) == 1

    kinds = _kinds(outcome)
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "patch"
    assert primary.payload_schema_id == "patch@2"
    assert kinds.count("ir_snapshot") == 1  # the non-authoritative preview
    # one config_export per candidate_export_profile, bound to /export_profile.
    assert kinds.count("config_export") == 1
    config = next(a for a in outcome.artifacts if a.kind == "config_export")
    assert config.meta["export_profile"] == {"profile_id": "csv", "version": 1}
    # gate checker/sim/review evidence (one resolved requirement each).
    assert kinds.count("checker_run") == 1
    assert kinds.count("simulation_run") == 1
    assert kinds.count("review_report") == 1

    # the (agent) patch's producer_run_id binds to THIS run.
    patch = PatchV2.model_validate(json.loads(store.read_prepared(primary.object_ref)))
    assert patch.produced_by == "agent"
    assert patch.producer_run_id == "run:1"
    assert patch.revision == 1


def test_generation_gate_reject_is_evidence_only_failure_without_subject() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_BAD_OPS,))
    outcome = _handler(store)(_context(bridge))

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "generation_gate_rejected"
    assert outcome.failure_class == "business_rule"
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


def test_generation_gate_pass_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _handler(store_a)(_context(FakeModelBridge(responses=(_BENIGN_OPS,))))
    out_b = _handler(store_b)(_context(FakeModelBridge(responses=(_BENIGN_OPS,))))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]
