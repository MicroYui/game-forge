"""Drive one fenced Run attempt: generic executor dispatch + terminal hand-off.

The dispatcher claims a Run and starts its attempt; :class:`AttemptRunner` then
runs the Run kind's executor (resolved generically by ``executor_key``) OFF the
event loop on the injected bounded pool while the heartbeat keeps the lease live,
and hands the single sealed ``PreparedRunOutcome`` to the terminal sink under the
attempt write fence. An executor that raises — or a Run kind whose executor is
not registered — never escapes the loop: it becomes a classified, redacted
``PreparedRunFailure`` that still flows through the terminal outcome policy so
cost/lease/audit close correctly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from gameforge.apps.worker.executor import (
    ExecutorContext,
    RunExecutor,
    WorkerModelBridgePort,
    redacted_execution_failure,
)
from gameforge.apps.worker.pool import BlockingExecutorPool
from gameforge.contracts.jobs import (
    FailureClassifierV1,
    PreparedRunOutcome,
    RunAttempt,
    RunLease,
    RunRecord,
)
from gameforge.contracts.errors import (
    AttemptFenceConflict,
    AttemptFenceStateRejected,
    IntegrityViolation,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.lifecycle import AttemptWriteFence


class ExecutorResolver(Protocol):
    """Resolve the generic executor for a Run (kind -> executor_key -> callable)."""

    def __call__(self, run: RunRecord) -> RunExecutor: ...


class TerminalSink(Protocol):
    """Publish the sealed outcome through the fenced terminal authority."""

    def publish(
        self,
        *,
        fence: AttemptWriteFence,
        outcome: PreparedRunOutcome,
        actor: AuditActor,
    ) -> object: ...


ModelBridgeFactory = Callable[..., WorkerModelBridgePort]
RunRevisionReader = Callable[[str], int]
FailureClassifierResolver = Callable[[RunRecord], FailureClassifierV1]


class AttemptRunner:
    def __init__(
        self,
        *,
        executor_pool: BlockingExecutorPool,
        control_pool: BlockingExecutorPool,
        resolve_executor: ExecutorResolver,
        model_bridge_factory: ModelBridgeFactory,
        terminal: TerminalSink,
        read_run_revision: RunRevisionReader,
        resolve_failure_classifier: FailureClassifierResolver,
        worker_actor: AuditActor,
    ) -> None:
        # Blocking executor domain work runs on the bounded executor lane; the
        # revision read and terminal publication run on the ungated control lane so
        # they are never starved by a saturated executor pool.
        self._executor_pool = executor_pool
        self._control_pool = control_pool
        self._resolve_executor = resolve_executor
        self._model_bridge_factory = model_bridge_factory
        self._terminal = terminal
        self._read_run_revision = read_run_revision
        self._resolve_failure_classifier = resolve_failure_classifier
        self._worker_actor = worker_actor

    async def run_attempt(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        deadline_utc: datetime | None,
    ) -> object:
        outcome = await self._execute(
            run=run,
            attempt=attempt,
            lease=lease,
            deadline_utc=deadline_utc,
        )
        # Build the terminal fence from a FRESH read of the current run revision:
        # publish_progress and (once wired) RECORD response capture bump the run
        # revision mid-attempt, so the claim-time revision would be stale here.
        # lease_id / fencing_token / attempt_no are attempt-stable.
        current_revision = await self._control_pool.run(lambda: self._read_run_revision(run.run_id))
        fence = AttemptWriteFence(
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            expected_run_revision=current_revision,
            lease_id=lease.lease_id,
            fencing_token=attempt.fencing_token,
        )
        # The terminal publication is one authoritative DB transaction on the
        # control lane so the heartbeat coroutine stays responsive.
        return await self._control_pool.run(
            lambda: self._terminal.publish(fence=fence, outcome=outcome, actor=self._worker_actor)
        )

    async def _execute(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        deadline_utc: datetime | None,
    ) -> PreparedRunOutcome:
        try:

            def execute() -> PreparedRunOutcome:
                # Bridge composition may load exact DB/ObjectStore replay authority;
                # keep it on the same bounded blocking lane as the executor.
                bridge = self._model_bridge_factory(run=run, attempt=attempt, lease=lease)
                context = ExecutorContext(
                    run=run,
                    attempt=attempt,
                    payload=run.payload,
                    deadline_utc=deadline_utc,
                    model_bridge=bridge,
                )
                executor = self._resolve_executor(run)
                return executor(context)

            return await self._executor_pool.run(execute)
        except asyncio.CancelledError:
            raise
        except (AttemptFenceConflict, AttemptFenceStateRejected):
            # A bridge/lifecycle fence loss is control-plane authority, not an
            # executor business failure.  Never let a stale worker manufacture a
            # new terminal outcome from it.
            raise
        except Exception as exc:
            # Executor faults (including a missing/unresolvable executor) are
            # classified into a conservative, non-leaking failure and published
            # through the terminal policy rather than crashing the worker loop.
            classifier = self._resolve_failure_classifier(run)
            if not isinstance(classifier, FailureClassifierV1):
                raise IntegrityViolation(
                    "worker failure classifier authority returned an invalid value"
                )
            return redacted_execution_failure(
                run=run,
                attempt=attempt,
                classifier=classifier,
                error=exc,
            )


__all__ = [
    "AttemptRunner",
    "ExecutorResolver",
    "FailureClassifierResolver",
    "ModelBridgeFactory",
    "RunRevisionReader",
    "TerminalSink",
]
