from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from gameforge.contracts.errors import Conflict, IntegrityViolation, InvalidStateTransition
from gameforge.contracts.execution_profiles import ArtifactLineagePolicyRefV1, RunKindRef
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    AttemptProgressDataV1,
    AttemptStartedDataV1,
    FailureClassificationRuleV1,
    FailureClassifierRefV1,
    FailureClassifierV1,
    ExecutionVersionPlanV1,
    LeaseExpiredDataV1,
    OutcomeArtifactPolicyV1,
    OutcomeArtifactRuleV1,
    PlannedAgentNodeVersionV1,
    PreparedRunOutcome,
    RetryDecisionV1,
    RetryScheduledDataV1,
    RunAttempt,
    RunCommandRecordV1,
    RunEvent,
    RunKindDefinition,
    RunIntermediateArtifactLinkV1,
    RunLease,
    RunQueuedDataV1,
    RunRecord,
    RunPayloadEnvelope,
    VersionTransitionPolicyRefV1,
    canonical_payload_hash,
    failure_classifier_digest,
    execution_version_plan_digest,
    outcome_policy_set_digest,
    run_kind_definition_digest,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.commands import (
    PromptRenderPublicationRequest,
    RunCommandCapabilities,
    RunCommandService,
)
from gameforge.platform.runs.lifecycle import (
    AttemptFailurePublication,
    AttemptWriteFence,
    PermitGroupBinding,
    RenewLeaseRequest,
    RunFailurePublication,
    RunLifecycleCapabilities,
    RunLifecycleService,
    RunResultPublication,
    StartAttemptRequest,
)
from gameforge.runtime.clock import FrozenUtcClock
from tests.platform.m4.test_run_create_claim import (
    _HASH_A,
    _definition,
    _payload,
    _retry_policy,
)


NOW_DT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-14T12:00:00Z"
QUEUE_DEADLINE = "2026-07-14T12:10:00Z"
DEFAULT_OVERALL_DEADLINE = "2026-07-14T12:01:00Z"
DEFAULT_LEASE_EXPIRES = "2026-07-14T12:00:30Z"
WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")
_HASH_B = "b" * 64
_HASH_C = "c" * 64


@dataclass
class _SequenceUtcClock:
    values: list[datetime]
    calls: int = 0

    def now_utc(self) -> datetime:
        value = self.values[self.calls]
        self.calls += 1
        return value


def _failure_classifier() -> FailureClassifierV1:
    rules = (
        FailureClassificationRuleV1(
            cause_code="cancelled",
            failure_class="cancelled",
            intrinsic_retry_eligible=False,
            dependency_required=False,
            allowed_dependency_kinds=(),
        ),
        FailureClassificationRuleV1(
            cause_code="execution_failed",
            failure_class="execution",
            intrinsic_retry_eligible=False,
            dependency_required=False,
            allowed_dependency_kinds=(),
        ),
        FailureClassificationRuleV1(
            cause_code="subject_superseded",
            failure_class="subject_superseded",
            intrinsic_retry_eligible=False,
            dependency_required=False,
            allowed_dependency_kinds=(),
        ),
        FailureClassificationRuleV1(
            cause_code="lease_expired",
            failure_class="lease",
            intrinsic_retry_eligible=True,
            dependency_required=False,
            allowed_dependency_kinds=(),
        ),
        FailureClassificationRuleV1(
            cause_code="dependency_unavailable",
            failure_class="transient_dependency",
            intrinsic_retry_eligible=True,
            dependency_required=True,
            allowed_dependency_kinds=("model_provider",),
        ),
        FailureClassificationRuleV1(
            cause_code="queue_timed_out",
            failure_class="timeout",
            intrinsic_retry_eligible=False,
            dependency_required=False,
            allowed_dependency_kinds=(),
        ),
        FailureClassificationRuleV1(
            cause_code="timed_out",
            failure_class="timeout",
            intrinsic_retry_eligible=False,
            dependency_required=False,
            allowed_dependency_kinds=(),
        ),
    )
    payload = {"classifier_version": 1, "rules": rules}
    return FailureClassifierV1(
        **payload,
        classifier_digest=failure_classifier_digest(payload),
    )


def _record_payload() -> RunPayloadEnvelope:
    base = _payload()
    node = PlannedAgentNodeVersionV1(
        agent_node_id="checker",
        prompt_version="checker@1",
        tool_version="checker-tool@1",
        allowed_model_snapshots=("model:test",),
    )
    raw_plan = {
        "agent_graph_version": "graph@1",
        "nodes": (node,),
        "model_catalog_version": 1,
        "model_catalog_digest": _HASH_A,
        "routing_policy_version": 1,
        "routing_policy_digest": _HASH_B,
    }
    plan = ExecutionVersionPlanV1(
        **raw_plan,
        plan_digest=execution_version_plan_digest(raw_plan),
    )
    return RunPayloadEnvelope.model_validate(
        {
            **base.model_dump(mode="python"),
            "execution_version_plan": plan,
            "llm_execution_mode": "record",
        }
    )


def _failure_policy(
    *,
    policy_id: str,
    outcome_code: str,
    publication_scope: str,
    attempt_terminal_status: str | None,
    run_status_after_publication: str,
    failure_class: str,
    retry_disposition: str,
) -> OutcomeArtifactPolicyV1:
    return OutcomeArtifactPolicyV1(
        policy_schema_version="outcome-artifact-policy@1",
        policy_id=policy_id,
        policy_version=1,
        outcome_code=outcome_code,
        prepared_outcome="failure",
        publication_scope=publication_scope,
        attempt_terminal_status=attempt_terminal_status,
        run_status_after_publication=run_status_after_publication,
        failure_class=failure_class,
        retry_disposition=retry_disposition,
        artifact_rules=(),
        workflow_effect_key=(
            "close_attempt_for_retry@1"
            if retry_disposition == "retry"
            else (
                "close_attempt_for_terminal@1"
                if publication_scope == "attempt"
                else "terminal_only@1"
            )
        ),
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id=f"{publication_scope}-manifest-transition@1",
            policy_version=1,
            digest=_HASH_C,
        ),
    )


def _outcome_policies() -> tuple[OutcomeArtifactPolicyV1, ...]:
    success = OutcomeArtifactPolicyV1(
        policy_schema_version="outcome-artifact-policy@1",
        policy_id="checker-completed@1",
        policy_version=1,
        outcome_code="checker_completed",
        prepared_outcome="success",
        publication_scope="run",
        run_status_after_publication="succeeded",
        artifact_rules=(
            OutcomeArtifactRuleV1(
                rule_id="primary",
                role="primary",
                artifact_kind="checker_run",
                payload_schema_ids=("checker-report@1",),
                min_count=1,
                max_count=1,
                lineage_policy_ref=ArtifactLineagePolicyRefV1(
                    policy_id="checker-completed@1/primary-lineage@1",
                    policy_version=1,
                    digest=_HASH_B,
                ),
            ),
        ),
        workflow_effect_key="no_workflow_change@1",
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition@1",
            policy_version=1,
            digest=_HASH_C,
        ),
    )
    failures = (
        _failure_policy(
            policy_id="dependency-unavailable-attempt-retry@1",
            outcome_code="dependency_unavailable",
            publication_scope="attempt",
            attempt_terminal_status="failed",
            run_status_after_publication="retry_wait",
            failure_class="transient_dependency",
            retry_disposition="retry",
        ),
        _failure_policy(
            policy_id="dependency-unavailable-attempt-final@1",
            outcome_code="dependency_unavailable",
            publication_scope="attempt",
            attempt_terminal_status="failed",
            run_status_after_publication="failed",
            failure_class="transient_dependency",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="dependency-unavailable@1",
            outcome_code="dependency_unavailable",
            publication_scope="run",
            attempt_terminal_status="failed",
            run_status_after_publication="failed",
            failure_class="transient_dependency",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="cancelled-attempt-final@1",
            outcome_code="cancelled",
            publication_scope="attempt",
            attempt_terminal_status="cancelled",
            run_status_after_publication="cancelled",
            failure_class="cancelled",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="cancelled@1",
            outcome_code="cancelled",
            publication_scope="run",
            attempt_terminal_status="cancelled",
            run_status_after_publication="cancelled",
            failure_class="cancelled",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="control-cancelled@1",
            outcome_code="cancelled",
            publication_scope="run",
            attempt_terminal_status=None,
            run_status_after_publication="cancelled",
            failure_class="cancelled",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="timed-out-attempt-final@1",
            outcome_code="timed_out",
            publication_scope="attempt",
            attempt_terminal_status="timed_out",
            run_status_after_publication="timed_out",
            failure_class="timeout",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="timed-out@1",
            outcome_code="timed_out",
            publication_scope="run",
            attempt_terminal_status="timed_out",
            run_status_after_publication="timed_out",
            failure_class="timeout",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="queue-timed-out@1",
            outcome_code="queue_timed_out",
            publication_scope="run",
            attempt_terminal_status=None,
            run_status_after_publication="timed_out",
            failure_class="timeout",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="retry-wait-timed-out@1",
            outcome_code="timed_out",
            publication_scope="run",
            attempt_terminal_status=None,
            run_status_after_publication="timed_out",
            failure_class="timeout",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="lease-expired-attempt-retry@1",
            outcome_code="lease_expired",
            publication_scope="attempt",
            attempt_terminal_status="lease_expired",
            run_status_after_publication="retry_wait",
            failure_class="lease",
            retry_disposition="retry",
        ),
        _failure_policy(
            policy_id="lease-expired-attempt-final-timeout@1",
            outcome_code="lease_expired",
            publication_scope="attempt",
            attempt_terminal_status="lease_expired",
            run_status_after_publication="timed_out",
            failure_class="lease",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="lease-expired-final-timeout@1",
            outcome_code="lease_expired",
            publication_scope="run",
            attempt_terminal_status="lease_expired",
            run_status_after_publication="timed_out",
            failure_class="lease",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="execution-failed-attempt-final@1",
            outcome_code="execution_failed",
            publication_scope="attempt",
            attempt_terminal_status="failed",
            run_status_after_publication="failed",
            failure_class="execution",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="execution-failed@1",
            outcome_code="execution_failed",
            publication_scope="run",
            attempt_terminal_status="failed",
            run_status_after_publication="failed",
            failure_class="execution",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="subject-superseded-attempt-final@1",
            outcome_code="subject_superseded",
            publication_scope="attempt",
            attempt_terminal_status="cancelled",
            run_status_after_publication="cancelled",
            failure_class="subject_superseded",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="subject-superseded@1",
            outcome_code="subject_superseded",
            publication_scope="run",
            attempt_terminal_status="cancelled",
            run_status_after_publication="cancelled",
            failure_class="subject_superseded",
            retry_disposition="terminal",
        ),
        _failure_policy(
            policy_id="control-subject-superseded@1",
            outcome_code="subject_superseded",
            publication_scope="run",
            attempt_terminal_status=None,
            run_status_after_publication="cancelled",
            failure_class="subject_superseded",
            retry_disposition="terminal",
        ),
    )
    return (success, *failures)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _State:
    runs: dict[str, RunRecord] = field(default_factory=dict)
    attempts: dict[tuple[str, int], RunAttempt] = field(default_factory=dict)
    leases: dict[str, RunLease] = field(default_factory=dict)
    events: dict[tuple[str, int], RunEvent] = field(default_factory=dict)
    commands: dict[tuple[str, str], RunCommandRecordV1] = field(default_factory=dict)
    intermediate_links: dict[tuple[str, int, int], RunIntermediateArtifactLinkV1] = field(
        default_factory=dict
    )
    permit: PermitGroupBinding | None = None
    audit_actions: list[str] = field(default_factory=list)
    accounting_actions: list[str] = field(default_factory=list)
    publisher_actions: list[str] = field(default_factory=list)
    fail_permit_renewal: bool = False
    fail_publication: bool = False
    fail_accounting: bool = False
    fail_audit: bool = False
    omit_cassette_publication: bool = False
    forced_preflight_outcome: PreparedRunOutcome | None = None


@dataclass(frozen=True)
class _PersistedStart:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


@dataclass(frozen=True)
class _PersistedProgress:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


@dataclass(frozen=True)
class _PersistedClaim:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


@dataclass(frozen=True)
class _PersistedClose:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    events: tuple[RunEvent, ...]


@dataclass(frozen=True)
class _PersistedTerminal:
    run: RunRecord
    attempt: RunAttempt | None
    lease: RunLease | None
    event: RunEvent


@dataclass(frozen=True)
class _PersistedAcceptance:
    run: RunRecord
    record: RunCommandRecordV1
    events: tuple[RunEvent, ...]


class _Repo:
    def __init__(self, state: _State) -> None:
        self.state = state

    def get(self, run_id: str) -> RunRecord | None:
        return self.state.runs.get(run_id)

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        return self.state.attempts.get((run_id, attempt_no))

    def get_current_lease(self, run_id: str) -> RunLease | None:
        return next(
            (
                lease
                for lease in self.state.leases.values()
                if lease.run_id == run_id and lease.status == "active"
            ),
            None,
        )

    def get_event(self, run_id: str, seq: int) -> RunEvent | None:
        return self.state.events.get((run_id, seq))

    def list_events(
        self,
        run_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> tuple[RunEvent, ...]:
        return tuple(
            event
            for (candidate_run_id, seq), event in sorted(self.state.events.items())
            if candidate_run_id == run_id and seq > after_seq
        )[:limit]

    def get_claim_candidate(self, *, now_utc: str) -> RunRecord | None:
        candidates = tuple(
            run
            for run in self.state.runs.values()
            if run.cancel_requested_at is None
            and (
                run.status == "queued"
                or (
                    run.status == "retry_wait"
                    and run.retry_not_before_utc is not None
                    and run.retry_not_before_utc <= now_utc
                )
            )
        )
        return min(candidates, key=lambda run: (run.created_at, run.run_id), default=None)

    def claim(
        self,
        *,
        run_id: str,
        expected_revision: int,
        worker_principal_id: str,
        lease_id: str,
        acquired_at: str,
        expires_at: str,
        permit_group_id: str,
        trace_id: str | None = None,
    ) -> _PersistedClaim:
        run = self.state.runs[run_id]
        if (
            run.revision != expected_revision
            or run.status not in {"queued", "retry_wait"}
            or run.current_attempt_no is not None
        ):
            raise Conflict("Run claim compare-and-set differs")
        attempt = RunAttempt(
            run_id=run.run_id,
            attempt_no=run.next_attempt_no,
            status="leased",
            fencing_token=run.next_fencing_token,
            worker_principal_id=worker_principal_id,
            trace_id=trace_id,
            next_call_ordinal=1,
        )
        lease = RunLease(
            lease_id=lease_id,
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            fencing_token=attempt.fencing_token,
            lease_version=1,
            owner_principal_id=worker_principal_id,
            acquired_at=acquired_at,
            heartbeat_at=acquired_at,
            expires_at=expires_at,
            status="active",
        )
        event = RunEvent(
            run_id=run.run_id,
            seq=run.next_event_seq,
            event_type="attempt.leased",
            attempt_no=attempt.attempt_no,
            occurred_at=acquired_at,
            data_schema_version="attempt-leased@1",
            data=AttemptLeasedDataV1(
                attempt_no=attempt.attempt_no,
                lease_expires_at=expires_at,
            ),
            trace_id=trace_id,
        )
        updated = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": "leased",
                "revision": run.revision + 1,
                "current_attempt_no": attempt.attempt_no,
                "next_attempt_no": attempt.attempt_no + 1,
                "next_fencing_token": attempt.fencing_token + 1,
                "next_event_seq": event.seq + 1,
                "concurrency_permit_group_id": permit_group_id,
                "retry_not_before_utc": None,
                "updated_at": acquired_at,
            }
        )
        self.state.runs[run.run_id] = updated
        self.state.attempts[(run.run_id, attempt.attempt_no)] = attempt
        self.state.leases[lease.lease_id] = lease
        self.state.events[(run.run_id, event.seq)] = event
        return _PersistedClaim(run=updated, attempt=attempt, lease=lease, event=event)

    def start_attempt(
        self,
        *,
        run_id: str,
        attempt_no: int,
        expected_run_revision: int,
        lease_id: str,
        fencing_token: int,
        started_at: str,
        attempt_deadline_utc: str,
    ) -> _PersistedStart:
        run = self.state.runs[run_id]
        attempt = self.state.attempts[(run_id, attempt_no)]
        lease = self.state.leases[lease_id]
        if run.revision != expected_run_revision:
            raise Conflict("Run revision differs")
        if (
            run.status != "leased"
            or run.current_attempt_no != attempt_no
            or attempt.status != "leased"
            or attempt.fencing_token != fencing_token
            or lease.run_id != run_id
            or lease.attempt_no != attempt_no
            or lease.fencing_token != fencing_token
            or lease.status != "active"
        ):
            raise Conflict("attempt start fence differs")

        event_seq = run.next_event_seq
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": "running",
                "revision": run.revision + 1,
                "next_event_seq": event_seq + 1,
                "updated_at": started_at,
            }
        )
        updated_attempt = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "status": "running",
                "started_at": started_at,
                "attempt_deadline_utc": attempt_deadline_utc,
            }
        )
        event = RunEvent(
            run_id=run_id,
            seq=event_seq,
            event_type="attempt.started",
            attempt_no=attempt_no,
            occurred_at=started_at,
            data_schema_version="attempt-started@1",
            data=AttemptStartedDataV1(
                attempt_no=attempt_no,
                started_at=started_at,
                attempt_deadline_utc=attempt_deadline_utc,
            ),
            trace_id=attempt.trace_id,
        )
        self.state.runs[run_id] = updated_run
        self.state.attempts[(run_id, attempt_no)] = updated_attempt
        self.state.events[(run_id, event_seq)] = event
        return _PersistedStart(
            run=updated_run,
            attempt=updated_attempt,
            lease=lease,
            event=event,
        )

    def renew_lease(
        self,
        *,
        run_id: str,
        attempt_no: int,
        lease_id: str,
        fencing_token: int,
        expected_lease_version: int,
        heartbeat_at: str,
        expires_at: str,
    ) -> RunLease:
        run = self.state.runs[run_id]
        attempt = self.state.attempts[(run_id, attempt_no)]
        lease = self.state.leases[lease_id]
        if (
            run.status not in {"leased", "running"}
            or run.current_attempt_no != attempt_no
            or attempt.fencing_token != fencing_token
            or lease.run_id != run_id
            or lease.attempt_no != attempt_no
            or lease.fencing_token != fencing_token
            or lease.lease_version != expected_lease_version
            or lease.status != "active"
        ):
            raise Conflict("lease renewal compare-and-set differs")
        updated = RunLease.model_validate(
            {
                **lease.model_dump(mode="python"),
                "lease_version": lease.lease_version + 1,
                "heartbeat_at": heartbeat_at,
                "expires_at": expires_at,
            }
        )
        self.state.leases[lease_id] = updated
        return updated

    def append_progress(
        self,
        *,
        fence: AttemptWriteFence,
        event: RunEvent,
    ) -> _PersistedProgress:
        run = self.state.runs[fence.run_id]
        attempt = self.state.attempts[(fence.run_id, fence.attempt_no)]
        lease = self.state.leases[fence.lease_id]
        if run.revision != fence.expected_run_revision:
            raise Conflict("progress Run revision differs")
        if event.seq != run.next_event_seq:
            raise Conflict("progress event head differs")
        updated = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + 1,
                "updated_at": event.occurred_at,
            }
        )
        self.state.runs[run.run_id] = updated
        self.state.events[(event.run_id, event.seq)] = event
        return _PersistedProgress(
            run=updated,
            attempt=attempt,
            lease=lease,
            event=event,
        )

    def close_attempt_for_retry(
        self,
        *,
        fence: AttemptWriteFence,
        ended_at: str,
        attempt_status: str,
        lease_status: str,
        failure_class: str,
        failure_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        retry_decision: RetryDecisionV1,
        events: tuple[RunEvent, ...],
    ) -> _PersistedClose:
        run, attempt, lease = self._fenced(fence)
        if retry_decision.decision != "retry" or not events:
            raise IntegrityViolation("retry close requires a scheduled retry")
        if tuple(event.seq for event in events) != tuple(
            range(run.next_event_seq, run.next_event_seq + len(events))
        ):
            raise Conflict("retry event head differs")
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": "retry_wait",
                "revision": run.revision + 1,
                "current_attempt_no": None,
                "next_event_seq": run.next_event_seq + len(events),
                "concurrency_permit_group_id": None,
                "retry_not_before_utc": retry_decision.retry_not_before_utc,
                "updated_at": ended_at,
            }
        )
        updated_attempt = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "status": attempt_status,
                "ended_at": ended_at,
                "failure_class": failure_class,
                "retryable": True,
                "failure_artifact_id": failure_artifact_id,
                "cassette_bundle_artifact_id": attempt_cassette_artifact_id,
            }
        )
        updated_lease = lease.model_copy(update={"status": lease_status})
        self.state.runs[run.run_id] = updated_run
        self.state.attempts[(run.run_id, attempt.attempt_no)] = updated_attempt
        self.state.leases[lease.lease_id] = updated_lease
        self.state.permit = None
        for event in events:
            self.state.events[(event.run_id, event.seq)] = event
        for identity, record in tuple(self.state.commands.items()):
            if record.run_id == run.run_id and record.status == "claimed":
                self.state.commands[identity] = RunCommandRecordV1.model_validate(
                    {
                        **record.model_dump(mode="python"),
                        "status": "pending",
                        "revision": record.revision + 1,
                        "claimed_at": None,
                        "claimed_attempt_no": None,
                        "claimed_fencing_token": None,
                    }
                )
        return _PersistedClose(
            run=updated_run,
            attempt=updated_attempt,
            lease=updated_lease,
            events=events,
        )

    def complete_attempt_success(
        self,
        *,
        fence: AttemptWriteFence,
        ended_at: str,
        result_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        terminal_cassette_artifact_id: str | None,
        event: RunEvent,
    ) -> _PersistedTerminal:
        run, attempt, lease = self._fenced(fence)
        if run.status != "running" or attempt.status != "running":
            raise Conflict("success attempt is not running")
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": "succeeded",
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + 1,
                "concurrency_permit_group_id": None,
                "result_artifact_id": result_artifact_id,
                "terminal_cassette_artifact_id": terminal_cassette_artifact_id,
                "updated_at": ended_at,
            }
        )
        updated_attempt = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "status": "succeeded",
                "ended_at": ended_at,
                "cassette_bundle_artifact_id": attempt_cassette_artifact_id,
            }
        )
        updated_lease = lease.model_copy(update={"status": "closed"})
        self.state.runs[run.run_id] = updated_run
        self.state.attempts[(run.run_id, attempt.attempt_no)] = updated_attempt
        self.state.leases[lease.lease_id] = updated_lease
        self.state.events[(event.run_id, event.seq)] = event
        self.state.permit = None
        self._reject_outstanding(run.run_id, event.seq, ended_at)
        return _PersistedTerminal(updated_run, updated_attempt, updated_lease, event)

    def close_attempt_terminal(
        self,
        *,
        fence: AttemptWriteFence,
        ended_at: str,
        attempt_status: str,
        lease_status: str,
        run_status: str,
        failure_class: str,
        attempt_failure_artifact_id: str,
        run_failure_artifact_id: str,
        attempt_cassette_artifact_id: str | None,
        terminal_cassette_artifact_id: str | None,
        retry_decision: RetryDecisionV1,
        leading_events: tuple[RunEvent, ...],
        terminal_event: RunEvent,
    ) -> _PersistedTerminal:
        run, attempt, lease = self._fenced(fence)
        events = (*leading_events, terminal_event)
        if retry_decision.decision != "terminal" or tuple(event.seq for event in events) != tuple(
            range(run.next_event_seq, run.next_event_seq + len(events))
        ):
            raise Conflict("terminal event head differs")
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": run_status,
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + len(events),
                "concurrency_permit_group_id": None,
                "retry_not_before_utc": None,
                "failure_artifact_id": run_failure_artifact_id,
                "terminal_cassette_artifact_id": terminal_cassette_artifact_id,
                "updated_at": ended_at,
            }
        )
        updated_attempt = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "status": attempt_status,
                "ended_at": ended_at,
                "failure_class": failure_class,
                "retryable": False,
                "failure_artifact_id": attempt_failure_artifact_id,
                "cassette_bundle_artifact_id": attempt_cassette_artifact_id,
            }
        )
        updated_lease = lease.model_copy(update={"status": lease_status})
        self.state.runs[run.run_id] = updated_run
        self.state.attempts[(run.run_id, attempt.attempt_no)] = updated_attempt
        self.state.leases[lease.lease_id] = updated_lease
        for event in events:
            self.state.events[(event.run_id, event.seq)] = event
        self.state.permit = None
        self._reject_outstanding(run.run_id, terminal_event.seq, ended_at)
        return _PersistedTerminal(
            updated_run,
            updated_attempt,
            updated_lease,
            terminal_event,
        )

    def terminate_inactive_run(
        self,
        *,
        run_id: str,
        expected_run_revision: int,
        run_status: str,
        failure_artifact_id: str,
        terminal_cassette_artifact_id: str | None,
        retry_decision: RetryDecisionV1,
        event: RunEvent,
    ) -> _PersistedTerminal:
        run = self.state.runs[run_id]
        if (
            run.revision != expected_run_revision
            or run.status not in {"queued", "retry_wait"}
            or run.current_attempt_no is not None
            or self.get_current_lease(run_id) is not None
            or retry_decision.decision != "terminal"
            or event.seq != run.next_event_seq
        ):
            raise Conflict("inactive terminal compare-and-set differs")
        updated = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": run_status,
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + 1,
                "retry_not_before_utc": None,
                "failure_artifact_id": failure_artifact_id,
                "terminal_cassette_artifact_id": terminal_cassette_artifact_id,
                "updated_at": event.occurred_at,
            }
        )
        latest = (
            self.state.attempts.get((run_id, run.next_attempt_no - 1))
            if run.next_attempt_no > 1
            else None
        )
        self.state.runs[run_id] = updated
        self.state.events[(run_id, event.seq)] = event
        self._reject_outstanding(run_id, event.seq, event.occurred_at)
        return _PersistedTerminal(updated, latest, None, event)

    def get_command(self, run_id: str, command_id: str) -> RunCommandRecordV1 | None:
        return self.state.commands.get((run_id, command_id))

    def get_command_by_idempotency(
        self,
        *,
        run_id: str,
        idempotency_key: str,
    ) -> RunCommandRecordV1 | None:
        return next(
            (
                record
                for record in self.state.commands.values()
                if record.run_id == run_id and record.command.idempotency_key == idempotency_key
            ),
            None,
        )

    def get_command_by_client_sequence(
        self,
        *,
        run_id: str,
        client_id: str,
        client_seq: int,
    ) -> RunCommandRecordV1 | None:
        return next(
            (
                record
                for record in self.state.commands.values()
                if record.run_id == run_id
                and record.command.client_id == client_id
                and record.command.client_seq == client_seq
            ),
            None,
        )

    def put_command(self, record: RunCommandRecordV1) -> RunCommandRecordV1:
        identity = (record.run_id, record.command.command_id)
        retained = self.state.commands.get(identity)
        if retained is not None and retained != record:
            raise IntegrityViolation("command collision")
        self.state.commands[identity] = record
        return record

    def accept_command(
        self,
        *,
        expected_run_revision: int,
        record: RunCommandRecordV1,
        events: tuple[RunEvent, ...],
        terminal_status: str | None = None,
        terminal_failure_artifact_id: str | None = None,
        terminal_cassette_artifact_id: str | None = None,
    ) -> _PersistedAcceptance:
        run = self.state.runs[record.run_id]
        if (
            run.revision != expected_run_revision
            or not events
            or tuple(event.seq for event in events)
            != tuple(range(run.next_event_seq, run.next_event_seq + len(events)))
        ):
            raise Conflict("command acceptance compare-and-set differs")
        updates: dict[str, object] = {
            "revision": run.revision + 1,
            "next_event_seq": run.next_event_seq + len(events),
            "updated_at": events[-1].occurred_at,
        }
        if record.command.type == "cancel":
            updates.update(
                {
                    "cancel_requested_at": events[0].occurred_at,
                    "cancel_requested_by": record.actor,
                }
            )
        if terminal_status is not None:
            if terminal_failure_artifact_id is None:
                raise IntegrityViolation("terminal command omitted failure artifact")
            updates.update(
                {
                    "status": terminal_status,
                    "failure_artifact_id": terminal_failure_artifact_id,
                    "terminal_cassette_artifact_id": terminal_cassette_artifact_id,
                    "retry_not_before_utc": None,
                    "concurrency_permit_group_id": None,
                }
            )
        updated = RunRecord.model_validate({**run.model_dump(mode="python"), **updates})
        self.put_command(record)
        self.state.runs[run.run_id] = updated
        for event in events:
            self.state.events[(event.run_id, event.seq)] = event
        if terminal_status is not None:
            self._reject_outstanding(
                run.run_id,
                events[-1].seq,
                events[-1].occurred_at,
            )
        return _PersistedAcceptance(updated, record, events)

    def claim_command(
        self,
        *,
        fence: AttemptWriteFence,
        command_id: str,
        claimed_at: str,
    ) -> RunCommandRecordV1:
        self._fenced(fence)
        record = self.state.commands[(fence.run_id, command_id)]
        if record.status != "pending":
            raise Conflict("command is not pending")
        updated = RunCommandRecordV1.model_validate(
            {
                **record.model_dump(mode="python"),
                "status": "claimed",
                "revision": record.revision + 1,
                "claimed_at": claimed_at,
                "claimed_attempt_no": fence.attempt_no,
                "claimed_fencing_token": fence.fencing_token,
            }
        )
        self.state.commands[(fence.run_id, command_id)] = updated
        return updated

    def complete_command(
        self,
        *,
        fence: AttemptWriteFence,
        command_id: str,
        expected_command_revision: int,
        outcome: str,
        outcome_code: str,
        occurred_at: str,
        event: RunEvent,
    ) -> RunCommandRecordV1:
        run, _, _ = self._fenced(fence)
        record = self.state.commands[(fence.run_id, command_id)]
        if (
            record.status != "claimed"
            or record.revision != expected_command_revision
            or record.claimed_attempt_no != fence.attempt_no
            or record.claimed_fencing_token != fence.fencing_token
            or event.seq != run.next_event_seq
        ):
            raise Conflict("command completion compare-and-set differs")
        updated_record = RunCommandRecordV1.model_validate(
            {
                **record.model_dump(mode="python"),
                "status": outcome,
                "revision": record.revision + 1,
                "applied_at": occurred_at,
                "result_event_seq": event.seq,
                "rejection_code": outcome_code if outcome == "rejected" else None,
            }
        )
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "revision": run.revision + 1,
                "next_event_seq": run.next_event_seq + 1,
                "updated_at": occurred_at,
            }
        )
        self.state.commands[(run.run_id, command_id)] = updated_record
        self.state.runs[run.run_id] = updated_run
        self.state.events[(run.run_id, event.seq)] = event
        return updated_record

    def _fenced(
        self,
        fence: AttemptWriteFence,
    ) -> tuple[RunRecord, RunAttempt, RunLease]:
        run = self.state.runs[fence.run_id]
        attempt = self.state.attempts[(fence.run_id, fence.attempt_no)]
        lease = self.state.leases.get(fence.lease_id)
        if (
            lease is None
            or run.revision != fence.expected_run_revision
            or run.current_attempt_no != fence.attempt_no
            or attempt.fencing_token != fence.fencing_token
            or lease.run_id != fence.run_id
            or lease.attempt_no != fence.attempt_no
            or lease.fencing_token != fence.fencing_token
            or lease.status != "active"
        ):
            raise Conflict("attempt write fence differs")
        return run, attempt, lease

    def _reject_outstanding(self, run_id: str, event_seq: int, occurred_at: str) -> None:
        for identity, record in tuple(self.state.commands.items()):
            if record.run_id != run_id or record.status not in {"pending", "claimed"}:
                continue
            self.state.commands[identity] = RunCommandRecordV1.model_validate(
                {
                    **record.model_dump(mode="python"),
                    "status": "rejected",
                    "revision": record.revision + 1,
                    "applied_at": occurred_at,
                    "result_event_seq": event_seq,
                    "rejection_code": "run_terminal",
                }
            )

    def get_intermediate_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
    ) -> RunIntermediateArtifactLinkV1 | None:
        return self.state.intermediate_links.get((run_id, attempt_no, call_ordinal))

    def put_intermediate_link(
        self,
        link: RunIntermediateArtifactLinkV1,
    ) -> RunIntermediateArtifactLinkV1:
        identity = (link.run_id, link.attempt_no, link.call_ordinal)
        retained = self.state.intermediate_links.get(identity)
        if retained is not None:
            if retained != link:
                raise IntegrityViolation("intermediate link collision")
            return retained
        attempt = self.state.attempts[(link.run_id, link.attempt_no)]
        if (
            attempt.next_call_ordinal != link.call_ordinal
            or attempt.fencing_token != link.fencing_token
        ):
            raise Conflict("attempt call ordinal or fencing token differs")
        self.state.attempts[(link.run_id, link.attempt_no)] = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "next_call_ordinal": attempt.next_call_ordinal + 1,
            }
        )
        self.state.intermediate_links[identity] = link
        return link


class _Registry:
    def __init__(self, definition: RunKindDefinition | None = None) -> None:
        self.retry = _retry_policy()
        self.classifier = _failure_classifier()
        classifier_ref = FailureClassifierRefV1(
            classifier_version=self.classifier.classifier_version,
            classifier_digest=self.classifier.classifier_digest,
        )
        source = definition or _definition(self.retry)
        allowed_commands = tuple(sorted({*source.allowed_command_schema_ids, "run-cancel@1"}))
        self.definition = source.model_copy(
            update={
                "allowed_command_schema_ids": allowed_commands,
                "failure_classifier": classifier_ref,
                "outcome_policies": _outcome_policies(),
            }
        )

    def get_run_kind(self, kind):
        if (kind.kind, kind.version) != (
            self.definition.kind,
            self.definition.version,
        ):
            return None
        return self.definition

    def get_retry_policy(self, ref):
        actual = (
            ref.retry_policy_id,
            ref.retry_policy_version,
            ref.retry_policy_digest,
        )
        expected = (
            self.retry.retry_policy_id,
            self.retry.retry_policy_version,
            self.retry.retry_policy_digest,
        )
        return self.retry if actual == expected else None

    def get_failure_classifier(self, ref):
        actual = (ref.classifier_version, ref.classifier_digest)
        expected = (
            self.classifier.classifier_version,
            self.classifier.classifier_digest,
        )
        return self.classifier if actual == expected else None

    def validate_payload_bindings(self, *, payload, definition) -> None:
        assert payload.payload_schema_version == definition.payload_schema_id


class _Accounting:
    def __init__(self, state: _State) -> None:
        self.state = state

    def renew_execution_permits(
        self,
        *,
        permit_group_id: str,
        expected_revision: int,
        lease_id: str,
        fencing_token: int,
        expires_at: str,
    ) -> PermitGroupBinding:
        retained = self.state.permit
        if (
            retained is None
            or retained.permit_group_id != permit_group_id
            or retained.revision != expected_revision
        ):
            raise Conflict("permit renewal compare-and-set differs")
        updated = PermitGroupBinding(
            permit_group_id=permit_group_id,
            revision=retained.revision + 1,
        )
        self.state.permit = updated
        self.state.accounting_actions.append(f"renew:{lease_id}:{fencing_token}:{expires_at}")
        if self.state.fail_permit_renewal:
            raise IntegrityViolation("injected permit renewal failure")
        return updated

    def reserve_run_budget(self, **kwargs) -> str:
        return "hold:run:1"

    def acquire_execution_permits(
        self,
        *,
        run: RunRecord,
        attempt_no: int,
        fencing_token: int,
        worker_principal_id: str,
        lease_id: str,
        expires_at: str,
    ) -> str:
        assert lease_id
        permit_group_id = f"permit:{run.run_id}:attempt:{attempt_no}"
        self.state.permit = PermitGroupBinding(
            permit_group_id=permit_group_id,
            revision=1,
        )
        self.state.accounting_actions.append(
            f"acquire:{attempt_no}:{fencing_token}:{worker_principal_id}:{expires_at}"
        )
        return permit_group_id

    def retry_budget_available(self, *, run: RunRecord) -> bool:
        return True

    def release_attempt(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        retry_decision: RetryDecisionV1 | None,
    ) -> None:
        self.state.accounting_actions.append(
            f"release:{run.run_id}:{attempt.attempt_no}:{lease.lease_id}"
        )
        if self.state.fail_accounting:
            raise IntegrityViolation("injected accounting failure")

    def close_run(self, *, run: RunRecord, terminal_status: str) -> None:
        self.state.accounting_actions.append(f"close:{run.run_id}:{terminal_status}")
        if self.state.fail_accounting:
            raise IntegrityViolation("injected accounting failure")


class _Publication:
    def __init__(self, state: _State, repo: _Repo) -> None:
        self.state = state
        self.repo = repo
        self.prompt_idempotency: dict[
            tuple[str, str], tuple[str, RunIntermediateArtifactLinkV1]
        ] = {}

    def preflight_outcome(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunOutcome,
    ) -> PreparedRunOutcome:
        assert attempt is None or run.run_id == attempt.run_id
        return self.state.forced_preflight_outcome or prepared

    def record_attempt_started(
        self,
        *,
        previous: RunRecord,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        assert previous.run_id == run.run_id == attempt.run_id == lease.run_id
        assert event.event_type == "attempt.started"
        assert actor == WORKER
        self.state.audit_actions.append("run.attempt_started")
        if self.state.fail_publication:
            raise IntegrityViolation("injected attempt-start publication failure")

    def record_run_claimed(self, **kwargs) -> None:
        self.state.audit_actions.append("run.claimed")
        if self.state.fail_audit:
            raise IntegrityViolation("injected audit failure")

    def record_attempt_progress(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        assert run.run_id == attempt.run_id == event.run_id
        assert actor == WORKER
        self.state.audit_actions.append("run.attempt_progress")
        if self.state.fail_publication:
            raise IntegrityViolation("injected progress publication failure")

    def get_prompt_replay(
        self,
        *,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> RunIntermediateArtifactLinkV1 | None:
        retained = self.prompt_idempotency.get((idempotency_scope, idempotency_key))
        if retained is None:
            return None
        retained_hash, link = retained
        if retained_hash != request_hash:
            raise Conflict("prompt publication idempotency payload differs")
        return link

    def publish_prompt_rendered(
        self,
        *,
        link: RunIntermediateArtifactLinkV1,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
        actor: AuditActor,
    ) -> RunIntermediateArtifactLinkV1:
        retained = self.get_prompt_replay(
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if retained is not None:
            return retained
        assert actor == WORKER
        stored = self.repo.put_intermediate_link(link)
        self.prompt_idempotency[(idempotency_scope, idempotency_key)] = (
            request_hash,
            stored,
        )
        self.state.publisher_actions.append(f"prompt:{stored.artifact_id}")
        self.state.audit_actions.append("run.prompt_rendered")
        return stored

    def publish_run_result(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> RunResultPublication:
        assert policy.prepared_outcome == "success"
        if self.state.fail_publication:
            raise IntegrityViolation("injected terminal publication failure")
        artifact_id = f"artifact:run-result:{run.run_id}:{attempt.attempt_no}"
        self.state.publisher_actions.append(f"result:{artifact_id}")
        return RunResultPublication(
            result_artifact_id=artifact_id,
            attempt_cassette_artifact_id=(
                f"artifact:attempt-cassette:{run.run_id}:{attempt.attempt_no}"
                if run.payload.llm_execution_mode == "record"
                and not self.state.omit_cassette_publication
                else None
            ),
            terminal_cassette_artifact_id=(
                f"artifact:run-cassette:{run.run_id}"
                if run.payload.llm_execution_mode == "record"
                and not self.state.omit_cassette_publication
                else (
                    run.payload.cassette_artifact_id
                    if run.payload.llm_execution_mode == "replay"
                    else None
                )
            ),
        )

    def publish_attempt_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> AttemptFailurePublication:
        assert policy.publication_scope == "attempt"
        if self.state.fail_publication:
            raise IntegrityViolation("injected terminal publication failure")
        artifact_id = f"artifact:attempt-failure:{prepared.cause_code}:{attempt.attempt_no}"
        self.state.publisher_actions.append(f"attempt-failure:{artifact_id}")
        return AttemptFailurePublication(
            failure_artifact_id=artifact_id,
            cassette_bundle_artifact_id=(
                f"artifact:attempt-cassette:{run.run_id}:{attempt.attempt_no}"
                if run.payload.llm_execution_mode == "record"
                and not self.state.omit_cassette_publication
                else None
            ),
        )

    def publish_run_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        attempt_failure_artifact_id: str | None,
        occurred_at: str,
        actor: AuditActor,
    ) -> RunFailurePublication:
        assert policy.publication_scope == "run"
        if self.state.fail_publication:
            raise IntegrityViolation("injected terminal publication failure")
        artifact_id = f"artifact:run-failure:{prepared.cause_code}:{run.run_id}"
        self.state.publisher_actions.append(f"run-failure:{artifact_id}")
        return RunFailurePublication(
            failure_artifact_id=artifact_id,
            terminal_cassette_artifact_id=(
                f"artifact:run-cassette:{run.run_id}"
                if run.payload.llm_execution_mode == "record"
                and not self.state.omit_cassette_publication
                else (
                    run.payload.cassette_artifact_id
                    if run.payload.llm_execution_mode == "replay"
                    else None
                )
            ),
        )

    def record_attempt_closed(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        events: tuple[RunEvent, ...],
        actor: AuditActor,
    ) -> None:
        self.state.audit_actions.append(f"run.attempt_closed:{attempt.status}")
        if self.state.fail_audit:
            raise IntegrityViolation("injected audit failure")

    def record_run_terminal(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        self.state.audit_actions.append(event.event_type)
        if self.state.fail_audit:
            raise IntegrityViolation("injected audit failure")

    def record_command_submitted(
        self,
        *,
        run: RunRecord,
        record: RunCommandRecordV1,
        events: tuple[RunEvent, ...],
        actor: AuditActor,
    ) -> None:
        self.state.audit_actions.append("run.command_submitted")
        if self.state.fail_audit:
            raise IntegrityViolation("injected audit failure")

    def record_command_completed(
        self,
        *,
        run: RunRecord,
        record: RunCommandRecordV1,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        self.state.audit_actions.append(event.event_type)
        if self.state.fail_audit:
            raise IntegrityViolation("injected audit failure")


class _Uow:
    def __init__(self, state: _State) -> None:
        self.state = state

    @contextmanager
    def begin(self):
        snapshot = deepcopy(self.state)
        try:
            yield object()
        except Exception:
            self.state.__dict__.clear()
            self.state.__dict__.update(deepcopy(snapshot).__dict__)
            raise


@dataclass
class _Harness:
    state: _State
    repo: _Repo
    registry: _Registry
    accounting: _Accounting
    publication: _Publication
    unit_of_work: _Uow
    lifecycle: RunLifecycleService
    commands: RunCommandService

    def service_at(self, now: datetime) -> RunLifecycleService:
        capabilities = RunLifecycleCapabilities(
            runs=self.repo,
            registry=self.registry,
            accounting=self.accounting,
            publication=self.publication,
        )
        return RunLifecycleService(
            unit_of_work=self.unit_of_work,
            bind_capabilities=lambda transaction: capabilities,
            clock=FrozenUtcClock(now),
        )

    def command_service_at(self, now: datetime) -> RunCommandService:
        capabilities = RunCommandCapabilities(
            runs=self.repo,
            registry=self.registry,
            admission=self.accounting,
            publication=self.publication,
            accounting=self.accounting,
        )
        return RunCommandService(
            unit_of_work=self.unit_of_work,
            bind_capabilities=lambda transaction: capabilities,
            clock=FrozenUtcClock(now),
        )


def _harness(
    *,
    now: datetime = NOW_DT,
    attempt_timeout_ns: int = 10_000_000_000,
    overall_deadline_utc: str = DEFAULT_OVERALL_DEADLINE,
    lease_expires_at: str = DEFAULT_LEASE_EXPIRES,
    definition: RunKindDefinition | None = None,
    run_payload: RunPayloadEnvelope | None = None,
) -> _Harness:
    registry = _Registry(definition)
    payload = run_payload or _payload()
    run_kind = RunKindRef(
        kind=registry.definition.kind,
        version=registry.definition.version,
    )
    run = RunRecord(
        run_id="run:1",
        kind=run_kind,
        status="leased",
        revision=2,
        idempotency_scope="principal:human:a",
        idempotency_key="request:1",
        request_hash=_HASH_A,
        payload=payload,
        payload_hash=canonical_payload_hash(payload),
        run_kind_definition_digest=run_kind_definition_digest(registry.definition),
        outcome_policy_set_digest=outcome_policy_set_digest(
            run_kind,
            registry.definition.outcome_policies,
        ),
        failure_classifier=registry.definition.failure_classifier,
        initiated_by=AuditActor(principal_id="human:a", principal_kind="human"),
        queue_deadline_utc=QUEUE_DEADLINE,
        attempt_timeout_ns=attempt_timeout_ns,
        overall_deadline_utc=overall_deadline_utc,
        current_attempt_no=1,
        next_attempt_no=2,
        next_fencing_token=2,
        next_event_seq=3,
        budget_set_snapshot_id=payload.budget_set_snapshot_id,
        run_budget_hold_group_id="hold:run:1",
        concurrency_permit_group_id="permit:run:1",
        retry_policy=registry.definition.retry_policy,
        max_attempts=registry.retry.max_attempts,
        created_at=_utc_text(now - timedelta(seconds=10)),
        updated_at=_utc_text(now),
    )
    attempt = RunAttempt(
        run_id=run.run_id,
        attempt_no=1,
        status="leased",
        fencing_token=1,
        worker_principal_id=WORKER.principal_id,
        trace_id="trace:attempt:1",
        next_call_ordinal=1,
    )
    lease = RunLease(
        lease_id="lease:1",
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        fencing_token=attempt.fencing_token,
        lease_version=1,
        owner_principal_id=WORKER.principal_id,
        acquired_at=_utc_text(now),
        heartbeat_at=_utc_text(now),
        expires_at=lease_expires_at,
        status="active",
    )
    queued_event = RunEvent(
        run_id=run.run_id,
        seq=1,
        event_type="run.queued",
        occurred_at=run.created_at,
        data_schema_version="run-queued@1",
        data=RunQueuedDataV1(
            run_kind=run.kind,
            queue_deadline_utc=run.queue_deadline_utc,
            overall_deadline_utc=run.overall_deadline_utc,
        ),
    )
    leased_event = RunEvent(
        run_id=run.run_id,
        seq=2,
        event_type="attempt.leased",
        attempt_no=attempt.attempt_no,
        occurred_at=run.updated_at,
        data_schema_version="attempt-leased@1",
        data=AttemptLeasedDataV1(
            attempt_no=attempt.attempt_no,
            lease_expires_at=lease.expires_at,
        ),
        trace_id=attempt.trace_id,
    )
    state = _State(
        runs={run.run_id: run},
        attempts={(run.run_id, attempt.attempt_no): attempt},
        leases={lease.lease_id: lease},
        events={(run.run_id, 1): queued_event, (run.run_id, 2): leased_event},
        permit=PermitGroupBinding(permit_group_id="permit:run:1", revision=1),
    )
    repo = _Repo(state)
    accounting = _Accounting(state)
    publication = _Publication(state, repo)
    unit_of_work = _Uow(state)
    lifecycle_capabilities = RunLifecycleCapabilities(
        runs=repo,
        registry=registry,
        accounting=accounting,
        publication=publication,
    )
    command_capabilities = RunCommandCapabilities(
        runs=repo,
        registry=registry,
        admission=accounting,
        publication=publication,
        accounting=accounting,
    )
    return _Harness(
        state=state,
        repo=repo,
        registry=registry,
        accounting=accounting,
        publication=publication,
        unit_of_work=unit_of_work,
        lifecycle=RunLifecycleService(
            unit_of_work=unit_of_work,
            bind_capabilities=lambda transaction: lifecycle_capabilities,
            clock=FrozenUtcClock(now),
        ),
        commands=RunCommandService(
            unit_of_work=unit_of_work,
            bind_capabilities=lambda transaction: command_capabilities,
            clock=FrozenUtcClock(now),
        ),
    )


def _fence(harness: _Harness, *, expected_run_revision: int | None = None):
    run = harness.state.runs["run:1"]
    attempt = harness.state.attempts[(run.run_id, 1)]
    return AttemptWriteFence(
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        expected_run_revision=expected_run_revision or run.revision,
        lease_id="lease:1",
        fencing_token=attempt.fencing_token,
    )


def _start(harness: _Harness):
    return harness.lifecycle.start_attempt(StartAttemptRequest(fence=_fence(harness), actor=WORKER))


@pytest.mark.parametrize(
    ("attempt_timeout_ns", "overall_deadline", "expected_deadline"),
    [
        (5_000_000_000, "2026-07-14T12:00:12Z", "2026-07-14T12:00:05Z"),
        (20_000_000_000, "2026-07-14T12:00:12Z", "2026-07-14T12:00:12Z"),
    ],
)
def test_start_fixes_attempt_deadline_to_timeout_or_overall_minimum(
    attempt_timeout_ns: int,
    overall_deadline: str,
    expected_deadline: str,
) -> None:
    harness = _harness(
        attempt_timeout_ns=attempt_timeout_ns,
        overall_deadline_utc=overall_deadline,
    )

    result = _start(harness)

    assert result.run.status == "running"
    assert result.run.revision == 3
    assert result.run.next_event_seq == 4
    assert result.attempt.status == "running"
    assert result.attempt.started_at == NOW
    assert result.attempt.attempt_deadline_utc == expected_deadline
    assert result.lease == harness.state.leases["lease:1"]
    assert result.event == harness.state.events[("run:1", 3)]
    assert result.event.data == AttemptStartedDataV1(
        attempt_no=1,
        started_at=NOW,
        attempt_deadline_utc=expected_deadline,
    )
    assert harness.state.audit_actions == ["run.attempt_started"]


@pytest.mark.parametrize(
    ("update", "error"),
    [
        ({"expected_run_revision": 1}, Conflict),
        ({"lease_id": "lease:other"}, Conflict),
        ({"fencing_token": 2}, Conflict),
    ],
)
def test_start_rejects_stale_revision_or_wrong_lease_identity(
    update: dict[str, object],
    error: type[Exception],
) -> None:
    harness = _harness()
    before = deepcopy(harness.state)
    request = StartAttemptRequest(
        fence=_fence(harness).model_copy(update=update),
        actor=WORKER,
    )

    with pytest.raises(error):
        harness.lifecycle.start_attempt(request)

    assert harness.state == before


def test_start_rejects_an_expired_lease_without_state_or_audit() -> None:
    harness = _harness(lease_expires_at=NOW)
    before = deepcopy(harness.state)

    with pytest.raises(InvalidStateTransition, match="expired"):
        _start(harness)

    assert harness.state == before


def test_heartbeat_atomically_cas_renews_lease_and_permits_without_run_event_or_audit() -> None:
    harness = _harness()
    started = _start(harness)
    before_run = started.run
    before_events = tuple(harness.repo.list_events("run:1", after_seq=0, limit=100))
    before_audit = tuple(harness.state.audit_actions)
    heartbeat_at = NOW_DT + timedelta(seconds=5)
    service = harness.service_at(heartbeat_at)
    request = RenewLeaseRequest(
        run_id="run:1",
        attempt_no=1,
        lease_id="lease:1",
        fencing_token=1,
        expected_lease_version=1,
        expected_permit_revision=1,
        lease_duration_ns=30_000_000_000,
        actor=WORKER,
    )

    result = service.renew_lease(request)

    assert result.lease.lease_version == 2
    assert result.lease.heartbeat_at == "2026-07-14T12:00:05Z"
    assert result.lease.expires_at == started.attempt.attempt_deadline_utc
    assert result.permit == PermitGroupBinding(
        permit_group_id="permit:run:1",
        revision=2,
    )
    assert harness.state.runs["run:1"] == before_run
    assert tuple(harness.repo.list_events("run:1", after_seq=0, limit=100)) == before_events
    assert tuple(harness.state.audit_actions) == before_audit

    with pytest.raises(Conflict):
        service.renew_lease(request)
    assert harness.state.leases["lease:1"] == result.lease
    assert harness.state.permit == result.permit


def test_heartbeat_is_capped_by_overall_deadline_before_start() -> None:
    harness = _harness(overall_deadline_utc="2026-07-14T12:00:08Z")
    service = harness.service_at(NOW_DT + timedelta(seconds=2))

    result = service.renew_lease(
        RenewLeaseRequest(
            run_id="run:1",
            attempt_no=1,
            lease_id="lease:1",
            fencing_token=1,
            expected_lease_version=1,
            expected_permit_revision=1,
            lease_duration_ns=30_000_000_000,
            actor=WORKER,
        )
    )

    assert result.lease.expires_at == "2026-07-14T12:00:08Z"


def test_permit_renewal_failure_rolls_back_the_lease_and_permit_together() -> None:
    harness = _harness()
    _start(harness)
    harness.state.fail_permit_renewal = True
    before = deepcopy(harness.state)

    with pytest.raises(IntegrityViolation, match="permit renewal"):
        harness.service_at(NOW_DT + timedelta(seconds=5)).renew_lease(
            RenewLeaseRequest(
                run_id="run:1",
                attempt_no=1,
                lease_id="lease:1",
                fencing_token=1,
                expected_lease_version=1,
                expected_permit_revision=1,
                lease_duration_ns=5_000_000_000,
                actor=WORKER,
            )
        )

    assert harness.state == before


def test_progress_and_prompt_ignore_heartbeat_version_but_keep_the_run_fence() -> None:
    harness = _harness()
    _start(harness)
    service = harness.service_at(NOW_DT + timedelta(seconds=2))
    service.renew_lease(
        RenewLeaseRequest(
            run_id="run:1",
            attempt_no=1,
            lease_id="lease:1",
            fencing_token=1,
            expected_lease_version=1,
            expected_permit_revision=1,
            lease_duration_ns=5_000_000_000,
            actor=WORKER,
        )
    )
    fence = _fence(harness)

    progress = service.publish_progress(
        fence=fence,
        data=AttemptProgressDataV1(
            attempt_no=1,
            phase_code="checker",
            completed_units=1,
            total_units=2,
        ),
        actor=WORKER,
    )
    assert progress.event.event_type == "attempt.progress"
    assert progress.event.seq == 4

    prompt_fence = fence.model_copy(update={"expected_run_revision": progress.run.revision})
    prompt_request = PromptRenderPublicationRequest(
        fence=prompt_fence,
        artifact_id="artifact:prompt:1",
        request_hash=_HASH_A,
        idempotency_scope="run:1/attempt:1",
        idempotency_key="prompt-call:1",
        actor=WORKER,
    )
    prompt = harness.command_service_at(NOW_DT + timedelta(seconds=2)).publish_prompt_rendered(
        prompt_request
    )

    assert "lease_version" not in AttemptWriteFence.model_fields
    assert prompt.link.call_ordinal == 1
    assert prompt.link.fencing_token == 1
    assert harness.state.leases["lease:1"].lease_version == 2


def test_expired_or_replaced_worker_cannot_publish_progress_or_prompt() -> None:
    harness = _harness(attempt_timeout_ns=5_000_000_000)
    _start(harness)
    stale_fence = _fence(harness)
    expired_service = harness.service_at(NOW_DT + timedelta(seconds=6))
    before = deepcopy(harness.state)

    with pytest.raises(InvalidStateTransition, match="deadline|expired"):
        expired_service.publish_progress(
            fence=stale_fence,
            data=AttemptProgressDataV1(
                attempt_no=1,
                phase_code="checker",
                completed_units=1,
            ),
            actor=WORKER,
        )
    with pytest.raises(InvalidStateTransition, match="deadline|expired"):
        harness.command_service_at(NOW_DT + timedelta(seconds=6)).publish_prompt_rendered(
            PromptRenderPublicationRequest(
                fence=stale_fence,
                artifact_id="artifact:prompt:expired",
                request_hash=_HASH_A,
                idempotency_scope="run:1/attempt:1",
                idempotency_key="prompt-expired",
                actor=WORKER,
            )
        )
    assert harness.state == before

    run = harness.state.runs["run:1"]
    attempt_one = harness.state.attempts[("run:1", 1)]
    lease_one = harness.state.leases["lease:1"]
    expired_at = _utc_text(NOW_DT + timedelta(milliseconds=100))
    retry_at = _utc_text(NOW_DT + timedelta(milliseconds=200))
    replacement_at = _utc_text(NOW_DT + timedelta(milliseconds=300))
    attempt_deadline = _utc_text(NOW_DT + timedelta(seconds=5))
    attempt_failure_artifact_id = "artifact:attempt-failure:lease-expired"
    retry_decision = RetryDecisionV1(
        cause_code="lease_expired",
        failure_class="lease",
        intrinsic_retry_eligible=True,
        decision="retry",
        reason_code="transient_eligible",
        retry_not_before_utc=retry_at,
        classifier=run.failure_classifier,
        retry_policy=run.retry_policy,
        evaluated_at_utc=expired_at,
    )
    harness.state.attempts[("run:1", 1)] = RunAttempt.model_validate(
        {
            **attempt_one.model_dump(mode="python"),
            "status": "lease_expired",
            "ended_at": expired_at,
            "failure_class": "lease",
            "retryable": True,
            "failure_artifact_id": attempt_failure_artifact_id,
        }
    )
    harness.state.leases["lease:1"] = RunLease.model_validate(
        {**lease_one.model_dump(mode="python"), "status": "expired"}
    )
    replacement_attempt = RunAttempt(
        run_id="run:1",
        attempt_no=2,
        status="running",
        fencing_token=2,
        worker_principal_id="service:worker:2",
        trace_id="trace:attempt:2",
        next_call_ordinal=1,
        started_at=replacement_at,
        attempt_deadline_utc=attempt_deadline,
    )
    replacement_lease = RunLease(
        lease_id="lease:2",
        run_id="run:1",
        attempt_no=2,
        fencing_token=2,
        lease_version=1,
        owner_principal_id="service:worker:2",
        acquired_at=replacement_at,
        heartbeat_at=replacement_at,
        expires_at=attempt_deadline,
        status="active",
    )
    harness.state.attempts[("run:1", 2)] = replacement_attempt
    harness.state.leases["lease:2"] = replacement_lease
    harness.state.events[("run:1", 4)] = RunEvent(
        run_id="run:1",
        seq=4,
        event_type="attempt.lease_expired",
        attempt_no=1,
        occurred_at=expired_at,
        data_schema_version="lease-expired@1",
        data=LeaseExpiredDataV1(
            attempt_no=1,
            failure_artifact_id=attempt_failure_artifact_id,
            will_retry=True,
        ),
        trace_id="trace:attempt:1",
    )
    harness.state.events[("run:1", 5)] = RunEvent(
        run_id="run:1",
        seq=5,
        event_type="attempt.retry_scheduled",
        attempt_no=1,
        occurred_at=expired_at,
        data_schema_version="retry-scheduled@1",
        data=RetryScheduledDataV1(
            attempt_no=1,
            failure_artifact_id=attempt_failure_artifact_id,
            cause_code="lease_expired",
            failure_class="lease",
            retry_decision=retry_decision,
            retry_not_before_utc=retry_at,
        ),
        trace_id="trace:attempt:1",
    )
    harness.state.events[("run:1", 6)] = RunEvent(
        run_id="run:1",
        seq=6,
        event_type="attempt.leased",
        attempt_no=2,
        occurred_at=replacement_at,
        data_schema_version="attempt-leased@1",
        data=AttemptLeasedDataV1(
            attempt_no=2,
            lease_expires_at=attempt_deadline,
        ),
        trace_id="trace:attempt:2",
    )
    harness.state.events[("run:1", 7)] = RunEvent(
        run_id="run:1",
        seq=7,
        event_type="attempt.started",
        attempt_no=2,
        occurred_at=replacement_at,
        data_schema_version="attempt-started@1",
        data=AttemptStartedDataV1(
            attempt_no=2,
            started_at=replacement_at,
            attempt_deadline_utc=attempt_deadline,
        ),
        trace_id="trace:attempt:2",
    )
    harness.state.runs["run:1"] = RunRecord.model_validate(
        {
            **run.model_dump(mode="python"),
            "revision": run.revision + 3,
            "current_attempt_no": 2,
            "next_attempt_no": 3,
            "next_fencing_token": 3,
            "next_event_seq": 8,
            "updated_at": replacement_at,
        }
    )
    replaced_state = deepcopy(harness.state)

    with pytest.raises(Conflict):
        harness.service_at(NOW_DT + timedelta(seconds=1)).publish_progress(
            fence=stale_fence,
            data=AttemptProgressDataV1(
                attempt_no=1,
                phase_code="checker",
                completed_units=1,
            ),
            actor=WORKER,
        )
    with pytest.raises(Conflict):
        harness.command_service_at(NOW_DT + timedelta(seconds=1)).publish_prompt_rendered(
            PromptRenderPublicationRequest(
                fence=stale_fence,
                artifact_id="artifact:prompt:replaced",
                request_hash=_HASH_A,
                idempotency_scope="run:1/attempt:1",
                idempotency_key="prompt-replaced",
                actor=WORKER,
            )
        )

    assert harness.state == replaced_state


def test_prompt_replay_rechecks_the_current_attempt_fence_and_deadline() -> None:
    harness = _harness(attempt_timeout_ns=5_000_000_000)
    _start(harness)
    request = PromptRenderPublicationRequest(
        fence=_fence(harness),
        artifact_id="artifact:prompt:replay-expired",
        request_hash=_HASH_A,
        idempotency_scope="run:1/attempt:1",
        idempotency_key="prompt-replay-expired",
        actor=WORKER,
    )
    first = harness.command_service_at(NOW_DT + timedelta(seconds=1)).publish_prompt_rendered(
        request
    )
    before = deepcopy(harness.state)

    with pytest.raises(InvalidStateTransition, match="deadline|expired"):
        harness.command_service_at(NOW_DT + timedelta(seconds=6)).publish_prompt_rendered(request)

    assert first.replayed is False
    assert harness.state == before


def test_prompt_publication_uses_one_authoritative_time_for_fence_and_link() -> None:
    harness = _harness(attempt_timeout_ns=5_000_000_000)
    _start(harness)
    clock = _SequenceUtcClock(
        values=[
            NOW_DT + timedelta(seconds=4, milliseconds=999),
            NOW_DT + timedelta(seconds=5, milliseconds=1),
        ]
    )
    harness.commands._clock = clock  # type: ignore[assignment]

    result = harness.commands.publish_prompt_rendered(
        PromptRenderPublicationRequest(
            fence=_fence(harness),
            artifact_id="artifact:prompt:deadline-edge",
            request_hash=_HASH_A,
            idempotency_scope="run:1/attempt:1",
            idempotency_key="prompt-deadline-edge",
            actor=WORKER,
        )
    )

    assert clock.calls == 1
    assert result.link.published_at == "2026-07-14T12:00:04.999000Z"
