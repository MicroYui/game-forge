"""Seam #3 — generic executor dispatch + fenced terminal hand-off (M4c Task 10).

The runner drives one already-started, fenced attempt: it resolves the Run kind's
executor GENERICALLY by ``executor_key``, runs it OFF the event loop on the
injected bounded pool, and hands the single sealed ``PreparedRunOutcome`` to the
terminal sink. An executor that raises never escapes the loop: it becomes a
classified, redacted ``PreparedRunFailure`` that flows through the terminal
outcome policy.
"""

from __future__ import annotations

import asyncio
import threading

from gameforge.apps.worker.executor import ExecutorContext, redacted_execution_failure
from gameforge.apps.worker.pool import ThreadedBlockingExecutorPool
from gameforge.apps.worker.runner import AttemptRunner
from gameforge.contracts.jobs import (
    PreparedRunFailure,
    PreparedRunOutcome,
    RunLease,
)
from gameforge.contracts.lineage import AuditActor
from tests.platform.m4c.test_terminal_publisher import (
    _attempt,
    _prepared_success,
    _checker_artifact,
    _registry_and_definition,
    _run_record,
)
from tests.platform.m4c.test_terminal_publisher import _Blobs


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")


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


class _CapturingTerminal:
    def __init__(self) -> None:
        self.published: list[tuple[object, PreparedRunOutcome]] = []

    def publish(self, *, fence, outcome: PreparedRunOutcome, actor):
        self.published.append((fence, outcome))
        return outcome  # the runner returns whatever the sink returns


def _runner(resolve_executor, terminal, pool):
    return AttemptRunner(
        pool=pool,
        resolve_executor=resolve_executor,
        model_bridge_factory=lambda **_: object(),
        terminal=terminal,
        worker_actor=WORKER,
    )


def _context_run():
    _, definition = _registry_and_definition()
    return _run_record(definition), _attempt(), _lease()


def test_generic_dispatch_runs_executor_off_loop_and_hands_outcome_to_terminal() -> None:
    run, attempt, lease = _context_run()
    prepared = _prepared_success(artifacts=(_checker_artifact(_Blobs()),))
    seen: dict[str, object] = {}
    loop_thread = threading.get_ident()

    def executor(context: ExecutorContext) -> PreparedRunOutcome:
        seen["thread"] = threading.get_ident()
        seen["run_id"] = context.run.run_id
        seen["attempt_no"] = context.attempt.attempt_no
        seen["has_bridge"] = context.model_bridge is not None
        return prepared

    terminal = _CapturingTerminal()
    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        runner = _runner(lambda r: executor, terminal, pool)
        result = asyncio.run(
            runner.run_attempt(run=run, attempt=attempt, lease=lease, deadline_utc=None)
        )

    assert result is prepared
    assert len(terminal.published) == 1
    fence, outcome = terminal.published[0]
    assert outcome is prepared
    # Fenced hand-off uses the exact attempt identity.
    assert fence.run_id == run.run_id
    assert fence.attempt_no == attempt.attempt_no
    assert fence.expected_run_revision == run.revision
    assert fence.lease_id == lease.lease_id
    assert fence.fencing_token == attempt.fencing_token
    # The executor ran on a pool thread, not the event-loop thread.
    assert seen["thread"] != loop_thread
    assert seen["run_id"] == run.run_id
    assert seen["has_bridge"] is True


def test_executor_exception_becomes_redacted_failure_through_terminal() -> None:
    run, attempt, lease = _context_run()

    def exploding(context: ExecutorContext) -> PreparedRunOutcome:
        raise RuntimeError("secret internal detail: password=hunter2")

    terminal = _CapturingTerminal()
    with ThreadedBlockingExecutorPool(max_workers=1) as pool:
        runner = _runner(lambda r: exploding, terminal, pool)
        asyncio.run(runner.run_attempt(run=run, attempt=attempt, lease=lease, deadline_utc=None))

    assert len(terminal.published) == 1
    _, outcome = terminal.published[0]
    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.failure_class == "execution"
    assert outcome.run_id == run.run_id
    assert outcome.attempt_no == attempt.attempt_no
    # The redacted message never leaks the raised exception text.
    assert "hunter2" not in outcome.redacted_message
    assert "password" not in outcome.redacted_message


def test_missing_executor_is_fenced_failure_not_a_leaked_exception() -> None:
    run, attempt, lease = _context_run()

    def resolver(_run):
        raise KeyError("no executor registered")

    terminal = _CapturingTerminal()
    with ThreadedBlockingExecutorPool(max_workers=1) as pool:
        runner = _runner(resolver, terminal, pool)
        asyncio.run(runner.run_attempt(run=run, attempt=attempt, lease=lease, deadline_utc=None))

    assert len(terminal.published) == 1
    _, outcome = terminal.published[0]
    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.failure_class == "execution"


def test_redacted_failure_helper_uses_run_classifier() -> None:
    run, attempt, _ = _context_run()
    failure = redacted_execution_failure(run=run, attempt=attempt)
    assert failure.classifier == run.failure_classifier
    assert failure.intrinsic_retry_eligible is False
