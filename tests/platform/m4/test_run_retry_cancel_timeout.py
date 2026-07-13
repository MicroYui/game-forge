from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from gameforge.contracts.errors import Conflict, IntegrityViolation, InvalidStateTransition
from gameforge.contracts.jobs import (
    CancelRequestedDataV1,
    CancelRunPayloadV1,
    DependencyFailureV1,
    PreparedArtifact,
    PreparedRunFailure,
    PreparedRunResult,
    PreparedRunResultSummaryV1,
    RetryScheduledDataV1,
    RunCommandV1,
    RunQueuedDataV1,
    RunRecord,
)
from gameforge.contracts.lineage import (
    AuditActor,
    ObjectLocation,
    ObjectRef,
    VersionTuple,
)
from gameforge.platform.runs.commands import RunClaimRequest
from gameforge.platform.runs.lifecycle import (
    PublishAttemptOutcomeRequest,
    ReapExpiredLeaseRequest,
    SweepRunTimeoutRequest,
)
from tests.platform.m4.test_run_create_claim import _definition, _retry_policy
from tests.platform.m4.test_run_fencing import (
    NOW_DT,
    WORKER,
    _fence,
    _harness,
    _record_payload,
    _start,
)


_HASH_A = "a" * 64
_HUMAN = AuditActor(principal_id="human:a", principal_kind="human")


def _run_harness(**kwargs):
    run_payload = kwargs.get("run_payload")
    modes = (
        (run_payload.llm_execution_mode,)
        if run_payload is not None
        else ("not_applicable",)
    )
    definition = _definition(_retry_policy()).model_copy(
        update={
            "allowed_command_schema_ids": ("run-cancel@1",),
            "allowed_llm_execution_modes": modes,
        }
    )
    return _harness(definition=definition, **kwargs)


def _event_types(harness) -> tuple[str, ...]:
    return tuple(
        event.event_type
        for event in harness.repo.list_events("run:1", after_seq=0, limit=100)
    )


def _prepared_failure(
    harness,
    *,
    cause_code: str,
    failure_class: str,
    intrinsic_retry_eligible: bool,
) -> PreparedRunFailure:
    dependency = None
    if failure_class in {"transient_dependency", "permanent_dependency"}:
        dependency = DependencyFailureV1(
            dependency_kind="model_provider",
            dependency_id="provider:primary",
            operation_code="responses.create",
            classifier_code=cause_code,
        )
    run = harness.state.runs["run:1"]
    return PreparedRunFailure(
        run_id=run.run_id,
        attempt_no=run.current_attempt_no,
        run_kind=run.kind,
        artifacts=(),
        requirement_dispositions=(),
        cause_code=cause_code,
        failure_class=failure_class,
        intrinsic_retry_eligible=intrinsic_retry_eligible,
        classifier=run.failure_classifier,
        dependency=dependency,
        redacted_message="worker outcome",
    )


def _prepared_success(harness) -> PreparedRunResult:
    run = harness.state.runs["run:1"]
    key = f"objects/v1/sha256/{_HASH_A[:2]}/{_HASH_A}"
    artifact = PreparedArtifact(
        kind="checker_run",
        payload_schema_id="checker-report@1",
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:input",
            tool_version="checker@1",
        ),
        lineage=("artifact:input",),
        payload_hash=_HASH_A,
        meta={},
        object_ref=ObjectRef(key=key, sha256=_HASH_A, size_bytes=1),
        location=ObjectLocation(
            store_id="local",
            key=key,
            backend_generation="generation:1",
        ),
    )
    return PreparedRunResult(
        run_id=run.run_id,
        attempt_no=run.current_attempt_no,
        run_kind=run.kind,
        primary_index=0,
        artifacts=(artifact,),
        findings=(),
        requirement_dispositions=(),
        summary=PreparedRunResultSummaryV1(
            outcome_code="checker_completed",
            primary_artifact_kind="checker_run",
            prepared_domain_artifact_count=1,
            prepared_finding_count=0,
        ),
    )


def _publish_failure(
    harness,
    *,
    at=NOW_DT + timedelta(seconds=1),
    cause_code: str = "dependency_unavailable",
    failure_class: str = "transient_dependency",
    intrinsic_retry_eligible: bool = True,
):
    return harness.service_at(at).publish_attempt_outcome(
        PublishAttemptOutcomeRequest(
            fence=_fence(harness),
            prepared_outcome=_prepared_failure(
                harness,
                cause_code=cause_code,
                failure_class=failure_class,
                intrinsic_retry_eligible=intrinsic_retry_eligible,
            ),
            actor=WORKER,
        )
    )


def _cancel_command(harness, *, command_id: str = "command:cancel:1") -> RunCommandV1:
    run = harness.state.runs["run:1"]
    return RunCommandV1(
        command_id=command_id,
        client_id="browser:a",
        client_seq=1,
        idempotency_key=command_id,
        expected_run_revision=run.revision,
        type="cancel",
        payload_schema_id="run-cancel@1",
        payload=CancelRunPayloadV1(reason_code="user_requested"),
    )


def _submit_cancel(harness, *, at=NOW_DT + timedelta(seconds=1)):
    return harness.command_service_at(at).submit(
        run_id="run:1",
        command=_cancel_command(harness),
        actor=_HUMAN,
    )


def _as_queued(harness, *, queue_deadline: str = "2026-07-14T12:10:00Z") -> None:
    run = harness.state.runs["run:1"]
    harness.state.runs[run.run_id] = RunRecord.model_validate(
        {
            **run.model_dump(mode="python"),
            "status": "queued",
            "revision": 1,
            "queue_deadline_utc": queue_deadline,
            "current_attempt_no": None,
            "next_attempt_no": 1,
            "next_fencing_token": 1,
            "next_event_seq": 2,
            "concurrency_permit_group_id": None,
            "updated_at": run.created_at,
        }
    )
    queued = harness.state.events[(run.run_id, 1)]
    harness.state.events = {
        (run.run_id, 1): queued.model_copy(
            update={
                "data": RunQueuedDataV1(
                    run_kind=run.kind,
                    queue_deadline_utc=queue_deadline,
                    overall_deadline_utc=run.overall_deadline_utc,
                )
            }
        )
    }
    harness.state.attempts.clear()
    harness.state.leases.clear()
    harness.state.permit = None


def test_transient_failure_closes_attempt_and_persists_exact_retry_projection() -> None:
    harness = _run_harness()
    _start(harness)

    result = _publish_failure(harness)

    run = harness.state.runs["run:1"]
    attempt = harness.state.attempts[("run:1", 1)]
    lease = harness.state.leases["lease:1"]
    retry_event = harness.state.events[("run:1", 4)]
    assert run.status == "retry_wait"
    assert run.current_attempt_no is None
    assert run.failure_artifact_id is None
    assert run.concurrency_permit_group_id is None
    assert run.next_attempt_no == 2
    assert run.next_fencing_token == 2
    assert attempt.status == "failed"
    assert attempt.failure_class == "transient_dependency"
    assert attempt.retryable is True
    assert attempt.failure_artifact_id == result.attempt_failure_artifact_id
    assert lease.status == "closed"
    assert retry_event.event_type == "attempt.retry_scheduled"
    assert isinstance(retry_event.data, RetryScheduledDataV1)
    assert retry_event.data.retry_decision.decision == "retry"
    assert retry_event.data.retry_decision.reason_code == "transient_eligible"
    assert retry_event.data.retry_decision.retry_not_before_utc == run.retry_not_before_utc
    assert retry_event.data.retry_not_before_utc == run.retry_not_before_utc
    assert retry_event.data.failure_artifact_id == attempt.failure_artifact_id
    assert _event_types(harness)[-1:] == ("attempt.retry_scheduled",)


def test_nonretryable_failure_keeps_intrinsic_reason_on_the_last_attempt() -> None:
    harness = _run_harness()
    _start(harness)
    first_fence = _fence(harness)
    run = harness.state.runs["run:1"]
    first_attempt = harness.state.attempts.pop(("run:1", 1))
    first_lease = harness.state.leases["lease:1"]
    last_attempt_no = run.max_attempts
    harness.state.runs["run:1"] = RunRecord.model_validate(
        {
            **run.model_dump(mode="python"),
            "current_attempt_no": last_attempt_no,
            "next_attempt_no": last_attempt_no + 1,
            "next_fencing_token": last_attempt_no + 1,
        }
    )
    harness.state.attempts[("run:1", last_attempt_no)] = first_attempt.model_copy(
        update={"attempt_no": last_attempt_no, "fencing_token": last_attempt_no}
    )
    harness.state.leases["lease:1"] = first_lease.model_copy(
        update={"attempt_no": last_attempt_no, "fencing_token": last_attempt_no}
    )
    fence = first_fence.model_copy(
        update={"attempt_no": last_attempt_no, "fencing_token": last_attempt_no}
    )

    result = harness.service_at(NOW_DT + timedelta(seconds=1)).publish_attempt_outcome(
        PublishAttemptOutcomeRequest(
            fence=fence,
            prepared_outcome=_prepared_failure(
                harness,
                cause_code="execution_failed",
                failure_class="execution",
                intrinsic_retry_eligible=False,
            ),
            actor=WORKER,
        )
    )

    assert result.run.status == "failed"
    assert result.retry_decision is not None
    assert result.retry_decision.reason_code == "not_retry_eligible"


def test_retry_backoff_crossing_overall_deadline_keeps_dependency_failure_status() -> None:
    harness = _run_harness(
        overall_deadline_utc="2026-07-14T12:00:01.050000Z",
    )
    _start(harness)

    result = _publish_failure(harness, at=NOW_DT + timedelta(seconds=1))

    assert result.run.status == "failed"
    assert result.retry_decision is not None
    assert result.retry_decision.reason_code == "overall_deadline_exhausted"
    assert result.attempt_failure_artifact_id is not None
    assert result.run_failure_artifact_id is not None


def test_subject_superseded_closes_the_attempt_and_run_as_cancelled() -> None:
    harness = _run_harness()
    _start(harness)

    result = _publish_failure(
        harness,
        cause_code="subject_superseded",
        failure_class="subject_superseded",
        intrinsic_retry_eligible=False,
    )

    assert result.run.status == "cancelled"
    assert result.attempt is not None
    assert result.attempt.status == "cancelled"
    assert result.retry_decision is not None
    assert result.retry_decision.reason_code == "not_retry_eligible"


def test_retry_wait_claim_is_not_eligible_early_and_allocates_new_heads_only_when_due() -> None:
    harness = _run_harness()
    _start(harness)
    _publish_failure(harness)
    retry_wait = harness.state.runs["run:1"]
    assert retry_wait.next_attempt_no == 2
    assert retry_wait.next_fencing_token == 2

    request = RunClaimRequest(
        worker=AuditActor(
            principal_id="service:worker:2",
            principal_kind="service",
        ),
        lease_id="lease:2",
        lease_duration_ns=10_000_000_000,
        trace_id="trace:attempt:2",
    )
    assert harness.command_service_at(
        NOW_DT + timedelta(seconds=1, microseconds=50_000)
    ).claim_next(request) is None
    assert harness.state.runs["run:1"] == retry_wait

    claim = harness.command_service_at(
        NOW_DT + timedelta(seconds=1, microseconds=100_000)
    ).claim_next(request)

    assert claim is not None
    assert claim.run.status == "leased"
    assert claim.run.current_attempt_no == 2
    assert claim.run.next_attempt_no == 3
    assert claim.run.next_fencing_token == 3
    assert claim.run.retry_not_before_utc is None
    assert claim.attempt.attempt_no == 2
    assert claim.attempt.fencing_token == 2
    assert claim.lease.lease_id == "lease:2"


@pytest.mark.parametrize(
    ("overall_deadline", "reap_at", "expected_tail", "expected_status"),
    [
        (
            "2026-07-14T12:01:00Z",
            NOW_DT + timedelta(seconds=31),
            ("attempt.lease_expired", "attempt.retry_scheduled"),
            "retry_wait",
        ),
        (
            "2026-07-14T12:00:20Z",
            NOW_DT + timedelta(seconds=21),
            ("attempt.lease_expired", "run.timed_out"),
            "timed_out",
        ),
    ],
)
def test_lease_reaper_orders_expiry_before_retry_or_terminal_event(
    overall_deadline: str,
    reap_at,
    expected_tail: tuple[str, str],
    expected_status: str,
) -> None:
    harness = _run_harness(
        overall_deadline_utc=overall_deadline,
        lease_expires_at="2026-07-14T12:00:10Z",
    )

    result = harness.service_at(reap_at).reap_expired_lease(
        ReapExpiredLeaseRequest(
            run_id="run:1",
            expected_run_revision=2,
            actor=AuditActor(
                principal_id="system:lease-reaper",
                principal_kind="system",
            ),
        )
    )

    assert _event_types(harness)[-2:] == expected_tail
    assert harness.state.runs["run:1"].status == expected_status
    assert harness.state.attempts[("run:1", 1)].status == "lease_expired"
    assert harness.state.leases["lease:1"].status == "expired"
    if expected_status == "retry_wait":
        assert result.retry_decision.decision == "retry"
        assert harness.state.runs["run:1"].failure_artifact_id is None
    else:
        assert result.retry_decision.reason_code == "overall_deadline_exhausted"
        assert result.run_failure_artifact_id is not None


def test_started_attempt_deadline_prevents_lease_expiry_retry() -> None:
    harness = _run_harness(
        attempt_timeout_ns=5_000_000_000,
        lease_expires_at="2026-07-14T12:00:05Z",
    )
    started = _start(harness)

    result = harness.service_at(NOW_DT + timedelta(seconds=6)).reap_expired_lease(
        ReapExpiredLeaseRequest(
            run_id="run:1",
            expected_run_revision=started.run.revision,
            actor=AuditActor(
                principal_id="system:lease-reaper",
                principal_kind="system",
            ),
        )
    )

    assert result.run.status == "timed_out"
    assert result.retry_decision is not None
    assert result.retry_decision.reason_code == "attempt_deadline_exhausted"
    assert harness.state.attempts[("run:1", 1)].status == "lease_expired"
    assert _event_types(harness)[-2:] == ("attempt.lease_expired", "run.timed_out")


def test_cancelled_run_is_not_retried_when_the_worker_lease_expires() -> None:
    harness = _run_harness(
        attempt_timeout_ns=40_000_000_000,
        lease_expires_at="2026-07-14T12:00:10Z",
    )
    _start(harness)
    _submit_cancel(harness)
    requested = harness.state.runs["run:1"]

    result = harness.service_at(NOW_DT + timedelta(seconds=11)).reap_expired_lease(
        ReapExpiredLeaseRequest(
            run_id="run:1",
            expected_run_revision=requested.revision,
            actor=AuditActor(
                principal_id="system:lease-reaper",
                principal_kind="system",
            ),
        )
    )

    assert result.run.status == "cancelled"
    assert result.retry_decision is not None
    assert result.retry_decision.reason_code == "not_retry_eligible"
    assert harness.state.attempts[("run:1", 1)].status == "cancelled"
    assert harness.state.leases["lease:1"].status == "expired"
    assert _event_types(harness)[-3:] == (
        "run.cancel_requested",
        "attempt.lease_expired",
        "run.cancelled",
    )


def test_active_cancel_is_applied_at_request_then_worker_cooperatively_terminates() -> None:
    harness = _run_harness()
    _start(harness)

    submitted = _submit_cancel(harness)

    requested = harness.state.runs["run:1"]
    command = harness.state.commands[("run:1", "command:cancel:1")]
    assert requested.status == "running"
    assert requested.cancel_requested_at is not None
    assert requested.cancel_requested_by == _HUMAN
    assert command.status == "applied"
    assert command.result_event_seq == 4
    assert submitted.event.data == CancelRequestedDataV1(
        command_id="command:cancel:1",
        reason_code="user_requested",
    )
    assert _event_types(harness)[-1:] == ("run.cancel_requested",)

    outcome = _publish_failure(
        harness,
        at=NOW_DT + timedelta(seconds=2),
        cause_code="cancelled",
        failure_class="cancelled",
        intrinsic_retry_eligible=False,
    )

    assert harness.state.runs["run:1"].status == "cancelled"
    assert harness.state.attempts[("run:1", 1)].status == "cancelled"
    assert outcome.run_failure_artifact_id == harness.state.runs["run:1"].failure_artifact_id
    assert _event_types(harness)[-2:] == ("run.cancel_requested", "run.cancelled")


def test_active_cancel_takes_precedence_over_a_retryable_worker_failure() -> None:
    harness = _run_harness()
    _start(harness)
    _submit_cancel(harness)

    outcome = _publish_failure(
        harness,
        at=NOW_DT + timedelta(seconds=2),
        cause_code="dependency_unavailable",
        failure_class="transient_dependency",
        intrinsic_retry_eligible=True,
    )

    assert outcome.run.status == "cancelled"
    assert outcome.retry_decision is not None
    assert outcome.retry_decision.reason_code == "not_retry_eligible"
    assert harness.state.attempts[("run:1", 1)].status == "cancelled"
    assert _event_types(harness)[-2:] == ("run.cancel_requested", "run.cancelled")


def test_queued_and_retry_wait_cancel_directly_without_allocating_an_attempt() -> None:
    queued = _run_harness()
    _as_queued(queued)
    _submit_cancel(queued)
    assert _event_types(queued) == (
        "run.queued",
        "run.cancel_requested",
        "run.cancelled",
    )
    assert queued.state.runs["run:1"].status == "cancelled"
    assert queued.state.attempts == {}
    assert queued.state.leases == {}

    retry_wait = _run_harness()
    _start(retry_wait)
    _publish_failure(retry_wait)
    attempt_count = len(retry_wait.state.attempts)
    _submit_cancel(retry_wait, at=NOW_DT + timedelta(seconds=2))
    assert _event_types(retry_wait)[-2:] == (
        "run.cancel_requested",
        "run.cancelled",
    )
    assert retry_wait.state.runs["run:1"].status == "cancelled"
    assert len(retry_wait.state.attempts) == attempt_count == 1
    assert retry_wait.state.runs["run:1"].current_attempt_no is None


@pytest.mark.parametrize(
    ("scope", "expected_reason"),
    [
        ("queue", "queue_deadline_exhausted"),
        ("attempt", "attempt_deadline_exhausted"),
        ("overall", "overall_deadline_exhausted"),
    ],
)
def test_queue_attempt_and_overall_timeout_keep_distinct_retry_reasons(
    scope: str,
    expected_reason: str,
) -> None:
    if scope == "queue":
        harness = _run_harness(overall_deadline_utc="2026-07-14T12:01:00Z")
        _as_queued(harness, queue_deadline="2026-07-14T12:00:05Z")
    elif scope == "attempt":
        harness = _run_harness(
            attempt_timeout_ns=5_000_000_000,
            overall_deadline_utc="2026-07-14T12:01:00Z",
        )
        _start(harness)
    else:
        harness = _run_harness(
            attempt_timeout_ns=30_000_000_000,
            overall_deadline_utc="2026-07-14T12:00:05Z",
        )
        _start(harness)
    before_attempts = len(harness.state.attempts)

    result = harness.service_at(NOW_DT + timedelta(seconds=6)).sweep_timeout(
        SweepRunTimeoutRequest(
            run_id="run:1",
            expected_run_revision=harness.state.runs["run:1"].revision,
            actor=AuditActor(
                principal_id="system:timeout-sweeper",
                principal_kind="system",
            ),
        )
    )

    assert result.retry_decision.decision == "terminal"
    assert result.retry_decision.reason_code == expected_reason
    assert harness.state.runs["run:1"].status == "timed_out"
    assert _event_types(harness)[-1] == "run.timed_out"
    if scope == "queue":
        assert before_attempts == 0
        assert harness.state.attempts == {}
        assert harness.state.events[("run:1", 2)].data.cause_code == "queue_timed_out"
    else:
        assert harness.state.attempts[("run:1", 1)].status == "timed_out"
        assert harness.state.events[("run:1", 4)].data.cause_code == "timed_out"


def test_cancel_intent_has_the_same_precedence_for_timeout_sweeping() -> None:
    harness = _run_harness(
        attempt_timeout_ns=5_000_000_000,
        overall_deadline_utc="2026-07-14T12:01:00Z",
    )
    _start(harness)
    _submit_cancel(harness)
    requested = harness.state.runs["run:1"]

    result = harness.service_at(NOW_DT + timedelta(seconds=6)).sweep_timeout(
        SweepRunTimeoutRequest(
            run_id="run:1",
            expected_run_revision=requested.revision,
            actor=AuditActor(
                principal_id="system:timeout-sweeper",
                principal_kind="system",
            ),
        )
    )

    assert result.run.status == "cancelled"
    assert result.attempt is not None
    assert result.attempt.status == "cancelled"
    assert result.retry_decision is not None
    assert result.retry_decision.reason_code == "attempt_deadline_exhausted"
    assert _event_types(harness)[-2:] == ("run.cancel_requested", "run.cancelled")


def test_success_publication_requires_the_exact_current_running_attempt() -> None:
    harness = _run_harness()
    prepared = _prepared_success(harness)
    before = deepcopy(harness.state)

    with pytest.raises(InvalidStateTransition, match="running"):
        harness.lifecycle.publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=_fence(harness),
                prepared_outcome=prepared,
                actor=WORKER,
            )
        )
    assert harness.state == before

    _start(harness)
    before = deepcopy(harness.state)
    with pytest.raises(Conflict):
        harness.lifecycle.publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=_fence(harness).model_copy(update={"attempt_no": 2}),
                prepared_outcome=prepared,
                actor=WORKER,
            )
        )
    assert harness.state == before

    result = harness.lifecycle.publish_attempt_outcome(
        PublishAttemptOutcomeRequest(
            fence=_fence(harness),
            prepared_outcome=prepared,
            actor=WORKER,
        )
    )
    assert result.run.status == "succeeded"
    assert result.attempt.status == "succeeded"
    assert result.run.result_artifact_id == result.result_artifact_id
    assert _event_types(harness)[-1:] == ("run.succeeded",)


def test_record_success_persists_distinct_attempt_and_run_cassette_bundles() -> None:
    harness = _run_harness(run_payload=_record_payload())
    _start(harness)

    result = harness.service_at(NOW_DT + timedelta(seconds=1)).publish_attempt_outcome(
        PublishAttemptOutcomeRequest(
            fence=_fence(harness),
            prepared_outcome=_prepared_success(harness),
            actor=WORKER,
        )
    )

    assert result.run.terminal_cassette_artifact_id == "artifact:run-cassette:run:1"
    assert result.attempt is not None
    assert (
        result.attempt.cassette_bundle_artifact_id
        == "artifact:attempt-cassette:run:1:1"
    )
    assert (
        result.attempt.cassette_bundle_artifact_id
        != result.run.terminal_cassette_artifact_id
    )


def test_record_retry_persists_only_the_closed_attempt_cassette_bundle() -> None:
    harness = _run_harness(run_payload=_record_payload())
    _start(harness)

    result = _publish_failure(harness)

    assert result.run.status == "retry_wait"
    assert result.run.terminal_cassette_artifact_id is None
    assert result.attempt is not None
    assert (
        result.attempt.cassette_bundle_artifact_id
        == "artifact:attempt-cassette:run:1:1"
    )


def test_record_terminal_failure_persists_both_cassette_scopes() -> None:
    harness = _run_harness(run_payload=_record_payload())
    _start(harness)

    result = _publish_failure(
        harness,
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
    )

    assert result.run.status == "failed"
    assert result.run.terminal_cassette_artifact_id == "artifact:run-cassette:run:1"
    assert result.attempt is not None
    assert (
        result.attempt.cassette_bundle_artifact_id
        == "artifact:attempt-cassette:run:1:1"
    )


def test_record_outcome_without_required_cassette_rolls_back() -> None:
    harness = _run_harness(run_payload=_record_payload())
    _start(harness)
    harness.state.omit_cassette_publication = True
    before = deepcopy(harness.state)

    with pytest.raises(IntegrityViolation, match="cassette bundle"):
        harness.service_at(NOW_DT + timedelta(seconds=1)).publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=_fence(harness),
                prepared_outcome=_prepared_success(harness),
                actor=WORKER,
            )
        )

    assert harness.state == before


def test_terminal_publisher_failure_rolls_back_and_attempt_and_run_manifests_are_distinct() -> None:
    harness = _run_harness()
    _start(harness)
    harness.state.fail_publication = True
    before = deepcopy(harness.state)

    with pytest.raises(IntegrityViolation, match="publication"):
        _publish_failure(
            harness,
            cause_code="execution_failed",
            failure_class="execution",
            intrinsic_retry_eligible=False,
        )
    assert harness.state == before

    harness.state.fail_publication = False
    result = _publish_failure(
        harness,
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
    )
    attempt_id = harness.state.attempts[("run:1", 1)].failure_artifact_id
    run_id = harness.state.runs["run:1"].failure_artifact_id
    assert attempt_id == result.attempt_failure_artifact_id
    assert run_id == result.run_failure_artifact_id
    assert attempt_id != run_id
