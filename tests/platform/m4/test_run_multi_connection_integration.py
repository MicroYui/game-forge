from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import Conflict
from gameforge.contracts.jobs import (
    OutcomeArtifactPolicyV1,
    PreparedRunFailure,
    PreparedRunOutcome,
    RetryDecisionV1,
    RunAttempt,
    RunEvent,
    RunLease,
    RunRecord,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.commands import (
    RunClaimRequest,
    RunCommandCapabilities,
    RunCommandService,
)
from gameforge.platform.runs.lifecycle import (
    AttemptFailurePublication,
    AttemptWriteFence,
    ReapExpiredLeaseRequest,
    RunLifecycleCapabilities,
    RunLifecycleService,
    StartAttemptRequest,
)
from gameforge.platform.terminal_staging import (
    StagedTerminalPublication,
    TerminalPublicationDraft,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import ArtifactRow
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from tests.platform.m4.test_run_create_claim import NOW_DT, _create_request
from tests.platform.m4.test_run_fencing import _NoBlobStager, _Registry


WORKER_1 = AuditActor(principal_id="service:worker:1", principal_kind="service")
WORKER_2 = AuditActor(principal_id="service:worker:2", principal_kind="service")
REAPER = AuditActor(principal_id="system:lease-reaper", principal_kind="system")


class _SqlPublication:
    """Test publisher that preserves Run failure foreign keys in the owning UoW."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record_run_created(
        self,
        *,
        run: RunRecord,
        event: RunEvent,
        request_id: str | None = None,
    ) -> None:
        del request_id
        assert event.run_id == run.run_id
        assert event.event_type == "run.queued"

    def record_run_claimed(
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
        assert event.event_type == "attempt.leased"
        assert actor.principal_id == attempt.worker_principal_id

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
        assert actor.principal_id == attempt.worker_principal_id

    def preflight_outcome(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunOutcome,
    ) -> PreparedRunOutcome:
        assert attempt is None or run.run_id == attempt.run_id
        return prepared

    def publish_attempt_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> AttemptFailurePublication:
        assert prepared.cause_code == "lease_expired"
        assert retry_decision.decision == "retry"
        assert policy.publication_scope == "attempt"
        assert actor == REAPER
        result = self._attempt_failure_projection(run=run, attempt=attempt)
        self._session.add(
            ArtifactRow(
                artifact_id=result.failure_artifact_id,
                lineage_schema_version="lineage@1",
                kind="run_failure",
                version_tuple={},
                lineage=[],
                payload_hash="f" * 64,
                created_at=occurred_at,
                meta={"cause_code": prepared.cause_code},
                object_ref=None,
            )
        )
        self._session.flush()
        return result

    @staticmethod
    def _attempt_failure_projection(
        *,
        run: RunRecord,
        attempt: RunAttempt,
    ) -> AttemptFailurePublication:
        return AttemptFailurePublication(
            failure_artifact_id=(f"artifact:attempt-failure:{run.run_id}:{attempt.attempt_no}")
        )

    def plan_attempt_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> TerminalPublicationDraft:
        assert prepared.cause_code == "lease_expired"
        assert retry_decision.decision == "retry"
        assert policy.publication_scope == "attempt"
        assert actor == REAPER
        result = self._attempt_failure_projection(run=run, attempt=attempt)
        operation_projection = (
            {
                "operation": "test.insert_attempt_failure",
                "cause_code": prepared.cause_code,
            },
        )
        result_projection = result.model_dump(mode="json")
        canonical_projection = {
            "publication_kind": "attempt_failure",
            "run_id": run.run_id,
            "attempt_no": attempt.attempt_no,
            "occurred_at": occurred_at,
            "materials": (),
            "operations": operation_projection,
            "result": result_projection,
        }
        return TerminalPublicationDraft(
            publication_kind="attempt_failure",
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            occurred_at=occurred_at,
            projection_digest=canonical_sha256(canonical_projection),
            materials=(),
            operations=operation_projection,
            operation_projection=operation_projection,
            result_projection=result_projection,
            result=result,
        )

    def plan_active_failure_aggregate(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        attempt_policy: OutcomeArtifactPolicyV1,
        run_policy: OutcomeArtifactPolicyV1 | None,
        occurred_at: str,
        actor: AuditActor,
    ) -> tuple[TerminalPublicationDraft, ...]:
        assert run_policy is None
        return (
            self.plan_attempt_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_decision=retry_decision,
                policy=attempt_policy,
                occurred_at=occurred_at,
                actor=actor,
            ),
        )

    def commit(
        self,
        fresh_draft: TerminalPublicationDraft,
        staged: StagedTerminalPublication,
    ) -> AttemptFailurePublication:
        assert fresh_draft.projection_digest == staged.projection_digest
        assert not fresh_draft.materials and not staged.receipts
        result = fresh_draft.result
        assert isinstance(result, AttemptFailurePublication)
        (operation,) = fresh_draft.operation_projection
        self._session.add(
            ArtifactRow(
                artifact_id=result.failure_artifact_id,
                lineage_schema_version="lineage@1",
                kind="run_failure",
                version_tuple={},
                lineage=[],
                payload_hash="f" * 64,
                created_at=fresh_draft.occurred_at,
                meta={"cause_code": operation["cause_code"]},
                object_ref=None,
            )
        )
        self._session.flush()
        return result

    def commit_many(
        self,
        publications: tuple[tuple[TerminalPublicationDraft, StagedTerminalPublication], ...],
    ) -> tuple[AttemptFailurePublication, ...]:
        for fresh_draft, staged in publications:
            assert fresh_draft.projection_digest == staged.projection_digest
        return tuple(self.commit(fresh, staged) for fresh, staged in publications)

    def record_attempt_closed(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        events: tuple[RunEvent, ...],
        actor: AuditActor,
    ) -> None:
        assert run.status == "retry_wait"
        assert attempt.status == "lease_expired"
        assert tuple(event.event_type for event in events) == (
            "attempt.lease_expired",
            "attempt.retry_scheduled",
        )
        assert actor == REAPER


class _Accounting:
    def reserve_run_budget(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        request_hash: str,
        initiated_by: AuditActor,
    ) -> str:
        assert budget_set_snapshot_id and request_hash and initiated_by.principal_id
        return f"hold:{run_id}"

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
        assert worker_principal_id and lease_id and expires_at
        return f"permit:{run.run_id}:{attempt_no}:{fencing_token}"

    def retry_budget_available(self, *, run: RunRecord) -> bool:
        assert run.run_budget_hold_group_id
        return True

    def release_attempt(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        retry_decision: RetryDecisionV1 | None,
    ) -> None:
        assert run.run_id == attempt.run_id == lease.run_id
        assert retry_decision is not None and retry_decision.decision == "retry"


def _capabilities(session: Session) -> TransactionCapabilities:
    repository = SqlRunRepository(session)
    return TransactionCapabilities(
        refs=_SqlPublication(session),
        audit=repository,
        approvals=repository,
        lineage=repository,
        object_bindings=repository,
        runs=repository,
        cost=_Accounting(),
    )


@contextmanager
def _services(
    database_url: str,
    registry: _Registry,
    now: datetime,
) -> Iterator[tuple[RunCommandService, RunLifecycleService]]:
    engine = get_engine(database_url)
    unit_of_work = SqliteUnitOfWork(engine, _capabilities)

    def bind_commands(transaction: object) -> RunCommandCapabilities:
        return RunCommandCapabilities(
            runs=transaction.runs,
            registry=registry,
            admission=transaction.cost,
            publication=transaction.refs,
            accounting=transaction.cost,
        )

    def bind_lifecycle(transaction: object) -> RunLifecycleCapabilities:
        return RunLifecycleCapabilities(
            runs=transaction.runs,
            registry=registry,
            accounting=transaction.cost,
            publication=transaction.refs,
        )

    @contextmanager
    def planning_scope() -> Iterator[TransactionCapabilities]:
        with Session(engine, autoflush=False) as session:
            try:
                yield _capabilities(session)
            finally:
                session.rollback()

    try:
        yield (
            RunCommandService(
                unit_of_work=unit_of_work,
                bind_capabilities=bind_commands,
                clock=FrozenUtcClock(now),
            ),
            RunLifecycleService(
                unit_of_work=unit_of_work,
                bind_capabilities=bind_lifecycle,
                clock=FrozenUtcClock(now),
                planning_scope=planning_scope,
                bind_planning_capabilities=bind_lifecycle,
                stage_publications=_NoBlobStager(),
            ),
        )
    finally:
        engine.dispose()


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_reaper_retry_is_monotonic_across_connections_and_fences_stale_worker(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'run-lifecycle.db'}"
    migrations_api.upgrade(database_url, "head")
    registry = _Registry()

    with _services(database_url, registry, NOW_DT) as (commands, _):
        created = commands.create_run(_create_request())

    first_claim_at = NOW_DT + timedelta(milliseconds=10)
    with _services(database_url, registry, first_claim_at) as (commands, _):
        first_claim = commands.claim_next(
            RunClaimRequest(
                worker=WORKER_1,
                lease_id="lease:attempt:1",
                lease_duration_ns=1_000_000_000,
                trace_id="trace:attempt:1",
            )
        )
    assert first_claim is not None

    first_fence = AttemptWriteFence(
        run_id=created.run.run_id,
        attempt_no=first_claim.attempt.attempt_no,
        expected_run_revision=first_claim.run.revision,
        lease_id=first_claim.lease.lease_id,
        fencing_token=first_claim.attempt.fencing_token,
    )
    with _services(
        database_url,
        registry,
        first_claim_at + timedelta(milliseconds=10),
    ) as (_, lifecycle):
        first_start = lifecycle.start_attempt(
            StartAttemptRequest(fence=first_fence, actor=WORKER_1)
        )

    reap_at = _utc(first_claim.lease.expires_at) + timedelta(milliseconds=10)
    with _services(database_url, registry, reap_at) as (_, lifecycle):
        reaped = lifecycle.reap_expired_lease(
            ReapExpiredLeaseRequest(
                run_id=created.run.run_id,
                expected_run_revision=first_start.run.revision,
                actor=REAPER,
            )
        )

    assert reaped.run.status == "retry_wait"
    assert reaped.run.current_attempt_no is None
    assert reaped.run.next_attempt_no == 2
    assert reaped.run.next_fencing_token == 2
    assert reaped.run.next_event_seq == 6

    inspection_engine = get_engine(database_url)
    try:
        with Session(inspection_engine) as session:
            repository = SqlRunRepository(session)
            after_reap = repository.get(created.run.run_id)
            assert after_reap == reaped.run
            assert repository.get_attempt(created.run.run_id, 2) is None
            assert repository.get_current_lease(created.run.run_id) is None
    finally:
        inspection_engine.dispose()

    assert reaped.run.retry_not_before_utc is not None
    second_claim_at = _utc(reaped.run.retry_not_before_utc)
    with _services(database_url, registry, second_claim_at) as (commands, _):
        second_claim = commands.claim_next(
            RunClaimRequest(
                worker=WORKER_2,
                lease_id="lease:attempt:2",
                lease_duration_ns=1_000_000_000,
                trace_id="trace:attempt:2",
            )
        )
    assert second_claim is not None

    stale_fence = first_fence.model_copy(
        update={"expected_run_revision": second_claim.run.revision}
    )
    with _services(
        database_url,
        registry,
        second_claim_at + timedelta(milliseconds=10),
    ) as (_, lifecycle):
        with pytest.raises(Conflict, match="fence"):
            lifecycle.start_attempt(StartAttemptRequest(fence=stale_fence, actor=WORKER_1))

    inspection_engine = get_engine(database_url)
    try:
        with Session(inspection_engine) as session:
            repository = SqlRunRepository(session)
            final_run = repository.get(created.run.run_id)
            attempts = (
                repository.get_attempt(created.run.run_id, 1),
                repository.get_attempt(created.run.run_id, 2),
            )
            events = repository.list_events(
                created.run.run_id,
                after_seq=0,
                limit=100,
            )
    finally:
        inspection_engine.dispose()

    assert final_run == second_claim.run
    assert all(attempt is not None for attempt in attempts)
    assert tuple(attempt.attempt_no for attempt in attempts if attempt is not None) == (1, 2)
    assert tuple(attempt.fencing_token for attempt in attempts if attempt is not None) == (
        1,
        2,
    )
    assert tuple(event.seq for event in events) == tuple(range(1, 7))
    assert tuple(event.event_type for event in events) == (
        "run.queued",
        "attempt.leased",
        "attempt.started",
        "attempt.lease_expired",
        "attempt.retry_scheduled",
        "attempt.leased",
    )
    assert tuple(event.attempt_no for event in events) == (None, 1, 1, 1, 1, 2)
    assert final_run is not None
    assert final_run.next_attempt_no == 3
    assert final_run.next_fencing_token == 3
    assert final_run.next_event_seq == events[-1].seq + 1
