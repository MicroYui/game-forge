from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    RunKindRef,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.findings import FindingPayloadV1
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    CheckerRunPayloadV1,
    DependencyFailureV1,
    ExecutionVersionPlanV1,
    FailureClassifierRefV1,
    GenerationProposePayloadV1,
    GraphSelectionV1,
    PatchRepairPayloadV1,
    PlannedAgentNodeVersionV1,
    PreparedArtifact,
    PreparedFindingV1,
    PreparedRunFailure,
    PreparedRunResult,
    PreparedRunResultSummaryV1,
    Problem,
    PromptGoalBindingV1,
    RefReadBindingV1,
    ResolvedArtifactRequirementV1,
    ResolvedPolicySnapshotV1,
    RequirementDispositionV1,
    RetryDecisionV1,
    RetryPolicyRefV1,
    RunPayloadEnvelope,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunFailureV1,
    RunResultSummaryV1,
    RunResultV1,
)
from gameforge.contracts.lineage import ObjectLocation, ObjectRef, VersionTuple
from gameforge.contracts.storage import RefValue


_HASH_A = "a" * 64
_HASH_B = "b" * 64
_OBJECT_KEY = f"objects/v1/sha256/{_HASH_A[:2]}/{_HASH_A}"
_MAX_ITEMS = 1024
_MAX_STRING_LENGTH = 4096
_MAX_ID_LENGTH = 512
_MAX_SEED = (1 << 64) - 1


def _profile(profile_id: str = "profile") -> ProfileRefV1:
    return ProfileRefV1(profile_id=profile_id, version=1)


def _target() -> RefReadBindingV1:
    return RefReadBindingV1(
        ref_name="refs/content/current",
        expected_ref=RefValue(artifact_id="artifact:base", revision=1),
    )


def _generation_payload(
    *,
    constraint_snapshot_artifact_id: str | None,
    candidate_export_profiles: tuple[ProfileRefV1, ...],
) -> GenerationProposePayloadV1:
    return GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:base",
        constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal",
            expected_payload_hash=_HASH_A,
        ),
        domain_scope=DomainScope(domain_ids=("quests",)),
        target=_target(),
        generation_policy=_profile("generation"),
        candidate_export_profiles=candidate_export_profiles,
    )


def _patch_repair_payload(
    *,
    constraint_snapshot_artifact_id: str | None,
    candidate_export_profiles: tuple[ProfileRefV1, ...],
) -> PatchRepairPayloadV1:
    return PatchRepairPayloadV1(
        subject_patch_artifact_id="artifact:patch",
        expected_subject_head_revision=1,
        expected_workflow_revision=1,
        base_snapshot_artifact_id="artifact:base",
        preview_snapshot_artifact_id="artifact:preview",
        constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
        validation_evidence_artifact_id="artifact:validation",
        findings=(),
        target=_target(),
        repair_policy=_profile("repair"),
        checker_profiles=(),
        simulation_profiles=(),
        regression_suite_artifact_ids=(),
        candidate_export_profiles=candidate_export_profiles,
    )


def _run_payload(**changes: Any) -> RunPayloadEnvelope:
    snapshot_artifact_id = changes.pop("snapshot_artifact_id", "artifact:snapshot")
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot_artifact_id,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=_profile("checker"),
        checker_ids=(),
        defect_classes=(),
    )
    values: dict[str, Any] = {
        "payload_schema_version": "checker-run@1",
        "input_artifact_ids": (snapshot_artifact_id,),
        "version_tuple": VersionTuple(
            ir_snapshot_id="snapshot:1",
            tool_version="checker@1",
        ),
        "policy_bindings": (),
        "schema_bindings": (),
        "execution_profile_catalog_version": 1,
        "execution_profile_catalog_digest": _HASH_A,
        "resolved_profiles": (),
        "resolved_policy_snapshots": (),
        "budget_set_snapshot_id": "budget:1",
        "llm_execution_mode": "not_applicable",
        "params": params,
    }
    values.update(changes)
    return RunPayloadEnvelope.model_validate(values)


@pytest.mark.parametrize(
    "factory",
    [_generation_payload, _patch_repair_payload],
    ids=["generation", "patch-repair"],
)
def test_candidate_exports_require_constraint_snapshot(
    factory: Callable[..., BaseModel],
) -> None:
    with pytest.raises(ValidationError, match="constraint_snapshot_artifact_id"):
        factory(
            constraint_snapshot_artifact_id=None,
            candidate_export_profiles=(_profile("export"),),
        )


@pytest.mark.parametrize(
    "factory",
    [_generation_payload, _patch_repair_payload],
    ids=["generation", "patch-repair"],
)
def test_empty_candidate_exports_allow_missing_constraint_snapshot(
    factory: Callable[..., BaseModel],
) -> None:
    payload = factory(
        constraint_snapshot_artifact_id=None,
        candidate_export_profiles=(),
    )

    assert payload.constraint_snapshot_artifact_id is None
    assert payload.candidate_export_profiles == ()


def test_run_payload_envelope_publishes_direct_hard_bounds() -> None:
    properties = RunPayloadEnvelope.model_json_schema()["properties"]

    assert properties["payload_schema_version"]["maxLength"] == _MAX_STRING_LENGTH
    assert properties["input_artifact_ids"]["maxItems"] == _MAX_ITEMS
    assert properties["input_artifact_ids"]["items"]["maxLength"] == _MAX_ID_LENGTH
    assert properties["budget_set_snapshot_id"]["maxLength"] == _MAX_ID_LENGTH
    cassette_schema = next(
        branch
        for branch in properties["cassette_artifact_id"]["anyOf"]
        if branch.get("type") == "string"
    )
    assert cassette_schema["maxLength"] == _MAX_ID_LENGTH
    seed_schema = next(
        branch for branch in properties["seed"]["anyOf"] if branch.get("type") == "integer"
    )
    assert seed_schema == {
        "maximum": _MAX_SEED,
        "minimum": 0,
        "type": "integer",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"payload_schema_version": "x" * (_MAX_STRING_LENGTH + 1)},
        {"snapshot_artifact_id": "x" * (_MAX_ID_LENGTH + 1)},
        {"budget_set_snapshot_id": "x" * (_MAX_ID_LENGTH + 1)},
        {"cassette_artifact_id": "x" * (_MAX_ID_LENGTH + 1)},
        {"seed": -1},
        {"seed": _MAX_SEED + 1},
    ],
    ids=[
        "payload-schema-version",
        "input-artifact-id",
        "budget-set-id",
        "cassette-artifact-id",
        "negative-seed",
        "oversized-seed",
    ],
)
def test_run_payload_envelope_rejects_unbounded_direct_fields(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        _run_payload(**changes)


def _prepared_artifact(**changes: Any) -> PreparedArtifact:
    values: dict[str, Any] = {
        "kind": "checker_run",
        "payload_schema_id": "checker-run@1",
        "version_tuple": VersionTuple(
            ir_snapshot_id="snapshot:1",
            tool_version="checker@1",
        ),
        "lineage": ("artifact:input",),
        "payload_hash": _HASH_A,
        "meta": {"source": "historical", "nested": {"items": [1, None, True]}},
        "object_ref": ObjectRef(
            key=_OBJECT_KEY,
            sha256=_HASH_A,
            size_bytes=1,
        ),
        "location": ObjectLocation(
            store_id="local",
            key=_OBJECT_KEY,
            backend_generation="generation:1",
        ),
    }
    values.update(changes)
    return PreparedArtifact.model_validate(values)


def _prepared_finding(index: int = 0) -> PreparedFindingV1:
    return PreparedFindingV1(
        finding_id=f"finding:{index}",
        expected_previous_revision=None,
        evidence_artifact_index=0,
        payload=FindingPayloadV1(
            source="checker",
            producer_id="checker:graph",
            producer_run_id="run:1",
            oracle_type="deterministic",
            defect_class="quest_unreachable",
            severity="major",
            snapshot_id="snapshot:1",
            status="confirmed",
            message="unreachable quest",
        ),
    )


def _disposition(index: int = 0) -> RequirementDispositionV1:
    return RequirementDispositionV1(
        resolved_policy_id="policy:1",
        outcome_rule_id="rule:1",
        requirement_id=f"requirement:{index}",
        status="produced",
    )


def _prepared_result(**changes: Any) -> PreparedRunResult:
    artifact = _prepared_artifact()
    values: dict[str, Any] = {
        "run_id": "run:1",
        "attempt_no": 1,
        "run_kind": RunKindRef(kind="checker.run", version=1),
        "primary_index": 0,
        "artifacts": (artifact,),
        "findings": (),
        "requirement_dispositions": (),
        "summary": PreparedRunResultSummaryV1(
            outcome_code="passed",
            primary_artifact_kind="checker_run",
            prepared_domain_artifact_count=1,
            prepared_finding_count=0,
        ),
    }
    values.update(changes)
    return PreparedRunResult.model_validate(values)


def _classifier_ref() -> FailureClassifierRefV1:
    return FailureClassifierRefV1(
        classifier_version=1,
        classifier_digest=_HASH_A,
    )


def _retry_policy_ref() -> RetryPolicyRefV1:
    return RetryPolicyRefV1(
        retry_policy_id="retry-policy:1",
        retry_policy_version=1,
        retry_policy_digest=_HASH_B,
    )


def _retry_decision(**changes: Any) -> RetryDecisionV1:
    values: dict[str, Any] = {
        "cause_code": "validation_failed",
        "failure_class": "validation",
        "intrinsic_retry_eligible": False,
        "decision": "terminal",
        "reason_code": "not_retry_eligible",
        "classifier": _classifier_ref(),
        "retry_policy": _retry_policy_ref(),
        "evaluated_at_utc": "2026-07-14T00:00:00Z",
    }
    values.update(changes)
    return RetryDecisionV1.model_validate(values)


def _prepared_failure(**changes: Any) -> PreparedRunFailure:
    values: dict[str, Any] = {
        "run_id": "run:1",
        "attempt_no": 1,
        "run_kind": RunKindRef(kind="checker.run", version=1),
        "artifacts": (),
        "requirement_dispositions": (),
        "cause_code": "validation_failed",
        "failure_class": "validation",
        "intrinsic_retry_eligible": False,
        "classifier": _classifier_ref(),
        "redacted_message": "deterministic validation failed",
    }
    values.update(changes)
    return PreparedRunFailure.model_validate(values)


def _run_result(**changes: Any) -> RunResultV1:
    run_kind = RunKindRef(kind="checker.run", version=1)
    version_tuple = VersionTuple(
        ir_snapshot_id="snapshot:1",
        tool_version="checker@1",
    )
    values: dict[str, Any] = {
        "run_id": "run:1",
        "attempt_no": 1,
        "run_kind": run_kind,
        "primary_artifact_id": "artifact:output",
        "produced_artifact_ids": ("artifact:output",),
        "finding_count": 0,
        "outcome_code": "passed",
        "summary": RunResultSummaryV1(
            outcome_code="passed",
            primary_artifact_kind="checker_run",
            produced_artifact_count=1,
            finding_count=0,
        ),
        "requirement_dispositions": (),
        "version_projection": RunManifestVersionProjectionV1(
            manifest_scope="run",
            attempt_no=1,
            run_kind=run_kind,
            run_payload_hash=_HASH_A,
            frozen_input_version_tuple=version_tuple,
            terminal_version_tuple=version_tuple,
            version_transition_policy_ref=VersionTransitionPolicyRefV1(
                policy_id="deterministic",
                policy_version=1,
                digest=_HASH_B,
            ),
            parents=(
                RunManifestParentBindingV1(
                    artifact_id="artifact:output",
                    role="output",
                    publication="run_published",
                    ordinal=1,
                ),
            ),
        ),
    }
    values.update(changes)
    return RunResultV1.model_validate(values)


def _run_failure(**changes: Any) -> RunFailureV1:
    run_kind = RunKindRef(kind="checker.run", version=1)
    version_tuple = VersionTuple(
        ir_snapshot_id="snapshot:1",
        tool_version="checker@1",
    )
    values: dict[str, Any] = {
        "run_id": "run:1",
        "attempt_no": 1,
        "run_kind": run_kind,
        "cause_code": "validation_failed",
        "failure_class": "validation",
        "retryable": False,
        "retry_decision": _retry_decision(),
        "redacted_message": "deterministic validation failed",
        "evidence_artifact_ids": (),
        "requirement_dispositions": (),
        "occurred_at": "2026-07-14T00:00:00Z",
        "version_projection": RunManifestVersionProjectionV1(
            manifest_scope="run",
            attempt_no=1,
            run_kind=run_kind,
            run_payload_hash=_HASH_A,
            frozen_input_version_tuple=version_tuple,
            terminal_version_tuple=version_tuple,
            version_transition_policy_ref=VersionTransitionPolicyRefV1(
                policy_id="deterministic",
                policy_version=1,
                digest=_HASH_B,
            ),
            parents=(),
        ),
    }
    values.update(changes)
    return RunFailureV1.model_validate(values)


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (PreparedArtifact, "lineage"),
        (PlannedAgentNodeVersionV1, "allowed_model_snapshots"),
        (ExecutionVersionPlanV1, "nodes"),
        (ResolvedPolicySnapshotV1, "requirements"),
        (PreparedRunResult, "artifacts"),
        (PreparedRunResult, "findings"),
        (PreparedRunResult, "requirement_dispositions"),
        (PreparedRunFailure, "artifacts"),
        (PreparedRunFailure, "requirement_dispositions"),
        (RunManifestVersionProjectionV1, "parents"),
        (RunResultV1, "produced_artifact_ids"),
        (RunResultV1, "requirement_dispositions"),
        (RunFailureV1, "evidence_artifact_ids"),
        (RunFailureV1, "requirement_dispositions"),
    ],
)
def test_run_result_collections_publish_frozen_max_items(
    model: type[BaseModel], field: str
) -> None:
    property_schema = model.model_json_schema()["properties"][field]

    assert property_schema["maxItems"] == _MAX_ITEMS


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (PlannedAgentNodeVersionV1, "agent_node_id"),
        (PlannedAgentNodeVersionV1, "prompt_version"),
        (PlannedAgentNodeVersionV1, "tool_version"),
        (ExecutionVersionPlanV1, "agent_graph_version"),
        (ResolvedArtifactRequirementV1, "requirement_id"),
        (ResolvedArtifactRequirementV1, "outcome_rule_id"),
        (ResolvedArtifactRequirementV1, "payload_schema_id"),
        (ResolvedPolicySnapshotV1, "resolved_policy_id"),
        (DependencyFailureV1, "dependency_id"),
        (DependencyFailureV1, "operation_code"),
        (DependencyFailureV1, "classifier_code"),
    ],
)
def test_nested_run_payload_and_failure_strings_publish_max_length(
    model: type[BaseModel],
    field: str,
) -> None:
    property_schema = model.model_json_schema()["properties"][field]

    assert property_schema["maxLength"] == _MAX_STRING_LENGTH


def test_prepared_artifact_rejects_oversized_lineage() -> None:
    with pytest.raises(ValidationError):
        _prepared_artifact(lineage=tuple(f"artifact:{index}" for index in range(_MAX_ITEMS + 1)))


def test_prepared_result_rejects_oversized_artifact_collection() -> None:
    artifact = _prepared_artifact()
    with pytest.raises(ValidationError):
        _prepared_result(
            artifacts=(artifact,) * (_MAX_ITEMS + 1),
            summary=PreparedRunResultSummaryV1(
                outcome_code="passed",
                primary_artifact_kind="checker_run",
                prepared_domain_artifact_count=_MAX_ITEMS + 1,
                prepared_finding_count=0,
            ),
        )


def test_prepared_result_rejects_oversized_finding_collection() -> None:
    finding = _prepared_finding()
    with pytest.raises(ValidationError):
        _prepared_result(
            findings=(finding,) * (_MAX_ITEMS + 1),
            summary=PreparedRunResultSummaryV1(
                outcome_code="passed",
                primary_artifact_kind="checker_run",
                prepared_domain_artifact_count=1,
                prepared_finding_count=_MAX_ITEMS + 1,
            ),
        )


def test_prepared_result_rejects_oversized_disposition_collection() -> None:
    with pytest.raises(ValidationError):
        _prepared_result(
            requirement_dispositions=tuple(_disposition(index) for index in range(_MAX_ITEMS + 1))
        )


def test_run_result_rejects_oversized_produced_artifact_collection() -> None:
    artifact_ids = tuple(f"artifact:{index}" for index in range(_MAX_ITEMS + 1))
    with pytest.raises(ValidationError):
        _run_result(
            primary_artifact_id=artifact_ids[0],
            produced_artifact_ids=artifact_ids,
            summary=RunResultSummaryV1(
                outcome_code="passed",
                primary_artifact_kind="checker_run",
                produced_artifact_count=len(artifact_ids),
                finding_count=0,
            ),
        )


def test_run_result_rejects_oversized_disposition_collection() -> None:
    with pytest.raises(ValidationError):
        _run_result(
            requirement_dispositions=tuple(_disposition(index) for index in range(_MAX_ITEMS + 1))
        )


def test_prepared_failure_rejects_oversized_artifact_collection() -> None:
    artifact = _prepared_artifact()
    with pytest.raises(ValidationError):
        _prepared_failure(artifacts=(artifact,) * (_MAX_ITEMS + 1))


def test_prepared_failure_rejects_oversized_disposition_collection() -> None:
    with pytest.raises(ValidationError):
        _prepared_failure(
            requirement_dispositions=tuple(_disposition(index) for index in range(_MAX_ITEMS + 1))
        )


def test_manifest_projection_rejects_oversized_parent_collection() -> None:
    projection = _run_result().version_projection
    with pytest.raises(ValidationError):
        RunManifestVersionProjectionV1.model_validate(
            {
                **projection.model_dump(mode="python"),
                "parents": tuple(
                    RunManifestParentBindingV1(
                        artifact_id=f"artifact:{index}",
                        role="evidence",
                        publication="existing",
                    )
                    for index in range(_MAX_ITEMS + 1)
                ),
            }
        )


def test_run_failure_rejects_oversized_evidence_collection() -> None:
    with pytest.raises(ValidationError):
        _run_failure(
            evidence_artifact_ids=tuple(f"artifact:{index}" for index in range(_MAX_ITEMS + 1))
        )


def test_run_failure_rejects_oversized_disposition_collection() -> None:
    with pytest.raises(ValidationError):
        _run_failure(
            requirement_dispositions=tuple(_disposition(index) for index in range(_MAX_ITEMS + 1))
        )


def _nested_meta(depth: int) -> dict[str, Any]:
    value: Any = "leaf"
    for _ in range(depth - 1):
        value = {"child": value}
    return value


@pytest.mark.parametrize(
    "meta",
    [
        {"value": "x" * (_MAX_STRING_LENGTH + 1)},
        {"x" * (_MAX_STRING_LENGTH + 1): "value"},
        {"items": list(range(_MAX_ITEMS + 1))},
        _nested_meta(33),
        {f"key:{index}": "x" * 64 for index in range(_MAX_ITEMS)},
    ],
    ids=[
        "string-length",
        "key-length",
        "nested-collection-size",
        "depth",
        "canonical-byte-size",
    ],
)
def test_prepared_artifact_meta_rejects_unbounded_json(meta: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _prepared_artifact(meta=meta)


def test_prepared_artifact_meta_preserves_ordinary_historical_payload() -> None:
    meta = {"source": "historical", "nested": {"items": [1, None, True]}}

    assert _prepared_artifact(meta=meta).meta == meta


@pytest.mark.parametrize(
    "build",
    [
        lambda value: _prepared_artifact(payload_schema_id=value),
        lambda value: _prepared_artifact(lineage=(value,)),
        lambda value: RequirementDispositionV1.model_validate(
            {**_disposition().model_dump(), "requirement_id": value}
        ),
        lambda value: _run_result(
            primary_artifact_id=value,
            produced_artifact_ids=(value,),
        ),
    ],
    ids=[
        "payload-schema-id",
        "lineage-id",
        "requirement-id",
        "produced-artifact-id",
    ],
)
def test_direct_run_result_strings_are_bounded(
    build: Callable[[str], BaseModel],
) -> None:
    with pytest.raises(ValidationError):
        build("x" * (_MAX_STRING_LENGTH + 1))


@pytest.mark.parametrize(
    "build",
    [
        lambda value: _prepared_failure(run_id=value),
        lambda value: _prepared_failure(cause_code=value),
        lambda value: _prepared_failure(redacted_message=value),
        lambda value: _run_failure(run_id=value),
        lambda value: _run_failure(
            cause_code=value,
            retry_decision=_retry_decision(cause_code=value),
        ),
        lambda value: _run_failure(redacted_message=value),
        lambda value: _run_failure(occurred_at=value),
        lambda value: _run_failure(evidence_artifact_ids=(value,)),
        lambda value: RunManifestParentBindingV1(
            artifact_id=value,
            role="input",
            publication="existing",
        ),
    ],
    ids=[
        "prepared-run-id",
        "prepared-cause-code",
        "prepared-message",
        "failure-run-id",
        "failure-cause-code",
        "failure-message",
        "failure-occurred-at",
        "failure-evidence-id",
        "manifest-parent-id",
    ],
)
def test_failure_and_manifest_strings_are_bounded(
    build: Callable[[str], BaseModel],
) -> None:
    with pytest.raises(ValidationError):
        build("x" * (_MAX_STRING_LENGTH + 1))


@pytest.mark.parametrize(
    "errors",
    [
        ({"message": "x" * (_MAX_STRING_LENGTH + 1)},),
        ({"x" * (_MAX_STRING_LENGTH + 1): "value"},),
        ({"items": list(range(_MAX_ITEMS + 1))},),
        (_nested_meta(33),),
        ({f"key:{index}": "x" * 64 for index in range(_MAX_ITEMS)},),
        tuple({"message": "x" * 64} for _ in range(_MAX_ITEMS)),
    ],
    ids=[
        "string-length",
        "key-length",
        "nested-collection-size",
        "depth",
        "canonical-byte-size",
        "aggregate-canonical-byte-size",
    ],
)
def test_problem_errors_reject_unbounded_json(
    errors: tuple[dict[str, Any], ...],
) -> None:
    with pytest.raises(ValidationError):
        Problem(
            type="https://gameforge.dev/problems/invalid-request",
            title="Invalid request",
            status=422,
            detail="Request validation failed",
            instance="/api/v1/runs",
            code="request.invalid",
            request_id="request:1",
            errors=errors,
        )


def test_problem_errors_preserve_ordinary_bounded_json() -> None:
    errors = ({"location": ["body", "kind"], "message": "required"},)

    problem = Problem(
        type="https://gameforge.dev/problems/invalid-request",
        title="Invalid request",
        status=422,
        detail="Request validation failed",
        instance="/api/v1/runs",
        code="request.invalid",
        request_id="request:1",
        errors=errors,
    )

    assert problem.errors == errors
