"""One authoritative Run read-domain projection shared by API transports."""

from __future__ import annotations

from typing import Protocol

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.identity import DomainRegistryV1, DomainScope, DomainScopeValue
from gameforge.contracts.jobs import (
    ConstraintProposalProposePayloadV1,
    ConstraintValidationPayloadV1,
    DrDrillPayloadV1,
    GenerationProposePayloadV1,
    PatchValidationPayloadV1,
    RollbackValidationPayloadV1,
    RunRecord,
)
from gameforge.contracts.workflow import ApprovalItem


class ApprovalItemReader(Protocol):
    """Load one ApprovalItem for legacy validation-Run compatibility."""

    def get(self, approval_id: str) -> ApprovalItem | None: ...


def _all_known_domain_scope(registry: DomainRegistryV1) -> DomainScope:
    known = tuple(definition.domain_id for definition in registry.definitions)
    if not known:
        raise IntegrityViolation("domain registry has no retained domain for Run read")
    return DomainScope(domain_ids=known)


def resolve_run_read_domain(
    run: RunRecord,
    registry: DomainRegistryV1,
    approvals: ApprovalItemReader | None,
) -> DomainScopeValue:
    """Return admission-frozen Run scope, retaining the exact legacy fallback.

    Current Runs persist the scope admission authorized. Only historical rows whose
    compatibility field is absent are projected from their frozen typed payload and,
    for validation Runs, the retained approval subject.
    """

    frozen_scope = getattr(run, "resource_domain_scope", None)
    if frozen_scope is not None:
        return frozen_scope
    params = run.payload.params
    if isinstance(params, DrDrillPayloadV1):
        return None
    if isinstance(
        params,
        (PatchValidationPayloadV1, ConstraintValidationPayloadV1, RollbackValidationPayloadV1),
    ):
        item = approvals.get(params.subject.approval_id) if approvals is not None else None
        expected_subject_kind = {
            PatchValidationPayloadV1: "patch",
            ConstraintValidationPayloadV1: "constraint_proposal",
            RollbackValidationPayloadV1: "rollback_request",
        }[type(params)]
        subject = params.subject
        if (
            isinstance(item, ApprovalItem)
            and item.subject_kind == expected_subject_kind
            and item.subject_revision == subject.subject_head_revision
            and item.subject_artifact_id == subject.subject_artifact_id
            and item.subject_digest == subject.subject_digest
            and run.run_id == subject.active_validation_run_id
        ):
            return item.domain_scope
        return _all_known_domain_scope(registry)
    if isinstance(params, (GenerationProposePayloadV1, ConstraintProposalProposePayloadV1)):
        return params.domain_scope
    return _all_known_domain_scope(registry)


__all__ = ["ApprovalItemReader", "resolve_run_read_domain"]
