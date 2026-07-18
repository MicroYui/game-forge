"""Task 13 — ``patch_validator@1`` (deterministic preview re-verification).

Re-runs the selected checkers + economy simulations + regression suites against a
subject patch's PREVIEW snapshot and seals ONE ``evidence-set@1`` primary plus one
``regression-evidence@1`` per dimension. ``EvidenceSet.overall_status`` IS the
outcome code; the auto-apply proof is sealed ONLY for the exact deterministic
eligible policy.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import gameforge.apps.worker.components as worker_components
from gameforge.contracts.dsl import Constraint, Predicate
from gameforge.contracts.execution_profiles import (
    MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    ExecutionProfileDefinitionV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    canonical_config_hash,
    execution_profile_payload_hash,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import (
    Finding,
    FindingPayloadV1,
    FindingRevisionV1,
    finding_revision_digest,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    FindingEvidenceBindingV1,
    PatchValidationPayloadV1,
    PreparedRunResult,
    RefReadBindingV1,
    ValidationSubjectBindingV1,
)
from gameforge.contracts.lineage import (
    InvocationVersionBindingV1,
    VersionTuple,
    artifact_id_v2_for,
    build_execution_identity,
)
from gameforge.contracts.playtest import (
    MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES,
    MAX_PLAYTEST_EPISODE_METADATA_CANONICAL_BYTES,
    MAX_PLAYTEST_TRACE_JSON_BYTES,
    MAX_PLAYTEST_TRACE_ROOT_METADATA_CANONICAL_BYTES,
    PLAYTEST_MODEL_CALLS_PER_STEP_OFF,
    PlaytestTraceV1,
    bind_exact_playtest_trace_bytes,
    derive_playtest_trace_markers,
)
from gameforge.contracts.review import ReviewReport
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    AutoApplyOracleEvidenceBindingV1,
    AutoApplyOutcomeEvidenceBindingV1,
    AutoApplyProofV1,
    AutoApplyValidationProfileBindingV1,
    DeterministicOracleRefV1,
    EvidenceSet,
    PatchTargetBindingV1,
    QualifiedOutcomeRuleRefV1,
)
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.checkers.base import CheckerExecutionBinding
from gameforge.spine.sim.economy import EconomyModel, SimResult
from gameforge.platform.run_handlers.patch_validation import (
    AutoApplyEvaluationRequest,
    ExactLinkedFindingRevision,
    PatchValidationHandler,
)
from gameforge.platform.run_handlers.readers import load_snapshot
from gameforge.platform.run_handlers.review import ReviewSimConfig
from gameforge.platform.run_handlers.simulation import validate_economy_simulation_work_budget
from gameforge.platform.run_handlers.validation_common import (
    DETERMINISTIC_VALIDATION_EXECUTION_SEED,
    RegressionSuiteResultV1,
    VALIDATION_SEED_DERIVATION_VERSION,
    derive_validation_subseed,
    content_addressed_artifact_id,
    regression_suite_execution_coverage_binding,
)
from gameforge.platform.publication.payload_binding import (
    FinalSiblingFact,
    bind_final_payload_references,
)
from gameforge.platform.publication.payload_schema import decode_and_validate_artifact_payload
from gameforge.platform.registry.defaults import build_builtin_registry
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    build_context,
    resolved_binding,
    snapshot_bytes,
)

PATCH_VALIDATE_KIND = RunKindRef(kind="patch.validate", version=1)
SUBJECT_ID = "artifact:subject-patch"
BASE_ID = "artifact:base-snapshot"
PREVIEW_ID = "artifact:preview-snapshot"
REVIEW_ID = "artifact:review"
FINDING_EVIDENCE_ID = "artifact:finding-evidence"
REGRESSION_SUITE_ID = "artifact:regression-suite"
REGRESSION_SUITE_2_ID = "artifact:regression-suite:2"
CONFIG_ID = "artifact:candidate-config"
PLAYTEST_TRACE_ID = "artifact:playtest-trace"
CONSTRAINT_ARTIFACT_ID = "artifact:constraint"
CONSTRAINT_SNAPSHOT_ID = "constraint:semantic:1"
_HEX = "a" * 64
_CHECKER = ProfileRefV1(profile_id="checker", version=1)
_SIM = ProfileRefV1(profile_id="sim", version=1)
_VALIDATION = ProfileRefV1(profile_id="validation", version=1)
_ENVIRONMENT = ProfileRefV1(profile_id="environment", version=1)


def _patch_simulation_execution_binding(
    *,
    root_seed: int,
    profile: ProfileRefV1 = _SIM,
    n_agents: int = 6,
    n_ticks: int = 12,
) -> dict[str, object]:
    case_id = f"simulation:{profile.profile_id}@{profile.version}"
    execution_seed = derive_validation_subseed(
        root_seed=root_seed,
        run_kind=PATCH_VALIDATE_KIND,
        profile=profile,
        case_id=case_id,
        replication_index=0,
    )
    return {
        "binding_schema_version": "simulation-expected-finding-binding@1",
        "producer_id": "economy_sim",
        "simulation_profile": profile.model_dump(mode="json"),
        "execution_mode": "single_population@1",
        "seed_binding": {
            "root_seed": root_seed,
            "run_kind": PATCH_VALIDATE_KIND.model_dump(mode="json"),
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "case_id": case_id,
            "replication_index": 0,
            "seed": execution_seed,
            "seed_derivation_version": VALIDATION_SEED_DERIVATION_VERSION,
        },
        "constraint_snapshot_binding_status": "not_applicable",
        "constraint_ids": [],
        "constraint_application": {"status": "not_applicable"},
        "n_agents": n_agents,
        "n_ticks": n_ticks,
    }


def _clean_snapshot() -> bytes:
    npc = Entity(id="npc:1", type=NodeType.NPC, attrs={})
    return snapshot_bytes([npc], [])


def _dangling_snapshot() -> bytes:
    npc = Entity(id="npc:1", type=NodeType.NPC, attrs={})
    dangling = Relation(id="r1", type=EdgeType.DROPS_FROM, src_id="monster:ghost", dst_id="npc:1")
    return snapshot_bytes([npc], [dangling])


def _subject() -> ValidationSubjectBindingV1:
    return ValidationSubjectBindingV1(
        approval_id="approval:1",
        expected_workflow_revision=2,
        subject_head_revision=1,
        subject_artifact_id=SUBJECT_ID,
        subject_digest=_HEX,
        active_validation_run_id="run:1",
    )


def _payload(
    *,
    constraint_snapshot_artifact_id=None,
    candidate_config_export_artifact_ids=(),
    checker_profiles=(_CHECKER,),
    simulation_profiles=(),
    expected_findings=(),
    findings=(),
    review_artifact_ids=(),
    playtest_trace_artifact_ids=(),
    regression_suite_artifact_ids=(),
) -> PatchValidationPayloadV1:
    return PatchValidationPayloadV1(
        subject=_subject(),
        base_snapshot_artifact_id=BASE_ID,
        preview_snapshot_artifact_id=PREVIEW_ID,
        constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
        candidate_config_export_artifact_ids=candidate_config_export_artifact_ids,
        target=RefReadBindingV1(
            ref_name="ref:main", expected_ref=RefValue(artifact_id=BASE_ID, revision=1)
        ),
        validation_policy=_VALIDATION,
        checker_profiles=checker_profiles,
        simulation_profiles=simulation_profiles,
        expected_findings=expected_findings,
        findings=findings,
        review_artifact_ids=review_artifact_ids,
        playtest_trace_artifact_ids=playtest_trace_artifact_ids,
        regression_suite_artifact_ids=regression_suite_artifact_ids,
    )


def _finding(*, status: str, finding_id: str = "f1", snapshot_id: str) -> Finding:
    return Finding(
        id=finding_id,
        source="checker",
        producer_id="checker:graph",
        producer_run_id="run:finding-producer",
        oracle_type="deterministic",
        defect_class="dangling_reference",
        severity="major",
        snapshot_id=snapshot_id,
        status=status,
        message="bound target finding",
    )


def _simulation_finding(*, status: str, snapshot_id: str) -> Finding:
    return Finding(
        id="sim:economy-collapse",
        source="sim",
        producer_id="economy_sim",
        producer_run_id="run:simulation-producer",
        oracle_type="simulation",
        defect_class="economy_collapse",
        severity="major",
        snapshot_id=snapshot_id,
        status=status,
        message="economy collapsed",
    )


def _compiled_finding(
    *,
    status: str,
    snapshot_id: str,
    constraint_id: str = "C_compiled",
    producer_id: str = "checker:asp",
) -> Finding:
    return Finding(
        id="compiled:finding",
        source="checker",
        producer_id=producer_id,
        producer_run_id="run:compiled-checker",
        oracle_type="deterministic",
        defect_class="cyclic_dependency",
        severity="major",
        snapshot_id=snapshot_id,
        constraint_id=constraint_id,
        status=status,
        message="compiled constraint defect",
    )


def _playtest_finding(
    *,
    status: str,
    snapshot_id: str,
    episode_id: str = "episode:1",
    scenario_spec_artifact_id: str = "artifact:scenario",
) -> Finding:
    return Finding(
        id="playtest-incomplete:agent_stopped",
        source="playtest",
        producer_id="playtest.completion_oracle",
        producer_run_id="run:playtest-producer",
        oracle_type="deterministic",
        defect_class="playtest_incomplete",
        severity="major",
        snapshot_id=snapshot_id,
        evidence={
            "episode_id": episode_id,
            "scenario_spec_artifact_id": scenario_spec_artifact_id,
            "terminal_reason": "agent_stopped",
        },
        minimal_repro={
            "episode_id": episode_id,
            "scenario_spec_artifact_id": scenario_spec_artifact_id,
        },
        status=status,
        message="completion oracle was not satisfied",
    )


def _finding_revision(
    finding: Finding,
    *,
    finding_id: str | None = None,
) -> FindingRevisionV1:
    return FindingRevisionV1(
        finding_id=finding_id or finding.id,
        revision=1,
        created_at="2026-07-17T09:00:00Z",
        payload=FindingPayloadV1.model_validate(
            finding.model_dump(
                mode="python",
                exclude={"id", "finding_schema_version", "created_at"},
            )
        ),
    )


def _finding_binding(
    revision: FindingRevisionV1,
    *,
    evidence_artifact_id: str = FINDING_EVIDENCE_ID,
) -> FindingEvidenceBindingV1:
    return FindingEvidenceBindingV1(
        finding_id=revision.finding_id,
        finding_revision=revision.revision,
        evidence_artifact_id=evidence_artifact_id,
        finding_digest=finding_revision_digest(revision),
    )


class _ExactFindingRevisionLoader:
    def __init__(
        self,
        *revisions: FindingRevisionV1,
        evidence_artifact_id: str = FINDING_EVIDENCE_ID,
        linked_evidence_by_finding: dict[tuple[str, int], str] | None = None,
    ) -> None:
        self._revisions = {(item.finding_id, item.revision): item for item in revisions}
        self._linked_evidence_by_finding = (
            {identity: evidence_artifact_id for identity in self._revisions}
            if linked_evidence_by_finding is None
            else dict(linked_evidence_by_finding)
        )

    def load_exact(
        self,
        *,
        finding_id: str,
        finding_revision: int,
        finding_digest: str,
    ) -> FindingRevisionV1:
        revision = self._revisions[(finding_id, finding_revision)]
        if finding_revision_digest(revision) != finding_digest:
            raise IntegrityViolation("bound Finding digest differs from exact revision")
        return revision

    def load_many_exact(
        self,
        *,
        bindings: tuple[FindingEvidenceBindingV1, ...],
    ) -> tuple[FindingRevisionV1, ...]:
        return tuple(
            self.load_exact(
                finding_id=binding.finding_id,
                finding_revision=binding.finding_revision,
                finding_digest=binding.finding_digest,
            )
            for binding in bindings
        )

    def list_linked_exact(
        self,
        *,
        evidence_artifact_ids: tuple[str, ...],
    ) -> tuple[ExactLinkedFindingRevision, ...]:
        return tuple(
            ExactLinkedFindingRevision(
                evidence_artifact_id=evidence_artifact_id,
                revision=revision,
            )
            for identity, revision in self._revisions.items()
            if (evidence_artifact_id := self._linked_evidence_by_finding.get(identity))
            in evidence_artifact_ids
        )


def _playtest_trace(
    *,
    completed: bool,
    root_seed: int = 11,
    environment_profile: ProfileRefV1 = _ENVIRONMENT,
    planner_policy: ProfileRefV1 = ProfileRefV1(profile_id="planner", version=1),
    planner_profile_payload_hash: str = _HEX,
    completion_oracle_id: str = "all-quests",
    config_artifact_id: str = CONFIG_ID,
    task_suite_artifact_id: str = "artifact:task-suite",
    scenario_spec_artifact_id: str = "artifact:scenario",
    episode_id: str = "episode:1",
    actual_model_calls: int = 1,
) -> PlaytestTraceV1:
    task_suite_id = task_suite_artifact_id
    run_kind = RunKindRef(kind="playtest.run", version=1)
    execution_seed = derive_validation_subseed(
        root_seed=root_seed,
        run_kind=run_kind,
        profile=environment_profile,
        case_id=f"{task_suite_id}:{episode_id}",
        replication_index=0,
    )
    state_hash = f"sha256:{'0' * 64}"
    terminal_reason = "completion_oracle_satisfied" if completed else "agent_stopped"
    requested_steps = 1
    per_episode_upper_bound = min(
        MAX_PLAYTEST_TRACE_JSON_BYTES,
        2 + requested_steps * (MAX_PLAYTEST_ACTION_RECORD_CANONICAL_BYTES + 1),
    )
    raw = {
        "playtest_trace_schema_version": "playtest-trace@1",
        "config_artifact_id": config_artifact_id,
        "constraint_snapshot_artifact_id": "artifact:constraint",
        "task_suite_artifact_id": task_suite_id,
        "environment_profile": environment_profile.model_dump(mode="json"),
        "planner_policy": planner_policy.model_dump(mode="json"),
        "env_contract_version": "env@1",
        "interaction_mode": "autonomous",
        "seed": root_seed,
        "requested_max_steps_per_episode": requested_steps,
        "planner_memory_mode": "off",
        "execution_envelope": {
            "planner_profile_payload_hash": planner_profile_payload_hash,
            "selected_episode_count": 1,
            "total_step_limit": requested_steps,
            "model_call_upper_bound": (requested_steps * PLAYTEST_MODEL_CALLS_PER_STEP_OFF),
            "total_trace_byte_upper_bound": (
                MAX_PLAYTEST_TRACE_ROOT_METADATA_CANONICAL_BYTES
                + per_episode_upper_bound
                + MAX_PLAYTEST_EPISODE_METADATA_CANONICAL_BYTES
            ),
            "actual_model_calls": actual_model_calls,
            "total_action_count": 0,
            "total_action_trace_bytes": 2,
            "actual_trace_bytes": 1,
        },
        "episodes": [
            {
                "episode_id": episode_id,
                "scenario_spec_artifact_id": scenario_spec_artifact_id,
                "seed": execution_seed,
                "seed_binding": {
                    "seed_derivation_version": "subseed@1",
                    "root_seed": root_seed,
                    "run_kind": run_kind.model_dump(mode="json"),
                    "profile": environment_profile.model_dump(mode="json"),
                    "case_id": f"{task_suite_id}:{episode_id}",
                    "replication_index": 0,
                    "seed": execution_seed,
                },
                "step_budget": requested_steps,
                "execution_step_limit": requested_steps,
                "completion_oracle": {
                    "oracle_id": completion_oracle_id,
                    "version": 1,
                    "params_schema_id": "state-predicate-params@1",
                    "params": {"predicate": "all_quests_completed"},
                },
                "completed": completed,
                "terminal_reason": terminal_reason,
                "initial_state_hash": state_hash,
                "final_state_hash": state_hash,
                "action_trace": [],
                "markers": [
                    marker.model_dump(mode="json")
                    for marker in derive_playtest_trace_markers(
                        (),
                        initial_state_hash=state_hash,
                        final_state_hash=state_hash,
                        terminal_reason=terminal_reason,
                    )
                ],
            }
        ],
    }
    return PlaytestTraceV1.model_validate(bind_exact_playtest_trace_bytes(raw))


class _FakeAutoApplyEvaluator:
    """Builds a self-consistent auto-apply proof for the eligible scenario."""

    def evaluate(self, request: AutoApplyEvaluationRequest) -> AutoApplyProofV1 | None:
        scope = DomainScope(domain_ids=("economy",))
        registry = AutoApplyPolicyRegistryRefV1(registry_version="reg@1", registry_digest=_HEX)
        policy = AutoApplyPolicyRefV1(
            registry=registry, policy_id="auto", policy_version="1", policy_digest=_HEX
        )
        oracle = DeterministicOracleRefV1(
            oracle_id="checker", oracle_version="1", oracle_digest=_HEX
        )
        first_requirement = request.requirements[0]
        return AutoApplyProofV1(
            subject_artifact_id=request.subject_artifact_id,
            subject_digest=request.subject_digest,
            target_binding=request.target_binding,
            affected_domain_scope=scope,
            validation_evidence_artifact_id=request.validation_evidence_artifact_id,
            regression_evidence_artifact_ids=request.regression_evidence_artifact_ids,
            validation_profile_binding=AutoApplyValidationProfileBindingV1(
                validation_profile=request.validation_profile,
                validation_profile_payload_hash=request.validation_profile_payload_hash,
                policy=policy,
            ),
            deterministic_oracle_evidence=(
                AutoApplyOracleEvidenceBindingV1(
                    oracle=oracle,
                    evaluated_domain_scope=scope,
                    evidence_artifact_id=first_requirement.evidence_artifact_id,
                    evidence_payload_hash=_HEX,
                ),
            ),
            required_outcome_evidence=(
                AutoApplyOutcomeEvidenceBindingV1(
                    rule=QualifiedOutcomeRuleRefV1(
                        resolved_policy_id="patch-validation", outcome_rule_id="regression"
                    ),
                    requirement_id=first_requirement.requirement_id,
                    evidence_artifact_id=first_requirement.evidence_artifact_id,
                    evidence_payload_hash=_HEX,
                ),
            ),
            policy=policy,
        )


class _CleanChecker:
    """A deterministic checker that reports no defect (a vetted preview)."""

    id = "graph"

    def check(self, snapshot, nav=None):
        return []


class _CleanSimulator:
    """An economy simulator whose horizon shows no invariant violation."""

    def run(self, model, *, seed, n_agents, n_ticks) -> SimResult:
        return SimResult(distributions={}, invariants=[], sensitivity={})


class _RecordingSimulator(_CleanSimulator):
    def __init__(self) -> None:
        self.seeds: list[int] = []

    def run(self, model, *, seed, n_agents, n_ticks) -> SimResult:
        self.seeds.append(seed)
        return super().run(model, seed=seed, n_agents=n_agents, n_ticks=n_ticks)


class _RecordingRegressionRunner:
    def __init__(self) -> None:
        self.seeds: list[int] = []

    def run(self, request) -> RegressionSuiteResultV1:
        self.seeds.append(request.seed)
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="passed",
            env_contract_version="suite-env@1",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "status": "passed",
                "seed": request.seed,
            },
            action_work_units=10,
        )


class _FailingRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        finding = Finding(
            id="regression:failed",
            source="playtest",
            producer_id="agent-env-action-replay@1",
            producer_run_id="regression-runner",
            oracle_type="deterministic",
            defect_class="regression_expectation_mismatch",
            severity="major",
            snapshot_id=request.snapshot_id,
            status="confirmed",
            message="committed regression expectation failed",
        )
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="failed",
            env_contract_version="suite-env@1",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "status": "failed",
                "findings": [finding.model_dump(mode="json")],
            },
            action_work_units=10,
        )


class _UnprovenRegressionRunner:
    reason = "adapter_environment_unavailable"

    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="unproven",
            reason_code=self.reason,
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "status": "unproven",
            },
        )


class _MeasuredWorkRegressionRunner:
    def __init__(self, work_units: tuple[int | None, ...]) -> None:
        self.work_units = list(work_units)
        self.remaining_limits: list[int | None] = []

    def run(self, request) -> RegressionSuiteResultV1:
        self.remaining_limits.append(request.max_action_work_units)
        work_units = self.work_units.pop(0)
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="passed",
            env_contract_version="suite-env@1",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "status": "passed",
            },
            action_work_units=work_units,
        )


def _store(
    *,
    snapshot: bytes | None = None,
    store: FakeArtifactStore | None = None,
) -> FakeArtifactStore:
    if store is None:
        store = FakeArtifactStore()
    store.register(BASE_ID, _clean_snapshot())
    store.register(PREVIEW_ID, snapshot if snapshot is not None else _clean_snapshot())
    preview = load_snapshot(store, PREVIEW_ID)
    store.register(REVIEW_ID, ReviewReport(snapshot_id=preview.snapshot_id).model_dump(mode="json"))
    store.register(
        FINDING_EVIDENCE_ID,
        {
            "payload_schema_version": "checker-report@1",
            "snapshot_id": preview.snapshot_id,
            "checker_ids": ["graph"],
            "defect_classes": [],
            "constraint_application": [],
            "findings": [
                _finding(status="fixed", snapshot_id=preview.snapshot_id).model_dump(mode="json")
            ],
        },
    )
    store.register(REGRESSION_SUITE_ID, {"suite": "s"})
    store.register(REGRESSION_SUITE_2_ID, {"suite": "s2"})
    store.register(CONFIG_ID, {"config": "candidate"})
    store.register(
        CONSTRAINT_ARTIFACT_ID,
        {"dsl_grammar_version": "dsl@1", "constraints": []},
    )
    return store


def _register_historical_evidence(
    store: FakeArtifactStore,
    *,
    kind: str,
    payload_schema_id: str,
    payload: object,
    version_tuple: VersionTuple | None = None,
    lineage: tuple[str, ...] = (),
    meta: dict[str, object] | None = None,
) -> str:
    return store.register_exact_artifact(
        kind=kind,
        payload_schema_id=payload_schema_id,
        payload=payload,
        version_tuple=version_tuple,
        lineage=lineage,
        meta=meta,
    ).artifact_id


def _playtest_execution_identity(
    *,
    prompt_version: str = "playtest-prompt@1",
    model_snapshot: str = "provider/playtest/model@1",
    agent_graph_version: str = "playtest-graph@1",
    execution_source: str = "cassette_replay",
    routing_decision_kind: str = "native",
    consumed_call_count: int = 1,
):
    bindings = tuple(
        InvocationVersionBindingV1(
            attempt_no=1,
            call_ordinal=ordinal,
            route_ordinal=1,
            transport_attempt=(1 if execution_source == "online" else None),
            routing_decision_kind=routing_decision_kind,
            routing_decision_id=f"route:{routing_decision_kind}:{ordinal}",
            agent_node_id="playtest.executor",
            prompt_version=prompt_version,
            model_snapshot=model_snapshot,
            tool_version="playtest-agent-node@1",
            execution_source=execution_source,
            response_consumed=True,
        )
        for ordinal in range(1, consumed_call_count + 1)
    )
    return build_execution_identity(
        scope="artifact",
        bindings=bindings,
        agent_graph_version=agent_graph_version,
    )


def _register_authoritative_playtest_trace(
    store: FakeArtifactStore,
    *,
    trace: PlaytestTraceV1,
    ir_snapshot_id: str,
    prompt_version: str = "playtest-prompt@1",
    model_snapshot: str = "provider/playtest/model@1",
    agent_graph_version: str = "playtest-graph@1",
    execution_mode: str = "replay",
    routing_decision_kind: str = "native",
    cassette_id: str = f"sha256:{'1' * 64}",
    # Mirrors the real Playtest producer projection: playtest traces do not
    # project a document version into their VersionTuple.
    doc_version: str | None = None,
    artifact_constraint_snapshot_id: str = CONSTRAINT_SNAPSHOT_ID,
    producer_tool_version: str = "playtest@1",
    artifact_env_contract_version: str | None = None,
    consumed_call_count: int = 1,
    omit_execution_identity: bool = False,
    omit_lineage_ids: tuple[str, ...] = (),
) -> str:
    source_by_mode = {
        "live": "online",
        "record": "online",
        "replay": "cassette_replay",
    }
    identity = _playtest_execution_identity(
        prompt_version=prompt_version,
        model_snapshot=model_snapshot,
        agent_graph_version=agent_graph_version,
        execution_source=source_by_mode[execution_mode],
        routing_decision_kind=routing_decision_kind,
        consumed_call_count=consumed_call_count,
    )
    cassette_bound = execution_mode in {"record", "replay"}
    version_tuple = VersionTuple(
        doc_version=doc_version,
        ir_snapshot_id=ir_snapshot_id,
        constraint_snapshot_id=artifact_constraint_snapshot_id,
        prompt_version=(
            None if omit_execution_identity else identity.prompt_projection.tuple_value
        ),
        model_snapshot=(None if omit_execution_identity else identity.model_projection.tuple_value),
        agent_graph_version=(None if omit_execution_identity else identity.agent_graph_version),
        tool_version=producer_tool_version,
        env_contract_version=(
            trace.env_contract_version
            if artifact_env_contract_version is None
            else artifact_env_contract_version
        ),
        seed=trace.seed,
        cassette_id=(cassette_id if cassette_bound else None),
    )
    required_lineage = {
        trace.config_artifact_id,
        trace.constraint_snapshot_artifact_id,
        trace.task_suite_artifact_id,
        *(episode.scenario_spec_artifact_id for episode in trace.episodes),
    }
    lineage = tuple(sorted(required_lineage.difference(omit_lineage_ids)))
    meta: dict[str, object] = {
        "replayability": "online_only" if execution_mode == "live" else "cassette_replay"
    }
    if not omit_execution_identity:
        meta["execution_identity"] = identity
    return _register_historical_evidence(
        store,
        kind="playtest_trace",
        payload_schema_id="playtest-trace@1",
        payload=trace.model_dump(mode="json"),
        version_tuple=version_tuple,
        lineage=lineage,
        meta=meta,
    )


def _handler(
    store: FakeArtifactStore,
    *,
    checker_resolver=lambda profile, constraints: _CleanChecker(),
    sim_config_resolver=None,
    simulator=None,
    wire_finding_authority: bool = True,
    **kwargs,
) -> PatchValidationHandler:
    if wire_finding_authority:
        kwargs.setdefault("finding_revision_loader", _ExactFindingRevisionLoader())
    return PatchValidationHandler(
        blobs=store,
        store=store,
        checker_resolver=checker_resolver,
        sim_config_resolver=sim_config_resolver
        or (lambda profile: ReviewSimConfig(n_agents=6, n_ticks=12, max_work_units=2_000_000)),
        simulator=simulator if simulator is not None else _CleanSimulator(),
        **kwargs,
    )


def _context(
    store: FakeArtifactStore,
    payload: PatchValidationPayloadV1,
    *,
    seed: int | None = None,
    constraint_snapshot_id: str | None = None,
    env_contract_version: str | None = None,
    resolved_profiles_override=None,
):
    preview = load_snapshot(store, payload.preview_snapshot_artifact_id)
    return build_context(
        params=payload,
        kind=PATCH_VALIDATE_KIND,
        resolved_profiles=(
            resolved_profiles_override
            if resolved_profiles_override is not None
            else (
                resolved_binding(
                    "/params/validation_policy",
                    profile_id="validation",
                    version=1,
                    kind="validation",
                ),
                *(
                    resolved_binding(
                        f"/params/checker_profiles/{index}",
                        profile_id=profile.profile_id,
                        version=profile.version,
                        kind="checker",
                    )
                    for index, profile in enumerate(payload.checker_profiles)
                ),
                *(
                    resolved_binding(
                        f"/params/simulation_profiles/{index}",
                        profile_id=profile.profile_id,
                        version=profile.version,
                        kind="simulation",
                    )
                    for index, profile in enumerate(payload.simulation_profiles)
                ),
            )
        ),
        seed=seed,
        version_tuple=VersionTuple(
            doc_version="doc@1",
            ir_snapshot_id=preview.snapshot_id,
            constraint_snapshot_id=constraint_snapshot_id,
            env_contract_version=env_contract_version,
            tool_version="patch-validation@1",
            seed=seed,
        ),
    )


def _read_evidence_set(store: FakeArtifactStore, outcome: PreparedRunResult) -> EvidenceSet:
    primary = outcome.artifacts[outcome.primary_index]
    return EvidenceSet.model_validate(json.loads(store.read_prepared(primary.object_ref)))


def test_empty_validation_request_is_explicitly_unproven() -> None:
    store = _store()
    outcome = _handler(store)(_context(store, _payload(checker_profiles=())))

    evidence = _read_evidence_set(store, outcome)
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert evidence.overall_status == "unproven"
    assert [item.requirement_id for item in evidence.requirements] == [
        "validation:required-dimension"
    ]
    assert evidence.requirements[0].reason_code == "no_validation_dimension_selected"
    companion = outcome.artifacts[1]
    sealed = decode_and_validate_artifact_payload(
        payload_schema_id=companion.payload_schema_id,
        blob=store.read_prepared(companion.object_ref),
    )
    assert sealed["status"] == "unproven"
    assert sealed["lineage_suite_artifact_ids"] == []


def test_patch_validation_rejects_incomplete_exact_executor_profile_closure() -> None:
    store = _store()
    payload = _payload()
    validation_only = (
        resolved_binding(
            "/params/validation_policy",
            profile_id="validation",
            version=1,
            kind="validation",
        ),
    )

    with pytest.raises(IntegrityViolation, match="execution profile"):
        _handler(store)(
            _context(
                store,
                payload,
                resolved_profiles_override=validation_only,
            )
        )


@pytest.mark.parametrize("forgery", ("missing", "extra", "duplicate", "catalog"))
def test_patch_validation_rejects_non_exact_profile_sets_before_input_reads(
    forgery: str,
) -> None:
    class ReadTrackingStore(FakeArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0

        def read_bytes(self, artifact_id: str) -> bytes:
            self.read_count += 1
            return super().read_bytes(artifact_id)

    store = _store(store=ReadTrackingStore())
    payload = _payload()
    context = _context(store, payload)
    profiles = list(context.payload.resolved_profiles)
    if forgery == "missing":
        profiles.pop()
    elif forgery == "extra":
        profiles.append(
            resolved_binding(
                "/params/injected_profile",
                profile_id="injected",
                version=1,
                kind="checker",
            )
        )
    elif forgery == "duplicate":
        profiles.append(profiles[-1])
    else:
        profiles[-1] = profiles[-1].model_copy(update={"catalog_digest": "b" * 64})
    forged_payload = context.payload.model_copy(update={"resolved_profiles": tuple(profiles)})
    forged_context = replace(context, payload=forged_payload)
    store.read_count = 0

    with pytest.raises(IntegrityViolation, match="execution profile"):
        _handler(store)(forged_context)

    assert store.read_count == 0
    assert store.put_count == 0


@pytest.mark.parametrize("authority_failure", ("payload_hash", "lifecycle"))
def test_patch_validation_resolves_profile_authority_before_input_reads(
    authority_failure: str,
) -> None:
    class ReadTrackingStore(FakeArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0

        def read_bytes(self, artifact_id: str) -> bytes:
            self.read_count += 1
            return super().read_bytes(artifact_id)

    store = _store(store=ReadTrackingStore())
    payload = _payload()
    context = _context(store, payload)
    if authority_failure == "payload_hash":
        profiles = tuple(
            binding.model_copy(update={"profile_payload_hash": "0" * 64})
            if binding.field_path == "/params/checker_profiles/0"
            else binding
            for binding in context.payload.resolved_profiles
        )
        context = replace(
            context,
            payload=context.payload.model_copy(update={"resolved_profiles": profiles}),
        )
    seen: list[str] = []

    def reject_authority(binding, *, llm_execution_mode, run_kind) -> None:
        seen.append(binding.field_path)
        assert llm_execution_mode == "not_applicable"
        assert run_kind == PATCH_VALIDATE_KIND
        if binding.field_path == "/params/checker_profiles/0":
            if authority_failure == "payload_hash":
                assert binding.profile_payload_hash == "0" * 64
            raise IntegrityViolation(f"execution profile {authority_failure} is invalid")

    store.read_count = 0
    with pytest.raises(IntegrityViolation, match=authority_failure):
        _handler(store, profile_binding_validator=reject_authority)(context)

    assert seen == ["/params/validation_policy", "/params/checker_profiles/0"]
    assert store.read_count == 0
    assert store.put_count == 0


def test_production_patch_sim_config_resolves_only_the_exact_catalog_binding() -> None:
    registry = build_builtin_registry()
    profile = ProfileRefV1(profile_id="builtin.simulation", version=1)
    catalog = next(
        catalog
        for catalog in registry.list_execution_profile_catalogs()
        if any(definition.profile == profile for definition in catalog.definitions)
    )
    binding = registry.resolve_execution_profile(
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        field_path="/params/simulation_profiles/0",
        profile=profile,
        expected_profile_kind="simulation",
    )
    resolver = worker_components._build_patch_sim_config_resolver(registry)

    assert resolver(binding).n_agents > 0
    with pytest.raises(IntegrityViolation, match="catalog history"):
        resolver(binding.model_copy(update={"catalog_digest": "b" * 64}))


@pytest.mark.parametrize("forgery", ("compiled_native", "output_defect"))
def test_production_patch_checker_enforces_exact_profile_taxonomy(forgery: str) -> None:
    builtin = next(
        definition
        for catalog in build_builtin_registry().list_execution_profile_catalogs()
        for definition in catalog.definitions
        if definition.profile_kind == "checker"
        and definition.profile.profile_id == "builtin.checker"
    )
    config = dict(builtin.config)
    config["allowed_checker_ids"] = ["graph"]
    config["allowed_defect_classes"] = [
        "cyclic_dependency" if forgery == "output_defect" else "reward_out_of_range"
    ]
    definition = ExecutionProfileDefinitionV1.model_validate(
        builtin.model_copy(
            update={
                "config": config,
                "config_hash": canonical_config_hash(config),
            }
        ).model_dump(mode="python")
    )
    binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/checker_profiles/0",
        profile=definition.profile,
        expected_profile_kind="checker",
        profile_payload_hash=execution_profile_payload_hash(definition),
        catalog_version=1,
        catalog_digest="a" * 64,
    )

    class _ExactRegistry:
        def resolve_execution_profile_binding(self, actual):
            assert actual == binding
            return definition, SimpleNamespace(state="active")

    constraints = (
        [
            Constraint(
                id="C_numeric",
                kind="numeric",
                oracle="deterministic",
                **{"assert": "reward >= 0"},
                severity="major",
            )
        ]
        if forgery == "compiled_native"
        else []
    )
    checker = worker_components._build_patch_checker_resolver(_ExactRegistry())(
        binding,
        constraints,
    )
    snapshot = load_snapshot(
        _store(snapshot=_dangling_snapshot() if forgery == "output_defect" else _clean_snapshot()),
        PREVIEW_ID,
    )

    with pytest.raises(IntegrityViolation, match=r"exact profile.*taxonomy"):
        checker.check(snapshot)


def test_default_regression_runner_is_unavailable_not_passing() -> None:
    store = _store()
    outcome = _handler(store)(
        _context(
            store,
            _payload(
                checker_profiles=(),
                regression_suite_artifact_ids=(REGRESSION_SUITE_ID,),
            ),
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == f"regression:{REGRESSION_SUITE_ID}"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "regression_runner_unavailable"


def test_regression_work_ledger_debits_each_suite_and_binds_its_lineage() -> None:
    store = _store()
    first_work = 15_000_000
    second_work = MAX_REPAIR_REGRESSION_WORK_UNITS_V1 - first_work
    runner = _MeasuredWorkRegressionRunner((first_work, second_work))
    outcome = _handler(store, regression_runner=runner)(
        _context(
            store,
            _payload(
                checker_profiles=(),
                regression_suite_artifact_ids=(
                    REGRESSION_SUITE_ID,
                    REGRESSION_SUITE_2_ID,
                ),
            ),
            seed=23,
        )
    )

    assert outcome.summary.outcome_code == "patch_validation_passed"
    regression_artifacts = [
        item for item in outcome.artifacts if item.payload_schema_id == "regression-evidence@1"
    ]
    assert {item.version_tuple.env_contract_version for item in regression_artifacts} == {
        "suite-env@1"
    }
    assert runner.remaining_limits == [
        MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
        second_work,
    ]
    evidence_by_requirement = {
        item.meta.get("requirement_id"): item
        for item in outcome.artifacts
        if item.payload_schema_id == "regression-evidence@1"
    }
    first = evidence_by_requirement[f"regression:{REGRESSION_SUITE_ID}"]
    second = evidence_by_requirement[f"regression:{REGRESSION_SUITE_2_ID}"]
    assert REGRESSION_SUITE_ID in first.lineage
    assert REGRESSION_SUITE_2_ID not in first.lineage
    assert REGRESSION_SUITE_2_ID in second.lineage
    assert REGRESSION_SUITE_ID not in second.lineage
    for artifact, suite_id in (
        (first, REGRESSION_SUITE_ID),
        (second, REGRESSION_SUITE_2_ID),
    ):
        sealed = decode_and_validate_artifact_payload(
            payload_schema_id=artifact.payload_schema_id,
            blob=store.read_prepared(artifact.object_ref),
        )
        assert sealed["lineage_suite_artifact_ids"] == [suite_id]


@pytest.mark.parametrize(
    ("reported_work", "message"),
    (
        (None, "omitted measured action work"),
        (MAX_REPAIR_REGRESSION_WORK_UNITS_V1 + 1, "aggregate work budget"),
    ),
)
def test_passing_regression_requires_bounded_measured_work(
    reported_work: int | None,
    message: str,
) -> None:
    store = _store()
    with pytest.raises(IntegrityViolation, match=message):
        _handler(
            store,
            regression_runner=_MeasuredWorkRegressionRunner((reported_work,)),
        )(
            _context(
                store,
                _payload(
                    checker_profiles=(),
                    regression_suite_artifact_ids=(REGRESSION_SUITE_ID,),
                ),
                seed=29,
            )
        )


@pytest.mark.parametrize(
    ("finding_status", "expected_status"),
    (
        ("confirmed", "failed"),
        ("unproven", "unproven"),
        ("fixed", "passed"),
        ("dismissed", "passed"),
    ),
)
def test_exact_scoped_playtest_finding_status_affects_validation_without_embedded_findings(
    finding_status: str,
    expected_status: str,
) -> None:
    store = _store()
    preview = load_snapshot(store, PREVIEW_ID)
    finding = Finding(
        id="local-playtest-finding-id",
        source="playtest",
        producer_id="playtest-completion@1",
        producer_run_id="run:playtest-producer",
        oracle_type="deterministic",
        defect_class="playtest_completion",
        severity="major",
        snapshot_id=preview.snapshot_id,
        status=finding_status,
        message="exact playtest completion finding",
    )
    revision = _finding_revision(
        finding,
        finding_id="finding-series:playtest-run:episode-1:completion",
    )
    binding = _finding_binding(revision)
    # A playtest trace has no embedded ``findings`` array, and its local marker IDs
    # are not the scoped Finding-series identity. The immutable revision authority,
    # not this evidence blob, owns the status used by validation.
    store.register(FINDING_EVIDENCE_ID, _playtest_trace(completed=True).model_dump(mode="json"))

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(revision),
    )(_context(store, _payload(checker_profiles=(), findings=(binding,))))

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == "finding:finding-series:playtest-run:episode-1:completion@1"
    )
    assert outcome.summary.outcome_code == f"patch_validation_{expected_status}"
    assert requirement.status == expected_status


def test_target_finding_without_exact_revision_authority_fails_closed() -> None:
    store = _store()
    preview = load_snapshot(store, PREVIEW_ID)
    revision = _finding_revision(_finding(status="fixed", snapshot_id=preview.snapshot_id))

    with pytest.raises(IntegrityViolation, match="exact Finding revision authority"):
        _handler(store, wire_finding_authority=False)(
            _context(
                store,
                _payload(
                    checker_profiles=(),
                    findings=(_finding_binding(revision),),
                ),
            )
        )


def test_omitted_confirmed_playtest_finding_cannot_pass_selected_trace_closure() -> None:
    store = _store()
    preview = load_snapshot(store, PREVIEW_ID)
    trace = _playtest_trace(completed=True)
    store.register(PLAYTEST_TRACE_ID, trace.model_dump(mode="json"))
    revision = _finding_revision(
        Finding(
            id="producer-local-playtest-id",
            source="playtest",
            producer_id="playtest-completion@1",
            producer_run_id="run:playtest-producer",
            oracle_type="deterministic",
            defect_class="playtest_state_violation",
            severity="major",
            snapshot_id=preview.snapshot_id,
            status="confirmed",
            message="trace completed but violated a deterministic state predicate",
        ),
        finding_id="finding-series:playtest-run:episode-1:state",
    )

    with pytest.raises(IntegrityViolation, match="exactly cover selected evidence"):
        _handler(
            store,
            finding_revision_loader=_ExactFindingRevisionLoader(
                revision,
                evidence_artifact_id=PLAYTEST_TRACE_ID,
            ),
        )(
            _context(
                store,
                _payload(
                    constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                    candidate_config_export_artifact_ids=(CONFIG_ID,),
                    checker_profiles=(),
                    playtest_trace_artifact_ids=(PLAYTEST_TRACE_ID,),
                    findings=(),
                ),
                constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
                env_contract_version="env@1",
            )
        )


@pytest.mark.parametrize(
    ("finding_status", "expected_status"),
    (("confirmed", "failed"), ("unproven", "unproven")),
)
def test_supporting_review_status_affects_validation(
    finding_status: str,
    expected_status: str,
) -> None:
    store = _store()
    preview = load_snapshot(store, PREVIEW_ID)
    report = ReviewReport.partition(
        preview.snapshot_id,
        [_finding(status=finding_status, snapshot_id=preview.snapshot_id)],
    )
    store.register(REVIEW_ID, report.model_dump(mode="json"))

    outcome = _handler(store)(
        _context(
            store,
            _payload(checker_profiles=(), review_artifact_ids=(REVIEW_ID,)),
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item for item in evidence.requirements if item.requirement_id == f"review:{REVIEW_ID}"
    )
    assert outcome.summary.outcome_code == f"patch_validation_{expected_status}"
    assert requirement.status == expected_status


@pytest.mark.parametrize(
    ("completed", "expected_status"),
    ((True, "passed"), (False, "failed")),
)
def test_supporting_playtest_completion_affects_validation(
    completed: bool,
    expected_status: str,
) -> None:
    store = _store()
    trace = _playtest_trace(completed=completed)
    preview = load_snapshot(store, PREVIEW_ID)
    playtest_trace_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=trace,
        ir_snapshot_id=preview.snapshot_id,
    )
    outcome = _handler(store)(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(CONFIG_ID,),
                checker_profiles=(),
                playtest_trace_artifact_ids=(playtest_trace_artifact_id,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == f"playtest:{playtest_trace_artifact_id}"
    )
    assert outcome.summary.outcome_code == f"patch_validation_{expected_status}"
    assert requirement.status == expected_status


@pytest.mark.parametrize("forgery", ("missing_envelope", "missing_identity"))
def test_supporting_completed_playtest_requires_exact_execution_authority(
    forgery: str,
) -> None:
    store = _store()
    trace = _playtest_trace(completed=True)
    preview = load_snapshot(store, PREVIEW_ID)
    if forgery == "missing_envelope":
        artifact_id = PLAYTEST_TRACE_ID
        store.register(artifact_id, trace.model_dump(mode="json"))
    else:
        artifact_id = _register_authoritative_playtest_trace(
            store,
            trace=trace,
            ir_snapshot_id=preview.snapshot_id,
            omit_execution_identity=(forgery == "missing_identity"),
        )

    outcome = _handler(store)(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(CONFIG_ID,),
                checker_profiles=(),
                playtest_trace_artifact_ids=(artifact_id,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == f"playtest:{artifact_id}"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "playtest_execution_authority_unavailable"


def test_checker_receives_exact_resolved_constraints_and_artifacts_project_run_tuple() -> None:
    store = _store()
    constraint = Constraint(
        id="C_required",
        kind="structural",
        oracle="deterministic",
        **{"assert": "relations_resolve"},
        severity="major",
    )
    store.register(
        CONSTRAINT_ARTIFACT_ID,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    seen: list[Constraint] = []

    def resolve_checker(profile, constraints):
        seen.extend(constraints)
        return _CleanChecker()

    context = _context(
        store,
        _payload(constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID),
        seed=19,
        constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
        env_contract_version="env@1",
    )
    outcome = _handler(
        store,
        checker_resolver=resolve_checker,
    )(context)

    assert seen == [constraint]
    assert outcome.summary.outcome_code == "patch_validation_passed"
    assert CONSTRAINT_ARTIFACT_ID in outcome.artifacts[outcome.primary_index].lineage
    assert all(
        artifact.version_tuple.doc_version == "doc@1"
        and artifact.version_tuple.ir_snapshot_id == context.payload.version_tuple.ir_snapshot_id
        and artifact.version_tuple.constraint_snapshot_id == CONSTRAINT_SNAPSHOT_ID
        and artifact.version_tuple.env_contract_version == "env@1"
        and artifact.version_tuple.seed == 19
        for artifact in outcome.artifacts
    )


def test_checker_run_budget_counts_every_profile_and_compiled_constraint() -> None:
    entities = [Entity(id=f"npc:{index}", type=NodeType.NPC, attrs={}) for index in range(64)]
    store = _store(snapshot=snapshot_bytes(entities, []))
    constraints = [
        Constraint(
            id=f"C_{index}",
            kind="structural",
            oracle="deterministic",
            **{"assert": "relations_resolve"},
            severity="major",
        )
        for index in range(256)
    ]
    store.register(
        CONSTRAINT_ARTIFACT_ID,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [item.model_dump(mode="json", by_alias=True) for item in constraints],
        },
    )
    profiles = (
        ProfileRefV1(profile_id="checker-a", version=1),
        ProfileRefV1(profile_id="checker-b", version=1),
    )

    with pytest.raises(IntegrityViolation, match="aggregate work budget"):
        _handler(store)(
            _context(
                store,
                _payload(
                    constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                    checker_profiles=profiles,
                ),
                constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            )
        )


def test_simulation_run_budget_keeps_child_and_aggregate_limits_separate() -> None:
    store = _store()
    model = EconomyModel.from_snapshot(load_snapshot(store, PREVIEW_ID))
    per_profile_work = validate_economy_simulation_work_budget(
        model,
        n_agents=1,
        n_ticks=1,
        replication_count=1,
        max_work_units=10_000,
    )
    profiles = (
        ProfileRefV1(profile_id="simulation-a", version=1),
        ProfileRefV1(profile_id="simulation-b", version=1),
    )

    outcome = _handler(
        store,
        sim_config_resolver=lambda _binding: ReviewSimConfig(
            n_agents=1,
            n_ticks=1,
            max_work_units=per_profile_work,
        ),
    )(_context(store, _payload(checker_profiles=(), simulation_profiles=profiles), seed=7))

    assert outcome.summary.outcome_code == "patch_validation_passed"


@pytest.mark.parametrize(
    "mutation",
    ("source", "snapshot", "producer"),
)
def test_patch_checker_rejects_finding_outside_execution_authority(mutation: str) -> None:
    class ForgedChecker:
        id = "graph"

        def check(self, snapshot, nav=None):
            del nav
            finding = Finding(
                id="checker:forged",
                source="checker",
                producer_id="graph",
                producer_run_id="checker-runner",
                oracle_type="deterministic",
                defect_class="dangling_reference",
                severity="major",
                snapshot_id=snapshot.snapshot_id,
                status="confirmed",
                message="forged checker output",
            )
            updates = {
                "source": {"source": "llm"},
                "snapshot": {"snapshot_id": "snapshot:other"},
                "producer": {"producer_id": "asp"},
            }
            return [finding.model_copy(update=updates[mutation])]

    store = _store()
    with pytest.raises(IntegrityViolation, match="execution authority|exact target"):
        _handler(
            store,
            checker_resolver=lambda _binding, _constraints: ForgedChecker(),
        )(_context(store, _payload()))


def test_llm_constraint_remains_unproven_without_entering_deterministic_checker_wire() -> None:
    store = _store(snapshot=snapshot_bytes([], []))
    constraint = Constraint(
        id="C_llm",
        kind="narrative",
        oracle="mixed",
        predicates=(Predicate(expr="semantic_consistency(story)", oracle="llm-assisted"),),
        **{"assert": "continuity_consistent"},
        severity="major",
    )
    store.register(
        CONSTRAINT_ARTIFACT_ID,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    checker_profile = ProfileRefV1(profile_id="builtin.checker", version=1)
    registry = build_builtin_registry()
    catalog = next(
        catalog
        for catalog in registry.list_execution_profile_catalogs()
        if any(definition.profile == checker_profile for definition in catalog.definitions)
    )
    checker_binding = registry.resolve_execution_profile(
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
        field_path="/params/checker_profiles/0",
        profile=checker_profile,
        expected_profile_kind="checker",
    )
    validation_binding = resolved_binding(
        "/params/validation_policy",
        profile_id="validation",
        version=1,
        kind="validation",
    ).model_copy(
        update={
            "catalog_version": catalog.catalog_version,
            "catalog_digest": catalog.catalog_digest,
        }
    )
    context = _context(
        store,
        _payload(
            constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
            checker_profiles=(checker_profile,),
        ),
        constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
        resolved_profiles_override=(validation_binding, checker_binding),
    )
    context = replace(
        context,
        payload=context.payload.model_copy(
            update={
                "execution_profile_catalog_version": catalog.catalog_version,
                "execution_profile_catalog_digest": catalog.catalog_digest,
            }
        ),
    )
    outcome = _handler(
        store,
        checker_resolver=worker_components._build_patch_checker_resolver(registry),
    )(context)

    companion = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.meta.get("requirement_id") == "checker:builtin.checker@1"
    )
    sealed = decode_and_validate_artifact_payload(
        payload_schema_id=companion.payload_schema_id,
        blob=store.read_prepared(companion.object_ref),
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert outcome.findings == ()
    assert sealed["status"] == "unproven"
    assert sealed["checker_execution_bindings"] == [{"wrapper_id": "graph", "native_id": "graph"}]
    assert sealed["constraint_snapshot_artifact_id"] == CONSTRAINT_ARTIFACT_ID
    assert sealed["detail"]["findings"][0]["producer_id"] == "llm-routed"


def test_historical_expected_finding_without_matching_oracle_is_unproven_not_failed() -> None:
    store = _store()
    historical = _finding(status="confirmed", snapshot_id="snapshot:historical")
    revision = _finding_revision(historical)
    store.register(
        FINDING_EVIDENCE_ID,
        {
            "payload_schema_version": "checker-report@1",
            "snapshot_id": historical.snapshot_id,
            "checker_ids": ["graph"],
            "defect_classes": [historical.defect_class],
            "constraint_application": [],
            "findings": [historical.model_dump(mode="json")],
        },
    )
    binding = _finding_binding(revision)
    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(revision),
    )(
        _context(
            store,
            _payload(checker_profiles=(), expected_findings=(binding,)),
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item for item in evidence.requirements if item.requirement_id == "expected-finding:f1@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


def test_historical_confirmed_finding_passes_only_after_matching_clean_oracle_rerun() -> None:
    store = _store()
    historical = _finding(status="confirmed", snapshot_id="snapshot:historical")
    revision = _finding_revision(historical)
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="checker_run",
        payload_schema_id="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "checker_profile": _CHECKER.model_dump(mode="json"),
            "constraint_snapshot_binding_status": "not_applicable",
            "snapshot_id": historical.snapshot_id,
            "checker_ids": ["graph"],
            "defect_classes": [historical.defect_class],
            "constraint_application": [],
            "findings": [historical.model_dump(mode="json")],
        },
    )
    binding = _finding_binding(revision, evidence_artifact_id=evidence_artifact_id)

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(_context(store, _payload(expected_findings=(binding,))))

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item for item in evidence.requirements if item.requirement_id == "expected-finding:f1@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_passed"
    assert requirement.status == "passed"

    assert evidence.finding_bindings == (binding,)
    assert evidence_artifact_id in evidence.supporting_artifact_ids
    assert evidence_artifact_id in outcome.artifacts[outcome.primary_index].lineage


def test_maximum_expected_finding_closure_reads_and_indexes_shared_evidence_once() -> None:
    class CountingStore(FakeArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.read_counts: dict[str, int] = {}
            self.envelope_counts: dict[str, int] = {}

        def read_bytes(self, artifact_id: str) -> bytes:
            self.read_counts[artifact_id] = self.read_counts.get(artifact_id, 0) + 1
            return super().read_bytes(artifact_id)

        def load_artifact(self, artifact_id: str):
            self.envelope_counts[artifact_id] = self.envelope_counts.get(artifact_id, 0) + 1
            return super().load_artifact(artifact_id)

    class BatchRecordingLoader(_ExactFindingRevisionLoader):
        def __init__(self, *revisions, **kwargs) -> None:
            super().__init__(*revisions, **kwargs)
            self.batch_sizes: list[int] = []

        def load_many_exact(self, *, bindings):
            self.batch_sizes.append(len(bindings))
            return super().load_many_exact(bindings=bindings)

    store = _store(store=CountingStore())
    findings = tuple(
        _finding(
            status="confirmed",
            finding_id=f"historical:{index}",
            snapshot_id="snapshot:historical",
        ).model_copy(
            update={
                "entities": [f"entity:{index}"],
                "message": f"historical defect {index}",
            }
        )
        for index in range(1024)
    )
    revisions = tuple(
        _finding_revision(finding, finding_id=f"finding-series:{index}")
        for index, finding in enumerate(findings)
    )
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="checker_run",
        payload_schema_id="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "checker_profile": _CHECKER.model_dump(mode="json"),
            "constraint_snapshot_binding_status": "not_applicable",
            "snapshot_id": "snapshot:historical",
            "checker_ids": ["graph"],
            "defect_classes": ["dangling_reference"],
            "constraint_application": [],
            "findings": [finding.model_dump(mode="json") for finding in findings],
        },
    )
    bindings = tuple(
        _finding_binding(revision, evidence_artifact_id=evidence_artifact_id)
        for revision in revisions
    )
    loader = BatchRecordingLoader(
        *revisions,
        evidence_artifact_id=evidence_artifact_id,
    )

    outcome = _handler(store, finding_revision_loader=loader)(
        _context(store, _payload(expected_findings=bindings))
    )

    assert outcome.summary.outcome_code == "patch_validation_passed"
    assert loader.batch_sizes == [1024]
    assert store.read_counts[evidence_artifact_id] == 1
    assert store.envelope_counts[evidence_artifact_id] == 1


def test_historical_patch_validation_checker_companion_reexecutes_exact_oracle() -> None:
    store = _store()
    historical = _finding(status="confirmed", snapshot_id="snapshot:historical")
    revision = _finding_revision(historical)
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="regression_evidence",
        payload_schema_id="regression-evidence@1",
        payload={
            "payload_schema_version": "regression-evidence@1",
            "requirement_id": "checker:checker@1",
            "dimension": "checker",
            "lineage_suite_artifact_ids": [],
            "checker_profile": _CHECKER.model_dump(mode="json"),
            "checker_execution_bindings": [
                {
                    "wrapper_id": "graph",
                    "native_id": "graph",
                    "constraint_id": None,
                }
            ],
            "constraint_snapshot_binding_status": "not_applicable",
            "snapshot_id": historical.snapshot_id,
            "status": "failed",
            "findings": [historical.model_dump(mode="json")],
        },
    )
    binding = _finding_binding(revision, evidence_artifact_id=evidence_artifact_id)

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(_context(store, _payload(expected_findings=(binding,))))

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:f1@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_passed"
    assert requirement.status == "passed"


def test_historical_suite_companion_reexecutes_by_envelope_schema_not_finding_source() -> None:
    store = _store()
    root_seed = 7
    execution_seed = derive_validation_subseed(
        root_seed=root_seed,
        run_kind=PATCH_VALIDATE_KIND,
        profile=_VALIDATION,
        case_id=REGRESSION_SUITE_ID,
        replication_index=0,
    )
    execution_binding = regression_suite_execution_coverage_binding(
        suite_artifact_id=REGRESSION_SUITE_ID,
        validation_profile=_VALIDATION,
        constraint_snapshot_artifact_id=None,
        env_contract_version="suite-env@1",
        root_seed=root_seed,
        run_kind=PATCH_VALIDATE_KIND,
        execution_seed=execution_seed,
    )
    historical = _playtest_finding(
        status="confirmed",
        snapshot_id="snapshot:historical",
    )
    revision = _finding_revision(historical)
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="regression_evidence",
        payload_schema_id="regression-evidence@1",
        payload={
            "payload_schema_version": "regression-evidence@1",
            "requirement_id": f"regression:{REGRESSION_SUITE_ID}",
            "suite_artifact_id": REGRESSION_SUITE_ID,
            "lineage_suite_artifact_ids": [REGRESSION_SUITE_ID],
            "snapshot_id": historical.snapshot_id,
            "status": "failed",
            "findings": [historical.model_dump(mode="json")],
            "root_seed": root_seed,
            "run_kind": PATCH_VALIDATE_KIND.model_dump(mode="json"),
            "profile_id": _VALIDATION.profile_id,
            "profile_version": _VALIDATION.version,
            "case_id": REGRESSION_SUITE_ID,
            "replication_index": 0,
            "seed": execution_seed,
            "seed_derivation_version": VALIDATION_SEED_DERIVATION_VERSION,
            "execution_coverage_binding": execution_binding,
        },
        version_tuple=VersionTuple(
            env_contract_version="suite-env@1",
            tool_version="regression@1",
        ),
        lineage=(REGRESSION_SUITE_ID,),
    )
    binding = _finding_binding(revision, evidence_artifact_id=evidence_artifact_id)

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
        regression_runner=_RecordingRegressionRunner(),
    )(
        _context(
            store,
            _payload(
                checker_profiles=(),
                expected_findings=(binding,),
                regression_suite_artifact_ids=(REGRESSION_SUITE_ID,),
            ),
            seed=root_seed,
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:playtest-incomplete:agent_stopped@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_passed"
    assert requirement.status == "passed"

    unproven_finding = historical.model_copy(update={"status": "unproven"})
    unproven_revision = _finding_revision(unproven_finding)
    unproven_payload = json.loads(store.read_bytes(evidence_artifact_id))
    unproven_payload.update(
        {
            "status": "unproven",
            "reason_code": "suite_oracle_unavailable",
            "findings": [unproven_finding.model_dump(mode="json")],
        }
    )
    unproven_artifact_id = _register_historical_evidence(
        store,
        kind="regression_evidence",
        payload_schema_id="regression-evidence@1",
        payload=unproven_payload,
        version_tuple=VersionTuple(
            env_contract_version="suite-env@1",
            tool_version="regression@1",
        ),
        lineage=(REGRESSION_SUITE_ID,),
    )
    unproven_binding = _finding_binding(
        unproven_revision,
        evidence_artifact_id=unproven_artifact_id,
    )
    unproven_outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            unproven_revision,
            evidence_artifact_id=unproven_artifact_id,
        ),
        regression_runner=_RecordingRegressionRunner(),
    )(
        _context(
            store,
            _payload(
                checker_profiles=(),
                expected_findings=(unproven_binding,),
                regression_suite_artifact_ids=(REGRESSION_SUITE_ID,),
            ),
            seed=root_seed,
        )
    )
    unproven_requirement = next(
        item
        for item in _read_evidence_set(store, unproven_outcome).requirements
        if item.requirement_id == "expected-finding:playtest-incomplete:agent_stopped@1"
    )
    assert unproven_outcome.summary.outcome_code == "patch_validation_unproven"
    assert unproven_requirement.status == "unproven"


@pytest.mark.parametrize(
    "forgery",
    ("severity", "status", "evidence", "duplicate"),
)
def test_historical_checker_requires_one_exact_identity_free_finding_payload(
    forgery: str,
) -> None:
    store = _store()
    historical = _finding(status="confirmed", snapshot_id="snapshot:historical")
    revision = _finding_revision(historical)
    update = {
        "severity": {"severity": "minor"},
        "status": {"status": "fixed"},
        "evidence": {"evidence": {"forged": True}},
    }
    embedded = (
        [historical, historical]
        if forgery == "duplicate"
        else [historical.model_copy(update=update[forgery])]
    )
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="checker_run",
        payload_schema_id="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "checker_profile": _CHECKER.model_dump(mode="json"),
            "constraint_snapshot_binding_status": "not_applicable",
            "snapshot_id": historical.snapshot_id,
            "checker_ids": ["graph"],
            "defect_classes": [historical.defect_class],
            "constraint_application": [],
            "findings": [item.model_dump(mode="json") for item in embedded],
        },
    )

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(
        _context(
            store,
            _payload(
                expected_findings=(
                    _finding_binding(
                        revision,
                        evidence_artifact_id=evidence_artifact_id,
                    ),
                )
            ),
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:f1@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


@pytest.mark.parametrize(
    "historical_profile",
    (
        None,
        ProfileRefV1(profile_id="checker-other", version=1),
    ),
)
def test_historical_checker_requires_exact_profile_authority(
    historical_profile: ProfileRefV1 | None,
) -> None:
    store = _store()
    historical = _finding(status="confirmed", snapshot_id="snapshot:historical").model_copy(
        update={"evidence": {"checker_profile": _CHECKER.model_dump(mode="json")}}
    )
    revision = _finding_revision(historical)
    report = {
        "payload_schema_version": "checker-report@1",
        "snapshot_id": historical.snapshot_id,
        "checker_ids": ["graph"],
        "defect_classes": [historical.defect_class],
        "constraint_application": [],
        "findings": [historical.model_dump(mode="json")],
    }
    if historical_profile is not None:
        report["checker_profile"] = historical_profile.model_dump(mode="json")
        report["constraint_snapshot_binding_status"] = "not_applicable"
    store.register(FINDING_EVIDENCE_ID, report)

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(revision),
    )(_context(store, _payload(expected_findings=(_finding_binding(revision),))))

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item for item in evidence.requirements if item.requirement_id == "expected-finding:f1@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


@pytest.mark.parametrize(
    "historical_profile",
    (_SIM, ProfileRefV1(profile_id="sim-other", version=1)),
)
def test_review_simulation_result_never_covers_patch_single_population_mode(
    historical_profile: ProfileRefV1,
) -> None:
    store = _store()
    historical = _simulation_finding(
        status="confirmed",
        snapshot_id="snapshot:historical",
    )
    revision = _finding_revision(historical)
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="simulation_run",
        payload_schema_id="simulation-result@1",
        payload={
            "payload_schema_version": "simulation-result@1",
            "profile": historical_profile.model_dump(mode="json"),
            "snapshot_id": historical.snapshot_id,
            "seed": 999,
            "replication_count": 6,
            "horizon_steps": 12,
            "invariants": [],
            "sensitivity": {
                "execution_binding": {
                    "simulation_profile": historical_profile.model_dump(mode="json"),
                    "constraint_ids": [],
                    "constraint_application": {"status": "not_applicable"},
                }
            },
            "findings": [historical.model_dump(mode="json")],
        },
    )
    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(
        _context(
            store,
            _payload(
                checker_profiles=(),
                simulation_profiles=(_SIM,),
                expected_findings=(
                    _finding_binding(
                        revision,
                        evidence_artifact_id=evidence_artifact_id,
                    ),
                ),
            ),
            seed=7,
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == "expected-finding:sim:economy-collapse@1"
    )
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"
    assert outcome.summary.outcome_code == "patch_validation_unproven"


@pytest.mark.parametrize(
    ("historical_root_seed", "expected_status"),
    ((7, "passed"), (8, "unproven")),
)
def test_prior_patch_simulation_requires_exact_mode_and_subseed_closure(
    historical_root_seed: int,
    expected_status: str,
) -> None:
    store = _store()
    historical = _simulation_finding(
        status="confirmed",
        snapshot_id="snapshot:historical",
    )
    revision = _finding_revision(historical)
    execution_binding = _patch_simulation_execution_binding(
        root_seed=historical_root_seed,
    )
    seed_binding = execution_binding["seed_binding"]
    assert isinstance(seed_binding, dict)
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="regression_evidence",
        payload_schema_id="regression-evidence@1",
        payload={
            "payload_schema_version": "regression-evidence@1",
            "requirement_id": "simulation:sim@1",
            "dimension": "simulation",
            "lineage_suite_artifact_ids": [],
            "simulation_execution_binding": execution_binding,
            "snapshot_id": historical.snapshot_id,
            "status": "failed",
            "findings": [historical.model_dump(mode="json")],
            **seed_binding,
        },
        version_tuple=VersionTuple(
            ir_snapshot_id=historical.snapshot_id,
            tool_version="patch-validation@1",
            seed=historical_root_seed,
        ),
    )
    binding = _finding_binding(
        revision,
        evidence_artifact_id=evidence_artifact_id,
    )

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(
        _context(
            store,
            _payload(
                checker_profiles=(),
                simulation_profiles=(_SIM,),
                expected_findings=(binding,),
            ),
            seed=7,
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:sim:economy-collapse@1"
    )
    assert requirement.status == expected_status
    assert outcome.summary.outcome_code == f"patch_validation_{expected_status}"


def test_standalone_replication_mode_cannot_cover_patch_with_same_counts() -> None:
    store = _store()
    historical = _simulation_finding(
        status="confirmed",
        snapshot_id="snapshot:historical",
    )
    revision = _finding_revision(historical)
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="simulation_run",
        payload_schema_id="simulation-result@1",
        payload={
            "payload_schema_version": "simulation-result@1",
            "profile": _SIM.model_dump(mode="json"),
            "snapshot_id": historical.snapshot_id,
            "seed": 7,
            "replication_count": 6,
            "horizon_steps": 12,
            "invariants": [],
            "sensitivity": {
                "execution_binding": {
                    "simulation_profile": _SIM.model_dump(mode="json"),
                    "workload_profile": {"profile_id": "workload", "version": 1},
                    "constraint_ids": [],
                    "constraint_application": {"status": "not_applicable"},
                    "scenario_application": {"status": "not_applicable"},
                }
            },
            "findings": [historical.model_dump(mode="json")],
        },
    )
    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(
        _context(
            store,
            _payload(
                checker_profiles=(),
                simulation_profiles=(_SIM,),
                expected_findings=(
                    _finding_binding(
                        revision,
                        evidence_artifact_id=evidence_artifact_id,
                    ),
                ),
            ),
            seed=7,
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:sim:economy-collapse@1"
    )
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


@pytest.mark.parametrize(
    "forgery",
    ("severity", "status", "evidence", "duplicate"),
)
def test_historical_simulation_requires_one_exact_identity_free_finding_payload(
    forgery: str,
) -> None:
    store = _store()
    historical = _simulation_finding(
        status="confirmed",
        snapshot_id="snapshot:historical",
    )
    revision = _finding_revision(historical)
    update = {
        "severity": {"severity": "minor"},
        "status": {"status": "fixed"},
        "evidence": {"evidence": {"forged": True}},
    }
    embedded = (
        [historical, historical]
        if forgery == "duplicate"
        else [historical.model_copy(update=update[forgery])]
    )
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="simulation_run",
        payload_schema_id="simulation-result@1",
        payload={
            "payload_schema_version": "simulation-result@1",
            "profile": _SIM.model_dump(mode="json"),
            "snapshot_id": historical.snapshot_id,
            "seed": 999,
            "replication_count": 6,
            "horizon_steps": 12,
            "invariants": [],
            "sensitivity": {
                "execution_binding": {
                    "simulation_profile": _SIM.model_dump(mode="json"),
                    "constraint_ids": [],
                    "constraint_application": {"status": "not_applicable"},
                }
            },
            "findings": [item.model_dump(mode="json") for item in embedded],
        },
    )

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(
        _context(
            store,
            _payload(
                checker_profiles=(),
                simulation_profiles=(_SIM,),
                expected_findings=(
                    _finding_binding(
                        revision,
                        evidence_artifact_id=evidence_artifact_id,
                    ),
                ),
            ),
            seed=7,
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:sim:economy-collapse@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


@pytest.mark.parametrize(
    ("replication_count", "horizon_steps", "historical_constraint_artifact_id"),
    (
        (7, 12, None),
        (6, 13, None),
        (6, 12, "artifact:historical-constraints"),
    ),
)
def test_historical_simulation_coverage_binds_config_and_constraint_snapshot(
    replication_count: int,
    horizon_steps: int,
    historical_constraint_artifact_id: str | None,
) -> None:
    store = _store()
    historical = _simulation_finding(
        status="confirmed",
        snapshot_id="snapshot:historical",
    )
    revision = _finding_revision(historical)
    constraint_bound = historical_constraint_artifact_id is not None
    fresh_constraint_artifact_id = CONSTRAINT_ARTIFACT_ID if constraint_bound else None
    store.register(
        FINDING_EVIDENCE_ID,
        {
            "payload_schema_version": "simulation-result@1",
            "profile": _SIM.model_dump(mode="json"),
            "snapshot_id": historical.snapshot_id,
            "seed": 999,
            "replication_count": replication_count,
            "horizon_steps": horizon_steps,
            "invariants": [],
            "sensitivity": {
                "execution_binding": {
                    "simulation_profile": _SIM.model_dump(mode="json"),
                    **(
                        {"constraint_snapshot_artifact_id": (historical_constraint_artifact_id)}
                        if constraint_bound
                        else {}
                    ),
                    "constraint_ids": (["C_sim"] if constraint_bound else []),
                    "constraint_application": (
                        {
                            "status": "unproven",
                            "reason_code": "constraint_profile_not_executable",
                        }
                        if constraint_bound
                        else {"status": "not_applicable"}
                    ),
                }
            },
            "findings": [historical.model_dump(mode="json")],
        },
    )

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(revision),
    )(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=fresh_constraint_artifact_id,
                checker_profiles=(),
                simulation_profiles=(_SIM,),
                expected_findings=(_finding_binding(revision),),
            ),
            seed=7,
            constraint_snapshot_id=(
                CONSTRAINT_SNAPSHOT_ID if fresh_constraint_artifact_id else None
            ),
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:sim:economy-collapse@1"
    )
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


def test_historical_simulation_with_applied_scenario_cannot_cover_plain_patch_simulation() -> None:
    store = _store()
    historical = _simulation_finding(
        status="confirmed",
        snapshot_id="snapshot:historical",
    )
    revision = _finding_revision(historical)
    store.register(
        FINDING_EVIDENCE_ID,
        {
            "payload_schema_version": "simulation-result@1",
            "profile": _SIM.model_dump(mode="json"),
            "snapshot_id": historical.snapshot_id,
            "seed": 999,
            "replication_count": 6,
            "horizon_steps": 12,
            "invariants": [],
            "sensitivity": {
                "execution_binding": {
                    "simulation_profile": _SIM.model_dump(mode="json"),
                    "workload_profile": {"profile_id": "workload", "version": 1},
                    "scenario_artifact_id": "artifact:scenario",
                    "constraint_ids": [],
                    "scenario_id": "scenario:historical",
                    "constraint_application": {"status": "not_applicable"},
                    "scenario_application": {
                        "status": "unproven",
                        "reason_code": "scenario_reset_not_executable",
                    },
                }
            },
            "findings": [historical.model_dump(mode="json")],
        },
    )

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(revision),
    )(
        _context(
            store,
            _payload(
                checker_profiles=(),
                simulation_profiles=(_SIM,),
                expected_findings=(_finding_binding(revision),),
            ),
            seed=7,
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:sim:economy-collapse@1"
    )
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


def test_historical_expected_finding_fails_when_matching_oracle_reproduces_defect() -> None:
    store = _store()
    historical = _finding(status="confirmed", snapshot_id="snapshot:historical")
    revision = _finding_revision(historical)
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="checker_run",
        payload_schema_id="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "checker_profile": _CHECKER.model_dump(mode="json"),
            "constraint_snapshot_binding_status": "not_applicable",
            "snapshot_id": historical.snapshot_id,
            "checker_ids": ["graph"],
            "defect_classes": [historical.defect_class],
            "constraint_application": [],
            "findings": [historical.model_dump(mode="json")],
        },
    )
    binding = _finding_binding(revision, evidence_artifact_id=evidence_artifact_id)
    preview = load_snapshot(store, PREVIEW_ID)

    class _ReproducingChecker:
        id = "graph"

        def check(self, snapshot, nav=None):
            return [historical.model_copy(update={"snapshot_id": preview.snapshot_id})]

    outcome = _handler(
        store,
        checker_resolver=lambda profile, constraints: _ReproducingChecker(),
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(_context(store, _payload(expected_findings=(binding,))))

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item for item in evidence.requirements if item.requirement_id == "expected-finding:f1@1"
    )
    artifact = next(
        item
        for item in outcome.artifacts
        if item.meta.get("requirement_id") == "expected-finding:f1@1"
    )
    sealed = decode_and_validate_artifact_payload(
        payload_schema_id=artifact.payload_schema_id,
        blob=store.read_prepared(artifact.object_ref),
    )
    assert outcome.summary.outcome_code == "patch_validation_failed"
    assert requirement.status == "failed"
    assert requirement.reason_code is None
    assert sealed["reason_code"] == "expected_finding_reproduced"


@pytest.mark.parametrize(
    ("reproduce", "expected_status"),
    ((False, "passed"), (True, "failed")),
)
def test_compiled_checker_expected_finding_uses_exact_constraint_scoped_native_binding(
    reproduce: bool,
    expected_status: str,
) -> None:
    store = _store()
    constraint = Constraint(
        id="C_compiled",
        kind="structural",
        oracle="deterministic",
        **{"assert": "acyclic(quest_steps)"},
        severity="major",
    )
    store.register(
        CONSTRAINT_ARTIFACT_ID,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    historical = _compiled_finding(
        status="confirmed",
        snapshot_id="snapshot:historical",
    )
    revision = _finding_revision(historical)
    evidence_artifact_id = _register_historical_evidence(
        store,
        kind="checker_run",
        payload_schema_id="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "checker_profile": _CHECKER.model_dump(mode="json"),
            "constraint_snapshot_binding_status": "bound",
            "constraint_snapshot_artifact_id": CONSTRAINT_ARTIFACT_ID,
            "snapshot_id": historical.snapshot_id,
            "checker_ids": ["graph"],
            "defect_classes": [historical.defect_class],
            "constraint_application": [
                {
                    "constraint_id": "C_compiled",
                    "checker_id": "asp",
                    "status": "executed",
                }
            ],
            "findings": [historical.model_dump(mode="json")],
        },
    )
    preview = load_snapshot(store, PREVIEW_ID)

    class _CompiledCheckerGroup:
        id = "profile:checker@1"
        executed_checker_bindings = (
            CheckerExecutionBinding(
                wrapper_id="compiled:asp:C_compiled",
                native_id="asp",
                constraint_id="C_compiled",
            ),
        )

        def check(self, snapshot, nav=None):
            del snapshot, nav
            return (
                [historical.model_copy(update={"snapshot_id": preview.snapshot_id})]
                if reproduce
                else []
            )

    outcome = _handler(
        store,
        checker_resolver=lambda profile, constraints: _CompiledCheckerGroup(),
        finding_revision_loader=_ExactFindingRevisionLoader(
            revision,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                expected_findings=(
                    _finding_binding(
                        revision,
                        evidence_artifact_id=evidence_artifact_id,
                    ),
                ),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == "expected-finding:compiled:finding@1"
    )
    assert requirement.status == expected_status
    assert outcome.summary.outcome_code == f"patch_validation_{expected_status}"


@pytest.mark.parametrize(
    (
        "historical_profile",
        "historical_constraint_artifact_id",
        "historical_constraint_id",
        "direct",
        "historical_application_native",
    ),
    (
        (
            ProfileRefV1(profile_id="checker-other", version=1),
            CONSTRAINT_ARTIFACT_ID,
            "C_compiled",
            False,
            "asp",
        ),
        (_CHECKER, "artifact:other-constraints", "C_compiled", False, "asp"),
        (_CHECKER, "artifact:other-constraints", "C_other", False, "asp"),
        (_CHECKER, CONSTRAINT_ARTIFACT_ID, "C_compiled", True, "asp"),
        (_CHECKER, CONSTRAINT_ARTIFACT_ID, "C_compiled", False, "graph"),
    ),
)
def test_compiled_checker_coverage_does_not_cross_profile_snapshot_constraint_or_direct_context(
    historical_profile: ProfileRefV1,
    historical_constraint_artifact_id: str,
    historical_constraint_id: str,
    direct: bool,
    historical_application_native: str,
) -> None:
    store = _store()
    exact_constraint = Constraint(
        id="C_compiled",
        kind="structural",
        oracle="deterministic",
        **{"assert": "acyclic(quest_steps)"},
        severity="major",
    )
    store.register(
        CONSTRAINT_ARTIFACT_ID,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [exact_constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    historical = _compiled_finding(
        status="confirmed",
        snapshot_id="snapshot:historical",
        constraint_id=historical_constraint_id,
    )
    revision = _finding_revision(historical)
    store.register(
        FINDING_EVIDENCE_ID,
        {
            "payload_schema_version": "checker-report@1",
            "checker_profile": historical_profile.model_dump(mode="json"),
            "constraint_snapshot_binding_status": "bound",
            "constraint_snapshot_artifact_id": historical_constraint_artifact_id,
            "snapshot_id": historical.snapshot_id,
            "checker_ids": ["graph"],
            "defect_classes": [historical.defect_class],
            "constraint_application": [
                {
                    "constraint_id": historical_constraint_id,
                    "checker_id": historical_application_native,
                    "status": "executed",
                }
            ],
            "findings": [historical.model_dump(mode="json")],
        },
    )

    class _FreshCheckerGroup:
        id = "profile:checker@1"
        executed_checker_bindings = (
            CheckerExecutionBinding(
                wrapper_id=("asp" if direct else "compiled:asp:C_compiled"),
                native_id="asp",
                constraint_id=(None if direct else "C_compiled"),
            ),
        )

        def check(self, snapshot, nav=None):
            del snapshot, nav
            return []

    outcome = _handler(
        store,
        checker_resolver=lambda profile, constraints: _FreshCheckerGroup(),
        finding_revision_loader=_ExactFindingRevisionLoader(revision),
    )(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                expected_findings=(_finding_binding(revision),),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:compiled:finding@1"
    )
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


def test_direct_checker_coverage_does_not_cross_into_constraint_scoped_execution() -> None:
    store = _store()
    constraint = Constraint(
        id="C_compiled",
        kind="structural",
        oracle="deterministic",
        **{"assert": "relations_resolve"},
        severity="major",
    )
    store.register(
        CONSTRAINT_ARTIFACT_ID,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    historical = _finding(status="confirmed", snapshot_id="snapshot:historical")
    revision = _finding_revision(historical)
    store.register(
        FINDING_EVIDENCE_ID,
        {
            "payload_schema_version": "checker-report@1",
            "checker_profile": _CHECKER.model_dump(mode="json"),
            "constraint_snapshot_binding_status": "bound",
            "constraint_snapshot_artifact_id": CONSTRAINT_ARTIFACT_ID,
            "snapshot_id": historical.snapshot_id,
            "checker_ids": ["graph"],
            "defect_classes": [historical.defect_class],
            "constraint_application": [
                {
                    "constraint_id": "C_compiled",
                    "checker_id": "graph",
                    "status": "executed",
                }
            ],
            "findings": [historical.model_dump(mode="json")],
        },
    )

    class _ScopedGraphChecker:
        id = "profile:checker@1"
        executed_checker_bindings = (
            CheckerExecutionBinding(
                wrapper_id="compiled:graph:C_compiled",
                native_id="graph",
                constraint_id="C_compiled",
            ),
        )

        def check(self, snapshot, nav=None):
            del snapshot, nav
            return []

    outcome = _handler(
        store,
        checker_resolver=lambda profile, constraints: _ScopedGraphChecker(),
        finding_revision_loader=_ExactFindingRevisionLoader(revision),
    )(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                expected_findings=(_finding_binding(revision),),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == "expected-finding:f1@1"
    )
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


def test_historical_playtest_finding_passes_when_exact_episode_rerun_does_not_reproduce() -> None:
    store = _store()
    historical_trace = _playtest_trace(completed=False)
    evidence_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=historical_trace,
        ir_snapshot_id="snapshot:historical",
    )
    preview = load_snapshot(store, PREVIEW_ID)
    fresh_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=_playtest_trace(completed=True),
        ir_snapshot_id=preview.snapshot_id,
        cassette_id=f"sha256:{'2' * 64}",
    )
    historical = _finding_revision(
        _playtest_finding(status="confirmed", snapshot_id="snapshot:historical"),
        finding_id="finding-series:playtest:episode-1:incomplete",
    )
    expected = _finding_binding(historical, evidence_artifact_id=evidence_artifact_id)
    loader = _ExactFindingRevisionLoader(
        historical,
        linked_evidence_by_finding={
            (historical.finding_id, historical.revision): evidence_artifact_id,
        },
    )

    outcome = _handler(store, finding_revision_loader=loader)(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(CONFIG_ID,),
                checker_profiles=(),
                expected_findings=(expected,),
                playtest_trace_artifact_ids=(fresh_artifact_id,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == "expected-finding:finding-series:playtest:episode-1:incomplete@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_passed"
    assert requirement.status == "passed"


def test_historical_playtest_binding_to_non_trace_is_unproven() -> None:
    store = _store()
    store.register(PLAYTEST_TRACE_ID, _playtest_trace(completed=True).model_dump(mode="json"))
    historical = _finding_revision(
        _playtest_finding(status="confirmed", snapshot_id="snapshot:historical"),
        finding_id="finding-series:playtest:non-trace",
    )
    expected = _finding_binding(historical)
    loader = _ExactFindingRevisionLoader(historical)

    outcome = _handler(store, finding_revision_loader=loader)(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(CONFIG_ID,),
                checker_profiles=(),
                expected_findings=(expected,),
                playtest_trace_artifact_ids=(PLAYTEST_TRACE_ID,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == "expected-finding:finding-series:playtest:non-trace@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


@pytest.mark.parametrize("changed_binding", ("seed", "profile", "oracle"))
def test_historical_playtest_requires_exact_execution_binding(
    changed_binding: str,
) -> None:
    store = _store()
    historical_trace = _playtest_trace(completed=False)
    if changed_binding == "seed":
        fresh_trace = _playtest_trace(completed=True, root_seed=12)
    elif changed_binding == "profile":
        fresh_trace = _playtest_trace(
            completed=True,
            environment_profile=ProfileRefV1(profile_id="environment-other", version=1),
        )
    else:
        fresh_trace = _playtest_trace(
            completed=True,
            completion_oracle_id="another-completion-oracle",
        )
    store.register(FINDING_EVIDENCE_ID, historical_trace.model_dump(mode="json"))
    store.register(PLAYTEST_TRACE_ID, fresh_trace.model_dump(mode="json"))
    historical = _finding_revision(
        _playtest_finding(status="confirmed", snapshot_id="snapshot:historical"),
        finding_id=f"finding-series:playtest:changed-{changed_binding}",
    )

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(historical),
    )(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(CONFIG_ID,),
                checker_profiles=(),
                expected_findings=(_finding_binding(historical),),
                playtest_trace_artifact_ids=(PLAYTEST_TRACE_ID,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id
        == f"expected-finding:finding-series:playtest:changed-{changed_binding}@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


def test_historical_playtest_reverification_allows_rederived_candidate_artifact_ids() -> None:
    store = _store()
    old_config_id = "artifact:candidate-config:old"
    new_config_id = "artifact:candidate-config:new"
    old_suite_id = "artifact:task-suite:old"
    new_suite_id = "artifact:task-suite:new"
    old_scenario_id = "artifact:scenario:old"
    new_scenario_id = "artifact:scenario:new"
    store.register(old_config_id, {"config": "old candidate"})
    store.register(new_config_id, {"config": "new candidate"})
    historical_trace = _playtest_trace(
        completed=False,
        config_artifact_id=old_config_id,
        task_suite_artifact_id=old_suite_id,
        scenario_spec_artifact_id=old_scenario_id,
    )
    evidence_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=historical_trace,
        ir_snapshot_id="snapshot:historical",
        cassette_id=f"sha256:{'1' * 64}",
    )
    fresh_trace = _playtest_trace(
        completed=True,
        config_artifact_id=new_config_id,
        task_suite_artifact_id=new_suite_id,
        scenario_spec_artifact_id=new_scenario_id,
    )
    preview = load_snapshot(store, PREVIEW_ID)
    fresh_trace_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=fresh_trace,
        ir_snapshot_id=preview.snapshot_id,
        # Candidate-specific replay bytes produce a different cassette Artifact,
        # but the same replay execution variant remains comparable.
        cassette_id=f"sha256:{'2' * 64}",
    )
    historical = _finding_revision(
        _playtest_finding(
            status="confirmed",
            snapshot_id="snapshot:historical",
            scenario_spec_artifact_id=old_scenario_id,
        ),
        finding_id="finding-series:playtest:new-config",
    )

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            historical,
            evidence_artifact_id=evidence_artifact_id,
        ),
    )(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(new_config_id,),
                checker_profiles=(),
                expected_findings=(
                    _finding_binding(
                        historical,
                        evidence_artifact_id=evidence_artifact_id,
                    ),
                ),
                playtest_trace_artifact_ids=(fresh_trace_artifact_id,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == "expected-finding:finding-series:playtest:new-config@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_passed"
    assert requirement.status == "passed"


@pytest.mark.parametrize(
    "changed_authority",
    (
        "prompt",
        "model",
        "agent_graph",
        "execution_mode_live",
        "execution_mode_record",
        "routing_identity_kind",
        "producer_tool",
        "artifact_environment",
    ),
)
def test_historical_playtest_reverification_does_not_cross_execution_authority(
    changed_authority: str,
) -> None:
    store = _store()
    historical_trace = _playtest_trace(completed=False)
    historical_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=historical_trace,
        ir_snapshot_id="snapshot:historical",
    )
    fresh_trace = _playtest_trace(completed=True)
    fresh_kwargs: dict[str, object] = {}
    if changed_authority == "prompt":
        fresh_kwargs["prompt_version"] = "playtest-prompt@2"
    elif changed_authority == "model":
        fresh_kwargs["model_snapshot"] = "provider/playtest/model@2"
    elif changed_authority == "agent_graph":
        fresh_kwargs["agent_graph_version"] = "playtest-graph@2"
    elif changed_authority == "execution_mode_live":
        fresh_kwargs["execution_mode"] = "live"
    elif changed_authority == "execution_mode_record":
        fresh_kwargs["execution_mode"] = "record"
    elif changed_authority == "routing_identity_kind":
        fresh_kwargs["routing_decision_kind"] = "legacy_import"
    elif changed_authority == "producer_tool":
        fresh_kwargs["producer_tool_version"] = "playtest@2"
    else:
        fresh_kwargs["artifact_env_contract_version"] = "env@2"
    preview = load_snapshot(store, PREVIEW_ID)
    fresh_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=fresh_trace,
        ir_snapshot_id=preview.snapshot_id,
        **fresh_kwargs,
    )
    historical = _finding_revision(
        _playtest_finding(status="confirmed", snapshot_id="snapshot:historical"),
        finding_id=f"finding-series:playtest:authority:{changed_authority}",
    )
    binding = _finding_binding(
        historical,
        evidence_artifact_id=historical_artifact_id,
    )

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            historical,
            evidence_artifact_id=historical_artifact_id,
        ),
    )(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(CONFIG_ID,),
                checker_profiles=(),
                expected_findings=(binding,),
                playtest_trace_artifact_ids=(fresh_artifact_id,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id
        == f"expected-finding:finding-series:playtest:authority:{changed_authority}@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


@pytest.mark.parametrize(
    "forgery",
    (
        "missing_identity",
        "fresh_missing_identity",
        "identity_call_count",
        "missing_scenario_lineage",
    ),
)
def test_historical_playtest_reverification_requires_artifact_authority(
    forgery: str,
) -> None:
    store = _store()
    historical_trace = _playtest_trace(completed=False)
    historical_kwargs: dict[str, object] = {}
    fresh_kwargs: dict[str, object] = {}
    if forgery == "missing_identity":
        historical_kwargs["omit_execution_identity"] = True
    elif forgery == "fresh_missing_identity":
        fresh_kwargs["omit_execution_identity"] = True
    elif forgery == "identity_call_count":
        historical_kwargs["consumed_call_count"] = 2
    elif forgery == "missing_scenario_lineage":
        historical_kwargs["omit_lineage_ids"] = ("artifact:scenario",)
    historical_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=historical_trace,
        ir_snapshot_id="snapshot:historical",
        **historical_kwargs,
    )
    fresh_trace = _playtest_trace(completed=True)
    preview = load_snapshot(store, PREVIEW_ID)
    fresh_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=fresh_trace,
        ir_snapshot_id=preview.snapshot_id,
        **fresh_kwargs,
    )
    historical = _finding_revision(
        _playtest_finding(status="confirmed", snapshot_id="snapshot:historical"),
        finding_id=f"finding-series:playtest:forgery:{forgery}",
    )

    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(
            historical,
            evidence_artifact_id=historical_artifact_id,
        ),
    )(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(CONFIG_ID,),
                checker_profiles=(),
                expected_findings=(
                    _finding_binding(
                        historical,
                        evidence_artifact_id=historical_artifact_id,
                    ),
                ),
                playtest_trace_artifact_ids=(fresh_artifact_id,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    requirement = next(
        item
        for item in _read_evidence_set(store, outcome).requirements
        if item.requirement_id == f"expected-finding:finding-series:playtest:forgery:{forgery}@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.status == "unproven"
    assert requirement.reason_code == "expected_finding_oracle_not_reexecuted"


def test_historical_playtest_finding_fails_when_exact_episode_rerun_reproduces() -> None:
    store = _store()
    trace = _playtest_trace(completed=False)
    evidence_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=trace,
        ir_snapshot_id="snapshot:historical",
    )
    historical = _finding_revision(
        _playtest_finding(status="confirmed", snapshot_id="snapshot:historical"),
        finding_id="finding-series:playtest:episode-1:incomplete:old",
    )
    preview = load_snapshot(store, PREVIEW_ID)
    fresh_artifact_id = _register_authoritative_playtest_trace(
        store,
        trace=trace,
        ir_snapshot_id=preview.snapshot_id,
        cassette_id=f"sha256:{'2' * 64}",
    )
    reproduced = _finding_revision(
        _playtest_finding(status="confirmed", snapshot_id=preview.snapshot_id),
        finding_id="finding-series:playtest:episode-1:incomplete:new",
    )
    expected = _finding_binding(historical, evidence_artifact_id=evidence_artifact_id)
    target = _finding_binding(
        reproduced,
        evidence_artifact_id=fresh_artifact_id,
    )
    loader = _ExactFindingRevisionLoader(
        historical,
        reproduced,
        linked_evidence_by_finding={
            (historical.finding_id, historical.revision): evidence_artifact_id,
            (reproduced.finding_id, reproduced.revision): fresh_artifact_id,
        },
    )

    outcome = _handler(store, finding_revision_loader=loader)(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(CONFIG_ID,),
                checker_profiles=(),
                expected_findings=(expected,),
                findings=(target,),
                playtest_trace_artifact_ids=(fresh_artifact_id,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id
        == "expected-finding:finding-series:playtest:episode-1:incomplete:old@1"
    )
    assert outcome.summary.outcome_code == "patch_validation_failed"
    assert requirement.status == "failed"
    expected_artifact = next(
        item
        for item in outcome.artifacts
        if item.meta.get("requirement_id") == requirement.requirement_id
    )
    sealed = decode_and_validate_artifact_payload(
        payload_schema_id=expected_artifact.payload_schema_id,
        blob=store.read_prepared(expected_artifact.object_ref),
    )
    assert sealed["reason_code"] == "expected_finding_reproduced"


def test_selected_playtest_trace_does_not_republish_forged_deterministic_producer() -> None:
    store = _store()
    store.register(
        PLAYTEST_TRACE_ID,
        _playtest_trace(completed=True).model_dump(mode="json"),
    )
    preview = load_snapshot(store, PREVIEW_ID)
    forged = _finding_revision(
        _playtest_finding(status="confirmed", snapshot_id=preview.snapshot_id).model_copy(
            update={"producer_id": "playtest.forged"}
        ),
        finding_id="finding-series:playtest:forged",
    )
    binding = _finding_binding(forged, evidence_artifact_id=PLAYTEST_TRACE_ID)
    loader = _ExactFindingRevisionLoader(
        forged,
        linked_evidence_by_finding={
            (forged.finding_id, forged.revision): PLAYTEST_TRACE_ID,
        },
    )

    outcome = _handler(store, finding_revision_loader=loader)(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                candidate_config_export_artifact_ids=(CONFIG_ID,),
                checker_profiles=(),
                findings=(binding,),
                playtest_trace_artifact_ids=(PLAYTEST_TRACE_ID,),
            ),
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
            env_contract_version="env@1",
        )
    )

    assert outcome.summary.outcome_code == "patch_validation_failed"
    assert outcome.findings == ()


def test_clean_preview_passes_and_seals_evidence_set() -> None:
    store = _store()
    outcome = _handler(store)(_context(store, _payload(simulation_profiles=(_SIM,)), seed=7))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "patch_validation_passed"
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "validation_evidence"
    assert primary.payload_schema_id == "evidence-set@1"

    evidence = _read_evidence_set(store, outcome)
    assert evidence.overall_status == "passed"
    assert evidence.subject_artifact_id == SUBJECT_ID
    assert isinstance(evidence.target_binding, PatchTargetBindingV1)
    assert evidence.target_binding.target_artifact_id == PREVIEW_ID
    # one regression-evidence per checker + simulation dimension.
    regression = [a for a in outcome.artifacts if a.kind == "regression_evidence"]
    assert len(regression) == 2
    kinds = {req.kind for req in evidence.requirements}
    assert kinds == {"regression"}
    assert all(req.status == "passed" for req in evidence.requirements)
    # no auto-apply proof without an eligible evaluator.
    assert all(a.payload_schema_id != "auto-apply-proof@1" for a in outcome.artifacts)


def test_defect_preview_fails_validation() -> None:
    store = _store(snapshot=_dangling_snapshot())
    outcome = _handler(store, checker_resolver=lambda profile, constraints: GraphChecker())(
        _context(store, _payload())
    )

    assert outcome.summary.outcome_code == "patch_validation_failed"
    evidence = _read_evidence_set(store, outcome)
    assert evidence.overall_status == "failed"
    assert any(req.status == "failed" for req in evidence.requirements)
    # discovered violations are emitted as validation findings.
    assert outcome.findings, "the dangling reference must be reported as a validation finding"


def test_unproven_checker_finding_seals_exact_reasoned_dimension_wire() -> None:
    store = _store()
    preview = load_snapshot(store, PREVIEW_ID)

    class _UnprovenChecker:
        id = "graph"

        def check(self, snapshot, nav=None):
            return [_finding(status="unproven", snapshot_id=preview.snapshot_id)]

    outcome = _handler(
        store,
        checker_resolver=lambda profile, constraints: _UnprovenChecker(),
    )(_context(store, _payload()))

    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item for item in evidence.requirements if item.requirement_id == "checker:checker@1"
    )
    artifact = next(
        item for item in outcome.artifacts if item.meta.get("requirement_id") == "checker:checker@1"
    )
    sealed = decode_and_validate_artifact_payload(
        payload_schema_id=artifact.payload_schema_id,
        blob=store.read_prepared(artifact.object_ref),
    )
    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert requirement.reason_code == "checker_reported_unproven"
    assert sealed["reason_code"] == requirement.reason_code


def test_failing_regression_suite_fails_validation() -> None:
    store = _store()
    outcome = _handler(store, regression_runner=_FailingRegressionRunner())(
        _context(store, _payload(regression_suite_artifact_ids=(REGRESSION_SUITE_ID,)))
    )
    assert outcome.summary.outcome_code == "patch_validation_failed"
    evidence = _read_evidence_set(store, outcome)
    assert evidence.overall_status == "failed"
    assert any(
        req.requirement_id == f"regression:{REGRESSION_SUITE_ID}" and req.status == "failed"
        for req in evidence.requirements
    )


def test_regression_runner_rejects_llm_finding_authority() -> None:
    class LlmRegressionRunner:
        def run(self, request) -> RegressionSuiteResultV1:
            finding = Finding(
                id="regression:llm",
                source="llm",
                producer_id="llm-routed",
                producer_run_id="regression-runner",
                oracle_type="llm-assisted",
                defect_class="llm_assisted_predicate",
                severity="major",
                snapshot_id=request.snapshot_id,
                status="unproven",
                message="suggestion-only output",
            )
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="unproven",
                reason_code="llm_only",
                env_contract_version="suite-env@1",
                payload={
                    "payload_schema_version": "regression-evidence@1",
                    "suite_artifact_id": request.suite_artifact_id,
                    "snapshot_id": request.snapshot_id,
                    "status": "unproven",
                    "reason_code": "llm_only",
                    "findings": [finding.model_dump(mode="json")],
                },
            )

    store = _store()
    with pytest.raises(IntegrityViolation, match="deterministic oracle authority"):
        _handler(store, regression_runner=LlmRegressionRunner())(
            _context(
                store,
                _payload(
                    checker_profiles=(),
                    regression_suite_artifact_ids=(REGRESSION_SUITE_ID,),
                ),
                seed=7,
            )
        )


def test_unproven_regression_seals_the_adapter_reason_on_both_wires() -> None:
    store = _store()
    runner = _UnprovenRegressionRunner()
    outcome = _handler(store, regression_runner=runner)(
        _context(store, _payload(regression_suite_artifact_ids=(REGRESSION_SUITE_ID,)))
    )
    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == f"regression:{REGRESSION_SUITE_ID}"
    )
    artifact = next(
        item
        for item in outcome.artifacts
        if item.payload_schema_id == "regression-evidence@1"
        and item.meta.get("requirement_id") == f"regression:{REGRESSION_SUITE_ID}"
    )
    sealed = json.loads(store.read_prepared(artifact.object_ref))

    assert requirement.reason_code == runner.reason
    assert sealed["reason_code"] == runner.reason


def test_finding_bindings_and_supporting_are_bound_exactly() -> None:
    store = _store()
    preview = load_snapshot(store, PREVIEW_ID)
    revision = _finding_revision(_finding(status="fixed", snapshot_id=preview.snapshot_id))
    binding = _finding_binding(revision)
    outcome = _handler(
        store,
        finding_revision_loader=_ExactFindingRevisionLoader(revision),
    )(_context(store, _payload(findings=(binding,), review_artifact_ids=(REVIEW_ID,))))
    evidence = _read_evidence_set(store, outcome)
    assert evidence.finding_bindings == (binding,)
    assert REVIEW_ID in evidence.supporting_artifact_ids
    assert FINDING_EVIDENCE_ID in evidence.supporting_artifact_ids
    # the bound review/finding evidence become typed lineage parents.
    primary = outcome.artifacts[outcome.primary_index]
    assert REVIEW_ID in primary.lineage
    assert FINDING_EVIDENCE_ID in primary.lineage


def test_auto_apply_eligible_seals_proof() -> None:
    store = _store()
    outcome = _handler(store, auto_apply_evaluator=_FakeAutoApplyEvaluator())(
        _context(store, _payload())
    )
    assert outcome.summary.outcome_code == "patch_validation_auto_eligible"
    proofs = [a for a in outcome.artifacts if a.payload_schema_id == "auto-apply-proof@1"]
    assert len(proofs) == 1
    proof = AutoApplyProofV1.model_validate(json.loads(store.read_prepared(proofs[0].object_ref)))
    primary = outcome.artifacts[outcome.primary_index]
    from gameforge.platform.run_handlers.validation_common import content_addressed_artifact_id

    assert proof.validation_evidence_artifact_id == content_addressed_artifact_id(primary)


def test_empty_bound_constraint_snapshot_makes_simulation_explicitly_unproven() -> None:
    store = _store()
    outcome = _handler(store, auto_apply_evaluator=_FakeAutoApplyEvaluator())(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                checker_profiles=(),
                simulation_profiles=(_SIM,),
            ),
            seed=7,
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
        )
    )

    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert not any(
        artifact.payload_schema_id == "auto-apply-proof@1" for artifact in outcome.artifacts
    )
    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item for item in evidence.requirements if item.requirement_id == "simulation:sim@1"
    )
    assert requirement.status == "unproven"
    companion = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.meta.get("requirement_id") == "simulation:sim@1"
    )
    sealed = json.loads(store.read_prepared(companion.object_ref))
    constraint_findings = [
        finding
        for finding in sealed["detail"]["findings"]
        if finding["defect_class"] == "simulation_constraint_unproven"
    ]
    assert len(constraint_findings) == 1
    assert constraint_findings[0]["evidence"] == {
        "reason": "constraint_profile_not_executable",
        "constraint_snapshot_artifact_id": CONSTRAINT_ARTIFACT_ID,
        "constraint_ids": [],
    }


def test_bound_impossible_constraint_makes_clean_simulation_unproven_and_ineligible() -> None:
    store = _store()
    constraint = Constraint(
        id="C_impossible",
        kind="numeric",
        oracle="deterministic",
        **{"assert": "reward_gold < 0"},
        severity="critical",
    )
    store.register(
        CONSTRAINT_ARTIFACT_ID,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [constraint.model_dump(mode="json", by_alias=True)],
        },
    )
    outcome = _handler(store, auto_apply_evaluator=_FakeAutoApplyEvaluator())(
        _context(
            store,
            _payload(
                constraint_snapshot_artifact_id=CONSTRAINT_ARTIFACT_ID,
                checker_profiles=(),
                simulation_profiles=(_SIM,),
            ),
            seed=7,
            constraint_snapshot_id=CONSTRAINT_SNAPSHOT_ID,
        )
    )

    assert outcome.summary.outcome_code == "patch_validation_unproven"
    assert not any(
        artifact.payload_schema_id == "auto-apply-proof@1" for artifact in outcome.artifacts
    )
    evidence = _read_evidence_set(store, outcome)
    requirement = next(
        item for item in evidence.requirements if item.requirement_id == "simulation:sim@1"
    )
    assert requirement.status == "unproven"
    companion = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.meta.get("requirement_id") == "simulation:sim@1"
    )
    sealed = json.loads(store.read_prepared(companion.object_ref))
    assert sealed["simulation_execution_binding"]["constraint_application"] == {
        "status": "unproven",
        "reason_code": "constraint_profile_not_executable",
    }
    assert any(
        finding["defect_class"] == "simulation_constraint_unproven"
        for finding in sealed["detail"]["findings"]
    )


def test_auto_apply_proof_reseals_pre_final_sibling_references() -> None:
    store = _store()
    context = _context(store, _payload())
    outcome = _handler(store, auto_apply_evaluator=_FakeAutoApplyEvaluator())(context)
    primary = outcome.artifacts[outcome.primary_index]
    proof_artifact = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.payload_schema_id == "auto-apply-proof@1"
    )
    proof = json.loads(store.read_prepared(proof_artifact.object_ref))
    regression = tuple(
        artifact
        for artifact in outcome.artifacts
        if artifact.payload_schema_id == "regression-evidence@1"
    )
    regression_ids = tuple(content_addressed_artifact_id(artifact) for artifact in regression)
    regression_by_id = dict(zip(regression_ids, regression, strict=True))
    for collection_name in ("deterministic_oracle_evidence", "required_outcome_evidence"):
        for binding in proof[collection_name]:
            binding["evidence_payload_hash"] = regression_by_id[
                binding["evidence_artifact_id"]
            ].payload_hash
    prepared_primary_id = content_addressed_artifact_id(primary)
    final_primary_id = artifact_id_v2_for(
        kind=primary.kind,
        version_tuple=primary.version_tuple,
        lineage=(*primary.lineage, *regression_ids),
        payload_hash=primary.payload_hash,
        meta={**primary.meta, "replayability": "deterministic_recompute"},
    )
    assert final_primary_id != prepared_primary_id

    registry = build_builtin_registry()
    definition = registry.get_run_kind(PATCH_VALIDATE_KIND)
    assert definition is not None
    policy = next(
        item
        for item in definition.outcome_policies
        if item.policy_id == "patch-validation-auto-eligible"
    )
    rule = next(item for item in policy.artifact_rules if item.rule_id == "auto-apply-proof")
    binding_kwargs = dict(
        run=context.run,
        outcome_policy=policy,
        outcome_rule=rule,
        payload_schema_id="auto-apply-proof@1",
        projected_tuple=proof_artifact.version_tuple,
        final_artifact_ids_by_rule={
            "primary": (final_primary_id,),
            "regression": regression_ids,
        },
        final_sibling_facts_by_id={
            final_primary_id: FinalSiblingFact(
                artifact_id=final_primary_id,
                outcome_rule_id="primary",
                artifact_kind=primary.kind,
                payload_schema_id=primary.payload_schema_id,
                payload_hash=primary.payload_hash,
                requirement_id=None,
                requirement_kind=None,
            ),
            **{
                artifact_id: FinalSiblingFact(
                    artifact_id=artifact_id,
                    outcome_rule_id="regression",
                    artifact_kind=artifact.kind,
                    payload_schema_id=artifact.payload_schema_id,
                    payload_hash=artifact.payload_hash,
                    requirement_id=json.loads(store.read_prepared(artifact.object_ref)).get(
                        "requirement_id"
                    ),
                    requirement_kind="regression",
                )
                for artifact_id, artifact in regression_by_id.items()
            },
        },
        prepared_to_final_artifact_ids_by_rule={
            "primary": {prepared_primary_id: final_primary_id},
            "regression": {artifact_id: artifact_id for artifact_id in regression_ids},
        },
    )
    bound = bind_final_payload_references(canonical_payload=proof, **binding_kwargs)
    assert bound["validation_evidence_artifact_id"] == final_primary_id
    assert bound["regression_evidence_artifact_ids"] == list(regression_ids)
    assert {item["evidence_artifact_id"] for item in bound["deterministic_oracle_evidence"]} <= set(
        regression_ids
    )
    assert {item["evidence_artifact_id"] for item in bound["required_outcome_evidence"]} <= set(
        regression_ids
    )

    forged = {
        **proof,
        "deterministic_oracle_evidence": [
            {**proof["deterministic_oracle_evidence"][0], "evidence_payload_hash": _HEX}
        ],
    }
    with pytest.raises(IntegrityViolation, match="authoritative semantic binding"):
        bind_final_payload_references(canonical_payload=forged, **binding_kwargs)

    wrong_requirement = {
        **proof,
        "required_outcome_evidence": [
            {**proof["required_outcome_evidence"][0], "requirement_id": "checker:swapped@1"}
        ],
    }
    with pytest.raises(IntegrityViolation, match="authoritative semantic binding"):
        bind_final_payload_references(canonical_payload=wrong_requirement, **binding_kwargs)


def test_passed_scenario_has_no_proof_without_eligibility() -> None:
    store = _store()
    outcome = _handler(store)(_context(store, _payload()))
    assert outcome.summary.outcome_code == "patch_validation_passed"
    assert all(a.payload_schema_id != "auto-apply-proof@1" for a in outcome.artifacts)


def test_patch_validation_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _handler(store_a)(_context(store_a, _payload(simulation_profiles=(_SIM,)), seed=7))
    out_b = _handler(store_b)(_context(store_b, _payload(simulation_profiles=(_SIM,)), seed=7))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]


def test_stochastic_child_uses_subseed_while_artifact_tuples_keep_root_seed() -> None:
    store = _store()
    simulator = _RecordingSimulator()
    regression = _RecordingRegressionRunner()
    payload = _payload(
        simulation_profiles=(_SIM,),
        regression_suite_artifact_ids=(REGRESSION_SUITE_ID,),
    )
    outcome = _handler(
        store,
        simulator=simulator,
        regression_runner=regression,
    )(_context(store, payload, seed=17))

    expected = derive_validation_subseed(
        root_seed=17,
        run_kind=PATCH_VALIDATE_KIND,
        profile=_SIM,
        case_id="simulation:sim@1",
        replication_index=0,
    )
    expected_regression = derive_validation_subseed(
        root_seed=17,
        run_kind=PATCH_VALIDATE_KIND,
        profile=_VALIDATION,
        case_id=REGRESSION_SUITE_ID,
        replication_index=0,
    )
    assert expected == 4264169507110697303  # frozen subseed@1 canonical-vector
    assert simulator.seeds == [expected]
    assert regression.seeds == [expected_regression]
    assert all(artifact.version_tuple.seed == 17 for artifact in outcome.artifacts)

    simulation = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.meta.get("requirement_id") == "simulation:sim@1"
    )
    evidence = json.loads(store.read_prepared(simulation.object_ref))
    assert evidence["root_seed"] == 17
    assert evidence["run_kind"] == {"kind": "patch.validate", "version": 1}
    assert evidence["profile_id"] == "sim"
    assert evidence["profile_version"] == 1
    assert evidence["case_id"] == "simulation:sim@1"
    assert evidence["replication_index"] == 0
    assert evidence["seed"] == expected
    assert evidence["seed_derivation_version"] == VALIDATION_SEED_DERIVATION_VERSION

    regression_artifact = next(
        artifact
        for artifact in outcome.artifacts
        if artifact.meta.get("requirement_id") == f"regression:{REGRESSION_SUITE_ID}"
    )
    regression_evidence = json.loads(store.read_prepared(regression_artifact.object_ref))
    assert regression_evidence["root_seed"] == 17
    assert regression_evidence["seed"] == expected_regression
    assert regression_evidence["profile_id"] == "validation"


def test_deterministic_validation_keeps_root_seed_null_and_internal_default_private() -> None:
    store = _store()
    regression = _RecordingRegressionRunner()
    outcome = _handler(store, regression_runner=regression)(
        _context(
            store,
            _payload(regression_suite_artifact_ids=(REGRESSION_SUITE_ID,)),
            seed=None,
        )
    )

    assert regression.seeds == [DETERMINISTIC_VALIDATION_EXECUTION_SEED]
    assert all(artifact.version_tuple.seed is None for artifact in outcome.artifacts)


def test_regression_evidence_seed_comes_from_the_exact_execution_request() -> None:
    class MisreportingSeedRunner:
        def run(self, request) -> RegressionSuiteResultV1:
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="passed",
                env_contract_version="suite-env@1",
                payload={
                    "payload_schema_version": "forged-regression-evidence@9",
                    "suite_artifact_id": request.suite_artifact_id,
                    "snapshot_id": request.snapshot_id,
                    "seed": request.seed + 1,
                    "status": "passed",
                },
                action_work_units=1,
            )

    store = _store()
    outcome = _handler(store, regression_runner=MisreportingSeedRunner())(
        _context(
            store,
            _payload(regression_suite_artifact_ids=(REGRESSION_SUITE_ID,)),
            seed=None,
        )
    )
    artifact = next(
        item
        for item in outcome.artifacts
        if item.meta.get("requirement_id") == f"regression:{REGRESSION_SUITE_ID}"
    )
    sealed = json.loads(store.read_prepared(artifact.object_ref))

    assert sealed["payload_schema_version"] == "regression-evidence@1"
    assert sealed["seed"] == DETERMINISTIC_VALIDATION_EXECUTION_SEED


def test_simulation_without_frozen_root_seed_fails_closed() -> None:
    store = _store()
    with pytest.raises(ValueError, match="frozen root seed"):
        _handler(store)(_context(store, _payload(simulation_profiles=(_SIM,)), seed=None))


def test_wrong_payload_type_is_rejected() -> None:
    from gameforge.contracts.jobs import SimulationRunPayloadV1

    store = _store()
    sim = SimulationRunPayloadV1(
        snapshot_artifact_id=PREVIEW_ID,
        simulation_profile=ProfileRefV1(profile_id="sim", version=1),
        workload_profile=ProfileRefV1(profile_id="wl", version=1),
        replication_count=1,
        horizon_steps=1,
    )
    context = build_context(params=sim, kind=RunKindRef(kind="simulation.run", version=1), seed=1)
    with pytest.raises(TypeError):
        _handler(store)(context)
