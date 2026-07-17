"""Deterministic built-in policy materialization for the M4c composition root."""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType
from typing import Any, get_args

from pydantic import BaseModel

from gameforge.contracts.benchmark import (
    BenchmarkEvaluatorProfileConfigV1,
    build_builtin_benchmark_evaluator_policy,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.execution_graphs import (
    AgentExecutionGraphV1,
    AgentExecutionNodeV1,
    AgentExecutionProfileSelectorV1,
    agent_execution_graph_digest,
)
from gameforge.contracts.execution_profiles import (
    ArtifactCollectionResolvedPolicyRequirementConfigV1,
    ArtifactLineagePolicyRefV1,
    CheckerProfileConfigV1,
    ConfigExportProfileDetailsV1,
    ConstraintExtractionProfileConfigV1,
    EnvironmentContractDescriptorV1,
    EnvironmentProfileDetailsV1,
    ExecutionProfileCatalogSnapshotV1,
    ExecutionProfileDefinitionV1,
    ExecutionProfileKindV1,
    ExecutionProfileLifecycleV1,
    GenericProfileDetailsV1,
    GenerationProfileConfigV1,
    MigrationCapabilityMatrixRefV1,
    MigrationCapabilityMatrixRegistryV1,
    MigrationCapabilityMatrixV1,
    MigrationKindDefaultV1,
    MigrationProfileDetailsV1,
    PatchRepairProfileConfigV1,
    PlaytestPlannerProfileConfigV1,
    PlaytestPlannerProfileConfigV2,
    ProfileRefV1,
    ProfileCollectionResolvedPolicyRequirementConfigV1,
    FixedResolvedPolicyRequirementConfigV1,
    ResolvedPolicyProfileConfigV1,
    ReviewProfileConfigV1,
    RunKindRef,
    SimulationProfileConfigV1,
    TaskSuiteDerivationProfileConfigV1,
    TaskSuiteDerivationProfileConfigV2,
    ValidationProfileDetailsV1,
    VersionTransitionPolicyRefV1,
    WorkloadProfileConfigV1,
    canonical_config_hash,
    execution_profile_catalog_digest,
    migration_capability_matrix_digest,
    migration_capability_registry_digest,
)
from gameforge.contracts.identity import DomainScope, Permission
from gameforge.contracts.jobs import (
    ArtifactIdentityBindingV1,
    ArtifactLineagePolicyV1,
    ArtifactParentRuleV1,
    ExecutionIdentityCountBindingV1,
    ExecutionModeCountBindingV1,
    ExecutionModeCountsV1,
    FailureClassificationRuleV1,
    FailureClassifierRefV1,
    FailureClassifierV1,
    FindingOutputPolicyRefV1,
    FindingOutputPolicyV1,
    IntermediateCountBindingV1,
    JsonCollectionCountBindingV1,
    OutcomeArtifactPolicyV1,
    OutcomeArtifactRuleV1,
    ResolvedPolicyCountBindingV1,
    ResolvedPolicySubsetCountBindingV1,
    RetryPolicyRefV1,
    RetryPolicySnapshot,
    RunEventRegistryV1,
    RunKindDefinition,
    RuntimeParentRuleSetRef,
    RuntimeParentRuleSetV1,
    RuntimeParentRuleV1,
    TerminalPublisherHooks,
    VersionFieldProjectionRuleV1,
    VersionTransitionFieldRuleV1,
    VersionTransitionModeRuleV1,
    VersionTransitionPolicyV1,
    artifact_lineage_policy_digest,
    failure_classifier_digest,
    frozen_run_event_definitions_v1,
    retry_policy_digest,
    run_event_registry_digest,
)
from gameforge.contracts.lineage import ArtifactKind, VersionTuple
from gameforge.contracts.playtest import (
    CompletionOracleDefinitionV1,
    CompletionOracleRegistryV1,
    PlaytestPayloadSchemaDefinitionV1,
    PlaytestPayloadSchemaRegistryV1,
    compute_completion_oracle_registry_digest,
    compute_playtest_payload_schema_registry_digest,
)
from gameforge.platform.registry.model import (
    FROZEN_PROFILE_REQUIREMENT_SHAPES,
    FROZEN_RUN_KIND_SHAPES,
    ProfileRequirement,
    RunKindIdentity,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.lineage.validation import PRODUCER_RULES


_POLICY_VERSION = 1
_SCHEMA_VERSION = "@1"
_PROFILE_CHANGED_AT = "2026-07-14T00:00:00Z"
_ALL_ARTIFACT_KINDS = tuple(get_args(ArtifactKind))
_VERSION_FIELDS = tuple(VersionTuple.model_fields)
_ARTIFACT_SCHEMAS: dict[ArtifactKind, tuple[str, ...]] = {
    "source_raw": ("source-raw@1", "agent-prompt-context@1"),
    "source_rendered": ("source-rendered@1",),
    "ir_snapshot": ("ir-core@1",),
    "constraint_snapshot": ("constraint-snapshot@1",),
    "constraint_proposal": ("constraint-proposal@1",),
    "config_export": ("config-export-package@1",),
    "scenario_spec": ("scenario-spec@1",),
    "task_suite": ("task-suite@1",),
    "regression_suite": ("regression-suite@1",),
    "golden_suite": ("golden-suite@1",),
    "bench_dataset": ("bench-dataset@1",),
    "benchmark_spec": ("benchmark-spec@1",),
    "review_report": ("review@1",),
    "checker_run": ("checker-report@1",),
    "simulation_run": ("simulation-result@1",),
    "playtest_trace": ("playtest-trace@1",),
    "patch": ("patch@2",),
    "validation_evidence": (
        "auto-apply-proof@1",
        "constraint-compile-evidence@1",
        "evidence-set@1",
    ),
    "regression_evidence": ("regression-evidence@1",),
    "rollback_request": ("rollback-request@1",),
    "run_result": ("run-result@1",),
    "run_failure": ("run-failure@1",),
    "cassette_bundle": ("cassette-bundle@1", "cassette-record-shard@1"),
    "migration_report": ("migration-report@1",),
    "bench_report": ("bench-report@2",),
    "operational_evidence": ("backup-object-manifest@1", "dr-drill-evidence@1"),
}
ARTIFACT_PAYLOAD_SCHEMAS = MappingProxyType(_ARTIFACT_SCHEMAS)

# Complete output interface advertised by each semantic profile kind.  A profile may
# participate in several RunKinds (for example checker output is a standalone report
# in ``checker.run`` and a re-verification evidence row in ``patch.validate``), so the
# retained catalog declares the union instead of a placeholder string. Admission can
# then reject a selected profile whose frozen interface cannot produce the schemas its
# profile kind promises, before any Run or budget hold is created.
PROFILE_OUTPUT_SCHEMA_REQUIREMENTS: MappingProxyType[ExecutionProfileKindV1, tuple[str, ...]] = (
    MappingProxyType(
        {
            "generation": (
                "checker-report@1",
                "config-export-package@1",
                "ir-core@1",
                "patch@2",
                "review@1",
                "simulation-result@1",
            ),
            "patch_repair": (
                "checker-report@1",
                "config-export-package@1",
                "ir-core@1",
                "patch@2",
                "regression-evidence@1",
                "simulation-result@1",
            ),
            "constraint_extraction": ("constraint-proposal@1",),
            "review": ("review@1",),
            "llm_triage": ("review@1",),
            "checker": ("checker-report@1", "regression-evidence@1"),
            "simulation": ("regression-evidence@1", "simulation-result@1"),
            "workload": ("simulation-result@1",),
            "config_export": ("config-export-package@1",),
            "task_suite_derivation": ("scenario-spec@1", "task-suite@1"),
            "environment": ("playtest-trace@1", "scenario-spec@1"),
            "playtest_planner": ("playtest-trace@1",),
            "validation": ("auto-apply-proof@1", "evidence-set@1"),
            "constraint_compiler": (
                "constraint-compile-evidence@1",
                "constraint-snapshot@1",
            ),
            "rollback": ("evidence-set@1", "regression-evidence@1"),
            "schema_compatibility": ("regression-evidence@1",),
            "impact_analysis": ("regression-evidence@1",),
            "bench_evaluator": ("bench-report@2",),
            "artifact_migrator": ("migration-report@1",),
            "dr_plan": ("dr-drill-evidence@1",),
            "restore_target": ("dr-drill-evidence@1",),
            "dr_verifier": ("dr-drill-evidence@1",),
        }
    )
)


def _model_digest(value: BaseModel) -> str:
    return canonical_sha256(value.model_dump(mode="json"))


def _ref(kind: RunKindIdentity) -> RunKindRef:
    return RunKindRef(kind=kind[0], version=kind[1])


def _failure_classifier() -> FailureClassifierV1:
    dependency_kinds = (
        "model_provider",
        "database",
        "object_store",
        "cost_ledger",
        "solver_executor",
        "simulation_backend",
        "game_environment",
        "identity_provider",
    )
    rows = (
        ("cancelled", "cancelled", False, ()),
        ("dependency_unavailable", "transient_dependency", True, dependency_kinds),
        ("execution_failed", "execution", False, ()),
        ("generation_gate_rejected", "business_rule", False, ()),
        ("integrity_violation", "integrity", False, ()),
        ("lease_expired", "lease", True, ()),
        ("permanent_dependency_failed", "permanent_dependency", False, dependency_kinds),
        ("quota_exceeded", "quota", False, ()),
        ("queue_timed_out", "timeout", False, ()),
        ("repair_unverified", "validation", False, ()),
        ("subject_superseded", "subject_superseded", False, ()),
        ("timed_out", "timeout", False, ()),
    )
    rules = tuple(
        FailureClassificationRuleV1(
            cause_code=cause,
            failure_class=failure_class,
            intrinsic_retry_eligible=eligible,
            dependency_required=bool(dependencies),
            allowed_dependency_kinds=dependencies,
        )
        for cause, failure_class, eligible, dependencies in rows
    )
    payload = {"classifier_version": 1, "rules": rules}
    return FailureClassifierV1(
        **payload,
        classifier_digest=failure_classifier_digest(payload),
    )


def _retry_policy(policy_id: str, *, max_attempts: int) -> RetryPolicySnapshot:
    payload: dict[str, Any] = {
        "retry_policy_id": policy_id,
        "retry_policy_version": 1,
        "max_attempts": max_attempts,
        "retryable_failure_classes": ("lease", "transient_dependency"),
        "backoff": "exponential",
        "base_delay_ms": 250,
        "max_delay_ms": 30_000,
        "jitter_policy": "deterministic-request-hash@1",
        "honor_retry_after": True,
    }
    return RetryPolicySnapshot(
        **payload,
        retry_policy_digest=retry_policy_digest(payload),
    )


def _retry_policies() -> tuple[RetryPolicySnapshot, ...]:
    return tuple(
        _retry_policy(policy_id, max_attempts=max_attempts)
        for policy_id, max_attempts in (
            ("llm_transient", 3),
            ("composite_transient", 3),
            ("deterministic_job", 2),
            ("agent_environment", 3),
            ("validation_job", 2),
            ("migration_job", 2),
            ("operational_job", 2),
        )
    )


def _transition_policy(*, scope: str) -> VersionTransitionPolicyV1:
    cassette_scope = "attempt_bundle" if scope == "attempt" else "run_bundle"
    modes: list[VersionTransitionModeRuleV1] = []
    for mode in ("not_applicable", "live", "record", "replay"):
        rules: list[VersionTransitionFieldRuleV1] = []
        for field in _VERSION_FIELDS:
            operation = "copy_frozen"
            exact_cassette_scope = None
            if field in {"prompt_version", "model_snapshot"}:
                operation = (
                    "set_null_no_invocation"
                    if mode == "not_applicable"
                    else "set_from_execution_identity"
                )
            elif field == "agent_graph_version" and mode != "not_applicable":
                operation = "set_from_execution_identity"
            elif field == "cassette_id":
                if mode in {"not_applicable", "live"}:
                    operation = "set_null_no_invocation"
                else:
                    operation = "set_from_exact_cassette_parent"
                    exact_cassette_scope = cassette_scope if mode == "record" else "replay_input"
            rules.append(
                VersionTransitionFieldRuleV1(
                    field=field,
                    operation=operation,
                    cassette_scope=exact_cassette_scope,
                )
            )
        modes.append(
            VersionTransitionModeRuleV1(
                llm_execution_mode=mode,
                field_rules=tuple(rules),
            )
        )
    return VersionTransitionPolicyV1(
        policy_schema_version="version-transition-policy@1",
        policy_id=f"{scope}-manifest-transition",
        policy_version=1,
        manifest_scope=scope,
        mode_rules=tuple(modes),
    )


def _transition_ref(policy: VersionTransitionPolicyV1) -> VersionTransitionPolicyRefV1:
    return VersionTransitionPolicyRefV1(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        digest=_model_digest(policy),
    )


def _runtime_parent_rules() -> RuntimeParentRuleSetV1:
    rules = (
        RuntimeParentRuleV1(
            rule_id="attempt-prompts",
            manifest_scope="attempt",
            source="published_intermediate",
            parent_role="intermediate",
            artifact_kind="source_rendered",
            payload_schema_ids=("source-rendered@1",),
            attempt_selector="current",
            min_count=0,
            max_count=None,
            count_binding=IntermediateCountBindingV1(
                link_role="prompt_rendered",
                scope="current_attempt",
            ),
        ),
        RuntimeParentRuleV1(
            rule_id="run-prompts",
            manifest_scope="run",
            source="published_intermediate",
            parent_role="intermediate",
            artifact_kind="source_rendered",
            payload_schema_ids=("source-rendered@1",),
            attempt_selector="all_closed",
            min_count=0,
            max_count=None,
            count_binding=IntermediateCountBindingV1(
                link_role="prompt_rendered",
                scope="all_attempts",
            ),
        ),
        RuntimeParentRuleV1(
            rule_id="attempt-agent-prompt-contexts",
            manifest_scope="attempt",
            source="published_intermediate",
            parent_role="intermediate",
            artifact_kind="source_raw",
            payload_schema_ids=("agent-prompt-context@1",),
            attempt_selector="current",
            enabled_execution_modes=("live", "record", "replay"),
            min_count=0,
            max_count=None,
            count_binding=IntermediateCountBindingV1(
                link_role="agent_prompt_context",
                scope="current_attempt",
            ),
        ),
        RuntimeParentRuleV1(
            rule_id="run-agent-prompt-contexts",
            manifest_scope="run",
            source="published_intermediate",
            parent_role="intermediate",
            artifact_kind="source_raw",
            payload_schema_ids=("agent-prompt-context@1",),
            attempt_selector="all_closed",
            enabled_execution_modes=("live", "record", "replay"),
            min_count=0,
            max_count=None,
            count_binding=IntermediateCountBindingV1(
                link_role="agent_prompt_context",
                scope="all_attempts",
            ),
        ),
        RuntimeParentRuleV1(
            rule_id="attempt-record-shards",
            manifest_scope="attempt",
            source="record_shard",
            parent_role="intermediate",
            artifact_kind="cassette_bundle",
            payload_schema_ids=("cassette-record-shard@1",),
            attempt_selector="current",
            min_count=0,
            max_count=None,
            enabled_execution_modes=("record",),
            count_binding=ExecutionIdentityCountBindingV1(
                scope="current_attempt",
            ),
        ),
        RuntimeParentRuleV1(
            rule_id="run-record-shards",
            manifest_scope="run",
            source="record_shard",
            parent_role="intermediate",
            artifact_kind="cassette_bundle",
            payload_schema_ids=("cassette-record-shard@1",),
            attempt_selector="all_closed",
            min_count=0,
            max_count=None,
            enabled_execution_modes=("record",),
            count_binding=ExecutionIdentityCountBindingV1(
                scope="all_attempts",
            ),
        ),
        RuntimeParentRuleV1(
            rule_id="attempt-cassette-bundle",
            manifest_scope="attempt",
            source="attempt_bundle",
            parent_role="intermediate",
            artifact_kind="cassette_bundle",
            payload_schema_ids=("cassette-bundle@1",),
            attempt_selector="current",
            min_count=0,
            max_count=1,
            enabled_execution_modes=("record",),
            count_binding=ExecutionModeCountBindingV1(
                exact_count_by_mode=ExecutionModeCountsV1(
                    not_applicable=0,
                    live=0,
                    record=1,
                    replay=0,
                )
            ),
        ),
        RuntimeParentRuleV1(
            rule_id="run-cassette-bundle",
            manifest_scope="run",
            source="run_bundle",
            parent_role="intermediate",
            artifact_kind="cassette_bundle",
            payload_schema_ids=("cassette-bundle@1",),
            attempt_selector="all_closed",
            min_count=0,
            max_count=1,
            enabled_execution_modes=("record",),
            count_binding=ExecutionModeCountBindingV1(
                exact_count_by_mode=ExecutionModeCountsV1(
                    not_applicable=0,
                    live=0,
                    record=1,
                    replay=0,
                )
            ),
        ),
        RuntimeParentRuleV1(
            rule_id="replay-input-cassette-bundle",
            manifest_scope="both",
            source="run_input",
            parent_role="input",
            artifact_kind="cassette_bundle",
            payload_schema_ids=("cassette-bundle@1",),
            attempt_selector="none",
            min_count=1,
            max_count=1,
            enabled_execution_modes=("replay",),
            count_binding=ExecutionModeCountBindingV1(
                exact_count_by_mode=ExecutionModeCountsV1(
                    not_applicable=0,
                    live=0,
                    record=0,
                    replay=1,
                )
            ),
        ),
        RuntimeParentRuleV1(
            rule_id="closed-attempt-failures",
            manifest_scope="run",
            source="closed_attempt_failure",
            parent_role="intermediate",
            artifact_kind="run_failure",
            payload_schema_ids=("run-failure@1",),
            attempt_selector="all_closed",
            min_count=0,
            max_count=None,
        ),
    )
    return RuntimeParentRuleSetV1(
        rule_set_id="runtime-parents",
        version=1,
        rules=rules,
    )


def _runtime_parent_ref(rule_set: RuntimeParentRuleSetV1) -> RuntimeParentRuleSetRef:
    return RuntimeParentRuleSetRef(
        rule_set_id=rule_set.rule_set_id,
        version=rule_set.version,
        digest=_model_digest(rule_set),
    )


def _finding_policies() -> tuple[FindingOutputPolicyV1, ...]:
    return (
        FindingOutputPolicyV1(
            policy_id="review-findings",
            policy_version=1,
            max_findings=10_000,
            allowed_evidence_outcome_rule_ids=("primary", "checker", "simulation"),
            allowed_oracle_types=("deterministic", "llm-assisted", "simulation"),
            allowed_sources=("checker", "llm", "sim"),
        ),
        FindingOutputPolicyV1(
            policy_id="checker-findings",
            policy_version=1,
            max_findings=10_000,
            allowed_evidence_outcome_rule_ids=("primary",),
            allowed_oracle_types=("deterministic",),
            allowed_sources=("checker",),
        ),
        FindingOutputPolicyV1(
            policy_id="simulation-findings",
            policy_version=1,
            max_findings=10_000,
            allowed_evidence_outcome_rule_ids=("primary",),
            allowed_oracle_types=("simulation",),
            allowed_sources=("sim",),
        ),
        FindingOutputPolicyV1(
            policy_id="playtest-findings",
            policy_version=1,
            max_findings=10_000,
            allowed_evidence_outcome_rule_ids=("primary",),
            allowed_oracle_types=("deterministic", "llm-assisted"),
            allowed_sources=("playtest",),
        ),
        FindingOutputPolicyV1(
            policy_id="validation-findings",
            policy_version=1,
            max_findings=10_000,
            allowed_evidence_outcome_rule_ids=(
                "auto-apply-proof",
                "compile-evidence",
                "primary",
                "regression",
            ),
            allowed_oracle_types=("deterministic", "simulation"),
            allowed_sources=("checker", "playtest", "sim"),
        ),
    )


def _finding_ref(policy: FindingOutputPolicyV1) -> FindingOutputPolicyRefV1:
    return FindingOutputPolicyRefV1(
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        digest=_model_digest(policy),
    )


def _json_count(
    pointer: str,
    *,
    identity: ArtifactIdentityBindingV1 | None = None,
    source: str = "run_payload",
) -> JsonCollectionCountBindingV1:
    return JsonCollectionCountBindingV1(
        source=source,
        collection_pointer=pointer,
        identity_binding=identity,
    )


def _requirement_identity() -> ArtifactIdentityBindingV1:
    return ArtifactIdentityBindingV1(
        collection_item_pointer="/requirement_id",
        artifact_value_source="payload",
        artifact_payload_pointer="/requirement_id",
    )


def _resolved(policy_id: str, rule_id: str) -> ResolvedPolicyCountBindingV1:
    return ResolvedPolicyCountBindingV1(
        resolved_policy_id=policy_id,
        outcome_rule_id=rule_id,
        identity_binding=_requirement_identity(),
    )


def _subset(
    policy_id: str,
    rule_id: str,
    reasons: tuple[str, ...],
) -> ResolvedPolicySubsetCountBindingV1:
    return ResolvedPolicySubsetCountBindingV1(
        resolved_policy_id=policy_id,
        outcome_rule_id=rule_id,
        allowed_not_executed_reason_codes=reasons,
        identity_binding=_requirement_identity(),
    )


def _schemas_for(kinds: tuple[ArtifactKind, ...]) -> tuple[str, ...]:
    return tuple(sorted({schema for kind in kinds for schema in _ARTIFACT_SCHEMAS[kind]}))


def _parent(
    parent_role: str,
    *,
    source: str,
    kinds: tuple[ArtifactKind, ...],
    min_count: int = 1,
    max_count: int | None = 1,
    source_rule_id: str | None = None,
    child_payload_pointer: str | None = None,
    schemas: tuple[str, ...] | None = None,
) -> ArtifactParentRuleV1:
    return ArtifactParentRuleV1(
        parent_role=parent_role,
        source=source,
        source_rule_id=source_rule_id,
        child_payload_pointer=child_payload_pointer,
        artifact_kinds=kinds,
        payload_schema_ids=schemas or _schemas_for(kinds),
        min_count=min_count,
        max_count=max_count,
    )


def _rendered_parent() -> ArtifactParentRuleV1:
    return _parent(
        "rendered_prompt",
        source="run_intermediate",
        kinds=("source_rendered",),
        min_count=0,
        max_count=None,
    )


def _supporting_input_parent() -> ArtifactParentRuleV1:
    return _parent(
        "supporting_evidence",
        source="run_input",
        kinds=(
            "review_report",
            "checker_run",
            "simulation_run",
            "playtest_trace",
            "validation_evidence",
            "regression_evidence",
        ),
        min_count=0,
        max_count=None,
    )


def _lineage_spec(
    *,
    policy_id: str,
    rule_id: str,
    child_kind: ArtifactKind,
) -> tuple[tuple[ArtifactParentRuleV1, ...], dict[str, tuple[str, tuple[str, ...]]]]:
    """Return the frozen typed direct-parent roles and inherited tuple fields."""

    parents: list[ArtifactParentRuleV1] = []
    projection: dict[str, tuple[str, tuple[str, ...]]] = {}

    if policy_id.startswith("generation-gate-"):
        if child_kind == "patch":
            parents.extend(
                (
                    _parent("snapshot", source="run_input", kinds=("ir_snapshot",)),
                    _parent(
                        "constraint",
                        source="run_input",
                        kinds=("constraint_snapshot",),
                        min_count=0,
                    ),
                    _parent("goal", source="run_input", kinds=("source_raw",)),
                    _supporting_input_parent(),
                    _rendered_parent(),
                )
            )
            projection.update(
                {
                    "doc_version": ("snapshot", ()),
                    "ir_snapshot_id": ("snapshot", ()),
                    "constraint_snapshot_id": ("constraint", ()),
                }
            )
        elif child_kind == "ir_snapshot":
            parents.extend(
                (
                    _parent("base", source="run_input", kinds=("ir_snapshot",)),
                    _parent(
                        "patch",
                        source="prepared_rule",
                        source_rule_id="primary",
                        kinds=("patch",),
                    ),
                )
            )
            projection["doc_version"] = ("base", ())
        elif child_kind == "config_export":
            parents.extend(
                (
                    _parent(
                        "preview",
                        source="prepared_rule",
                        source_rule_id="preview",
                        kinds=("ir_snapshot",),
                    ),
                    _parent(
                        "constraint",
                        source="run_input",
                        kinds=("constraint_snapshot",),
                    ),
                )
            )
            projection.update(
                {
                    "doc_version": ("preview", ()),
                    "ir_snapshot_id": ("preview", ()),
                    "constraint_snapshot_id": ("constraint", ()),
                }
            )
        else:
            parents.extend(
                (
                    _parent(
                        "preview",
                        source="prepared_rule",
                        source_rule_id="preview",
                        kinds=("ir_snapshot",),
                    ),
                    _parent(
                        "constraint",
                        source="run_input",
                        kinds=("constraint_snapshot",),
                        min_count=0,
                    ),
                )
            )
            projection.update(
                {
                    "ir_snapshot_id": ("preview", ()),
                    "constraint_snapshot_id": ("constraint", ()),
                }
            )

    elif policy_id.startswith("repair-"):
        has_preview = policy_id == "repair-verified"
        if child_kind == "patch":
            parents.extend(
                (
                    _parent("subject", source="run_input", kinds=("patch",)),
                    _parent("base", source="run_input", kinds=("ir_snapshot",)),
                    _parent("preview", source="run_input", kinds=("ir_snapshot",)),
                    _parent(
                        "constraint",
                        source="run_input",
                        kinds=("constraint_snapshot",),
                        min_count=0,
                    ),
                    _parent(
                        "validation",
                        source="run_input",
                        kinds=("validation_evidence",),
                        schemas=("evidence-set@1",),
                    ),
                    _supporting_input_parent(),
                    _rendered_parent(),
                )
            )
            projection.update(
                {
                    "doc_version": ("base", ()),
                    "ir_snapshot_id": ("base", ()),
                    "constraint_snapshot_id": ("constraint", ()),
                }
            )
        elif child_kind == "ir_snapshot":
            parents.extend(
                (
                    _parent("base", source="run_input", kinds=("ir_snapshot",)),
                    _parent(
                        "patch",
                        source="prepared_rule",
                        source_rule_id="primary",
                        kinds=("patch",),
                    ),
                )
            )
            projection["doc_version"] = ("base", ())
        elif child_kind == "config_export":
            parents.extend(
                (
                    _parent(
                        "preview",
                        source="prepared_rule",
                        source_rule_id="preview",
                        kinds=("ir_snapshot",),
                    ),
                    _parent(
                        "constraint",
                        source="run_input",
                        kinds=("constraint_snapshot",),
                    ),
                )
            )
            projection.update(
                {
                    "doc_version": ("preview", ()),
                    "ir_snapshot_id": ("preview", ()),
                    "constraint_snapshot_id": ("constraint", ()),
                }
            )
        else:
            preview_source = "prepared_rule" if has_preview else "run_input"
            parents.extend(
                (
                    _parent(
                        "preview",
                        source=preview_source,
                        source_rule_id="preview" if has_preview else None,
                        kinds=("ir_snapshot",),
                    ),
                    _parent(
                        "constraint",
                        source="run_input",
                        kinds=("constraint_snapshot",),
                        min_count=0,
                    ),
                )
            )
            projection.update(
                {
                    "ir_snapshot_id": ("preview", ()),
                    "constraint_snapshot_id": ("constraint", ()),
                }
            )
            if child_kind == "regression_evidence":
                parents.append(
                    _parent(
                        "regression_suite",
                        source="child_payload_reference",
                        child_payload_pointer="/suite_artifact_id",
                        kinds=("regression_suite",),
                    )
                )
                projection["doc_version"] = ("preview", ())
                # Each evidence Artifact inherits the exact environment contract
                # from its referenced suite.  The repair Run-wide tuple cannot
                # safely represent this fact when suites use distinct contracts.
                projection["env_contract_version"] = ("regression_suite", ())

    elif policy_id == "constraint-proposal-drafted":
        parents.extend(
            (
                _parent(
                    "source",
                    source="run_input",
                    kinds=("source_raw", "source_rendered"),
                    min_count=1,
                    max_count=None,
                ),
                _parent(
                    "goal",
                    source="run_input",
                    kinds=("source_raw",),
                ),
                _parent(
                    "base_constraint",
                    source="run_input",
                    kinds=("constraint_snapshot",),
                    min_count=0,
                ),
                _rendered_parent(),
            )
        )
        projection.update(
            {
                "doc_version": ("source", ("source",)),
                "ir_snapshot_id": ("base_constraint", ()),
                "constraint_snapshot_id": ("base_constraint", ()),
            }
        )

    elif policy_id == "review-completed" or policy_id in {
        "checker-completed",
        "simulation-completed",
    }:
        parents.extend(
            (
                _parent("snapshot", source="run_input", kinds=("ir_snapshot",)),
                _parent(
                    "constraint",
                    source="run_input",
                    kinds=("constraint_snapshot",),
                    min_count=0,
                ),
            )
        )
        if policy_id == "simulation-completed":
            parents.append(
                _parent(
                    "scenario",
                    source="run_input",
                    kinds=("scenario_spec",),
                    min_count=0,
                )
            )
        if policy_id == "review-completed":
            parents.append(_rendered_parent())
        projection.update(
            {
                "ir_snapshot_id": (
                    "snapshot",
                    ("scenario",) if policy_id == "simulation-completed" else (),
                ),
                "constraint_snapshot_id": (
                    "constraint",
                    ("scenario",) if policy_id == "simulation-completed" else (),
                ),
            }
        )
        if policy_id == "simulation-completed":
            projection["env_contract_version"] = ("scenario", ())

    elif policy_id == "task-suite-derived":
        parents.extend(
            (
                _parent("preview", source="run_input", kinds=("ir_snapshot",)),
                _parent("config", source="run_input", kinds=("config_export",)),
                _parent("constraint", source="run_input", kinds=("constraint_snapshot",)),
            )
        )
        if rule_id == "primary":
            parents.append(
                _parent(
                    "scenarios",
                    source="prepared_rule",
                    source_rule_id="scenario",
                    kinds=("scenario_spec",),
                    min_count=1,
                    max_count=None,
                )
            )
        scenario_equality = ("scenarios",) if rule_id == "primary" else ()
        projection.update(
            {
                "doc_version": ("preview", ("config", *scenario_equality)),
                "ir_snapshot_id": ("preview", ("config", *scenario_equality)),
                "constraint_snapshot_id": (
                    "constraint",
                    ("config", *scenario_equality),
                ),
                "env_contract_version": ("config", scenario_equality),
            }
        )

    elif policy_id == "playtest-completed":
        parents.extend(
            (
                _parent("config", source="run_input", kinds=("config_export",)),
                _parent("constraint", source="run_input", kinds=("constraint_snapshot",)),
                _parent("task_suite", source="run_input", kinds=("task_suite",)),
                _parent(
                    "selected_scenarios",
                    source="run_input",
                    kinds=("scenario_spec",),
                    min_count=1,
                    max_count=None,
                ),
                _rendered_parent(),
            )
        )
        projection.update(
            {
                "ir_snapshot_id": (
                    "config",
                    ("selected_scenarios", "task_suite"),
                ),
                "constraint_snapshot_id": (
                    "constraint",
                    ("config", "selected_scenarios", "task_suite"),
                ),
                "env_contract_version": (
                    "config",
                    ("selected_scenarios", "task_suite"),
                ),
            }
        )

    elif policy_id.startswith("patch-validation-"):
        parents.extend(
            (
                _parent("subject", source="run_input", kinds=("patch",)),
                _parent("target", source="run_input", kinds=("ir_snapshot",)),
                _parent(
                    "constraint",
                    source="run_input",
                    kinds=("constraint_snapshot",),
                    min_count=0,
                    max_count=1,
                ),
                _parent(
                    "candidate_config",
                    source="run_input",
                    kinds=("config_export",),
                    min_count=0,
                    max_count=None,
                ),
                _supporting_input_parent(),
            )
        )
        if rule_id == "regression":
            parents.append(
                _parent(
                    "regression_suite",
                    source="child_payload_reference",
                    child_payload_pointer="/lineage_suite_artifact_ids",
                    kinds=("regression_suite",),
                    min_count=0,
                    max_count=1,
                )
            )
        if rule_id == "primary":
            parents.extend(
                (
                    _parent(
                        "regression_suite",
                        source="run_input",
                        kinds=("regression_suite",),
                        min_count=0,
                        max_count=None,
                    ),
                    _parent(
                        "regression",
                        source="prepared_rule",
                        source_rule_id="regression",
                        kinds=("regression_evidence",),
                        min_count=0,
                        max_count=None,
                    ),
                )
            )
        elif rule_id == "auto-apply-proof":
            parents.extend(
                (
                    _parent(
                        "evidence_set",
                        source="prepared_rule",
                        source_rule_id="primary",
                        kinds=("validation_evidence",),
                        schemas=("evidence-set@1",),
                    ),
                    _parent(
                        "regression",
                        source="prepared_rule",
                        source_rule_id="regression",
                        kinds=("regression_evidence",),
                        min_count=0,
                        max_count=None,
                    ),
                )
            )
        derived_equality: tuple[str, ...] = ()
        if rule_id == "primary":
            derived_equality = ("regression",)
        elif rule_id == "auto-apply-proof":
            derived_equality = ("evidence_set", "regression")
        projection.update(
            {
                "doc_version": ("target", derived_equality),
                "ir_snapshot_id": (
                    "target",
                    ("candidate_config", *derived_equality),
                ),
                "constraint_snapshot_id": (
                    "constraint",
                    ("subject", "candidate_config", *derived_equality),
                ),
            }
        )
        if rule_id != "regression":
            env_equality = () if rule_id == "primary" else derived_equality
            projection["env_contract_version"] = (
                "candidate_config",
                ("candidate_config", *env_equality),
            )
        if rule_id == "auto-apply-proof":
            # The proof is not another broad validation report.  Its direct
            # lineage is the exact guard closure only: subject + target + final
            # EvidenceSet + every qualified oracle/outcome/regression evidence.
            # Constraint/config/review/playtest inputs remain transitively bound
            # through that EvidenceSet and must not become extra proof parents.
            parents[:] = [
                parent
                for parent in parents
                if parent.parent_role in {"subject", "target", "evidence_set", "regression"}
            ]
            proof_equality = ("evidence_set", "regression")
            projection = {
                "doc_version": ("target", proof_equality),
                "ir_snapshot_id": ("target", proof_equality),
                "constraint_snapshot_id": ("evidence_set", ("regression",)),
                # Individual regression suites may execute under distinct exact
                # environment contracts.  The proof binds the primary
                # EvidenceSet projection; it must not fabricate sibling equality.
                "env_contract_version": ("evidence_set", ()),
            }

    elif policy_id.startswith("constraint-validation-") or policy_id.startswith(
        "constraint-validated-"
    ):
        parents.extend(
            (
                _parent("proposal", source="run_input", kinds=("constraint_proposal",)),
                _parent(
                    "base_constraint",
                    source="run_input",
                    kinds=("constraint_snapshot",),
                    min_count=0,
                ),
            )
        )
        if rule_id != "candidate":
            parents.append(
                _parent(
                    "candidate",
                    source="prepared_rule",
                    source_rule_id="candidate",
                    kinds=("constraint_snapshot",),
                    min_count=0,
                )
            )
        if rule_id == "regression":
            parents.append(
                _parent(
                    "regression_suite",
                    source="child_payload_reference",
                    child_payload_pointer="/suite_artifact_id",
                    kinds=("regression_suite",),
                    min_count=1,
                    max_count=1,
                )
            )
        if rule_id == "primary":
            parents.extend(
                (
                    _parent(
                        "compile_evidence",
                        source="prepared_rule",
                        source_rule_id="compile-evidence",
                        kinds=("validation_evidence",),
                        schemas=("constraint-compile-evidence@1",),
                    ),
                    _parent(
                        "regression",
                        source="prepared_rule",
                        source_rule_id="regression",
                        kinds=("regression_evidence",),
                        min_count=0,
                        max_count=None,
                    ),
                )
            )
        has_candidate = policy_id != "constraint-validation-failed-without-candidate"
        if rule_id == "candidate":
            projection.update(
                {
                    "doc_version": ("proposal", ()),
                    "ir_snapshot_id": ("proposal", ("base_constraint",)),
                    "prompt_version": ("proposal", ()),
                    "model_snapshot": ("proposal", ()),
                    "agent_graph_version": ("proposal", ()),
                    "cassette_id": ("proposal", ()),
                }
            )
        else:
            target_role = "candidate" if has_candidate else "proposal"
            equality_roles: tuple[str, ...] = ()
            if rule_id == "primary":
                equality_roles = ("compile_evidence", "regression")
            projection.update(
                {
                    "doc_version": (target_role, equality_roles),
                    "ir_snapshot_id": (target_role, equality_roles),
                    "constraint_snapshot_id": (target_role, equality_roles),
                }
            )

    elif policy_id.startswith("rollback-validation-"):
        parents.append(_parent("subject", source="run_input", kinds=("rollback_request",)))
        parents.append(
            _parent(
                "target",
                source="child_payload_reference",
                child_payload_pointer=(
                    "/target_binding/target_artifact_id"
                    if rule_id == "primary"
                    else "/detail/target_artifact_id"
                ),
                kinds=_ALL_ARTIFACT_KINDS,
                min_count=1,
                max_count=1,
            )
        )
        parents.append(
            _parent(
                "current",
                source="child_payload_reference",
                child_payload_pointer=(
                    "/target_binding/expected_ref/artifact_id"
                    if rule_id == "primary"
                    else "/detail/current_artifact_id"
                ),
                kinds=_ALL_ARTIFACT_KINDS,
                min_count=1,
                max_count=1,
            )
        )
        if rule_id == "regression":
            parents.extend(
                (
                    _parent(
                        "regression_suite",
                        source="child_payload_reference",
                        child_payload_pointer="/lineage_suite_artifact_ids",
                        kinds=("regression_suite",),
                        min_count=0,
                        max_count=1,
                    ),
                )
            )
        if rule_id == "primary":
            parents.extend(
                (
                    _parent(
                        "regression",
                        source="prepared_rule",
                        source_rule_id="regression",
                        kinds=("regression_evidence",),
                        min_count=0,
                        max_count=None,
                    ),
                )
            )
        equality_roles = ("regression",) if rule_id == "primary" else ()
        for field in (
            "doc_version",
            "ir_snapshot_id",
            "constraint_snapshot_id",
        ):
            projection[field] = ("target", equality_roles)
        if rule_id == "primary":
            projection["env_contract_version"] = ("target", ())

    elif policy_id == "bench-completed":
        parents.extend(
            (
                _parent("dataset", source="run_input", kinds=("bench_dataset",)),
                _parent("benchmark_spec", source="run_input", kinds=("benchmark_spec",)),
                _parent(
                    "case_results",
                    source="run_input",
                    kinds=(
                        "checker_run",
                        "simulation_run",
                        "playtest_trace",
                        "review_report",
                        "run_result",
                        "validation_evidence",
                        "regression_evidence",
                    ),
                    min_count=0,
                    max_count=None,
                ),
                _rendered_parent(),
            )
        )
        projection.update(
            {
                "doc_version": ("dataset", ()),
                "ir_snapshot_id": ("dataset", ()),
                "constraint_snapshot_id": ("dataset", ()),
                "env_contract_version": ("dataset", ()),
            }
        )

    elif policy_id.startswith("artifact-migration-"):
        parents.append(
            _parent(
                "source",
                source="child_payload_reference",
                child_payload_pointer="/source_artifact_id",
                kinds=_ALL_ARTIFACT_KINDS,
            )
        )
        for field in _VERSION_FIELDS:
            if field != "tool_version":
                projection[field] = ("source", ())

    elif policy_id == "dr-drill-completed":
        parents.append(
            _parent(
                "recovery_manifest",
                source="run_input",
                kinds=("operational_evidence",),
                schemas=("backup-object-manifest@1",),
            )
        )
        for field in (
            "doc_version",
            "ir_snapshot_id",
            "constraint_snapshot_id",
            "env_contract_version",
        ):
            projection[field] = ("recovery_manifest", ())

    else:
        raise ValueError(f"lineage shape is not frozen for {policy_id}/{rule_id}")

    return tuple(parents), projection


def _producer_local_fields(
    *,
    policy_id: str,
    child_kind: ArtifactKind,
    inherited_fields: set[str],
) -> set[str]:
    """Return fields created by this producer rather than copied from parents."""

    fields = {"tool_version"}
    if child_kind == "ir_snapshot":
        fields.add("ir_snapshot_id")
    elif child_kind == "constraint_snapshot":
        fields.add("constraint_snapshot_id")

    if child_kind in {
        "simulation_run",
        "playtest_trace",
        "validation_evidence",
        "regression_evidence",
        "bench_report",
    }:
        fields.add("seed")

    current_run_has_llm_identity = (
        policy_id.startswith("generation-gate-")
        or policy_id.startswith("repair-")
        or policy_id
        in {
            "constraint-proposal-drafted",
            "review-completed",
            "playtest-completed",
            "bench-completed",
        }
    )
    if current_run_has_llm_identity:
        fields.update(
            {
                "prompt_version",
                "model_snapshot",
                "agent_graph_version",
                "cassette_id",
            }
        )

    if child_kind == "config_export" or (
        child_kind in {"simulation_run", "regression_evidence"}
        and "env_contract_version" not in inherited_fields
    ):
        fields.add("env_contract_version")
    return fields


class _OutcomeBuilder:
    def __init__(
        self,
        *,
        attempt_transition: VersionTransitionPolicyV1,
        run_transition: VersionTransitionPolicyV1,
    ) -> None:
        self.lineage_policies: dict[tuple[str, int], ArtifactLineagePolicyV1] = {}
        self._attempt_transition = _transition_ref(attempt_transition)
        self._run_transition = _transition_ref(run_transition)

    def artifact_rule(
        self,
        *,
        policy_id: str,
        rule_id: str,
        role: str,
        artifact_kind: ArtifactKind,
        payload_schema_ids: tuple[str, ...],
        min_count: int,
        max_count: int | None,
        count_binding: object | None = None,
    ) -> OutcomeArtifactRuleV1:
        parent_rules, inherited = _lineage_spec(
            policy_id=policy_id,
            rule_id=rule_id,
            child_kind=artifact_kind,
        )
        producer_rule = PRODUCER_RULES[artifact_kind]
        supported_producer_fields = set(producer_rule.required_fields) | set(
            producer_rule.projected_fields
        )
        producer_local_fields = (
            _producer_local_fields(
                policy_id=policy_id,
                child_kind=artifact_kind,
                inherited_fields=set(inherited),
            )
            & supported_producer_fields
        )
        lineage = ArtifactLineagePolicyV1(
            policy_schema_version="artifact-lineage-policy@1",
            policy_id=f"{policy_id}/{rule_id}-lineage",
            policy_version=1,
            child_kind=artifact_kind,
            child_payload_schema_ids=payload_schema_ids,
            parent_rules=parent_rules,
            version_projection=tuple(
                VersionFieldProjectionRuleV1(
                    field=field,
                    source=(
                        "parent_role"
                        if field in inherited
                        else "producer_value"
                        if field in producer_local_fields
                        else "constant_null"
                    ),
                    parent_role=inherited[field][0] if field in inherited else None,
                    equality_parent_roles=inherited[field][1] if field in inherited else (),
                )
                for field in _VERSION_FIELDS
            ),
        )
        identity = (lineage.policy_id, lineage.policy_version)
        retained = self.lineage_policies.setdefault(identity, lineage)
        if retained != lineage:
            raise ValueError(f"lineage policy {identity!r} has conflicting definitions")
        lineage_ref = ArtifactLineagePolicyRefV1(
            policy_id=lineage.policy_id,
            policy_version=lineage.policy_version,
            digest=artifact_lineage_policy_digest(lineage),
        )
        return OutcomeArtifactRuleV1(
            rule_id=rule_id,
            role=role,
            artifact_kind=artifact_kind,
            payload_schema_ids=payload_schema_ids,
            min_count=min_count,
            max_count=max_count,
            count_binding=count_binding,
            lineage_policy_ref=lineage_ref,
        )

    def policy(
        self,
        *,
        policy_id: str,
        outcome_code: str,
        workflow_effect_key: str,
        artifact_rules: Iterable[OutcomeArtifactRuleV1] = (),
        prepared_outcome: str = "success",
        publication_scope: str = "run",
        attempt_terminal_status: str | None = None,
        run_status_after_publication: str = "succeeded",
        failure_class: str | None = None,
        retry_disposition: str | None = None,
    ) -> OutcomeArtifactPolicyV1:
        transition = (
            self._attempt_transition if publication_scope == "attempt" else self._run_transition
        )
        return OutcomeArtifactPolicyV1(
            policy_schema_version="outcome-artifact-policy@1",
            policy_id=policy_id,
            policy_version=1,
            outcome_code=outcome_code,
            prepared_outcome=prepared_outcome,
            publication_scope=publication_scope,
            attempt_terminal_status=attempt_terminal_status,
            run_status_after_publication=run_status_after_publication,
            failure_class=failure_class,
            retry_disposition=retry_disposition,
            artifact_rules=tuple(artifact_rules),
            workflow_effect_key=workflow_effect_key,
            version_transition_policy_ref=transition,
        )


def _rule(
    builder: _OutcomeBuilder,
    policy_id: str,
    rule_id: str,
    role: str,
    kind: ArtifactKind,
    schema: str,
    *,
    min_count: int = 1,
    max_count: int | None = 1,
    binding: object | None = None,
) -> OutcomeArtifactRuleV1:
    return builder.artifact_rule(
        policy_id=policy_id,
        rule_id=rule_id,
        role=role,
        artifact_kind=kind,
        payload_schema_ids=(schema,),
        min_count=min_count,
        max_count=max_count,
        count_binding=binding,
    )


def _common_failure_policies(
    builder: _OutcomeBuilder,
    *,
    validation_workflow: bool,
) -> tuple[OutcomeArtifactPolicyV1, ...]:
    attempt_retry = (
        builder.policy(
            policy_id="dependency-unavailable-attempt-retry",
            outcome_code="dependency_unavailable",
            prepared_outcome="failure",
            publication_scope="attempt",
            attempt_terminal_status="failed",
            run_status_after_publication="retry_wait",
            failure_class="transient_dependency",
            retry_disposition="retry",
            workflow_effect_key="close_attempt_for_retry@1",
        ),
        builder.policy(
            policy_id="lease-expired-attempt-retry",
            outcome_code="lease_expired",
            prepared_outcome="failure",
            publication_scope="attempt",
            attempt_terminal_status="lease_expired",
            run_status_after_publication="retry_wait",
            failure_class="lease",
            retry_disposition="retry",
            workflow_effect_key="close_attempt_for_retry@1",
        ),
    )
    attempt_final_rows = (
        ("execution-failed-attempt-final", "execution_failed", "failed", "failed", "execution"),
        ("cancelled-attempt-final", "cancelled", "cancelled", "cancelled", "cancelled"),
        ("timed-out-attempt-final", "timed_out", "timed_out", "timed_out", "timeout"),
        (
            "subject-superseded-attempt-final",
            "subject_superseded",
            "cancelled",
            "cancelled",
            "subject_superseded",
        ),
        (
            "dependency-unavailable-attempt-final",
            "dependency_unavailable",
            "failed",
            "failed",
            "transient_dependency",
        ),
        (
            "permanent-dependency-attempt-final",
            "permanent_dependency_failed",
            "failed",
            "failed",
            "permanent_dependency",
        ),
        ("quota-exceeded-attempt-final", "quota_exceeded", "failed", "failed", "quota"),
        (
            "integrity-violation-attempt-final",
            "integrity_violation",
            "failed",
            "failed",
            "integrity",
        ),
        (
            "lease-expired-attempt-final-failed",
            "lease_expired",
            "lease_expired",
            "failed",
            "lease",
        ),
        (
            "lease-expired-attempt-final-timeout",
            "lease_expired",
            "lease_expired",
            "timed_out",
            "lease",
        ),
    )
    attempt_final = tuple(
        builder.policy(
            policy_id=policy_id,
            outcome_code=outcome_code,
            prepared_outcome="failure",
            publication_scope="attempt",
            attempt_terminal_status=attempt_status,
            run_status_after_publication=run_status,
            failure_class=failure_class,
            retry_disposition="terminal",
            workflow_effect_key="close_attempt_for_terminal@1",
        )
        for policy_id, outcome_code, attempt_status, run_status, failure_class in attempt_final_rows
    )
    terminal_effect = "restore_current_draft@1" if validation_workflow else "terminal_only@1"
    run_final_rows = (
        ("execution-failed", "execution_failed", "failed", "failed", "execution"),
        ("cancelled", "cancelled", "cancelled", "cancelled", "cancelled"),
        ("control-cancelled", "cancelled", None, "cancelled", "cancelled"),
        ("timed-out", "timed_out", "timed_out", "timed_out", "timeout"),
        ("queue-timed-out", "queue_timed_out", None, "timed_out", "timeout"),
        ("retry-wait-timed-out", "timed_out", None, "timed_out", "timeout"),
        (
            "subject-superseded",
            "subject_superseded",
            "cancelled",
            "cancelled",
            "subject_superseded",
        ),
        (
            "control-subject-superseded",
            "subject_superseded",
            None,
            "cancelled",
            "subject_superseded",
        ),
        ("lease-expired-final-failed", "lease_expired", "lease_expired", "failed", "lease"),
        (
            "lease-expired-final-timeout",
            "lease_expired",
            "lease_expired",
            "timed_out",
            "lease",
        ),
        (
            "dependency-unavailable",
            "dependency_unavailable",
            "failed",
            "failed",
            "transient_dependency",
        ),
        (
            "permanent-dependency-failed",
            "permanent_dependency_failed",
            "failed",
            "failed",
            "permanent_dependency",
        ),
        ("quota-exceeded", "quota_exceeded", "failed", "failed", "quota"),
        (
            "integrity-violation",
            "integrity_violation",
            "failed",
            "failed",
            "integrity",
        ),
    )
    run_final = tuple(
        builder.policy(
            policy_id=policy_id,
            outcome_code=outcome_code,
            prepared_outcome="failure",
            publication_scope="run",
            attempt_terminal_status=attempt_status,
            run_status_after_publication=run_status,
            failure_class=failure_class,
            retry_disposition="terminal",
            workflow_effect_key=terminal_effect,
        )
        for policy_id, outcome_code, attempt_status, run_status, failure_class in run_final_rows
    )
    return (*attempt_retry, *attempt_final, *run_final)


def _generation_policies(builder: _OutcomeBuilder) -> tuple[OutcomeArtifactPolicyV1, ...]:
    export_identity = ArtifactIdentityBindingV1(
        artifact_value_source="payload",
        artifact_payload_pointer="/export_profile",
    )

    def rules(policy_id: str, *, include_export: bool) -> tuple[OutcomeArtifactRuleV1, ...]:
        return (
            _rule(
                builder,
                policy_id,
                "primary",
                "primary" if include_export else "evidence",
                "patch",
                "patch@2",
            ),
            _rule(
                builder,
                policy_id,
                "preview",
                "output" if include_export else "evidence",
                "ir_snapshot",
                "ir-core@1",
            ),
            _rule(
                builder,
                policy_id,
                "config-export",
                "output",
                "config_export",
                "config-export-package@1",
                min_count=0,
                max_count=None if include_export else 0,
                binding=(
                    _json_count(
                        "/params/candidate_export_profiles",
                        identity=export_identity,
                    )
                    if include_export
                    else None
                ),
            ),
            _rule(
                builder,
                policy_id,
                "checker",
                "evidence",
                "checker_run",
                "checker-report@1",
                min_count=0,
                max_count=None,
                binding=_resolved("generation-gate", "checker"),
            ),
            _rule(
                builder,
                policy_id,
                "simulation",
                "evidence",
                "simulation_run",
                "simulation-result@1",
                min_count=0,
                max_count=None,
                binding=_resolved("generation-gate", "simulation"),
            ),
            _rule(
                builder,
                policy_id,
                "review",
                "evidence",
                "review_report",
                "review@1",
                min_count=0,
                max_count=None,
                binding=_resolved("generation-gate", "review"),
            ),
        )

    return (
        builder.policy(
            policy_id="generation-gate-pass",
            outcome_code="generation_gate_passed",
            artifact_rules=rules("generation-gate-pass", include_export=True),
            workflow_effect_key="create_patch_subject_head_and_draft@1",
        ),
        builder.policy(
            policy_id="generation-gate-rejected",
            outcome_code="generation_gate_rejected",
            artifact_rules=rules("generation-gate-rejected", include_export=False),
            prepared_outcome="failure",
            publication_scope="run",
            attempt_terminal_status="failed",
            run_status_after_publication="failed",
            failure_class="business_rule",
            retry_disposition="terminal",
            workflow_effect_key="no_workflow_subject@1",
        ),
        builder.policy(
            policy_id="generation-gate-rejected-attempt-final",
            outcome_code="generation_gate_rejected",
            prepared_outcome="failure",
            publication_scope="attempt",
            attempt_terminal_status="failed",
            run_status_after_publication="failed",
            failure_class="business_rule",
            retry_disposition="terminal",
            workflow_effect_key="close_attempt_for_terminal@1",
        ),
    )


def _repair_policies(builder: _OutcomeBuilder) -> tuple[OutcomeArtifactPolicyV1, ...]:
    verified_id = "repair-verified"
    export_identity = ArtifactIdentityBindingV1(
        artifact_value_source="payload",
        artifact_payload_pointer="/export_profile",
    )
    verified_rules = (
        _rule(builder, verified_id, "primary", "primary", "patch", "patch@2"),
        _rule(builder, verified_id, "preview", "output", "ir_snapshot", "ir-core@1"),
        _rule(
            builder,
            verified_id,
            "config-export",
            "output",
            "config_export",
            "config-export-package@1",
            min_count=0,
            max_count=None,
            binding=_json_count(
                "/params/candidate_export_profiles",
                identity=export_identity,
            ),
        ),
        _rule(
            builder,
            verified_id,
            "checker",
            "evidence",
            "checker_run",
            "checker-report@1",
            min_count=0,
            max_count=None,
            binding=_resolved("repair-verifier", "checker"),
        ),
        _rule(
            builder,
            verified_id,
            "simulation",
            "evidence",
            "simulation_run",
            "simulation-result@1",
            min_count=0,
            max_count=None,
            binding=_resolved("repair-verifier", "simulation"),
        ),
        _rule(
            builder,
            verified_id,
            "regression",
            "evidence",
            "regression_evidence",
            "regression-evidence@1",
            min_count=0,
            max_count=None,
            binding=_resolved("repair-verifier", "regression"),
        ),
    )
    reasons = (
        "execution_short_circuited",
        "prior_requirement_failed",
        "search_exhausted",
    )
    unverified_id = "repair-unverified"
    unverified_rules = tuple(
        _rule(
            builder,
            unverified_id,
            rule_id,
            "evidence",
            kind,
            schema,
            min_count=0,
            max_count=None,
            binding=_subset("repair-verifier", rule_id, reasons),
        )
        for rule_id, kind, schema in (
            ("checker", "checker_run", "checker-report@1"),
            ("simulation", "simulation_run", "simulation-result@1"),
            ("regression", "regression_evidence", "regression-evidence@1"),
        )
    )
    return (
        builder.policy(
            policy_id=verified_id,
            outcome_code="repair_verified",
            artifact_rules=verified_rules,
            workflow_effect_key="supersede_patch_head_create_draft@1",
        ),
        builder.policy(
            policy_id=unverified_id,
            outcome_code="repair_unverified",
            artifact_rules=unverified_rules,
            prepared_outcome="failure",
            attempt_terminal_status="failed",
            run_status_after_publication="failed",
            failure_class="validation",
            retry_disposition="terminal",
            workflow_effect_key="leave_patch_head_unchanged@1",
        ),
        builder.policy(
            policy_id="repair-unverified-attempt-final",
            outcome_code="repair_unverified",
            prepared_outcome="failure",
            publication_scope="attempt",
            attempt_terminal_status="failed",
            run_status_after_publication="failed",
            failure_class="validation",
            retry_disposition="terminal",
            workflow_effect_key="close_attempt_for_terminal@1",
        ),
    )


def _simple_primary_policy(
    builder: _OutcomeBuilder,
    *,
    policy_id: str,
    outcome_code: str,
    artifact_kind: ArtifactKind,
    payload_schema_id: str,
    workflow_effect_key: str = "no_workflow_change@1",
) -> OutcomeArtifactPolicyV1:
    return builder.policy(
        policy_id=policy_id,
        outcome_code=outcome_code,
        artifact_rules=(
            _rule(
                builder,
                policy_id,
                "primary",
                "primary",
                artifact_kind,
                payload_schema_id,
            ),
        ),
        workflow_effect_key=workflow_effect_key,
    )


def _review_policy(builder: _OutcomeBuilder) -> OutcomeArtifactPolicyV1:
    policy_id = "review-completed"
    profile_identity = ArtifactIdentityBindingV1(
        collection_item_pointer="",
        artifact_value_source="payload",
        artifact_payload_pointer="/profile",
    )
    return builder.policy(
        policy_id=policy_id,
        outcome_code="review_completed",
        artifact_rules=(
            _rule(builder, policy_id, "primary", "primary", "review_report", "review@1"),
            _rule(
                builder,
                policy_id,
                "checker",
                "output",
                "checker_run",
                "checker-report@1",
                min_count=0,
                max_count=None,
                binding=_json_count(
                    "/params/checker_profiles",
                    identity=profile_identity,
                ),
            ),
            _rule(
                builder,
                policy_id,
                "simulation",
                "output",
                "simulation_run",
                "simulation-result@1",
                min_count=0,
                max_count=None,
                binding=_json_count(
                    "/params/simulation_profiles",
                    identity=profile_identity,
                ),
            ),
        ),
        workflow_effect_key="no_workflow_change@1",
    )


def _task_suite_policy(builder: _OutcomeBuilder) -> OutcomeArtifactPolicyV1:
    policy_id = "task-suite-derived"
    return builder.policy(
        policy_id=policy_id,
        outcome_code="task_suite_derived",
        artifact_rules=(
            _rule(builder, policy_id, "primary", "primary", "task_suite", "task-suite@1"),
            _rule(
                builder,
                policy_id,
                "scenario",
                "output",
                "scenario_spec",
                "scenario-spec@1",
                min_count=1,
                max_count=None,
                binding=_json_count(
                    "/episodes",
                    source="prepared_primary_payload",
                    identity=ArtifactIdentityBindingV1(
                        collection_item_pointer="/scenario_spec_artifact_id",
                        artifact_value_source="artifact_id",
                    ),
                ),
            ),
        ),
        workflow_effect_key="no_workflow_change@1",
    )


def _validation_primary(
    builder: _OutcomeBuilder,
    *,
    policy_id: str,
) -> OutcomeArtifactRuleV1:
    return _rule(
        builder,
        policy_id,
        "primary",
        "primary",
        "validation_evidence",
        "evidence-set@1",
    )


def _validation_regression(
    builder: _OutcomeBuilder,
    *,
    policy_id: str,
    resolved_policy_id: str,
    binding: object | None = None,
) -> OutcomeArtifactRuleV1:
    return _rule(
        builder,
        policy_id,
        "regression",
        "output",
        "regression_evidence",
        "regression-evidence@1",
        min_count=0,
        max_count=None,
        binding=binding or _resolved(resolved_policy_id, "regression"),
    )


def _patch_validation_policies(
    builder: _OutcomeBuilder,
) -> tuple[OutcomeArtifactPolicyV1, ...]:
    policies: list[OutcomeArtifactPolicyV1] = []
    rows = (
        ("patch-validation-passed", "patch_validation_passed", "set_patch_validated@1"),
        (
            "patch-validation-auto-eligible",
            "patch_validation_auto_eligible",
            "set_patch_validated_with_auto_proof@1",
        ),
        ("patch-validation-failed", "patch_validation_failed", "set_patch_validation_failed@1"),
        (
            "patch-validation-unproven",
            "patch_validation_unproven",
            "set_patch_validation_failed@1",
        ),
    )
    for policy_id, code, effect in rows:
        rules: list[OutcomeArtifactRuleV1] = [
            _validation_primary(builder, policy_id=policy_id),
            _validation_regression(
                builder,
                policy_id=policy_id,
                resolved_policy_id="patch-validation",
            ),
        ]
        if policy_id == "patch-validation-auto-eligible":
            rules.append(
                _rule(
                    builder,
                    policy_id,
                    "auto-apply-proof",
                    "evidence",
                    "validation_evidence",
                    "auto-apply-proof@1",
                )
            )
        policies.append(
            builder.policy(
                policy_id=policy_id,
                outcome_code=code,
                artifact_rules=rules,
                workflow_effect_key=effect,
            )
        )
    return tuple(policies)


def _constraint_validation_policies(
    builder: _OutcomeBuilder,
) -> tuple[OutcomeArtifactPolicyV1, ...]:
    policies: list[OutcomeArtifactPolicyV1] = []
    rows = (
        (
            "constraint-validated-with-candidate",
            "constraint_validated",
            "set_exact_binding_and_validated@1",
            True,
            _resolved("constraint-validation", "regression"),
        ),
        (
            "constraint-validation-failed-with-candidate",
            "constraint_validation_failed_with_candidate",
            "set_exact_binding_and_validation_failed@1",
            True,
            _subset(
                "constraint-validation",
                "regression",
                ("execution_short_circuited", "prior_requirement_failed"),
            ),
        ),
        (
            "constraint-validation-failed-without-candidate",
            "constraint_validation_failed_without_candidate",
            "leave_binding_null_and_validation_failed@1",
            False,
            _subset(
                "constraint-validation",
                "regression",
                ("candidate_unavailable", "compile_failed"),
            ),
        ),
    )
    for policy_id, code, effect, has_candidate, regression_binding in rows:
        rules = (
            _validation_primary(builder, policy_id=policy_id),
            _rule(
                builder,
                policy_id,
                "candidate",
                "output",
                "constraint_snapshot",
                "constraint-snapshot@1",
                min_count=1 if has_candidate else 0,
                max_count=1 if has_candidate else 0,
            ),
            _rule(
                builder,
                policy_id,
                "compile-evidence",
                "evidence",
                "validation_evidence",
                "constraint-compile-evidence@1",
            ),
            _validation_regression(
                builder,
                policy_id=policy_id,
                resolved_policy_id="constraint-validation",
                binding=regression_binding,
            ),
        )
        policies.append(
            builder.policy(
                policy_id=policy_id,
                outcome_code=code,
                artifact_rules=rules,
                workflow_effect_key=effect,
            )
        )
    return tuple(policies)


def _rollback_validation_policies(
    builder: _OutcomeBuilder,
) -> tuple[OutcomeArtifactPolicyV1, ...]:
    rows = (
        ("rollback-validation-passed", "rollback_validation_passed", "set_rollback_validated@1"),
        (
            "rollback-validation-failed",
            "rollback_validation_failed",
            "set_rollback_validation_failed@1",
        ),
        (
            "rollback-validation-unproven",
            "rollback_validation_unproven",
            "set_rollback_validation_failed@1",
        ),
    )
    return tuple(
        builder.policy(
            policy_id=policy_id,
            outcome_code=code,
            artifact_rules=(
                _validation_primary(builder, policy_id=policy_id),
                _validation_regression(
                    builder,
                    policy_id=policy_id,
                    resolved_policy_id="rollback-validation",
                ),
            ),
            workflow_effect_key=effect,
        )
        for policy_id, code, effect in rows
    )


def _migration_policies(builder: _OutcomeBuilder) -> tuple[OutcomeArtifactPolicyV1, ...]:
    rows = (
        ("artifact-migration-reported", "artifact_migration_reported"),
        ("artifact-migration-compatible", "artifact_migration_compatible"),
        ("artifact-migration-needs-action", "artifact_migration_needs_action"),
    )
    return tuple(
        _simple_primary_policy(
            builder,
            policy_id=policy_id,
            outcome_code=code,
            artifact_kind="migration_report",
            payload_schema_id="migration-report@1",
        )
        for policy_id, code in rows
    )


def _business_policies(
    builder: _OutcomeBuilder,
    kind: RunKindIdentity,
) -> tuple[OutcomeArtifactPolicyV1, ...]:
    name = kind[0]
    if name == "generation.propose":
        return _generation_policies(builder)
    if name == "patch.repair":
        return _repair_policies(builder)
    if name == "constraint_proposal.propose":
        return (
            _simple_primary_policy(
                builder,
                policy_id="constraint-proposal-drafted",
                outcome_code="constraint_proposal_drafted",
                artifact_kind="constraint_proposal",
                payload_schema_id="constraint-proposal@1",
                workflow_effect_key="create_constraint_subject_head_and_draft@1",
            ),
        )
    if name == "review.run":
        return (_review_policy(builder),)
    if name == "checker.run":
        return (
            _simple_primary_policy(
                builder,
                policy_id="checker-completed",
                outcome_code="checker_completed",
                artifact_kind="checker_run",
                payload_schema_id="checker-report@1",
            ),
        )
    if name == "simulation.run":
        return (
            _simple_primary_policy(
                builder,
                policy_id="simulation-completed",
                outcome_code="simulation_completed",
                artifact_kind="simulation_run",
                payload_schema_id="simulation-result@1",
            ),
        )
    if name == "task_suite.derive":
        return (_task_suite_policy(builder),)
    if name == "playtest.run":
        return (
            _simple_primary_policy(
                builder,
                policy_id="playtest-completed",
                outcome_code="playtest_completed",
                artifact_kind="playtest_trace",
                payload_schema_id="playtest-trace@1",
            ),
        )
    if name == "patch.validate":
        return _patch_validation_policies(builder)
    if name == "constraint_proposal.validate":
        return _constraint_validation_policies(builder)
    if name == "rollback.validate":
        return _rollback_validation_policies(builder)
    if name == "bench.run":
        return (
            _simple_primary_policy(
                builder,
                policy_id="bench-completed",
                outcome_code="bench_completed",
                artifact_kind="bench_report",
                payload_schema_id="bench-report@2",
            ),
        )
    if name == "artifact.migrate":
        return _migration_policies(builder)
    if name == "dr.drill":
        return (
            _simple_primary_policy(
                builder,
                policy_id="dr-drill-completed",
                outcome_code="dr_drill_completed",
                artifact_kind="operational_evidence",
                payload_schema_id="dr-drill-evidence@1",
            ),
        )
    raise ValueError(f"unregistered frozen Run kind: {kind!r}")


def _outcome_policies(
    builder: _OutcomeBuilder,
    kind: RunKindIdentity,
) -> tuple[OutcomeArtifactPolicyV1, ...]:
    validation = kind[0] in {
        "patch.validate",
        "constraint_proposal.validate",
        "rollback.validate",
    }
    return (
        *_business_policies(builder, kind),
        *_common_failure_policies(builder, validation_workflow=validation),
    )


def _run_event_registry() -> RunEventRegistryV1:
    definitions = frozen_run_event_definitions_v1()
    payload = {"registry_version": 1, "definitions": definitions}
    return RunEventRegistryV1(
        **payload,
        registry_digest=run_event_registry_digest(payload),
    )


def _completion_oracle_registry() -> CompletionOracleRegistryV1:
    definitions = (
        CompletionOracleDefinitionV1(
            oracle_id="state-predicate",
            version=1,
            params_schema_id="state-predicate-params@1",
            result_schema_id="completion-oracle-result@1",
            executor_key="state_predicate_oracle@1",
        ),
        CompletionOracleDefinitionV1(
            oracle_id="bounded-progress",
            version=1,
            params_schema_id="bounded-progress-params@1",
            result_schema_id="completion-oracle-result@1",
            executor_key="bounded_progress_oracle@1",
        ),
    )
    payload = {"registry_version": 1, "definitions": definitions}
    return CompletionOracleRegistryV1(
        **payload,
        registry_digest=compute_completion_oracle_registry_digest(payload),
    )


def _playtest_payload_schema_registry() -> PlaytestPayloadSchemaRegistryV1:
    definitions = (
        PlaytestPayloadSchemaDefinitionV1(
            schema_id="generic-env-reset@1",
            purpose="scenario_reset",
            validator_key="generic_env_reset_payload@1",
        ),
        PlaytestPayloadSchemaDefinitionV1(
            schema_id="state-predicate-params@1",
            purpose="completion_oracle_params",
            validator_key="state_predicate_params@1",
        ),
        PlaytestPayloadSchemaDefinitionV1(
            schema_id="bounded-progress-params@1",
            purpose="completion_oracle_params",
            validator_key="bounded_progress_params@1",
        ),
    )
    payload = {"registry_version": 1, "definitions": definitions}
    return PlaytestPayloadSchemaRegistryV1(
        **payload,
        registry_digest=compute_playtest_payload_schema_registry_digest(payload),
    )


def _migration_registry() -> MigrationCapabilityMatrixRegistryV1:
    matrix_payload = {
        "matrix_version": 1,
        "kind_defaults": tuple(
            MigrationKindDefaultV1(
                source_kind=kind,
                unsupported_edge_action="reject_409",
            )
            for kind in _ALL_ARTIFACT_KINDS
        ),
        # Publication is deliberately closed until an exact, reviewed edge and
        # kind-specific lineage policy are registered.  Enum membership alone
        # never fabricates a publish_same_kind capability.
        "edges": (),
    }
    matrix = MigrationCapabilityMatrixV1(
        **matrix_payload,
        matrix_digest=migration_capability_matrix_digest(matrix_payload),
    )
    registry_payload = {"matrices": (matrix,)}
    return MigrationCapabilityMatrixRegistryV1(
        **registry_payload,
        registry_digest=migration_capability_registry_digest(registry_payload),
    )


def _profile_compatibility(
    *,
    historical_v1_only: bool = False,
) -> dict[ExecutionProfileKindV1, tuple[RunKindRef, ...]]:
    by_kind: dict[str, set[RunKindIdentity]] = {}
    for run_kind, requirements in FROZEN_PROFILE_REQUIREMENT_SHAPES.items():
        if historical_v1_only and run_kind[1] != 1:
            continue
        for _field_path, profile_kind, _cardinality in requirements:
            by_kind.setdefault(profile_kind, set()).add(run_kind)
    return {
        profile_kind: tuple(_ref(run_kind) for run_kind in sorted(run_kinds))
        for profile_kind, run_kinds in sorted(by_kind.items())
    }


def _resolved_policy_profile_config(
    profile_kind: ExecutionProfileKindV1,
    *,
    profile_version: int = 1,
) -> dict[str, Any]:
    if profile_kind == "checker":
        return CheckerProfileConfigV1(
            allowed_checker_ids=("asp", "graph", "smt"),
            allowed_defect_classes=(
                "cyclic_dependency",
                "dangling_reference",
                "dead_quest",
                "gacha_expectation_violation",
                "interval_violation",
                "isolated_node",
                "missing_drop_source",
                "non_monotonic_curve",
                "prob_sum_ne_1",
                "reward_out_of_range",
                "unreachable_target",
                "unsatisfiable_completion",
            ),
            max_direct_checker_count=3,
            max_constraint_count=256,
            max_work_units=2_000_000,
        ).model_dump(mode="json")
    if profile_kind == "simulation":
        return SimulationProfileConfigV1(
            default_population=16,
            default_horizon_steps=64,
            max_horizon_steps=100_000,
            max_output_ticks=100_000,
            max_work_units=2_000_000,
        ).model_dump(mode="json")
    if profile_kind == "workload":
        return WorkloadProfileConfigV1(
            max_replication_count=10_000,
            max_total_replication_ticks=2_000_000,
            max_total_work_units=10_000_000,
        ).model_dump(mode="json")
    if profile_kind == "review":
        return ReviewProfileConfigV1(
            max_prompt_message_bytes=16 * 1024 * 1024,
            max_checker_profile_count=64,
            max_simulation_profile_count=64,
            max_total_checker_work_units=2_000_000,
            max_total_simulation_work_units=2_000_000,
            max_total_prepared_artifact_bytes=128 * 1024 * 1024,
        ).model_dump(mode="json")
    if profile_kind == "constraint_extraction":
        return ConstraintExtractionProfileConfigV1(
            max_prompt_message_bytes=17 * 1024 * 1024,
            max_source_artifact_count=64,
            max_source_artifact_bytes=4 * 1024 * 1024,
            max_total_input_bytes=16 * 1024 * 1024,
            max_proposal_count=256,
            max_output_bytes=8 * 1024 * 1024,
        ).model_dump(mode="json")
    if profile_kind == "generation":
        config = GenerationProfileConfigV1(
            max_prompt_message_bytes=16 * 1024 * 1024,
            max_checker_constraint_count=256,
            max_checker_work_units=2_000_000,
            gate_simulation_seed=0,
            gate_simulation_population=30,
            gate_simulation_horizon_steps=120,
            max_simulation_work_units=2_000_000,
            max_candidate_export_profiles=16,
            max_total_prepared_artifact_bytes=128 * 1024 * 1024,
            resolved_policy=ResolvedPolicyProfileConfigV1(
                resolved_policy_id="generation-gate",
                requirement_sources=(
                    FixedResolvedPolicyRequirementConfigV1(
                        outcome_rule_id="checker",
                        requirement_id="generation-gate:checker",
                        artifact_kind="checker_run",
                        payload_schema_id="checker-report@1",
                        producer_profile_field_path="/params/generation_policy",
                        ordinal=1,
                    ),
                    FixedResolvedPolicyRequirementConfigV1(
                        outcome_rule_id="simulation",
                        requirement_id="generation-gate:simulation",
                        artifact_kind="simulation_run",
                        payload_schema_id="simulation-result@1",
                        producer_profile_field_path="/params/generation_policy",
                        ordinal=1,
                    ),
                    FixedResolvedPolicyRequirementConfigV1(
                        outcome_rule_id="review",
                        requirement_id="generation-gate:review",
                        artifact_kind="review_report",
                        payload_schema_id="review@1",
                        producer_profile_field_path="/params/generation_policy",
                        ordinal=1,
                    ),
                ),
            ),
        )
        return config.model_dump(mode="json")
    if profile_kind == "patch_repair":
        config = PatchRepairProfileConfigV1(
            max_prompt_message_bytes=16 * 1024 * 1024,
            max_search_steps=4,
            max_total_checker_work_units=2_000_000,
            max_total_simulation_work_units=2_000_000,
            max_checker_profile_count=64,
            max_simulation_profile_count=64,
            max_regression_suite_count=64,
            max_total_regression_work_units=10_000_000,
            max_regression_suite_bytes=17 * 1024 * 1024,
            max_total_regression_suite_bytes=64 * 1024 * 1024,
            max_candidate_export_profiles=16,
            max_total_prepared_artifact_bytes=128 * 1024 * 1024,
            resolved_policy=ResolvedPolicyProfileConfigV1(
                resolved_policy_id="repair-verifier",
                requirement_sources=(
                    ProfileCollectionResolvedPolicyRequirementConfigV1(
                        outcome_rule_id="checker",
                        artifact_kind="checker_run",
                        payload_schema_id="checker-report@1",
                        collection_field_path="/params/checker_profiles",
                    ),
                    ProfileCollectionResolvedPolicyRequirementConfigV1(
                        outcome_rule_id="simulation",
                        artifact_kind="simulation_run",
                        payload_schema_id="simulation-result@1",
                        collection_field_path="/params/simulation_profiles",
                    ),
                    ArtifactCollectionResolvedPolicyRequirementConfigV1(
                        outcome_rule_id="regression",
                        artifact_kind="regression_evidence",
                        payload_schema_id="regression-evidence@1",
                        collection_field_path="/params/regression_suite_artifact_ids",
                    ),
                ),
            ),
        )
        return config.model_dump(mode="json")
    if profile_kind == "playtest_planner":
        if profile_version == 1:
            return PlaytestPlannerProfileConfigV1(memory_mode="off").model_dump(mode="json")
        if profile_version != 2:
            raise ValueError("unsupported built-in playtest planner profile version")
        return PlaytestPlannerProfileConfigV2(
            memory_mode="off",
            max_episode_count=1024,
            max_steps_per_episode=1024,
            max_total_steps=1024,
            max_total_model_calls=3072,
            max_total_trace_bytes=256 * 1024 * 1024,
        ).model_dump(mode="json")
    if profile_kind == "task_suite_derivation":
        if profile_version == 1:
            return TaskSuiteDerivationProfileConfigV1().model_dump(mode="json")
        if profile_version != 2:
            raise ValueError("unsupported built-in task-suite profile version")
        oracle_registry = _completion_oracle_registry()
        return TaskSuiteDerivationProfileConfigV2(
            target_environment_profile=ProfileRefV1(profile_id="builtin.environment", version=1),
            completion_oracle_registry_version=oracle_registry.registry_version,
            completion_oracle_registry_digest=oracle_registry.registry_digest,
            max_scenarios=1024,
            max_total_prepared_artifact_bytes=256 * 1024 * 1024,
        ).model_dump(mode="json")
    if profile_kind == "bench_evaluator":
        return BenchmarkEvaluatorProfileConfigV1(
            policy=build_builtin_benchmark_evaluator_policy()
        ).model_dump(mode="json")
    return {}


def _execution_profile_definition(
    *,
    profile_kind: ExecutionProfileKindV1,
    run_kinds: tuple[RunKindRef, ...],
    profile_version: int,
) -> ExecutionProfileDefinitionV1:
    """Materialize one immutable built-in profile definition."""

    environment_ref = ProfileRefV1(profile_id="builtin.environment", version=1)
    profile = ProfileRefV1(
        profile_id=f"builtin.{profile_kind}",
        version=profile_version,
    )
    if profile_kind == "environment":
        details = EnvironmentProfileDetailsV1(
            contract=EnvironmentContractDescriptorV1(
                env_contract_version="generic-agent-env@1",
                reset_schema_id="generic-env-reset@1",
                action_schema_id="generic-env-action@1",
                observation_schema_id="generic-env-observation@1",
                max_navigation_grid_cells=65_536,
            )
        )
    elif profile_kind == "config_export":
        details = ConfigExportProfileDetailsV1(
            target_environment_profile=environment_ref,
            env_contract_version="generic-agent-env@1",
            format_schema_id="config-export-files@1",
        )
    elif profile_kind == "validation":
        details = ValidationProfileDetailsV1(
            subject_kinds=("patch", "constraint_proposal", "rollback_request"),
        )
    elif profile_kind == "artifact_migrator":
        details = MigrationProfileDetailsV1(edges=())
    else:
        details = GenericProfileDetailsV1()
    config = _resolved_policy_profile_config(
        profile_kind,
        profile_version=profile_version,
    )
    return ExecutionProfileDefinitionV1(
        profile=profile,
        profile_kind=profile_kind,
        compatible_run_kinds=run_kinds,
        domain_scope=DomainScope(domain_ids=("builtin",)),
        stochastic=profile_kind == "simulation",
        input_schema_ids=tuple(
            FROZEN_RUN_KIND_SHAPES[(item.kind, item.version)].payload_schema_id
            for item in run_kinds
        ),
        output_schema_ids=PROFILE_OUTPUT_SCHEMA_REQUIREMENTS[profile_kind],
        required_capabilities=(),
        display_name=f"Built-in {profile_kind.replace('_', ' ')} profile",
        handler_key=f"builtin_{profile_kind}_profile@{profile_version if profile_kind in {'playtest_planner', 'task_suite_derivation'} else 1}",
        config_schema_id=f"{profile_kind}-profile-config@{profile_version if profile_kind in {'playtest_planner', 'task_suite_derivation'} else 1}",
        config=config,
        config_hash=canonical_config_hash(config),
        details=details,
    )


def _execution_profile_catalog_v1() -> ExecutionProfileCatalogSnapshotV1:
    """Return the byte-identical Task 11 catalog retained for audit/history."""

    compatibility = _profile_compatibility(historical_v1_only=True)
    definitions = tuple(
        _execution_profile_definition(
            profile_kind=profile_kind,
            run_kinds=run_kinds,
            profile_version=1,
        )
        for profile_kind, run_kinds in compatibility.items()
    )
    lifecycle = tuple(
        ExecutionProfileLifecycleV1(
            profile=definition.profile,
            state="active",
            revision=1,
            changed_at=_PROFILE_CHANGED_AT,
        )
        for definition in definitions
    )
    payload = {
        "catalog_version": 1,
        "definitions": definitions,
        "lifecycle": lifecycle,
    }
    return ExecutionProfileCatalogSnapshotV1(
        **payload,
        catalog_digest=execution_profile_catalog_digest(payload),
    )


def _execution_profile_catalog_v2() -> ExecutionProfileCatalogSnapshotV1:
    """Add bounded Task 12 profiles without redefining either historical @1."""

    previous = _execution_profile_catalog_v1()
    compatibility = _profile_compatibility()
    upgraded_kinds: tuple[ExecutionProfileKindV1, ...] = (
        "playtest_planner",
        "task_suite_derivation",
    )
    disabled_v1_kinds: tuple[ExecutionProfileKindV1, ...] = (
        "playtest_planner",
        "task_suite_derivation",
    )
    definitions = (
        *previous.definitions,
        *(
            _execution_profile_definition(
                profile_kind=profile_kind,
                run_kinds=compatibility[profile_kind],
                profile_version=2,
            )
            for profile_kind in upgraded_kinds
        ),
    )
    previous_by_ref = {item.profile: item for item in previous.lifecycle}
    lifecycle = tuple(
        ExecutionProfileLifecycleV1(
            profile=definition.profile,
            state=(
                "disabled"
                if definition.profile_kind in disabled_v1_kinds and definition.profile.version == 1
                else "active"
            ),
            revision=(
                2
                if definition.profile_kind in disabled_v1_kinds and definition.profile.version == 1
                else previous_by_ref.get(
                    definition.profile,
                    ExecutionProfileLifecycleV1(
                        profile=definition.profile,
                        state="active",
                        revision=1,
                        changed_at="2026-07-16T00:00:00Z",
                    ),
                ).revision
            ),
            reason_code=(
                "missing_exact_task12_resource_authority"
                if definition.profile_kind in disabled_v1_kinds and definition.profile.version == 1
                else None
            ),
            changed_at=(
                "2026-07-16T00:00:00Z"
                if definition.profile_kind in disabled_v1_kinds
                and definition.profile.version == 1
                or definition.profile.version == 2
                else previous_by_ref[definition.profile].changed_at
            ),
        )
        for definition in definitions
    )
    payload = {
        "catalog_version": 2,
        "definitions": definitions,
        "lifecycle": lifecycle,
    }
    return ExecutionProfileCatalogSnapshotV1(
        **payload,
        catalog_digest=execution_profile_catalog_digest(payload),
    )


def _profile_requirements() -> dict[RunKindIdentity, tuple[ProfileRequirement, ...]]:
    return {
        run_kind: tuple(
            ProfileRequirement(
                field_path=field_path,
                expected_profile_kind=profile_kind,
                cardinality=cardinality,
            )
            for field_path, profile_kind, cardinality in requirements
        )
        for run_kind, requirements in FROZEN_PROFILE_REQUIREMENT_SHAPES.items()
    }


def _agent_execution_graphs() -> tuple[AgentExecutionGraphV1, ...]:
    """Freeze the exact Agent nodes implemented by each LLM-capable executor.

    Model allowlists remain Run-specific in ``ExecutionVersionPlanV1``.  This
    retained authority owns the graph/node/prompt/tool half of that plan so clients
    cannot queue a Run whose executor can never issue the declared calls.
    """

    shapes: tuple[
        tuple[
            str,
            RunKindIdentity,
            str,
            AgentExecutionProfileSelectorV1 | None,
            tuple[tuple[str, str, str], ...],
        ],
        ...,
    ] = (
        (
            "generation-graph@1",
            ("generation.propose", 1),
            "generation_proposer@1",
            None,
            (("generation", "generation@1", "generation@1"),),
        ),
        (
            "repair-graph@1",
            ("patch.repair", 1),
            "repair_search@1",
            None,
            (("repair", "repair@4", "repair@1"),),
        ),
        (
            "constraint-proposal-graph@1",
            ("constraint_proposal.propose", 1),
            "constraint_proposer@1",
            None,
            (("extraction", "extraction@1", "extraction@1"),),
        ),
        (
            "review-triage-graph@1",
            ("review.run", 1),
            "review_runner@1",
            None,
            (("review-triage", "review-triage@1", "review-triage@1"),),
        ),
        (
            "playtest-core-graph@1",
            ("playtest.run", 1),
            "playtest_runner@1",
            AgentExecutionProfileSelectorV1(
                profile_field_path="/params/planner_policy",
                config_pointer="/memory_mode",
                expected_value="off",
            ),
            (
                ("playtest.planner", "playtest@1", "playtest@1"),
                ("playtest.executor", "playtest@2", "playtest@1"),
                ("playtest.reflect", "playtest@1", "playtest@1"),
            ),
        ),
        (
            "playtest-memory-graph@1",
            ("playtest.run", 1),
            "playtest_runner@1",
            AgentExecutionProfileSelectorV1(
                profile_field_path="/params/planner_policy",
                config_pointer="/memory_mode",
                expected_value="llm_compaction",
            ),
            (
                ("playtest.planner", "playtest@1", "playtest@1"),
                ("playtest.executor", "playtest@2", "playtest@1"),
                ("playtest.reflect", "playtest@1", "playtest@1"),
                (
                    "playtest.memory",
                    "playtest.memory.compact@1",
                    "playtest@1",
                ),
            ),
        ),
        (
            "bench-agent-graph@1",
            ("bench.run", 1),
            "bench_runner@1",
            None,
            (("bench-agent-case", "bench-agent@1", "bench@1"),),
        ),
    )
    graphs: list[AgentExecutionGraphV1] = []
    for graph_version, run_kind, executor_key, profile_selector, node_shapes in shapes:
        nodes = tuple(
            AgentExecutionNodeV1(
                agent_node_id=node_id,
                prompt_version=prompt_version,
                tool_version=tool_version,
                required_capabilities=("reasoning",),
            )
            for node_id, prompt_version, tool_version in node_shapes
        )
        body = {
            "agent_graph_version": graph_version,
            "run_kind": RunKindRef(kind=run_kind[0], version=run_kind[1]),
            "executor_key": executor_key,
            "status": "active",
            "profile_selector": profile_selector,
            "nodes": nodes,
        }
        graphs.append(
            AgentExecutionGraphV1(
                **body,
                graph_digest=agent_execution_graph_digest(body),
            )
        )
    return tuple(graphs)


def _run_definitions(
    *,
    builder: _OutcomeBuilder,
    retry_policies: tuple[RetryPolicySnapshot, ...],
    classifier: FailureClassifierV1,
    runtime_parent_rules: RuntimeParentRuleSetV1,
    finding_policies: tuple[FindingOutputPolicyV1, ...],
    migration_registry: MigrationCapabilityMatrixRegistryV1,
) -> tuple[RunKindDefinition, ...]:
    retry_by_id = {item.retry_policy_id: item for item in retry_policies}
    finding_by_id = {item.policy_id: item for item in finding_policies}
    classifier_ref = FailureClassifierRefV1(
        classifier_version=classifier.classifier_version,
        classifier_digest=classifier.classifier_digest,
    )
    runtime_ref = _runtime_parent_ref(runtime_parent_rules)
    matrix = migration_registry.matrices[0]
    matrix_ref = MigrationCapabilityMatrixRefV1(
        matrix_version=matrix.matrix_version,
        matrix_digest=matrix.matrix_digest,
    )
    definitions: list[RunKindDefinition] = []
    for identity, shape in sorted(FROZEN_RUN_KIND_SHAPES.items()):
        retry = retry_by_id[shape.retry_policy_id]
        finding = (
            finding_by_id[shape.finding_policy_id] if shape.finding_policy_id is not None else None
        )
        validation = identity[0] in {
            "patch.validate",
            "constraint_proposal.validate",
            "rollback.validate",
        }
        non_success_hook = "publish_validation_non_success@1" if validation else None
        definitions.append(
            RunKindDefinition(
                kind=identity[0],
                version=identity[1],
                status="active",
                payload_schema_id=shape.payload_schema_id,
                prepared_result_schema_id="prepared-run-result@1",
                prepared_failure_schema_id="prepared-run-failure@1",
                result_schema_id="run-result@1",
                failure_schema_id="run-failure@1",
                outcome_policies=_outcome_policies(builder, identity),
                runtime_parent_rule_set=runtime_ref,
                finding_output_policy_ref=(_finding_ref(finding) if finding is not None else None),
                allowed_command_schema_ids=shape.command_schema_ids,
                creation_mode=shape.creation_mode,
                allowed_llm_execution_modes=shape.llm_modes,
                seed_policy=shape.seed_policy,
                seed_derivation_version=shape.seed_derivation_version,
                required_permission=Permission(
                    action=shape.permission_action,
                    resource_kind=shape.permission_resource_kind,
                    domain_scope="all" if shape.dynamic_domain_permission else None,
                ),
                executor_key=shape.executor_key,
                terminal_hooks=TerminalPublisherHooks(
                    on_success=shape.success_hook,
                    on_failure=non_success_hook or "publish_run_failure@1",
                    on_cancel=non_success_hook or "publish_run_cancel@1",
                    on_timeout=non_success_hook or "publish_run_timeout@1",
                ),
                failure_classifier=classifier_ref,
                retry_policy=RetryPolicyRefV1(
                    retry_policy_id=retry.retry_policy_id,
                    retry_policy_version=retry.retry_policy_version,
                    retry_policy_digest=retry.retry_policy_digest,
                ),
                migration_capability_matrix=(
                    matrix_ref if shape.migration_matrix_required else None
                ),
            )
        )
    return tuple(definitions)


def build_builtin_registry() -> ImmutablePlatformRegistry:
    """Materialize the exact built-in M4c registry without a mutable current alias."""

    classifier = _failure_classifier()
    retry_policies = _retry_policies()
    attempt_transition = _transition_policy(scope="attempt")
    run_transition = _transition_policy(scope="run")
    builder = _OutcomeBuilder(
        attempt_transition=attempt_transition,
        run_transition=run_transition,
    )
    runtime_parent_rules = _runtime_parent_rules()
    finding_policies = _finding_policies()
    migration_registry = _migration_registry()
    run_kinds = _run_definitions(
        builder=builder,
        retry_policies=retry_policies,
        classifier=classifier,
        runtime_parent_rules=runtime_parent_rules,
        finding_policies=finding_policies,
        migration_registry=migration_registry,
    )
    permission_resolvers = {
        identity: f"{identity[0]}-domain-resolver@1"
        for identity, shape in FROZEN_RUN_KIND_SHAPES.items()
        if shape.dynamic_domain_permission
    }
    return ImmutablePlatformRegistry(
        run_kinds=run_kinds,
        retry_policies=retry_policies,
        failure_classifiers=(classifier,),
        lineage_policies=tuple(builder.lineage_policies.values()),
        version_transition_policies=(attempt_transition, run_transition),
        runtime_parent_rule_sets=(runtime_parent_rules,),
        finding_output_policies=finding_policies,
        run_event_registries=(_run_event_registry(),),
        completion_oracle_registries=(_completion_oracle_registry(),),
        playtest_payload_schema_registries=(_playtest_payload_schema_registry(),),
        agent_execution_graphs=_agent_execution_graphs(),
        execution_profile_catalogs=(
            _execution_profile_catalog_v1(),
            _execution_profile_catalog_v2(),
        ),
        migration_capability_registries=(migration_registry,),
        profile_requirements=_profile_requirements(),
        permission_resolver_keys=permission_resolvers,
    )


__all__ = [
    "ARTIFACT_PAYLOAD_SCHEMAS",
    "PROFILE_OUTPUT_SCHEMA_REQUIREMENTS",
    "build_builtin_registry",
]
