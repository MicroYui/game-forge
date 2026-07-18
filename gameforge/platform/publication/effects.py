"""Trusted workflow-effect registry for terminal publication.

``OutcomeArtifactPolicyV1.workflow_effect_key`` is resolved **only** through this
composition-root map into a registered handler — never a client callable or import
path (M4 design line 1116).  Unknown keys fail closed.

The handlers registered here fall into three families:

* the ``no_workflow_change`` / ``no_workflow_subject`` family that leaves
  SubjectHead / ApprovalItem state untouched (Review/Checker/Simulation/Playtest/
  TaskSuite success and every non-success close), plus the evidence-only reject and
  terminal-only failure effects; and
* the transaction-bound asynchronous draft effects, delegated to the same Task-7
  Approval command authority that builds governance bindings and performs
  SubjectHead CAS; and
* the **validation-completion** effects (M4c Task 17b) — ``set_patch_validated@1``
  and friends, plus the ``validating→draft`` revert ``restore_current_draft@1``.
  Per §5.5 (spec line 117: ONE UoW — "插 immutable EvidenceSet … 再 CAS validated"),
  these run INSIDE ``TerminalPublisher``'s terminal UoW: the publisher already
  published the EvidenceSet + booked the Run terminal + wrote the terminal audit;
  the effect does ONLY the ApprovalItem compare-and-set, reusing the single
  validation-completion core in ``platform.approvals.validation`` (no duplicated
  ``_validate_evidence`` logic) against the just-published EvidenceSet payload.

There are no string/deferred effect sentinels in the executable worker registry:
every active ``workflow_effect_key`` resolves to a callable, and any missing
transaction-bound authority port fails the enclosing terminal UoW closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from threading import Lock
from types import MappingProxyType
from typing import Literal, Protocol
from weakref import WeakKeyDictionary, WeakSet

from gameforge.contracts.canonical import canonical_json, canonical_sha256, sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.jobs import (
    ConstraintProposalProposePayloadV1,
    ConstraintValidationPayloadV1,
    GenerationProposePayloadV1,
    OutcomeArtifactPolicyV1,
    PatchRepairPayloadV1,
    PatchValidationPayloadV1,
    RollbackValidationPayloadV1,
    RunRecord,
)
from gameforge.contracts.lineage import ArtifactV2, AuditActor
from gameforge.contracts.workflow import (
    ApprovalItem,
    AutoApplyProofBindingV1,
    AutoApplyProofV1,
    ConstraintProposalV1,
    EvidenceSet,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.approvals.commands import (
    ApprovalCommandCapabilities,
    ApprovalCommandContext,
    ApprovalCommandService,
    DraftPublicationResult,
    PreparedDraft,
    PreflightedTerminalDraft,
    PreparedTerminalDraft,
    TerminalDraftAuditIntents,
)
from gameforge.platform.approvals.auto_apply_runtime import PreparedAutoApplyEligibility
from gameforge.platform.approvals.state import ValidationResetReason, validate_status_transition
from gameforge.platform.approvals.validation import (
    ValidationCompletionApprovalRepository,
    build_validation_completion_replacement,
    build_auto_apply_proof_binding,
    build_validation_revert_replacement,
    payload_subject_kind,
    regression_evidence_ids_from_set,
    validate_constraint_evidence_candidate_binding,
    validate_current_subject_binding,
    validate_evidence_subject,
    validate_immutable_subject_binding,
    validate_patch_evidence_binding,
    validate_rollback_evidence_binding,
    validate_strict_superseded_subject_binding,
)
from gameforge.platform.terminal_staging import deep_freeze_value


_ValidationPayload = (
    PatchValidationPayloadV1 | ConstraintValidationPayloadV1 | RollbackValidationPayloadV1
)

AgentDraftEffectKey = Literal[
    "create_patch_subject_head_and_draft@1",
    "supersede_patch_head_create_draft@1",
    "create_constraint_subject_head_and_draft@1",
]


@dataclass(frozen=True, slots=True)
class AgentDraftWorkflowRequest:
    """Exact, already-published inputs handed to the Task-7 draft authority.

    The port is transaction-bound by construction: the lifecycle binder creates it
    from the same UnitOfWork capabilities as ``TerminalPublisher``.  Implementations
    must use the shared ``ApprovalCommandService`` draft authority core, including
    governance resolution, SubjectHead CAS, supersede/cancel semantics and audit;
    this request deliberately carries no client-authored ApprovalItem.
    """

    effect_key: AgentDraftEffectKey
    run: RunRecord
    policy: OutcomeArtifactPolicyV1
    initiated_by: AuditActor
    executed_by: AuditActor
    subject_artifact_id: str
    artifacts_by_rule: Mapping[str, tuple[ArtifactV2, ...]]
    artifact_ids_by_rule: Mapping[str, tuple[str, ...]]
    payloads_by_rule: Mapping[str, tuple[Mapping[str, object], ...]]
    expected_subject_head_revision: int | None
    expected_workflow_revision: int | None
    expected_current_approval: ApprovalItem | None
    expected_current_subject_head: SubjectHead | None
    occurred_at: str


class AgentDraftWorkflowPort(Protocol):
    def publish_agent_draft(self, request: AgentDraftWorkflowRequest) -> DraftPublicationResult: ...

    def prepare_agent_draft(self, request: AgentDraftWorkflowRequest) -> PreparedTerminalDraft: ...

    def commit_prepared_agent_draft(
        self,
        *,
        prepared: PreparedTerminalDraft,
        request: AgentDraftWorkflowRequest,
    ) -> DraftPublicationResult: ...

    def preflight_prepared_agent_draft(
        self,
        *,
        prepared: PreparedTerminalDraft,
        request: AgentDraftWorkflowRequest,
        merge_audit_into_terminal_batch: bool = False,
    ) -> PreflightedTerminalDraft: ...

    def apply_preflighted_agent_draft(
        self,
        *,
        preflighted: PreflightedTerminalDraft,
        request: AgentDraftWorkflowRequest,
    ) -> DraftPublicationResult: ...

    def preflighted_agent_draft_audit_intents(
        self,
        *,
        preflighted: PreflightedTerminalDraft,
        request: AgentDraftWorkflowRequest,
    ) -> TerminalDraftAuditIntents: ...


class AgentDraftPreparedAssembler(Protocol):
    """Build a complete ``PreparedDraft`` from final retained Artifact authority."""

    def prepare(self, request: AgentDraftWorkflowRequest) -> PreparedDraft: ...

    def prepare_terminal(self, request: AgentDraftWorkflowRequest) -> PreparedTerminalDraft: ...


@dataclass(frozen=True, slots=True)
class ApprovalCommandAgentDraftWorkflowPort:
    """Transaction-bound adapter into the canonical Task-7 draft command core.

    Task 10 supplies the governance-aware assembler and capabilities bound to the
    terminal UoW.  This adapter owns the non-client idempotency/audit context and
    invokes :class:`ApprovalCommandService`'s in-transaction entry point; it never
    implements ApprovalItem/SubjectHead transitions itself.
    """

    commands: ApprovalCommandService
    capabilities: ApprovalCommandCapabilities
    assembler: AgentDraftPreparedAssembler

    def publish_agent_draft(self, request: AgentDraftWorkflowRequest) -> DraftPublicationResult:
        """Compatibility path for synchronous/pre-Task-9 callers."""

        self._validate_request(request)
        prepared = self.assembler.prepare(request)
        self._validate_prepared(request, prepared)
        return self.commands.publish_draft_in_transaction(
            prepared=prepared,
            context=self._context(request),
            capabilities=self.capabilities,
        )

    def prepare_agent_draft(self, request: AgentDraftWorkflowRequest) -> PreparedTerminalDraft:
        self._validate_request(request)
        prepared = self.assembler.prepare_terminal(request)
        self._validate_prepared(request, prepared)
        return prepared

    def commit_prepared_agent_draft(
        self,
        *,
        prepared: PreparedTerminalDraft,
        request: AgentDraftWorkflowRequest,
    ) -> DraftPublicationResult:
        preflighted = self.preflight_prepared_agent_draft(
            prepared=prepared,
            request=request,
        )
        return self.apply_preflighted_agent_draft(
            preflighted=preflighted,
            request=request,
        )

    def preflight_prepared_agent_draft(
        self,
        *,
        prepared: PreparedTerminalDraft,
        request: AgentDraftWorkflowRequest,
        merge_audit_into_terminal_batch: bool = False,
    ) -> PreflightedTerminalDraft:
        self._validate_request(request)
        self._validate_prepared(request, prepared)
        return self.commands.preflight_prepared_terminal_draft_in_transaction(
            prepared=prepared,
            context=self._context(request),
            capabilities=self.capabilities,
            merge_audit_into_terminal_batch=merge_audit_into_terminal_batch,
        )

    def preflighted_agent_draft_audit_intents(
        self,
        *,
        preflighted: PreflightedTerminalDraft,
        request: AgentDraftWorkflowRequest,
    ) -> TerminalDraftAuditIntents:
        return preflighted.audit_intents_for_terminal_merge(
            context=self._context(request),
            capabilities=self.capabilities,
        )

    def apply_preflighted_agent_draft(
        self,
        *,
        preflighted: PreflightedTerminalDraft,
        request: AgentDraftWorkflowRequest,
    ) -> DraftPublicationResult:
        return self.commands.apply_preflighted_terminal_draft_in_transaction(
            preflighted=preflighted,
            context=self._context(request),
            capabilities=self.capabilities,
        )

    @staticmethod
    def _validate_request(request: AgentDraftWorkflowRequest) -> None:
        if request.initiated_by != request.run.initiated_by:
            raise IntegrityViolation("agent draft initiator differs from its immutable Run")
        if request.executed_by.principal_kind not in {"service", "system"}:
            raise IntegrityViolation("agent draft terminal publisher must be service/system")

    @staticmethod
    def _validate_prepared(
        request: AgentDraftWorkflowRequest,
        prepared: PreparedDraft | PreparedTerminalDraft,
    ) -> None:
        primary = request.artifacts_by_rule.get("primary", ())
        if len(primary) != 1 or prepared.subject_artifact != primary[0]:
            raise IntegrityViolation(
                "agent draft assembler did not preserve the final subject Artifact"
            )
        expected_companions = tuple(
            sorted(
                (
                    *request.artifacts_by_rule.get("preview", ()),
                    *request.artifacts_by_rule.get("config-export", ()),
                ),
                key=lambda artifact: artifact.artifact_id,
            )
        )
        if prepared.companion_artifacts != expected_companions:
            raise IntegrityViolation(
                "agent draft assembler did not preserve the final preview/config Artifacts"
            )
        current_item = request.expected_current_approval
        current_head = request.expected_current_subject_head
        if (current_item is None) != (current_head is None):
            raise IntegrityViolation("agent draft current ApprovalItem/SubjectHead is partial")
        if current_item is None:
            if (
                request.expected_subject_head_revision is not None
                or request.expected_workflow_revision is not None
                or prepared.expected_subject_head is not None
                or prepared.expected_previous_workflow_revision is not None
            ):
                raise IntegrityViolation("initial agent draft carries repair CAS authority")
        elif (
            prepared.expected_subject_head != current_head
            or prepared.expected_previous_workflow_revision != current_item.workflow_revision
            or request.expected_subject_head_revision != current_head.revision
            or request.expected_workflow_revision != current_item.workflow_revision
            or prepared.approval_item.subject_series_id != current_item.subject_series_id
            or prepared.approval_item.domain_scope != current_item.domain_scope
            or prepared.approval_item.supersedes_approval_id != current_item.approval_id
        ):
            raise IntegrityViolation(
                "repair draft assembler differs from current workflow/domain CAS authority"
            )
        if prepared.approval_item.proposer != request.initiated_by:
            raise IntegrityViolation("agent draft proposer differs from immutable Run initiator")

    @staticmethod
    def _context(request: AgentDraftWorkflowRequest) -> ApprovalCommandContext:
        request_payload = {
            "effect_key": request.effect_key,
            "run_id": request.run.run_id,
            "policy_id": request.policy.policy_id,
            "policy_version": request.policy.policy_version,
            "subject_artifact_id": request.subject_artifact_id,
            "artifact_ids_by_rule": {
                key: list(values) for key, values in sorted(request.artifact_ids_by_rule.items())
            },
            "expected_subject_head_revision": request.expected_subject_head_revision,
            "expected_workflow_revision": request.expected_workflow_revision,
        }
        identity = sha256_lowerhex(canonical_json(request_payload).encode("utf-8"))
        return ApprovalCommandContext(
            actor=request.executed_by,
            initiated_by=request.initiated_by,
            request_id=f"terminal-workflow:{request.run.run_id}",
            run_id=request.run.run_id,
            idempotency_scope=f"run:{request.run.run_id}",
            idempotency_key=f"{request.effect_key}:{identity}",
            request_hash=identity,
        )


@dataclass(frozen=True, slots=True)
class AutoApplyValidationRequest:
    """Same-UoW proof inputs for the deterministic auto-apply completion guard."""

    run: RunRecord
    policy: OutcomeArtifactPolicyV1
    current_item: ApprovalItem
    projected_item: ApprovalItem
    evidence: EvidenceSet
    evidence_artifact: ArtifactV2
    evidence_artifact_id: str
    proof: AutoApplyProofV1
    proof_artifact: ArtifactV2
    proof_artifact_id: str
    regression_artifacts: tuple[ArtifactV2, ...]
    regression_artifact_ids: tuple[str, ...]
    occurred_at: str
    regression_payloads: tuple[Mapping[str, object], ...] = ()


class AutoApplyValidationPort(Protocol):
    def validate_completion(self, request: AutoApplyValidationRequest) -> None: ...

    def prepare_completion(
        self, request: AutoApplyValidationRequest
    ) -> PreparedAutoApplyEligibility: ...

    def commit_prepared_completion(
        self,
        *,
        prepared: PreparedAutoApplyEligibility,
        request: AutoApplyValidationRequest,
    ) -> None: ...


# The run-final validation failure outcome codes that revert ``validating→draft``.
# Every terminal that leaves the ApprovalItem stranded in ``validating`` maps to a
# reset reason ``validate_status_transition`` accepts; dependency/quota/lease/
# integrity terminals fold into ``execution_failed`` per spec §"validating→draft"
# ("仅执行/依赖失败、用户取消或超时"). ``subject_superseded`` is absent: a superseded
# subject is caught by the current-head guard and never reverted.
_REVERT_RESET_REASON: Mapping[str, ValidationResetReason] = MappingProxyType(
    {
        "execution_failed": "execution_failed",
        "cancelled": "cancelled",
        "timed_out": "timed_out",
        "queue_timed_out": "timed_out",
        "lease_expired": "execution_failed",
        "dependency_unavailable": "execution_failed",
        "permanent_dependency_failed": "execution_failed",
        "quota_exceeded": "execution_failed",
        "integrity_violation": "execution_failed",
    }
)


@dataclass(frozen=True, slots=True)
class WorkflowEffectContext:
    """Everything a workflow effect may read/write inside the terminal UoW."""

    run: RunRecord
    policy: OutcomeArtifactPolicyV1
    scope: str
    published_primary_artifact_id: str | None
    published_output_artifact_ids: tuple[str, ...]
    approvals: ValidationCompletionApprovalRepository | None
    actor: AuditActor
    occurred_at: str
    published_primary_payload: Mapping[str, object] | None = None
    published_artifact_ids_by_rule: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    published_payloads_by_rule: Mapping[str, tuple[Mapping[str, object], ...]] = field(
        default_factory=dict
    )
    published_artifacts_by_rule: Mapping[str, tuple[ArtifactV2, ...]] = field(default_factory=dict)
    agent_drafts: AgentDraftWorkflowPort | None = None
    auto_apply: AutoApplyValidationPort | None = None


@dataclass(frozen=True, slots=True)
class _PreparedNoWorkflowMutation:
    effect_kind: Literal["no_workflow_mutation"] = "no_workflow_mutation"


@dataclass(frozen=True, slots=True)
class _PreparedAgentDraftMutation:
    request: AgentDraftWorkflowRequest
    draft: PreparedTerminalDraft
    effect_kind: Literal["agent_draft"] = "agent_draft"


@dataclass(frozen=True, slots=True)
class _PreparedValidationMutation:
    expected_item: ApprovalItem
    expected_head: SubjectHead
    replacement: ApprovalItem
    auto_apply_request: AutoApplyValidationRequest | None
    auto_apply_preparation: PreparedAutoApplyEligibility | None
    effect_kind: Literal["validation_completion"] = "validation_completion"


@dataclass(frozen=True, slots=True)
class _PreparedValidationRevertMutation:
    expected_item: ApprovalItem
    expected_head: SubjectHead
    replacement: ApprovalItem
    effect_kind: Literal["validation_revert"] = "validation_revert"


@dataclass(frozen=True, slots=True)
class _PreparedSupersededValidationNoop:
    expected_item: ApprovalItem
    expected_head: SubjectHead
    effect_kind: Literal["superseded_validation_noop"] = "superseded_validation_noop"


_PreparedWorkflowPayload = (
    _PreparedNoWorkflowMutation
    | _PreparedAgentDraftMutation
    | _PreparedValidationMutation
    | _PreparedValidationRevertMutation
    | _PreparedSupersededValidationNoop
)
_PREPARED_WORKFLOW_SEAL = object()


@dataclass(frozen=True, slots=True)
class _PreparedWorkflowState:
    effect_key: str
    run_id: str
    context_digest: str
    context_selector: Mapping[str, object]
    preparation_digest: str
    payload: _PreparedWorkflowPayload
    projection: Mapping[str, object]


class PreparedWorkflowEffect:
    """Opaque, immutable read-phase workflow plan.

    Construction is restricted to :func:`prepare_workflow_effect`; the module
    seal is intentionally absent from canonical data, while the complete context
    and payload projections are digest-bound. Copy operations deliberately return
    an unregistered data-free handle; terminal staging keeps the original handle
    only through its private operation-seal path.
    """

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        *,
        effect_key: str,
        run_id: str,
        context_digest: str,
        context_selector: Mapping[str, object],
        payload: _PreparedWorkflowPayload,
        projection: Mapping[str, object],
        preparation_digest: str,
        _seal: object,
    ) -> None:
        if _seal is not _PREPARED_WORKFLOW_SEAL:
            raise TypeError("PreparedWorkflowEffect is issued only by the trusted planner")
        if not effect_key or not run_id:
            raise ValueError("prepared workflow identity must be complete")
        if len(preparation_digest) != 64 or any(
            character not in "0123456789abcdef" for character in preparation_digest
        ):
            raise ValueError("prepared workflow digest is not canonical")
        state = _PreparedWorkflowState(
            effect_key=effect_key,
            run_id=run_id,
            context_digest=context_digest,
            context_selector=context_selector,
            preparation_digest=preparation_digest,
            payload=payload,
            projection=projection,
        )
        with _PREPARED_WORKFLOW_LOCK:
            _PREPARED_WORKFLOW_STATES[self] = state

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("prepared workflow effect is immutable")

    def __delattr__(self, _name: str) -> None:
        raise TypeError("prepared workflow effect is immutable")

    @classmethod
    def _issue(
        cls,
        *,
        effect_key: str,
        context_digest: str,
        context_selector: Mapping[str, object],
        run_id: str,
        payload: _PreparedWorkflowPayload,
    ) -> PreparedWorkflowEffect:
        frozen_payload = _freeze_prepared_payload(payload)
        frozen_selector = deep_freeze_value(context_selector)
        if not isinstance(frozen_selector, Mapping):  # pragma: no cover - fixed projection
            raise IntegrityViolation("prepared workflow selector is not canonical")
        projection = deep_freeze_value(
            {
                "prepared_schema_version": "prepared-workflow-effect@1",
                "effect_key": effect_key,
                "run_id": run_id,
                "context_digest": context_digest,
                "context_selector": frozen_selector,
                "payload": _prepared_payload_projection(frozen_payload),
            }
        )
        if not isinstance(projection, Mapping):  # pragma: no cover - fixed projection
            raise IntegrityViolation("prepared workflow projection is not canonical")
        return cls(
            effect_key=effect_key,
            run_id=run_id,
            context_digest=context_digest,
            context_selector=frozen_selector,
            payload=frozen_payload,
            projection=projection,
            preparation_digest=canonical_sha256(projection),
            _seal=_PREPARED_WORKFLOW_SEAL,
        )

    def require_trusted(self, *, verify_projection: bool = False) -> None:
        state = self._state()
        if state is None:
            raise IntegrityViolation("prepared workflow effect has no trusted planner seal")
        if verify_projection and state.preparation_digest != canonical_sha256(state.projection):
            raise IntegrityViolation("prepared workflow effect projection changed")

    def canonical_projection(self) -> Mapping[str, object]:
        self.require_trusted(verify_projection=True)
        state = self._require_state()
        projection = deep_freeze_value(state.projection)
        if not isinstance(projection, Mapping):  # pragma: no cover - fixed projection
            raise IntegrityViolation("prepared workflow projection changed type")
        return projection

    @property
    def effect_key(self) -> str:
        return self._require_state().effect_key

    @property
    def run_id(self) -> str:
        return self._require_state().run_id

    @property
    def context_digest(self) -> str:
        return self._require_state().context_digest

    @property
    def context_selector(self) -> Mapping[str, object]:
        selector = deep_freeze_value(self._require_state().context_selector)
        if not isinstance(selector, Mapping):  # pragma: no cover - fixed projection
            raise IntegrityViolation("prepared workflow selector changed type")
        return selector

    @property
    def preparation_digest(self) -> str:
        return self._require_state().preparation_digest

    def detached_payload(self) -> _PreparedWorkflowPayload:
        """Return an isolated payload projection for transaction preflight."""

        return _freeze_prepared_payload(self._require_state().payload)

    def _state(self) -> _PreparedWorkflowState | None:
        with _PREPARED_WORKFLOW_LOCK:
            return _PREPARED_WORKFLOW_STATES.get(self)

    def _require_state(self) -> _PreparedWorkflowState:
        state = self._state()
        if state is None:
            raise IntegrityViolation("prepared workflow effect has no trusted planner seal")
        return state

    def __copy__(self) -> PreparedWorkflowEffect:
        return object.__new__(type(self))

    def __deepcopy__(self, _memo: dict[int, object]) -> PreparedWorkflowEffect:
        return object.__new__(type(self))


_PREPARED_WORKFLOW_LOCK = Lock()
_PREPARED_WORKFLOW_STATES: WeakKeyDictionary[
    PreparedWorkflowEffect,
    _PreparedWorkflowState,
] = WeakKeyDictionary()


def _frozen_model[T](value: T) -> T:
    return deep_freeze_value(value)  # type: ignore[return-value]


def _freeze_agent_request(request: AgentDraftWorkflowRequest) -> AgentDraftWorkflowRequest:
    return AgentDraftWorkflowRequest(
        effect_key=request.effect_key,
        run=_frozen_model(request.run),
        policy=_frozen_model(request.policy),
        initiated_by=_frozen_model(request.initiated_by),
        executed_by=_frozen_model(request.executed_by),
        subject_artifact_id=request.subject_artifact_id,
        artifacts_by_rule=MappingProxyType(
            {
                key: tuple(_frozen_model(artifact) for artifact in values)
                for key, values in request.artifacts_by_rule.items()
            }
        ),
        artifact_ids_by_rule=MappingProxyType(
            {key: tuple(values) for key, values in request.artifact_ids_by_rule.items()}
        ),
        payloads_by_rule=MappingProxyType(
            {
                key: tuple(_frozen_model(payload) for payload in values)
                for key, values in request.payloads_by_rule.items()
            }
        ),
        expected_subject_head_revision=request.expected_subject_head_revision,
        expected_workflow_revision=request.expected_workflow_revision,
        expected_current_approval=(
            None
            if request.expected_current_approval is None
            else _frozen_model(request.expected_current_approval)
        ),
        expected_current_subject_head=(
            None
            if request.expected_current_subject_head is None
            else _frozen_model(request.expected_current_subject_head)
        ),
        occurred_at=request.occurred_at,
    )


def _freeze_auto_apply_request(
    request: AutoApplyValidationRequest,
) -> AutoApplyValidationRequest:
    return AutoApplyValidationRequest(
        run=_frozen_model(request.run),
        policy=_frozen_model(request.policy),
        current_item=_frozen_model(request.current_item),
        projected_item=_frozen_model(request.projected_item),
        evidence=_frozen_model(request.evidence),
        evidence_artifact=_frozen_model(request.evidence_artifact),
        evidence_artifact_id=request.evidence_artifact_id,
        proof=_frozen_model(request.proof),
        proof_artifact=_frozen_model(request.proof_artifact),
        proof_artifact_id=request.proof_artifact_id,
        regression_artifacts=tuple(
            _frozen_model(artifact) for artifact in request.regression_artifacts
        ),
        regression_artifact_ids=request.regression_artifact_ids,
        occurred_at=request.occurred_at,
        regression_payloads=tuple(
            _frozen_model(payload) for payload in request.regression_payloads
        ),
    )


def _freeze_prepared_payload(
    payload: _PreparedWorkflowPayload,
) -> _PreparedWorkflowPayload:
    if isinstance(payload, _PreparedNoWorkflowMutation):
        return payload
    if isinstance(payload, _PreparedAgentDraftMutation):
        return _PreparedAgentDraftMutation(
            request=_freeze_agent_request(payload.request),
            draft=_frozen_model(payload.draft),
        )
    if isinstance(payload, _PreparedValidationMutation):
        return _PreparedValidationMutation(
            expected_item=_frozen_model(payload.expected_item),
            expected_head=_frozen_model(payload.expected_head),
            replacement=_frozen_model(payload.replacement),
            auto_apply_request=(
                None
                if payload.auto_apply_request is None
                else _freeze_auto_apply_request(payload.auto_apply_request)
            ),
            auto_apply_preparation=(
                None
                if payload.auto_apply_preparation is None
                else _frozen_model(payload.auto_apply_preparation)
            ),
        )
    if isinstance(payload, _PreparedValidationRevertMutation):
        return _PreparedValidationRevertMutation(
            expected_item=_frozen_model(payload.expected_item),
            expected_head=_frozen_model(payload.expected_head),
            replacement=_frozen_model(payload.replacement),
        )
    if isinstance(payload, _PreparedSupersededValidationNoop):
        return _PreparedSupersededValidationNoop(
            expected_item=_frozen_model(payload.expected_item),
            expected_head=_frozen_model(payload.expected_head),
        )
    raise IntegrityViolation("workflow preparation produced an unknown payload")


def _project_value(value: object) -> object:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _project_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_project_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise IntegrityViolation(
        "workflow preparation contains a non-canonical value",
        value_type=type(value).__qualname__,
    )


def _agent_request_projection(request: AgentDraftWorkflowRequest) -> Mapping[str, object]:
    return {
        "effect_key": request.effect_key,
        "run": request.run.model_dump(mode="json"),
        "policy": request.policy.model_dump(mode="json"),
        "initiated_by": request.initiated_by.model_dump(mode="json"),
        "executed_by": request.executed_by.model_dump(mode="json"),
        "subject_artifact_id": request.subject_artifact_id,
        "artifacts_by_rule": _project_value(request.artifacts_by_rule),
        "artifact_ids_by_rule": _project_value(request.artifact_ids_by_rule),
        "payloads_by_rule": _project_value(request.payloads_by_rule),
        "expected_subject_head_revision": request.expected_subject_head_revision,
        "expected_workflow_revision": request.expected_workflow_revision,
        "expected_current_approval": _project_value(request.expected_current_approval),
        "expected_current_subject_head": _project_value(request.expected_current_subject_head),
        "occurred_at": request.occurred_at,
    }


def _prepared_payload_projection(payload: _PreparedWorkflowPayload) -> Mapping[str, object]:
    if isinstance(payload, _PreparedNoWorkflowMutation):
        return {"effect_kind": payload.effect_kind}
    if isinstance(payload, _PreparedAgentDraftMutation):
        return {
            "effect_kind": payload.effect_kind,
            "request": _agent_request_projection(payload.request),
            "draft": payload.draft.model_dump(mode="json"),
        }
    if isinstance(payload, _PreparedValidationMutation):
        return {
            "effect_kind": payload.effect_kind,
            "expected_item": payload.expected_item.model_dump(mode="json"),
            "expected_head": payload.expected_head.model_dump(mode="json"),
            "replacement": payload.replacement.model_dump(mode="json"),
            "auto_apply_request": (
                None
                if payload.auto_apply_request is None
                else {
                    field_name: _project_value(getattr(payload.auto_apply_request, field_name))
                    for field_name in payload.auto_apply_request.__dataclass_fields__
                }
            ),
            "auto_apply_preparation": _project_value(payload.auto_apply_preparation),
        }
    if isinstance(payload, _PreparedValidationRevertMutation):
        return {
            "effect_kind": payload.effect_kind,
            "expected_item": payload.expected_item.model_dump(mode="json"),
            "expected_head": payload.expected_head.model_dump(mode="json"),
            "replacement": payload.replacement.model_dump(mode="json"),
        }
    if isinstance(payload, _PreparedSupersededValidationNoop):
        return {
            "effect_kind": payload.effect_kind,
            "expected_item": payload.expected_item.model_dump(mode="json"),
            "expected_head": payload.expected_head.model_dump(mode="json"),
        }
    raise IntegrityViolation("unknown prepared workflow payload")


def _workflow_context_projection(context: WorkflowEffectContext) -> Mapping[str, object]:
    return {
        "run": context.run.model_dump(mode="json"),
        "policy": context.policy.model_dump(mode="json"),
        "scope": context.scope,
        "published_primary_artifact_id": context.published_primary_artifact_id,
        "published_output_artifact_ids": context.published_output_artifact_ids,
        "actor": context.actor.model_dump(mode="json"),
        "occurred_at": context.occurred_at,
        "published_primary_payload": _project_value(context.published_primary_payload),
        "published_artifact_ids_by_rule": _project_value(context.published_artifact_ids_by_rule),
        "published_payloads_by_rule": _project_value(context.published_payloads_by_rule),
        "published_artifacts_by_rule": _project_value(context.published_artifacts_by_rule),
    }


def _workflow_context_selector(context: WorkflowEffectContext) -> Mapping[str, object]:
    """Compact write-phase identity; immutable bulk data stays projection-bound."""

    return {
        "run_id": context.run.run_id,
        "run_kind": context.run.kind.model_dump(mode="json"),
        "policy_id": context.policy.policy_id,
        "policy_version": context.policy.policy_version,
        "outcome_code": context.policy.outcome_code,
        "workflow_effect_key": context.policy.workflow_effect_key,
        "scope": context.scope,
        "published_primary_artifact_id": context.published_primary_artifact_id,
        "occurred_at": context.occurred_at,
        "actor": context.actor.model_dump(mode="json"),
    }


WorkflowEffect = Callable[[WorkflowEffectContext], None]


def _no_workflow_mutation(context: WorkflowEffectContext) -> None:
    """A genuine no-op: this outcome never advances SubjectHead/ApprovalItem."""

    return None


def _validation_payload(run: RunRecord) -> _ValidationPayload:
    params = run.payload.params
    if not isinstance(
        params,
        (PatchValidationPayloadV1, ConstraintValidationPayloadV1, RollbackValidationPayloadV1),
    ):
        raise IntegrityViolation(
            "validation workflow effect requires a validation Run payload",
            run_id=run.run_id,
        )
    return params


def _require_approvals(
    context: WorkflowEffectContext,
) -> ValidationCompletionApprovalRepository:
    if context.approvals is None:
        raise IntegrityViolation(
            "workflow effect requires a transaction-bound approvals capability",
            run_id=context.run.run_id,
        )
    return context.approvals


_AGENT_DRAFT_SELECTORS: Mapping[AgentDraftEffectKey, tuple[str, type[object], str]] = (
    MappingProxyType(
        {
            "create_patch_subject_head_and_draft@1": (
                "generation.propose",
                GenerationProposePayloadV1,
                "patch",
            ),
            "supersede_patch_head_create_draft@1": (
                "patch.repair",
                PatchRepairPayloadV1,
                "patch",
            ),
            "create_constraint_subject_head_and_draft@1": (
                "constraint_proposal.propose",
                ConstraintProposalProposePayloadV1,
                "constraint_proposal",
            ),
        }
    )
)


def _agent_draft_effect(effect_key: AgentDraftEffectKey) -> WorkflowEffect:
    """Delegate one exact asynchronous draft mutation to the Task-7 authority.

    The generic publisher owns Artifact identity and the terminal UoW; the port
    owns ApprovalItem construction/governance and SubjectHead mutation.  Keeping
    that split explicit prevents either side from fabricating the other's facts.
    """

    def _effect(context: WorkflowEffectContext) -> None:
        expected_kind, expected_payload_type, expected_subject_kind = _AGENT_DRAFT_SELECTORS[
            effect_key
        ]
        if (
            context.scope != "run"
            or context.policy.prepared_outcome != "success"
            or context.policy.workflow_effect_key != effect_key
            or context.run.kind.kind != expected_kind
            or context.run.kind.version != 1
            or not isinstance(context.run.payload.params, expected_payload_type)
            or context.actor.principal_kind not in {"service", "system"}
        ):
            raise IntegrityViolation(
                "agent draft workflow effect differs from its exact Run/policy selector",
                workflow_effect_key=effect_key,
                run_id=context.run.run_id,
            )
        port = context.agent_drafts
        if port is None:
            raise IntegrityViolation(
                "agent draft workflow effect has no transaction-bound authority port",
                workflow_effect_key=effect_key,
                run_id=context.run.run_id,
            )
        rule_ids = tuple(rule.rule_id for rule in context.policy.artifact_rules)
        if (
            set(context.published_artifact_ids_by_rule) != set(rule_ids)
            or set(context.published_payloads_by_rule) != set(rule_ids)
            or set(context.published_artifacts_by_rule) != set(rule_ids)
        ):
            raise IntegrityViolation(
                "agent draft workflow effect lacks the exact publication rule closure",
                workflow_effect_key=effect_key,
            )
        for rule_id in rule_ids:
            artifacts = context.published_artifacts_by_rule[rule_id]
            ids = context.published_artifact_ids_by_rule[rule_id]
            payloads = context.published_payloads_by_rule[rule_id]
            if tuple(artifact.artifact_id for artifact in artifacts) != ids or len(payloads) != len(
                artifacts
            ):
                raise IntegrityViolation(
                    "agent draft final Artifact/payload mapping differs",
                    workflow_effect_key=effect_key,
                    outcome_rule_id=rule_id,
                )
        primary_ids = context.published_artifact_ids_by_rule.get("primary", ())
        primary_payloads = context.published_payloads_by_rule.get("primary", ())
        if (
            len(primary_ids) != 1
            or len(primary_payloads) != 1
            or context.published_primary_artifact_id != primary_ids[0]
            or context.published_primary_payload != primary_payloads[0]
        ):
            raise IntegrityViolation(
                "agent draft workflow effect lacks one exact published subject",
                workflow_effect_key=effect_key,
            )
        params = context.run.payload.params
        if expected_subject_kind == "patch":
            subject = PatchV2.model_validate(primary_payloads[0])
        else:
            subject = ConstraintProposalV1.model_validate(primary_payloads[0])
        current_item: ApprovalItem | None = None
        current_head: SubjectHead | None = None
        if isinstance(params, PatchRepairPayloadV1):
            approvals = _require_approvals(context)
            old_approval_id = f"approval:patch:{params.subject_patch_artifact_id}"
            current_item = approvals.get(old_approval_id)
            if current_item is None:
                raise IntegrityViolation(
                    "repair workflow current ApprovalItem is missing",
                    approval_id=old_approval_id,
                )
            current_head = approvals.get_subject_head(current_item.subject_series_id)
            current_target = current_item.target_binding
            if (
                current_head is None
                or current_item.subject_kind != "patch"
                or current_item.subject_artifact_id != params.subject_patch_artifact_id
                or current_item.status != "validation_failed"
                or current_item.workflow_revision != params.expected_workflow_revision
                or current_item.last_validation_failure_artifact_id is not None
                or current_item.evidence_set_artifact_id != params.validation_evidence_artifact_id
                or current_head.current_approval_id != current_item.approval_id
                or current_head.current_subject_artifact_id != current_item.subject_artifact_id
                or current_head.revision != params.expected_subject_head_revision
                or current_head.revision != current_item.subject_revision
                or subject.revision != current_item.subject_revision + 1
                or subject.supersedes_artifact_id != current_item.subject_artifact_id
                or not isinstance(current_target, PatchTargetBindingV1)
                or current_target.target_artifact_id != params.preview_snapshot_artifact_id
                or current_target.ref_name != params.target.ref_name
                or current_target.expected_ref != params.target.expected_ref
                or params.target.expected_ref is None
                or params.target.expected_ref.artifact_id != params.base_snapshot_artifact_id
            ):
                raise IntegrityViolation(
                    "repair workflow differs from current ApprovalItem/SubjectHead CAS",
                    run_id=context.run.run_id,
                )

        request = AgentDraftWorkflowRequest(
            effect_key=effect_key,
            run=context.run,
            policy=context.policy,
            initiated_by=context.run.initiated_by,
            executed_by=context.actor,
            subject_artifact_id=primary_ids[0],
            artifacts_by_rule=MappingProxyType(
                {key: tuple(values) for key, values in context.published_artifacts_by_rule.items()}
            ),
            artifact_ids_by_rule=MappingProxyType(
                {
                    key: tuple(values)
                    for key, values in context.published_artifact_ids_by_rule.items()
                }
            ),
            payloads_by_rule=MappingProxyType(
                {
                    key: tuple(dict(payload) for payload in values)
                    for key, values in context.published_payloads_by_rule.items()
                }
            ),
            expected_subject_head_revision=(
                context.run.payload.params.expected_subject_head_revision
                if isinstance(context.run.payload.params, PatchRepairPayloadV1)
                else None
            ),
            expected_workflow_revision=(
                context.run.payload.params.expected_workflow_revision
                if isinstance(context.run.payload.params, PatchRepairPayloadV1)
                else None
            ),
            expected_current_approval=current_item,
            expected_current_subject_head=current_head,
            occurred_at=context.occurred_at,
        )
        result = port.publish_agent_draft(request)
        item = result.approval_item
        head = result.subject_head
        expected_head_revision = (
            params.expected_subject_head_revision + 1
            if isinstance(params, PatchRepairPayloadV1)
            else 1
        )
        expected_series_id = (
            current_item.subject_series_id
            if current_item is not None
            else f"series:{expected_subject_kind}:{primary_ids[0]}"
        )
        expected_supersedes = (
            f"approval:patch:{params.subject_patch_artifact_id}"
            if isinstance(params, PatchRepairPayloadV1)
            else None
        )
        expected_domain_scope = (
            current_item.domain_scope if current_item is not None else params.domain_scope
        )
        target_ok = item.target_binding is None
        if expected_subject_kind == "patch":
            preview_ids = context.published_artifact_ids_by_rule.get("preview", ())
            preview_artifacts = context.published_artifacts_by_rule.get("preview", ())
            target = item.target_binding
            target_ok = (
                len(preview_ids) == 1
                and len(preview_artifacts) == 1
                and isinstance(target, PatchTargetBindingV1)
                and preview_artifacts[0].artifact_id == preview_ids[0]
                and preview_artifacts[0].kind == "ir_snapshot"
                and target.target_artifact_id == preview_ids[0]
                and target.target_snapshot_id == subject.target_snapshot_id
                and target.target_digest == preview_artifacts[0].payload_hash
                and target.ref_name == params.target.ref_name
                and target.expected_ref == params.target.expected_ref
            )
        if (
            item.subject_kind != expected_subject_kind
            or item.subject_artifact_id != primary_ids[0]
            or item.approval_id != f"approval:{expected_subject_kind}:{primary_ids[0]}"
            or item.subject_series_id != expected_series_id
            or item.subject_revision != subject.revision
            or item.supersedes_approval_id != expected_supersedes
            or not target_ok
            or item.proposer != context.run.initiated_by
            or item.domain_scope != expected_domain_scope
            or item.status != "draft"
            or item.workflow_revision != 1
            or item.created_at != context.occurred_at
            or head.subject_series_id != item.subject_series_id
            or head.current_subject_artifact_id != primary_ids[0]
            or head.current_approval_id != item.approval_id
            or head.revision != expected_head_revision
        ):
            raise IntegrityViolation(
                "agent draft workflow authority returned another draft/head projection",
                workflow_effect_key=effect_key,
                run_id=context.run.run_id,
            )

    return _effect


def _load_published_evidence(context: WorkflowEffectContext) -> EvidenceSet:
    payload = context.published_primary_payload
    if payload is None:
        raise IntegrityViolation(
            "validation completion has no published EvidenceSet payload",
            run_id=context.run.run_id,
        )
    return EvidenceSet.model_validate(payload)


def _validate_overall_status(evidence: EvidenceSet, target_status: str) -> None:
    if target_status == "validated":
        if evidence.overall_status != "passed":
            raise IntegrityViolation(
                "validated completion requires a passed EvidenceSet",
                overall_status=evidence.overall_status,
            )
    elif evidence.overall_status not in {"failed", "unproven"}:
        raise IntegrityViolation(
            "validation_failed completion requires a failed or unproven EvidenceSet",
            overall_status=evidence.overall_status,
        )


def _validate_kind_binding(
    evidence: EvidenceSet,
    item: object,
    run: RunRecord,
    payload: _ValidationPayload,
    subject_kind: str,
) -> None:
    if subject_kind == "patch":
        assert isinstance(payload, PatchValidationPayloadV1)
        validate_patch_evidence_binding(evidence, item, payload)
    elif subject_kind == "rollback_request":
        assert isinstance(payload, RollbackValidationPayloadV1)
        matches = tuple(
            binding
            for binding in run.payload.resolved_profiles
            if binding.field_path == "/params/rollback_profile"
        )
        if len(matches) != 1:
            raise IntegrityViolation(
                "rollback validation Run lacks one exact rollback profile binding",
                run_id=run.run_id,
            )
        validate_rollback_evidence_binding(
            evidence,
            item,
            payload,
            profile_binding=matches[0],
        )
    else:  # constraint_proposal
        assert isinstance(payload, ConstraintValidationPayloadV1)
        validate_constraint_evidence_candidate_binding(evidence, payload)


def _make_validation_completion_effect(
    *,
    subject_kind: str,
    target_status: str,
    require_auto_apply_proof: bool = False,
) -> WorkflowEffect:
    """Build a ``validated``/``validation_failed`` completion effect.

    The effect runs inside the publisher's terminal UoW: it reads the just-published
    EvidenceSet + the frozen validation payload, reuses the shared completion core to
    re-verify the subject/current-binding/evidence, then CASes the ApprovalItem. It
    NEVER re-publishes artifacts, re-books the Run terminal, or writes a second audit
    record (the publisher already did those).  Subject supersede must have been
    converted into a typed terminal failure by the publisher preflight before any
    EvidenceSet was published; observing it here is an atomicity invariant breach.
    """

    def _effect(context: WorkflowEffectContext) -> None:
        payload = _validation_payload(context.run)
        if payload_subject_kind(payload) != subject_kind:
            raise IntegrityViolation(
                "validation effect key does not match the Run payload subject kind",
                run_id=context.run.run_id,
            )
        approvals = _require_approvals(context)
        subject = payload.subject
        item = approvals.get(subject.approval_id)
        if item is None:
            raise IntegrityViolation(
                "validation completion ApprovalItem is missing",
                approval_id=subject.approval_id,
            )
        head = approvals.get_subject_head(item.subject_series_id)
        if head is None:
            raise IntegrityViolation("validation subject series has no SubjectHead")
        if head.current_approval_id != item.approval_id:
            raise IntegrityViolation(
                "validation success reached its workflow effect after subject supersede",
                approval_id=item.approval_id,
                run_id=context.run.run_id,
            )

        validate_immutable_subject_binding(item, subject, subject_kind)
        validate_current_subject_binding(item, head, subject, context.run.run_id)

        evidence = _load_published_evidence(context)
        if evidence.validation_run_id != context.run.run_id:
            raise IntegrityViolation(
                "published EvidenceSet validation_run_id differs from the Run",
                run_id=context.run.run_id,
            )
        _validate_overall_status(evidence, target_status)
        validate_evidence_subject(evidence, item)
        _validate_kind_binding(evidence, item, context.run, payload, subject_kind)

        validate_status_transition(
            current="validating", target=target_status, subject_kind=item.subject_kind
        )
        target_binding = (
            evidence.target_binding
            if subject_kind == "constraint_proposal"
            else item.target_binding
        )
        evidence_artifact_id = context.published_primary_artifact_id
        if evidence_artifact_id is None:
            raise IntegrityViolation(
                "validation completion has no published EvidenceSet artifact id",
                run_id=context.run.run_id,
            )
        regression_evidence_artifact_ids = tuple(
            sorted(context.published_artifact_ids_by_rule.get("regression", ()))
        )
        if regression_evidence_artifact_ids != regression_evidence_ids_from_set(evidence):
            raise IntegrityViolation(
                "published regression evidence differs from the final EvidenceSet"
            )
        proof_binding: AutoApplyProofBindingV1 | None = None
        auto_apply_request: (
            tuple[
                AutoApplyValidationPort,
                AutoApplyProofV1,
                ArtifactV2,
                ArtifactV2,
                tuple[ArtifactV2, ...],
                tuple[str, ...],
            ]
            | None
        ) = None
        if require_auto_apply_proof:
            if (
                subject_kind != "patch"
                or target_status != "validated"
                or context.policy.workflow_effect_key != "set_patch_validated_with_auto_proof@1"
            ):
                raise IntegrityViolation("auto-apply proof effect selector is inconsistent")
            auto_apply = context.auto_apply
            if auto_apply is None:
                raise IntegrityViolation(
                    "auto-apply validation has no transaction-bound deterministic guard",
                    run_id=context.run.run_id,
                )
            rule_ids = {rule.rule_id for rule in context.policy.artifact_rules}
            if (
                set(context.published_artifact_ids_by_rule) != rule_ids
                or set(context.published_payloads_by_rule) != rule_ids
                or set(context.published_artifacts_by_rule) != rule_ids
            ):
                raise IntegrityViolation(
                    "auto-eligible validation lacks exact final rule closure",
                    run_id=context.run.run_id,
                )
            primary_ids = context.published_artifact_ids_by_rule.get("primary", ())
            primary_artifacts = context.published_artifacts_by_rule.get("primary", ())
            if (
                primary_ids != (evidence_artifact_id,)
                or len(primary_artifacts) != 1
                or primary_artifacts[0].artifact_id != evidence_artifact_id
                or primary_artifacts[0].kind != "validation_evidence"
                or primary_artifacts[0].meta.get("payload_schema_id") != "evidence-set@1"
            ):
                raise IntegrityViolation(
                    "auto-eligible validation primary Artifact differs from EvidenceSet"
                )
            proof_ids = context.published_artifact_ids_by_rule.get("auto-apply-proof", ())
            proof_payloads = context.published_payloads_by_rule.get("auto-apply-proof", ())
            proof_artifacts = context.published_artifacts_by_rule.get("auto-apply-proof", ())
            regression_artifacts = tuple(
                sorted(
                    context.published_artifacts_by_rule.get("regression", ()),
                    key=lambda artifact: artifact.artifact_id,
                )
            )
            regression_ids = regression_evidence_artifact_ids
            if (
                len(proof_ids) != 1
                or len(proof_payloads) != 1
                or len(proof_artifacts) != 1
                or proof_artifacts[0].artifact_id != proof_ids[0]
                or proof_artifacts[0].kind != "validation_evidence"
                or proof_artifacts[0].meta.get("payload_schema_id") != "auto-apply-proof@1"
                or tuple(sorted(artifact.artifact_id for artifact in regression_artifacts))
                != regression_ids
            ):
                raise IntegrityViolation(
                    "auto-eligible validation lacks one exact final proof Artifact",
                    run_id=context.run.run_id,
                )
            proof = AutoApplyProofV1.model_validate(proof_payloads[0])
            proof_binding = build_auto_apply_proof_binding(
                proof=proof,
                proof_artifact_id=proof_ids[0],
                evidence_artifact_id=evidence_artifact_id,
                regression_artifact_ids=regression_ids,
                item=item,
            )
            auto_apply_request = (
                auto_apply,
                proof,
                primary_artifacts[0],
                proof_artifacts[0],
                regression_artifacts,
                regression_ids,
            )
        replacement = build_validation_completion_replacement(
            item,
            target_status=target_status,
            evidence_set_artifact_id=evidence_artifact_id,
            regression_evidence_artifact_ids=regression_evidence_artifact_ids,
            target_binding=target_binding,
            auto_apply_proof=proof_binding,
        )
        if auto_apply_request is not None:
            (
                auto_apply,
                proof,
                evidence_artifact,
                proof_artifact,
                regression_artifacts,
                regression_ids,
            ) = auto_apply_request
            auto_apply.validate_completion(
                AutoApplyValidationRequest(
                    run=context.run,
                    policy=context.policy,
                    current_item=item,
                    projected_item=replacement,
                    evidence=evidence,
                    evidence_artifact=evidence_artifact,
                    evidence_artifact_id=evidence_artifact_id,
                    proof=proof,
                    proof_artifact=proof_artifact,
                    proof_artifact_id=proof_artifact.artifact_id,
                    regression_artifacts=regression_artifacts,
                    regression_artifact_ids=regression_ids,
                    occurred_at=context.occurred_at,
                )
            )
        approvals.compare_and_set_validation_completion(
            item.approval_id, item.workflow_revision, replacement
        )

    return _effect


def _restore_current_draft(context: WorkflowEffectContext) -> None:
    """Revert ``validating→draft`` after a validation run-final failure.

    Only a strictly proven supersede is an allowed no-op. Every other drift fails
    closed so a stale/already-reverted/re-validating item cannot be hidden behind a
    successfully published terminal manifest.
    """

    payload = _validation_payload(context.run)
    approvals = _require_approvals(context)
    subject = payload.subject
    item = approvals.get(subject.approval_id)
    if item is None:
        raise IntegrityViolation(
            "validation revert ApprovalItem is missing",
            approval_id=subject.approval_id,
        )
    validate_immutable_subject_binding(item, subject, payload_subject_kind(payload))
    head = approvals.get_subject_head(item.subject_series_id)
    if head is None:
        raise IntegrityViolation("validation revert subject series has no SubjectHead")
    if head.current_approval_id != item.approval_id:
        validate_strict_superseded_subject_binding(item, head, subject)
        return
    validate_current_subject_binding(item, head, subject, context.run.run_id)

    reset_reason = _REVERT_RESET_REASON.get(context.policy.outcome_code)
    if reset_reason is None:
        raise IntegrityViolation(
            "validation revert has no reset reason for the terminal outcome",
            outcome_code=context.policy.outcome_code,
        )
    validate_status_transition(
        current="validating",
        target="draft",
        subject_kind=item.subject_kind,
        validation_reset_reason=reset_reason,
    )
    replacement = build_validation_revert_replacement(
        item, failure_artifact_id=context.published_primary_artifact_id
    )
    approvals.compare_and_set(item.approval_id, item.workflow_revision, replacement)


_NO_MUTATION_EFFECT_KEYS = frozenset(
    {
        "no_workflow_change@1",
        "no_workflow_subject@1",
        "terminal_only@1",
        "close_attempt_for_terminal@1",
        "close_attempt_for_retry@1",
        "leave_patch_head_unchanged@1",
    }
)

_VALIDATION_EFFECT_SELECTORS: Mapping[str, tuple[str, str, bool]] = MappingProxyType(
    {
        "set_patch_validated@1": ("patch", "validated", False),
        "set_patch_validated_with_auto_proof@1": ("patch", "validated", True),
        "set_patch_validation_failed@1": ("patch", "validation_failed", False),
        "set_rollback_validated@1": ("rollback_request", "validated", False),
        "set_rollback_validation_failed@1": (
            "rollback_request",
            "validation_failed",
            False,
        ),
        "set_exact_binding_and_validated@1": (
            "constraint_proposal",
            "validated",
            False,
        ),
        "set_exact_binding_and_validation_failed@1": (
            "constraint_proposal",
            "validation_failed",
            False,
        ),
        "leave_binding_null_and_validation_failed@1": (
            "constraint_proposal",
            "validation_failed",
            False,
        ),
    }
)


def _build_agent_draft_request(
    context: WorkflowEffectContext,
    *,
    effect_key: AgentDraftEffectKey,
) -> AgentDraftWorkflowRequest:
    expected_kind, expected_payload_type, expected_subject_kind = _AGENT_DRAFT_SELECTORS[effect_key]
    if (
        context.scope != "run"
        or context.policy.prepared_outcome != "success"
        or context.policy.workflow_effect_key != effect_key
        or context.run.kind.kind != expected_kind
        or context.run.kind.version != 1
        or not isinstance(context.run.payload.params, expected_payload_type)
        or context.actor.principal_kind not in {"service", "system"}
    ):
        raise IntegrityViolation(
            "agent draft workflow preparation differs from its exact selector",
            workflow_effect_key=effect_key,
            run_id=context.run.run_id,
        )
    rule_ids = tuple(rule.rule_id for rule in context.policy.artifact_rules)
    if (
        set(context.published_artifact_ids_by_rule) != set(rule_ids)
        or set(context.published_payloads_by_rule) != set(rule_ids)
        or set(context.published_artifacts_by_rule) != set(rule_ids)
    ):
        raise IntegrityViolation(
            "agent draft workflow preparation lacks exact rule closure",
            workflow_effect_key=effect_key,
        )
    for rule_id in rule_ids:
        artifacts = context.published_artifacts_by_rule[rule_id]
        ids = context.published_artifact_ids_by_rule[rule_id]
        payloads = context.published_payloads_by_rule[rule_id]
        if tuple(artifact.artifact_id for artifact in artifacts) != ids or len(payloads) != len(
            artifacts
        ):
            raise IntegrityViolation(
                "agent draft final Artifact/payload mapping differs",
                workflow_effect_key=effect_key,
                outcome_rule_id=rule_id,
            )
    primary_ids = context.published_artifact_ids_by_rule.get("primary", ())
    primary_payloads = context.published_payloads_by_rule.get("primary", ())
    if (
        len(primary_ids) != 1
        or len(primary_payloads) != 1
        or context.published_primary_artifact_id != primary_ids[0]
        or context.published_primary_payload != primary_payloads[0]
    ):
        raise IntegrityViolation(
            "agent draft workflow preparation lacks one exact subject",
            workflow_effect_key=effect_key,
        )
    if expected_subject_kind == "patch":
        subject = PatchV2.model_validate(primary_payloads[0])
    else:
        subject = ConstraintProposalV1.model_validate(primary_payloads[0])
    params = context.run.payload.params
    current_item: ApprovalItem | None = None
    current_head: SubjectHead | None = None
    if isinstance(params, PatchRepairPayloadV1):
        approvals = _require_approvals(context)
        old_approval_id = f"approval:patch:{params.subject_patch_artifact_id}"
        current_item = approvals.get(old_approval_id)
        if current_item is None:
            raise IntegrityViolation(
                "repair workflow current ApprovalItem is missing",
                approval_id=old_approval_id,
            )
        current_head = approvals.get_subject_head(current_item.subject_series_id)
        current_target = current_item.target_binding
        if (
            current_head is None
            or current_item.subject_kind != "patch"
            or current_item.subject_artifact_id != params.subject_patch_artifact_id
            or current_item.status != "validation_failed"
            or current_item.workflow_revision != params.expected_workflow_revision
            or current_item.last_validation_failure_artifact_id is not None
            or current_item.evidence_set_artifact_id != params.validation_evidence_artifact_id
            or current_head.current_approval_id != current_item.approval_id
            or current_head.current_subject_artifact_id != current_item.subject_artifact_id
            or current_head.revision != params.expected_subject_head_revision
            or current_head.revision != current_item.subject_revision
            or subject.revision != current_item.subject_revision + 1
            or subject.supersedes_artifact_id != current_item.subject_artifact_id
            or not isinstance(current_target, PatchTargetBindingV1)
            or current_target.target_artifact_id != params.preview_snapshot_artifact_id
            or current_target.ref_name != params.target.ref_name
            or current_target.expected_ref != params.target.expected_ref
            or params.target.expected_ref is None
            or params.target.expected_ref.artifact_id != params.base_snapshot_artifact_id
        ):
            raise IntegrityViolation(
                "repair workflow differs from current ApprovalItem/SubjectHead CAS",
                run_id=context.run.run_id,
            )
    return AgentDraftWorkflowRequest(
        effect_key=effect_key,
        run=context.run,
        policy=context.policy,
        initiated_by=context.run.initiated_by,
        executed_by=context.actor,
        subject_artifact_id=primary_ids[0],
        artifacts_by_rule=MappingProxyType(
            {key: tuple(values) for key, values in context.published_artifacts_by_rule.items()}
        ),
        artifact_ids_by_rule=MappingProxyType(
            {key: tuple(values) for key, values in context.published_artifact_ids_by_rule.items()}
        ),
        payloads_by_rule=MappingProxyType(
            {
                key: tuple(dict(payload) for payload in values)
                for key, values in context.published_payloads_by_rule.items()
            }
        ),
        expected_subject_head_revision=(
            params.expected_subject_head_revision
            if isinstance(params, PatchRepairPayloadV1)
            else None
        ),
        expected_workflow_revision=(
            params.expected_workflow_revision if isinstance(params, PatchRepairPayloadV1) else None
        ),
        expected_current_approval=current_item,
        expected_current_subject_head=current_head,
        occurred_at=context.occurred_at,
    )


def _prepare_validation_mutation(
    context: WorkflowEffectContext,
    *,
    subject_kind: str,
    target_status: str,
    require_auto_apply_proof: bool,
) -> _PreparedValidationMutation:
    payload = _validation_payload(context.run)
    if payload_subject_kind(payload) != subject_kind:
        raise IntegrityViolation(
            "validation effect key does not match Run payload subject kind",
            run_id=context.run.run_id,
        )
    approvals = _require_approvals(context)
    subject = payload.subject
    item = approvals.get(subject.approval_id)
    if item is None:
        raise IntegrityViolation(
            "validation completion ApprovalItem is missing",
            approval_id=subject.approval_id,
        )
    head = approvals.get_subject_head(item.subject_series_id)
    if head is None:
        raise IntegrityViolation("validation subject series has no SubjectHead")
    if head.current_approval_id != item.approval_id:
        raise IntegrityViolation(
            "validation success reached preparation after subject supersede",
            approval_id=item.approval_id,
            run_id=context.run.run_id,
        )
    validate_immutable_subject_binding(item, subject, subject_kind)
    validate_current_subject_binding(item, head, subject, context.run.run_id)
    evidence = _load_published_evidence(context)
    if evidence.validation_run_id != context.run.run_id:
        raise IntegrityViolation(
            "published EvidenceSet validation_run_id differs from Run",
            run_id=context.run.run_id,
        )
    _validate_overall_status(evidence, target_status)
    validate_evidence_subject(evidence, item)
    _validate_kind_binding(evidence, item, context.run, payload, subject_kind)
    validate_status_transition(
        current="validating", target=target_status, subject_kind=item.subject_kind
    )
    target_binding = (
        evidence.target_binding if subject_kind == "constraint_proposal" else item.target_binding
    )
    evidence_artifact_id = context.published_primary_artifact_id
    if evidence_artifact_id is None:
        raise IntegrityViolation("validation completion lacks EvidenceSet Artifact")
    regression_ids = tuple(sorted(context.published_artifact_ids_by_rule.get("regression", ())))
    if regression_ids != regression_evidence_ids_from_set(evidence):
        raise IntegrityViolation("published regression evidence differs from final EvidenceSet")

    proof_binding: AutoApplyProofBindingV1 | None = None
    auto_request: AutoApplyValidationRequest | None = None
    auto_preparation: PreparedAutoApplyEligibility | None = None
    if require_auto_apply_proof:
        if (
            subject_kind != "patch"
            or target_status != "validated"
            or context.policy.workflow_effect_key != "set_patch_validated_with_auto_proof@1"
        ):
            raise IntegrityViolation("auto-apply proof effect selector is inconsistent")
        auto_apply = context.auto_apply
        if auto_apply is None:
            raise IntegrityViolation(
                "auto-apply validation has no planning-phase deterministic guard",
                run_id=context.run.run_id,
            )
        rule_ids = {rule.rule_id for rule in context.policy.artifact_rules}
        if (
            set(context.published_artifact_ids_by_rule) != rule_ids
            or set(context.published_payloads_by_rule) != rule_ids
            or set(context.published_artifacts_by_rule) != rule_ids
        ):
            raise IntegrityViolation("auto-eligible validation lacks exact rule closure")
        primary_ids = context.published_artifact_ids_by_rule.get("primary", ())
        primary_artifacts = context.published_artifacts_by_rule.get("primary", ())
        proof_ids = context.published_artifact_ids_by_rule.get("auto-apply-proof", ())
        proof_payloads = context.published_payloads_by_rule.get("auto-apply-proof", ())
        proof_artifacts = context.published_artifacts_by_rule.get("auto-apply-proof", ())
        raw_regression_artifacts = tuple(context.published_artifacts_by_rule.get("regression", ()))
        raw_regression_payloads = tuple(context.published_payloads_by_rule.get("regression", ()))
        if len(raw_regression_artifacts) != len(raw_regression_payloads):
            raise IntegrityViolation("regression Artifact/payload closure is partial")
        regression_pairs = tuple(
            sorted(
                zip(
                    raw_regression_artifacts,
                    raw_regression_payloads,
                    strict=True,
                ),
                key=lambda pair: pair[0].artifact_id,
            )
        )
        regression_artifacts = tuple(pair[0] for pair in regression_pairs)
        regression_payloads = tuple(pair[1] for pair in regression_pairs)
        if (
            primary_ids != (evidence_artifact_id,)
            or len(primary_artifacts) != 1
            or primary_artifacts[0].artifact_id != evidence_artifact_id
            or primary_artifacts[0].kind != "validation_evidence"
            or primary_artifacts[0].meta.get("payload_schema_id") != "evidence-set@1"
            or len(proof_ids) != 1
            or len(proof_payloads) != 1
            or len(proof_artifacts) != 1
            or proof_artifacts[0].artifact_id != proof_ids[0]
            or proof_artifacts[0].kind != "validation_evidence"
            or proof_artifacts[0].meta.get("payload_schema_id") != "auto-apply-proof@1"
            or tuple(sorted(artifact.artifact_id for artifact in regression_artifacts))
            != regression_ids
            or len(regression_payloads) != len(regression_artifacts)
        ):
            raise IntegrityViolation("auto-eligible validation Artifact closure differs")
        proof = AutoApplyProofV1.model_validate(proof_payloads[0])
        proof_binding = build_auto_apply_proof_binding(
            proof=proof,
            proof_artifact_id=proof_ids[0],
            evidence_artifact_id=evidence_artifact_id,
            regression_artifact_ids=regression_ids,
            item=item,
        )
        projected = build_validation_completion_replacement(
            item,
            target_status=target_status,
            evidence_set_artifact_id=evidence_artifact_id,
            regression_evidence_artifact_ids=regression_ids,
            target_binding=target_binding,
            auto_apply_proof=proof_binding,
        )
        auto_request = AutoApplyValidationRequest(
            run=context.run,
            policy=context.policy,
            current_item=item,
            projected_item=projected,
            evidence=evidence,
            evidence_artifact=primary_artifacts[0],
            evidence_artifact_id=evidence_artifact_id,
            proof=proof,
            proof_artifact=proof_artifacts[0],
            proof_artifact_id=proof_artifacts[0].artifact_id,
            regression_artifacts=regression_artifacts,
            regression_artifact_ids=regression_ids,
            occurred_at=context.occurred_at,
            regression_payloads=regression_payloads,
        )
        auto_preparation = auto_apply.prepare_completion(auto_request)
        replacement = projected
    else:
        replacement = build_validation_completion_replacement(
            item,
            target_status=target_status,
            evidence_set_artifact_id=evidence_artifact_id,
            regression_evidence_artifact_ids=regression_ids,
            target_binding=target_binding,
            auto_apply_proof=proof_binding,
        )
    return _PreparedValidationMutation(
        expected_item=item,
        expected_head=head,
        replacement=replacement,
        auto_apply_request=auto_request,
        auto_apply_preparation=auto_preparation,
    )


def _prepare_validation_revert(
    context: WorkflowEffectContext,
) -> _PreparedValidationRevertMutation | _PreparedSupersededValidationNoop:
    payload = _validation_payload(context.run)
    approvals = _require_approvals(context)
    subject = payload.subject
    item = approvals.get(subject.approval_id)
    if item is None:
        raise IntegrityViolation(
            "validation revert ApprovalItem is missing", approval_id=subject.approval_id
        )
    validate_immutable_subject_binding(item, subject, payload_subject_kind(payload))
    head = approvals.get_subject_head(item.subject_series_id)
    if head is None:
        raise IntegrityViolation("validation revert subject series has no SubjectHead")
    if head.current_approval_id != item.approval_id:
        validate_strict_superseded_subject_binding(item, head, subject)
        return _PreparedSupersededValidationNoop(
            expected_item=item,
            expected_head=head,
        )
    validate_current_subject_binding(item, head, subject, context.run.run_id)
    reset_reason = _REVERT_RESET_REASON.get(context.policy.outcome_code)
    if reset_reason is None:
        raise IntegrityViolation(
            "validation revert has no reset reason for terminal outcome",
            outcome_code=context.policy.outcome_code,
        )
    validate_status_transition(
        current="validating",
        target="draft",
        subject_kind=item.subject_kind,
        validation_reset_reason=reset_reason,
    )
    replacement = build_validation_revert_replacement(
        item, failure_artifact_id=context.published_primary_artifact_id
    )
    return _PreparedValidationRevertMutation(
        expected_item=item,
        expected_head=head,
        replacement=replacement,
    )


def prepare_workflow_effect(
    key: str,
    context: WorkflowEffectContext,
) -> PreparedWorkflowEffect:
    """Perform all immutable/CPU/ObjectStore workflow work in the read phase."""

    if context.policy.workflow_effect_key != key:
        raise IntegrityViolation(
            "workflow effect key differs from exact outcome policy",
            workflow_effect_key=key,
        )
    context_digest = canonical_sha256(_workflow_context_projection(context))
    payload: _PreparedWorkflowPayload
    if key in _NO_MUTATION_EFFECT_KEYS:
        payload = _PreparedNoWorkflowMutation()
    elif key in _AGENT_DRAFT_SELECTORS:
        request = _build_agent_draft_request(
            context,
            effect_key=key,  # type: ignore[arg-type]
        )
        port = context.agent_drafts
        if port is None:
            raise IntegrityViolation(
                "agent draft workflow preparation has no authority port",
                workflow_effect_key=key,
            )
        payload = _PreparedAgentDraftMutation(
            request=request,
            draft=port.prepare_agent_draft(request),
        )
    elif key in _VALIDATION_EFFECT_SELECTORS:
        subject_kind, target_status, require_auto = _VALIDATION_EFFECT_SELECTORS[key]
        payload = _prepare_validation_mutation(
            context,
            subject_kind=subject_kind,
            target_status=target_status,
            require_auto_apply_proof=require_auto,
        )
    elif key == "restore_current_draft@1":
        payload = _prepare_validation_revert(context)
    else:
        raise IntegrityViolation("workflow effect key is not registered", workflow_effect_key=key)
    return PreparedWorkflowEffect._issue(
        effect_key=key,
        context_digest=context_digest,
        context_selector=_workflow_context_selector(context),
        run_id=context.run.run_id,
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class _PreflightedNoWorkflowMutation:
    pass


@dataclass(frozen=True, slots=True)
class _PreflightedAgentDraftMutation:
    port: AgentDraftWorkflowPort
    request: AgentDraftWorkflowRequest
    draft: PreparedTerminalDraft
    authority: PreflightedTerminalDraft
    audit_intents: TerminalDraftAuditIntents | None


@dataclass(frozen=True, slots=True)
class _PreflightedValidationCompletionCas:
    approvals: ValidationCompletionApprovalRepository
    current: ApprovalItem
    replacement: ApprovalItem


@dataclass(frozen=True, slots=True)
class _PreflightedValidationRevertCas:
    approvals: ValidationCompletionApprovalRepository
    current: ApprovalItem
    replacement: ApprovalItem


_PreflightedWorkflowPayload = (
    _PreflightedNoWorkflowMutation
    | _PreflightedAgentDraftMutation
    | _PreflightedValidationCompletionCas
    | _PreflightedValidationRevertCas
)
_PREFLIGHTED_WORKFLOW_SEAL = object()


@dataclass(frozen=True, slots=True)
class _PreflightedWorkflowState:
    payload: _PreflightedWorkflowPayload
    context_selector: Mapping[str, object]
    transaction_capabilities: tuple[object | None, object | None, object | None]
    transaction_identity: tuple[object, object] | None


def _workflow_transaction_identity(
    context: WorkflowEffectContext,
) -> tuple[object, object] | None:
    """Resolve one optional SQL transaction from raw workflow capabilities."""

    queue = [context.approvals, context.agent_drafts, context.auto_apply]
    seen: set[int] = set()
    retained: tuple[object, object] | None = None
    while queue:
        capability = queue.pop()
        if capability is None or id(capability) in seen:
            continue
        seen.add(id(capability))
        try:
            session = object.__getattribute__(capability, "_session")
        except AttributeError:
            session = None
        if session is not None:
            get_nested = getattr(session, "get_nested_transaction", None)
            get_transaction = getattr(session, "get_transaction", None)
            transaction = (get_nested() if callable(get_nested) else None) or (
                get_transaction() if callable(get_transaction) else None
            )
            if transaction is None or not getattr(transaction, "is_active", False):
                raise IntegrityViolation("workflow preflight requires an active transaction")
            identity = (session, transaction)
            if retained is None:
                retained = identity
            elif retained[0] is not session or retained[1] is not transaction:
                raise IntegrityViolation("workflow capabilities belong to different transactions")
        for attribute in ("capabilities", "_capabilities"):
            try:
                nested = object.__getattribute__(capability, attribute)
            except AttributeError:
                continue
            if is_dataclass(nested) and not isinstance(nested, type):
                queue.extend(getattr(nested, item.name) for item in fields(nested))
    return retained


def _detach_preflighted_workflow_payload(
    payload: _PreflightedWorkflowPayload,
) -> _PreflightedWorkflowPayload:
    if isinstance(payload, _PreflightedNoWorkflowMutation):
        return _PreflightedNoWorkflowMutation()
    if isinstance(payload, _PreflightedAgentDraftMutation):
        return _PreflightedAgentDraftMutation(
            port=payload.port,
            request=_freeze_agent_request(payload.request),
            draft=payload.draft.model_copy(deep=True),
            authority=payload.authority,
            audit_intents=(
                None
                if payload.audit_intents is None
                else TerminalDraftAuditIntents(
                    chain_id=payload.audit_intents.chain_id,
                    intents=tuple(payload.audit_intents.intents),
                )
            ),
        )
    if isinstance(payload, _PreflightedValidationCompletionCas):
        return _PreflightedValidationCompletionCas(
            approvals=payload.approvals,
            current=payload.current.model_copy(deep=True),
            replacement=payload.replacement.model_copy(deep=True),
        )
    if isinstance(payload, _PreflightedValidationRevertCas):
        return _PreflightedValidationRevertCas(
            approvals=payload.approvals,
            current=payload.current.model_copy(deep=True),
            replacement=payload.replacement.model_copy(deep=True),
        )
    raise IntegrityViolation("workflow preflight payload has an unknown type")


class PreflightedWorkflowEffect:
    """Opaque transaction-bound one-shot token; it contains no heavy authority."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        payload: _PreflightedWorkflowPayload,
        *,
        context: WorkflowEffectContext,
        _seal: object,
    ) -> None:
        if _seal is not _PREFLIGHTED_WORKFLOW_SEAL:
            raise TypeError("workflow preflight token is authority-issued only")
        selector = deep_freeze_value(_workflow_context_selector(context))
        if not isinstance(selector, Mapping):  # pragma: no cover - fixed projection
            raise IntegrityViolation("workflow preflight selector is not canonical")
        state = _PreflightedWorkflowState(
            payload=_detach_preflighted_workflow_payload(payload),
            context_selector=selector,
            transaction_capabilities=(
                context.approvals,
                context.agent_drafts,
                context.auto_apply,
            ),
            transaction_identity=_workflow_transaction_identity(context),
        )
        with _PREFLIGHTED_WORKFLOW_LOCK:
            _PREFLIGHTED_WORKFLOW_STATES[self] = state

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("workflow preflight token is immutable")

    def consume(self, context: WorkflowEffectContext) -> _PreflightedWorkflowPayload:
        current_transaction_identity = _workflow_transaction_identity(context)
        with _PREFLIGHTED_WORKFLOW_LOCK:
            state = _PREFLIGHTED_WORKFLOW_STATES.get(self)
            if state is None or self in _CONSUMED_PREFLIGHTED_WORKFLOWS:
                raise IntegrityViolation("workflow preflight token is invalid or reused")
        if state.context_selector != _workflow_context_selector(context):
            raise IntegrityViolation("workflow preflight token selector changed before apply")
        fresh_capabilities = (
            context.approvals,
            context.agent_drafts,
            context.auto_apply,
        )
        if any(
            retained is not fresh
            for retained, fresh in zip(
                state.transaction_capabilities,
                fresh_capabilities,
                strict=True,
            )
        ):
            raise IntegrityViolation(
                "workflow preflight token belongs to another transaction capability set"
            )
        if (state.transaction_identity is None) != (current_transaction_identity is None) or (
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
        ):
            raise IntegrityViolation("workflow preflight token belongs to another transaction")
        with _PREFLIGHTED_WORKFLOW_LOCK:
            if (
                _PREFLIGHTED_WORKFLOW_STATES.get(self) is not state
                or self in _CONSUMED_PREFLIGHTED_WORKFLOWS
            ):
                raise IntegrityViolation("workflow preflight token is invalid or reused")
            # Consume before the first effect can issue DML. A later failure rolls
            # back the surrounding transaction and cannot authorize a retry.
            _CONSUMED_PREFLIGHTED_WORKFLOWS.add(self)
        return state.payload

    def audit_intents_for_terminal_merge(
        self,
        context: WorkflowEffectContext,
    ) -> TerminalDraftAuditIntents | None:
        """Return same-transaction workflow audit intents without consuming DML authority."""

        current_transaction_identity = _workflow_transaction_identity(context)
        with _PREFLIGHTED_WORKFLOW_LOCK:
            state = _PREFLIGHTED_WORKFLOW_STATES.get(self)
            if state is None or self in _CONSUMED_PREFLIGHTED_WORKFLOWS:
                raise IntegrityViolation("workflow preflight token is invalid or reused")
        if state.context_selector != _workflow_context_selector(context):
            raise IntegrityViolation("workflow preflight token selector changed before Audit merge")
        fresh_capabilities = (
            context.approvals,
            context.agent_drafts,
            context.auto_apply,
        )
        if any(
            retained is not fresh
            for retained, fresh in zip(
                state.transaction_capabilities,
                fresh_capabilities,
                strict=True,
            )
        ):
            raise IntegrityViolation(
                "workflow Audit intents belong to another transaction capability set"
            )
        if (state.transaction_identity is None) != (current_transaction_identity is None) or (
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
        ):
            raise IntegrityViolation("workflow Audit intents belong to another transaction")
        payload = state.payload
        if not isinstance(payload, _PreflightedAgentDraftMutation):
            return None
        if payload.audit_intents is None:
            return None
        return TerminalDraftAuditIntents(
            chain_id=payload.audit_intents.chain_id,
            intents=tuple(payload.audit_intents.intents),
        )


_PREFLIGHTED_WORKFLOW_LOCK = Lock()
_PREFLIGHTED_WORKFLOW_STATES: WeakKeyDictionary[
    PreflightedWorkflowEffect,
    _PreflightedWorkflowState,
] = WeakKeyDictionary()
_CONSUMED_PREFLIGHTED_WORKFLOWS: WeakSet[PreflightedWorkflowEffect] = WeakSet()


def preflight_prepared_workflow_effect(
    prepared: PreparedWorkflowEffect,
    context: WorkflowEffectContext,
    *,
    merge_audit_into_terminal_batch: bool = False,
) -> PreflightedWorkflowEffect:
    """SELECT-only fresh authority pass, required before any terminal DML."""

    prepared.require_trusted()
    if (
        prepared.effect_key != context.policy.workflow_effect_key
        or prepared.run_id != context.run.run_id
        or prepared.context_selector != _workflow_context_selector(context)
    ):
        raise IntegrityViolation("prepared workflow selector differs at preflight")
    payload = prepared.detached_payload()
    authority: _PreflightedWorkflowPayload
    if isinstance(payload, _PreparedNoWorkflowMutation):
        authority = _PreflightedNoWorkflowMutation()
    elif isinstance(payload, _PreparedAgentDraftMutation):
        port = context.agent_drafts
        if port is None:
            raise IntegrityViolation("prepared Agent draft has no fresh authority port")
        agent_authority = port.preflight_prepared_agent_draft(
            prepared=payload.draft,
            request=payload.request,
            merge_audit_into_terminal_batch=merge_audit_into_terminal_batch,
        )
        authority = _PreflightedAgentDraftMutation(
            port=port,
            request=payload.request,
            draft=payload.draft,
            authority=agent_authority,
            audit_intents=(
                port.preflighted_agent_draft_audit_intents(
                    preflighted=agent_authority,
                    request=payload.request,
                )
                if merge_audit_into_terminal_batch
                else None
            ),
        )
    else:
        approvals = _require_approvals(context)
        if isinstance(payload, _PreparedValidationMutation):
            current = approvals.get(payload.expected_item.approval_id)
            head = approvals.get_subject_head(payload.expected_item.subject_series_id)
            if current != payload.expected_item or head != payload.expected_head:
                raise IntegrityViolation("validation workflow authority changed after preparation")
            if (payload.auto_apply_request is None) != (payload.auto_apply_preparation is None):
                raise IntegrityViolation("prepared auto-apply closure is partial")
            if payload.auto_apply_request is not None:
                auto_apply = context.auto_apply
                if auto_apply is None:
                    raise IntegrityViolation("prepared auto-apply has no fresh authority port")
                assert payload.auto_apply_preparation is not None
                # This method performs exactly one fresh Ref SELECT and no DML.
                auto_apply.commit_prepared_completion(
                    prepared=payload.auto_apply_preparation,
                    request=payload.auto_apply_request,
                )
            authority = _PreflightedValidationCompletionCas(
                approvals=approvals,
                current=current,
                replacement=payload.replacement,
            )
        elif isinstance(payload, _PreparedValidationRevertMutation):
            current = approvals.get(payload.expected_item.approval_id)
            head = approvals.get_subject_head(payload.expected_item.subject_series_id)
            if current != payload.expected_item or head != payload.expected_head:
                raise IntegrityViolation("validation revert authority changed after preparation")
            authority = _PreflightedValidationRevertCas(
                approvals=approvals,
                current=current,
                replacement=payload.replacement,
            )
        elif isinstance(payload, _PreparedSupersededValidationNoop):
            current = approvals.get(payload.expected_item.approval_id)
            head = approvals.get_subject_head(payload.expected_item.subject_series_id)
            if current != payload.expected_item or head != payload.expected_head:
                raise IntegrityViolation(
                    "superseded validation authority changed after preparation"
                )
            authority = _PreflightedNoWorkflowMutation()
        else:
            raise IntegrityViolation("prepared workflow effect has an unknown payload")
    return PreflightedWorkflowEffect(
        authority,
        context=context,
        _seal=_PREFLIGHTED_WORKFLOW_SEAL,
    )


def apply_preflighted_workflow_effect(
    preflighted: PreflightedWorkflowEffect,
    context: WorkflowEffectContext,
) -> None:
    """DML-only application; all SELECTs and immutable work already completed."""

    payload = preflighted.consume(context)
    if isinstance(payload, _PreflightedNoWorkflowMutation):
        return
    if isinstance(payload, _PreflightedAgentDraftMutation):
        result = payload.port.apply_preflighted_agent_draft(
            preflighted=payload.authority,
            request=payload.request,
        )
        expected_head = SubjectHead(
            subject_series_id=payload.draft.approval_item.subject_series_id,
            current_subject_artifact_id=payload.draft.approval_item.subject_artifact_id,
            current_approval_id=payload.draft.approval_item.approval_id,
            revision=(
                1
                if payload.draft.expected_subject_head is None
                else payload.draft.expected_subject_head.revision + 1
            ),
        )
        if (
            result.approval_item != payload.draft.approval_item
            or result.subject_head != expected_head
        ):
            raise IntegrityViolation("prepared Agent draft committed another projection")
        return
    if isinstance(payload, _PreflightedValidationCompletionCas):
        payload.approvals.apply_preflighted_validation_completion(
            payload.current,
            payload.replacement,
        )
    elif isinstance(payload, _PreflightedValidationRevertCas):
        payload.approvals.apply_preflighted_compare_and_set(
            payload.current,
            payload.replacement,
        )
    else:  # pragma: no cover - sealed union is exhaustive
        raise IntegrityViolation("workflow preflight token has an unknown payload")


def commit_prepared_workflow_effect(
    prepared: PreparedWorkflowEffect,
    context: WorkflowEffectContext,
) -> None:
    """Compatibility wrapper; publisher uses the explicit two calls."""

    apply_preflighted_workflow_effect(
        preflight_prepared_workflow_effect(prepared, context),
        context,
    )


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
        # ── asynchronous draft publication ───────────────────────────────────
        "create_patch_subject_head_and_draft@1": _agent_draft_effect(
            "create_patch_subject_head_and_draft@1"
        ),
        "supersede_patch_head_create_draft@1": _agent_draft_effect(
            "supersede_patch_head_create_draft@1"
        ),
        "create_constraint_subject_head_and_draft@1": _agent_draft_effect(
            "create_constraint_subject_head_and_draft@1"
        ),
        # ── validation completion (Task 17b) ──────────────────────────────────
        "set_patch_validated@1": _make_validation_completion_effect(
            subject_kind="patch", target_status="validated"
        ),
        # Completion retains the exact proof binding on ``validated``.  The later
        # submit command reruns eligibility and performs validated ->
        # auto_apply_eligible; absence of either the proof or deterministic guard
        # fails this terminal UoW closed instead of silently downgrading.
        "set_patch_validated_with_auto_proof@1": _make_validation_completion_effect(
            subject_kind="patch",
            target_status="validated",
            require_auto_apply_proof=True,
        ),
        "set_patch_validation_failed@1": _make_validation_completion_effect(
            subject_kind="patch", target_status="validation_failed"
        ),
        "set_rollback_validated@1": _make_validation_completion_effect(
            subject_kind="rollback_request", target_status="validated"
        ),
        "set_rollback_validation_failed@1": _make_validation_completion_effect(
            subject_kind="rollback_request", target_status="validation_failed"
        ),
        "set_exact_binding_and_validated@1": _make_validation_completion_effect(
            subject_kind="constraint_proposal", target_status="validated"
        ),
        "set_exact_binding_and_validation_failed@1": _make_validation_completion_effect(
            subject_kind="constraint_proposal", target_status="validation_failed"
        ),
        "leave_binding_null_and_validation_failed@1": _make_validation_completion_effect(
            subject_kind="constraint_proposal", target_status="validation_failed"
        ),
        "restore_current_draft@1": _restore_current_draft,
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
    "AgentDraftWorkflowPort",
    "AgentDraftWorkflowRequest",
    "AgentDraftPreparedAssembler",
    "ApprovalCommandAgentDraftWorkflowPort",
    "AutoApplyValidationPort",
    "AutoApplyValidationRequest",
    "PreparedWorkflowEffect",
    "PreflightedWorkflowEffect",
    "WORKFLOW_EFFECTS",
    "WorkflowEffect",
    "WorkflowEffectContext",
    "apply_workflow_effect",
    "apply_preflighted_workflow_effect",
    "commit_prepared_workflow_effect",
    "prepare_workflow_effect",
    "preflight_prepared_workflow_effect",
    "resolve_workflow_effect",
]
