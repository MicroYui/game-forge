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
   validator ``validate_prepared_failure`` against the frozen classifier.
2. A wrong Run kind is rejected by the executor's own kind guard.
3. Admission owns the typed request, permission, profile, matrix, and input
   bindings; neither kind is admissible through generic/resource PUBLIC surfaces.
4. Platform readiness still passes, retains the M4e success contracts, and
   reports when a real implementation replaces a deferred callable.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
    },
    "dr.drill": {
        "executor_key": "dr_drill_runner@1",
        "executor": dr_drill_deferred,
        # dr.drill's envelope requires exactly one extra dynamic input (the
        # verified recovery manifest) beyond the payload-referenced inputs.
        "params": _dr_params,
        "extra_inputs": ("artifact:recovery-manifest",),
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
        execution_profile_catalog_digest="a" * 64,
        resolved_profiles=(),
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


# ── 3. the executor's own kind guard fails closed on a foreign Run kind ──────
def test_deferred_executor_rejects_a_foreign_run_kind() -> None:
    dr_request = _context("dr.drill")
    migrate_request = _context("artifact.migrate")
    with pytest.raises(IntegrityViolation, match="artifact migration deferred executor"):
        artifact_migration_deferred(dr_request)
    with pytest.raises(IntegrityViolation, match="disaster recovery drill deferred executor"):
        dr_drill_deferred(migrate_request)


# ── 4. internal_only: rejected on generic/resource and human actor paths ─────
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


# ── readiness passes AND the frozen M4e success contracts are retained ──────
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
