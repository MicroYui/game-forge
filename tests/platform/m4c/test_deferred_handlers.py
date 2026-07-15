"""Lock the two M4e-deferred internal-only Run seams (M4c Task 14).

``artifact.migrate@1`` (``artifact_migrator@1``) and ``dr.drill@1``
(``dr_drill_runner@1``) are registered with their COMPLETE M4e success contracts,
but their M4c executors are real trusted registry targets that fail-close with a
typed, *publishable* UNAVAILABLE failure — they can NEVER fabricate a migrated
Artifact / migration report, nor a backup/restore/RPO/RTO drill result.

These tests lock four properties:

1. Each deferred executor returns a ``PreparedRunFailure`` (never a
   ``PreparedRunResult``) with ``artifacts=()`` / ``requirement_dispositions=()``
   and a FROZEN typed cause, and that failure PASSES the real run-boundary
   validator ``validate_prepared_failure`` against the frozen classifier — the
   Task-12b lesson (a cause absent from the frozen allowlist detonates at the
   boundary with ``IntegrityViolation``, so "typed unavailable" is worthless
   unless it is genuinely publishable).
2. A wrong Run kind is rejected by the executor's own kind guard.
3. Neither kind is admissible through the generic/resource PUBLIC surfaces:
   both are ``creation_mode="internal_only"`` and the admission engine rejects a
   non-trusted-internal actor (reuses the Task-8 admission harness).
4. Platform readiness still PASSES with the deferred executors bound, and the
   frozen M4e success-policy contracts (migration capability matrix family, dr
   success policy + terminal success hooks) are RETAINED, not deleted.
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.jobs import (
    ArtifactMigrationPayloadV1,
    DrDrillPayloadV1,
    FailureClassifierV1,
    PreparedRunFailure,
    PreparedRunResult,
    RunAttempt,
    RunPayloadEnvelope,
    RunRecord,
    referenced_input_artifact_ids,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.platform.registry import (
    PlatformReadinessValidator,
    build_builtin_registry,
)
from gameforge.platform.run_handlers.deferred import (
    DEFERRED_EXECUTORS,
    DeferredExecutionRequest,
    DeferredExecutor,
    artifact_migration_deferred,
    dr_drill_deferred,
)
from gameforge.platform.runs.lifecycle import validate_prepared_failure
from tests.platform.m4c.handler_support import build_attempt, build_run_record
from tests.platform.m4c.test_readiness_registry import (
    _DEFERRED_EXECUTOR_KEYS,
    _components,
)
from tests.platform.m4c.test_run_admission import (
    Harness,
    _actor,
    _server,
)

_HEX = "a" * 64


# ── kind fixtures ────────────────────────────────────────────────────────────
def _migration_params() -> ArtifactMigrationPayloadV1:
    return ArtifactMigrationPayloadV1(
        source_artifact_id="artifact:source",
        target_payload_schema_id="ir-core@2",
        target_meta_schema_version="meta@2",
        migrator=ProfileRefV1(profile_id="builtin.artifact_migrator", version=1),
        publish_mode="publish_migrated_artifact",
    )


def _dr_params() -> DrDrillPayloadV1:
    return DrDrillPayloadV1(
        dr_plan=ProfileRefV1(profile_id="builtin.dr_plan", version=1),
        recovery_catalog_entry_id="recovery:1",
        expected_checkpoint_id="checkpoint:1",
        restore_target_profile=ProfileRefV1(profile_id="builtin.restore_target", version=1),
        verification_profile=ProfileRefV1(profile_id="builtin.verification", version=1),
        destroy_restored_target_after_verification=True,
    )


_KINDS: dict[str, dict[str, object]] = {
    "artifact.migrate": {
        "executor_key": "artifact_migrator@1",
        "executor": artifact_migration_deferred,
        "params": _migration_params,
        "extra_inputs": (),
        "success_hook": "publish_migration@1",
        "success_outcome_code": "artifact_migration_reported",
    },
    "dr.drill": {
        "executor_key": "dr_drill_runner@1",
        "executor": dr_drill_deferred,
        # dr.drill's envelope requires exactly one extra dynamic input (the
        # verified recovery manifest) beyond the payload-referenced inputs.
        "params": _dr_params,
        "extra_inputs": ("artifact:recovery-manifest",),
        "success_hook": "publish_operational_evidence@1",
        "success_outcome_code": "dr_drill_completed",
    },
}


def _run_binding(
    kind_name: str,
) -> tuple[RunRecord, RunAttempt, FailureClassifierV1, object]:
    """Build a real ``RunRecord``/``RunAttempt`` bound to the frozen classifier.

    The record's ``kind`` and ``failure_classifier`` are the authoritative
    registry values, so a deferred ``PreparedRunFailure`` built from the same
    identity can be run through the real ``validate_prepared_failure``.
    """

    registry = build_builtin_registry()
    kind = RunKindRef(kind=kind_name, version=1)
    definition = registry.get_run_kind(kind)
    assert definition is not None
    classifier_ref = definition.failure_classifier
    classifier = registry.get_failure_classifier(classifier_ref)
    assert classifier is not None

    params = _KINDS[kind_name]["params"]()  # type: ignore[operator]
    extra_inputs: tuple[str, ...] = _KINDS[kind_name]["extra_inputs"]  # type: ignore[assignment]
    inputs = tuple(referenced_input_artifact_ids(params)) + extra_inputs
    envelope = RunPayloadEnvelope(
        payload_schema_version=params.schema_version,
        input_artifact_ids=inputs,
        version_tuple=VersionTuple(tool_version="handler@1"),
        execution_version_plan=None,
        policy_bindings=(),
        schema_bindings=(),
        execution_profile_catalog_version=1,
        execution_profile_catalog_digest=_HEX,
        resolved_profiles=(),
        resolved_policy_snapshots=(),
        budget_set_snapshot_id="budget-set:1",
        seed=None,
        llm_execution_mode="not_applicable",
        cassette_artifact_id=None,
        params=params,
    )
    record = build_run_record(envelope, kind).model_copy(
        update={"failure_classifier": classifier_ref}
    )
    attempt = build_attempt(run_id=record.run_id, attempt_no=record.current_attempt_no or 1)
    return record, attempt, classifier, definition


def _request(kind_name: str) -> DeferredExecutionRequest:
    record, attempt, _classifier, _definition = _run_binding(kind_name)
    return DeferredExecutionRequest(
        run_id=record.run_id,
        attempt_no=attempt.attempt_no,
        run_kind=record.kind,
        classifier=record.failure_classifier,
    )


# ── 1. the registry maps exactly the two internal executor keys ──────────────
def test_deferred_registry_maps_exactly_the_two_internal_kinds() -> None:
    assert set(DEFERRED_EXECUTORS) == set(_DEFERRED_EXECUTOR_KEYS)
    assert DEFERRED_EXECUTORS["artifact_migrator@1"] is artifact_migration_deferred
    assert DEFERRED_EXECUTORS["dr_drill_runner@1"] is dr_drill_deferred
    # The public alias type constrains the executor to a failure-only return.
    hints = typing.get_type_hints(artifact_migration_deferred)
    assert hints["return"] is PreparedRunFailure
    assert typing.get_args(DeferredExecutor)[-1] is PreparedRunFailure


# ── 2. typed unavailable + passes the REAL run-boundary validator ────────────
@pytest.mark.parametrize("kind_name", list(_KINDS))
def test_deferred_failure_is_typed_unavailable_and_passes_real_validator(
    kind_name: str,
) -> None:
    record, attempt, classifier, _definition = _run_binding(kind_name)
    request = DeferredExecutionRequest(
        run_id=record.run_id,
        attempt_no=attempt.attempt_no,
        run_kind=record.kind,
        classifier=record.failure_classifier,
    )
    executor = _KINDS[kind_name]["executor"]
    prepared = executor(request)  # type: ignore[operator]

    # A failure only — never a success DTO.
    assert isinstance(prepared, PreparedRunFailure)
    assert not isinstance(prepared, PreparedRunResult)
    assert prepared.prepared_schema_version == "prepared-run-failure@1"
    assert prepared.run_kind == record.kind
    # No fabricated migrated artifact / migration report / backup / restore
    # evidence: the failure carries no artifacts and no requirement dispositions.
    assert prepared.artifacts == ()
    assert prepared.requirement_dispositions == ()
    # FROZEN typed cause: the honest, non-retryable execution-unavailable code.
    assert prepared.cause_code == "execution_failed"
    assert prepared.failure_class == "execution"
    assert prepared.intrinsic_retry_eligible is False
    # ``execution`` carries no fabricated infra dependency (no healthy
    # object_store/database blamed for an M4e-absent capability).
    assert prepared.dependency is None

    # THE GOTCHA (Task-12b): the failure must survive the real run boundary.
    validate_prepared_failure(
        run=record,
        attempt=attempt,
        prepared=prepared,
        classifier=classifier,
    )


def test_real_validator_rejects_a_cause_absent_from_the_frozen_classifier() -> None:
    """Proves the validator in the test above genuinely bites (not a no-op).

    A ``PreparedRunFailure`` whose ``cause_code`` is not in the frozen classifier
    allowlist is exactly the Task-12b defect: it detonates at the run boundary.
    """

    record, attempt, classifier, _definition = _run_binding("artifact.migrate")
    prepared = artifact_migration_deferred(
        DeferredExecutionRequest(
            run_id=record.run_id,
            attempt_no=attempt.attempt_no,
            run_kind=record.kind,
            classifier=record.failure_classifier,
        )
    )
    # The genuine failure passes; a tampered cause absent from the classifier does not.
    validate_prepared_failure(run=record, attempt=attempt, prepared=prepared, classifier=classifier)
    tampered = prepared.model_copy(update={"cause_code": "capability_absent_unknown"})
    with pytest.raises(IntegrityViolation, match="cause is absent"):
        validate_prepared_failure(
            run=record, attempt=attempt, prepared=tampered, classifier=classifier
        )


# ── 3. the executor's own kind guard fails closed on a foreign Run kind ──────
def test_deferred_executor_rejects_a_foreign_run_kind() -> None:
    dr_request = _request("dr.drill")
    migrate_request = _request("artifact.migrate")
    with pytest.raises(ValueError, match="artifact migration deferred executor"):
        artifact_migration_deferred(dr_request)
    with pytest.raises(ValueError, match="disaster recovery drill deferred executor"):
        dr_drill_deferred(migrate_request)


# ── 4. the failure return type structurally forbids a success artifact ───────
@pytest.mark.parametrize("kind_name", list(_KINDS))
def test_deferred_failure_can_never_be_reshaped_into_a_success(kind_name: str) -> None:
    request = _request(kind_name)
    prepared = _KINDS[kind_name]["executor"](request)  # type: ignore[operator]
    assert isinstance(prepared, PreparedRunFailure)
    # A ``PreparedRunResult`` requires at least one artifact + a summary, so the
    # empty-artifact deferred failure could never be re-expressed as a success.
    with pytest.raises(Exception):
        PreparedRunResult(
            run_id=prepared.run_id,
            attempt_no=prepared.attempt_no or 1,
            run_kind=prepared.run_kind,
            primary_index=0,
            artifacts=(),  # min_length=1 → rejected
            findings=(),
            requirement_dispositions=(),
            summary=None,  # type: ignore[arg-type]
        )


# ── 5. internal_only: rejected on the generic and non-trusted actor paths ────
@pytest.mark.parametrize("kind_name", list(_KINDS))
def test_internal_only_kind_creation_mode_is_frozen(kind_name: str) -> None:
    registry = build_builtin_registry()
    definition = registry.get_run_kind(RunKindRef(kind=kind_name, version=1))
    assert definition is not None
    assert definition.creation_mode == "internal_only"
    assert definition.executor_key == _KINDS[kind_name]["executor_key"]


def _params_for(kind_name: str):
    return _KINDS[kind_name]["params"]()  # type: ignore[operator]


@pytest.mark.parametrize("kind_name", list(_KINDS))
def test_generic_endpoint_rejects_internal_only_kind(kind_name: str, tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(IntegrityViolation):
        harness.engine_admission.admit_generic_run(
            params=_params_for(kind_name),
            actor=_actor(),
            server=_server(f"{kind_name}:generic"),
        )


@pytest.mark.parametrize("actor_kind", ["human", "service"])
@pytest.mark.parametrize("kind_name", list(_KINDS))
def test_internal_run_requires_a_trusted_system_actor(
    kind_name: str, actor_kind: str, tmp_path: Path
) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(IntegrityViolation, match="trusted system actor"):
        harness.engine_admission.admit_internal_run(
            params=_params_for(kind_name),
            actor=_actor(actor_kind),
            server=_server(f"{kind_name}:{actor_kind}"),
        )


# ── 6. readiness passes AND the frozen M4e success contracts are retained ────
def test_readiness_passes_and_retains_the_frozen_success_contracts() -> None:
    registry = build_builtin_registry()
    components = _components(registry)
    report = PlatformReadinessValidator(
        registry=registry,
        components=components,
    ).validate()

    # The intentionally-absent M4e implementation does NOT break readiness: the
    # executor_key is bound to a real deferred callable, closing the mapping.
    assert report.ready is True
    assert report.deferred_executor_keys == _DEFERRED_EXECUTOR_KEYS
    assert set(_DEFERRED_EXECUTOR_KEYS) <= set(components.executors)

    # artifact.migrate keeps the complete MigrationCapabilityMatrix family (the
    # M4e replacement seam) — retained, never deleted.
    migrate = registry.get_run_kind(RunKindRef(kind="artifact.migrate", version=1))
    assert migrate is not None
    assert migrate.migration_capability_matrix is not None
    matrix = registry.get_migration_capability_matrix(migrate.migration_capability_matrix)
    assert matrix is not None
    assert matrix.matrix_version == 1
    assert any(policy.prepared_outcome == "success" for policy in migrate.outcome_policies)
    assert migrate.terminal_hooks.on_success == "publish_migration@1"
    assert "publish_migration@1" in components.terminal_hooks

    # dr.drill keeps its complete success policy + operational-evidence hook.
    drill = registry.get_run_kind(RunKindRef(kind="dr.drill", version=1))
    assert drill is not None
    success = [policy for policy in drill.outcome_policies if policy.prepared_outcome == "success"]
    assert [policy.outcome_code for policy in success] == ["dr_drill_completed"]
    assert drill.terminal_hooks.on_success == "publish_operational_evidence@1"
    assert "publish_operational_evidence@1" in components.terminal_hooks
