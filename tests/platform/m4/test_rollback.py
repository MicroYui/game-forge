from __future__ import annotations

import pytest

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ExecutionProfileLifecycleV1,
    GenericProfileDetailsV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    canonical_config_hash,
    execution_profile_payload_hash,
)
from gameforge.contracts.jobs import (
    FailureClassifierRefV1,
    RetryPolicyRefV1,
    RollbackValidationPayloadV1,
    RunPayloadEnvelope,
    RunRecord,
    ValidationSubjectBindingV1,
    canonical_payload_hash,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    EvidenceSet,
    RollbackRequestV1,
    RollbackTargetBindingV1,
)
from gameforge.platform.approvals.apply import ExactRollbackExecutionVerifier
from tests.platform.m4.apply_testkit import authority_snapshot, harness, request


HASH_A = "a" * 64
HASH_B = "b" * 64


class _Runs:
    def __init__(self, run: RunRecord) -> None:
        self.run = run

    def get(self, run_id: str) -> RunRecord | None:
        return self.run if run_id == self.run.run_id else None


class _Profiles:
    def __init__(
        self,
        definition: ExecutionProfileDefinitionV1,
        lifecycle: ExecutionProfileLifecycleV1,
    ) -> None:
        self.definition = definition
        self.lifecycle = lifecycle

    def resolve_execution_profile_binding(
        self,
        binding: ResolvedExecutionProfileBindingV1,
    ) -> tuple[ExecutionProfileDefinitionV1, ExecutionProfileLifecycleV1]:
        assert binding.profile == self.definition.profile
        return self.definition, self.lifecycle


def _replace_item(item: ApprovalItem, **updates: object) -> ApprovalItem:
    return ApprovalItem.model_validate({**item.model_dump(mode="python"), **updates})


def _exact_execution_fixture() -> tuple[
    ExactRollbackExecutionVerifier,
    ApprovalItem,
    RollbackRequestV1,
    EvidenceSet,
    _Runs,
    _Profiles,
]:
    subject = harness("rollback_request")
    item = subject.scenario.item
    rollback = subject.scenario.rollback_request
    evidence = subject.state.evidence_sets[item.evidence_set_artifact_id or ""]
    assert rollback is not None
    profile = rollback.rollback_profile_binding.profile
    definition = ExecutionProfileDefinitionV1(
        profile=profile,
        profile_kind="rollback",
        compatible_run_kinds=(RunKindRef(kind="rollback.validate", version=1),),
        domain_scope=item.domain_scope,
        stochastic=False,
        input_schema_ids=("rollback-validation@1",),
        output_schema_ids=("validation-evidence@1",),
        required_capabilities=("ref-history-read",),
        display_name="Rollback verifier",
        handler_key="rollback.verify@1",
        config_schema_id="rollback-config@1",
        config={},
        config_hash=canonical_config_hash({}),
        details=GenericProfileDetailsV1(),
    )
    exact_binding = rollback.rollback_profile_binding.model_copy(
        update={"profile_payload_hash": execution_profile_payload_hash(definition)}
    )
    rollback = rollback.model_copy(update={"rollback_profile_binding": exact_binding})
    target_binding = item.target_binding
    assert isinstance(target_binding, RollbackTargetBindingV1)
    target_binding = target_binding.model_copy(update={"rollback_profile_binding": exact_binding})
    item = _replace_item(item, target_binding=target_binding)
    evidence = evidence.model_copy(update={"target_binding": target_binding})
    compatibility_profile = ProfileRefV1(
        profile_id="schema-compatibility.default",
        version=1,
    )
    compatibility_binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/schema_compatibility_policy",
        profile=compatibility_profile,
        expected_profile_kind="schema_compatibility",
        profile_payload_hash=HASH_A,
        catalog_version=exact_binding.catalog_version,
        catalog_digest=exact_binding.catalog_digest,
    )
    params = RollbackValidationPayloadV1(
        subject=ValidationSubjectBindingV1(
            approval_id=item.approval_id,
            expected_workflow_revision=2,
            subject_head_revision=1,
            subject_artifact_id=item.subject_artifact_id,
            subject_digest=item.subject_digest,
            active_validation_run_id=evidence.validation_run_id,
        ),
        ref_name=rollback.ref_name,
        expected_current_ref=rollback.expected_current_ref,
        target_artifact_id=rollback.target_artifact_id,
        target_history_revision=rollback.target_history_revision,
        rollback_profile=profile,
        schema_compatibility_policy=compatibility_profile,
        impact_profiles=(),
        regression_suite_artifact_ids=(),
    )
    payload = RunPayloadEnvelope(
        payload_schema_version="rollback-validation@1",
        input_artifact_ids=tuple(
            sorted(
                {
                    item.subject_artifact_id,
                    rollback.expected_current_ref.artifact_id,
                    rollback.target_artifact_id,
                }
            )
        ),
        version_tuple=VersionTuple(tool_version="rollback-validator@1"),
        policy_bindings=(),
        schema_bindings=(),
        execution_profile_catalog_version=exact_binding.catalog_version,
        execution_profile_catalog_digest=exact_binding.catalog_digest,
        resolved_profiles=(exact_binding, compatibility_binding),
        resolved_policy_snapshots=(),
        budget_set_snapshot_id="budget-set:rollback",
        llm_execution_mode="not_applicable",
        params=params,
    )
    run = RunRecord(
        run_id=evidence.validation_run_id,
        kind=RunKindRef(kind="rollback.validate", version=1),
        status="succeeded",
        revision=6,
        idempotency_scope="approval:rollback",
        idempotency_key="validation:rollback",
        request_hash=HASH_A,
        payload=payload,
        payload_hash=canonical_payload_hash(payload),
        run_kind_definition_digest=HASH_A,
        outcome_policy_set_digest=HASH_B,
        failure_classifier=FailureClassifierRefV1(
            classifier_version=1,
            classifier_digest=HASH_A,
        ),
        initiated_by=item.proposer,
        queue_deadline_utc="2026-07-14T12:01:00Z",
        attempt_timeout_ns=1_000_000_000,
        overall_deadline_utc="2026-07-14T12:10:00Z",
        current_attempt_no=None,
        next_attempt_no=2,
        next_fencing_token=2,
        next_event_seq=4,
        budget_set_snapshot_id=payload.budget_set_snapshot_id,
        run_budget_hold_group_id="budget-hold:rollback",
        retry_policy=RetryPolicyRefV1(
            retry_policy_id="retry:none",
            retry_policy_version=1,
            retry_policy_digest=HASH_B,
        ),
        max_attempts=1,
        result_artifact_id="artifact:rollback-validation-result",
        created_at="2026-07-14T12:00:00Z",
        updated_at="2026-07-14T12:00:10Z",
    )
    lifecycle = ExecutionProfileLifecycleV1(
        profile=profile,
        state="active",
        revision=1,
        changed_at="2026-07-14T11:00:00Z",
    )
    runs = _Runs(run)
    profiles = _Profiles(definition, lifecycle)
    verifier = ExactRollbackExecutionVerifier(runs=runs, profiles=profiles)
    return verifier, item, rollback, evidence, runs, profiles


def test_approved_rollback_moves_ref_records_transition_and_marks_reversed_item() -> None:
    subject = harness("rollback_request", with_reversed_item=True)
    request_payload = subject.scenario.rollback_request
    reversed_before = subject.scenario.reversed_item
    assert request_payload is not None and reversed_before is not None

    result = subject.service.apply(request(subject))

    assert result.approval_item.status == "applied"
    assert result.ref_value == RefValue(
        artifact_id=request_payload.target_artifact_id,
        revision=request_payload.expected_current_ref.revision + 1,
    )
    assert result.ref_transition is not None
    assert result.ref_transition.from_ref == request_payload.expected_current_ref
    assert result.ref_transition.to_ref == result.ref_value
    assert result.ref_transition.approval_item_id == subject.scenario.item.approval_id
    assert result.reversed_approval_item is not None
    assert result.reversed_approval_item.status == "rolled_back"
    assert result.reversed_approval_item.applied_at == reversed_before.applied_at
    assert subject.rollback_execution.calls == 1
    assert len(subject.state.transitions) == 1
    assert subject.state.audit[0][0] == "approval.rollback_applied"


def test_rollback_requires_exact_historical_membership() -> None:
    subject = harness("rollback_request")
    rollback = subject.scenario.rollback_request
    assert rollback is not None
    subject.state.history[(rollback.ref_name, rollback.target_history_revision)] = RefValue(
        artifact_id="artifact:other",
        revision=rollback.target_history_revision,
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(Conflict, match="history"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_rollback_exact_execution_profile_binding_is_revalidated() -> None:
    subject = harness("rollback_request")
    subject.rollback_execution.fail = True
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="execution binding"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_rollback_audit_failure_rolls_back_ref_history_transition_and_items() -> None:
    subject = harness("rollback_request", with_reversed_item=True)
    subject.audit.fail = True
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="audit unavailable"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before
    assert subject.uow.rollbacks == 1


def test_rollback_rejects_reversed_item_from_another_ref_revision() -> None:
    subject = harness("rollback_request", with_reversed_item=True)
    reversed_item = subject.scenario.reversed_item
    assert reversed_item is not None and reversed_item.target_binding is not None
    drifted_binding = reversed_item.target_binding.model_copy(
        update={
            "expected_ref": RefValue(
                artifact_id="artifact:older-base",
                revision=7,
            )
        }
    )
    drifted = _replace_item(reversed_item, target_binding=drifted_binding)
    subject.state.approvals[reversed_item.approval_id] = drifted
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="current ref revision"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_rollback_never_exposes_a_lineage_write_capability() -> None:
    subject = harness("rollback_request")

    assert "lineage" not in subject.service.capability_names


def test_exact_rollback_execution_verifier_closes_run_payload_and_profile_catalog() -> None:
    verifier, item, rollback, evidence, _, _ = _exact_execution_fixture()

    verifier.validate(item=item, request=rollback, evidence_set=evidence)


def test_exact_rollback_execution_verifier_rejects_history_revision_drift() -> None:
    verifier, item, rollback, evidence, runs, _ = _exact_execution_fixture()
    params = runs.run.payload.params
    assert isinstance(params, RollbackValidationPayloadV1)
    drifted_params = params.model_copy(
        update={"target_history_revision": params.target_history_revision + 1}
    )
    drifted_payload = runs.run.payload.model_copy(update={"params": drifted_params})
    runs.run = runs.run.model_copy(
        update={
            "payload": drifted_payload,
            "payload_hash": canonical_payload_hash(drifted_payload),
        }
    )

    with pytest.raises(IntegrityViolation, match="differs from RollbackRequest"):
        verifier.validate(item=item, request=rollback, evidence_set=evidence)


def test_exact_rollback_execution_verifier_rejects_replay_only_for_live_validation() -> None:
    verifier, item, rollback, evidence, _, profiles = _exact_execution_fixture()
    profiles.lifecycle = profiles.lifecycle.model_copy(update={"state": "replay_only"})

    with pytest.raises(IntegrityViolation, match="lifecycle"):
        verifier.validate(item=item, request=rollback, evidence_set=evidence)


def test_exact_rollback_execution_verifier_rejects_profile_definition_drift() -> None:
    verifier, item, rollback, evidence, _, profiles = _exact_execution_fixture()
    profiles.definition = profiles.definition.model_copy(
        update={"display_name": "A different retained definition"}
    )

    with pytest.raises(IntegrityViolation, match="profile definition"):
        verifier.validate(item=item, request=rollback, evidence_set=evidence)


def test_exact_rollback_execution_verifier_rejects_profile_domain_mismatch() -> None:
    verifier, item, rollback, evidence, runs, profiles = _exact_execution_fixture()
    definition = profiles.definition.model_copy(
        update={"domain_scope": DomainScope(domain_ids=("narrative",))}
    )
    binding = rollback.rollback_profile_binding.model_copy(
        update={"profile_payload_hash": execution_profile_payload_hash(definition)}
    )
    rollback = rollback.model_copy(update={"rollback_profile_binding": binding})
    target_binding = item.target_binding
    assert isinstance(target_binding, RollbackTargetBindingV1)
    target_binding = target_binding.model_copy(update={"rollback_profile_binding": binding})
    item = _replace_item(item, target_binding=target_binding)
    evidence = evidence.model_copy(update={"target_binding": target_binding})
    payload = runs.run.payload.model_copy(
        update={
            "resolved_profiles": tuple(
                binding if resolved.field_path == binding.field_path else resolved
                for resolved in runs.run.payload.resolved_profiles
            )
        }
    )
    runs.run = runs.run.model_copy(
        update={
            "payload": payload,
            "payload_hash": canonical_payload_hash(payload),
        }
    )
    profiles.definition = definition

    with pytest.raises(IntegrityViolation, match="profile definition"):
        verifier.validate(item=item, request=rollback, evidence_set=evidence)
