"""Thin transport adapter binding the workflow-command port to the platform service.

Business orchestration lives in ``gameforge.platform.workflow``; this adapter only
translates the transport ``WorkflowCommand``/``WorkflowCommandResult`` value objects
to and from the platform composition service.
"""

from __future__ import annotations

from gameforge.apps.api.dependencies import WorkflowCommand, WorkflowCommandResult
from gameforge.platform.workflow.service import (
    WorkflowCommandService,
    WorkflowServerContext,
)


class WorkflowCommandAdapter:
    """Implements ``WorkflowCommandPort`` over the platform composition service."""

    def __init__(self, service: WorkflowCommandService) -> None:
        self._service = service

    def execute(self, command: WorkflowCommand) -> WorkflowCommandResult:
        metadata = command.metadata
        server = WorkflowServerContext(
            actor=metadata.actor,
            request_id=metadata.request_id,
            trace_id=metadata.trace_id,
            idempotency_key=metadata.idempotency_key,
            request_hash=metadata.request_hash,
            if_match=metadata.if_match,
            resource_id=command.resource_id,
        )
        outcome = self._service.execute(
            operation=command.operation,
            payload=command.payload,
            server=server,
        )
        return WorkflowCommandResult(
            value=outcome.value,
            resource_kind=outcome.resource_kind,
            resource_id=outcome.resource_id,
            revision=outcome.revision,
        )


__all__ = ["WorkflowCommandAdapter"]
