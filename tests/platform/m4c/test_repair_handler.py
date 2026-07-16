"""Task 11b — ``repair_search@1`` (verifier-guided repair).

Drives the REAL M2 ``repair_search`` (drafter LLM + deterministic verifier) through
the 11a model-bridge adapter with a REPLAY cassette. The LLM only DRAFTS ops; the
deterministic verifier (spine checkers + economy sim) decides closure. Verifier
closure yields ONE exact-base superseding patch + new preview + config + verifier
evidence; a failed search yields ``repair_unverified`` with a produced /
``not_executed`` disposition for EVERY frozen requirement and NO head advance.
"""

from __future__ import annotations

import json

from gameforge.apps.worker.agent_runners import M2RepairAgentRunner
from gameforge.contracts.config_export import ConfigExportFileV1, ConfigExportPackageV1
from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding, PatchV2, TypedOp
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    PatchRepairPayloadV1,
    PreparedRunFailure,
    PreparedRunResult,
    RefReadBindingV1,
    ResolvedArtifactRequirementV1,
)
from gameforge.contracts.workflow import FindingEvidenceBindingV1
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.platform.run_handlers.repair import RepairSearchHandler
from gameforge.platform.run_handlers.review import ReviewSimConfig
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    FakeModelBridge,
    build_context,
    execution_plan,
    resolved_binding,
    resolved_policy_snapshot,
    snapshot_bytes,
)

REPAIR_KIND = RunKindRef(kind="patch.repair", version=1)
MODEL_REF = "anthropic/claude-opus-4-8/m2a@1"
BASE_ID = "artifact:base"
PREVIEW_ID = "artifact:preview"
CONSTRAINT_ID = "artifact:constraints"
SUBJECT_ID = "artifact:subject-patch"
EVIDENCE_ID = "artifact:validation-evidence"
FINDING_EVIDENCE_ID = "artifact:finding-evidence"
SUITE_ID = "suite:1"
_HEX = "a" * 64

# The exact fix: delete the dangling SELLS relation.
_FIX_OPS = json.dumps([{"op": "delete_relation", "target": "bad", "op_id": "r0"}])


def _base_entities():
    gold = Entity(id="gold", type=NodeType.CURRENCY, attrs={})
    dangling = Relation(id="bad", type=EdgeType.SELLS, src_id="shop:ghost", dst_id="gold")
    return [gold], [dangling]


def _base_snapshot() -> Snapshot:
    entities, relations = _base_entities()
    return Snapshot.from_entities_relations(entities, relations)


BASE_SNAPSHOT_ID = _base_snapshot().snapshot_id


def _target_finding() -> Finding:
    return Finding(
        id="f:dangling",
        source="checker",
        producer_id="graph",
        producer_run_id=f"graph@{BASE_SNAPSHOT_ID}",
        oracle_type="deterministic",
        defect_class="dangling_reference",
        severity="critical",
        snapshot_id=BASE_SNAPSHOT_ID,
        entities=["shop:ghost", "gold"],
        status="confirmed",
        message="relation 'bad' references a missing entity 'shop:ghost'",
    )


def _current_patch() -> PatchV2:
    return PatchV2(
        revision=1,
        base_snapshot_id=BASE_SNAPSHOT_ID,
        target_snapshot_id="prev-preview-snapshot-id",
        side_effect_risk="low",
        ops=[TypedOp(op_id="p0", op="delete_relation", target="obsolete")],
        produced_by="agent",
        producer_run_id="prev-run",
        rationale="original defect patch",
    )


class _FakeConfigExporter:
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


def _payload() -> PatchRepairPayloadV1:
    return PatchRepairPayloadV1(
        subject_patch_artifact_id=SUBJECT_ID,
        expected_subject_head_revision=1,
        expected_workflow_revision=3,
        base_snapshot_artifact_id=BASE_ID,
        preview_snapshot_artifact_id=PREVIEW_ID,
        constraint_snapshot_artifact_id=CONSTRAINT_ID,
        validation_evidence_artifact_id=EVIDENCE_ID,
        findings=(
            FindingEvidenceBindingV1(
                finding_id="f:dangling",
                finding_revision=1,
                evidence_artifact_id=FINDING_EVIDENCE_ID,
                finding_digest=_HEX,
            ),
        ),
        target=RefReadBindingV1(ref_name="patch:main", expected_ref=None),
        repair_policy=ProfileRefV1(profile_id="repair", version=1),
        checker_profiles=(ProfileRefV1(profile_id="graph", version=1),),
        simulation_profiles=(ProfileRefV1(profile_id="econ", version=1),),
        regression_suite_artifact_ids=(SUITE_ID,),
        candidate_export_profiles=(ProfileRefV1(profile_id="csv", version=1),),
    )


def _store() -> FakeArtifactStore:
    store = FakeArtifactStore()
    entities, relations = _base_entities()
    store.register(BASE_ID, snapshot_bytes(entities, relations))
    store.register(CONSTRAINT_ID, {"dsl_grammar_version": "dsl@1", "constraints": []})
    store.register(SUBJECT_ID, _current_patch().model_dump(mode="json"))
    return store


def _handler(store: FakeArtifactStore, *, max_steps: int = 4) -> RepairSearchHandler:
    return RepairSearchHandler(
        blobs=store,
        store=store,
        agent_runner=M2RepairAgentRunner(),
        config_exporter=_FakeConfigExporter(),
        checker_resolver=lambda profile, constraints: GraphChecker(),
        sim_config_resolver=lambda profile: ReviewSimConfig(n_agents=8, n_ticks=20),
        finding_loader=lambda blobs, payload: (_target_finding(),),
        max_steps=max_steps,
    )


def _context(bridge: FakeModelBridge):
    requirements = (
        ResolvedArtifactRequirementV1(
            requirement_id="profile-verifier:checker",
            outcome_rule_id="checker",
            artifact_kind="checker_run",
            payload_schema_id="checker-report@1",
            producer_profile_field_path="/params/checker_profiles/0",
            ordinal=1,
        ),
        ResolvedArtifactRequirementV1(
            requirement_id="profile-verifier:simulation",
            outcome_rule_id="simulation",
            artifact_kind="simulation_run",
            payload_schema_id="simulation-result@1",
            producer_profile_field_path="/params/simulation_profiles/0",
            ordinal=1,
        ),
        ResolvedArtifactRequirementV1(
            requirement_id="profile-verifier:regression",
            outcome_rule_id="regression",
            artifact_kind="regression_evidence",
            payload_schema_id="regression-evidence@1",
            ordinal=1,
        ),
    )
    return build_context(
        params=_payload(),
        kind=REPAIR_KIND,
        seed=7,
        resolved_profiles=(
            resolved_binding(
                "/params/repair_policy", profile_id="repair", version=1, kind="patch_repair"
            ),
            resolved_binding(
                "/params/checker_profiles", profile_id="graph", version=1, kind="checker"
            ),
            resolved_binding(
                "/params/simulation_profiles", profile_id="econ", version=1, kind="simulation"
            ),
            resolved_binding(
                "/params/candidate_export_profiles",
                profile_id="csv",
                version=1,
                kind="config_export",
            ),
        ),
        resolved_policy_snapshots=(
            resolved_policy_snapshot("repair-verifier", "/params/repair_policy", requirements),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"repair": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )


def _kinds(outcome) -> list[str]:
    return [artifact.kind for artifact in outcome.artifacts]


def test_repair_verified_emits_superseding_patch_from_exact_base() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_FIX_OPS,))
    outcome = _handler(store)(_context(bridge))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "repair_verified"

    kinds = _kinds(outcome)
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "patch"
    assert primary.payload_schema_id == "patch@2"
    assert kinds.count("ir_snapshot") == 1  # the NEW preview
    assert kinds.count("config_export") == 1
    assert kinds.count("checker_run") == 1
    assert kinds.count("simulation_run") == 1
    assert kinds.count("regression_evidence") == 1
    assert {
        artifact.meta["requirement_id"]
        for artifact in outcome.artifacts
        if artifact.kind in {"checker_run", "simulation_run", "regression_evidence"}
    } == {
        "profile-verifier:checker",
        "profile-verifier:simulation",
        "profile-verifier:regression",
    }

    patch = PatchV2.model_validate(json.loads(store.read_prepared(primary.object_ref)))
    assert patch.revision == 2  # ONE combined superseding revision
    assert patch.supersedes_artifact_id == SUBJECT_ID
    assert patch.produced_by == "agent"
    assert patch.producer_run_id == "run:1"
    # exact-base: the superseding patch is grounded on the CURRENT-ref base snapshot.
    assert patch.base_snapshot_id == BASE_SNAPSHOT_ID
    preview = next(a for a in outcome.artifacts if a.kind == "ir_snapshot")
    assert patch.target_snapshot_id == preview.version_tuple.ir_snapshot_id
    assert patch.target_snapshot_id != BASE_SNAPSHOT_ID
    # doc_version/ir_snapshot_id inherit from the base, NOT the unapproved preview.
    assert primary.version_tuple.ir_snapshot_id == BASE_SNAPSHOT_ID
    assert primary.version_tuple.ir_snapshot_id != preview.version_tuple.ir_snapshot_id


def test_repair_unverified_leaves_head_unchanged_with_full_dispositions() -> None:
    store = _store()
    # the model never proposes a valid patch -> the search exhausts.
    bridge = FakeModelBridge(responses=("[]", "[]"))
    outcome = _handler(store, max_steps=2)(_context(bridge))

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "repair_unverified"
    assert outcome.failure_class == "validation"
    assert outcome.intrinsic_retry_eligible is False
    # NO new patch / preview / config: the SubjectHead is not advanced.
    assert outcome.artifacts == ()

    # every frozen requirement carries a produced/not_executed disposition.
    dispositions = outcome.requirement_dispositions
    assert {d.status for d in dispositions} == {"not_executed"}
    assert {d.reason_code for d in dispositions} == {"search_exhausted"}
    by_rule: dict[str, set[str]] = {}
    for disposition in dispositions:
        assert disposition.resolved_policy_id == "repair-verifier"
        by_rule.setdefault(disposition.outcome_rule_id, set()).add(disposition.requirement_id)
    assert by_rule == {
        "checker": {"profile-verifier:checker"},
        "simulation": {"profile-verifier:simulation"},
        "regression": {"profile-verifier:regression"},
    }


def test_repair_verified_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _handler(store_a)(_context(FakeModelBridge(responses=(_FIX_OPS,))))
    out_b = _handler(store_b)(_context(FakeModelBridge(responses=(_FIX_OPS,))))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]
