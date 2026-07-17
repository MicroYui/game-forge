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

import pytest

import gameforge.apps.worker.agent_runners as agent_runners_mod
import gameforge.agents.repair.verify as repair_verify_mod
from gameforge.apps.worker.agent_runners import M2RepairAgentRunner
from gameforge.contracts.config_export import ConfigExportFileV1, ConfigExportPackageV1
from gameforge.contracts.canonical import canonical_sha256, sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding, Patch, PatchV2, TypedOp
from gameforge.contracts.ir import EdgeType, Entity, NodeType
from gameforge.contracts.jobs import (
    PatchRepairPayloadV1,
    PreparedRunFailure,
    PreparedRunResult,
    RefReadBindingV1,
    ResolvedArtifactRequirementV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.workflow import (
    EvidenceRequirement,
    EvidenceSet,
    FindingEvidenceBindingV1,
    PatchTargetBindingV1,
)
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import apply_patch
from gameforge.platform.run_handlers.repair import (
    RepairExecutionConfig,
    RepairSearchHandler,
)
from gameforge.platform.run_handlers.review import ReviewSimConfig
from gameforge.platform.run_handlers.validation_common import (
    RegressionSuiteResultV1,
    derive_validation_subseed,
)
from gameforge.platform.publication.payload_schema import (
    decode_and_validate_artifact_payload,
)
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

# The exact fix is only applicable to the failed preview: the clean exact base
# does not contain relation ``bad``.
_FIX_OPS = json.dumps([{"op": "delete_relation", "target": "bad", "op_id": "r0"}])


def _base_entities():
    gold = Entity(id="gold", type=NodeType.CURRENCY, attrs={})
    # A pre-existing unrelated finding stays in the evidence so the test proves
    # embedded producer ids are rebound to the platform Run during sealing.
    item = Entity(id="item:preexisting", type=NodeType.ITEM, attrs={})
    return [gold, item], []


def _base_snapshot() -> Snapshot:
    entities, relations = _base_entities()
    return Snapshot.from_entities_relations(entities, relations)


BASE_SNAPSHOT_ID = _base_snapshot().snapshot_id


def _original_ops(*, second_dangling: bool = False) -> list[TypedOp]:
    ops = [
        TypedOp(
            op_id="p0",
            op="set_entity_attr",
            target="gold.original_change",
            new_value="retained",
        ),
        TypedOp(
            op_id="p1",
            op="add_relation",
            target="bad",
            new_value={
                "type": EdgeType.SELLS.value,
                "src_id": "shop:ghost",
                "dst_id": "gold",
            },
        ),
    ]
    if second_dangling:
        ops.append(
            TypedOp(
                op_id="p2",
                op="add_relation",
                target="bad:second",
                new_value={
                    "type": EdgeType.SELLS.value,
                    "src_id": "shop:other-ghost",
                    "dst_id": "gold",
                },
            )
        )
    return ops


def _preview_snapshot(*, second_dangling: bool = False) -> Snapshot:
    patch = Patch(
        id="fixture-current-patch",
        base_snapshot_id=BASE_SNAPSHOT_ID,
        target_snapshot_id="pending",
        preconditions=[{"kind": "entity_exists", "id": "gold"}],
        side_effect_risk="low",
        ops=_original_ops(second_dangling=second_dangling),
        produced_by="agent",
        producer_run_id="prev-run",
        rationale="fixture failed preview",
    )
    return apply_patch(_base_snapshot(), patch)


PREVIEW_SNAPSHOT_ID = _preview_snapshot().snapshot_id


def _target_finding() -> Finding:
    replayed = next(
        finding
        for finding in GraphChecker().check(_preview_snapshot())
        if finding.defect_class == "dangling_reference"
    )
    return replayed.model_copy(update={"id": "f:dangling"})


def _second_target_finding(snapshot_id: str) -> Finding:
    replayed = next(
        finding
        for finding in GraphChecker().check(_preview_snapshot(second_dangling=True))
        if set(finding.entities) == {"shop:other-ghost", "gold"}
    )
    assert replayed.snapshot_id == snapshot_id
    return replayed.model_copy(update={"id": "f:dangling:second"})


def _simulation_finding(snapshot_id: str, defect_class: str) -> Finding:
    return Finding(
        id=f"sim:{defect_class}",
        source="sim",
        producer_id="economy-sim",
        producer_run_id=f"economy-sim@{snapshot_id}",
        oracle_type="simulation",
        defect_class=defect_class,
        severity="critical",
        snapshot_id=snapshot_id,
        entities=["gold"],
        status="confirmed",
        message=f"simulation reproduced {defect_class}",
    )


def _current_patch(*, second_dangling: bool = False) -> PatchV2:
    preview = _preview_snapshot(second_dangling=second_dangling)
    return PatchV2(
        revision=1,
        base_snapshot_id=BASE_SNAPSHOT_ID,
        target_snapshot_id=preview.snapshot_id,
        expected_to_fix=["f:original"],
        preconditions=[{"kind": "entity_exists", "id": "gold"}],
        side_effect_risk="low",
        ops=_original_ops(second_dangling=second_dangling),
        produced_by="agent",
        producer_run_id="prev-run",
        rationale="original defect patch",
    )


class _PassingRegressionRunner:
    def __init__(self) -> None:
        self.requests = []

    def run(self, request) -> RegressionSuiteResultV1:
        self.requests.append(request)
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            action_work_units=1,
            status="passed",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "status": "passed",
            },
        )


class _EnvironmentBoundPassingRegressionRunner(_PassingRegressionRunner):
    def run(self, request) -> RegressionSuiteResultV1:
        result = super().run(request)
        return RegressionSuiteResultV1(
            suite_artifact_id=result.suite_artifact_id,
            action_work_units=result.action_work_units,
            status=result.status,
            payload=result.payload,
            reason_code=result.reason_code,
            env_contract_version="generic-agent-env@1",
        )


class _FailingRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            action_work_units=1,
            status="failed",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "status": "failed",
            },
        )


class _ContradictoryPassingRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            action_work_units=1,
            status="passed",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "status": "failed",
            },
        )


class _WrongSuiteRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id="suite:substituted",
            action_work_units=1,
            status="passed",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": "suite:substituted",
                "snapshot_id": request.snapshot_id,
                "status": "passed",
            },
        )


class _WrongSeedRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            action_work_units=1,
            status="passed",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "seed": request.seed + 1,
                "status": "passed",
            },
        )


class _MidRegressionUnavailableRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        if request.suite_artifact_id == "suite:2":
            raise RuntimeError("adapter became unavailable")
        return _PassingRegressionRunner().run(request)


class _CorruptRegressionAuthorityRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        del request
        raise IntegrityViolation("regression suite object binding is corrupt")


class _FindingRegressionRunner:
    def __init__(self, finding: Finding) -> None:
        self.finding = finding

    def run(self, request) -> RegressionSuiteResultV1:
        reproduced = request.snapshot_id == PREVIEW_SNAPSHOT_ID
        findings = (
            [self.finding.model_copy(update={"snapshot_id": request.snapshot_id})]
            if reproduced
            else []
        )
        status = "failed" if reproduced else "passed"
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            action_work_units=1,
            status=status,
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "status": status,
                "findings": [item.model_dump(mode="json") for item in findings],
            },
        )


class _ExplodingChecker:
    id = "exploding"

    def check(self, snapshot, nav=None):
        del snapshot, nav
        raise RuntimeError("checker unavailable")


class _GraphWithSemanticCollision:
    """Expose a relation-level regression hidden by the legacy class/entity key."""

    id = "graph-with-semantic-collision"

    def check(self, snapshot, nav=None):
        findings = GraphChecker().check(snapshot, nav=nav)
        relation_id = "relation:baseline" if "bad" in snapshot.relations else "relation:new"
        findings.append(
            Finding(
                id=f"finding:{relation_id}",
                source="checker",
                producer_id=self.id,
                producer_run_id="run:checker",
                oracle_type="deterministic",
                defect_class="semantic_collision",
                severity="major",
                snapshot_id=snapshot.snapshot_id,
                entities=["gold"],
                # Same endpoints/class and an empty relations[]; only the exact
                # semantic evidence distinguishes the newly introduced edge.
                relations=[],
                evidence={"relation_id": relation_id},
                minimal_repro={"entity_id": "gold"},
                status="confirmed",
                message=relation_id,
            )
        )
        return findings


class _FakeConfigExporter:
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


def _payload(
    *,
    finding_ids: tuple[str, ...] = ("f:dangling",),
    suite_ids: tuple[str, ...] = (SUITE_ID,),
    simulation_profiles: tuple[ProfileRefV1, ...] | None = None,
) -> PatchRepairPayloadV1:
    exact_simulation_profiles = (
        (ProfileRefV1(profile_id="econ", version=1),)
        if simulation_profiles is None
        else simulation_profiles
    )
    return PatchRepairPayloadV1(
        subject_patch_artifact_id=SUBJECT_ID,
        expected_subject_head_revision=1,
        expected_workflow_revision=3,
        base_snapshot_artifact_id=BASE_ID,
        preview_snapshot_artifact_id=PREVIEW_ID,
        constraint_snapshot_artifact_id=CONSTRAINT_ID,
        validation_evidence_artifact_id=EVIDENCE_ID,
        findings=tuple(
            FindingEvidenceBindingV1(
                finding_id=finding_id,
                finding_revision=1,
                evidence_artifact_id=(
                    FINDING_EVIDENCE_ID if index == 0 else f"{FINDING_EVIDENCE_ID}:{index}"
                ),
                finding_digest=_HEX,
            )
            for index, finding_id in enumerate(finding_ids)
        ),
        target=RefReadBindingV1(ref_name="patch:main", expected_ref=None),
        repair_policy=ProfileRefV1(profile_id="repair", version=1),
        checker_profiles=(ProfileRefV1(profile_id="graph", version=1),),
        simulation_profiles=exact_simulation_profiles,
        regression_suite_artifact_ids=suite_ids,
        candidate_export_profiles=(ProfileRefV1(profile_id="csv", version=1),),
    )


def _store(
    *,
    second_dangling: bool = False,
    finding_ids: tuple[str, ...] = ("f:dangling",),
    suite_ids: tuple[str, ...] = (SUITE_ID,),
) -> FakeArtifactStore:
    store = FakeArtifactStore()
    entities, relations = _base_entities()
    store.register(BASE_ID, snapshot_bytes(entities, relations))
    preview = _preview_snapshot(second_dangling=second_dangling)
    store.register(
        PREVIEW_ID,
        snapshot_bytes(list(preview.entities.values()), list(preview.relations.values())),
    )
    store.register(CONSTRAINT_ID, {"dsl_grammar_version": "dsl@1", "constraints": []})
    subject_payload = _current_patch(second_dangling=second_dangling).model_dump(mode="json")
    store.register(SUBJECT_ID, subject_payload)
    payload = _payload(finding_ids=finding_ids, suite_ids=suite_ids)
    preview_payload = json.loads(store.read_bytes(PREVIEW_ID))
    evidence = EvidenceSet(
        subject_artifact_id=SUBJECT_ID,
        subject_digest=canonical_sha256(subject_payload),
        policy_version="repair-test@1",
        validation_run_id="run:validation",
        target_binding=PatchTargetBindingV1(
            target_artifact_id=PREVIEW_ID,
            target_snapshot_id=preview.snapshot_id,
            target_digest=canonical_sha256(preview_payload),
            ref_name=payload.target.ref_name,
            expected_ref=payload.target.expected_ref,
        ),
        supporting_artifact_ids=(FINDING_EVIDENCE_ID,),
        finding_bindings=payload.findings,
        requirements=(
            EvidenceRequirement(
                requirement_id="checker",
                kind="checker",
                applicability="required",
                status="failed",
                evidence_artifact_id=FINDING_EVIDENCE_ID,
                tool_version="test@1",
            ),
        ),
        overall_status="failed",
    )
    store.register(EVIDENCE_ID, evidence.model_dump(mode="json"))
    for suite_id in suite_ids:
        store.register(suite_id, {"suite": suite_id})
    return store


_DEFAULT_RUNNER = object()


class _BytesOnlyArtifactReader:
    """Keep legacy repair fixtures on the byte-reader branch.

    ``FakeArtifactStore`` also exposes exact Artifact envelopes for Task 13
    validation tests.  The repair fixtures intentionally use readable logical
    ids rather than content-addressed ArtifactV2 ids, so handing that richer
    test double directly to ``RepairSearchHandler`` would incorrectly select
    the production envelope-size path.
    """

    def __init__(self, store: FakeArtifactStore) -> None:
        self._store = store

    def read_bytes(self, artifact_id: str) -> bytes:
        return self._store.read_bytes(artifact_id)


def _handler(
    store: FakeArtifactStore,
    *,
    max_steps: int = 4,
    findings: tuple[Finding, ...] | None = None,
    regression_runner=_DEFAULT_RUNNER,
    checker=None,
    max_checker_work_units: int = 2_000_000,
    max_simulation_work_units: int = 2_000_000,
    max_regression_work_units: int = 10_000_000,
    max_regression_suite_bytes: int = 17 * 1024 * 1024,
    max_total_regression_suite_bytes: int = 64 * 1024 * 1024,
    max_prepared_artifact_bytes: int = 128 * 1024 * 1024,
    max_candidate_export_profiles: int = 16,
    max_prompt_message_bytes: int = 16 * 1024 * 1024,
) -> RepairSearchHandler:
    resolved_regression_runner = (
        _PassingRegressionRunner() if regression_runner is _DEFAULT_RUNNER else regression_runner
    )
    return RepairSearchHandler(
        blobs=_BytesOnlyArtifactReader(store),
        store=store,
        agent_runner=M2RepairAgentRunner(regression_runner=resolved_regression_runner),
        config_exporter=_FakeConfigExporter(),
        checker_resolver=lambda profile, constraints: checker or GraphChecker(),
        sim_config_resolver=lambda profile: ReviewSimConfig(
            n_agents=8, n_ticks=20, max_work_units=2_000_000
        ),
        execution_config_resolver=lambda profile: RepairExecutionConfig(
            max_search_steps=max_steps,
            max_prompt_message_bytes=max_prompt_message_bytes,
            max_total_checker_work_units=max_checker_work_units,
            max_total_simulation_work_units=max_simulation_work_units,
            max_total_regression_work_units=max_regression_work_units,
            max_regression_suite_bytes=max_regression_suite_bytes,
            max_total_regression_suite_bytes=max_total_regression_suite_bytes,
            max_total_prepared_artifact_bytes=max_prepared_artifact_bytes,
            max_candidate_export_profiles=max_candidate_export_profiles,
        ),
        finding_loader=lambda blobs, payload: findings or (_target_finding(),),
    )


def test_repair_prompt_profile_cap_rejects_before_bridge_or_object_write() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_FIX_OPS,))

    with pytest.raises(IntegrityViolation, match="profile byte limit"):
        _handler(store, max_prompt_message_bytes=1)(_context(bridge))

    assert bridge.requests == []
    assert store.put_count == 0


def _context(
    bridge: FakeModelBridge,
    *,
    payload: PatchRepairPayloadV1 | None = None,
):
    actual_payload = payload or _payload()
    requirements = (
        *(
            ResolvedArtifactRequirementV1(
                requirement_id=(
                    "profile-verifier:checker"
                    if index == 0
                    else f"profile-verifier:checker:{index + 1}"
                ),
                outcome_rule_id="checker",
                artifact_kind="checker_run",
                payload_schema_id="checker-report@1",
                producer_profile_field_path=f"/params/checker_profiles/{index}",
                ordinal=index + 1,
            )
            for index, _profile in enumerate(actual_payload.checker_profiles)
        ),
        *(
            ResolvedArtifactRequirementV1(
                requirement_id=(
                    "profile-verifier:simulation"
                    if index == 0
                    else f"profile-verifier:simulation:{index + 1}"
                ),
                outcome_rule_id="simulation",
                artifact_kind="simulation_run",
                payload_schema_id="simulation-result@1",
                producer_profile_field_path=f"/params/simulation_profiles/{index}",
                ordinal=index + 1,
            )
            for index, _profile in enumerate(actual_payload.simulation_profiles)
        ),
        *(
            ResolvedArtifactRequirementV1(
                requirement_id=(
                    "profile-verifier:regression"
                    if index == 0
                    else f"profile-verifier:regression:{index + 1}"
                ),
                outcome_rule_id="regression",
                artifact_kind="regression_evidence",
                payload_schema_id="regression-evidence@1",
                ordinal=index + 1,
            )
            for index, _suite_id in enumerate(actual_payload.regression_suite_artifact_ids)
        ),
    )
    return build_context(
        params=actual_payload,
        kind=REPAIR_KIND,
        seed=7,
        resolved_profiles=(
            resolved_binding(
                "/params/repair_policy", profile_id="repair", version=1, kind="patch_repair"
            ),
            *(
                resolved_binding(
                    f"/params/checker_profiles/{index}",
                    profile_id=profile.profile_id,
                    version=profile.version,
                    kind="checker",
                )
                for index, profile in enumerate(actual_payload.checker_profiles)
            ),
            *(
                resolved_binding(
                    f"/params/simulation_profiles/{index}",
                    profile_id=profile.profile_id,
                    version=profile.version,
                    kind="simulation",
                )
                for index, profile in enumerate(actual_payload.simulation_profiles)
            ),
            resolved_binding(
                "/params/candidate_export_profiles/0",
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
        version_tuple=VersionTuple(
            doc_version="base-doc@1",
            ir_snapshot_id=BASE_SNAPSHOT_ID,
            constraint_snapshot_id="constraint:semantic:1",
            seed=7,
            tool_version="handler@1",
        ),
    )


def _kinds(outcome) -> list[str]:
    return [artifact.kind for artifact in outcome.artifacts]


def _assert_unverified_input_preview_evidence(
    outcome: PreparedRunFailure,
    store: FakeArtifactStore,
    *,
    expected_rules: set[str],
    expected_snapshot_id: str = PREVIEW_SNAPSHOT_ID,
) -> None:
    assert not {"patch", "ir_snapshot", "config_export"}.intersection(_kinds(outcome))
    produced = {
        disposition.outcome_rule_id
        for disposition in outcome.requirement_dispositions
        if disposition.status == "produced"
    }
    assert produced == expected_rules
    for artifact in outcome.artifacts:
        blob = store.read_prepared(artifact.object_ref)
        payload = decode_and_validate_artifact_payload(
            payload_schema_id=artifact.payload_schema_id,
            blob=blob,
        )
        assert payload["snapshot_id"] == expected_snapshot_id
        assert payload["requirement_id"] == artifact.meta["requirement_id"]
        assert PREVIEW_ID in artifact.lineage


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
    assert patch.preconditions == [{"kind": "entity_exists", "id": "gold"}]
    assert patch.expected_to_fix == ["f:original", "f:dangling"]
    # ONE full combined patch: original candidate operations are retained in
    # order and the failed-preview corrective op is appended.
    assert [(op.op, op.target) for op in patch.ops] == [
        ("set_entity_attr", "gold.original_change"),
        ("add_relation", "bad"),
        ("delete_relation", "bad"),
    ]
    # exact-base: the superseding patch is grounded on the CURRENT-ref base snapshot.
    assert patch.base_snapshot_id == BASE_SNAPSHOT_ID
    preview = next(a for a in outcome.artifacts if a.kind == "ir_snapshot")
    assert patch.target_snapshot_id == preview.version_tuple.ir_snapshot_id
    assert patch.target_snapshot_id != BASE_SNAPSHOT_ID
    # doc_version/ir_snapshot_id inherit from the base, NOT the unapproved preview.
    assert primary.version_tuple.ir_snapshot_id == BASE_SNAPSHOT_ID
    assert primary.version_tuple.ir_snapshot_id != preview.version_tuple.ir_snapshot_id
    assert primary.version_tuple.doc_version == "base-doc@1"
    assert primary.version_tuple.constraint_snapshot_id == "constraint:semantic:1"
    assert primary.version_tuple.seed is None
    assert preview.version_tuple.doc_version == "base-doc@1"
    assert preview.version_tuple.constraint_snapshot_id is None
    assert preview.version_tuple.seed is None
    config = next(a for a in outcome.artifacts if a.kind == "config_export")
    assert config.version_tuple.doc_version == "base-doc@1"
    assert config.version_tuple.constraint_snapshot_id == "constraint:semantic:1"
    assert config.version_tuple.seed is None

    applied = apply_patch(_base_snapshot(), patch)
    assert applied.snapshot_id == patch.target_snapshot_id
    assert applied.entities["gold"].attrs["original_change"] == "retained"
    assert "bad" not in applied.relations

    embedded_producers: list[str] = []
    for artifact in outcome.artifacts:
        if artifact.kind not in {"checker_run", "simulation_run"}:
            continue
        payload = json.loads(store.read_prepared(artifact.object_ref))
        embedded_producers.extend(finding["producer_run_id"] for finding in payload["findings"])
    assert embedded_producers
    assert set(embedded_producers) == {"run:1"}

    checker = next(a for a in outcome.artifacts if a.kind == "checker_run")
    assert checker.version_tuple.seed is None

    simulation = next(a for a in outcome.artifacts if a.kind == "simulation_run")
    simulation_payload = json.loads(store.read_prepared(simulation.object_ref))
    simulation_seed = derive_validation_subseed(
        root_seed=7,
        run_kind=REPAIR_KIND,
        profile=ProfileRefV1(profile_id="econ", version=1),
        case_id="profile-verifier:simulation",
        replication_index=0,
    )
    assert simulation_payload["profile"] == {"profile_id": "econ", "version": 1}
    assert simulation_payload["seed"] == 7
    assert simulation_payload["replication_count"] == 8
    assert simulation_payload["horizon_steps"] == 20
    assert simulation_payload["sensitivity"]["seed_binding"] == {
        "root_seed": 7,
        "run_kind": REPAIR_KIND.model_dump(mode="json"),
        "profile_id": "econ",
        "profile_version": 1,
        "case_id": "profile-verifier:simulation",
        "replication_index": 0,
        "seed": simulation_seed,
        "seed_derivation_version": "subseed@1",
    }
    assert simulation.version_tuple.seed == 7

    regression = next(a for a in outcome.artifacts if a.kind == "regression_evidence")
    regression_payload = json.loads(store.read_prepared(regression.object_ref))
    child_seed = derive_validation_subseed(
        root_seed=7,
        run_kind=REPAIR_KIND,
        profile=ProfileRefV1(profile_id="repair", version=1),
        case_id=SUITE_ID,
        replication_index=0,
    )
    assert regression_payload == {
        "case_id": SUITE_ID,
        "payload_schema_version": "regression-evidence@1",
        "profile_id": "repair",
        "profile_version": 1,
        "requirement_id": "profile-verifier:regression",
        "replication_index": 0,
        "root_seed": 7,
        "run_kind": REPAIR_KIND.model_dump(mode="json"),
        "seed": child_seed,
        "seed_derivation_version": "subseed@1",
        "snapshot_id": patch.target_snapshot_id,
        "status": "passed",
        "suite_artifact_id": SUITE_ID,
    }
    assert regression.version_tuple.seed == 7
    assert regression.version_tuple.doc_version == "base-doc@1"
    assert bridge.requests[0].source_artifact_ids == (
        FINDING_EVIDENCE_ID,
        PREVIEW_ID,
    )


def test_repair_regression_evidence_carries_consumed_environment_contract() -> None:
    store = _store()
    outcome = _handler(
        store,
        regression_runner=_EnvironmentBoundPassingRegressionRunner(),
    )(_context(FakeModelBridge(responses=(_FIX_OPS,))))

    assert isinstance(outcome, PreparedRunResult)
    regression = next(a for a in outcome.artifacts if a.kind == "regression_evidence")
    assert regression.version_tuple.env_contract_version == "generic-agent-env@1"


def test_repair_unverified_leaves_head_unchanged_with_full_dispositions() -> None:
    store = _store()
    # the model never proposes a valid patch -> the search exhausts.
    bridge = FakeModelBridge(responses=("[]", "[]"))
    outcome = _handler(store, max_steps=2)(_context(bridge))

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "repair_unverified"
    assert outcome.failure_class == "validation"
    assert outcome.intrinsic_retry_eligible is False
    # NO new patch / preview / config: the SubjectHead is not advanced. Exact
    # verifier work already executed on the immutable failed input remains as
    # auditable evidence instead of being relabelled `not_executed`.
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )

    # every frozen requirement carries a produced/not_executed disposition.
    dispositions = outcome.requirement_dispositions
    assert {d.status for d in dispositions} == {"produced"}
    assert {d.reason_code for d in dispositions} == {None}
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


def test_repair_fails_closed_when_any_exact_finding_remains() -> None:
    finding_ids = ("f:dangling", "f:dangling:second")
    store = _store(second_dangling=True, finding_ids=finding_ids)
    preview = _preview_snapshot(second_dangling=True)
    first = _target_finding().model_copy(update={"snapshot_id": preview.snapshot_id})
    second = _second_target_finding(preview.snapshot_id)
    payload = _payload(finding_ids=finding_ids)

    # Deletes only the first dangling relation. The second exact frozen Finding
    # remains, so the deterministic verifier must not close the repair.
    bridge = FakeModelBridge(responses=(_FIX_OPS,))
    outcome = _handler(
        store,
        max_steps=1,
        findings=(first, second),
    )(_context(bridge, payload=payload))

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "repair_unverified"
    _assert_unverified_input_preview_evidence(
        outcome,
        store,
        expected_rules={"checker", "simulation", "regression"},
        expected_snapshot_id=preview.snapshot_id,
    )


def test_repair_may_fix_one_bound_relation_without_erasing_same_class_peer() -> None:
    preview = _preview_snapshot(second_dangling=True)
    target = _target_finding().model_copy(update={"snapshot_id": preview.snapshot_id})
    payload = _payload(finding_ids=(target.id,))
    store = _store(second_dangling=True, finding_ids=(target.id,))

    outcome = _handler(store, findings=(target,))(
        _context(FakeModelBridge(responses=(_FIX_OPS,)), payload=payload)
    )

    assert isinstance(outcome, PreparedRunResult)
    patch = PatchV2.model_validate(
        json.loads(store.read_prepared(outcome.artifacts[outcome.primary_index].object_ref))
    )
    repaired = apply_patch(_base_snapshot(), patch)
    assert "bad" not in repaired.relations
    assert "bad:second" in repaired.relations


def test_repair_cannot_move_the_bound_target_to_a_new_relation() -> None:
    moved = json.dumps(
        [
            {"op": "delete_relation", "target": "bad", "op_id": "move-delete"},
            {
                "op": "add_relation",
                "target": "bad:moved",
                "new_value": {
                    "type": EdgeType.SELLS.value,
                    "src_id": "shop:ghost",
                    "dst_id": "gold",
                },
                "op_id": "move-add",
            },
        ]
    )
    store = _store()

    outcome = _handler(store, max_steps=1)(_context(FakeModelBridge(responses=(moved,))))

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )


def test_repair_closes_all_exact_findings_in_one_full_patch() -> None:
    finding_ids = ("f:dangling", "f:dangling:second")
    store = _store(second_dangling=True, finding_ids=finding_ids)
    preview = _preview_snapshot(second_dangling=True)
    first = _target_finding().model_copy(update={"snapshot_id": preview.snapshot_id})
    second = _second_target_finding(preview.snapshot_id)
    payload = _payload(finding_ids=finding_ids)
    fixes = json.dumps(
        [
            {"op": "delete_relation", "target": "bad", "op_id": "r0"},
            {"op": "delete_relation", "target": "bad:second", "op_id": "r1"},
        ]
    )

    outcome = _handler(store, findings=(first, second))(
        _context(FakeModelBridge(responses=(fixes,)), payload=payload)
    )

    assert isinstance(outcome, PreparedRunResult)
    primary = outcome.artifacts[outcome.primary_index]
    patch = PatchV2.model_validate(json.loads(store.read_prepared(primary.object_ref)))
    assert patch.expected_to_fix == ["f:original", first.id, second.id]
    assert [(op.op, op.target) for op in patch.ops[-2:]] == [
        ("delete_relation", "bad"),
        ("delete_relation", "bad:second"),
    ]
    repaired = apply_patch(_base_snapshot(), patch)
    assert not repaired.relations
    assert repaired.entities["gold"].attrs["original_change"] == "retained"


def test_second_finding_initial_prompt_consumes_prior_verified_candidate() -> None:
    finding_ids = ("f:dangling", "f:dangling:second")
    store = _store(second_dangling=True, finding_ids=finding_ids)
    preview = _preview_snapshot(second_dangling=True)
    first = _target_finding().model_copy(update={"snapshot_id": preview.snapshot_id})
    second = _second_target_finding(preview.snapshot_id)
    payload = _payload(finding_ids=finding_ids)
    second_fix = json.dumps([{"op": "delete_relation", "target": "bad:second", "op_id": "r1"}])
    bridge = FakeModelBridge(responses=(_FIX_OPS, second_fix))

    outcome = _handler(store, findings=(first, second))(_context(bridge, payload=payload))

    assert isinstance(outcome, PreparedRunResult)
    assert len(bridge.requests) == 2
    first_context, second_context = (request.prompt_context for request in bridge.requests)
    assert first_context.context_kind == "repair_initial"
    assert first_context.include_previous_consumption is False
    # This remains the second Finding's *local* initial prompt (no synthetic
    # counterexample), while its derived working Snapshot consumes call 1.
    assert second_context.context_kind == "repair_initial"
    assert second_context.include_previous_consumption is True
    assert second_context.source_artifact_ids == tuple(
        sorted(
            (
                CONSTRAINT_ID,
                f"{FINDING_EVIDENCE_ID}:1",
                PREVIEW_ID,
                SUITE_ID,
            )
        )
    )
    assert {binding.binding_key for binding in second_context.semantic_bindings} == {
        "repair.finding",
        "repair.verifier_closure",
        "repair.working_snapshot",
    }
    assert (
        "Your previous patch failed deterministic verification"
        not in second_context.messages[0].content
    )
    assert (
        second_context.messages[0].content == bridge.requests[1].model_request.messages[-1].content
    )


def test_repair_without_real_regression_runner_is_unverified() -> None:
    store = _store()
    bridge = FakeModelBridge(responses=(_FIX_OPS,))

    outcome = _handler(store, regression_runner=None)(_context(bridge))

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "repair_unverified"
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation"}
    )
    regression = next(
        item for item in outcome.requirement_dispositions if item.outcome_rule_id == "regression"
    )
    assert regression.status == "not_executed"
    assert regression.reason_code == "execution_short_circuited"
    assert bridge.requests == []


def test_repair_checker_execution_failure_marks_every_dimension_not_executed() -> None:
    store = _store()
    outcome = _handler(store, checker=_ExplodingChecker())(
        _context(FakeModelBridge(responses=(_FIX_OPS,)))
    )

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(outcome, store, expected_rules=set())
    assert {item.status for item in outcome.requirement_dispositions} == {"not_executed"}
    assert {item.reason_code for item in outcome.requirement_dispositions} == {
        "execution_short_circuited"
    }


def test_repair_simulation_execution_failure_preserves_checker_evidence(monkeypatch) -> None:
    def unavailable_sim(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("simulation unavailable")

    monkeypatch.setattr(agent_runners_mod.EconomySimulator, "run", unavailable_sim)
    store = _store()
    outcome = _handler(store)(_context(FakeModelBridge(responses=(_FIX_OPS,))))

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(outcome, store, expected_rules={"checker"})
    not_executed = {
        item.outcome_rule_id
        for item in outcome.requirement_dispositions
        if item.status == "not_executed"
    }
    assert not_executed == {"simulation", "regression"}


def test_repair_mid_regression_failure_preserves_only_executed_suite() -> None:
    suite_ids = (SUITE_ID, "suite:2", "suite:3")
    payload = _payload(suite_ids=suite_ids)
    store = _store(suite_ids=suite_ids)
    outcome = _handler(store, regression_runner=_MidRegressionUnavailableRunner())(
        _context(FakeModelBridge(responses=(_FIX_OPS,)), payload=payload)
    )

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )
    regression_rows = {
        item.requirement_id: item.status
        for item in outcome.requirement_dispositions
        if item.outcome_rule_id == "regression"
    }
    assert regression_rows == {
        "profile-verifier:regression": "produced",
        "profile-verifier:regression:2": "not_executed",
        "profile-verifier:regression:3": "not_executed",
    }
    regression_artifacts = [
        artifact for artifact in outcome.artifacts if artifact.kind == "regression_evidence"
    ]
    assert len(regression_artifacts) == 1
    assert SUITE_ID in regression_artifacts[0].lineage


def test_repair_does_not_downgrade_regression_integrity_failure() -> None:
    store = _store()

    with pytest.raises(IntegrityViolation, match="object binding is corrupt"):
        _handler(store, regression_runner=_CorruptRegressionAuthorityRunner())(
            _context(FakeModelBridge(responses=(_FIX_OPS,)))
        )


def test_repair_passes_exact_subseed_to_regression_runner() -> None:
    store = _store()
    runner = _PassingRegressionRunner()

    outcome = _handler(store, regression_runner=runner)(
        _context(FakeModelBridge(responses=(_FIX_OPS,)))
    )

    assert isinstance(outcome, PreparedRunResult)
    assert len(runner.requests) == 2
    baseline_request, request = runner.requests
    assert baseline_request.snapshot_id == PREVIEW_SNAPSHOT_ID
    assert request.seed == derive_validation_subseed(
        root_seed=7,
        run_kind=REPAIR_KIND,
        profile=ProfileRefV1(profile_id="repair", version=1),
        case_id=SUITE_ID,
        replication_index=0,
    )
    assert request.snapshot_id == next(
        artifact.version_tuple.ir_snapshot_id
        for artifact in outcome.artifacts
        if artifact.kind == "ir_snapshot"
    )


def test_repair_uses_only_the_exact_injected_regression_suite(monkeypatch) -> None:
    def forbidden_unbound_smoke(snapshot):
        del snapshot
        raise AssertionError("legacy unbound Aureus smoke must not run")

    monkeypatch.setattr(repair_verify_mod, "_aureus_regression", forbidden_unbound_smoke)
    store = _store()
    runner = _PassingRegressionRunner()

    outcome = _handler(store, regression_runner=runner)(
        _context(FakeModelBridge(responses=(_FIX_OPS,)))
    )

    assert isinstance(outcome, PreparedRunResult)
    assert len(runner.requests) == 2
    assert {request.suite_artifact_id for request in runner.requests} == {SUITE_ID}


def test_repair_without_simulation_profiles_never_runs_hidden_m2_sim(monkeypatch) -> None:
    def forbidden_fixed_budget_sim(snapshot):
        del snapshot
        raise AssertionError("unselected fixed-budget simulation must not run")

    monkeypatch.setattr(repair_verify_mod, "_economy_findings", forbidden_fixed_budget_sim)
    payload = _payload(simulation_profiles=())
    store = _store()

    outcome = _handler(store)(_context(FakeModelBridge(responses=(_FIX_OPS,)), payload=payload))

    assert isinstance(outcome, PreparedRunResult)
    assert all(artifact.kind != "simulation_run" for artifact in outcome.artifacts)


def test_repair_executes_exact_simulation_profile_budget(monkeypatch) -> None:
    calls: list[tuple[int, int, int]] = []
    original_run = agent_runners_mod.EconomySimulator.run

    def forbidden_fixed_budget_sim(snapshot):
        del snapshot
        raise AssertionError("unselected fixed-budget simulation must not run")

    def recording_run(self, model, seed, n_agents, n_ticks):
        calls.append((seed, n_agents, n_ticks))
        return original_run(
            self,
            model,
            seed=seed,
            n_agents=n_agents,
            n_ticks=n_ticks,
        )

    monkeypatch.setattr(agent_runners_mod.EconomySimulator, "run", recording_run)
    monkeypatch.setattr(repair_verify_mod, "_economy_findings", forbidden_fixed_budget_sim)
    store = _store()
    outcome = _handler(store)(_context(FakeModelBridge(responses=(_FIX_OPS,))))

    assert isinstance(outcome, PreparedRunResult)
    expected_seed = derive_validation_subseed(
        root_seed=7,
        run_kind=REPAIR_KIND,
        profile=ProfileRefV1(profile_id="econ", version=1),
        case_id="profile-verifier:simulation",
        replication_index=0,
    )
    assert calls == [(expected_seed, 8, 20), (expected_seed, 8, 20)]


def test_exact_simulation_target_guides_a_second_draft(monkeypatch) -> None:
    first_ops = json.dumps([{"op": "delete_relation", "target": "bad", "op_id": "sim-first"}])
    second_ops = json.dumps(
        [
            {"op": "delete_relation", "target": "bad", "op_id": "sim-second-delete"},
            {
                "op": "set_entity_attr",
                "target": "gold.simulation_fixed",
                "new_value": True,
                "op_id": "sim-second-fix",
            },
        ]
    )
    final_candidate = apply_patch(
        _preview_snapshot(),
        Patch(
            id="simulation-final",
            base_snapshot_id=PREVIEW_SNAPSHOT_ID,
            target_snapshot_id="pending",
            side_effect_risk="low",
            ops=[
                TypedOp(op_id="delete", op="delete_relation", target="bad"),
                TypedOp(
                    op_id="fix",
                    op="set_entity_attr",
                    target="gold.simulation_fixed",
                    new_value=True,
                ),
            ],
            produced_by="agent",
            producer_run_id="test",
            rationale="fixture",
        ),
    )

    def exact_sim_findings(result, snapshot_id, model):
        del result, model
        return (
            []
            if snapshot_id == final_candidate.snapshot_id
            else [_simulation_finding(snapshot_id, "economy_collapse")]
        )

    monkeypatch.setattr(agent_runners_mod, "to_findings", exact_sim_findings)
    finding = _simulation_finding(PREVIEW_SNAPSHOT_ID, "economy_collapse").model_copy(
        update={"id": "f:economy"}
    )
    payload = _payload(finding_ids=(finding.id,))
    store = _store(finding_ids=(finding.id,))
    bridge = FakeModelBridge(responses=(first_ops, second_ops))

    outcome = _handler(store, findings=(finding,), max_steps=2)(_context(bridge, payload=payload))

    assert isinstance(outcome, PreparedRunResult)
    assert len(bridge.requests) == 2
    initial, refine = (request.prompt_context for request in bridge.requests)
    assert initial.context_kind == "repair_initial"
    assert initial.include_previous_consumption is False
    assert initial.source_artifact_ids == (FINDING_EVIDENCE_ID, PREVIEW_ID)
    assert refine.context_kind == "repair_refine"
    assert refine.include_previous_consumption is True
    assert refine.source_artifact_ids == tuple(
        sorted((CONSTRAINT_ID, FINDING_EVIDENCE_ID, PREVIEW_ID, SUITE_ID))
    )
    assert {binding.binding_key for binding in refine.semantic_bindings} == {
        "repair.finding",
        "repair.previous_patch",
        "repair.previous_verdict",
        "repair.verifier_closure",
        "repair.working_snapshot",
    }
    assert refine.messages[0].content == bridge.requests[1].model_request.messages[-1].content


def test_external_finding_requires_exact_regression_identity_before_search() -> None:
    finding = Finding(
        id="f:playtest",
        source="playtest",
        producer_id="playtest",
        producer_run_id="run:playtest",
        oracle_type="deterministic",
        defect_class="playtest_path_failure",
        severity="major",
        snapshot_id=PREVIEW_SNAPSHOT_ID,
        entities=["gold"],
        evidence={"case_id": "case:path"},
        minimal_repro={"case_id": "case:path"},
        status="confirmed",
        message="path failed",
    )
    payload = _payload(finding_ids=(finding.id,))
    store = _store(finding_ids=(finding.id,))
    bridge = FakeModelBridge(responses=(_FIX_OPS, _FIX_OPS))

    outcome = _handler(store, findings=(finding,), max_steps=2)(_context(bridge, payload=payload))

    assert isinstance(outcome, PreparedRunFailure)
    assert bridge.requests == []
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )


def test_external_finding_closes_only_with_exact_regression_finding_identity() -> None:
    finding = Finding(
        id="f:playtest:exact",
        source="playtest",
        producer_id="playtest",
        producer_run_id="run:playtest",
        oracle_type="deterministic",
        defect_class="playtest_path_failure",
        severity="major",
        snapshot_id=PREVIEW_SNAPSHOT_ID,
        entities=["gold"],
        evidence={"case_id": "case:path"},
        minimal_repro={"case_id": "case:path"},
        status="confirmed",
        message="path failed",
    )
    payload = _payload(finding_ids=(finding.id,))
    store = _store(finding_ids=(finding.id,))

    outcome = _handler(
        store,
        findings=(finding,),
        regression_runner=_FindingRegressionRunner(finding),
    )(_context(FakeModelBridge(responses=(_FIX_OPS,)), payload=payload))

    assert isinstance(outcome, PreparedRunResult)
    regression = next(a for a in outcome.artifacts if a.kind == "regression_evidence")
    sealed = decode_and_validate_artifact_payload(
        payload_schema_id=regression.payload_schema_id,
        blob=store.read_prepared(regression.object_ref),
    )
    assert sealed["findings"] == []


def test_unrelated_same_class_simulation_finding_does_not_mask_exact_checker_fix(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        agent_runners_mod,
        "to_findings",
        lambda result, snapshot_id, model: [_simulation_finding(snapshot_id, "dangling_reference")],
    )
    store = _store()

    outcome = _handler(store)(_context(FakeModelBridge(responses=(_FIX_OPS,))))

    assert isinstance(outcome, PreparedRunResult)


def test_repair_is_unverified_on_new_profile_simulation_regression(monkeypatch) -> None:
    def profile_findings(result, snapshot_id, model):
        if snapshot_id == PREVIEW_SNAPSHOT_ID:
            return []
        return [_simulation_finding(snapshot_id, "profile_only_regression")]

    monkeypatch.setattr(agent_runners_mod, "to_findings", profile_findings)
    store = _store()

    outcome = _handler(store)(_context(FakeModelBridge(responses=(_FIX_OPS,))))

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )


def test_existing_simulation_predicate_may_change_observed_value(monkeypatch) -> None:
    def profile_findings(result, snapshot_id, model):
        del result, model
        observed = -10.0 if snapshot_id == PREVIEW_SNAPSHOT_ID else -2.0
        return [
            _simulation_finding(snapshot_id, "balance_floor").model_copy(
                update={
                    "evidence": {
                        "invariant": "balance_floor",
                        "relation_id": "flow:gold",
                        "observed": observed,
                        "threshold": 0.0,
                    }
                }
            )
        ]

    monkeypatch.setattr(agent_runners_mod, "to_findings", profile_findings)
    store = _store()

    outcome = _handler(store)(_context(FakeModelBridge(responses=(_FIX_OPS,))))

    assert isinstance(outcome, PreparedRunResult)


def test_existing_simulation_predicate_does_not_mask_new_relation(monkeypatch) -> None:
    def profile_findings(result, snapshot_id, model):
        del result, model
        findings = [
            _simulation_finding(snapshot_id, "balance_floor").model_copy(
                update={
                    "evidence": {
                        "invariant": "balance_floor",
                        "relation_id": "flow:gold",
                        "observed": -10.0 if snapshot_id == PREVIEW_SNAPSHOT_ID else -2.0,
                    }
                }
            )
        ]
        if snapshot_id != PREVIEW_SNAPSHOT_ID:
            findings.append(
                _simulation_finding(snapshot_id, "balance_floor").model_copy(
                    update={
                        "id": "sim:balance-floor:new-relation",
                        "evidence": {
                            "invariant": "balance_floor",
                            "relation_id": "flow:gems",
                            "observed": -1.0,
                        },
                    }
                )
            )
        return findings

    monkeypatch.setattr(agent_runners_mod, "to_findings", profile_findings)
    store = _store()

    outcome = _handler(store)(_context(FakeModelBridge(responses=(_FIX_OPS,))))

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )


def test_repair_rejects_new_relation_level_finding_hidden_by_legacy_key() -> None:
    store = _store()

    outcome = _handler(store, checker=_GraphWithSemanticCollision())(
        _context(FakeModelBridge(responses=(_FIX_OPS,)))
    )

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )


def test_repair_rejects_candidate_that_introduces_unproven_navigation() -> None:
    response = json.dumps(
        [
            {"op": "delete_relation", "target": "bad", "op_id": "fix-dangling"},
            {
                "op": "add_entity",
                "target": "quest:new",
                "new_value": {"type": "QUEST"},
                "op_id": "add-quest",
            },
            {
                "op": "add_entity",
                "target": "npc:new-giver",
                "new_value": {"type": "NPC"},
                "op_id": "add-giver",
            },
            {
                "op": "add_entity",
                "target": "step:new-talk",
                "new_value": {
                    "type": "QUEST_STEP",
                    "attrs": {"kind": "talk", "target": "npc:new-giver"},
                },
                "op_id": "add-talk",
            },
            {
                "op": "add_relation",
                "target": "quest:new-giver",
                "new_value": {
                    "type": "STARTS_AT",
                    "src_id": "quest:new",
                    "dst_id": "npc:new-giver",
                },
                "op_id": "bind-giver",
            },
            {
                "op": "add_relation",
                "target": "quest:new-step",
                "new_value": {
                    "type": "HAS_STEP",
                    "src_id": "quest:new",
                    "dst_id": "step:new-talk",
                },
                "op_id": "bind-step",
            },
        ]
    )
    store = _store()

    outcome = _handler(store, max_steps=1)(_context(FakeModelBridge(responses=(response,))))

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "repair_unverified"
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )


def test_repair_simulation_profiles_share_run_level_budget_before_drafting() -> None:
    simulation_profiles = (
        ProfileRefV1(profile_id="econ-a", version=1),
        ProfileRefV1(profile_id="econ-b", version=1),
    )
    payload = _payload(simulation_profiles=simulation_profiles)
    store = _store()
    bridge = FakeModelBridge(responses=(_FIX_OPS,))

    outcome = _handler(store, max_simulation_work_units=1_000)(_context(bridge, payload=payload))

    assert isinstance(outcome, PreparedRunFailure)
    assert bridge.requests == []
    _assert_unverified_input_preview_evidence(outcome, store, expected_rules=set())


def test_repair_checker_profiles_share_run_level_budget_before_drafting() -> None:
    checker_profiles = (
        ProfileRefV1(profile_id="graph-a", version=1),
        ProfileRefV1(profile_id="graph-b", version=1),
    )
    payload = _payload(simulation_profiles=()).model_copy(
        update={"checker_profiles": checker_profiles}
    )
    store = _store()
    bridge = FakeModelBridge(responses=(_FIX_OPS,))

    outcome = _handler(store, max_checker_work_units=10)(_context(bridge, payload=payload))

    assert isinstance(outcome, PreparedRunFailure)
    assert bridge.requests == []
    _assert_unverified_input_preview_evidence(outcome, store, expected_rules=set())


def test_repair_regression_work_ledger_is_shared_across_baseline_and_candidate() -> None:
    store = _store()
    runner = _PassingRegressionRunner()

    outcome = _handler(
        store,
        regression_runner=runner,
        max_regression_work_units=1,
    )(_context(FakeModelBridge(responses=(_FIX_OPS,))))

    assert isinstance(outcome, PreparedRunFailure)
    assert len(runner.requests) == 2
    assert runner.requests[0].max_action_work_units == 1
    assert runner.requests[1].max_action_work_units == 0
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )


def test_large_zero_action_regression_suite_is_rejected_before_agent_search() -> None:
    suite_ids = ("suite:large-a", "suite:large-b")
    payload = _payload(suite_ids=suite_ids)
    store = _store(suite_ids=suite_ids)
    bridge = FakeModelBridge(responses=(_FIX_OPS,))

    with pytest.raises(IntegrityViolation, match="total profile byte budget"):
        _handler(
            store,
            # Each low/zero-action suite is within its individual cap, but their
            # retained wires exceed the run-wide authority in aggregate.
            max_regression_suite_bytes=30,
            max_total_regression_suite_bytes=40,
        )(_context(bridge, payload=payload))

    assert bridge.requests == []
    assert store.put_count == 0


def test_repair_aggregate_output_budget_rejects_before_any_object_write() -> None:
    store = _store()

    with pytest.raises(IntegrityViolation, match="aggregate byte bound"):
        _handler(store, max_prepared_artifact_bytes=1)(
            _context(FakeModelBridge(responses=(_FIX_OPS,)))
        )

    assert store.put_count == 0


def test_repair_export_profile_count_rejects_before_drafting_or_object_write() -> None:
    payload = _payload().model_copy(
        update={
            "candidate_export_profiles": (
                ProfileRefV1(profile_id="csv-a", version=1),
                ProfileRefV1(profile_id="csv-b", version=1),
            )
        }
    )
    bridge = FakeModelBridge(responses=(_FIX_OPS,))
    store = _store()

    with pytest.raises(ValueError, match="profile count budget"):
        _handler(store, max_candidate_export_profiles=1)(_context(bridge, payload=payload))

    assert bridge.requests == []
    assert store.put_count == 0


def test_repair_does_not_forge_pass_when_exact_regression_fails() -> None:
    store = _store()
    outcome = _handler(store, regression_runner=_FailingRegressionRunner())(
        _context(FakeModelBridge(responses=(_FIX_OPS,)))
    )

    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "repair_unverified"
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation", "regression"}
    )


def test_repair_rejects_contradictory_regression_result() -> None:
    store = _store()
    outcome = _handler(store, regression_runner=_ContradictoryPassingRegressionRunner())(
        _context(FakeModelBridge(responses=(_FIX_OPS,)))
    )

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation"}
    )


def test_repair_regression_evidence_binds_each_exact_suite_as_its_own_parent() -> None:
    suite_ids = (SUITE_ID, "suite:2")
    payload = _payload(suite_ids=suite_ids)
    store = _store(suite_ids=suite_ids)

    outcome = _handler(store)(_context(FakeModelBridge(responses=(_FIX_OPS,)), payload=payload))

    assert isinstance(outcome, PreparedRunResult)
    evidence = [
        artifact for artifact in outcome.artifacts if artifact.kind == "regression_evidence"
    ]
    assert len(evidence) == 2
    for artifact, suite_id in zip(evidence, suite_ids, strict=True):
        evidence_payload = json.loads(store.read_prepared(artifact.object_ref))
        assert evidence_payload["suite_artifact_id"] == suite_id
        assert suite_id in artifact.lineage
        assert not (set(suite_ids) - {suite_id}).intersection(artifact.lineage)


def test_repair_rejects_regression_runner_suite_substitution() -> None:
    store = _store()

    outcome = _handler(store, regression_runner=_WrongSuiteRegressionRunner())(
        _context(FakeModelBridge(responses=(_FIX_OPS,)))
    )

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation"}
    )


def test_repair_rejects_regression_runner_wrong_seed() -> None:
    store = _store()
    outcome = _handler(store, regression_runner=_WrongSeedRegressionRunner())(
        _context(FakeModelBridge(responses=(_FIX_OPS,)))
    )

    assert isinstance(outcome, PreparedRunFailure)
    _assert_unverified_input_preview_evidence(
        outcome, store, expected_rules={"checker", "simulation"}
    )


def test_repair_rejects_subject_patch_that_does_not_target_failed_preview() -> None:
    store = _store()
    bad_patch = _current_patch().model_copy(update={"target_snapshot_id": "sha256:stale"})
    store.register(SUBJECT_ID, bad_patch.model_dump(mode="json"))
    bridge = FakeModelBridge(responses=(_FIX_OPS,))

    with pytest.raises(ValueError, match="target differs from the failed preview"):
        _handler(store)(_context(bridge))

    assert bridge.requests == []


def test_repair_rejects_validation_evidence_for_another_subject() -> None:
    store = _store()
    evidence = json.loads(store.read_bytes(EVIDENCE_ID))
    evidence["subject_artifact_id"] = "artifact:stale-subject"
    store.register(EVIDENCE_ID, evidence)
    bridge = FakeModelBridge(responses=(_FIX_OPS,))

    with pytest.raises(ValueError, match="exact failed subject"):
        _handler(store)(_context(bridge))

    assert bridge.requests == []


def test_repair_rejects_non_failed_validation_evidence() -> None:
    store = _store()
    evidence = json.loads(store.read_bytes(EVIDENCE_ID))
    evidence["requirements"][0]["status"] = "passed"
    evidence["overall_status"] = "passed"
    store.register(EVIDENCE_ID, evidence)
    bridge = FakeModelBridge(responses=(_FIX_OPS,))

    with pytest.raises(ValueError, match="exact failed subject"):
        _handler(store)(_context(bridge))

    assert bridge.requests == []


def test_repair_rejects_finding_not_grounded_on_failed_preview() -> None:
    store = _store()
    stale = _target_finding().model_copy(update={"snapshot_id": BASE_SNAPSHOT_ID})
    bridge = FakeModelBridge(responses=(_FIX_OPS,))

    with pytest.raises(ValueError, match="not grounded on the failed preview"):
        _handler(store, findings=(stale,))(_context(bridge))

    assert bridge.requests == []
