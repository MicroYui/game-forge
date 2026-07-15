"""Trusted workflow-effect registry for terminal publication.

``OutcomeArtifactPolicyV1.workflow_effect_key`` is resolved **only** through this
composition-root map into a registered handler — never a client callable or import
path (M4 design line 1116).  Unknown keys fail closed.

The handlers registered here are the effects that leave SubjectHead / ApprovalItem
state untouched: the ``no_workflow_change`` / ``no_workflow_subject`` family used by
Review/Checker/Simulation/Playtest/TaskSuite success and every non-success close,
plus the evidence-only reject and terminal-only failure effects.  Draft-creating /
SubjectHead-CAS effects (generation gate pass, repair verified, constraint
proposal, validation completion) and the ``validating→draft`` revert
(``restore_current_draft@1``) are intentionally NOT registered yet; a policy naming
one resolves to a fail-closed error until its approval-workflow body lands, rather
than being silently skipped (a no-op ``restore_current_draft@1`` would let a
validation-failure terminal silently skip the required revert).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import OutcomeArtifactPolicyV1, RunRecord
from gameforge.contracts.lineage import AuditActor


@dataclass(frozen=True, slots=True)
class WorkflowEffectContext:
    """Everything a workflow effect may read/write inside the terminal UoW."""

    run: RunRecord
    policy: OutcomeArtifactPolicyV1
    scope: str
    published_primary_artifact_id: str | None
    published_output_artifact_ids: tuple[str, ...]
    approvals: object
    actor: AuditActor
    occurred_at: str


WorkflowEffect = Callable[[WorkflowEffectContext], None]


def _no_workflow_mutation(context: WorkflowEffectContext) -> None:
    """A genuine no-op: this outcome never advances SubjectHead/ApprovalItem."""

    return None


# The composition-root registry.  Adding a draft-mutating key here is the only
# sanctioned way to enable it; nothing outside this module may inject a handler.
WORKFLOW_EFFECTS: Mapping[str, WorkflowEffect] = MappingProxyType(
    {
        "no_workflow_change@1": _no_workflow_mutation,
        "no_workflow_subject@1": _no_workflow_mutation,
        "terminal_only@1": _no_workflow_mutation,
        "close_attempt_for_terminal@1": _no_workflow_mutation,
        "close_attempt_for_retry@1": _no_workflow_mutation,
        "leave_patch_head_unchanged@1": _no_workflow_mutation,
    }
)


def resolve_workflow_effect(key: str) -> WorkflowEffect:
    """Resolve a workflow-effect key through the trusted registry, fail-closed."""

    effect = WORKFLOW_EFFECTS.get(key)
    if effect is None:
        raise IntegrityViolation("workflow effect key is not registered", workflow_effect_key=key)
    return effect


def apply_workflow_effect(key: str, context: WorkflowEffectContext) -> None:
    resolve_workflow_effect(key)(context)


__all__ = [
    "WORKFLOW_EFFECTS",
    "WorkflowEffect",
    "WorkflowEffectContext",
    "apply_workflow_effect",
    "resolve_workflow_effect",
]
