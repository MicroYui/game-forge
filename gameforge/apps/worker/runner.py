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
    PreparedRunOutcome,
    RunAttempt,
    RunLease,
    RunRecord,
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


class AttemptRunner:
    def __init__(
        self,
        *,
        pool: BlockingExecutorPool,
        resolve_executor: ExecutorResolver,
        model_bridge_factory: ModelBridgeFactory,
        terminal: TerminalSink,
        worker_actor: AuditActor,
    ) -> None:
        self._pool = pool
        self._resolve_executor = resolve_executor
        self._model_bridge_factory = model_bridge_factory
        self._terminal = terminal
        self._worker_actor = worker_actor

    async def run_attempt(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        deadline_utc: datetime | None,
    ) -> object:
        fence = AttemptWriteFence(
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            expected_run_revision=run.revision,
            lease_id=lease.lease_id,
            fencing_token=attempt.fencing_token,
        )
        bridge = self._model_bridge_factory(run=run, attempt=attempt, lease=lease)
        context = ExecutorContext(
            run=run,
            attempt=attempt,
            payload=run.payload,
            deadline_utc=deadline_utc,
            model_bridge=bridge,
        )
        outcome = await self._execute(context, run=run, attempt=attempt)
        # The terminal publication is one authoritative DB transaction; run it off
        # the event loop too so the heartbeat coroutine stays responsive.
        return await self._pool.run(
            lambda: self._terminal.publish(fence=fence, outcome=outcome, actor=self._worker_actor)
        )

    async def _execute(
        self,
        context: ExecutorContext,
        *,
        run: RunRecord,
        attempt: RunAttempt,
    ) -> PreparedRunOutcome:
        try:
            executor = self._resolve_executor(run)
            return await self._pool.run(lambda: executor(context))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Executor faults (including a missing/unresolvable executor) are
            # classified into a conservative, non-leaking failure and published
            # through the terminal policy rather than crashing the worker loop.
            return redacted_execution_failure(run=run, attempt=attempt)


__all__ = ["AttemptRunner", "ExecutorResolver", "ModelBridgeFactory", "TerminalSink"]
