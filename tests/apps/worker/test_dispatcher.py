"""``test_dispatcher`` — worker discovery + dispatch loop (M4c Task 10).

The DB RunStore is the queue authority; the dispatcher rediscovers work by
scanning it every iteration, so lost in-process hints and restarts never lose a
committed Run. Each iteration reaps expired leases (system actor), then claims +
starts + runs one Run with a live lease heartbeat, all fenced.
"""

from __future__ import annotations

import asyncio

import pytest

from gameforge.apps.worker.dispatcher import RunDispatcher
from gameforge.apps.worker.pool import ThreadedBlockingExecutorPool
from gameforge.contracts.errors import (
    Conflict,
    IntegrityViolation,
    InvalidStateTransition,
    QuotaExceeded,
)
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    AttemptStartedDataV1,
    RunAttempt,
    RunEvent,
    RunLease,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.observability import LogRecordV1, TraceContextV1
from gameforge.platform.runs.commands import RunClaimResult
from gameforge.platform.runs.lifecycle import StartAttemptResult
from gameforge.runtime.observability import AlwaysOnSampler, InMemoryExporter, TraceCarrier, Tracer
from gameforge.runtime.observability.logs import StructuredLogger
from tests.platform.m4c.test_terminal_publisher import (
    _attempt,
    _registry_and_definition,
    _run_record,
)


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")
REAPER = AuditActor(principal_id="system:lease-reaper", principal_kind="system")


def _leased_attempt() -> RunAttempt:
    return RunAttempt(
        run_id="run:1",
        attempt_no=1,
        status="leased",
        fencing_token=1,
        worker_principal_id=WORKER.principal_id,
        next_call_ordinal=1,
    )


def _lease() -> RunLease:
    return RunLease(
        lease_id="lease:1",
        run_id="run:1",
        attempt_no=1,
        fencing_token=1,
        lease_version=1,
        owner_principal_id=WORKER.principal_id,
        acquired_at="2026-07-14T12:00:10Z",
        heartbeat_at="2026-07-14T12:00:10Z",
        expires_at="2026-07-14T12:00:40Z",
        status="active",
    )


def _leased_event() -> RunEvent:
    return RunEvent(
        run_id="run:1",
        seq=2,
        event_type="attempt.leased",
        attempt_no=1,
        occurred_at="2026-07-14T12:00:10Z",
        data_schema_version="attempt-leased@1",
        data=AttemptLeasedDataV1(attempt_no=1, lease_expires_at="2026-07-14T12:00:40Z"),
    )


def _started_event(*, trace_id: str | None = None) -> RunEvent:
    return RunEvent(
        run_id="run:1",
        seq=3,
        event_type="attempt.started",
        attempt_no=1,
        occurred_at="2026-07-14T12:00:11Z",
        data_schema_version="attempt-started@1",
        data=AttemptStartedDataV1(
            attempt_no=1,
            started_at="2026-07-14T12:00:11Z",
            attempt_deadline_utc="2026-07-14T12:30:00Z",
        ),
        trace_id=trace_id,
    )


class _Clock:
    def now_utc(self):
        from datetime import UTC, datetime

        return datetime(2026, 7, 14, 12, 0, 20, tzinfo=UTC)


class _CapturingLogStore:
    def __init__(self) -> None:
        self.records: list[LogRecordV1] = []

    def append(self, record: LogRecordV1) -> None:
        self.records.append(record)


def _structured_logger(store=None) -> StructuredLogger:
    next_id = 0

    def id_generator() -> str:
        nonlocal next_id
        next_id += 1
        return f"log:test:{next_id}"

    return StructuredLogger(
        service="gameforge-worker",
        store=store or _CapturingLogStore(),
        clock=_Clock(),
        id_generator=id_generator,
    )


class _FakeClaim:
    def __init__(self, results) -> None:
        self._results = list(results)
        self.requests = []

    def claim_next(self, request):
        self.requests.append(request)
        if not self._results:
            return None
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeLifecycle:
    def __init__(
        self,
        run,
        attempt,
        lease,
        *,
        reap_raises=None,
        sweep_raises=None,
    ) -> None:
        self._run = run
        self._attempt = attempt
        self._lease = lease
        self._reap_raises = dict(reap_raises or {})
        self._sweep_raises = dict(sweep_raises or {})
        self.reaped: list[tuple[str, int, str]] = []
        self.swept: list[tuple[str, int, str]] = []
        self.started = 0
        self.started_trace_ids: list[str | None] = []

    def start_attempt(self, request):
        self.started += 1
        self.started_trace_ids.append(request.trace_id)
        running_run = self._run.model_copy(
            update={"status": "running", "revision": self._run.revision + 1}
        )
        running_attempt = self._attempt.model_copy(
            update={
                "status": "running",
                "trace_id": request.trace_id,
                "started_at": "2026-07-14T12:00:11Z",
                "attempt_deadline_utc": "2026-07-14T12:30:00Z",
            }
        )
        return StartAttemptResult(
            previous=self._run,
            run=running_run,
            attempt=running_attempt,
            lease=self._lease,
            event=_started_event(trace_id=request.trace_id),
        )

    def reap_expired_lease(self, request):
        raiser = self._reap_raises.get(request.run_id)
        if raiser is not None:
            raise raiser
        self.reaped.append(
            (request.run_id, request.expected_run_revision, request.actor.principal_kind)
        )
        return None

    def sweep_timeout(self, request):
        raiser = self._sweep_raises.get(request.run_id)
        if raiser is not None:
            raise raiser
        self.swept.append(
            (request.run_id, request.expected_run_revision, request.actor.principal_kind)
        )
        return None


class _FakeHeartbeat:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def run(self, stop: asyncio.Event) -> None:
        self.started = True
        await stop.wait()
        self.stopped = True


class _FakeRunner:
    def __init__(self, heartbeat: _FakeHeartbeat) -> None:
        self.calls = []
        self._heartbeat = heartbeat

    async def run_attempt(self, *, run, attempt, lease, deadline_utc):
        # The heartbeat coroutine is live while the (blocking) attempt runs.
        for _ in range(3):
            await asyncio.sleep(0)
        assert self._heartbeat.started is True
        self.calls.append((run.run_id, attempt.attempt_no, deadline_utc))
        return "published"


def _dispatcher(
    *,
    claim_results,
    expired,
    heartbeat,
    runner,
    lifecycle,
    pool,
    timed_out=(),
    on_contention=None,
    max_in_flight=1,
    tracer=None,
    logger=None,
    operational_metrics=None,
):
    return RunDispatcher(
        claim_service=_FakeClaim(claim_results),
        lifecycle=lifecycle,
        reaper_scan=lambda *, now_utc, limit: expired,
        timeout_scan=lambda *, now_utc, limit: timed_out,
        runner=runner,
        heartbeat_factory=lambda **_: heartbeat,
        control_pool=pool,
        clock=_Clock(),
        worker_actor=WORKER,
        reaper_actor=REAPER,
        lease_duration_ns=30_000_000_000,
        lease_id_factory=lambda: "lease:1",
        on_contention=on_contention,
        max_in_flight=max_in_flight,
        tracer=tracer or Tracer(exporter=InMemoryExporter(capacity=32)),
        logger=logger or _structured_logger(),
        operational_metrics=operational_metrics,
    )


def _claim_result(run, attempt, lease) -> RunClaimResult:
    return RunClaimResult(
        previous=run,
        run=run,
        attempt=attempt,
        lease=lease,
        event=_leased_event(),
    )


def test_dispatch_once_reaps_claims_starts_and_runs_with_live_heartbeat() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    leased_attempt = _leased_attempt()
    lease = _lease()
    expired_run = run.model_copy(update={"run_id": "run:expired", "revision": 7})
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, _attempt(), lease)

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[_claim_result(run, leased_attempt, lease)],
            expired=(expired_run,),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
        )
        worked = asyncio.run(dispatcher.dispatch_once())

    assert worked is True
    # The expired lease was reaped first, with the system actor + its own revision.
    assert lifecycle.reaped == [("run:expired", 7, "system")]
    assert lifecycle.started == 1
    # The runner executed the started attempt and the heartbeat was stopped after.
    assert runner.calls and runner.calls[0][0] == run.run_id
    assert heartbeat.started is True and heartbeat.stopped is True


def test_dispatch_attempt_metrics_follow_committed_boundaries_and_are_best_effort() -> None:
    class Metrics:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def record_worker_attempt(self, *, run_kind: str, phase: str) -> None:
            self.calls.append((run_kind, phase))
            raise OSError("metric exporter unavailable")

    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    leased_attempt = _leased_attempt()
    lease = _lease()
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, _attempt(), lease)
    metrics = Metrics()

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[_claim_result(run, leased_attempt, lease)],
            expired=(),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
            operational_metrics=metrics,
        )
        worked = asyncio.run(dispatcher.dispatch_once())

    assert worked is True
    assert metrics.calls == [
        (run.kind.kind, "started"),
        (run.kind.kind, "terminal_published"),
    ]


def test_idle_iteration_returns_false_and_a_missed_hint_loses_nothing() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    leased_attempt = _leased_attempt()
    lease = _lease()
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, _attempt(), lease)

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        # No claim candidate the first pass; the second pass (a re-scan of the DB,
        # not a hint) discovers the same queued Run.
        claim = _FakeClaim([None, _claim_result(run, leased_attempt, lease)])
        dispatcher = RunDispatcher(
            claim_service=claim,
            lifecycle=lifecycle,
            reaper_scan=lambda *, now_utc, limit: (),
            timeout_scan=lambda *, now_utc, limit: (),
            runner=runner,
            heartbeat_factory=lambda **_: heartbeat,
            control_pool=pool,
            clock=_Clock(),
            worker_actor=WORKER,
            reaper_actor=REAPER,
            lease_duration_ns=30_000_000_000,
            lease_id_factory=lambda: "lease:1",
            tracer=Tracer(exporter=InMemoryExporter(capacity=32)),
            logger=_structured_logger(),
        )
        first = asyncio.run(dispatcher.dispatch_once())
        second = asyncio.run(dispatcher.dispatch_once())

    assert first is False  # nothing to do this iteration
    assert second is True  # rediscovered by DB scan, not by a hint
    assert runner.calls and runner.calls[0][0] == run.run_id


def test_dispatch_once_survives_a_lost_claim_race_and_processes_the_next_run() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    leased_attempt = _leased_attempt()
    lease = _lease()
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, _attempt(), lease)
    contention: list[tuple[str, str]] = []

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            # A competing worker wins the revision CAS first (Conflict), then this
            # worker claims the next scan. The benign race must NOT kill the loop.
            claim_results=[
                Conflict("run revision differs"),
                _claim_result(run, leased_attempt, lease),
            ],
            expired=(),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
            on_contention=lambda op, exc: contention.append((op, type(exc).__name__)),
        )
        first = asyncio.run(dispatcher.dispatch_once())
        second = asyncio.run(dispatcher.dispatch_once())

    assert first is False  # lost the claim race → skipped, did not raise
    assert second is True  # the next scan claimed and ran
    assert runner.calls and runner.calls[0][0] == run.run_id
    assert ("claim", "Conflict") in contention


def test_reaper_conflict_is_skipped_and_the_next_expired_run_is_reaped() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    lease = _lease()
    expired_a = run.model_copy(update={"run_id": "run:already-reaped", "revision": 5})
    expired_b = run.model_copy(update={"run_id": "run:reapable", "revision": 9})
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    # The first expired Run was already reaped / just heartbeat-renewed by its owner.
    lifecycle = _FakeLifecycle(
        run,
        _attempt(),
        lease,
        reap_raises={"run:already-reaped": InvalidStateTransition("already reaped")},
    )
    contention: list[tuple[str, str]] = []

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[None],
            expired=(expired_a, expired_b),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
            on_contention=lambda op, exc: contention.append((op, type(exc).__name__)),
        )
        worked = asyncio.run(dispatcher.dispatch_once())

    assert worked is False  # nothing claimable, but no crash
    # The conflicted reap was skipped; the reapable one still settled.
    assert lifecycle.reaped == [("run:reapable", 9, "system")]
    assert ("reap", "InvalidStateTransition") in contention


def test_inactive_deadline_scan_sweeps_before_claiming() -> None:
    _, definition = _registry_and_definition()
    timed_out = _run_record(definition).model_copy(
        update={"run_id": "run:queue-timeout", "revision": 4, "status": "queued"}
    )
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(timed_out, _attempt(), _lease())

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[None],
            expired=(),
            timed_out=(timed_out,),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
        )
        worked = asyncio.run(dispatcher.dispatch_once())

    assert worked is False
    assert lifecycle.swept == [("run:queue-timeout", 4, "system")]


def test_integrity_violations_are_not_misclassified_as_benign_contention() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition)
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, _attempt(), _lease())

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[IntegrityViolation("cost authority is corrupt")],
            expired=(),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
        )
        with pytest.raises(IntegrityViolation, match="cost authority"):
            asyncio.run(dispatcher.dispatch_once())


def test_claim_quota_saturation_is_a_retryable_idle_iteration() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition)
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, _attempt(), _lease())
    contention: list[tuple[str, str]] = []

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[QuotaExceeded("concurrent run budget is saturated")],
            expired=(),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
            on_contention=lambda op, exc: contention.append((op, type(exc).__name__)),
        )
        assert asyncio.run(dispatcher.dispatch_once()) is False

    assert contention == [("claim_quota", "QuotaExceeded")]


def test_run_forever_bounds_parallel_claims_and_stops_claiming_before_drain() -> None:
    _, definition = _registry_and_definition()
    base = _run_record(definition).model_copy(update={"status": "leased"})
    claims = [
        _claim_result(
            base.model_copy(update={"run_id": f"run:{ordinal}"}),
            _leased_attempt().model_copy(update={"run_id": f"run:{ordinal}"}),
            _lease().model_copy(
                update={"run_id": f"run:{ordinal}", "lease_id": f"lease:{ordinal}"}
            ),
        )
        for ordinal in range(1, 4)
    ]
    claim_service = _FakeClaim(claims)
    lifecycle = _FakeLifecycle(base, _attempt(), _lease())
    release = asyncio.Event()
    started: list[str] = []
    active = 0
    peak = 0

    async def execute(claim) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.append(claim.run.run_id)
        try:
            await release.wait()
        finally:
            active -= 1

    async def scenario() -> None:
        with ThreadedBlockingExecutorPool(max_workers=2) as pool:
            dispatcher = RunDispatcher(
                claim_service=claim_service,
                lifecycle=lifecycle,
                reaper_scan=lambda *, now_utc, limit: (),
                timeout_scan=lambda *, now_utc, limit: (),
                runner=_FakeRunner(_FakeHeartbeat()),
                heartbeat_factory=lambda **_: _FakeHeartbeat(),
                control_pool=pool,
                clock=_Clock(),
                worker_actor=WORKER,
                reaper_actor=REAPER,
                lease_duration_ns=30_000_000_000,
                lease_id_factory=lambda: "lease:new",
                max_in_flight=2,
                poll_interval_s=0.01,
                tracer=Tracer(exporter=InMemoryExporter(capacity=32)),
                logger=_structured_logger(),
            )
            dispatcher._execute_guarded = execute  # type: ignore[method-assign]
            stop = asyncio.Event()
            loop_task = asyncio.create_task(dispatcher.run_forever(stop=stop))
            for _ in range(100):
                if len(started) == 2:
                    break
                await asyncio.sleep(0.01)
            assert len(started) == 2
            assert len(claim_service.requests) == 2
            stop.set()
            await asyncio.sleep(0)
            assert len(claim_service.requests) == 2
            release.set()
            await loop_task

    asyncio.run(scenario())
    assert peak == 2
    assert started == ["run:1", "run:2"]


def test_run_forever_keeps_reaping_while_a_blocking_attempt_is_in_flight() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    claim = _claim_result(run, _leased_attempt(), _lease())
    claim_service = _FakeClaim([claim, None, None])
    lifecycle = _FakeLifecycle(run, _attempt(), _lease())
    release = asyncio.Event()
    execution_started = asyncio.Event()
    scan_count = 0
    yielded = False

    def reaper_scan(*, now_utc, limit):
        nonlocal scan_count, yielded
        del now_utc, limit
        scan_count += 1
        if scan_count >= 2 and not yielded:
            yielded = True
            return (run.model_copy(update={"run_id": "run:expired", "revision": 9}),)
        return ()

    async def execute(_claim) -> None:
        execution_started.set()
        await release.wait()

    async def scenario() -> None:
        with ThreadedBlockingExecutorPool(max_workers=2) as pool:
            dispatcher = RunDispatcher(
                claim_service=claim_service,
                lifecycle=lifecycle,
                reaper_scan=reaper_scan,
                timeout_scan=lambda *, now_utc, limit: (),
                runner=_FakeRunner(_FakeHeartbeat()),
                heartbeat_factory=lambda **_: _FakeHeartbeat(),
                control_pool=pool,
                clock=_Clock(),
                worker_actor=WORKER,
                reaper_actor=REAPER,
                lease_duration_ns=30_000_000_000,
                lease_id_factory=lambda: "lease:new",
                max_in_flight=1,
                poll_interval_s=0.01,
                tracer=Tracer(exporter=InMemoryExporter(capacity=32)),
                logger=_structured_logger(),
            )
            dispatcher._execute_guarded = execute  # type: ignore[method-assign]
            stop = asyncio.Event()
            loop_task = asyncio.create_task(dispatcher.run_forever(stop=stop))
            await asyncio.wait_for(execution_started.wait(), timeout=2.0)
            for _ in range(100):
                if lifecycle.reaped:
                    break
                await asyncio.sleep(0.01)
            assert lifecycle.reaped == [("run:expired", 9, "system")]
            stop.set()
            release.set()
            await loop_task

    asyncio.run(scenario())


def test_attempt_consumer_span_uses_persisted_parent_and_binds_actual_trace_id() -> None:
    _, definition = _registry_and_definition()
    parent = TraceContextV1(
        trace_id="a" * 32,
        span_id="b" * 16,
        trace_flags="01",
        trace_state="vendor=state",
    )
    run = _run_record(definition).model_copy(
        update={
            "status": "leased",
            "dispatch_trace_carrier": TraceCarrier.inject(parent),
        }
    )
    leased_attempt = _leased_attempt()
    lease = _lease()
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, leased_attempt, lease)
    exporter = InMemoryExporter(capacity=8)
    tracer = Tracer(exporter=exporter, sampler=AlwaysOnSampler())

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[_claim_result(run, leased_attempt, lease)],
            expired=(),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
            tracer=tracer,
        )
        asyncio.run(dispatcher.dispatch_once())

    assert lifecycle.started == 1
    assert runner.calls
    worker_span = next(span for span in exporter.spans if span.name == "worker.attempt")
    assert worker_span.trace_id == parent.trace_id
    assert worker_span.parent_span_id == parent.span_id
    assert worker_span.span_id != parent.span_id
    assert lifecycle.started_trace_ids == [worker_span.trace_id]
    assert worker_span.trace_id == run.dispatch_trace_carrier.traceparent.split("-")[1]


def test_attempt_log_inherits_worker_span_and_binds_run_identity() -> None:
    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    leased_attempt = _leased_attempt()
    lease = _lease()
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, leased_attempt, lease)
    exporter = InMemoryExporter(capacity=8)
    tracer = Tracer(exporter=exporter, sampler=AlwaysOnSampler())
    log_store = _CapturingLogStore()

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[_claim_result(run, leased_attempt, lease)],
            expired=(),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
            tracer=tracer,
            logger=_structured_logger(log_store),
        )
        asyncio.run(dispatcher.dispatch_once())

    worker_span = next(span for span in exporter.spans if span.name == "worker.attempt")
    [record] = log_store.records
    assert record.event_name == "worker.attempt.started"
    assert record.run_id == run.run_id
    assert record.trace_id == worker_span.trace_id
    assert record.span_id == worker_span.span_id
    assert record.fields == {
        "attempt_no": leased_attempt.attempt_no,
        "run_kind": run.kind.kind,
        "run_kind_version": run.kind.version,
    }


def test_attempt_log_store_failure_is_fail_open() -> None:
    class BrokenStore:
        def append(self, record: LogRecordV1) -> None:
            del record
            raise OSError("telemetry disk unavailable")

    _, definition = _registry_and_definition()
    run = _run_record(definition).model_copy(update={"status": "leased"})
    leased_attempt = _leased_attempt()
    lease = _lease()
    heartbeat = _FakeHeartbeat()
    runner = _FakeRunner(heartbeat)
    lifecycle = _FakeLifecycle(run, leased_attempt, lease)
    logger = _structured_logger(BrokenStore())

    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        dispatcher = _dispatcher(
            claim_results=[_claim_result(run, leased_attempt, lease)],
            expired=(),
            heartbeat=heartbeat,
            runner=runner,
            lifecycle=lifecycle,
            pool=pool,
            logger=logger,
        )
        worked = asyncio.run(dispatcher.dispatch_once())

    assert worked is True
    assert runner.calls and runner.calls[0][:2] == (run.run_id, leased_attempt.attempt_no)
    assert logger.dropped_count == 1
