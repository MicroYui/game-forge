"""Typed M4e-deferred executors for the two internal-only Run kinds.

These functions are real, trusted registry targets.  They cannot fabricate a
migration or disaster-recovery success artifact while the production
executors remain deferred to M4e.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Callable, Mapping

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.jobs import (
    ArtifactMigrationPayloadV1,
    DrDrillPayloadV1,
    FailureClassifierRefV1,
    PreparedRunFailure,
    RunPayloadEnvelope,
)
from gameforge.platform.run_handlers.base import ExecutorContextLike

DeferredExecutor = Callable[[ExecutorContextLike], PreparedRunFailure]
DEFERRED_EXECUTOR_MARKER = "__gameforge_m4e_deferred__"


def _require_exact_profile_bindings(
    payload: RunPayloadEnvelope,
    expected: tuple[tuple[str, object, str], ...],
) -> None:
    bindings = {binding.field_path: binding for binding in payload.resolved_profiles}
    if set(bindings) != {field_path for field_path, _profile, _kind in expected}:
        raise IntegrityViolation("deferred Run profile bindings are incomplete or extra")
    for field_path, profile, profile_kind in expected:
        binding = bindings[field_path]
        if (
            binding.profile != profile
            or binding.expected_profile_kind != profile_kind
            or binding.catalog_version != payload.execution_profile_catalog_version
            or binding.catalog_digest != payload.execution_profile_catalog_digest
        ):
            raise IntegrityViolation("deferred Run profile bindings are not exact")


def _validate_deferred_context(
    context: ExecutorContextLike,
    *,
    expected_kind: str,
    capability: str,
) -> tuple[FailureClassifierRefV1, ArtifactMigrationPayloadV1 | DrDrillPayloadV1]:
    """Recheck the frozen authority that a future M4e executor will consume.

    Admission owns live Artifact/RBAC reads.  The worker seam still verifies their
    immutable projections instead of discarding the payload and accepting a
    hand-built four-field request.
    """

    run = context.run
    attempt = context.attempt
    payload = context.payload
    expected_ref = RunKindRef(kind=expected_kind, version=1)
    if run.kind != expected_ref:
        raise IntegrityViolation(f"{capability} deferred executor received another Run kind")
    if run.payload != payload:
        raise IntegrityViolation("deferred executor payload differs from the frozen Run payload")
    if (
        attempt.run_id != run.run_id
        or attempt.attempt_no != run.current_attempt_no
        or run.status != "running"
        or attempt.status != "running"
    ):
        raise IntegrityViolation("deferred executor attempt is not the current running attempt")
    if run.initiated_by.principal_kind != "system":
        raise IntegrityViolation("deferred internal Run requires a trusted system initiator")
    if (
        payload.llm_execution_mode != "not_applicable"
        or payload.execution_version_plan is not None
        or payload.cassette_artifact_id is not None
        or payload.seed is not None
    ):
        raise IntegrityViolation("deferred internal Run execution mode is not exact")

    params = payload.params
    if expected_kind == "artifact.migrate":
        if not isinstance(params, ArtifactMigrationPayloadV1):
            raise IntegrityViolation("artifact migration requires artifact-migration@1")
        if run.migration_capability_matrix is None:
            raise IntegrityViolation("artifact migration lacks its exact capability matrix")
        if payload.input_artifact_ids != (params.source_artifact_id,):
            raise IntegrityViolation("artifact migration source binding is not exact")
        _require_exact_profile_bindings(
            payload,
            (("/params/migrator", params.migrator, "artifact_migrator"),),
        )
    else:
        if not isinstance(params, DrDrillPayloadV1):
            raise IntegrityViolation("DR drill requires dr-drill@1")
        if run.migration_capability_matrix is not None:
            raise IntegrityViolation("DR drill cannot carry a migration capability matrix")
        if len(payload.input_artifact_ids) != 1:
            raise IntegrityViolation("DR drill requires one verified recovery manifest input")
        _require_exact_profile_bindings(
            payload,
            (
                ("/params/dr_plan", params.dr_plan, "dr_plan"),
                (
                    "/params/restore_target_profile",
                    params.restore_target_profile,
                    "restore_target",
                ),
                (
                    "/params/verification_profile",
                    params.verification_profile,
                    "dr_verifier",
                ),
            ),
        )
    return run.failure_classifier, params


def _deferred_failure(
    context: ExecutorContextLike,
    *,
    expected_kind: str,
    capability: str,
) -> PreparedRunFailure:
    classifier, _params = _validate_deferred_context(
        context,
        expected_kind=expected_kind,
        capability=capability,
    )
    # This failure MUST be genuinely publishable: the run boundary runs it through
    # ``validate_prepared_failure`` against the frozen classifier, and a cause that
    # is absent from that allowlist detonates with an ``IntegrityViolation`` (the
    # Task-12b defect).  ``execution_failed``/``execution`` is that honest,
    # non-retryable, dependency-free cause:
    #
    #   * it is a frozen classifier rule (``dependency_required=False``,
    #     ``intrinsic_retry_eligible=False``), so the failure passes validation and
    #     is NOT retried (``execution`` is not a retryable failure class);
    #   * ``permanent_dependency_failed`` was rejected on purpose — it would force a
    #     ``DependencyFailureV1`` whose ``dependency_kind`` must come from the frozen
    #     infra allowlist (model_provider/database/object_store/...).  None of those
    #     honestly names an M4e-absent *platform capability*, and blaming healthy
    #     infrastructure would fabricate a dependency failure (mis-routing
    #     dependency-health alerting).  The M4e-absence is carried by the redacted
    #     message, not by fabricating an infra fault.
    #
    # M4e replaces this executor seam with a real success path WITHOUT changing the
    # RunKind/classifier contract; it never weakens this fail-closed default.
    return PreparedRunFailure(
        run_id=context.run.run_id,
        attempt_no=context.attempt.attempt_no,
        run_kind=context.run.kind,
        artifacts=(),
        requirement_dispositions=(),
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        classifier=classifier,
        redacted_message=f"{capability} execution is deferred to M4e and unavailable in M4c",
    )


def artifact_migration_deferred(
    context: ExecutorContextLike,
) -> PreparedRunFailure:
    return _deferred_failure(
        context,
        expected_kind="artifact.migrate",
        capability="artifact migration",
    )


def dr_drill_deferred(context: ExecutorContextLike) -> PreparedRunFailure:
    return _deferred_failure(
        context,
        expected_kind="dr.drill",
        capability="disaster recovery drill",
    )


setattr(artifact_migration_deferred, DEFERRED_EXECUTOR_MARKER, True)
setattr(dr_drill_deferred, DEFERRED_EXECUTOR_MARKER, True)


DEFERRED_EXECUTORS: Mapping[str, DeferredExecutor] = MappingProxyType(
    {
        "artifact_migrator@1": artifact_migration_deferred,
        "dr_drill_runner@1": dr_drill_deferred,
    }
)


__all__ = [
    "DEFERRED_EXECUTORS",
    "DEFERRED_EXECUTOR_MARKER",
    "DeferredExecutor",
    "artifact_migration_deferred",
    "dr_drill_deferred",
]
