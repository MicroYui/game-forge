"""Typed M4e-deferred executors for the two internal-only Run kinds.

These functions are real, trusted registry targets.  They cannot fabricate a
migration or disaster-recovery success artifact while the production
executors remain deferred to M4e.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Callable, Mapping

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.jobs import PreparedRunFailure
from gameforge.platform.run_handlers.base import ExecutorContextLike

DeferredExecutor = Callable[[ExecutorContextLike], PreparedRunFailure]
DEFERRED_EXECUTOR_MARKER = "__gameforge_m4e_deferred__"


def _deferred_failure(
    context: ExecutorContextLike,
    *,
    expected_kind: str,
    capability: str,
) -> PreparedRunFailure:
    if context.run.kind != RunKindRef(kind=expected_kind, version=1):
        raise IntegrityViolation(f"{capability} deferred executor received another Run kind")
    return PreparedRunFailure(
        run_id=context.run.run_id,
        attempt_no=context.attempt.attempt_no,
        run_kind=context.run.kind,
        artifacts=(),
        requirement_dispositions=(),
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        classifier=context.run.failure_classifier,
        redacted_message=f"{capability} execution is deferred to M4e and unavailable in M4c",
    )


def artifact_migration_deferred(
    context: ExecutorContextLike,
) -> PreparedRunFailure:
    return _deferred_failure(
        context,
        expected_kind="artifact.migrate",
        capability="artifact migration",
    )


def dr_drill_deferred(context: ExecutorContextLike) -> PreparedRunFailure:
    return _deferred_failure(
        context,
        expected_kind="dr.drill",
        capability="disaster recovery drill",
    )


setattr(artifact_migration_deferred, DEFERRED_EXECUTOR_MARKER, True)
setattr(dr_drill_deferred, DEFERRED_EXECUTOR_MARKER, True)


DEFERRED_EXECUTORS: Mapping[str, DeferredExecutor] = MappingProxyType(
    {
        "artifact_migrator@1": artifact_migration_deferred,
        "dr_drill_runner@1": dr_drill_deferred,
    }
)


__all__ = [
    "DEFERRED_EXECUTORS",
    "DEFERRED_EXECUTOR_MARKER",
    "DeferredExecutor",
    "artifact_migration_deferred",
    "dr_drill_deferred",
]
