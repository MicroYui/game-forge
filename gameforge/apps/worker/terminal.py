"""Fenced terminal hand-off from the worker runner to the platform authority.

The runner produces exactly one sealed ``PreparedRunOutcome``; this adapter hands
it to :meth:`RunLifecycleService.publish_attempt_outcome` under the attempt write
fence. Only that platform service — through the Task-9 ``TerminalPublisher`` —
creates authoritative artifacts/findings/workflow/manifests/events/audit and
closes cost. The worker never writes those directly.
"""

from __future__ import annotations

from gameforge.contracts.jobs import PreparedRunOutcome
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.lifecycle import (
    AttemptOutcomePublicationResult,
    AttemptWriteFence,
    PublishAttemptOutcomeRequest,
    RunLifecycleService,
)


class WorkerTerminalPublisher:
    """Adapt the runner's ``TerminalSink`` to the fenced lifecycle command."""

    def __init__(self, lifecycle: RunLifecycleService) -> None:
        self._lifecycle = lifecycle

    def publish(
        self,
        *,
        fence: AttemptWriteFence,
        outcome: PreparedRunOutcome,
        actor: AuditActor,
    ) -> AttemptOutcomePublicationResult:
        return self._lifecycle.publish_attempt_outcome(
            PublishAttemptOutcomeRequest(
                fence=fence,
                prepared_outcome=outcome,
                actor=actor,
            )
        )


__all__ = ["WorkerTerminalPublisher"]
