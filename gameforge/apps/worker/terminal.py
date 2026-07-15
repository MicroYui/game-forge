"""Fenced terminal hand-off from the worker runner to the platform authority.

The runner produces exactly one sealed ``PreparedRunOutcome``; this adapter hands
it to :meth:`RunLifecycleService.publish_attempt_outcome` under the attempt write
fence. Only that platform service — through the Task-9 ``TerminalPublisher`` —
creates authoritative artifacts/findings/workflow/manifests/events/audit and
closes cost. The worker never writes those directly.
"""

from __future__ import annotations

from collections.abc import Callable

from gameforge.contracts.jobs import PreparedRunOutcome
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.lifecycle import (
    AttemptOutcomePublicationResult,
    AttemptWriteFence,
    PublishAttemptOutcomeRequest,
    RunLifecycleService,
)


class WorkerTerminalPublisher:
    """Adapt the runner's ``TerminalSink`` to the fenced lifecycle command.

    After the terminal transaction COMMITS (``publish_attempt_outcome`` returns), an
    optional :class:`RunEventNotifier`-backed ``notify(run_id)`` hint is fired so a
    same-process SSE subscriber wakes with sub-heartbeat latency (Task-15a deferral).
    The hint is non-authoritative: SSE always rereads the DB, so a dropped hint (or a
    two-process deployment with no shared notifier) loses nothing.
    """

    def __init__(
        self,
        lifecycle: RunLifecycleService,
        *,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._notify = notify

    def publish(
        self,
        *,
        fence: AttemptWriteFence,
        outcome: PreparedRunOutcome,
        actor: AuditActor,
    ) -> AttemptOutcomePublicationResult:
        result = self._lifecycle.publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=fence,
                prepared_outcome=outcome,
                actor=actor,
            )
        )
        if self._notify is not None:
            self._notify(result.run.run_id)
        return result


__all__ = ["WorkerTerminalPublisher"]
