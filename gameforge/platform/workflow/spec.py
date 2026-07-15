"""Narrow spec-upload composition: a direct authorized ref publication.

Unlike Patch/constraint/rollback subjects, a human spec upload has no ApprovalItem
and no maker-checker gate (M4 design §5.3). It publishes an ``ir_snapshot`` Artifact
blob-first and advances the target ref by exact CAS, all inside one UnitOfWork with
server-owned idempotency. A rolled-back publication leaves only a verified,
GC-eligible orphan blob (there is no dangling authoritative ref).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from gameforge.contracts.api import ArtifactSummaryV1, SpecViewV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditCorrelation,
    AuditSubject,
)
from gameforge.contracts.storage import RefValue, UtcClock
from gameforge.platform.approvals.commands import (
    ApprovalCommandContext,
    ApprovalUnitOfWork,
    PreparedObjectBinding,
)


@dataclass(frozen=True, slots=True)
class SpecPublicationPlan:
    """Blob-first assembled inputs to one spec publication transaction."""

    artifact: ArtifactV2
    binding: PreparedObjectBinding
    ref_name: str
    expected_ref: RefValue | None
    snapshot_id: str
    schema_registry_version: str
    domain_scope: DomainScope


@dataclass(slots=True)
class SpecUploadCapabilities:
    refs: Any
    artifacts: Any
    object_bindings: Any
    audit: Any
    idempotency: Any


SpecCapabilityBinder = Callable[[Any], SpecUploadCapabilities]


def _required(value: Any, name: str) -> Any:
    if value is None:
        raise IntegrityViolation(f"{name} spec upload capability is unavailable")
    return value


class SpecUploadService:
    def __init__(
        self,
        *,
        unit_of_work: ApprovalUnitOfWork,
        bind_capabilities: SpecCapabilityBinder,
        clock: UtcClock,
        audit_chain_id: str,
    ) -> None:
        if not audit_chain_id:
            raise ValueError("audit_chain_id must be non-empty")
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._clock = clock
        self._audit_chain_id = audit_chain_id

    def upload(
        self,
        *,
        plan: SpecPublicationPlan,
        context: ApprovalCommandContext,
    ) -> SpecViewV1:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            refs = _required(capabilities.refs, "refs")
            artifacts = _required(capabilities.artifacts, "artifacts")
            bindings = _required(capabilities.object_bindings, "object_bindings")
            audit = _required(capabilities.audit, "audit")
            idempotency = _required(capabilities.idempotency, "idempotency")

            replay = idempotency.get_result(
                scope=context.idempotency_scope,
                operation="workflow.spec_upload",
                key=context.idempotency_key,
                request_hash=context.request_hash,
            )
            if replay is not None:
                return self._replay(replay, plan)

            published_binding = bindings.bind_verified(
                plan.binding.object_ref,
                plan.binding.location,
                plan.binding.expected_revision,
            )
            if (
                published_binding.object_ref != plan.binding.object_ref
                or published_binding.status != "active"
            ):
                raise IntegrityViolation("ObjectBinding publisher returned another binding")
            if artifacts.put(plan.artifact) != plan.artifact:
                raise IntegrityViolation("Artifact publisher returned another Artifact")
            ref_value = refs.compare_and_set(
                plan.ref_name,
                plan.expected_ref,
                plan.artifact.artifact_id,
            )
            audit.append(
                chain_id=self._audit_chain_id,
                actor=context.actor,
                initiated_by=context.initiated_by,
                action="spec.uploaded",
                subject=AuditSubject(
                    resource_kind="spec_ref",
                    resource_id=plan.ref_name,
                    artifact_id=plan.artifact.artifact_id,
                ),
                correlation=AuditCorrelation(
                    request_id=context.request_id,
                    run_id=context.run_id,
                    trace_id=context.trace_id,
                ),
            )
            view = self._view(plan, ref_value)
            stored = idempotency.put_result(
                scope=context.idempotency_scope,
                operation="workflow.spec_upload",
                key=context.idempotency_key,
                request_hash=context.request_hash,
                resource_kind="spec_ref",
                resource_id=plan.ref_name,
                response=view.model_dump(mode="json"),
            )
            if dict(stored) != view.model_dump(mode="json"):
                raise IntegrityViolation("idempotency repository stored another spec response")
            return view

    def _view(self, plan: SpecPublicationPlan, ref_value: RefValue) -> SpecViewV1:
        return SpecViewV1(
            artifact=ArtifactSummaryV1(
                artifact_id=plan.artifact.artifact_id,
                lineage_schema_version="lineage@2",
                kind=plan.artifact.kind,
                version_tuple=plan.artifact.version_tuple,
                parent_artifact_ids=tuple(sorted(set(plan.artifact.lineage))),
                payload_hash=plan.artifact.payload_hash,
                payload_schema_id="ir-snapshot@1",
                domain_scope=plan.domain_scope,
                created_at=plan.artifact.created_at,
            ),
            snapshot_id=plan.snapshot_id,
            schema_registry_version=plan.schema_registry_version,
            ref_name=plan.ref_name,
            ref_value=ref_value,
        )

    @staticmethod
    def _replay(response: Any, plan: SpecPublicationPlan) -> SpecViewV1:
        try:
            view = SpecViewV1.model_validate(response)
        except ValidationError as exc:
            raise IntegrityViolation("spec upload idempotency response is malformed") from exc
        if (
            view.artifact.artifact_id != plan.artifact.artifact_id
            or view.snapshot_id != plan.snapshot_id
            or view.ref_name != plan.ref_name
        ):
            raise IntegrityViolation("spec upload idempotency response differs from the command")
        return view


__all__ = [
    "SpecCapabilityBinder",
    "SpecPublicationPlan",
    "SpecUploadCapabilities",
    "SpecUploadService",
]
