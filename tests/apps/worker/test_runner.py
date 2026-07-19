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
from types import SimpleNamespace

import pytest

from gameforge.apps.worker.executor import ExecutorContext, redacted_execution_failure
from gameforge.apps.worker.pool import ThreadedBlockingExecutorPool
from gameforge.apps.worker.runner import AttemptRunner
from gameforge.apps.worker.terminal import WorkerTerminalPublisher
from gameforge.contracts.jobs import (
    AttemptProgressDataV1,
    PreparedRunFailure,
    PreparedRunOutcome,
    RunLease,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.errors import (
    AttemptFenceConflict,
    Conflict,
    DependencyUnavailable,
    IntegrityViolation,
    PermanentDependencyFailure,
    QuotaExceeded,
)
from gameforge.platform.runs.lifecycle import AttemptWriteFence
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


class _CapturingProgress:
    def __init__(self) -> None:
        self.published: list[tuple[AttemptWriteFence, AttemptProgressDataV1]] = []

    def publish_progress(self, *, fence, data, actor):
        assert actor == WORKER
        self.published.append((fence, data))
        return data


def _runner(resolve_executor, terminal, pool, *, read_run_revision=None, progress=None):
    registry, _ = _registry_and_definition()
    return AttemptRunner(
        executor_pool=pool,
        control_pool=pool,
        resolve_executor=resolve_executor,
        model_bridge_factory=lambda **_: object(),
        terminal=terminal,
        progress=progress,
        read_run_revision=read_run_revision or (lambda run_id: 3),
        resolve_failure_classifier=lambda run: registry.get_failure_classifier(
            run.failure_classifier
        ),
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


def test_terminal_notify_hint_failure_cannot_reverse_committed_outcome() -> None:
    run, attempt, lease = _context_run()
    prepared = _prepared_success(artifacts=(_checker_artifact(_Blobs()),))
    expected = SimpleNamespace(run=run)

    class Lifecycle:
        request = None

        def publish_attempt_outcome(self, request):
            self.request = request
            return expected

    def broken_notify(_run_id: str) -> None:
        raise OSError("hint transport failed")

    lifecycle = Lifecycle()
    terminal = WorkerTerminalPublisher(
        lifecycle,  # type: ignore[arg-type]
        notify=broken_notify,
    )

    result = terminal.publish(
        fence=AttemptWriteFence(
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            expected_run_revision=run.revision,
            lease_id=lease.lease_id,
            fencing_token=attempt.fencing_token,
        ),
        outcome=prepared,
        actor=WORKER,
    )

    assert result is expected
    assert lifecycle.request.prepared_outcome is prepared


def test_redacted_failure_helper_uses_run_classifier() -> None:
    run, attempt, _ = _context_run()
    registry, _ = _registry_and_definition()
    classifier = registry.get_failure_classifier(run.failure_classifier)
    assert classifier is not None
    failure = redacted_execution_failure(
        run=run,
        attempt=attempt,
        classifier=classifier,
        error=RuntimeError("secret"),
    )
    assert failure.classifier == run.failure_classifier
    assert failure.intrinsic_retry_eligible is False


@pytest.mark.parametrize(
    ("error", "cause", "failure_class"),
    [
        (IntegrityViolation("secret payload"), "integrity_violation", "integrity"),
        (QuotaExceeded("secret budget"), "quota_exceeded", "quota"),
        (TimeoutError("secret timeout"), "timed_out", "timeout"),
    ],
)
def test_typed_executor_faults_use_frozen_classifier_without_leaking(
    error, cause, failure_class
) -> None:
    run, attempt, lease = _context_run()

    def exploding(context: ExecutorContext) -> PreparedRunOutcome:
        raise error

    terminal = _CapturingTerminal()
    with ThreadedBlockingExecutorPool(max_workers=1) as pool:
        asyncio.run(
            _runner(lambda r: exploding, terminal, pool).run_attempt(
                run=run, attempt=attempt, lease=lease, deadline_utc=None
            )
        )

    outcome = terminal.published[0][1]
    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == cause
    assert outcome.failure_class == failure_class
    assert "secret" not in outcome.redacted_message


def test_complete_typed_dependency_is_retryable_but_incomplete_metadata_is_not() -> None:
    run, attempt, _ = _context_run()
    registry, _ = _registry_and_definition()
    classifier = registry.get_failure_classifier(run.failure_classifier)
    assert classifier is not None
    complete = redacted_execution_failure(
        run=run,
        attempt=attempt,
        classifier=classifier,
        error=DependencyUnavailable(
            "provider leaked detail",
            dependency_kind="model_provider",
            dependency_id="gateway:primary",
            operation_code="model.complete",
            classifier_code="gateway_unavailable",
        ),
    )
    incomplete = redacted_execution_failure(
        run=run,
        attempt=attempt,
        classifier=classifier,
        error=DependencyUnavailable("secret", component="gateway"),
    )

    assert complete.cause_code == "dependency_unavailable"
    assert complete.failure_class == "transient_dependency"
    assert complete.intrinsic_retry_eligible is True
    assert complete.dependency is not None
    assert incomplete.cause_code == "execution_failed"
    assert incomplete.dependency is None


def test_permanent_dependency_failure_uses_nonretryable_frozen_rule() -> None:
    run, attempt, _ = _context_run()
    registry, _ = _registry_and_definition()
    classifier = registry.get_failure_classifier(run.failure_classifier)
    assert classifier is not None

    failure = redacted_execution_failure(
        run=run,
        attempt=attempt,
        classifier=classifier,
        error=PermanentDependencyFailure(
            "provider detail must remain redacted",
            dependency_kind="model_provider",
            dependency_id="openai:model:sha256:abc",
            operation_code="model.complete",
            classifier_code="provider_authentication_rejected",
            upstream_status_code=401,
        ),
    )

    assert failure.cause_code == "permanent_dependency_failed"
    assert failure.failure_class == "permanent_dependency"
    assert failure.intrinsic_retry_eligible is False
    assert failure.dependency is not None
    assert failure.dependency.upstream_status_code == 401
    assert "provider detail" not in failure.redacted_message


def test_executor_conflict_is_classified_instead_of_masquerading_as_fence_loss() -> None:
    run, attempt, lease = _context_run()

    def conflicting(context: ExecutorContext) -> PreparedRunOutcome:
        raise Conflict("domain conflict with secret detail")

    terminal = _CapturingTerminal()
    with ThreadedBlockingExecutorPool(max_workers=1) as pool:
        asyncio.run(
            _runner(lambda r: conflicting, terminal, pool).run_attempt(
                run=run, attempt=attempt, lease=lease, deadline_utc=None
            )
        )
    outcome = terminal.published[0][1]
    assert isinstance(outcome, PreparedRunFailure)
    assert outcome.cause_code == "execution_failed"
    assert "secret" not in outcome.redacted_message


def test_dedicated_fence_conflict_is_not_reclassified_into_a_terminal_outcome() -> None:
    run, attempt, lease = _context_run()

    def stale(context: ExecutorContext) -> PreparedRunOutcome:
        raise AttemptFenceConflict("stale secret fence")

    terminal = _CapturingTerminal()
    with ThreadedBlockingExecutorPool(max_workers=1) as pool:
        with pytest.raises(AttemptFenceConflict):
            asyncio.run(
                _runner(lambda r: stale, terminal, pool).run_attempt(
                    run=run, attempt=attempt, lease=lease, deadline_utc=None
                )
            )
    assert terminal.published == []


def test_terminal_fence_uses_fresh_current_revision_not_the_stale_claim_revision() -> None:
    run, attempt, lease = _context_run()  # run.revision == 3 at claim/start time
    prepared = _prepared_success(artifacts=(_checker_artifact(_Blobs()),))

    def executor(context: ExecutorContext) -> PreparedRunOutcome:
        return prepared

    terminal = _CapturingTerminal()
    # A mid-attempt publish_progress / RECORD capture bumped the run revision to 5.
    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        runner = _runner(lambda r: executor, terminal, pool, read_run_revision=lambda run_id: 5)
        asyncio.run(runner.run_attempt(run=run, attempt=attempt, lease=lease, deadline_utc=None))

    fence, _ = terminal.published[0]
    assert run.revision == 3  # the claim-time revision is now stale
    assert fence.expected_run_revision == 5  # terminal fences against the CURRENT revision
    # Attempt-stable fields are unchanged.
    assert fence.attempt_no == attempt.attempt_no
    assert fence.lease_id == lease.lease_id
    assert fence.fencing_token == attempt.fencing_token


def test_executor_progress_uses_a_fresh_fence_and_terminal_rereads_after_it() -> None:
    run, attempt, lease = _context_run()
    prepared = _prepared_success(artifacts=(_checker_artifact(_Blobs()),))
    progress = _CapturingProgress()
    revisions = iter((5, 6))

    def executor(context: ExecutorContext) -> PreparedRunOutcome:
        assert context.progress_publisher is not None
        context.progress_publisher(
            AttemptProgressDataV1(
                attempt_no=attempt.attempt_no,
                phase_code="generation.preliminary_gate",
                completed_units=1,
                total_units=1,
            )
        )
        return prepared

    terminal = _CapturingTerminal()
    with ThreadedBlockingExecutorPool(max_workers=2) as pool:
        runner = _runner(
            lambda r: executor,
            terminal,
            pool,
            progress=progress,
            read_run_revision=lambda run_id: next(revisions),
        )
        asyncio.run(runner.run_attempt(run=run, attempt=attempt, lease=lease, deadline_utc=None))

    assert progress.published[0][0].expected_run_revision == 5
    assert progress.published[0][1].phase_code == "generation.preliminary_gate"
    assert terminal.published[0][0].expected_run_revision == 6
