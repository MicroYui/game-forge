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
from collections.abc import Callable
from typing import Protocol

from gameforge.contracts.jobs import (
    AttemptProgressDataV1,
    DependencyFailureV1,
    FailureClassifierV1,
    PreparedRunFailure,
    PreparedRunOutcome,
    RunAttempt,
    RunPayloadEnvelope,
    RunRecord,
)
from gameforge.contracts.errors import (
    DependencyUnavailable,
    IntegrityViolation,
    PermanentDependencyFailure,
    QuotaExceeded,
)
from gameforge.runtime.model_router.router import CassetteReplayMiss
from gameforge.contracts.model_router import ModelSnapshot


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
    progress_publisher: Callable[[AttemptProgressDataV1], object] | None = None


class RunExecutor(Protocol):
    """The single generic executor signature every registered executor adapts to."""

    def __call__(self, context: ExecutorContext) -> PreparedRunOutcome: ...


def redacted_execution_failure(
    *,
    run: RunRecord,
    attempt: RunAttempt,
    classifier: FailureClassifierV1,
    error: BaseException,
) -> PreparedRunFailure:
    """Classify one executor fault through the exact frozen classifier.

    Only typed, complete dependency metadata may become a dependency failure.  All
    messages are fixed redactions: exception text/context may contain prompts,
    credentials, provider payloads, or other sensitive values and is never copied.
    """

    cause_code, dependency, message = _exception_projection(error)
    rule = next((item for item in classifier.rules if item.cause_code == cause_code), None)
    if rule is None or (
        rule.dependency_required != (dependency is not None)
        or (
            dependency is not None
            and dependency.dependency_kind not in rule.allowed_dependency_kinds
        )
    ):
        # An unknown/incomplete exception cannot self-assert classifier semantics.
        cause_code = "execution_failed"
        dependency = None
        message = "worker executor raised an unhandled error"
        rule = next(
            (item for item in classifier.rules if item.cause_code == cause_code),
            None,
        )
    if rule is None:
        raise IntegrityViolation("frozen failure classifier lacks execution_failed")

    return PreparedRunFailure(
        run_id=run.run_id,
        attempt_no=attempt.attempt_no,
        run_kind=run.kind,
        artifacts=(),
        requirement_dispositions=(),
        cause_code=rule.cause_code,
        failure_class=rule.failure_class,
        intrinsic_retry_eligible=rule.intrinsic_retry_eligible,
        classifier=run.failure_classifier,
        dependency=dependency,
        redacted_message=message,
    )


def _exception_projection(
    error: BaseException,
) -> tuple[str, DependencyFailureV1 | None, str]:
    if isinstance(error, (IntegrityViolation, CassetteReplayMiss)):
        return (
            "integrity_violation",
            None,
            "worker execution evidence failed an integrity check",
        )
    if isinstance(error, QuotaExceeded):
        return "quota_exceeded", None, "worker execution quota was exhausted"
    if isinstance(error, TimeoutError):
        return "timed_out", None, "worker execution exceeded its deadline"
    if isinstance(error, DependencyUnavailable):
        dependency = _typed_dependency(error)
        if dependency is not None:
            return (
                "dependency_unavailable",
                dependency,
                "a required worker dependency is temporarily unavailable",
            )
    if isinstance(error, PermanentDependencyFailure):
        dependency = _typed_dependency(error)
        if dependency is not None:
            return (
                "permanent_dependency_failed",
                dependency,
                "a required worker dependency permanently rejected the operation",
            )
    return "execution_failed", None, "worker executor raised an unhandled error"


def _typed_dependency(
    error: DependencyUnavailable | PermanentDependencyFailure,
) -> DependencyFailureV1 | None:
    context = error.context
    required = (
        "dependency_kind",
        "dependency_id",
        "operation_code",
        "classifier_code",
    )
    if any(not isinstance(context.get(key), str) or not context[key] for key in required):
        return None
    try:
        return DependencyFailureV1(
            dependency_kind=context["dependency_kind"],
            dependency_id=context["dependency_id"],
            operation_code=context["operation_code"],
            classifier_code=context["classifier_code"],
            upstream_status_code=context.get("upstream_status_code"),
            retry_after_ms=context.get("retry_after_ms"),
        )
    except (TypeError, ValueError):
        return None


__all__ = [
    "ExecutorContext",
    "RunExecutor",
    "WorkerModelBridgePort",
    "redacted_execution_failure",
]
