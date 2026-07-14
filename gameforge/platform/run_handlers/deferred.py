"""Typed M4e-deferred executors for the two internal-only Run kinds.

These functions are real, trusted registry targets.  They cannot fabricate a
migration or disaster-recovery success artifact while the production
executors remain deferred to M4e.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Annotated, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.jobs import (
    FailureClassifierRefV1,
    PreparedRunFailure,
)


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
PositiveInt = Annotated[int, Field(gt=0)]


class DeferredExecutionRequest(BaseModel):
    """Authoritative identity needed to construct a fenced prepared failure."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    run_id: NonEmptyStr
    attempt_no: PositiveInt
    run_kind: RunKindRef
    classifier: FailureClassifierRefV1


DeferredExecutor = Callable[[DeferredExecutionRequest], PreparedRunFailure]


def _deferred_failure(
    request: DeferredExecutionRequest,
    *,
    expected_kind: str,
    capability: str,
) -> PreparedRunFailure:
    if request.run_kind != RunKindRef(kind=expected_kind, version=1):
        raise ValueError(f"{capability} deferred executor received another Run kind")
    return PreparedRunFailure(
        run_id=request.run_id,
        attempt_no=request.attempt_no,
        run_kind=request.run_kind,
        artifacts=(),
        requirement_dispositions=(),
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        classifier=request.classifier,
        redacted_message=f"{capability} execution is unavailable in M4c",
    )


def artifact_migration_deferred(
    request: DeferredExecutionRequest,
) -> PreparedRunFailure:
    return _deferred_failure(
        request,
        expected_kind="artifact.migrate",
        capability="artifact migration",
    )


def dr_drill_deferred(request: DeferredExecutionRequest) -> PreparedRunFailure:
    return _deferred_failure(
        request,
        expected_kind="dr.drill",
        capability="disaster recovery drill",
    )


DEFERRED_EXECUTORS: Mapping[str, DeferredExecutor] = MappingProxyType(
    {
        "artifact_migrator@1": artifact_migration_deferred,
        "dr_drill_runner@1": dr_drill_deferred,
    }
)


__all__ = [
    "DEFERRED_EXECUTORS",
    "DeferredExecutionRequest",
    "DeferredExecutor",
    "artifact_migration_deferred",
    "dr_drill_deferred",
]
