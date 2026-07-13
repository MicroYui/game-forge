from __future__ import annotations

import pytest

from gameforge.contracts.errors import Conflict, Forbidden, IntegrityViolation
from gameforge.contracts.execution_profiles import (
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    AutoApplyProofBindingV1,
)
from tests.platform.m4.apply_testkit import (
    authority_snapshot,
    context,
    harness,
    principal,
    request,
)


def _replace_item(item: ApprovalItem, **updates: object) -> ApprovalItem:
    return ApprovalItem.model_validate({**item.model_dump(mode="python"), **updates})


def _make_auto_apply_eligible(subject: object) -> ApprovalItem:
    item = subject.scenario.item  # type: ignore[attr-defined]
    binding = item.target_binding
    assert binding is not None
    proof = AutoApplyProofBindingV1(
        proof_artifact_id="artifact:auto-apply-proof",
        policy=AutoApplyPolicyRefV1(
            registry=AutoApplyPolicyRegistryRefV1(
                registry_version="auto-apply-registry@1",
                registry_digest="a" * 64,
            ),
            policy_id="auto-apply.safe",
            policy_version="auto-apply.safe@1",
            policy_digest="b" * 64,
        ),
        subject_digest=item.subject_digest,
        target_digest=binding.target_digest,
        expected_ref=binding.expected_ref,
        validation_evidence_artifact_id=item.evidence_set_artifact_id or "",
    )
    replacement = _replace_item(
        item,
        status="auto_apply_eligible",
        decisions=(),
        decided_at=None,
        auto_apply_proof=proof,
    )
    subject.state.approvals[item.approval_id] = replacement  # type: ignore[attr-defined]
    subject.scenario.item = replacement  # type: ignore[attr-defined]
    return replacement


@pytest.mark.parametrize("kind", ["patch", "constraint_proposal"])
def test_approved_apply_atomically_moves_ref_and_item_without_republishing_target(
    kind: str,
) -> None:
    subject = harness(kind)  # type: ignore[arg-type]
    before_artifacts = dict(subject.state.artifacts)

    result = subject.service.apply(request(subject))

    assert result.approval_item.status == "applied"
    assert result.approval_item.workflow_revision == subject.scenario.item.workflow_revision + 1
    assert result.approval_item.applied_at == "2026-07-14T12:00:00Z"
    assert result.ref_value == RefValue(
        artifact_id=subject.scenario.target.artifact_id,
        revision=subject.scenario.expected_ref.revision + 1,
    )
    assert result.ref_transition is None
    assert subject.state.artifacts == before_artifacts
    assert subject.state.audit[0][0] == "approval.applied"
    assert subject.uow.commits == 1


def test_apply_reauthorizes_the_immutable_approval_decisions() -> None:
    subject = harness("patch")
    subject.state.principals["human:reviewer"] = principal(
        "human:reviewer",
        active=False,
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(Forbidden):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before
    assert subject.uow.rollbacks == 1


def test_apply_rejects_an_executor_missing_from_current_identity_state() -> None:
    subject = harness("patch")
    subject.state.principals.pop("human:operator")
    before = authority_snapshot(subject.state)

    with pytest.raises(Forbidden, match="executor"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_apply_rejects_current_actor_after_apply_permission_is_revoked() -> None:
    subject = harness("patch")
    operator = subject.state.principals["human:operator"]
    subject.state.principals["human:operator"] = operator.model_copy(
        update={"roles": (), "authz_revision": operator.authz_revision + 1}
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(Forbidden, match="lacks the current resource permission"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_apply_does_not_substitute_initiator_permission_for_executor_permission() -> None:
    subject = harness("patch")
    operator = subject.state.principals["human:operator"]
    subject.state.principals["human:operator"] = operator.model_copy(
        update={"roles": (), "authz_revision": operator.authz_revision + 1}
    )
    command_context = context().model_copy(
        update={"initiated_by": subject.scenario.item.decisions[0].actor}
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(Forbidden, match="executor lacks"):
        subject.service.apply(request(subject, context_value=command_context))

    assert authority_snapshot(subject.state) == before


@pytest.mark.parametrize(
    ("fact_updates", "message"),
    [
        (
            {
                "produced_by": "agent",
                "producer_run_id": "run:agent-proposal",
            },
            "superseding human author revision",
        ),
        (
            {
                "supersedes_artifact_id": None,
            },
            "superseding human author revision",
        ),
    ],
)
def test_constraint_publish_revalidates_human_superseding_revision(
    fact_updates: dict[str, object],
    message: str,
) -> None:
    subject = harness("constraint_proposal")
    artifact_id = subject.scenario.subject.artifact_id
    subject.state.facts[artifact_id] = subject.state.facts[artifact_id].model_copy(
        update=fact_updates
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match=message):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_constraint_publish_rejects_a_payload_bound_to_another_predecessor() -> None:
    subject = harness("constraint_proposal")
    artifact_id = subject.scenario.subject.artifact_id
    subject.state.facts[artifact_id] = subject.state.facts[artifact_id].model_copy(
        update={"supersedes_artifact_id": "artifact:not-the-predecessor"}
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="exact superseding human author revision"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_constraint_publish_rejects_a_missing_predecessor_approval() -> None:
    subject = harness("constraint_proposal")
    predecessor_id = subject.scenario.item.supersedes_approval_id
    assert predecessor_id is not None
    subject.state.approvals.pop(predecessor_id)
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="exact superseding human author revision"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_constraint_publish_rejects_a_consistent_initial_revision() -> None:
    subject = harness("constraint_proposal")
    item = _replace_item(subject.scenario.item, subject_revision=1)
    subject.state.approvals[item.approval_id] = item
    subject.scenario.item = item
    artifact_id = subject.scenario.subject.artifact_id
    subject.state.facts[artifact_id] = subject.state.facts[artifact_id].model_copy(
        update={"subject_revision": 1, "supersedes_artifact_id": None}
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="superseding human author revision"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_constraint_publish_requires_a_human_revision_proposer() -> None:
    subject = harness("constraint_proposal")
    item = subject.scenario.item
    service_proposer = item.proposer.model_copy(
        update={
            "principal_id": "service:constraint-publisher",
            "principal_kind": "service",
        }
    )
    item = _replace_item(item, proposer=service_proposer)
    subject.state.approvals[item.approval_id] = item
    subject.scenario.item = item
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="superseding human author revision"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_auto_apply_eligible_patch_reruns_the_exact_guard_at_apply() -> None:
    subject = harness("patch")
    _make_auto_apply_eligible(subject)

    result = subject.service.apply(request(subject))

    assert result.approval_item.status == "applied"
    assert subject.auto_apply.calls == 1


def test_auto_apply_guard_failure_leaves_authority_unchanged() -> None:
    subject = harness("patch")
    _make_auto_apply_eligible(subject)
    subject.auto_apply.fail = True
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="auto-apply proof rejected"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before
    assert subject.auto_apply.calls == 1


def test_apply_rejects_stale_ref_without_partial_workflow_or_audit_writes() -> None:
    subject = harness("patch")
    subject.state.refs["content/head"] = RefValue(
        artifact_id=subject.scenario.expected_ref.artifact_id,
        revision=subject.scenario.expected_ref.revision + 1,
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(Conflict):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before
    assert subject.uow.rollbacks == 1


def test_apply_rejects_stale_workflow_revision_without_partial_writes() -> None:
    subject = harness("patch")
    command = request(subject).model_copy(
        update={"expected_workflow_revision": (subject.scenario.item.workflow_revision + 1)}
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(Conflict, match="workflow revision"):
        subject.service.apply(command)

    assert authority_snapshot(subject.state) == before


def test_apply_rejects_noncurrent_subject_head_without_partial_writes() -> None:
    subject = harness("patch")
    item = subject.scenario.item
    other = _replace_item(item, approval_id="approval:other")
    subject.state.approvals[other.approval_id] = other
    head = subject.state.heads[item.subject_series_id]
    subject.state.heads[item.subject_series_id] = head.model_copy(
        update={"current_approval_id": other.approval_id}
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(Conflict, match="current SubjectHead"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_apply_rejects_unreadable_target_without_moving_authority() -> None:
    subject = harness("patch")
    subject.targets.fail = True
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="schema unreadable"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_apply_rejects_subject_digest_drift_from_the_retained_artifact() -> None:
    subject = harness("patch")
    item = subject.scenario.item
    drifted = _replace_item(item, subject_digest="f" * 64)
    subject.state.approvals[item.approval_id] = drifted
    subject.scenario.item = drifted
    evidence_id = drifted.evidence_set_artifact_id
    assert evidence_id is not None
    evidence = subject.state.evidence_sets[evidence_id]
    subject.state.evidence_sets[evidence_id] = evidence.model_copy(
        update={"subject_digest": drifted.subject_digest}
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="subject Artifact differs"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_apply_rejects_target_object_bytes_drift() -> None:
    subject = harness("patch")
    subject.state.payloads[subject.scenario.target.artifact_id] = b"corrupt"
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="target payload differs"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_apply_rejects_evidence_target_binding_drift() -> None:
    subject = harness("patch")
    item = subject.scenario.item
    evidence_id = item.evidence_set_artifact_id
    binding = item.target_binding
    assert evidence_id is not None and binding is not None
    evidence = subject.state.evidence_sets[evidence_id]
    subject.state.evidence_sets[evidence_id] = evidence.model_copy(
        update={"target_binding": binding.model_copy(update={"target_digest": "f" * 64})}
    )
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="EvidenceSet differs"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_apply_rejects_missing_exact_policy_history() -> None:
    subject = harness("patch")
    item = subject.scenario.item
    drifted = _replace_item(
        item,
        approval_policy=ApprovalPolicyRefV1(
            policy_version=item.approval_policy.policy_version,
            policy_digest="f" * 64,
        ),
    )
    subject.state.approvals[item.approval_id] = drifted
    subject.scenario.item = drifted
    before = authority_snapshot(subject.state)

    with pytest.raises(IntegrityViolation, match="policy history"):
        subject.service.apply(request(subject))

    assert authority_snapshot(subject.state) == before


def test_apply_exact_idempotent_replay_does_not_append_history_or_audit() -> None:
    subject = harness("patch")
    command = request(subject)
    first = subject.service.apply(command)
    after_first = authority_snapshot(subject.state)

    second = subject.service.apply(command)

    assert second == first
    assert authority_snapshot(subject.state) == after_first
    assert subject.uow.commits == 2


def test_apply_same_idempotency_key_with_another_hash_conflicts() -> None:
    subject = harness("patch")
    subject.service.apply(request(subject))
    conflicting = request(
        subject,
        context_value=context(key="apply:1", request_hash="f" * 64),
    )

    with pytest.raises(Conflict):
        subject.service.apply(conflicting)
