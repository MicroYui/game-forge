"""Pure guards for persistent Run creation, claim, and publication heads."""

from __future__ import annotations

from gameforge.contracts.errors import IntegrityViolation, InvalidStateTransition
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    RetryPolicySnapshot,
    RunAttempt,
    RunCommandRecordV1,
    RunEvent,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunKindDefinition,
    RunLease,
    RunQueuedDataV1,
    RunRecord,
    outcome_policy_set_digest,
    run_kind_definition_digest,
)


_IMMUTABLE_RUN_FIELDS = (
    "run_schema_version",
    "run_id",
    "kind",
    "idempotency_scope",
    "idempotency_key",
    "request_hash",
    "payload",
    "payload_hash",
    "run_kind_definition_digest",
    "outcome_policy_set_digest",
    "migration_capability_matrix",
    "failure_classifier",
    "dispatch_trace_carrier",
    "initiated_by",
    "queue_deadline_utc",
    "attempt_timeout_ns",
    "overall_deadline_utc",
    "budget_set_snapshot_id",
    "run_budget_hold_group_id",
    "retry_policy",
    "max_attempts",
    "created_at",
)


def validate_run_kind_binding(
    *,
    run: RunRecord,
    definition: RunKindDefinition,
    retry_policy: RetryPolicySnapshot,
) -> None:
    """Close every RunRecord projection derivable from retained registries."""

    if (run.kind.kind, run.kind.version) != (definition.kind, definition.version):
        raise IntegrityViolation("Run kind differs from its retained definition")
    if definition.status != "active":
        raise IntegrityViolation("Run kind definition is not active")
    if run.payload.payload_schema_version != definition.payload_schema_id:
        raise IntegrityViolation("Run payload schema differs from its Run kind definition")
    if run.payload.llm_execution_mode not in definition.allowed_llm_execution_modes:
        raise IntegrityViolation("Run execution mode is not allowed by its Run kind")
    if definition.seed_policy == "required" and run.payload.seed is None:
        raise IntegrityViolation("Run kind requires an explicit seed")
    if definition.seed_policy == "forbidden" and run.payload.seed is not None:
        raise IntegrityViolation("Run kind forbids an explicit seed")
    if run.run_kind_definition_digest != run_kind_definition_digest(definition):
        raise IntegrityViolation("Run kind definition digest differs from retained definition")
    expected_policy_digest = outcome_policy_set_digest(
        run.kind,
        definition.outcome_policies,
    )
    if run.outcome_policy_set_digest != expected_policy_digest:
        raise IntegrityViolation("Run outcome policy digest differs from retained policies")
    if run.failure_classifier != definition.failure_classifier:
        raise IntegrityViolation("Run failure classifier differs from its Run kind")
    if run.retry_policy != definition.retry_policy:
        raise IntegrityViolation("Run retry policy differs from its Run kind")
    if run.migration_capability_matrix != definition.migration_capability_matrix:
        raise IntegrityViolation("Run migration matrix differs from its Run kind")
    actual_retry_ref = (
        retry_policy.retry_policy_id,
        retry_policy.retry_policy_version,
        retry_policy.retry_policy_digest,
    )
    expected_retry_ref = (
        run.retry_policy.retry_policy_id,
        run.retry_policy.retry_policy_version,
        run.retry_policy.retry_policy_digest,
    )
    if actual_retry_ref != expected_retry_ref:
        raise IntegrityViolation("retained retry policy differs from the Run binding")
    if run.max_attempts != retry_policy.max_attempts:
        raise IntegrityViolation("Run max_attempts differs from its retained retry policy")


def validate_run_immutable_bindings(*, previous: RunRecord, current: RunRecord) -> None:
    """Reject mutation of all creation-time identity and execution bindings."""

    for field_name in _IMMUTABLE_RUN_FIELDS:
        if getattr(previous, field_name) != getattr(current, field_name):
            raise IntegrityViolation(
                "Run immutable binding changed",
                field_name=field_name,
                run_id=previous.run_id,
            )


def validate_queued_creation(*, run: RunRecord, initial_event: RunEvent) -> None:
    """Validate the only Task-13 creation shape, including its consumed event head."""

    expected = {
        "status": "queued",
        "revision": 1,
        "current_attempt_no": None,
        "next_attempt_no": 1,
        "next_fencing_token": 1,
        "next_event_seq": 2,
        "concurrency_permit_group_id": None,
        "retry_not_before_utc": None,
        "result_artifact_id": None,
        "failure_artifact_id": None,
        "terminal_cassette_artifact_id": None,
    }
    for field_name, expected_value in expected.items():
        if getattr(run, field_name) != expected_value:
            raise IntegrityViolation(
                "new Run has an invalid queued projection",
                field_name=field_name,
                expected=expected_value,
                actual=getattr(run, field_name),
            )
    if run.created_at != run.updated_at:
        raise IntegrityViolation("new Run created_at and updated_at must match")
    if not run.run_budget_hold_group_id:
        raise IntegrityViolation("new Run requires its admitted budget hold")
    if (
        initial_event.run_id != run.run_id
        or initial_event.seq != 1
        or initial_event.event_type != "run.queued"
        or initial_event.attempt_no is not None
        or initial_event.occurred_at != run.created_at
        or initial_event.trace_id is not None
        or not isinstance(initial_event.data, RunQueuedDataV1)
    ):
        raise IntegrityViolation("new Run initial event does not match the queued head")
    if initial_event.data_schema_version != "run-queued@1":
        raise IntegrityViolation("new Run initial event has the wrong data schema")
    data = initial_event.data
    if (
        data.run_kind != run.kind
        or data.queue_deadline_utc != run.queue_deadline_utc
        or data.overall_deadline_utc != run.overall_deadline_utc
    ):
        raise IntegrityViolation("new Run initial event differs from the Run binding")


def validate_claim_transition(
    *,
    previous: RunRecord,
    current: RunRecord,
    attempt: RunAttempt,
    lease: RunLease,
    event: RunEvent,
    permit_group_id: str,
    acquired_at: str,
    expires_at: str,
    worker_principal_id: str,
    lease_id: str,
    trace_id: str | None,
) -> None:
    """Validate one queued -> leased transition against the three persisted heads."""

    if previous.status not in {"queued", "retry_wait"}:
        raise InvalidStateTransition("Run claim accepts only queued or due retry-wait Runs")
    validate_run_immutable_bindings(previous=previous, current=current)
    attempt_no = previous.next_attempt_no
    fencing_token = previous.next_fencing_token
    event_seq = previous.next_event_seq
    expected_run = {
        "status": "leased",
        "revision": previous.revision + 1,
        "current_attempt_no": attempt_no,
        "next_attempt_no": attempt_no + 1,
        "next_fencing_token": fencing_token + 1,
        "next_event_seq": event_seq + 1,
        "concurrency_permit_group_id": permit_group_id,
        "retry_not_before_utc": None,
        "updated_at": acquired_at,
    }
    for field_name, expected_value in expected_run.items():
        if getattr(current, field_name) != expected_value:
            raise IntegrityViolation(
                "claimed Run does not consume its persisted head exactly once",
                field_name=field_name,
                expected=expected_value,
                actual=getattr(current, field_name),
            )

    expected_attempt = RunAttempt(
        run_id=previous.run_id,
        attempt_no=attempt_no,
        status="leased",
        fencing_token=fencing_token,
        worker_principal_id=worker_principal_id,
        trace_id=trace_id,
        next_call_ordinal=1,
    )
    if attempt != expected_attempt:
        raise IntegrityViolation("claimed RunAttempt differs from the allocated heads")
    expected_lease = RunLease(
        lease_id=lease_id,
        run_id=previous.run_id,
        attempt_no=attempt_no,
        fencing_token=fencing_token,
        lease_version=1,
        owner_principal_id=worker_principal_id,
        acquired_at=acquired_at,
        heartbeat_at=acquired_at,
        expires_at=expires_at,
        status="active",
    )
    if lease != expected_lease:
        raise IntegrityViolation("claimed RunLease differs from the allocated heads")
    expected_event = RunEvent(
        run_id=previous.run_id,
        seq=event_seq,
        event_type="attempt.leased",
        attempt_no=attempt_no,
        occurred_at=acquired_at,
        data_schema_version="attempt-leased@1",
        data=AttemptLeasedDataV1(
            attempt_no=attempt_no,
            lease_expires_at=expires_at,
        ),
        trace_id=trace_id,
    )
    if event != expected_event:
        raise IntegrityViolation("claim event differs from the allocated event head")


def validate_prompt_link_binding(
    *,
    run: RunRecord,
    attempt: RunAttempt,
    lease: RunLease,
    link: RunIntermediateArtifactLinkV1,
) -> None:
    if run.current_attempt_no != attempt.attempt_no:
        raise IntegrityViolation("prompt publication attempt is not current")
    if (
        lease.status != "active"
        or lease.run_id != run.run_id
        or lease.attempt_no != attempt.attempt_no
        or lease.fencing_token != attempt.fencing_token
    ):
        raise IntegrityViolation("prompt publication lease differs from the current attempt")
    call_head_matches = (
        link.call_ordinal == attempt.next_call_ordinal
        if link.route_ordinal == 1
        else link.call_ordinal < attempt.next_call_ordinal
    )
    if (
        link.run_id != run.run_id
        or link.attempt_no != attempt.attempt_no
        or not call_head_matches
        or link.fencing_token != attempt.fencing_token
    ):
        raise IntegrityViolation("prompt link differs from the current Attempt head")


def validate_finding_link_binding(
    *,
    run: RunRecord,
    attempt: RunAttempt,
    link: RunFindingLinkV1,
) -> None:
    if (
        run.current_attempt_no != attempt.attempt_no
        or link.run_id != run.run_id
        or link.attempt_no != attempt.attempt_no
    ):
        raise IntegrityViolation("Finding link differs from the current Run attempt")


def validate_command_binding(
    *,
    run: RunRecord,
    definition: RunKindDefinition,
    record: RunCommandRecordV1,
) -> None:
    """Close command identity/allowlist; Task 14 owns command state transitions."""

    if record.run_id != run.run_id:
        raise IntegrityViolation("Run command is bound to a different Run")
    if record.command.expected_run_revision != run.revision:
        raise InvalidStateTransition("Run command expected revision is stale")
    if record.command.payload_schema_id not in definition.allowed_command_schema_ids:
        raise InvalidStateTransition("Run command schema is not allowed for this Run kind")


__all__ = [
    "validate_claim_transition",
    "validate_command_binding",
    "validate_finding_link_binding",
    "validate_prompt_link_binding",
    "validate_queued_creation",
    "validate_run_immutable_bindings",
    "validate_run_kind_binding",
]
