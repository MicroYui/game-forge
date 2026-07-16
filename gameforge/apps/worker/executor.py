"""The generic executor seam driven by the persistent worker's runner.

Task 10 owns the *dispatch* contract, not the executors themselves: Tasks 11-13
register the eleven real executors (``checker_runner@1``, ``simulation_runner@1``,
``playtest_runner@1``, ``generation_proposer@1``, ...) into
``TrustedComponentMaps.executors``. Every executor — real or the two already
present M4e-deferred ones — is adapted to the single :class:`RunExecutor`
signature below and dispatched *generically* by ``executor_key``; the runner
never hard-codes a Run kind.

An executor receives a fully-resolved :class:`ExecutorContext` (the fenced Run /
attempt, its frozen payload + resolved profiles, the M4b model bridge for any
LLM work, and the authoritative attempt deadline) and returns exactly one sealed
``PreparedRunOutcome``. It must NOT touch persistence, cost, or publication
directly — that authority stays with the platform services the runner drives.
Any exception it raises is caught by the runner and converted to a classified,
redacted ``PreparedRunFailure`` (see :func:`redacted_execution_failure`) that
flows through the terminal outcome policy rather than escaping the worker loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from gameforge.contracts.jobs import (
    PreparedRunFailure,
    PreparedRunOutcome,
    RunAttempt,
    RunPayloadEnvelope,
    RunRecord,
)
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.platform.run_handlers.deferred import (
    DeferredExecutionRequest,
    DeferredExecutor,
)


class WorkerModelBridgePort(Protocol):
    """The per-call M4b execution bridge the executor uses for LLM work.

    Kept structural so ``executor.py`` does not import the concrete bridge (which
    itself imports the M4b router/cassette/cost seams), avoiding an import cycle.
    """

    def call_model(self, request: object) -> object: ...

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot: ...


@dataclass(frozen=True, slots=True)
class ExecutorContext:
    """Everything a Run-kind executor needs for one fenced attempt."""

    run: RunRecord
    attempt: RunAttempt
    payload: RunPayloadEnvelope
    deadline_utc: datetime
    model_bridge: WorkerModelBridgePort


class RunExecutor(Protocol):
    """The single generic executor signature every registered executor adapts to."""

    def __call__(self, context: ExecutorContext) -> PreparedRunOutcome: ...


def redacted_execution_failure(
    *,
    run: RunRecord,
    attempt: RunAttempt,
    cause_code: str = "execution_failed",
    redacted_message: str = "worker executor raised an unhandled error",
) -> PreparedRunFailure:
    """Build the conservative, non-leaking failure for an executor that raised.

    Uses the Run's frozen failure classifier and the ``execution`` class with no
    intrinsic retry eligibility (the terminal outcome policy + retry policy decide
    retry vs. terminal). No exception text or payload is copied into the message.
    """

    return PreparedRunFailure(
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        run_kind=run.kind,
        artifacts=(),
        requirement_dispositions=(),
        cause_code=cause_code,
        failure_class="execution",
        intrinsic_retry_eligible=False,
        classifier=run.failure_classifier,
        redacted_message=redacted_message,
    )


def deferred_executor_adapter(deferred: DeferredExecutor) -> RunExecutor:
    """Adapt an M4e-deferred executor to the generic :class:`RunExecutor` shape.

    The composition root wraps each still-deferred executor so the runner can
    dispatch it by key exactly like a real one; it constructs the narrow
    ``DeferredExecutionRequest`` from the fenced context and returns the typed
    ``PreparedRunFailure`` the deferred executor produces.
    """

    def _run(context: ExecutorContext) -> PreparedRunOutcome:
        request = DeferredExecutionRequest(
            run_id=context.run.run_id,
            attempt_no=context.attempt.attempt_no,
            run_kind=context.run.kind,
            classifier=context.run.failure_classifier,
        )
        return deferred(request)

    return _run


__all__ = [
    "ExecutorContext",
    "RunExecutor",
    "WorkerModelBridgePort",
    "deferred_executor_adapter",
    "redacted_execution_failure",
]
