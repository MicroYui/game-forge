"""Lock the two M4e-deferred internal-only Run seams (M4c Task 14).

``artifact.migrate@1`` (``artifact_migrator@1``) and ``dr.drill@1``
(``dr_drill_runner@1``) are registered with their COMPLETE M4e success contracts,
but their M4c executors are real trusted registry targets that fail-close with a
typed, *publishable* UNAVAILABLE failure — they can NEVER fabricate a migrated
Artifact / migration report, nor a backup/restore/RPO/RTO drill result.

These tests lock six properties:

1. Each deferred executor returns a ``PreparedRunFailure`` (never a
   ``PreparedRunResult``) with ``artifacts=()`` / ``requirement_dispositions=()``
   and a FROZEN typed cause, and that failure PASSES the real run-boundary
   validator ``validate_prepared_failure`` against the frozen classifier — the
   Task-12b lesson (a cause absent from the frozen allowlist detonates at the
   boundary with ``IntegrityViolation``, so "typed unavailable" is worthless
   unless it is genuinely publishable).
2. The normal full worker context is rechecked for exact payload/profile/matrix,
   source/recovery-manifest, current attempt, and trusted-system projections.
3. A wrong Run kind is rejected by the executor's own kind guard.
4. Neither kind is admissible through the generic/resource PUBLIC surfaces:
   both are ``creation_mode="internal_only"`` and the admission engine rejects a
   non-trusted-internal actor (reuses the Task-8 admission harness).
5. Platform readiness still PASSES with the deferred executors bound, and the
   frozen M4e success-policy contracts (migration capability matrix family, DR
   success policy + terminal success hooks) are RETAINED, not deleted.
6. A real M4e executor can replace a deferred callable under the retained key;
   readiness reports the actual callable rather than hard-coding the key as deferred.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    execution_profile_payload_hash,
)
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
from gameforge.contracts.lineage import AuditActor, VersionTuple
from gameforge.platform.registry import (
    PlatformReadinessValidator,
    build_builtin_registry,
)
from gameforge.platform.run_handlers.deferred import (
    DEFERRED_EXECUTORS,
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

_SYSTEM = AuditActor(principal_id="system:operations", principal_kind="system")


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
        verification_profile=ProfileRefV1(profile_id="builtin.dr_verifier", version=1),
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
    catalog = max(
        registry.list_execution_profile_catalogs(),
        key=lambda item: item.catalog_version,
    )
    profile_definitions = {item.profile: item for item in catalog.definitions}

    params = _KINDS[kind_name]["params"]()  # type: ignore[operator]
    expected_profiles = (
        (
            (
                "/params/migrator",
                params.migrator,
                "artifact_migrator",
            ),
        )
        if isinstance(params, ArtifactMigrationPayloadV1)
        else (
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
        )
    )
    resolved_profiles = tuple(
        ResolvedExecutionProfileBindingV1(
            field_path=field_path,
            profile=profile,
            expected_profile_kind=profile_kind,
            profile_payload_hash=execution_profile_payload_hash(profile_definitions[profile]),
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
        )
        for field_path, profile, profile_kind in expected_profiles
    )
    extra_inputs: tuple[str, ...] = _KINDS[kind_name]["extra_inputs"]  # type: ignore[assignment]
    inputs = tuple(referenced_input_artifact_ids(params)) + extra_inputs
    envelope = RunPayloadEnvelope(
        payload_schema_version=params.schema_version,
        input_artifact_ids=inputs,
        version_tuple=VersionTuple(tool_version="handler@1"),
        execution_version_plan=None,
        policy_bindings=(),
        schema_bindings=(),
        execution_profile_catalog_version=catalog.catalog_version,
        execution_profile_catalog_digest=catalog.catalog_digest,
        resolved_profiles=resolved_profiles,
        resolved_policy_snapshots=(),
        budget_set_snapshot_id="budget-set:1",
        seed=None,
        llm_execution_mode="not_applicable",
        cassette_artifact_id=None,
        params=params,
    )
    record = build_run_record(envelope, kind).model_copy(
        update={
            "failure_classifier": classifier_ref,
            "initiated_by": _SYSTEM,
            "migration_capability_matrix": definition.migration_capability_matrix,
        }
    )
    attempt = build_attempt(run_id=record.run_id, attempt_no=record.current_attempt_no or 1)
    return record, attempt, classifier, definition


def _context(kind_name: str) -> SimpleNamespace:
    record, attempt, _classifier, _definition = _run_binding(kind_name)
    return SimpleNamespace(
        run=record,
        attempt=attempt,
        payload=record.payload,
        deadline_utc=None,
        model_bridge=None,
    )


# ── 1. the registry maps exactly the two internal executor keys ──────────────
def test_deferred_registry_maps_exactly_the_two_internal_kinds() -> None:
    assert set(DEFERRED_EXECUTORS) == set(_DEFERRED_EXECUTOR_KEYS)
    assert DEFERRED_EXECUTORS["artifact_migrator@1"] is artifact_migration_deferred
    assert DEFERRED_EXECUTORS["dr_drill_runner@1"] is dr_drill_deferred
    # Both deferred implementations use the same full ExecutorContext-shaped
    # callable seam as every real Run handler; M4e can replace either value under
    # the retained key without changing dispatch.
    assert callable(artifact_migration_deferred)
    assert callable(dr_drill_deferred)
    assert DeferredExecutor is not None


# ── 2. typed unavailable + passes the REAL run-boundary validator ────────────
@pytest.mark.parametrize("kind_name", list(_KINDS))
def test_deferred_failure_is_typed_unavailable_and_passes_real_validator(
    kind_name: str,
) -> None:
    record, attempt, classifier, _definition = _run_binding(kind_name)
    context = _context(kind_name)
    executor = _KINDS[kind_name]["executor"]
    prepared = executor(context)  # type: ignore[operator]

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
    prepared = artifact_migration_deferred(_context("artifact.migrate"))
    # The genuine failure passes; a tampered cause absent from the classifier does not.
    validate_prepared_failure(run=record, attempt=attempt, prepared=prepared, classifier=classifier)
    tampered = prepared.model_copy(update={"cause_code": "capability_absent_unknown"})
    with pytest.raises(IntegrityViolation, match="cause is absent"):
        validate_prepared_failure(
            run=record, attempt=attempt, prepared=tampered, classifier=classifier
        )


# ── 3. the executor's own kind guard fails closed on a foreign Run kind ──────
def test_deferred_executor_rejects_a_foreign_run_kind() -> None:
    dr_request = _context("dr.drill")
    migrate_request = _context("artifact.migrate")
    with pytest.raises(IntegrityViolation, match="artifact migration deferred executor"):
        artifact_migration_deferred(dr_request)
    with pytest.raises(IntegrityViolation, match="disaster recovery drill deferred executor"):
        dr_drill_deferred(migrate_request)


# ── 4. the failure return type structurally forbids a success artifact ───────
@pytest.mark.parametrize("kind_name", list(_KINDS))
def test_deferred_failure_can_never_be_reshaped_into_a_success(kind_name: str) -> None:
    request = _context(kind_name)
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


@pytest.mark.parametrize("kind_name", list(_KINDS))
@pytest.mark.parametrize(
    ("tamper", "message"),
    (
        ("payload", "payload differs"),
        ("initiator", "trusted system"),
        ("profiles", "profile bindings"),
    ),
)
def test_deferred_executor_rejects_incomplete_frozen_authority(
    kind_name: str,
    tamper: str,
    message: str,
) -> None:
    context = _context(kind_name)
    if tamper == "payload":
        context.payload = _context(kind_name).payload.model_copy(
            update={"execution_profile_catalog_digest": "b" * 64}
        )
    elif tamper == "initiator":
        context.run = context.run.model_copy(
            update={
                "initiated_by": AuditActor(
                    principal_id="human:not-internal",
                    principal_kind="human",
                )
            }
        )
    else:
        context.payload = context.payload.model_copy(update={"resolved_profiles": ()})
        context.run = context.run.model_copy(update={"payload": context.payload})

    with pytest.raises(IntegrityViolation, match=message):
        _KINDS[kind_name]["executor"](context)  # type: ignore[operator]


def test_artifact_migration_requires_exact_matrix_and_source_binding() -> None:
    context = _context("artifact.migrate")
    context.run = context.run.model_copy(update={"migration_capability_matrix": None})
    with pytest.raises(IntegrityViolation, match="capability matrix"):
        artifact_migration_deferred(context)

    context = _context("artifact.migrate")
    malformed = context.payload.model_copy(update={"input_artifact_ids": ("artifact:other",)})
    context.payload = malformed
    context.run = context.run.model_copy(update={"payload": malformed})
    with pytest.raises(IntegrityViolation, match="source binding"):
        artifact_migration_deferred(context)


def test_dr_drill_requires_one_verified_recovery_manifest_input() -> None:
    context = _context("dr.drill")
    malformed = context.payload.model_copy(update={"input_artifact_ids": ()})
    context.payload = malformed
    context.run = context.run.model_copy(update={"payload": malformed})
    with pytest.raises(IntegrityViolation, match="recovery manifest"):
        dr_drill_deferred(context)


# ── 5. internal_only: rejected on generic/resource and human actor paths ─────
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


@pytest.mark.parametrize("kind_name", list(_KINDS))
def test_resource_endpoint_rejects_internal_only_kind(kind_name: str, tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(IntegrityViolation, match="dedicated typed platform admission surface"):
        harness.engine_admission.admit_resource_run(
            params=_params_for(kind_name),
            actor=_actor(),
            server=_server(f"{kind_name}:resource"),
        )


@pytest.mark.parametrize("kind_name", list(_KINDS))
def test_internal_run_rejects_a_human_actor(kind_name: str, tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(IntegrityViolation, match="trusted service or system actor"):
        harness.engine_admission.admit_internal_run(
            params=_params_for(kind_name),
            actor=_actor("human"),
            server=_server(f"{kind_name}:human"),
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


def test_readiness_reports_actual_deferred_components_not_retained_keys() -> None:
    registry = build_builtin_registry()
    components = _components(registry)
    executors = dict(components.executors)

    def real_migration_executor(context: object) -> object:
        return context

    executors["artifact_migrator@1"] = real_migration_executor
    replaced = components.__class__(
        executors=executors,
        terminal_hooks=components.terminal_hooks,
        workflow_effects=components.workflow_effects,
        completion_oracles=components.completion_oracles,
        playtest_payload_validators=components.playtest_payload_validators,
        profile_handlers=components.profile_handlers,
        permission_domain_resolvers=components.permission_domain_resolvers,
    )

    report = PlatformReadinessValidator(registry=registry, components=replaced).validate()

    assert report.ready is True
    assert report.deferred_executor_keys == ("dr_drill_runner@1",)


@pytest.mark.parametrize("executor_key", list(_DEFERRED_EXECUTOR_KEYS))
def test_readiness_rejects_a_missing_deferred_executor(executor_key: str) -> None:
    registry = build_builtin_registry()
    components = _components(registry)
    executors = dict(components.executors)
    executors.pop(executor_key)
    incomplete = components.__class__(
        executors=executors,
        terminal_hooks=components.terminal_hooks,
        workflow_effects=components.workflow_effects,
        completion_oracles=components.completion_oracles,
        playtest_payload_validators=components.playtest_payload_validators,
        profile_handlers=components.profile_handlers,
        permission_domain_resolvers=components.permission_domain_resolvers,
    )

    with pytest.raises(IntegrityViolation, match="executor trusted key set"):
        PlatformReadinessValidator(registry=registry, components=incomplete).validate()
