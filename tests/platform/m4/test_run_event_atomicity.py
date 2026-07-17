from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from gameforge.contracts.errors import Conflict, IdempotencyConflict, IntegrityViolation
from gameforge.contracts.jobs import (
    CancelRequestedDataV1,
    CancelRunPayloadV1,
    CommandAcceptedDataV1,
    CommandOutcomeDataV1,
    LeaseExpiredDataV1,
    PlaytestProvideInputPayloadV1,
    PreparedRunFailure,
    RetryScheduledDataV1,
    RunCommandV1,
    RunTerminatedDataV1,
    canonical_payload_hash,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.lifecycle import (
    PublishAttemptOutcomeRequest,
    ReapExpiredLeaseRequest,
)
from tests.platform.m4.test_run_create_claim import _definition, _retry_policy
from tests.platform.m4.test_run_fencing import (
    NOW_DT,
    WORKER,
    _fence,
    _harness,
    _start,
)


HUMAN = AuditActor(principal_id="human:a", principal_kind="human")
REAPER = AuditActor(principal_id="system:run-reaper", principal_kind="system")
_RUN_ID = "run:1"
_HASH_A = "a" * 64


def _command_harness(*, attempt_timeout_ns: int = 10_000_000_000):
    definition = _definition(_retry_policy()).model_copy(
        update={
            "allowed_command_schema_ids": (
                "playtest-provide-input@1",
                "run-cancel@1",
            )
        }
    )
    return _harness(
        definition=definition,
        attempt_timeout_ns=attempt_timeout_ns,
    )


def _input_command(
    *,
    expected_run_revision: int,
    command_id: str = "command:input:1",
    client_id: str = "browser:1",
    client_seq: int = 1,
    idempotency_key: str = "input:1",
    choice_id: str = "choice:a",
) -> RunCommandV1:
    return RunCommandV1(
        command_id=command_id,
        client_id=client_id,
        client_seq=client_seq,
        idempotency_key=idempotency_key,
        expected_run_revision=expected_run_revision,
        type="provide_input",
        payload_schema_id="playtest-provide-input@1",
        payload=PlaytestProvideInputPayloadV1(
            interaction_id="interaction:1",
            expected_state_hash=_HASH_A,
            choice_id=choice_id,
        ),
    )


def _cancel_command(*, expected_run_revision: int) -> RunCommandV1:
    return RunCommandV1(
        command_id="command:cancel:1",
        client_id="browser:1",
        client_seq=99,
        idempotency_key="cancel:1",
        expected_run_revision=expected_run_revision,
        type="cancel",
        payload_schema_id="run-cancel@1",
        payload=CancelRunPayloadV1(reason_code="requested_by_operator"),
    )


def _submit(harness, command: RunCommandV1):
    return harness.commands.submit(
        run_id=_RUN_ID,
        command=command,
        actor=HUMAN,
    )


def _claim_command(harness, command_id: str):
    return harness.commands.claim_command(
        fence=_fence(harness),
        command_id=command_id,
        actor=WORKER,
    )


def _expire_for_retry(harness, *, fence=None):
    expired_at = NOW_DT + timedelta(seconds=31)
    expected = fence or _fence(harness)
    return harness.service_at(expired_at).reap_expired_lease(
        ReapExpiredLeaseRequest(
            run_id=_RUN_ID,
            expected_run_revision=expected.expected_run_revision,
            actor=REAPER,
        )
    )


def test_command_submission_computes_hash_and_persists_one_typed_accepted_event() -> None:
    harness = _command_harness()
    _start(harness)
    command = _input_command(expected_run_revision=3)

    ack = _submit(harness, command)

    record = harness.state.commands[(_RUN_ID, command.command_id)]
    event = harness.state.events[(_RUN_ID, 4)]
    run = harness.state.runs[_RUN_ID]
    assert record.request_hash == canonical_payload_hash(command)
    assert record.status == "pending"
    assert record.revision == 1
    assert event.event_type == "run.command_accepted"
    assert event.attempt_no is None
    assert event.data == CommandAcceptedDataV1(
        command_id=command.command_id,
        command_type="provide_input",
        command_revision=record.revision,
    )
    assert run.revision == 4
    assert run.next_event_seq == 5
    assert ack.persisted_status == "pending"
    assert ack.command_revision == record.revision
    assert ack.run_revision == run.revision


@pytest.mark.parametrize("collision", ["command_id", "client_seq", "idempotency_key"])
def test_command_identity_and_idempotency_are_exact(collision: str) -> None:
    harness = _command_harness()
    _start(harness)
    original = _input_command(expected_run_revision=3)
    first = _submit(harness, original)
    before_replay = deepcopy(harness.state)

    replay = _submit(harness, original)

    assert replay.status == "duplicate"
    assert replay.command_revision == first.command_revision
    assert replay.run_revision == first.run_revision
    assert harness.state == before_replay

    if collision == "command_id":
        conflicting = original.model_copy(
            update={"payload": original.payload.model_copy(update={"choice_id": "choice:b"})}
        )
    elif collision == "client_seq":
        conflicting = _input_command(
            expected_run_revision=4,
            command_id="command:input:other",
            idempotency_key="input:other",
            choice_id="choice:b",
        )
    else:
        conflicting = _input_command(
            expected_run_revision=4,
            command_id="command:input:other",
            client_seq=2,
            idempotency_key=original.idempotency_key,
            choice_id="choice:b",
        )
    before_conflict = deepcopy(harness.state)

    with pytest.raises(Conflict):
        _submit(harness, conflicting)

    assert harness.state == before_conflict


def test_exact_command_replay_is_not_redefined_by_current_authorized_actor() -> None:
    harness = _command_harness()
    _start(harness)
    command = _input_command(expected_run_revision=3)
    first = _submit(harness, command)
    other = AuditActor(principal_id="human:b", principal_kind="human")

    replay = harness.commands.submit(run_id=_RUN_ID, command=command, actor=other)

    assert replay.status == "duplicate"
    assert replay.command_revision == first.command_revision
    assert harness.state.commands[(_RUN_ID, command.command_id)].actor == HUMAN


def test_command_submission_rejects_an_unbounded_request_id_before_writes() -> None:
    harness = _command_harness()
    _start(harness)
    command = _input_command(expected_run_revision=3)
    before = deepcopy(harness.state)

    with pytest.raises(ValueError, match="request_id"):
        harness.commands.submit(
            run_id=_RUN_ID,
            command=command,
            actor=HUMAN,
            request_id="r" * 513,
        )

    assert harness.state == before


def test_command_id_cannot_be_reused_for_another_run() -> None:
    harness = _command_harness()
    _start(harness)
    first_run = harness.state.runs[_RUN_ID]
    second_run_id = "run:2"
    harness.state.runs[second_run_id] = first_run.model_copy(
        update={
            "run_id": second_run_id,
            "idempotency_key": "request:2",
        }
    )
    command = _input_command(expected_run_revision=first_run.revision)
    _submit(harness, command)
    before_conflict = deepcopy(harness.state)

    with pytest.raises(IdempotencyConflict):
        harness.commands.submit(run_id=second_run_id, command=command, actor=HUMAN)

    assert harness.state == before_conflict


@pytest.mark.parametrize(
    ("outcome", "event_type", "outcome_code"),
    [
        ("applied", "run.command_applied", "input_applied"),
        ("rejected", "run.command_rejected", "input_rejected"),
    ],
)
def test_provide_input_accept_claim_and_outcome_are_fenced_and_event_complete(
    outcome: str,
    event_type: str,
    outcome_code: str,
) -> None:
    harness = _command_harness()
    _start(harness)
    command = _input_command(expected_run_revision=3)
    _submit(harness, command)
    before_claim_run = harness.state.runs[_RUN_ID]
    before_claim_events = deepcopy(harness.state.events)

    claimed = _claim_command(harness, command.command_id)

    assert claimed.status == "claimed"
    assert claimed.revision == 2
    assert claimed.claimed_attempt_no == 1
    assert claimed.claimed_fencing_token == 1
    assert harness.state.runs[_RUN_ID] == before_claim_run
    assert harness.state.events == before_claim_events
    claimed_replay = _submit(harness, command)
    assert claimed_replay.status == "duplicate"
    assert claimed_replay.persisted_status == "claimed"
    assert claimed_replay.command_revision == claimed.revision
    assert claimed_replay.event is None

    completed = harness.commands.complete_command(
        fence=_fence(harness),
        command_id=command.command_id,
        expected_command_revision=claimed.revision,
        outcome=outcome,
        outcome_code=outcome_code,
        actor=WORKER,
    )

    event = harness.state.events[(_RUN_ID, 5)]
    run = harness.state.runs[_RUN_ID]
    assert completed.status == outcome
    assert completed.revision == 3
    assert completed.result_event_seq == event.seq
    assert completed.rejection_code == (outcome_code if outcome == "rejected" else None)
    assert event.event_type == event_type
    assert event.attempt_no == 1
    assert event.data == CommandOutcomeDataV1(
        command_id=command.command_id,
        command_type="provide_input",
        command_revision=completed.revision,
        outcome_code=outcome_code,
    )
    assert run.revision == 5
    assert run.next_event_seq == 6
    completed_replay = _submit(harness, command)
    assert completed_replay.status == "duplicate"
    assert completed_replay.persisted_status == outcome
    assert completed_replay.command_revision == completed.revision
    assert completed_replay.event == event


def test_stale_worker_cannot_claim_or_complete_a_command() -> None:
    harness = _command_harness()
    _start(harness)
    command = _input_command(expected_run_revision=3)
    _submit(harness, command)
    stale = _fence(harness).model_copy(update={"lease_id": "lease:stale"})
    before_claim = deepcopy(harness.state)

    with pytest.raises(Conflict):
        harness.commands.claim_command(
            fence=stale,
            command_id=command.command_id,
            actor=WORKER,
        )
    assert harness.state == before_claim

    claimed = _claim_command(harness, command.command_id)
    before_complete = deepcopy(harness.state)
    with pytest.raises(Conflict):
        harness.commands.complete_command(
            fence=stale,
            command_id=command.command_id,
            expected_command_revision=claimed.revision,
            outcome="applied",
            outcome_code="input_applied",
            actor=WORKER,
        )
    assert harness.state == before_complete


def test_lease_expiry_retry_emits_contiguous_typed_events_and_requeues_claimed_commands() -> None:
    harness = _command_harness(attempt_timeout_ns=60_000_000_000)
    _start(harness)
    command = _input_command(expected_run_revision=3)
    _submit(harness, command)
    _claim_command(harness, command.command_id)

    result = _expire_for_retry(harness)
    decision = result.retry_decision
    assert decision is not None

    lease_event = harness.state.events[(_RUN_ID, 5)]
    retry_event = harness.state.events[(_RUN_ID, 6)]
    run = harness.state.runs[_RUN_ID]
    attempt = harness.state.attempts[(_RUN_ID, 1)]
    lease = harness.state.leases["lease:1"]
    command_record = harness.state.commands[(_RUN_ID, command.command_id)]
    assert lease_event.event_type == "attempt.lease_expired"
    assert lease_event.attempt_no == 1
    assert lease_event.data == LeaseExpiredDataV1(
        attempt_no=1,
        failure_artifact_id=attempt.failure_artifact_id,
        will_retry=True,
    )
    assert retry_event.event_type == "attempt.retry_scheduled"
    assert retry_event.attempt_no == 1
    assert retry_event.data == RetryScheduledDataV1(
        attempt_no=1,
        failure_artifact_id=attempt.failure_artifact_id,
        cause_code="lease_expired",
        failure_class="lease",
        retry_decision=decision,
        retry_not_before_utc=run.retry_not_before_utc,
    )
    assert retry_event.seq == lease_event.seq + 1
    assert run.status == "retry_wait"
    assert run.current_attempt_no is None
    assert run.next_attempt_no == 2
    assert run.next_fencing_token == 2
    assert run.next_event_seq == 7
    assert run.failure_artifact_id is None
    assert attempt.failure_artifact_id == result.attempt_failure_artifact_id
    assert attempt.status == "lease_expired"
    assert lease.status == "expired"
    assert command_record.status == "pending"
    assert command_record.revision == 3
    assert command_record.claimed_at is None
    assert command_record.claimed_attempt_no is None
    assert command_record.claimed_fencing_token is None


@pytest.mark.parametrize(
    ("failure_stage", "error"),
    [
        ("publisher", IntegrityViolation),
        ("accounting", IntegrityViolation),
        ("audit", IntegrityViolation),
        ("cas", Conflict),
    ],
)
def test_expiry_publication_failure_rolls_back_state_events_and_artifact_pointers(
    failure_stage: str,
    error: type[Exception],
) -> None:
    harness = _command_harness(attempt_timeout_ns=60_000_000_000)
    _start(harness)
    if failure_stage == "publisher":
        harness.state.fail_publication = True
    elif failure_stage == "accounting":
        harness.state.fail_accounting = True
    elif failure_stage == "audit":
        harness.state.fail_audit = True
    fence = _fence(harness)
    if failure_stage == "cas":
        fence = fence.model_copy(update={"expected_run_revision": fence.expected_run_revision - 1})
    before = deepcopy(harness.state)

    with pytest.raises(error):
        _expire_for_retry(harness, fence=fence)

    assert harness.state == before
    assert harness.state.runs[_RUN_ID].failure_artifact_id is None
    assert harness.state.attempts[(_RUN_ID, 1)].failure_artifact_id is None
    assert (_RUN_ID, 4) not in harness.state.events


def test_terminal_cancel_reuses_one_event_for_bulk_command_rejection() -> None:
    harness = _command_harness()
    _start(harness)
    first = _input_command(
        expected_run_revision=3,
        command_id="command:input:1",
        client_seq=1,
        idempotency_key="input:1",
    )
    _submit(harness, first)
    second = _input_command(
        expected_run_revision=4,
        command_id="command:input:2",
        client_seq=2,
        idempotency_key="input:2",
        choice_id="choice:b",
    )
    _submit(harness, second)
    _claim_command(harness, second.command_id)
    cancel = _cancel_command(expected_run_revision=5)

    cancel_ack = _submit(harness, cancel)

    cancel_record = harness.state.commands[(_RUN_ID, cancel.command_id)]
    cancel_event = harness.state.events[(_RUN_ID, 6)]
    assert cancel_ack.persisted_status == "applied"
    assert cancel_record.status == "applied"
    assert cancel_record.result_event_seq == cancel_event.seq
    assert cancel_event.event_type == "run.cancel_requested"
    assert cancel_event.attempt_no is None
    assert cancel_event.data == CancelRequestedDataV1(
        command_id=cancel.command_id,
        reason_code="requested_by_operator",
    )

    run = harness.state.runs[_RUN_ID]
    result = harness.lifecycle.publish_attempt_outcome(
        PublishAttemptOutcomeRequest(
            fence=_fence(harness),
            prepared_outcome=PreparedRunFailure(
                run_id=run.run_id,
                attempt_no=run.current_attempt_no,
                run_kind=run.kind,
                artifacts=(),
                requirement_dispositions=(),
                cause_code="cancelled",
                failure_class="cancelled",
                intrinsic_retry_eligible=False,
                classifier=run.failure_classifier,
                redacted_message="cooperative cancellation",
            ),
            actor=WORKER,
        )
    )
    assert result.attempt_failure_artifact_id is not None
    assert result.run_failure_artifact_id is not None

    terminal_event = harness.state.events[(_RUN_ID, 7)]
    assert terminal_event.event_type == "run.cancelled"
    assert terminal_event.attempt_no == 1
    assert terminal_event.data == RunTerminatedDataV1(
        attempt_no=1,
        failure_artifact_id=result.run_failure_artifact_id,
        cause_code="cancelled",
    )
    for command_id in (first.command_id, second.command_id):
        record = harness.state.commands[(_RUN_ID, command_id)]
        assert record.status == "rejected"
        assert record.rejection_code == "run_terminal"
        assert record.result_event_seq == terminal_event.seq
    retained_cancel = harness.state.commands[(_RUN_ID, cancel.command_id)]
    assert retained_cancel.status == "applied"
    assert retained_cancel.result_event_seq == cancel_event.seq
    assert harness.state.runs[_RUN_ID].status == "cancelled"
    assert harness.state.runs[_RUN_ID].failure_artifact_id == result.run_failure_artifact_id
    assert (
        harness.state.attempts[(_RUN_ID, 1)].failure_artifact_id
        == result.attempt_failure_artifact_id
    )
