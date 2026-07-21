"""Transactional M4 approval commands over injected, transaction-bound capabilities."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4
from weakref import WeakKeyDictionary, WeakSet

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from gameforge.contracts.errors import (
    Conflict,
    Forbidden,
    IntegrityViolation,
    InvalidStateTransition,
)
from gameforge.contracts.api import compute_resource_etag
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.findings import PatchV2, PatchView
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRoutePolicyRefV1,
    Permission,
    Principal,
    RolePolicy,
    SubjectKind,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    AuditSubject,
    ObjectBinding,
    ObjectLocation,
    ObjectRef,
)
from gameforge.contracts.storage import RefValue, UtcClock
from gameforge.contracts.workflow import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyV1,
    PatchTargetBindingV1,
    RollbackRequestV1,
    RollbackTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.approvals.decisions import (
    apply_approval_decision,
    validate_approval_policy_bindings,
)
from gameforge.platform.approvals.state import (
    next_workflow_revision,
    validate_status_transition,
)
from gameforge.platform.audit.gate import AuditAppendIntent, PreflightedAuditBatch
from gameforge.platform.rbac.authorization import AuthorizationDecision, authorize


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
LowerHexSha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ApprovalCommandContext(_FrozenModel):
    actor: AuditActor
    initiated_by: AuditActor | None = None
    request_id: NonEmptyStr
    run_id: NonEmptyStr | None = None
    trace_id: NonEmptyStr | None = None
    idempotency_scope: NonEmptyStr
    idempotency_key: NonEmptyStr
    request_hash: LowerHexSha256
    if_match: NonEmptyStr | None = None

    @property
    def accountable_actor(self) -> AuditActor:
        return self.initiated_by or self.actor


class ApprovalDecisionRequest(_FrozenModel):
    """Client-controlled fields for one server-owned approval decision."""

    requirement_ids: tuple[NonEmptyStr, ...]
    decision: Literal["approve", "reject", "request_changes"]
    expected_workflow_revision: PositiveInt
    reason_code: NonEmptyStr
    comment: NonEmptyStr | None = None

    @field_validator("requirement_ids")
    @classmethod
    def _canonical_requirement_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical:
            raise ValueError("decision requirement_ids must be non-empty")
        return canonical


class PreparedObjectBinding(_FrozenModel):
    object_ref: ObjectRef
    location: ObjectLocation
    expected_revision: PositiveInt | None

    @model_validator(mode="after")
    def _same_key(self) -> PreparedObjectBinding:
        if self.object_ref.key != self.location.key:
            raise ValueError("prepared ObjectRef and ObjectLocation keys differ")
        return self


class DraftSubjectFacts(_FrozenModel):
    """Trusted facts returned after parsing the exact subject object bytes."""

    subject_kind: SubjectKind
    subject_revision: PositiveInt | None
    produced_by: Literal["agent", "human"]
    producer_run_id: NonEmptyStr | None
    supersedes_artifact_id: NonEmptyStr | None
    target_artifact_id: NonEmptyStr | None
    target_snapshot_id: NonEmptyStr | None
    rollback_request: RollbackRequestV1 | None = None

    @model_validator(mode="after")
    def _producer_binding(self) -> DraftSubjectFacts:
        if self.produced_by == "agent" and self.producer_run_id is None:
            raise ValueError("agent subject facts require producer_run_id")
        if self.produced_by == "human" and self.producer_run_id is not None:
            raise ValueError("human subject facts cannot carry producer_run_id")
        if self.subject_kind in {"patch", "constraint_proposal"}:
            if self.subject_revision is None:
                raise ValueError("patch and constraint facts require subject_revision")
            if self.rollback_request is not None:
                raise ValueError("only rollback facts may carry rollback_request")
        elif self.rollback_request is None:
            raise ValueError("rollback facts require parsed rollback_request")
        return self


class EvidenceStateProjection(_FrozenModel):
    validation_status: Literal[
        "not_started",
        "running",
        "passed",
        "failed",
        "unproven",
        "execution_failed",
    ]
    regression_status: Literal[
        "not_started",
        "passed",
        "failed",
        "unproven",
        "not_applicable",
    ]


class PreparedDraft(_FrozenModel):
    subject_artifact: ArtifactV2
    companion_artifacts: tuple[ArtifactV2, ...]
    object_bindings: tuple[PreparedObjectBinding, ...]
    approval_item: ApprovalItem
    expected_subject_head: SubjectHead | None
    expected_previous_workflow_revision: PositiveInt | None = None

    @field_validator("companion_artifacts")
    @classmethod
    def _unique_companions(cls, value: tuple[ArtifactV2, ...]) -> tuple[ArtifactV2, ...]:
        ids = [artifact.artifact_id for artifact in value]
        if len(ids) != len(set(ids)):
            raise ValueError("prepared companion artifact IDs must be unique")
        return tuple(sorted(value, key=lambda artifact: artifact.artifact_id))

    @field_validator("object_bindings")
    @classmethod
    def _unique_bindings(
        cls, value: tuple[PreparedObjectBinding, ...]
    ) -> tuple[PreparedObjectBinding, ...]:
        identities = [(binding.object_ref.key, binding.location.store_id) for binding in value]
        if len(identities) != len(set(identities)):
            raise ValueError("prepared object binding identities must be unique")
        return tuple(
            sorted(
                value,
                key=lambda binding: (
                    binding.object_ref.key,
                    binding.location.store_id,
                ),
            )
        )

    @model_validator(mode="after")
    def _publication_shape(self) -> PreparedDraft:
        if any(
            artifact.artifact_id == self.subject_artifact.artifact_id
            for artifact in self.companion_artifacts
        ):
            raise ValueError("subject artifact cannot also be a companion")
        artifacts = (self.subject_artifact, *self.companion_artifacts)
        artifact_refs = {artifact.object_ref for artifact in artifacts}
        binding_refs = {binding.object_ref for binding in self.object_bindings}
        if artifact_refs != binding_refs:
            raise ValueError("prepared bindings must cover exactly every prepared Artifact")

        item = self.approval_item
        expected_kind = {
            "patch": "patch",
            "constraint_proposal": "constraint_proposal",
            "rollback_request": "rollback_request",
        }[item.subject_kind]
        if (
            self.subject_artifact.kind != expected_kind
            or item.subject_artifact_id != self.subject_artifact.artifact_id
            or item.subject_digest != self.subject_artifact.payload_hash
        ):
            raise ValueError("prepared subject Artifact and ApprovalItem differ")
        allowed_companions = {
            "patch": {"ir_snapshot", "config_export"},
            "constraint_proposal": set(),
            "rollback_request": set(),
        }[item.subject_kind]
        if any(artifact.kind not in allowed_companions for artifact in self.companion_artifacts):
            raise ValueError("prepared draft contains an unsupported companion Artifact")
        if (
            item.subject_kind == "patch"
            and sum(artifact.kind == "ir_snapshot" for artifact in self.companion_artifacts) != 1
        ):
            raise ValueError("prepared patch draft requires exactly one preview Artifact")
        return self

    @property
    def artifacts(self) -> tuple[ArtifactV2, ...]:
        return (self.subject_artifact, *self.companion_artifacts)


class PreparedTerminalDraft(_FrozenModel):
    """Fully validated Agent draft prepared before the terminal write UoW.

    Unlike :class:`PreparedDraft`, this type deliberately has no
    ``PreparedObjectBinding`` collection.  The generic terminal publisher owns
    blob staging and Artifact/ObjectBinding publication.  The read/planning phase
    validates the exact subject bytes, companion closure, retained lineage, and
    governance and seals those facts here; the write phase may therefore perform
    only mutable authority checks and Approval/SubjectHead/idempotency/audit
    writes.

    ``preparation_digest`` is mandatory and covers every canonical field.  There
    is no ``skip_validation`` switch: callers can obtain a valid instance only by
    supplying the complete typed projection and its exact digest (normally via
    :meth:`seal`).
    """

    prepared_schema_version: Literal["prepared-terminal-draft@1"] = "prepared-terminal-draft@1"
    subject_artifact: ArtifactV2
    companion_artifacts: tuple[ArtifactV2, ...]
    subject_facts: DraftSubjectFacts
    retained_parent_ids: tuple[NonEmptyStr, ...]
    approval_item: ApprovalItem
    expected_subject_head: SubjectHead | None
    expected_previous_workflow_revision: PositiveInt | None = None
    preparation_digest: LowerHexSha256

    @field_validator("companion_artifacts")
    @classmethod
    def _canonical_companions(cls, value: tuple[ArtifactV2, ...]) -> tuple[ArtifactV2, ...]:
        ids = tuple(artifact.artifact_id for artifact in value)
        if len(ids) != len(set(ids)):
            raise ValueError("prepared terminal companion Artifact IDs must be unique")
        return tuple(sorted(value, key=lambda artifact: artifact.artifact_id))

    @field_validator("retained_parent_ids")
    @classmethod
    def _canonical_retained_parents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if canonical != value:
            raise ValueError("prepared terminal retained parents must be canonical unique")
        return canonical

    @model_validator(mode="after")
    def _complete_projection(self) -> PreparedTerminalDraft:
        item = self.approval_item
        expected_kind = {
            "patch": "patch",
            "constraint_proposal": "constraint_proposal",
            "rollback_request": "rollback_request",
        }[item.subject_kind]
        if (
            self.subject_artifact.kind != expected_kind
            or item.subject_artifact_id != self.subject_artifact.artifact_id
            or item.subject_digest != self.subject_artifact.payload_hash
            or self.subject_facts.subject_kind != item.subject_kind
            or (
                self.subject_facts.subject_revision is not None
                and self.subject_facts.subject_revision != item.subject_revision
            )
        ):
            raise ValueError("prepared terminal subject projection is inconsistent")
        if any(
            artifact.artifact_id == self.subject_artifact.artifact_id
            for artifact in self.companion_artifacts
        ):
            raise ValueError("terminal subject Artifact cannot also be a companion")
        allowed_companions = {
            "patch": {"ir_snapshot", "config_export"},
            "constraint_proposal": set(),
            "rollback_request": set(),
        }[item.subject_kind]
        if any(artifact.kind not in allowed_companions for artifact in self.companion_artifacts):
            raise ValueError("prepared terminal draft has an unsupported companion")
        if (
            item.subject_kind == "patch"
            and sum(artifact.kind == "ir_snapshot" for artifact in self.companion_artifacts) != 1
        ):
            raise ValueError("prepared terminal Patch requires exactly one preview")
        prepared_ids = {artifact.artifact_id for artifact in self.artifacts}
        projected_retained = tuple(
            sorted(
                {
                    parent_id
                    for artifact in self.artifacts
                    for parent_id in artifact.lineage
                    if parent_id not in prepared_ids
                }
            )
        )
        if projected_retained != self.retained_parent_ids:
            raise ValueError("prepared terminal retained lineage projection is incomplete")
        expected_digest = canonical_sha256(
            self.model_dump(mode="json", exclude={"preparation_digest"})
        )
        if self.preparation_digest != expected_digest:
            raise ValueError("prepared terminal draft digest is not canonical")
        return self

    @classmethod
    def seal(
        cls,
        *,
        subject_artifact: ArtifactV2,
        companion_artifacts: tuple[ArtifactV2, ...],
        subject_facts: DraftSubjectFacts,
        retained_parent_ids: tuple[str, ...],
        approval_item: ApprovalItem,
        expected_subject_head: SubjectHead | None,
        expected_previous_workflow_revision: int | None,
    ) -> PreparedTerminalDraft:
        payload: dict[str, object] = {
            "prepared_schema_version": "prepared-terminal-draft@1",
            "subject_artifact": subject_artifact.model_dump(mode="json"),
            "companion_artifacts": [
                artifact.model_dump(mode="json")
                for artifact in sorted(
                    companion_artifacts, key=lambda artifact: artifact.artifact_id
                )
            ],
            "subject_facts": subject_facts.model_dump(mode="json"),
            "retained_parent_ids": list(tuple(sorted(set(retained_parent_ids)))),
            "approval_item": approval_item.model_dump(mode="json"),
            "expected_subject_head": (
                None
                if expected_subject_head is None
                else expected_subject_head.model_dump(mode="json")
            ),
            "expected_previous_workflow_revision": expected_previous_workflow_revision,
        }
        return cls.model_validate(
            {
                **payload,
                "preparation_digest": canonical_sha256(payload),
            }
        )

    @property
    def artifacts(self) -> tuple[ArtifactV2, ...]:
        return (self.subject_artifact, *self.companion_artifacts)


class PreparedValidationStart(_FrozenModel):
    run_id: NonEmptyStr
    approval_id: NonEmptyStr
    subject_artifact_id: NonEmptyStr
    subject_digest: LowerHexSha256
    expected_workflow_revision: PositiveInt


class DraftPublicationResult(_FrozenModel):
    result_schema_version: Literal["draft-publication-result@1"] = "draft-publication-result@1"
    approval_item: ApprovalItem
    subject_head: SubjectHead


class ValidationStartResult(_FrozenModel):
    result_schema_version: Literal["validation-start-result@1"] = "validation-start-result@1"
    approval_item: ApprovalItem
    run_id: NonEmptyStr


class ApprovalRepository(Protocol):
    def insert_draft(self, item: ApprovalItem) -> ApprovalItem: ...

    def get(self, approval_id: str) -> ApprovalItem | None: ...

    def compare_and_set(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        replacement: ApprovalItem,
    ) -> ApprovalItem: ...

    def append_decision_and_compare_and_set(
        self,
        approval_id: str,
        expected_workflow_revision: int,
        decision: ApprovalDecision,
        replacement: ApprovalItem,
    ) -> ApprovalItem: ...

    def get_subject_head(self, subject_series_id: str) -> SubjectHead | None: ...

    def compare_and_set_subject_head(
        self,
        subject_series_id: str,
        expected: SubjectHead | None,
        replacement: SubjectHead,
    ) -> SubjectHead: ...

    def current(self, subject_series_id: str) -> tuple[SubjectHead, ApprovalItem] | None: ...

    def apply_preflighted_insert_draft(self, item: ApprovalItem) -> ApprovalItem: ...

    def apply_preflighted_compare_and_set(
        self,
        current: ApprovalItem,
        replacement: ApprovalItem,
    ) -> ApprovalItem: ...

    def apply_preflighted_validation_completion(
        self,
        current: ApprovalItem,
        replacement: ApprovalItem,
    ) -> ApprovalItem: ...

    def apply_preflighted_subject_head(
        self,
        *,
        expected: SubjectHead | None,
        expected_item: ApprovalItem | None,
        replacement: SubjectHead,
        replacement_item: ApprovalItem,
    ) -> SubjectHead: ...


class GovernancePolicyRepository(Protocol):
    def get_domain_registry(self, ref: DomainRegistryRefV1) -> DomainRegistryV1 | None: ...

    def get_domain_route_policy(self, ref: DomainRoutePolicyRefV1) -> DomainRoutePolicy | None: ...

    def get_role_policy(self, policy_version: str, policy_digest: str) -> RolePolicy | None: ...

    def get_approval_policy(self, ref: ApprovalPolicyRefV1) -> ApprovalPolicyV1 | None: ...


class ApprovalDecisionPrincipalRepository(Protocol):
    """Project the current principal and active roles in the command transaction."""

    def get(self, principal_id: str) -> Principal | None: ...


class ArtifactRepository(Protocol):
    def get(self, artifact_id: str) -> ArtifactV2 | None: ...

    def put(self, artifact: ArtifactV2) -> ArtifactV2: ...


class BindingRepository(Protocol):
    def bind_verified(
        self,
        ref: ObjectRef,
        location: ObjectLocation,
        expected_revision: int | None,
    ) -> ObjectBinding: ...


class IdempotencyRepository(Protocol):
    def get_result(
        self, *, scope: str, operation: str, key: str, request_hash: str
    ) -> dict[str, Any] | None: ...

    def put_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
        resource_kind: str,
        resource_id: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def put_preflighted_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
        resource_kind: str,
        resource_id: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any]: ...


class RefReader(Protocol):
    def get(self, name: str) -> RefValue | None: ...

    def get_history_entry(self, name: str, revision: int) -> RefValue | None: ...


class ApprovalAuditWriter(Protocol):
    def append(
        self,
        *,
        chain_id: str,
        actor: AuditActor,
        initiated_by: AuditActor | None,
        action: str,
        subject: AuditSubject,
        correlation: AuditCorrelation,
    ) -> object: ...

    def prepare_batch(
        self,
        *,
        chain_id: str,
        intents: tuple[AuditAppendIntent, ...],
    ) -> PreflightedAuditBatch: ...

    def apply_prepared_batch(self, prepared: PreflightedAuditBatch) -> None: ...


class ApprovalRunGateway(Protocol):
    def verify_producer_membership(
        self,
        *,
        run_id: str,
        artifact_id: str,
        initiated_by: AuditActor,
    ) -> None: ...

    def verify_prepared_terminal_producer_authority(
        self,
        *,
        run_id: str,
        initiated_by: AuditActor,
    ) -> None:
        """Fresh DB-only check for a planning-validated terminal subject."""

        ...

    def start_validation(
        self,
        *,
        prepared: PreparedValidationStart,
        item: ApprovalItem,
        initiated_by: AuditActor,
    ) -> str: ...

    def request_validation_cancel(
        self,
        *,
        run_id: str,
        reason: str,
        requested_by: AuditActor,
    ) -> None: ...


class SubjectPayloadGateway(Protocol):
    def inspect_draft_subject(self, artifact: ArtifactV2) -> DraftSubjectFacts: ...

    def load_patch(self, artifact: ArtifactV2) -> PatchV2: ...


class DraftLineageVerifier(Protocol):
    def validate_draft_publication(
        self,
        *,
        prepared: PreparedDraft,
        retained_parent_ids: tuple[str, ...],
    ) -> None: ...


class ApprovalEvidenceGateway(Protocol):
    def validate_submission(
        self,
        *,
        item: ApprovalItem,
        subject_artifact: ArtifactV2,
        target_artifact: ArtifactV2,
        evidence_artifact: ArtifactV2,
        regression_artifacts: tuple[ArtifactV2, ...],
    ) -> EvidenceStateProjection: ...

    def project_state(self, *, item: ApprovalItem) -> EvidenceStateProjection: ...


class ApprovalAutoApplyGateway(Protocol):
    """Resolve exact retained inputs and rerun the pure auto-apply guard."""

    def validate_eligibility(self, *, item: ApprovalItem) -> None: ...


@dataclass(slots=True)
class ApprovalCommandCapabilities:
    approvals: ApprovalRepository | None
    policies: GovernancePolicyRepository | None
    artifacts: ArtifactRepository | None
    object_bindings: BindingRepository | None
    idempotency: IdempotencyRepository | None
    audit: ApprovalAuditWriter | None
    runs: ApprovalRunGateway | None
    subjects: SubjectPayloadGateway | None
    lineage: DraftLineageVerifier | None
    evidence: ApprovalEvidenceGateway | None
    auto_apply: ApprovalAutoApplyGateway | None = None
    refs: RefReader | None = None
    principals: ApprovalDecisionPrincipalRepository | None = None


class ApprovalUnitOfWork(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...


CapabilityBinder = Callable[[Any], ApprovalCommandCapabilities]


_TERMINAL_DRAFT_PREFLIGHT_SEAL = object()


@dataclass(frozen=True, slots=True)
class TerminalDraftAuditIntents:
    """Approval audit intents delegated to one enclosing terminal batch."""

    chain_id: str
    intents: tuple[AuditAppendIntent, ...]


@dataclass(frozen=True, slots=True)
class _TerminalDraftPreflightState:
    prepared: PreparedTerminalDraft
    context: ApprovalCommandContext
    expected_head: SubjectHead
    old_item: ApprovalItem | None
    old_replacement: ApprovalItem | None
    idempotent_result: DraftPublicationResult | None
    audit_batch: PreflightedAuditBatch | None
    merged_audit_intents: "TerminalDraftAuditIntents | None"
    capabilities_owner: ApprovalCommandCapabilities
    capabilities: ApprovalCommandCapabilities
    transaction_identity: tuple[object, object] | None


def _approval_transaction_identity(
    capabilities: ApprovalCommandCapabilities,
) -> tuple[object, object] | None:
    """Resolve one optional SQL transaction shared by raw repository capabilities."""

    retained: tuple[object, object] | None = None
    for item in fields(ApprovalCommandCapabilities):
        capability = getattr(capabilities, item.name)
        if capability is None:
            continue
        try:
            session = object.__getattribute__(capability, "_session")
        except AttributeError:
            continue
        get_nested = getattr(session, "get_nested_transaction", None)
        get_transaction = getattr(session, "get_transaction", None)
        transaction = (get_nested() if callable(get_nested) else None) or (
            get_transaction() if callable(get_transaction) else None
        )
        if transaction is None or not getattr(transaction, "is_active", False):
            raise IntegrityViolation("terminal draft preflight requires an active transaction")
        identity = (session, transaction)
        if retained is None:
            retained = identity
        elif retained[0] is not session or retained[1] is not transaction:
            raise IntegrityViolation("terminal draft capabilities belong to different transactions")
    return retained


class PreflightedTerminalDraft:
    """Opaque one-shot authority token bound to one active DB transaction."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        *,
        prepared: PreparedTerminalDraft,
        context: ApprovalCommandContext,
        expected_head: SubjectHead,
        old_item: ApprovalItem | None,
        old_replacement: ApprovalItem | None,
        idempotent_result: DraftPublicationResult | None,
        audit_batch: PreflightedAuditBatch | None,
        merged_audit_intents: "TerminalDraftAuditIntents | None",
        capabilities: ApprovalCommandCapabilities,
        _seal: object,
    ) -> None:
        if _seal is not _TERMINAL_DRAFT_PREFLIGHT_SEAL:
            raise TypeError("PreflightedTerminalDraft is issued only by the authority service")
        state = _TerminalDraftPreflightState(
            prepared=prepared.model_copy(deep=True),
            context=context.model_copy(deep=True),
            expected_head=expected_head.model_copy(deep=True),
            old_item=None if old_item is None else old_item.model_copy(deep=True),
            old_replacement=(
                None if old_replacement is None else old_replacement.model_copy(deep=True)
            ),
            idempotent_result=(
                None if idempotent_result is None else idempotent_result.model_copy(deep=True)
            ),
            audit_batch=audit_batch,
            merged_audit_intents=merged_audit_intents,
            capabilities_owner=capabilities,
            capabilities=replace(capabilities),
            transaction_identity=_approval_transaction_identity(capabilities),
        )
        with _TERMINAL_DRAFT_PREFLIGHT_LOCK:
            _TERMINAL_DRAFT_PREFLIGHT_STATES[self] = state

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("terminal draft preflight token is immutable")

    def consume(
        self,
        *,
        context: ApprovalCommandContext,
        capabilities: ApprovalCommandCapabilities,
    ) -> _TerminalDraftPreflightState:
        current_transaction_identity = _approval_transaction_identity(capabilities)
        with _TERMINAL_DRAFT_PREFLIGHT_LOCK:
            state = _TERMINAL_DRAFT_PREFLIGHT_STATES.get(self)
            capabilities_match = state is not None and all(
                getattr(state.capabilities, item.name) is getattr(capabilities, item.name)
                for item in fields(ApprovalCommandCapabilities)
            )
            if (
                state is None
                or self in _CONSUMED_TERMINAL_DRAFT_PREFLIGHTS
                or state.context != context
                or state.capabilities_owner is not capabilities
                or not capabilities_match
                or (state.transaction_identity is None) != (current_transaction_identity is None)
                or (
                    state.transaction_identity is not None
                    and current_transaction_identity is not None
                    and any(
                        retained is not current
                        for retained, current in zip(
                            state.transaction_identity,
                            current_transaction_identity,
                            strict=True,
                        )
                    )
                )
            ):
                raise IntegrityViolation(
                    "terminal draft preflight token is invalid, stale, cross-transaction, or reused"
                )
            _CONSUMED_TERMINAL_DRAFT_PREFLIGHTS.add(self)
        return state

    def audit_intents_for_terminal_merge(
        self,
        *,
        context: ApprovalCommandContext,
        capabilities: ApprovalCommandCapabilities,
    ) -> "TerminalDraftAuditIntents":
        """Expose only the audit intents reserved for the enclosing terminal batch.

        This is a non-consuming transaction/identity check.  The enclosing terminal
        publisher must include the returned intents in its one AuditGate preflight;
        the later workflow apply consumes this token but deliberately performs no
        independent audit-head CAS.
        """

        current_transaction_identity = _approval_transaction_identity(capabilities)
        with _TERMINAL_DRAFT_PREFLIGHT_LOCK:
            state = _TERMINAL_DRAFT_PREFLIGHT_STATES.get(self)
            capabilities_match = state is not None and all(
                getattr(state.capabilities, item.name) is getattr(capabilities, item.name)
                for item in fields(ApprovalCommandCapabilities)
            )
            if (
                state is None
                or self in _CONSUMED_TERMINAL_DRAFT_PREFLIGHTS
                or state.context != context
                or state.capabilities_owner is not capabilities
                or not capabilities_match
                or (state.transaction_identity is None) != (current_transaction_identity is None)
                or (
                    state.transaction_identity is not None
                    and current_transaction_identity is not None
                    and any(
                        retained is not current
                        for retained, current in zip(
                            state.transaction_identity,
                            current_transaction_identity,
                            strict=True,
                        )
                    )
                )
            ):
                raise IntegrityViolation(
                    "terminal draft audit intents are invalid, stale, or cross-transaction"
                )
            merged = state.merged_audit_intents
            if merged is None or state.audit_batch is not None:
                raise IntegrityViolation(
                    "terminal draft was not preflighted for a merged Audit batch"
                )
            return TerminalDraftAuditIntents(
                chain_id=merged.chain_id,
                intents=tuple(merged.intents),
            )


_TERMINAL_DRAFT_PREFLIGHT_LOCK = Lock()
_TERMINAL_DRAFT_PREFLIGHT_STATES: WeakKeyDictionary[
    PreflightedTerminalDraft,
    _TerminalDraftPreflightState,
] = WeakKeyDictionary()
_CONSUMED_TERMINAL_DRAFT_PREFLIGHTS: WeakSet[PreflightedTerminalDraft] = WeakSet()


def _utc_text(clock: UtcClock) -> str:
    now = clock.now_utc()
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("approval command clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_decision_id() -> str:
    return f"decision:{uuid4()}"


def _required[T](value: T | None, name: str) -> T:
    if value is None:
        raise IntegrityViolation(f"{name} approval command capability is unavailable")
    return value


def _replace_item(item: ApprovalItem, **updates: object) -> ApprovalItem:
    payload = item.model_dump(mode="python")
    payload.update(updates)
    return ApprovalItem.model_validate(payload)


def require_human_constraint_revision(
    item: ApprovalItem,
    *,
    subject_kind: SubjectKind,
    subject_revision: int | None,
    produced_by: Literal["agent", "human"],
    producer_run_id: str | None,
    supersedes_artifact_id: str | None,
) -> None:
    """Apply the one constraint human-revision guard at every validation seam."""

    if subject_kind != item.subject_kind:
        raise IntegrityViolation("subject payload kind differs from ApprovalItem")
    if subject_revision is not None and subject_revision != item.subject_revision:
        raise IntegrityViolation("subject payload revision differs from ApprovalItem")
    if item.subject_kind == "constraint_proposal" and (
        produced_by != "human"
        or producer_run_id is not None
        or item.proposer.principal_kind != "human"
        or subject_revision is None
        or subject_revision <= 1
        or supersedes_artifact_id is None
    ):
        raise InvalidStateTransition(
            "constraint proposal requires a superseding human author revision"
        )


class ApprovalCommandService:
    def __init__(
        self,
        *,
        unit_of_work: ApprovalUnitOfWork,
        bind_capabilities: CapabilityBinder,
        clock: UtcClock,
        audit_chain_id: str,
        decision_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not audit_chain_id:
            raise ValueError("audit_chain_id must be non-empty")
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._clock = clock
        self._audit_chain_id = audit_chain_id
        self._decision_id_factory = decision_id_factory or _default_decision_id

    def publish_draft(
        self,
        *,
        prepared: PreparedDraft,
        context: ApprovalCommandContext,
    ) -> DraftPublicationResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            return self._publish_draft_in_transaction(
                prepared=prepared,
                context=context,
                capabilities=capabilities,
                operation="approval.publish_draft",
            )

    def publish_draft_in_transaction(
        self,
        *,
        prepared: PreparedDraft,
        context: ApprovalCommandContext,
        capabilities: ApprovalCommandCapabilities,
    ) -> DraftPublicationResult:
        """Run the canonical draft authority inside an already-owned write UoW.

        Terminal publication owns the transaction that first publishes the final
        Run Artifacts.  Its workflow-effect adapter must therefore reuse the same
        draft validation/CAS/idempotency/audit core without opening a nested UoW.
        The caller may only pass capabilities bound to that active transaction;
        this entry point deliberately performs no alternate state transition.
        """

        return self._publish_draft_in_transaction(
            prepared=prepared,
            context=context,
            capabilities=capabilities,
            operation="approval.publish_draft",
        )

    def commit_prepared_terminal_draft_in_transaction(
        self,
        *,
        prepared: PreparedTerminalDraft,
        context: ApprovalCommandContext,
        capabilities: ApprovalCommandCapabilities,
    ) -> DraftPublicationResult:
        """Compatibility wrapper over explicit SELECT-only preflight + DML apply."""

        preflighted = self.preflight_prepared_terminal_draft_in_transaction(
            prepared=prepared,
            context=context,
            capabilities=capabilities,
        )
        return self.apply_preflighted_terminal_draft_in_transaction(
            preflighted=preflighted,
            context=context,
            capabilities=capabilities,
        )

    def preflight_prepared_terminal_draft_in_transaction(
        self,
        *,
        prepared: PreparedTerminalDraft,
        context: ApprovalCommandContext,
        capabilities: ApprovalCommandCapabilities,
        merge_audit_into_terminal_batch: bool = False,
    ) -> PreflightedTerminalDraft:
        """Perform every mutable authority SELECT before the first terminal DML."""

        approvals = _required(capabilities.approvals, "approvals")
        policies = _required(capabilities.policies, "policies")
        idempotency = _required(capabilities.idempotency, "idempotency")
        audit = _required(capabilities.audit, "audit")
        runs = _required(capabilities.runs, "runs")
        item = prepared.approval_item
        facts = prepared.subject_facts
        self._validate_new_draft(item, context)
        registry, route, role, approval_policy = self._resolve_policies(
            item=item,
            policies=policies,
        )
        validate_approval_policy_bindings(
            item=item,
            domain_registry=registry,
            route_policy=route,
            role_policy=role,
            approval_policy=approval_policy,
        )
        if facts.subject_kind != item.subject_kind:
            raise IntegrityViolation("terminal subject payload kind differs from ApprovalItem")
        self._require_subject_revision_binding(item, facts)
        if (
            facts.produced_by != "agent"
            or context.actor.principal_kind not in {"service", "system"}
            or context.initiated_by is None
            or context.run_id != facts.producer_run_id
        ):
            raise IntegrityViolation("prepared terminal draft lacks exact Agent producer authority")
        runs.verify_prepared_terminal_producer_authority(
            run_id=facts.producer_run_id,
            initiated_by=item.proposer,
        )

        expected_head = self._next_head(prepared)
        replay = self._get_idempotent(
            idempotency,
            context,
            operation="approval.publish_draft",
        )
        if replay is not None:
            result = self._draft_publication_response(replay)
            normalized = item.model_copy(update={"created_at": result.approval_item.created_at})
            if result.approval_item != normalized or result.subject_head != expected_head:
                raise IntegrityViolation(
                    "terminal draft idempotency result differs from its preparation"
                )
            return PreflightedTerminalDraft(
                prepared=prepared,
                context=context,
                expected_head=expected_head,
                old_item=None,
                old_replacement=None,
                idempotent_result=result,
                audit_batch=None,
                merged_audit_intents=(
                    TerminalDraftAuditIntents(
                        chain_id=self._audit_chain_id,
                        intents=(),
                    )
                    if merge_audit_into_terminal_batch
                    else None
                ),
                capabilities=capabilities,
                _seal=_TERMINAL_DRAFT_PREFLIGHT_SEAL,
            )

        if isinstance(item.target_binding, (PatchTargetBindingV1, RollbackTargetBindingV1)):
            self._verify_fresh_draft_ref_authority(
                prepared=prepared,
                facts=facts,
                refs=_required(capabilities.refs, "refs"),
            )

        current = approvals.current(item.subject_series_id)
        old_item: ApprovalItem | None = None
        if prepared.expected_subject_head is None:
            if current is not None:
                raise Conflict("terminal draft expected no current SubjectHead")
            if (
                item.subject_revision != 1
                or item.supersedes_approval_id is not None
                or facts.supersedes_artifact_id is not None
            ):
                raise IntegrityViolation(
                    "initial terminal draft must be revision 1 without supersedes bindings"
                )
        else:
            if current is None or current[0] != prepared.expected_subject_head:
                raise Conflict("terminal draft SubjectHead precondition did not match")
            old_item = current[1]
            self._require_if_match(
                context,
                resource_kind=old_item.subject_kind,
                resource_id=old_item.subject_artifact_id,
                revision=old_item.workflow_revision,
            )
            self._validate_superseding_draft(prepared, facts, old_item)
            if old_item.active_validation_run_id is not None:
                raise IntegrityViolation(
                    "prepared repair draft cannot supersede an active validation Run"
                )

        audit_items = (() if old_item is None else (("approval.superseded", old_item),)) + (
            ("approval.draft_published", item),
        )
        audit_intents = tuple(
            self._audit_intent(context, action=action, item=audit_item)
            for action, audit_item in audit_items
        )
        audit_batch = (
            None
            if merge_audit_into_terminal_batch
            else audit.prepare_batch(
                chain_id=self._audit_chain_id,
                intents=audit_intents,
            )
        )
        merged_audit_intents = (
            TerminalDraftAuditIntents(
                chain_id=self._audit_chain_id,
                intents=audit_intents,
            )
            if merge_audit_into_terminal_batch
            else None
        )

        return PreflightedTerminalDraft(
            prepared=prepared,
            context=context,
            expected_head=expected_head,
            old_item=old_item,
            old_replacement=(None if old_item is None else self._superseded_item(old_item)),
            idempotent_result=None,
            audit_batch=audit_batch,
            merged_audit_intents=merged_audit_intents,
            capabilities=capabilities,
            _seal=_TERMINAL_DRAFT_PREFLIGHT_SEAL,
        )

    def apply_preflighted_terminal_draft_in_transaction(
        self,
        *,
        preflighted: PreflightedTerminalDraft,
        context: ApprovalCommandContext,
        capabilities: ApprovalCommandCapabilities,
    ) -> DraftPublicationResult:
        """Apply a same-transaction preflight token without any SELECT/recompute."""

        state = preflighted.consume(context=context, capabilities=capabilities)
        retained_capabilities = state.capabilities
        retained_context = state.context
        if state.idempotent_result is not None:
            return state.idempotent_result
        approvals = _required(retained_capabilities.approvals, "approvals")
        idempotency = _required(retained_capabilities.idempotency, "idempotency")
        audit = _required(retained_capabilities.audit, "audit")
        prepared = state.prepared
        item = prepared.approval_item
        old_item = state.old_item

        if old_item is not None:
            old_replacement = state.old_replacement
            if old_replacement is None:
                raise IntegrityViolation("terminal draft preflight lost supersede replacement")
            approvals.apply_preflighted_compare_and_set(
                old_item,
                old_replacement,
            )
        approvals.apply_preflighted_insert_draft(item)
        approvals.apply_preflighted_subject_head(
            expected=prepared.expected_subject_head,
            expected_item=old_item,
            replacement=state.expected_head,
            replacement_item=item,
        )
        if state.merged_audit_intents is None:
            if state.audit_batch is None:
                raise IntegrityViolation("terminal draft preflight lacks its audit batch")
            audit.apply_prepared_batch(state.audit_batch)
        elif state.audit_batch is not None:
            raise IntegrityViolation("terminal draft preflight has two Audit authorities")
        result = DraftPublicationResult(
            approval_item=item,
            subject_head=state.expected_head,
        )
        self._put_preflighted_idempotent(
            idempotency,
            retained_context,
            operation="approval.publish_draft",
            item=item,
            response=result.model_dump(mode="json"),
        )
        return result

    def publish_rebased_draft(
        self,
        *,
        prepared: PreparedDraft,
        context: ApprovalCommandContext,
        expected_ref: RefValue,
    ) -> DraftPublicationResult:
        with self._unit_of_work.begin() as transaction:
            return self.publish_rebased_draft_in_transaction(
                transaction=transaction,
                prepared=prepared,
                context=context,
                expected_ref=expected_ref,
            )

    def publish_rebased_draft_in_transaction(
        self,
        *,
        transaction: Any,
        prepared: PreparedDraft,
        context: ApprovalCommandContext,
        expected_ref: RefValue,
    ) -> DraftPublicationResult:
        capabilities = self._bind_capabilities(transaction)
        binding = prepared.approval_item.target_binding
        if not isinstance(binding, PatchTargetBindingV1):
            raise IntegrityViolation("rebased draft requires a Patch target binding")
        if binding.expected_ref != expected_ref:
            raise IntegrityViolation("rebased draft expected ref differs from its target binding")
        return self._publish_draft_in_transaction(
            prepared=prepared,
            context=context,
            capabilities=capabilities,
            operation="approval.publish_rebased_draft",
        )

    def _publish_draft_in_transaction(
        self,
        *,
        prepared: PreparedDraft,
        context: ApprovalCommandContext,
        capabilities: ApprovalCommandCapabilities,
        operation: Literal[
            "approval.publish_draft",
            "approval.publish_rebased_draft",
        ],
    ) -> DraftPublicationResult:
        approvals = _required(capabilities.approvals, "approvals")
        policies = _required(capabilities.policies, "policies")
        artifacts = _required(capabilities.artifacts, "artifacts")
        bindings = _required(capabilities.object_bindings, "object_bindings")
        idempotency = _required(capabilities.idempotency, "idempotency")
        audit = _required(capabilities.audit, "audit")
        runs = capabilities.runs
        subjects = _required(capabilities.subjects, "subjects")
        lineage = _required(capabilities.lineage, "lineage")

        item = prepared.approval_item
        self._validate_new_draft(item, context)
        registry, route, role, approval_policy = self._resolve_policies(
            item=item,
            policies=policies,
        )
        validate_approval_policy_bindings(
            item=item,
            domain_registry=registry,
            route_policy=route,
            role_policy=role,
            approval_policy=approval_policy,
        )
        facts = subjects.inspect_draft_subject(prepared.subject_artifact)
        self._validate_subject_facts(
            prepared=prepared,
            facts=facts,
            context=context,
            runs=runs,
        )
        self._authorize_human_draft(
            capabilities=capabilities,
            context=context,
            item=item,
            facts=facts,
            registry=registry,
            role_policy=role,
        )
        retained_parents = self._validate_lineage_parents(prepared, artifacts)
        lineage.validate_draft_publication(
            prepared=prepared,
            retained_parent_ids=retained_parents,
        )
        self._validate_target_binding(prepared, facts, artifacts)

        expected_head = self._next_head(prepared)
        replay = self._get_idempotent(
            idempotency,
            context,
            operation=operation,
        )
        if replay is not None:
            result = self._draft_publication_response(replay)
            # ``created_at`` is a server-owned timestamp stamped at first creation; a
            # re-assembled draft under a real advancing clock carries a later value.
            # Verify the retained committed item matches the prepared item modulo that
            # timestamp so a duplicate exact request replays instead of failing closed.
            normalized = item.model_copy(update={"created_at": result.approval_item.created_at})
            if result.approval_item != normalized or result.subject_head != expected_head:
                raise IntegrityViolation("draft idempotency result differs from prepared draft")
            return result

        if isinstance(item.target_binding, (PatchTargetBindingV1, RollbackTargetBindingV1)):
            self._verify_fresh_draft_ref_authority(
                prepared=prepared,
                facts=facts,
                refs=_required(capabilities.refs, "refs"),
            )

        current = approvals.current(item.subject_series_id)
        old_item: ApprovalItem | None = None
        if prepared.expected_subject_head is None:
            if current is not None:
                raise Conflict("draft expected no current SubjectHead")
            if (
                item.subject_revision != 1
                or item.supersedes_approval_id is not None
                or facts.supersedes_artifact_id is not None
            ):
                raise IntegrityViolation(
                    "initial draft must be revision 1 without supersedes bindings"
                )
        else:
            if current is None or current[0] != prepared.expected_subject_head:
                raise Conflict("draft SubjectHead precondition did not match")
            old_item = current[1]
            self._require_if_match(
                context,
                resource_kind=old_item.subject_kind,
                resource_id=old_item.subject_artifact_id,
                revision=old_item.workflow_revision,
            )
            self._validate_superseding_draft(prepared, facts, old_item)

        for binding in prepared.object_bindings:
            published_binding = bindings.bind_verified(
                binding.object_ref,
                binding.location,
                binding.expected_revision,
            )
            if (
                published_binding.object_ref != binding.object_ref
                or published_binding.location != binding.location
                or published_binding.status != "active"
            ):
                raise IntegrityViolation("ObjectBinding publisher returned another binding")
        for artifact in self._topological_artifacts(prepared):
            if artifacts.put(artifact) != artifact:
                raise IntegrityViolation("Artifact publisher returned another Artifact")

        if old_item is not None:
            old_replacement = self._superseded_item(old_item)
            if old_item.active_validation_run_id is not None:
                _required(runs, "runs").request_validation_cancel(
                    run_id=old_item.active_validation_run_id,
                    reason="subject_superseded",
                    requested_by=context.actor,
                )
            approvals.compare_and_set(
                old_item.approval_id,
                old_item.workflow_revision,
                old_replacement,
            )
        approvals.insert_draft(item)
        approvals.compare_and_set_subject_head(
            item.subject_series_id,
            prepared.expected_subject_head,
            expected_head,
        )

        if old_item is not None:
            self._audit(
                audit,
                context,
                action="approval.superseded",
                item=old_item,
            )
        self._audit(
            audit,
            context,
            action="approval.draft_published",
            item=item,
        )
        result = DraftPublicationResult(
            approval_item=item,
            subject_head=expected_head,
        )
        self._put_idempotent(
            idempotency,
            context,
            operation=operation,
            item=item,
            response=result.model_dump(mode="json"),
        )
        return result

    @staticmethod
    def _verify_fresh_draft_ref_authority(
        *,
        prepared: PreparedDraft | PreparedTerminalDraft,
        facts: DraftSubjectFacts,
        refs: RefReader,
    ) -> None:
        binding = prepared.approval_item.target_binding
        if not isinstance(binding, (PatchTargetBindingV1, RollbackTargetBindingV1)):
            raise IntegrityViolation("ref-bound draft requires a Patch or Rollback binding")
        actual = refs.get(binding.ref_name)
        if actual != binding.expected_ref:
            raise Conflict(
                "draft ref precondition did not match",
                ref_name=binding.ref_name,
                expected=(
                    None
                    if binding.expected_ref is None
                    else binding.expected_ref.model_dump(mode="json")
                ),
                actual=None if actual is None else actual.model_dump(mode="json"),
            )
        if isinstance(binding, PatchTargetBindingV1):
            return
        request = facts.rollback_request
        if request is None:
            raise IntegrityViolation("rollback ref guard requires the typed RollbackRequest")
        historical = refs.get_history_entry(
            binding.ref_name,
            request.target_history_revision,
        )
        expected_history = RefValue(
            artifact_id=binding.target_artifact_id,
            revision=request.target_history_revision,
        )
        if historical != expected_history:
            raise Conflict(
                "rollback target is not the exact ref history member",
                ref_name=binding.ref_name,
                target_history_revision=request.target_history_revision,
            )

    def start_validation(
        self,
        *,
        prepared: PreparedValidationStart,
        context: ApprovalCommandContext,
    ) -> ValidationStartResult:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            approvals = _required(capabilities.approvals, "approvals")
            policies = _required(capabilities.policies, "policies")
            runs = _required(capabilities.runs, "runs")
            artifacts = _required(capabilities.artifacts, "artifacts")
            subjects = _required(capabilities.subjects, "subjects")
            idempotency = _required(capabilities.idempotency, "idempotency")
            audit = _required(capabilities.audit, "audit")
            item = self._load_item(approvals, prepared.approval_id)
            self._validate_start_binding(item, prepared)
            subject = self._load_artifact(artifacts, item.subject_artifact_id)
            facts = subjects.inspect_draft_subject(subject)
            require_human_constraint_revision(
                item,
                subject_kind=facts.subject_kind,
                subject_revision=facts.subject_revision,
                produced_by=facts.produced_by,
                producer_run_id=facts.producer_run_id,
                supersedes_artifact_id=facts.supersedes_artifact_id,
            )

            replay = self._get_idempotent(
                idempotency,
                context,
                operation="approval.start_validation",
            )
            if replay is not None:
                return self._validation_start_response(
                    replay,
                    prepared=prepared,
                    current=item,
                )

            self._require_current_head(approvals, item)
            self._validate_bound_policies(item, policies)
            self._verify_agent_producer(item=item, facts=facts, runs=runs)

            next_revision = next_workflow_revision(
                actual=item.workflow_revision,
                expected=prepared.expected_workflow_revision,
            )
            validate_status_transition(
                current=item.status,
                target="validating",
                subject_kind=item.subject_kind,
            )
            run_id = runs.start_validation(
                prepared=prepared,
                item=item,
                initiated_by=context.accountable_actor,
            )
            if run_id != prepared.run_id:
                raise IntegrityViolation("validation starter returned another run_id")
            replacement = _replace_item(
                item,
                status="validating",
                workflow_revision=next_revision,
                active_validation_run_id=run_id,
                last_validation_failure_artifact_id=None,
            )
            approvals.compare_and_set(
                item.approval_id,
                prepared.expected_workflow_revision,
                replacement,
            )
            self._audit(
                audit,
                context,
                action="approval.validation_started",
                item=replacement,
            )
            result = ValidationStartResult(approval_item=replacement, run_id=run_id)
            self._put_idempotent(
                idempotency,
                context,
                operation="approval.start_validation",
                item=replacement,
                response=result.model_dump(mode="json"),
            )
            return result

    def submit_for_approval(
        self,
        *,
        approval_id: str,
        expected_workflow_revision: int,
        context: ApprovalCommandContext,
        expected_subject_artifact_id: str | None = None,
        expected_subject_kind: str | None = None,
    ) -> ApprovalItem:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            approvals = _required(capabilities.approvals, "approvals")
            policies = _required(capabilities.policies, "policies")
            artifacts = _required(capabilities.artifacts, "artifacts")
            idempotency = _required(capabilities.idempotency, "idempotency")
            audit = _required(capabilities.audit, "audit")
            subjects = _required(capabilities.subjects, "subjects")
            evidence = _required(capabilities.evidence, "evidence")

            item = self._load_item(approvals, approval_id)
            if (
                expected_subject_artifact_id is not None
                and item.subject_artifact_id != expected_subject_artifact_id
            ):
                raise Conflict(
                    "submit path does not bind the ApprovalItem subject Artifact",
                    expected_subject_artifact_id=expected_subject_artifact_id,
                    actual_subject_artifact_id=item.subject_artifact_id,
                )
            if expected_subject_kind is not None and item.subject_kind != expected_subject_kind:
                raise Conflict(
                    "submit endpoint does not match the ApprovalItem subject kind",
                    expected_subject_kind=expected_subject_kind,
                    actual_subject_kind=item.subject_kind,
                )
            replay = self._get_idempotent(
                idempotency,
                context,
                operation="approval.submit",
            )
            if replay is not None:
                return self._approval_item_response(
                    replay,
                    operation="submit",
                    approval_id=approval_id,
                    expected_workflow_revision=expected_workflow_revision,
                    expected_statuses=frozenset({"pending_approval", "auto_apply_eligible"}),
                    current=item,
                )

            self._require_if_match(
                context,
                resource_kind=item.subject_kind,
                resource_id=item.subject_artifact_id,
                revision=item.workflow_revision,
            )
            subject = self._load_artifact(artifacts, item.subject_artifact_id)
            facts = subjects.inspect_draft_subject(subject)
            require_human_constraint_revision(
                item,
                subject_kind=facts.subject_kind,
                subject_revision=facts.subject_revision,
                produced_by=facts.produced_by,
                producer_run_id=facts.producer_run_id,
                supersedes_artifact_id=facts.supersedes_artifact_id,
            )
            self._require_current_head(approvals, item)
            self._validate_bound_policies(item, policies)
            self._verify_agent_producer(
                item=item,
                facts=facts,
                runs=capabilities.runs,
            )
            # Fail closed with a workflow guard BEFORE inspecting evidence: submit is a
            # ``validated → pending_approval`` transition, so a draft / validating /
            # validation_failed / already-decided subject is rejected here rather than
            # tripping a downstream evidence integrity check (a validation_failed subject
            # carries the failed regression evidence, which the submission-evidence guard
            # would otherwise surface as an internal error instead of this guard).
            if item.status != "validated":
                raise InvalidStateTransition(
                    "approval subject is not validated: submit requires a validated subject"
                )
            projection = self._validate_submission_evidence(
                item=item,
                subject=subject,
                artifacts=artifacts,
                evidence=evidence,
            )
            if projection.validation_status != "passed" or projection.regression_status not in {
                "passed",
                "not_applicable",
            }:
                raise InvalidStateTransition("submission evidence is not passed")

            target_status = "pending_approval"
            if item.auto_apply_proof is not None:
                auto_apply = _required(capabilities.auto_apply, "auto_apply")
                auto_apply.validate_eligibility(item=item)
                target_status = "auto_apply_eligible"

            next_revision = next_workflow_revision(
                actual=item.workflow_revision,
                expected=expected_workflow_revision,
            )
            validate_status_transition(
                current=item.status,
                target=target_status,
                subject_kind=item.subject_kind,
            )
            replacement = _replace_item(
                item,
                status=target_status,
                workflow_revision=next_revision,
                submitted_at=_utc_text(self._clock),
            )
            approvals.compare_and_set(
                item.approval_id,
                expected_workflow_revision,
                replacement,
            )
            self._audit(
                audit,
                context,
                action="approval.submitted",
                item=replacement,
            )
            response = {"approval_item": replacement.model_dump(mode="json")}
            self._put_idempotent(
                idempotency,
                context,
                operation="approval.submit",
                item=replacement,
                response=response,
            )
            return replacement

    def decide(
        self,
        *,
        approval_id: str,
        decision: ApprovalDecision,
        principal: Principal,
        context: ApprovalCommandContext,
    ) -> ApprovalItem:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            approvals = _required(capabilities.approvals, "approvals")
            policies = _required(capabilities.policies, "policies")
            principals = _required(capabilities.principals, "principals")
            idempotency = _required(capabilities.idempotency, "idempotency")
            audit = _required(capabilities.audit, "audit")
            item = self._load_item(approvals, approval_id)
            if context.actor != decision.actor or context.initiated_by is not None:
                raise IntegrityViolation("decision context must be the human decision actor")

            replay = self._get_idempotent(
                idempotency,
                context,
                operation="approval.decide",
            )
            if replay is not None:
                return self._decision_response(
                    replay,
                    approval_id=approval_id,
                    decision=decision,
                    current=item,
                )

            self._require_if_match(
                context,
                resource_kind="approval",
                resource_id=item.approval_id,
                revision=item.workflow_revision,
            )
            registry, route, role, approval_policy = self._resolve_policies(
                item=item,
                policies=policies,
            )
            validate_approval_policy_bindings(
                item=item,
                domain_registry=registry,
                route_policy=route,
                role_policy=role,
                approval_policy=approval_policy,
            )
            self._require_current_head(approvals, item)
            replacement = apply_approval_decision(
                item=item,
                decision=decision,
                principal=principal,
                domain_registry=registry,
                route_policy=route,
                role_policy=role,
                approval_policy=approval_policy,
                principal_resolver=lambda principal_id: (
                    principal if principal_id == principal.id else principals.get(principal_id)
                ),
            )
            if any(prior.decision_id == decision.decision_id for prior in item.decisions):
                raise IntegrityViolation("decision exists without its command idempotency result")

            approvals.append_decision_and_compare_and_set(
                item.approval_id,
                decision.expected_workflow_revision,
                decision,
                replacement,
            )
            action = {
                "approved": "approval.approved",
                "rejected": "approval.rejected",
                "changes_requested": "approval.changes_requested",
                "pending_approval": "approval.partially_approved",
            }[replacement.status]
            self._audit(audit, context, action=action, item=replacement)
            response = {"approval_item": replacement.model_dump(mode="json")}
            self._put_idempotent(
                idempotency,
                context,
                operation="approval.decide",
                item=replacement,
                response=response,
            )
            return replacement

    def decide_current(
        self,
        *,
        approval_id: str,
        request: ApprovalDecisionRequest,
        context: ApprovalCommandContext,
    ) -> ApprovalItem:
        """Decide using only current server authority and server-owned metadata."""

        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            approvals = _required(capabilities.approvals, "approvals")
            idempotency = _required(capabilities.idempotency, "idempotency")
            item = self._load_item(approvals, approval_id)
            if context.actor.principal_kind != "human" or context.initiated_by is not None:
                raise Forbidden("approval decisions require a direct human actor")

            replay = self._get_idempotent(
                idempotency,
                context,
                operation="approval.decide",
            )
            if replay is not None:
                return self._decision_request_response(
                    replay,
                    approval_id=approval_id,
                    request=request,
                    actor=context.actor,
                    current=item,
                )

            self._require_if_match(
                context,
                resource_kind="approval",
                resource_id=item.approval_id,
                revision=item.workflow_revision,
            )
            self._require_current_head(approvals, item)
            policies = _required(capabilities.policies, "policies")
            principals = _required(capabilities.principals, "principals")
            audit = _required(capabilities.audit, "audit")
            principal = principals.get(context.actor.principal_id)
            if principal is None:
                raise Forbidden("approval decision actor has no current principal")
            registry, route, role, approval_policy = self._resolve_policies(
                item=item,
                policies=policies,
            )
            validate_approval_policy_bindings(
                item=item,
                domain_registry=registry,
                route_policy=route,
                role_policy=role,
                approval_policy=approval_policy,
            )

            decision = ApprovalDecision(
                decision_id=self._decision_id_factory(),
                requirement_ids=request.requirement_ids,
                decision=request.decision,
                actor=context.actor,
                expected_workflow_revision=request.expected_workflow_revision,
                reason_code=request.reason_code,
                comment=request.comment,
                occurred_at=_utc_text(self._clock),
            )
            replacement = apply_approval_decision(
                item=item,
                decision=decision,
                principal=principal,
                domain_registry=registry,
                route_policy=route,
                role_policy=role,
                approval_policy=approval_policy,
                principal_resolver=principals.get,
            )
            if any(prior.decision_id == decision.decision_id for prior in item.decisions):
                raise IntegrityViolation("decision exists without its command idempotency result")

            approvals.append_decision_and_compare_and_set(
                item.approval_id,
                request.expected_workflow_revision,
                decision,
                replacement,
            )
            action = {
                "approved": "approval.approved",
                "rejected": "approval.rejected",
                "changes_requested": "approval.changes_requested",
                "pending_approval": "approval.partially_approved",
            }[replacement.status]
            self._audit(audit, context, action=action, item=replacement)
            response = {"approval_item": replacement.model_dump(mode="json")}
            self._put_idempotent(
                idempotency,
                context,
                operation="approval.decide",
                item=replacement,
                response=response,
            )
            return replacement

    def project_patch_state(self, approval_id: str) -> PatchView:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._bind_capabilities(transaction)
            approvals = _required(capabilities.approvals, "approvals")
            artifacts = _required(capabilities.artifacts, "artifacts")
            subjects = _required(capabilities.subjects, "subjects")
            evidence = _required(capabilities.evidence, "evidence")
            item = self._load_item(approvals, approval_id)
            if item.subject_kind != "patch":
                raise InvalidStateTransition("PatchView requires a patch ApprovalItem")
            subject = self._load_artifact(artifacts, item.subject_artifact_id)
            patch = subjects.load_patch(subject)
            if patch.revision != item.subject_revision:
                raise IntegrityViolation("Patch revision differs from ApprovalItem")
            projection = evidence.project_state(item=item)
            return PatchView(
                patch=patch,
                validation_status=projection.validation_status,
                regression_status=projection.regression_status,
                approval_status=item.status,
                workflow_revision=item.workflow_revision,
            )

    @staticmethod
    def _require_if_match(
        context: ApprovalCommandContext,
        *,
        resource_kind: str,
        resource_id: str,
        revision: int,
    ) -> None:
        if context.if_match is None:
            return
        expected = compute_resource_etag(
            resource_kind=resource_kind,
            resource_id=resource_id,
            revision=revision,
        )
        if context.if_match != expected:
            raise Conflict(
                "If-Match does not match the authoritative resource revision",
                resource_kind=resource_kind,
                resource_id=resource_id,
                revision=revision,
            )

    @staticmethod
    def _validate_new_draft(
        item: ApprovalItem,
        context: ApprovalCommandContext,
    ) -> None:
        if (
            item.status != "draft"
            or item.workflow_revision != 1
            or item.decisions
            or item.active_validation_run_id is not None
            or item.last_validation_failure_artifact_id is not None
            or item.evidence_set_artifact_id is not None
            or item.regression_evidence_artifact_ids
            or item.auto_apply_proof is not None
            or item.submitted_at is not None
            or item.decided_at is not None
            or item.applied_at is not None
        ):
            raise IntegrityViolation("new ApprovalItem must be a clean draft")
        if item.proposer != context.accountable_actor:
            raise IntegrityViolation("ApprovalItem proposer differs from accountable actor")

    @staticmethod
    def _validate_subject_facts(
        *,
        prepared: PreparedDraft,
        facts: DraftSubjectFacts,
        context: ApprovalCommandContext,
        runs: ApprovalRunGateway | None,
    ) -> None:
        item = prepared.approval_item
        if facts.subject_kind != item.subject_kind:
            raise IntegrityViolation("subject payload kind differs from ApprovalItem")
        ApprovalCommandService._require_subject_revision_binding(item, facts)
        if facts.produced_by == "agent":
            if (
                context.actor.principal_kind not in {"service", "system"}
                or context.initiated_by is None
            ):
                raise IntegrityViolation(
                    "Agent draft publication requires a service/system worker and initiator"
                )
            if context.run_id != facts.producer_run_id:
                raise IntegrityViolation(
                    "Agent draft audit correlation differs from its producer Run"
                )
            ApprovalCommandService._verify_agent_producer(
                item=item,
                facts=facts,
                runs=runs,
            )
        elif (
            item.proposer.principal_kind != "human"
            or context.actor.principal_kind != "human"
            or context.initiated_by is not None
        ):
            raise IntegrityViolation(
                "human-authored draft requires direct publication by its human proposer"
            )

    @staticmethod
    def _authorize_human_draft(
        *,
        capabilities: ApprovalCommandCapabilities,
        context: ApprovalCommandContext,
        item: ApprovalItem,
        facts: DraftSubjectFacts,
        registry: DomainRegistryV1,
        role_policy: RolePolicy,
    ) -> None:
        if facts.produced_by != "human":
            return
        principals = _required(capabilities.principals, "principals")
        principal = principals.get(context.actor.principal_id)
        if principal is None or principal.kind != "human":
            raise Forbidden("draft actor has no current human principal")
        permission = Permission(
            action="propose",
            resource_kind=item.subject_kind,
            domain_scope=item.domain_scope,
        )
        if (
            authorize(
                principal=principal,
                role_policy=role_policy,
                requested_permission=permission,
                domain_registry=registry,
            )
            is not AuthorizationDecision.ALLOW
        ):
            raise Forbidden("draft actor lacks the current domain permission")

    @staticmethod
    def _require_subject_revision_binding(
        item: ApprovalItem,
        facts: DraftSubjectFacts,
    ) -> None:
        if facts.subject_revision is not None and facts.subject_revision != item.subject_revision:
            raise IntegrityViolation("subject payload revision differs from ApprovalItem")

    @staticmethod
    def _verify_agent_producer(
        *,
        item: ApprovalItem,
        facts: DraftSubjectFacts,
        runs: ApprovalRunGateway | None,
    ) -> None:
        if facts.produced_by != "agent":
            return
        if facts.producer_run_id is None:  # guarded by DraftSubjectFacts
            raise IntegrityViolation("agent subject has no producer Run")
        _required(runs, "runs").verify_producer_membership(
            run_id=facts.producer_run_id,
            artifact_id=item.subject_artifact_id,
            initiated_by=item.proposer,
        )

    @staticmethod
    def _validate_lineage_parents(
        prepared: PreparedDraft,
        artifacts: ArtifactRepository,
    ) -> tuple[str, ...]:
        prepared_ids = {artifact.artifact_id for artifact in prepared.artifacts}
        retained: set[str] = set()
        for artifact in prepared.artifacts:
            for parent_id in artifact.lineage:
                if parent_id in prepared_ids:
                    continue
                parent = artifacts.get(parent_id)
                if not isinstance(parent, ArtifactV2):
                    raise IntegrityViolation(
                        "prepared Artifact lineage parent is unavailable",
                        artifact_id=artifact.artifact_id,
                        parent_artifact_id=parent_id,
                    )
                retained.add(parent_id)
        return tuple(sorted(retained))

    @staticmethod
    def _validate_target_binding(
        prepared: PreparedDraft,
        facts: DraftSubjectFacts,
        artifacts: ArtifactRepository,
    ) -> None:
        item = prepared.approval_item
        binding = item.target_binding
        if item.subject_kind == "constraint_proposal":
            if (
                binding is not None
                or facts.target_artifact_id is not None
                or facts.target_snapshot_id is not None
            ):
                raise IntegrityViolation("draft constraint proposal cannot have a target")
            return
        if binding is None:
            raise IntegrityViolation("draft target binding is missing")
        if item.subject_kind == "patch":
            candidates = {
                artifact.artifact_id: artifact for artifact in prepared.companion_artifacts
            }
            target = candidates.get(binding.target_artifact_id)
            if target is None:
                raise IntegrityViolation("patch target is not its prepared preview Artifact")
            if facts.target_artifact_id is not None:
                raise IntegrityViolation("Patch payload cannot bind a target Artifact ID")
            for config in (
                artifact
                for artifact in prepared.companion_artifacts
                if artifact.kind == "config_export"
            ):
                constraint_parent_ids = set(config.lineage) - {target.artifact_id}
                if len(constraint_parent_ids) != 1:
                    raise IntegrityViolation("config export must bind one exact constraint parent")
                constraint = artifacts.get(next(iter(constraint_parent_ids)))
                if (
                    not isinstance(constraint, ArtifactV2)
                    or constraint.kind != "constraint_snapshot"
                    or constraint.version_tuple.constraint_snapshot_id is None
                    or config.version_tuple.constraint_snapshot_id
                    != constraint.version_tuple.constraint_snapshot_id
                ):
                    raise IntegrityViolation(
                        "config export constraint lineage/VersionTuple differs"
                    )
        else:
            target = artifacts.get(binding.target_artifact_id)
            if not isinstance(target, ArtifactV2):
                raise IntegrityViolation("rollback target Artifact is unavailable")
            if not isinstance(binding, RollbackTargetBindingV1):
                raise IntegrityViolation("rollback target binding has another subject kind")
            request = facts.rollback_request
            if request is None:  # guarded by DraftSubjectFacts
                raise IntegrityViolation("rollback payload is unavailable")
            if (
                request.ref_name != binding.ref_name
                or request.expected_current_ref != binding.expected_ref
                or request.target_artifact_id != binding.target_artifact_id
                or request.rollback_profile_binding != binding.rollback_profile_binding
            ):
                raise IntegrityViolation("rollback request differs from exact target binding")
        if (
            target.kind != binding.target_artifact_kind
            or target.payload_hash != binding.target_digest
            or facts.target_snapshot_id != binding.target_snapshot_id
        ):
            raise IntegrityViolation("subject payload and exact target binding differ")
        if binding.target_snapshot_id is not None:
            snapshot_id = {
                "ir_snapshot": target.version_tuple.ir_snapshot_id,
                "constraint_snapshot": target.version_tuple.constraint_snapshot_id,
            }.get(target.kind)
            if snapshot_id is None or snapshot_id != binding.target_snapshot_id:
                raise IntegrityViolation(
                    "target Artifact VersionTuple differs from exact target binding"
                )
        if (
            item.subject_kind == "rollback_request"
            and facts.target_artifact_id != binding.target_artifact_id
        ):
            raise IntegrityViolation("rollback payload and target Artifact differ")

    @staticmethod
    def _next_head(prepared: PreparedDraft | PreparedTerminalDraft) -> SubjectHead:
        item = prepared.approval_item
        expected = prepared.expected_subject_head
        return SubjectHead(
            subject_series_id=item.subject_series_id,
            current_subject_artifact_id=item.subject_artifact_id,
            current_approval_id=item.approval_id,
            revision=1 if expected is None else expected.revision + 1,
        )

    @staticmethod
    def _validate_superseding_draft(
        prepared: PreparedDraft | PreparedTerminalDraft,
        facts: DraftSubjectFacts,
        old_item: ApprovalItem,
    ) -> None:
        item = prepared.approval_item
        if (
            prepared.expected_previous_workflow_revision is not None
            and old_item.workflow_revision != prepared.expected_previous_workflow_revision
        ):
            raise Conflict(
                "superseded ApprovalItem workflow revision is stale",
                expected_workflow_revision=prepared.expected_previous_workflow_revision,
                actual_workflow_revision=old_item.workflow_revision,
            )
        if (
            item.subject_kind != old_item.subject_kind
            or item.subject_revision != old_item.subject_revision + 1
            or item.supersedes_approval_id != old_item.approval_id
            or facts.supersedes_artifact_id != old_item.subject_artifact_id
        ):
            raise IntegrityViolation("superseding draft does not bind the current revision")
        validate_status_transition(
            current=old_item.status,
            target="superseded",
            subject_kind=old_item.subject_kind,
        )

    @staticmethod
    def _superseded_item(item: ApprovalItem) -> ApprovalItem:
        return _replace_item(
            item,
            status="superseded",
            workflow_revision=item.workflow_revision + 1,
            active_validation_run_id=None,
        )

    @staticmethod
    def _topological_artifacts(prepared: PreparedDraft) -> tuple[ArtifactV2, ...]:
        pending = {artifact.artifact_id: artifact for artifact in prepared.artifacts}
        ordered: list[ArtifactV2] = []
        while pending:
            ready = sorted(
                (
                    artifact
                    for artifact in pending.values()
                    if not set(artifact.lineage).intersection(pending)
                ),
                key=lambda artifact: artifact.artifact_id,
            )
            if not ready:
                raise IntegrityViolation("prepared Artifact lineage contains a cycle")
            for artifact in ready:
                ordered.append(artifact)
                del pending[artifact.artifact_id]
        return tuple(ordered)

    @staticmethod
    def _validate_start_binding(
        item: ApprovalItem,
        prepared: PreparedValidationStart,
    ) -> None:
        if (
            prepared.approval_id != item.approval_id
            or prepared.subject_artifact_id != item.subject_artifact_id
            or prepared.subject_digest != item.subject_digest
        ):
            raise IntegrityViolation("validation start does not bind the ApprovalItem")

    def _validate_submission_evidence(
        self,
        *,
        item: ApprovalItem,
        subject: ArtifactV2,
        artifacts: ArtifactRepository,
        evidence: ApprovalEvidenceGateway,
    ) -> EvidenceStateProjection:
        if item.target_binding is None or item.evidence_set_artifact_id is None:
            raise InvalidStateTransition(
                "approval subject is not validated: submit requires a validated target and "
                "EvidenceSet"
            )
        target = self._load_artifact(artifacts, item.target_binding.target_artifact_id)
        evidence_artifact = self._load_artifact(
            artifacts,
            item.evidence_set_artifact_id,
            expected_kind="validation_evidence",
        )
        regression = tuple(
            self._load_artifact(
                artifacts,
                artifact_id,
                expected_kind="regression_evidence",
            )
            for artifact_id in item.regression_evidence_artifact_ids
        )
        return evidence.validate_submission(
            item=item,
            subject_artifact=subject,
            target_artifact=target,
            evidence_artifact=evidence_artifact,
            regression_artifacts=regression,
        )

    @staticmethod
    def _load_item(
        approvals: ApprovalRepository,
        approval_id: str,
    ) -> ApprovalItem:
        item = approvals.get(approval_id)
        if item is None:
            raise Conflict("ApprovalItem does not exist", approval_id=approval_id)
        return item

    @staticmethod
    def _require_current_head(
        approvals: ApprovalRepository,
        item: ApprovalItem,
    ) -> SubjectHead:
        current = approvals.current(item.subject_series_id)
        if current is None or current[1] != item:
            raise Conflict("ApprovalItem is not the current SubjectHead")
        return current[0]

    @staticmethod
    def _load_artifact(
        artifacts: ArtifactRepository,
        artifact_id: str,
        *,
        expected_kind: str | None = None,
    ) -> ArtifactV2:
        artifact = artifacts.get(artifact_id)
        if not isinstance(artifact, ArtifactV2):
            raise IntegrityViolation("required ArtifactV2 is unavailable", artifact_id=artifact_id)
        if expected_kind is not None and artifact.kind != expected_kind:
            raise IntegrityViolation(
                "Artifact kind differs from workflow binding",
                artifact_id=artifact_id,
                expected_kind=expected_kind,
                actual_kind=artifact.kind,
            )
        return artifact

    @staticmethod
    def _resolve_policies(
        *,
        item: ApprovalItem,
        policies: GovernancePolicyRepository,
    ) -> tuple[DomainRegistryV1, DomainRoutePolicy, RolePolicy, ApprovalPolicyV1]:
        registry = policies.get_domain_registry(item.domain_registry_ref)
        route = policies.get_domain_route_policy(item.route_policy)
        role = policies.get_role_policy(
            item.role_policy_version,
            item.role_policy_digest,
        )
        approval = policies.get_approval_policy(item.approval_policy)
        missing = [
            name
            for name, value in (
                ("domain registry", registry),
                ("route policy", route),
                ("role policy", role),
                ("approval policy", approval),
            )
            if value is None
        ]
        if missing:
            raise IntegrityViolation(
                "exact retained governance policy is unavailable",
                missing=missing,
            )
        assert registry is not None
        assert route is not None
        assert role is not None
        assert approval is not None
        return registry, route, role, approval

    @classmethod
    def _validate_bound_policies(
        cls,
        item: ApprovalItem,
        policies: GovernancePolicyRepository,
    ) -> None:
        registry, route, role, approval = cls._resolve_policies(
            item=item,
            policies=policies,
        )
        validate_approval_policy_bindings(
            item=item,
            domain_registry=registry,
            route_policy=route,
            role_policy=role,
            approval_policy=approval,
        )

    @staticmethod
    def _get_idempotent(
        repository: IdempotencyRepository,
        context: ApprovalCommandContext,
        *,
        operation: str,
    ) -> dict[str, Any] | None:
        return repository.get_result(
            scope=context.idempotency_scope,
            operation=operation,
            key=context.idempotency_key,
            request_hash=context.request_hash,
        )

    @staticmethod
    def _put_idempotent(
        repository: IdempotencyRepository,
        context: ApprovalCommandContext,
        *,
        operation: str,
        item: ApprovalItem,
        response: Mapping[str, Any],
    ) -> None:
        stored = repository.put_result(
            scope=context.idempotency_scope,
            operation=operation,
            key=context.idempotency_key,
            request_hash=context.request_hash,
            resource_kind="approval",
            resource_id=item.approval_id,
            response=response,
        )
        if dict(stored) != dict(response):
            raise IntegrityViolation("idempotency repository stored another response")

    @staticmethod
    def _put_preflighted_idempotent(
        repository: IdempotencyRepository,
        context: ApprovalCommandContext,
        *,
        operation: str,
        item: ApprovalItem,
        response: Mapping[str, Any],
    ) -> None:
        stored = repository.put_preflighted_result(
            scope=context.idempotency_scope,
            operation=operation,
            key=context.idempotency_key,
            request_hash=context.request_hash,
            resource_kind="approval",
            resource_id=item.approval_id,
            response=response,
        )
        if dict(stored) != dict(response):
            raise IntegrityViolation("idempotency repository stored another response")

    @staticmethod
    def _draft_publication_response(
        response: Mapping[str, Any],
    ) -> DraftPublicationResult:
        try:
            return DraftPublicationResult.model_validate(response)
        except ValidationError as exc:
            raise IntegrityViolation("draft idempotency response is malformed") from exc

    @staticmethod
    def _item_response(response: Mapping[str, Any]) -> ApprovalItem:
        value = response.get("approval_item")
        if value is None:
            raise IntegrityViolation("idempotency response lacks approval_item")
        try:
            return ApprovalItem.model_validate(value)
        except ValidationError as exc:
            raise IntegrityViolation("approval idempotency response is malformed") from exc

    @staticmethod
    def _stable_subject_identity(item: ApprovalItem) -> tuple[object, ...]:
        return (
            item.approval_id,
            item.subject_series_id,
            item.subject_revision,
            item.subject_kind,
            item.subject_artifact_id,
            item.subject_digest,
            item.supersedes_approval_id,
            item.proposer,
            item.domain_scope,
            item.domain_registry_ref,
            item.route_policy,
            item.role_policy_version,
            item.role_policy_digest,
            item.approval_policy,
            item.requirements,
            item.created_at,
        )

    @classmethod
    def _require_same_subject_identity(
        cls,
        retained: ApprovalItem,
        current: ApprovalItem,
        *,
        operation: str,
    ) -> None:
        if cls._stable_subject_identity(retained) != cls._stable_subject_identity(current):
            raise IntegrityViolation(
                f"{operation} idempotency response binds another subject revision"
            )

    @classmethod
    def _validation_start_response(
        cls,
        response: Mapping[str, Any],
        *,
        prepared: PreparedValidationStart,
        current: ApprovalItem,
    ) -> ValidationStartResult:
        try:
            result = ValidationStartResult.model_validate(response)
        except ValidationError as exc:
            raise IntegrityViolation("validation start idempotency response is malformed") from exc
        retained = result.approval_item
        cls._require_same_subject_identity(
            retained,
            current,
            operation="validation start",
        )
        if (
            result.run_id != prepared.run_id
            or retained.approval_id != prepared.approval_id
            or retained.subject_artifact_id != prepared.subject_artifact_id
            or retained.subject_digest != prepared.subject_digest
            or retained.workflow_revision != prepared.expected_workflow_revision + 1
            or retained.status != "validating"
            or retained.active_validation_run_id != prepared.run_id
        ):
            raise IntegrityViolation(
                "validation start idempotency response differs from the command"
            )
        return result

    @classmethod
    def _approval_item_response(
        cls,
        response: Mapping[str, Any],
        *,
        operation: str,
        approval_id: str,
        expected_workflow_revision: int,
        expected_statuses: frozenset[str],
        current: ApprovalItem,
    ) -> ApprovalItem:
        retained = cls._item_response(response)
        cls._require_same_subject_identity(
            retained,
            current,
            operation=operation,
        )
        if (
            retained.approval_id != approval_id
            or retained.workflow_revision != expected_workflow_revision + 1
            or retained.status not in expected_statuses
        ):
            raise IntegrityViolation(f"{operation} idempotency response differs from the command")
        return retained

    @classmethod
    def _decision_response(
        cls,
        response: Mapping[str, Any],
        *,
        approval_id: str,
        decision: ApprovalDecision,
        current: ApprovalItem,
    ) -> ApprovalItem:
        expected_statuses = {
            "approve": frozenset({"pending_approval", "approved"}),
            "reject": frozenset({"rejected"}),
            "request_changes": frozenset({"changes_requested"}),
        }[decision.decision]
        retained = cls._approval_item_response(
            response,
            operation="decision",
            approval_id=approval_id,
            expected_workflow_revision=decision.expected_workflow_revision,
            expected_statuses=expected_statuses,
            current=current,
        )
        if decision not in retained.decisions:
            raise IntegrityViolation(
                "decision idempotency response does not contain the exact decision"
            )
        return retained

    @classmethod
    def _decision_request_response(
        cls,
        response: Mapping[str, Any],
        *,
        approval_id: str,
        request: ApprovalDecisionRequest,
        actor: AuditActor,
        current: ApprovalItem,
    ) -> ApprovalItem:
        expected_statuses = {
            "approve": frozenset({"pending_approval", "approved"}),
            "reject": frozenset({"rejected"}),
            "request_changes": frozenset({"changes_requested"}),
        }[request.decision]
        retained = cls._approval_item_response(
            response,
            operation="decision",
            approval_id=approval_id,
            expected_workflow_revision=request.expected_workflow_revision,
            expected_statuses=expected_statuses,
            current=current,
        )
        matching = tuple(
            decision
            for decision in retained.decisions
            if (
                decision.requirement_ids == request.requirement_ids
                and decision.decision == request.decision
                and decision.actor == actor
                and decision.expected_workflow_revision == request.expected_workflow_revision
                and decision.reason_code == request.reason_code
                and decision.comment == request.comment
            )
        )
        if len(matching) != 1:
            raise IntegrityViolation(
                "decision idempotency response does not contain the requested decision"
            )
        return retained

    def _audit(
        self,
        audit: ApprovalAuditWriter,
        context: ApprovalCommandContext,
        *,
        action: str,
        item: ApprovalItem,
    ) -> None:
        audit.append(
            chain_id=self._audit_chain_id,
            actor=context.actor,
            initiated_by=context.initiated_by,
            action=action,
            subject=AuditSubject(
                resource_kind="approval",
                resource_id=item.approval_id,
                artifact_id=item.subject_artifact_id,
            ),
            correlation=AuditCorrelation(
                request_id=context.request_id,
                run_id=context.run_id,
                trace_id=context.trace_id,
            ),
        )

    @staticmethod
    def _audit_intent(
        context: ApprovalCommandContext,
        *,
        action: str,
        item: ApprovalItem,
    ) -> AuditAppendIntent:
        return AuditAppendIntent(
            actor=context.actor,
            initiated_by=context.initiated_by,
            action=action,
            subject=AuditSubject(
                resource_kind="approval",
                resource_id=item.approval_id,
                artifact_id=item.subject_artifact_id,
            ),
            correlation=AuditCorrelation(
                request_id=context.request_id,
                run_id=context.run_id,
                trace_id=context.trace_id,
            ),
        )


__all__ = [
    "ApprovalCommandCapabilities",
    "ApprovalCommandContext",
    "ApprovalCommandService",
    "ApprovalDecisionPrincipalRepository",
    "ApprovalDecisionRequest",
    "ApprovalEvidenceGateway",
    "ApprovalRunGateway",
    "DraftLineageVerifier",
    "DraftPublicationResult",
    "DraftSubjectFacts",
    "EvidenceStateProjection",
    "PreparedDraft",
    "PreparedObjectBinding",
    "PreparedTerminalDraft",
    "PreflightedTerminalDraft",
    "TerminalDraftAuditIntents",
    "PreparedValidationStart",
    "RefReader",
    "require_human_constraint_revision",
    "SubjectPayloadGateway",
    "ValidationStartResult",
]
