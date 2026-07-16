"""The generic terminal publication engine.

``TerminalPublisher`` is the concrete
:class:`gameforge.platform.runs.lifecycle.RunLifecyclePublicationGateway`.  The Run
lifecycle service selects the unique policy per scope and owns cost/event closure.
This engine first produces a pure, complete ``TerminalPublicationDraft`` from a
short authority snapshot.  A separate stager writes every content-addressed blob
after that snapshot closes.  The single write UoW then reprojects from fresh
authority and :meth:`TerminalPublisher.commit` consumes only exact staged receipts
to publish Artifact/Finding/workflow/manifest/audit authority atomically.

The commit surface has no ObjectStore write capability. Active final failure is one
aggregate projection: the run manifest validates the planned attempt manifest via
an exact in-memory overlay, both are staged together, and ``commit_many`` validates
both fresh digests before the first DB write.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from gameforge.contracts.canonical import canonical_json, canonical_sha256, sha256_lowerhex
from gameforge.contracts.cassette import CassetteRecordV1, CassetteRecordV2
from gameforge.contracts.cassette_import import CassetteBundleV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    ProfileRefV1,
    execution_profile_payload_hash,
)
from gameforge.contracts.findings import FindingRevisionV1
from gameforge.contracts.jobs import (
    ConstraintValidationPayloadV1,
    OutcomeArtifactRuleV1,
    OutcomeArtifactPolicyV1,
    PreparedRunFailure,
    PreparedRunOutcome,
    PreparedRunResult,
    PatchValidationPayloadV1,
    RequirementDispositionV1,
    ResolvedPolicySnapshotV1,
    RetryDecisionV1,
    RunAttempt,
    RunFailureV1,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunRecord,
    RunResultSummaryV1,
    RunResultV1,
    RollbackValidationPayloadV1,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    ExecutionIdentityV1,
    ObjectRef,
    VersionTuple,
    artifact_id_v2_for,
    build_artifact_v2,
    build_execution_identity,
    object_ref_for_bytes,
    parse_artifact,
)
from gameforge.contracts.model_router import ModelRequestV2, parse_model_request, request_hash
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.platform.publication.effects import (
    AgentDraftWorkflowPort,
    AutoApplyValidationPort,
    WorkflowEffectContext,
    apply_workflow_effect,
)
from gameforge.platform.approvals.validation import (
    ValidationCompletionApprovalRepository,
    payload_subject_kind,
    validate_current_subject_binding,
    validate_immutable_subject_binding,
    validate_strict_superseded_subject_binding,
)
from gameforge.platform.publication.findings import PlannedFindingWrite, plan_finding_write
from gameforge.platform.publication.lineage import (
    LineageParentSources,
    ParentInfo,
    project_typed_lineage,
    resolve_child_payload_references,
)
from gameforge.platform.publication.planner import (
    PublicationPlan,
    PublicationRegistry,
    build_publication_plan,
    resolve_definition,
)
from gameforge.platform.publication.payload_binding import (
    FinalSiblingFact,
    bind_final_payload_references,
    final_sibling_fact_for,
    validate_domain_payload_bindings,
)
from gameforge.platform.publication.payload_schema import (
    PayloadBlobDecoder,
    decode_and_validate_artifact_payload,
    encode_validated_artifact_payload,
)
from gameforge.platform.publication.producer import (
    BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER,
    DomainProducerFactsResolver,
    validate_domain_artifact_producer,
)
from gameforge.platform.terminal_staging import (
    BlobMaterial,
    StagedReceipt,
    StagedTerminalPublication,
    TerminalPublicationDraft,
)
from gameforge.platform.lineage.validation import (
    ProducerValidationContext,
    validate_artifact_producer,
)
from gameforge.platform.publication.validator import (
    PreparedArtifactView,
    ProjectedRuntimeParent,
    RuleAllocation,
    allocate_artifacts,
    validate_plan_dispositions,
    validate_published_artifact_ids,
    validate_requirement_profile_bindings,
    validate_rule_cardinality,
    validate_runtime_parents,
)
from gameforge.contracts.jobs import RuntimeParentRuleSetV1
from gameforge.platform.publication.version import (
    project_domain_version_tuple,
    project_manifest_version_tuple,
)
from gameforge.platform.runs.lifecycle import (
    AttemptFailurePublication,
    RunFailurePublication,
    RunResultPublication,
    validate_prepared_failure,
)


class ArtifactPort(Protocol):
    def get(self, artifact_id: str) -> object | None: ...

    def put_staged(self, artifact: ArtifactV2, receipt: StagedReceipt) -> ArtifactV2:
        """Bind the explicit staged generation and persist the Artifact atomically."""
        ...

    def read_bytes(self, artifact_id: str) -> bytes:
        """Read the exact bytes of one already-published Artifact."""
        ...


class BlobStore(Protocol):
    def read(self, object_ref: ObjectRef, location: object) -> bytes: ...


class FindingStore(Protocol):
    def put(
        self, revision: FindingRevisionV1, *, expected_current_revision: int | None
    ) -> FindingRevisionV1: ...


class ManifestLedger(Protocol):
    def prompt_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]: ...

    def closed_attempt_failures(self, run_id: str) -> tuple[tuple[int, str], ...]: ...

    def put_finding_link(self, link: RunFindingLinkV1) -> RunFindingLinkV1: ...

    def execution_identity(self, run_id: str, *, attempt_no: int | None) -> ExecutionIdentityV1:
        """Authoritative identity for one attempt, or the whole Run when null."""
        ...

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        """Read the exact transaction-bound Attempt head used to fence prompt links."""
        ...

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None:
        """Read one reserve-before-use native routing decision exactly."""
        ...

    # --- Task 10 runtime-parent sources (RECORD/REPLAY only) -----------------
    # These supply the recorded-cassette runtime parents the RECORD/REPLAY
    # publication projects. A ``not_applicable``/``live`` Run never calls them.
    def record_shard_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[tuple[int, int, str], ...]:
        """(attempt_no, call_ordinal, artifact_id) for each RECORD response shard."""
        ...

    def attempt_cassette_bundle(self, run_id: str, *, attempt_no: int) -> str | None:
        """The current attempt's aggregate ``cassette_bundle`` artifact id (RECORD)."""
        ...

    def run_cassette_bundle(self, run_id: str) -> str | None:
        """The Run aggregate ``cassette_bundle`` artifact id (RECORD)."""
        ...

    def replay_input_cassette(self, run_id: str) -> str | None:
        """The REPLAY input ``cassette_bundle`` artifact id (== payload cassette)."""
        ...


class AuditPort(Protocol):
    def record(
        self,
        *,
        action: str,
        run: RunRecord,
        artifact_id: str | None,
        actor: AuditActor,
        occurred_at: str,
    ) -> None: ...


def _role_for_manifest(rule_role: str) -> str:
    return "evidence" if rule_role == "evidence" else "output"


def _failure_attempt_status(
    prepared: PreparedRunFailure,
) -> str:
    if prepared.cause_code == "lease_expired" or prepared.failure_class == "lease":
        return "lease_expired"
    if prepared.failure_class in {"cancelled", "subject_superseded"}:
        return "cancelled"
    if prepared.failure_class == "timeout":
        return "timed_out"
    return "failed"


def _failure_run_status(
    prepared: PreparedRunFailure,
    retry_decision: RetryDecisionV1,
) -> str:
    if prepared.failure_class in {"cancelled", "subject_superseded"}:
        return "cancelled"
    if prepared.failure_class == "timeout" or (
        prepared.failure_class == "lease"
        and retry_decision.reason_code
        in {"attempt_deadline_exhausted", "overall_deadline_exhausted"}
    ):
        return "timed_out"
    return "failed"


def project_runtime_parents(
    *,
    rule_set: RuntimeParentRuleSetV1,
    run_id: str,
    manifest_scope: str,
    current_attempt_no: int | None,
    llm_execution_mode: str,
    prompt_links: Sequence[RunIntermediateArtifactLinkV1],
    record_shards: Sequence[tuple[int, int, str]],
    closed: Mapping[str, int | None],
    attempt_bundle_id: str | None,
    run_bundle_id: str | None,
    replay_input_id: str | None,
    committed_link_counts: Mapping[str, int],
    artifact_info_by_id: Mapping[str, ParentInfo],
    consumed_response_call_keys: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[RunManifestParentBindingV1, ...]:
    """Project runtime parents from re-read Artifact facts, never source labels.

    Ledger rows prove *which* Artifacts belong to a Run/attempt.  The immutable
    Artifact rows prove their kind/schema/VersionTuple.  Both are required here:
    trusting a ledger source label as if it also proved ``kind`` or
    ``payload_schema_id`` would let a stale or forged link bypass the retained
    runtime-parent policy.
    """

    if manifest_scope == "attempt" and current_attempt_no is None:
        raise IntegrityViolation("attempt runtime-parent projection requires attempt_no")

    bindings: list[RunManifestParentBindingV1] = []
    projected: list[ProjectedRuntimeParent] = []

    def append_parent(
        *,
        artifact_id: str,
        source: str,
        role: str,
        publication: str,
        attempt_no: int | None = None,
        ordinal: int | None = None,
        cassette_scope: str | None = None,
    ) -> None:
        info = artifact_info_by_id.get(artifact_id)
        if info is None or info.artifact_id != artifact_id:
            raise IntegrityViolation(
                "runtime parent Artifact facts are unavailable",
                artifact_id=artifact_id,
                source=source,
            )
        try:
            binding = RunManifestParentBindingV1(
                artifact_id=artifact_id,
                role=role,
                publication=publication,
                attempt_no=attempt_no,
                ordinal=ordinal,
                cassette_scope=cassette_scope,
            )
        except ValueError as exc:
            raise IntegrityViolation(
                "runtime parent binding shape is invalid",
                artifact_id=artifact_id,
                source=source,
            ) from exc
        bindings.append(binding)
        projected.append(
            ProjectedRuntimeParent(
                artifact_id=artifact_id,
                source=source,
                kind=info.kind,
                payload_schema_id=info.payload_schema_id,
            )
        )

    prompt_keys: list[tuple[int, int]] = []
    for link in prompt_links:
        if link.run_id != run_id:
            raise IntegrityViolation("prompt link belongs to another Run")
        if manifest_scope == "attempt" and link.attempt_no != current_attempt_no:
            raise IntegrityViolation("attempt manifest contains another attempt's prompt link")
        if manifest_scope == "run" and (
            current_attempt_no is None or link.attempt_no > current_attempt_no
        ):
            raise IntegrityViolation("run manifest contains a future prompt link")
        prompt_keys.append((link.attempt_no, link.call_ordinal))
        append_parent(
            artifact_id=link.artifact_id,
            source="published_intermediate",
            role="intermediate",
            publication="run_published",
            attempt_no=link.attempt_no,
            ordinal=link.call_ordinal,
        )
    if len(prompt_keys) != len(set(prompt_keys)):
        raise IntegrityViolation("runtime prompt links contain a duplicate logical call")

    shard_keys: list[tuple[int, int]] = []
    for shard_attempt_no, call_ordinal, artifact_id in record_shards:
        if manifest_scope == "attempt" and shard_attempt_no != current_attempt_no:
            raise IntegrityViolation("attempt manifest contains another attempt's record shard")
        if manifest_scope == "run" and (
            current_attempt_no is None or shard_attempt_no > current_attempt_no
        ):
            raise IntegrityViolation("run manifest contains a future record shard")
        shard_keys.append((shard_attempt_no, call_ordinal))
        append_parent(
            artifact_id=artifact_id,
            source="record_shard",
            role="intermediate",
            publication="run_published",
            attempt_no=shard_attempt_no,
            ordinal=call_ordinal,
            cassette_scope="record_shard",
        )
    if len(shard_keys) != len(set(shard_keys)):
        raise IntegrityViolation("runtime record shards contain a duplicate logical call")
    if llm_execution_mode == "record":
        if not consumed_response_call_keys.issubset(prompt_keys):
            raise IntegrityViolation(
                "consumed RECORD responses have no committed rendered-prompt links"
            )
        if set(shard_keys) != consumed_response_call_keys:
            raise IntegrityViolation("record shards do not map one-to-one onto consumed responses")
    elif consumed_response_call_keys:
        raise IntegrityViolation("consumed RECORD response keys were supplied outside RECORD mode")

    if attempt_bundle_id is not None:
        append_parent(
            artifact_id=attempt_bundle_id,
            source="attempt_bundle",
            role="intermediate",
            publication="run_published",
            attempt_no=current_attempt_no,
            cassette_scope="attempt_bundle",
        )
    if run_bundle_id is not None:
        append_parent(
            artifact_id=run_bundle_id,
            source="run_bundle",
            role="intermediate",
            publication="run_published",
            cassette_scope="run_bundle",
        )

    if replay_input_id is not None:
        append_parent(
            artifact_id=replay_input_id,
            source="run_input",
            role="input",
            publication="existing",
            cassette_scope="replay_input",
        )

    for failure_id, closed_attempt_no in closed.items():
        if manifest_scope != "run" or closed_attempt_no is None:
            raise IntegrityViolation("closed-attempt failure has an invalid manifest scope")
        if current_attempt_no is None or closed_attempt_no > current_attempt_no:
            raise IntegrityViolation("closed-attempt failure belongs to a future attempt")
        append_parent(
            artifact_id=failure_id,
            source="closed_attempt_failure",
            role="intermediate",
            publication="run_published",
            attempt_no=closed_attempt_no,
        )

    parent_ids = [binding.artifact_id for binding in bindings]
    if len(parent_ids) != len(set(parent_ids)):
        raise IntegrityViolation("one Artifact occupies more than one runtime-parent binding")

    validate_runtime_parents(
        rule_set=rule_set,
        manifest_scope=manifest_scope,
        llm_execution_mode=llm_execution_mode,
        parents=projected,
        committed_link_counts=committed_link_counts,
        execution_identity_counts={
            "current_attempt": sum(
                attempt_no == current_attempt_no for attempt_no, _ in consumed_response_call_keys
            ),
            "all_attempts": len(consumed_response_call_keys),
        },
    )

    scoped_rules = tuple(
        rule for rule in rule_set.rules if rule.manifest_scope in (manifest_scope, "both")
    )
    for binding, parent in zip(bindings, projected, strict=True):
        matches = tuple(
            rule
            for rule in scoped_rules
            if rule.source == parent.source
            and rule.artifact_kind == parent.kind
            and parent.payload_schema_id in rule.payload_schema_ids
        )
        if len(matches) != 1 or binding.role != matches[0].parent_role:
            raise IntegrityViolation(
                "runtime parent role differs from its exact rule",
                artifact_id=binding.artifact_id,
            )
    return tuple(bindings)


@dataclass(frozen=True, slots=True)
class _TerminalRuntimeProjection:
    parents: tuple[RunManifestParentBindingV1, ...]
    execution_identity: ExecutionIdentityV1 | None
    cassette_ids_by_scope: Mapping[str, str]
    attempt_cassette_artifact_id: str | None
    terminal_cassette_artifact_id: str | None


@dataclass(frozen=True, slots=True)
class _CassetteNode:
    artifact: ArtifactV2
    payload: CassetteBundleV1


def _same_immutable_artifact(stored: object, expected: ArtifactV2) -> bool:
    """Mirror the Artifact repository's immutable identity contract.

    ``created_at`` records the first successful insertion and is deliberately not
    content identity.  A deterministic recomputation may therefore receive the
    pre-existing row with its original timestamp, but no other field may differ.
    """

    return isinstance(stored, ArtifactV2) and canonical_json(
        stored.model_dump(mode="json", exclude={"created_at"})
    ) == canonical_json(expected.model_dump(mode="json", exclude={"created_at"}))


@dataclass(frozen=True, slots=True)
class _ArtifactWrite:
    slot: str
    artifact: ArtifactV2


@dataclass(frozen=True, slots=True)
class _FindingWrite:
    planned: PlannedFindingWrite


@dataclass(frozen=True, slots=True)
class _WorkflowWrite:
    effect_key: str
    context: WorkflowEffectContext


@dataclass(frozen=True, slots=True)
class _AuditWrite:
    action: str
    run: RunRecord
    artifact_id: str | None
    actor: AuditActor
    occurred_at: str


def _model_projection(value: object) -> object:
    """Return the complete JSON projection used by the draft digest."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _model_projection(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_model_projection(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise IntegrityViolation(
        "terminal publication contains a non-canonical projected value",
        value_type=type(value).__qualname__,
    )


def _operation_projection(operation: object) -> Mapping[str, object]:
    if isinstance(operation, _ArtifactWrite):
        return {
            "operation": "artifact.put_staged",
            "slot": operation.slot,
            "artifact": operation.artifact.model_dump(mode="json"),
        }
    if isinstance(operation, _FindingWrite):
        return {
            "operation": "finding.put_and_link",
            "revision": operation.planned.revision.model_dump(mode="json"),
            "expected_current_revision": operation.planned.expected_current_revision,
            "link": operation.planned.link.model_dump(mode="json"),
        }
    if isinstance(operation, _WorkflowWrite):
        context = operation.context
        return {
            "operation": "workflow.apply",
            "effect_key": operation.effect_key,
            "run": context.run.model_dump(mode="json"),
            "policy": context.policy.model_dump(mode="json"),
            "scope": context.scope,
            "published_primary_artifact_id": context.published_primary_artifact_id,
            "published_output_artifact_ids": context.published_output_artifact_ids,
            "actor": context.actor.model_dump(mode="json"),
            "occurred_at": context.occurred_at,
            "published_primary_payload": _model_projection(context.published_primary_payload),
            "published_artifact_ids_by_rule": _model_projection(
                context.published_artifact_ids_by_rule
            ),
            "published_payloads_by_rule": _model_projection(context.published_payloads_by_rule),
            "published_artifacts_by_rule": _model_projection(context.published_artifacts_by_rule),
        }
    if isinstance(operation, _AuditWrite):
        return {
            "operation": "audit.record",
            "action": operation.action,
            "run": operation.run.model_dump(mode="json"),
            "artifact_id": operation.artifact_id,
            "actor": operation.actor.model_dump(mode="json"),
            "occurred_at": operation.occurred_at,
        }
    raise IntegrityViolation(
        "terminal publication contains an unknown commit operation",
        operation_type=type(operation).__qualname__,
    )


class _PublicationCollector:
    """Capture a complete pure publication plan without mutating authority."""

    def __init__(
        self,
        *,
        publication_kind: str,
        run_id: str,
        attempt_no: int | None,
        occurred_at: str,
    ) -> None:
        self.publication_kind = publication_kind
        self.run_id = run_id
        self.attempt_no = attempt_no
        self.occurred_at = occurred_at
        self.materials: list[BlobMaterial] = []
        self.operations: list[object] = []
        self._pending_slots_by_ref: dict[str, list[str]] = {}

    def add_blob(self, payload: bytes) -> ObjectRef:
        expected_ref = object_ref_for_bytes(payload)
        slot = f"blob:{len(self.materials) + 1:04d}"
        self.materials.append(BlobMaterial(slot=slot, payload=payload, expected_ref=expected_ref))
        self._pending_slots_by_ref.setdefault(expected_ref.key, []).append(slot)
        return expected_ref

    def add_artifact(self, artifact: ArtifactV2) -> ArtifactV2:
        pending = self._pending_slots_by_ref.get(artifact.object_ref.key, [])
        if not pending:
            raise IntegrityViolation(
                "planned Artifact has no exact blob material",
                artifact_id=artifact.artifact_id,
            )
        slot = pending.pop(0)
        material = next(item for item in self.materials if item.slot == slot)
        if material.expected_ref != artifact.object_ref:
            raise IntegrityViolation(
                "planned Artifact differs from its blob material",
                artifact_id=artifact.artifact_id,
                slot=slot,
            )
        self.operations.append(_ArtifactWrite(slot=slot, artifact=artifact))
        return artifact

    def add_finding(self, planned: PlannedFindingWrite) -> None:
        self.operations.append(_FindingWrite(planned=planned))

    def add_workflow(self, effect_key: str, context: WorkflowEffectContext) -> None:
        self.operations.append(_WorkflowWrite(effect_key=effect_key, context=context))

    def add_audit(
        self,
        *,
        action: str,
        run: RunRecord,
        artifact_id: str | None,
        actor: AuditActor,
        occurred_at: str,
    ) -> None:
        self.operations.append(
            _AuditWrite(
                action=action,
                run=run,
                artifact_id=artifact_id,
                actor=actor,
                occurred_at=occurred_at,
            )
        )

    def freeze(self, result: object) -> TerminalPublicationDraft:
        dangling = tuple(slot for slots in self._pending_slots_by_ref.values() for slot in slots)
        if dangling:
            raise IntegrityViolation(
                "terminal publication has blob materials without Artifact writes",
                slots=dangling,
            )
        operations = tuple(self.operations)
        operation_projection = tuple(_operation_projection(item) for item in operations)
        result_projection_value = _model_projection(result)
        if not isinstance(result_projection_value, Mapping):
            raise IntegrityViolation("terminal publication result is not a canonical model")
        base = {
            "publication_kind": self.publication_kind,
            "run_id": self.run_id,
            "attempt_no": self.attempt_no,
            "occurred_at": self.occurred_at,
            "materials": tuple(
                {
                    "slot": material.slot,
                    "expected_ref": material.expected_ref.model_dump(mode="json"),
                }
                for material in self.materials
            ),
            "operations": operation_projection,
            "result": result_projection_value,
        }
        return TerminalPublicationDraft(
            publication_kind=self.publication_kind,
            run_id=self.run_id,
            attempt_no=self.attempt_no,
            occurred_at=self.occurred_at,
            projection_digest=canonical_sha256(base),
            materials=tuple(self.materials),
            operations=operations,
            operation_projection=operation_projection,
            result_projection=result_projection_value,
            result=result,
        )


class TerminalPublisher:
    """Concrete ``RunLifecyclePublicationGateway`` for every M4 Run kind."""

    def __init__(
        self,
        *,
        registry: PublicationRegistry,
        artifacts: ArtifactPort,
        blobs: BlobStore,
        findings: FindingStore,
        ledger: ManifestLedger,
        audit: AuditPort,
        approvals: ValidationCompletionApprovalRepository | None = None,
        agent_drafts: AgentDraftWorkflowPort | None = None,
        auto_apply: AutoApplyValidationPort | None = None,
        producer_facts: DomainProducerFactsResolver = BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER,
        payload_decoders: Mapping[str, PayloadBlobDecoder] | None = None,
    ) -> None:
        self._registry = registry
        self._artifacts = artifacts
        self._blobs = blobs
        self._findings = findings
        self._ledger = ledger
        self._audit = audit
        self._producer_facts = producer_facts
        self._payload_decoders = dict(payload_decoders or {})
        # The transaction-bound approvals capability the validation-completion
        # workflow effects CAS the ApprovalItem through, inside this same terminal
        # UoW (Task 17b). ``None`` for a composition that never runs a validation
        # kind; a validation terminal then fails closed rather than silently
        # skipping the required ApprovalItem transition.
        self._approvals = approvals
        self._agent_drafts = agent_drafts
        self._auto_apply = auto_apply
        self._collector: _PublicationCollector | None = None
        self._planned_artifact_overlay: dict[str, tuple[ArtifactV2, bytes]] = {}

    # ---------------------------------------------------------- three phases
    def _collect_draft(
        self,
        *,
        publication_kind: str,
        run_id: str,
        attempt_no: int | None,
        occurred_at: str,
        publish: object,
    ) -> TerminalPublicationDraft:
        if self._collector is not None:
            raise IntegrityViolation("terminal publication planning cannot be nested")
        collector = _PublicationCollector(
            publication_kind=publication_kind,
            run_id=run_id,
            attempt_no=attempt_no,
            occurred_at=occurred_at,
        )
        self._collector = collector
        try:
            result = publish()  # type: ignore[operator]
            return collector.freeze(result)
        finally:
            self._collector = None

    def plan_run_result(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunResult,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> TerminalPublicationDraft:
        return self._collect_draft(
            publication_kind="run_result",
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            occurred_at=occurred_at,
            publish=lambda: self.publish_run_result(
                run=run,
                attempt=attempt,
                prepared=prepared,
                policy=policy,
                occurred_at=occurred_at,
                actor=actor,
            ),
        )

    def plan_attempt_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> TerminalPublicationDraft:
        return self._collect_draft(
            publication_kind="attempt_failure",
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            occurred_at=occurred_at,
            publish=lambda: self.publish_attempt_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_decision=retry_decision,
                policy=policy,
                occurred_at=occurred_at,
                actor=actor,
            ),
        )

    def plan_run_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        attempt_failure_artifact_id: str | None,
        occurred_at: str,
        actor: AuditActor,
    ) -> TerminalPublicationDraft:
        return self._collect_draft(
            publication_kind="run_failure",
            run_id=run.run_id,
            attempt_no=attempt.attempt_no if attempt is not None else None,
            occurred_at=occurred_at,
            publish=lambda: self.publish_run_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_decision=retry_decision,
                policy=policy,
                attempt_failure_artifact_id=attempt_failure_artifact_id,
                occurred_at=occurred_at,
                actor=actor,
            ),
        )

    def plan_active_failure_aggregate(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        attempt_policy: OutcomeArtifactPolicyV1,
        run_policy: OutcomeArtifactPolicyV1 | None,
        occurred_at: str,
        actor: AuditActor,
    ) -> tuple[TerminalPublicationDraft, ...]:
        """Plan attempt-close and optional run-close as one immutable aggregate.

        The run-scope failure must validate and reference the not-yet-committed
        attempt failure.  A private exact Artifact+bytes overlay supplies that parent
        only while the second pure projection runs; nothing is inserted into DB until
        both drafts have been staged and reproduced in the single write UoW.
        """

        attempt_draft = self.plan_attempt_failure(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=retry_decision,
            policy=attempt_policy,
            occurred_at=occurred_at,
            actor=actor,
        )
        if retry_decision.decision == "retry":
            if run_policy is not None:
                raise IntegrityViolation("retry aggregate cannot include a run failure policy")
            return (attempt_draft,)
        if run_policy is None:
            raise IntegrityViolation("terminal failure aggregate requires a run policy")
        attempt_result = attempt_draft.result
        if not isinstance(attempt_result, AttemptFailurePublication):
            raise IntegrityViolation("attempt failure draft returned another result type")
        material_by_slot = {item.slot: item for item in attempt_draft.materials}
        overlay: dict[str, tuple[ArtifactV2, bytes]] = {}
        for operation in attempt_draft.operations:
            if not isinstance(operation, _ArtifactWrite):
                continue
            material = material_by_slot.get(operation.slot)
            if material is None or material.expected_ref != operation.artifact.object_ref:
                raise IntegrityViolation("attempt draft Artifact has no exact material")
            overlay[operation.artifact.artifact_id] = (operation.artifact, material.payload)
        if attempt_result.failure_artifact_id not in overlay:
            raise IntegrityViolation("attempt draft omitted its failure manifest Artifact")
        previous_overlay = self._planned_artifact_overlay
        self._planned_artifact_overlay = {**previous_overlay, **overlay}
        try:
            run_draft = self.plan_run_failure(
                run=run,
                attempt=attempt,
                prepared=prepared,
                retry_decision=retry_decision,
                policy=run_policy,
                attempt_failure_artifact_id=attempt_result.failure_artifact_id,
                occurred_at=occurred_at,
                actor=actor,
            )
        finally:
            self._planned_artifact_overlay = previous_overlay
        return (attempt_draft, run_draft)

    def commit(
        self,
        fresh_draft: TerminalPublicationDraft,
        staged: StagedTerminalPublication,
    ) -> object:
        """Commit one freshly reprojected draft without any ObjectStore write.

        The caller must build ``fresh_draft`` from the current write-UoW snapshot.
        This method checks the complete canonical digest and exact slot/ref closure,
        then performs only transaction-bound repository/effect/audit operations.
        """

        return self.commit_many(((fresh_draft, staged),))[0]

    def commit_many(
        self,
        publications: tuple[tuple[TerminalPublicationDraft, StagedTerminalPublication], ...],
    ) -> tuple[object, ...]:
        """Validate a complete aggregate before executing its first DB write."""

        validated: list[tuple[TerminalPublicationDraft, Mapping[str, StagedReceipt]]] = []
        for fresh_draft, staged in publications:
            current_operation_projection = tuple(
                _operation_projection(operation) for operation in fresh_draft.operations
            )
            current_result_projection = _model_projection(fresh_draft.result)
            if (
                current_operation_projection != fresh_draft.operation_projection
                or current_result_projection != fresh_draft.result_projection
            ):
                raise IntegrityViolation(
                    "terminal draft operations/result mutated after projection",
                    run_id=fresh_draft.run_id,
                )
            # Rebuild every material so payload/ref validation cannot be bypassed by
            # mutating a nested object after the read phase.
            materials_tuple = tuple(
                BlobMaterial(
                    slot=material.slot,
                    payload=bytes(material.payload),
                    expected_ref=material.expected_ref,
                )
                for material in fresh_draft.materials
            )
            checked = TerminalPublicationDraft(
                publication_kind=fresh_draft.publication_kind,
                run_id=fresh_draft.run_id,
                attempt_no=fresh_draft.attempt_no,
                occurred_at=fresh_draft.occurred_at,
                projection_digest=fresh_draft.projection_digest,
                materials=materials_tuple,
                operations=fresh_draft.operations,
                operation_projection=current_operation_projection,
                result_projection=current_result_projection,  # type: ignore[arg-type]
                result=fresh_draft.result,
            )
            if staged.projection_digest != checked.projection_digest:
                raise IntegrityViolation(
                    "fresh terminal projection differs from the staged projection",
                    run_id=checked.run_id,
                )
            receipts = {receipt.slot: receipt for receipt in staged.receipts}
            materials = {material.slot: material for material in checked.materials}
            if set(receipts) != set(materials):
                raise IntegrityViolation(
                    "staged receipt slots do not exactly close the fresh draft",
                    run_id=checked.run_id,
                )
            artifact_slots = tuple(
                operation.slot
                for operation in checked.operations
                if isinstance(operation, _ArtifactWrite)
            )
            if len(artifact_slots) != len(set(artifact_slots)) or set(artifact_slots) != set(
                materials
            ):
                raise IntegrityViolation(
                    "fresh draft blob slots do not map one-to-one onto Artifact writes",
                    run_id=checked.run_id,
                )
            for slot, material in materials.items():
                receipt = receipts[slot]
                if receipt.ref != material.expected_ref:
                    raise IntegrityViolation(
                        "staged receipt ref differs from the fresh draft material",
                        slot=slot,
                    )
            validated.append((checked, receipts))

        for checked, receipts in validated:
            self._commit_operations(checked.operations, receipts=receipts)
        return tuple(checked.result for checked, _ in validated)

    def _commit_operations(
        self,
        operations: Sequence[object],
        *,
        receipts: Mapping[str, StagedReceipt],
    ) -> None:
        for operation in operations:
            if isinstance(operation, _ArtifactWrite):
                receipt = receipts.get(operation.slot)
                if receipt is None:
                    raise IntegrityViolation(
                        "Artifact write has no exact staged receipt",
                        slot=operation.slot,
                    )
                stored = self._artifacts.put_staged(operation.artifact, receipt)
                if not _same_immutable_artifact(stored, operation.artifact):
                    raise IntegrityViolation(
                        "Artifact store returned a different immutable Artifact",
                        expected_artifact_id=operation.artifact.artifact_id,
                    )
                continue
            if isinstance(operation, _FindingWrite):
                planned = operation.planned
                stored_revision = self._findings.put(
                    planned.revision,
                    expected_current_revision=planned.expected_current_revision,
                )
                if stored_revision != planned.revision:
                    raise IntegrityViolation(
                        "Finding store returned a different immutable revision"
                    )
                stored_link = self._ledger.put_finding_link(planned.link)
                if stored_link != planned.link:
                    raise IntegrityViolation(
                        "Finding-link ledger returned a different immutable link"
                    )
                continue
            if isinstance(operation, _WorkflowWrite):
                apply_workflow_effect(operation.effect_key, operation.context)
                continue
            if isinstance(operation, _AuditWrite):
                self._audit.record(
                    action=operation.action,
                    run=operation.run,
                    artifact_id=operation.artifact_id,
                    actor=operation.actor,
                    occurred_at=operation.occurred_at,
                )
                continue
            raise IntegrityViolation(
                "terminal commit contains an unknown operation",
                operation_type=type(operation).__qualname__,
            )

    # --------------------------------------------------------------- preflight
    def preflight_outcome(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunOutcome,
    ) -> PreparedRunOutcome:
        """Freeze validation subject authority before publishing any outcome data.

        A validation subject may be superseded while its worker is running.  The
        ApprovalItem/SubjectHead read here uses the lifecycle's terminal UoW, before
        any prepared Artifact is consumed.  A strictly proven supersede discards all
        prepared business evidence and becomes the retained typed terminal failure;
        every other binding drift fails closed.
        """

        if (
            prepared.run_id != run.run_id
            or prepared.run_kind != run.kind
            or prepared.attempt_no != (attempt.attempt_no if attempt is not None else None)
        ):
            raise IntegrityViolation("prepared outcome differs from the authoritative Run attempt")

        payload = run.payload.params
        if not isinstance(
            payload,
            (
                PatchValidationPayloadV1,
                ConstraintValidationPayloadV1,
                RollbackValidationPayloadV1,
            ),
        ):
            return prepared
        if self._approvals is None:
            raise IntegrityViolation(
                "validation outcome preflight requires transaction-bound approvals",
                run_id=run.run_id,
            )

        subject = payload.subject
        if subject.active_validation_run_id != run.run_id:
            raise IntegrityViolation(
                "validation outcome subject is bound to another Run",
                run_id=run.run_id,
            )
        item = self._approvals.get(subject.approval_id)
        if item is None:
            raise IntegrityViolation(
                "validation outcome ApprovalItem is missing",
                approval_id=subject.approval_id,
            )
        validate_immutable_subject_binding(item, subject, payload_subject_kind(payload))
        head = self._approvals.get_subject_head(item.subject_series_id)
        if head is None:
            raise IntegrityViolation("validation subject series has no SubjectHead")

        if head.current_approval_id == item.approval_id:
            validate_current_subject_binding(item, head, subject, run.run_id)
            return prepared

        validate_strict_superseded_subject_binding(item, head, subject)
        return PreparedRunFailure(
            run_id=run.run_id,
            attempt_no=attempt.attempt_no if attempt is not None else None,
            run_kind=run.kind,
            artifacts=(),
            requirement_dispositions=(),
            cause_code="subject_superseded",
            failure_class="subject_superseded",
            intrinsic_retry_eligible=False,
            classifier=run.failure_classifier,
            redacted_message="validation subject was superseded",
        )

    # ------------------------------------------------------------------ audit
    def record_attempt_started(self, **kwargs: object) -> None:
        self._record_event("run.attempt_started", kwargs)

    def record_attempt_progress(self, **kwargs: object) -> None:
        self._record_event("run.attempt_progress", kwargs)

    def record_attempt_closed(self, **kwargs: object) -> None:
        self._record_event("run.attempt_closed", kwargs)

    def record_run_terminal(self, **kwargs: object) -> None:
        self._record_event("run.terminal", kwargs)

    def _record_event(self, action: str, kwargs: Mapping[str, object]) -> None:
        run = kwargs.get("run")
        actor = kwargs.get("actor")
        event = kwargs.get("event")
        occurred_at = getattr(event, "occurred_at", None) if event is not None else None
        if isinstance(run, RunRecord) and isinstance(actor, AuditActor):
            self._audit.record(
                action=action,
                run=run,
                artifact_id=None,
                actor=actor,
                occurred_at=occurred_at or run.updated_at,
            )

    # ----------------------------------------------------------------- success
    def publish_run_result(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunResult,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> RunResultPublication:
        if self._collector is None:
            raise IntegrityViolation(
                "direct terminal publication is forbidden; plan, stage, and commit explicitly"
            )
        definition = resolve_definition(registry=self._registry, run=run)
        plan = build_publication_plan(
            registry=self._registry, definition=definition, policy=policy, scope="run"
        )
        self._verify_success_metadata(run=run, attempt=attempt, prepared=prepared, policy=policy)

        views = self._read_views(prepared.artifacts)
        primary_payload = dict(views[prepared.primary_index].payload)
        allocations = allocate_artifacts(plan_rules=plan.plan_rules, artifacts=views)
        self._validate_cardinalities(
            allocations=allocations,
            views=views,
            run=run,
            primary_payload=primary_payload,
            dispositions=prepared.requirement_dispositions,
            defer_artifact_id_identity=True,
        )
        closed = self._aggregate_closed_attempts(
            run.run_id,
            expected_closed_through=attempt.attempt_no - 1,
            current_attempt_no=None,
            current_attempt_failure_id=None,
        )
        runtime = self._validated_runtime_parents(
            run=run,
            plan=plan,
            manifest_scope="run",
            current_attempt_no=attempt.attempt_no,
            closed=closed,
        )

        published = self._publish_domain_artifacts(
            run=run,
            plan=plan,
            allocations=allocations,
            views=views,
            runtime=runtime,
            occurred_at=occurred_at,
            dispositions=prepared.requirement_dispositions,
        )
        final_primary_payload = dict(published.payloads_by_index[prepared.primary_index])
        self._validate_cardinalities(
            allocations=allocations,
            views=views,
            run=run,
            primary_payload=final_primary_payload,
            dispositions=prepared.requirement_dispositions,
            published_artifact_ids_by_index=published.index_to_id,
        )
        primary_rule_id = self._primary_rule_id(plan)
        primary_artifact_id = published.ids_by_rule[primary_rule_id][0]
        if views[prepared.primary_index].index not in published.index_to_id:  # defensive
            raise IntegrityViolation("primary prepared artifact was not published")
        if published.index_to_id[prepared.primary_index] != primary_artifact_id:
            raise IntegrityViolation("primary artifact id differs from the primary rule output")

        finding_count = self._publish_findings(
            run=run,
            attempt=attempt,
            prepared=prepared,
            plan=plan,
            allocations=allocations,
            published=published,
            occurred_at=occurred_at,
        )

        output_parents = self._domain_manifest_parents(published)
        projection = self._manifest_projection(
            run=run,
            attempt_no=attempt.attempt_no,
            scope="run",
            transition_policy=plan.transition_policy,
            transition_ref=plan.policy.version_transition_policy_ref,
            extra_parents=(*output_parents, *runtime.parents),
            execution_identity=runtime.execution_identity,
            cassette_ids_by_scope=runtime.cassette_ids_by_scope,
        )
        produced_ids = tuple(
            parent.artifact_id
            for parent in projection.parents
            if parent.publication == "run_published" and parent.role != "input"
        )
        result = RunResultV1(
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            run_kind=run.kind,
            primary_artifact_id=primary_artifact_id,
            produced_artifact_ids=produced_ids,
            finding_count=finding_count,
            outcome_code=policy.outcome_code,
            summary=RunResultSummaryV1(
                outcome_code=policy.outcome_code,
                primary_artifact_kind=prepared.summary.primary_artifact_kind,
                produced_artifact_count=len(produced_ids),
                finding_count=finding_count,
            ),
            requirement_dispositions=prepared.requirement_dispositions,
            version_projection=projection,
        )
        manifest_id = self._publish_manifest(
            kind="run_result",
            payload=result.model_dump(mode="json"),
            version_tuple=projection.terminal_version_tuple,
            parents=projection.parents,
            expected_projection=projection,
            occurred_at=occurred_at,
            execution_identity=runtime.execution_identity,
            replayability=self._replayability_for(run),
            llm_execution_mode=run.payload.llm_execution_mode,
        )

        workflow_context = WorkflowEffectContext(
            run=run,
            policy=policy,
            scope="run",
            published_primary_artifact_id=primary_artifact_id,
            published_output_artifact_ids=produced_ids,
            approvals=self._approvals,
            actor=actor,
            occurred_at=occurred_at,
            published_primary_payload=final_primary_payload,
            published_artifact_ids_by_rule=published.ids_by_rule,
            published_payloads_by_rule=published.payloads_by_rule,
            published_artifacts_by_rule=published.artifacts_by_rule,
            agent_drafts=self._agent_drafts,
            auto_apply=self._auto_apply,
        )
        if self._collector is None:  # pragma: no cover - public guard above
            raise IntegrityViolation("success publication has no active draft collector")
        self._collector.add_workflow(policy.workflow_effect_key, workflow_context)
        self._collector.add_audit(
            action=definition.terminal_hooks.on_success,
            run=run,
            artifact_id=manifest_id,
            actor=actor,
            occurred_at=occurred_at,
        )
        return RunResultPublication(
            result_artifact_id=manifest_id,
            attempt_cassette_artifact_id=runtime.attempt_cassette_artifact_id,
            terminal_cassette_artifact_id=runtime.terminal_cassette_artifact_id,
        )

    # ------------------------------------------------------- attempt failure
    def publish_attempt_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> AttemptFailurePublication:
        if self._collector is None:
            raise IntegrityViolation(
                "direct terminal publication is forbidden; plan, stage, and commit explicitly"
            )
        definition = resolve_definition(registry=self._registry, run=run)
        plan = build_publication_plan(
            registry=self._registry, definition=definition, policy=policy, scope="attempt"
        )
        self._verify_failure_metadata(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=retry_decision,
            policy=policy,
            scope="attempt",
        )
        if retry_decision.decision == "retry" and (
            prepared.artifacts or prepared.requirement_dispositions
        ):
            raise IntegrityViolation(
                "retrying attempt cannot discard prepared artifacts or dispositions"
            )
        # attempt-close policies never consume business evidence/dispositions.
        runtime = self._validated_runtime_parents(
            run=run,
            plan=plan,
            manifest_scope="attempt",
            current_attempt_no=attempt.attempt_no,
            closed={},
        )
        projection = self._manifest_projection(
            run=run,
            attempt_no=attempt.attempt_no,
            scope="attempt",
            transition_policy=plan.transition_policy,
            transition_ref=plan.policy.version_transition_policy_ref,
            extra_parents=runtime.parents,
            execution_identity=runtime.execution_identity,
            cassette_ids_by_scope=runtime.cassette_ids_by_scope,
        )
        evidence_ids = tuple(
            parent.artifact_id
            for parent in projection.parents
            if parent.publication == "run_published" and parent.role != "input"
        )
        failure = self._build_run_failure(
            run=run,
            attempt_no=attempt.attempt_no,
            prepared=prepared,
            retry_decision=retry_decision,
            evidence_ids=evidence_ids,
            dispositions=(),
            occurred_at=occurred_at,
            projection=projection,
        )
        manifest_id = self._publish_manifest(
            kind="run_failure",
            payload=failure.model_dump(mode="json"),
            version_tuple=projection.terminal_version_tuple,
            parents=projection.parents,
            expected_projection=projection,
            occurred_at=occurred_at,
            execution_identity=runtime.execution_identity,
            replayability=self._replayability_for(run),
            llm_execution_mode=run.payload.llm_execution_mode,
        )
        workflow_context = WorkflowEffectContext(
            run=run,
            policy=policy,
            scope="attempt",
            published_primary_artifact_id=None,
            published_output_artifact_ids=(),
            approvals=self._approvals,
            actor=actor,
            occurred_at=occurred_at,
        )
        if self._collector is None:  # pragma: no cover - public guard above
            raise IntegrityViolation("attempt publication has no active draft collector")
        self._collector.add_workflow(policy.workflow_effect_key, workflow_context)
        self._collector.add_audit(
            action="run.attempt_failure",
            run=run,
            artifact_id=manifest_id,
            actor=actor,
            occurred_at=occurred_at,
        )
        return AttemptFailurePublication(
            failure_artifact_id=manifest_id,
            cassette_bundle_artifact_id=runtime.attempt_cassette_artifact_id,
        )

    # ----------------------------------------------------------- run failure
    def publish_run_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        attempt_failure_artifact_id: str | None,
        occurred_at: str,
        actor: AuditActor,
    ) -> RunFailurePublication:
        if self._collector is None:
            raise IntegrityViolation(
                "direct terminal publication is forbidden; plan, stage, and commit explicitly"
            )
        definition = resolve_definition(registry=self._registry, run=run)
        plan = build_publication_plan(
            registry=self._registry, definition=definition, policy=policy, scope="run"
        )
        attempt_no = attempt.attempt_no if attempt is not None else None
        self._verify_failure_metadata(
            run=run,
            attempt=attempt,
            prepared=prepared,
            retry_decision=retry_decision,
            policy=policy,
            scope="run",
        )

        views = self._read_views(prepared.artifacts)
        primary_payload = None
        allocations = allocate_artifacts(plan_rules=plan.plan_rules, artifacts=views)
        self._validate_cardinalities(
            allocations=allocations,
            views=views,
            run=run,
            primary_payload=primary_payload,
            dispositions=prepared.requirement_dispositions,
            defer_artifact_id_identity=True,
        )
        closed = self._aggregate_closed_attempts(
            run.run_id,
            expected_closed_through=(
                (attempt_no - 1)
                if attempt_no is not None and attempt_failure_artifact_id is not None
                else (attempt_no or 0)
            ),
            current_attempt_no=attempt_no,
            current_attempt_failure_id=attempt_failure_artifact_id,
        )
        runtime = self._validated_runtime_parents(
            run=run,
            plan=plan,
            manifest_scope="run",
            current_attempt_no=attempt_no,
            closed=closed,
        )
        published = self._publish_domain_artifacts(
            run=run,
            plan=plan,
            allocations=allocations,
            views=views,
            runtime=runtime,
            occurred_at=occurred_at,
            dispositions=prepared.requirement_dispositions,
        )
        self._validate_cardinalities(
            allocations=allocations,
            views=views,
            run=run,
            primary_payload=primary_payload,
            dispositions=prepared.requirement_dispositions,
            published_artifact_ids_by_index=published.index_to_id,
        )

        extra_parents = [*self._domain_manifest_parents(published), *runtime.parents]
        projection = self._manifest_projection(
            run=run,
            attempt_no=attempt_no,
            scope="run",
            transition_policy=plan.transition_policy,
            transition_ref=plan.policy.version_transition_policy_ref,
            extra_parents=tuple(extra_parents),
            execution_identity=runtime.execution_identity,
            cassette_ids_by_scope=runtime.cassette_ids_by_scope,
        )
        evidence_ids = tuple(
            parent.artifact_id
            for parent in projection.parents
            if parent.publication == "run_published" and parent.role != "input"
        )
        failure = self._build_run_failure(
            run=run,
            attempt_no=attempt_no,
            prepared=prepared,
            retry_decision=retry_decision,
            evidence_ids=evidence_ids,
            dispositions=prepared.requirement_dispositions,
            occurred_at=occurred_at,
            projection=projection,
        )
        manifest_id = self._publish_manifest(
            kind="run_failure",
            payload=failure.model_dump(mode="json"),
            version_tuple=projection.terminal_version_tuple,
            parents=projection.parents,
            expected_projection=projection,
            occurred_at=occurred_at,
            execution_identity=runtime.execution_identity,
            replayability=self._replayability_for(run),
            llm_execution_mode=run.payload.llm_execution_mode,
        )
        workflow_context = WorkflowEffectContext(
            run=run,
            policy=policy,
            scope="run",
            # For a validation run-final failure the just-published run_failure
            # manifest IS the ``last_validation_failure_artifact_id`` the
            # ``restore_current_draft@1`` revert records (spec §"validation
            # execution failure"). Non-validation failures ignore it (no-op).
            published_primary_artifact_id=manifest_id,
            published_output_artifact_ids=evidence_ids,
            approvals=self._approvals,
            actor=actor,
            occurred_at=occurred_at,
            published_primary_payload=primary_payload,
        )
        if self._collector is None:  # pragma: no cover - public guard above
            raise IntegrityViolation("run failure publication has no active draft collector")
        self._collector.add_workflow(policy.workflow_effect_key, workflow_context)
        self._collector.add_audit(
            action="run.failure",
            run=run,
            artifact_id=manifest_id,
            actor=actor,
            occurred_at=occurred_at,
        )
        return RunFailurePublication(
            failure_artifact_id=manifest_id,
            terminal_cassette_artifact_id=runtime.terminal_cassette_artifact_id,
        )

    # -------------------------------------------------------------- internals
    def _verify_success_metadata(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunResult,
        policy: OutcomeArtifactPolicyV1,
    ) -> None:
        if prepared.run_id != run.run_id or prepared.attempt_no != attempt.attempt_no:
            raise IntegrityViolation("prepared result differs from the current Run attempt")
        if prepared.run_kind != run.kind:
            raise IntegrityViolation("prepared result Run kind differs from the RunRecord")
        if prepared.summary.outcome_code != policy.outcome_code:
            raise IntegrityViolation("prepared summary outcome differs from the selected policy")
        if (
            policy.prepared_outcome != "success"
            or policy.publication_scope != "run"
            or policy.run_status_after_publication != "succeeded"
            or policy.attempt_terminal_status is not None
            or policy.failure_class is not None
            or policy.retry_disposition is not None
        ):
            raise IntegrityViolation("selected policy is not an exact success selector")
        if prepared.summary.prepared_domain_artifact_count != len(prepared.artifacts):
            raise IntegrityViolation("prepared domain artifact count is fabricated")
        if prepared.summary.prepared_finding_count != len(prepared.findings):
            raise IntegrityViolation("prepared finding count is fabricated")
        if not 0 <= prepared.primary_index < len(prepared.artifacts):
            raise IntegrityViolation("prepared primary artifact index is out of bounds")
        if (
            prepared.summary.primary_artifact_kind
            != prepared.artifacts[prepared.primary_index].kind
        ):
            raise IntegrityViolation("prepared primary Artifact kind is fabricated")

    def _verify_failure_metadata(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        scope: str,
    ) -> None:
        classifier = self._registry.get_failure_classifier(run.failure_classifier)
        retry_policy = self._registry.get_retry_policy(run.retry_policy)
        if classifier is None or retry_policy is None:
            raise IntegrityViolation("failure publication bindings are not retained exactly")
        validate_prepared_failure(
            run=run,
            attempt=attempt,
            prepared=prepared,
            classifier=classifier,
        )
        if (
            retry_policy.retry_policy_id != run.retry_policy.retry_policy_id
            or retry_policy.retry_policy_version != run.retry_policy.retry_policy_version
            or retry_policy.retry_policy_digest != run.retry_policy.retry_policy_digest
            or retry_policy.max_attempts != run.max_attempts
        ):
            raise IntegrityViolation("failure publication retry policy differs from the Run")
        if (
            retry_decision.cause_code != prepared.cause_code
            or retry_decision.failure_class != prepared.failure_class
            or retry_decision.intrinsic_retry_eligible != prepared.intrinsic_retry_eligible
            or retry_decision.classifier != run.failure_classifier
            or retry_decision.retry_policy != run.retry_policy
        ):
            raise IntegrityViolation("RetryDecision differs from the prepared/frozen failure")
        expected_disposition = "retry" if retry_decision.decision == "retry" else "terminal"
        expected_attempt_status = _failure_attempt_status(prepared)
        expected_run_status = (
            "retry_wait"
            if retry_decision.decision == "retry"
            else _failure_run_status(prepared, retry_decision)
        )
        active_attempt = (
            attempt is not None
            and run.status in {"leased", "running"}
            and run.current_attempt_no == attempt.attempt_no
        )
        if scope == "attempt" and not active_attempt:
            raise IntegrityViolation(
                "attempt-scope failure publication requires the current active attempt"
            )
        if (
            policy.prepared_outcome != "failure"
            or policy.publication_scope != scope
            or policy.outcome_code != prepared.cause_code
            or policy.failure_class != prepared.failure_class
            or policy.retry_disposition != expected_disposition
            or policy.run_status_after_publication != expected_run_status
            or policy.attempt_terminal_status
            != (expected_attempt_status if active_attempt else None)
        ):
            raise IntegrityViolation("selected policy differs from the exact failure selector")

    def _read_views(self, artifacts: Sequence[object]) -> tuple[PreparedArtifactView, ...]:
        views: list[PreparedArtifactView] = []
        for index, prepared in enumerate(artifacts):
            # ``ObjectRef`` proves content identity while ``ObjectLocation`` proves
            # the exact backend generation the worker actually wrote.  Requiring
            # both prevents a prepared outcome from self-reporting an unused or
            # stale location while the publisher silently reads another binding.
            blob = self._blobs.read(prepared.object_ref, prepared.location)
            digest = sha256_lowerhex(blob)
            if digest != prepared.payload_hash or digest != prepared.object_ref.sha256:
                raise IntegrityViolation(
                    "prepared artifact blob hash differs from its declared payload hash",
                    artifact_index=index,
                )
            if prepared.object_ref.size_bytes != len(blob):
                raise IntegrityViolation(
                    "prepared artifact blob size differs from its ObjectRef", artifact_index=index
                )
            payload = decode_and_validate_artifact_payload(
                payload_schema_id=prepared.payload_schema_id,
                blob=blob,
                external_decoders=self._payload_decoders,
            )
            meta = dict(prepared.meta)
            if meta.get("payload_schema_id") != prepared.payload_schema_id:
                raise IntegrityViolation(
                    "prepared artifact meta must declare its exact payload schema id",
                    artifact_index=index,
                )
            views.append(
                PreparedArtifactView(
                    index=index,
                    kind=prepared.kind,
                    payload_schema_id=prepared.payload_schema_id,
                    version_tuple=prepared.version_tuple,
                    lineage=tuple(prepared.lineage),
                    payload_hash=prepared.payload_hash,
                    object_ref=prepared.object_ref,
                    location=prepared.location,
                    meta=meta,
                    payload=payload,
                    blob=blob,
                )
            )
        return tuple(views)

    def _validate_cardinalities(
        self,
        *,
        allocations: Sequence[RuleAllocation],
        views: Sequence[PreparedArtifactView],
        run: RunRecord,
        primary_payload: Mapping[str, object] | None,
        dispositions: Sequence[RequirementDispositionV1],
        published_artifact_ids_by_index: Mapping[int, str] | None = None,
        defer_artifact_id_identity: bool = False,
    ) -> None:
        by_index = {view.index: view for view in views}
        if len(by_index) != len(views):
            raise IntegrityViolation("prepared artifact indexes are not unique")
        if published_artifact_ids_by_index is not None:
            validate_published_artifact_ids(
                artifacts=views,
                published_artifact_ids_by_index=published_artifact_ids_by_index,
            )
        run_payload = run.payload.model_dump(mode="python")
        snapshots = _snapshots_by_id(run.payload.resolved_policy_snapshots)
        validate_requirement_profile_bindings(
            snapshots=run.payload.resolved_policy_snapshots,
            resolved_profiles=run.payload.resolved_profiles,
        )
        validate_plan_dispositions(
            plan_rules=tuple(allocation.plan_rule for allocation in allocations),
            snapshots_by_id=snapshots,
            dispositions=dispositions,
        )
        for allocation in allocations:
            validate_rule_cardinality(
                allocation=allocation,
                artifacts_by_index=by_index,
                run_payload=run_payload,
                primary_payload=primary_payload,
                snapshots_by_id=snapshots,
                dispositions=dispositions,
                published_artifact_ids_by_index=published_artifact_ids_by_index,
                defer_artifact_id_identity=defer_artifact_id_identity,
            )

    def _publish_domain_artifacts(
        self,
        *,
        run: RunRecord,
        plan: PublicationPlan,
        allocations: Sequence[RuleAllocation],
        views: Sequence[PreparedArtifactView],
        runtime: _TerminalRuntimeProjection,
        occurred_at: str,
        dispositions: Sequence[RequirementDispositionV1],
    ) -> "_PublishedArtifacts":
        by_index = {view.index: view for view in views}
        if not any(allocation.artifact_indexes for allocation in allocations):
            return _PublishedArtifacts(
                ids_by_rule={},
                index_to_id={},
                roles={},
                payloads_by_index={},
                payloads_by_rule={},
                artifacts_by_rule={},
            )
        run_inputs = self._input_parents(run.payload.input_artifact_ids)
        run_intermediates = self._intermediate_parents(run.run_id)
        siblings: dict[str, dict[str, ParentInfo]] = {}
        ids_by_rule: dict[str, list[str]] = {}
        prepared_to_final_ids_by_rule: dict[str, dict[str, str]] = {}
        final_sibling_facts_by_id: dict[str, FinalSiblingFact] = {}
        index_to_id: dict[int, str] = {}
        roles: dict[str, str] = {}
        payloads_by_index: dict[int, Mapping[str, object]] = {}
        payloads_by_rule: dict[str, list[Mapping[str, object]]] = {}
        artifacts_by_rule: dict[str, list[ArtifactV2]] = {}
        related_payloads_by_rule = {
            allocation.plan_rule.rule.rule_id: tuple(
                by_index[index].payload for index in allocation.artifact_indexes
            )
            for allocation in allocations
        }

        for allocation in _topological_rule_order(allocations, plan):
            rule = allocation.plan_rule.rule
            lineage_policy = plan.lineage_by_rule_id[rule.rule_id]
            ids_by_rule.setdefault(rule.rule_id, [])
            payloads_by_rule.setdefault(rule.rule_id, [])
            artifacts_by_rule.setdefault(rule.rule_id, [])
            prepared_to_final_ids_by_rule.setdefault(rule.rule_id, {})
            for index in allocation.artifact_indexes:
                view = by_index[index]
                payload = dict(view.payload)
                object_ref = view.object_ref
                payload_hash = view.payload_hash
                sibling_candidates = {
                    artifact_id: info
                    for by_id in siblings.values()
                    for artifact_id, info in by_id.items()
                }
                available_references = _merge_parent_sources(
                    run_inputs,
                    run_intermediates,
                    sibling_candidates,
                )
                child_references, referenced_ids = resolve_child_payload_references(
                    policy=lineage_policy,
                    child_payload=payload,
                    available_parents=available_references,
                )
                sources = LineageParentSources(
                    run_inputs=run_inputs,
                    run_intermediates=run_intermediates,
                    prepared_siblings={key: dict(value) for key, value in siblings.items()},
                    child_payload_references=child_references,
                )
                # Inject the content-addressed sibling ids the handler could not
                # compute (a ``prepared_rule`` parent is minted only here). The
                # topological walk guarantees each parent rule is minted before this
                # child, so ``siblings[source_rule_id]`` is already populated; the
                # child's bare handler lineage is completed with those exact ids so
                # e.g. an EvidenceSet links its ``regression`` siblings and a preview
                # links its ``patch`` sibling.
                child_lineage = _inject_prepared_siblings(
                    child_lineage=view.lineage,
                    lineage_policy=lineage_policy,
                    siblings=siblings,
                )
                child_lineage = tuple(dict.fromkeys((*child_lineage, *referenced_ids)))
                typed = project_typed_lineage(
                    policy=lineage_policy,
                    child_kind=view.kind,
                    child_payload_schema_id=view.payload_schema_id,
                    child_lineage=child_lineage,
                    sources=sources,
                )
                requires_identity = self._producer_facts.requires_identity(
                    run_kind=run.kind,
                    policy=plan.policy,
                    rule=rule,
                    payload_schema_id=view.payload_schema_id,
                )
                producer_identity: ExecutionIdentityV1 | None = None
                cassette_id: str | None = None
                if requires_identity and run.payload.llm_execution_mode != "not_applicable":
                    producer_identity = self._artifact_execution_identity(
                        run=run,
                        runtime=runtime,
                    )
                    if run.payload.llm_execution_mode == "record":
                        cassette_id = runtime.cassette_ids_by_scope.get("run_bundle")
                    elif run.payload.llm_execution_mode == "replay":
                        cassette_id = runtime.cassette_ids_by_scope.get("replay_input")
                producer_env_contract_version = self._config_export_producer_env(
                    run=run,
                    rule=rule,
                    payload_schema_id=view.payload_schema_id,
                    payload=payload,
                )
                producer_facts = self._producer_facts.resolve(
                    run=run,
                    policy=plan.policy,
                    rule=rule,
                    lineage_policy=lineage_policy,
                    payload_schema_id=view.payload_schema_id,
                    canonical_payload=payload,
                    execution_identity=producer_identity,
                    cassette_id=cassette_id,
                    producer_env_contract_version=producer_env_contract_version,
                )
                expected_tuple = project_domain_version_tuple(
                    policy=lineage_policy,
                    parent_tuples={
                        role: tuple(info.version_tuple for info in parents)
                        for role, parents in typed.parents_by_role.items()
                    },
                    producer_tuple=producer_facts.producer_tuple,
                )
                payload = bind_final_payload_references(
                    run=run,
                    outcome_policy=plan.policy,
                    outcome_rule=rule,
                    payload_schema_id=view.payload_schema_id,
                    canonical_payload=payload,
                    projected_tuple=expected_tuple,
                    final_artifact_ids_by_rule=ids_by_rule,
                    final_sibling_facts_by_id=final_sibling_facts_by_id,
                    prepared_to_final_artifact_ids_by_rule=(prepared_to_final_ids_by_rule),
                    requirement_dispositions=dispositions,
                )
                blob = (
                    encode_validated_artifact_payload(
                        payload_schema_id=view.payload_schema_id,
                        payload=payload,
                    )
                    if payload != view.payload
                    else view.blob
                )
                if not blob:
                    raise IntegrityViolation(
                        "prepared artifact view omitted its exact source bytes",
                        artifact_index=index,
                        rule_id=rule.rule_id,
                    )
                if self._collector is None:  # pragma: no cover - public guard above
                    raise IntegrityViolation("domain publication has no active draft collector")
                object_ref = self._collector.add_blob(blob)
                payload_hash = object_ref.sha256
                prepared_expected_tuple = expected_tuple
                if producer_facts.execution_identity is not None:
                    terminal_identity_fields = (
                        "prompt_version",
                        "model_snapshot",
                        "agent_graph_version",
                        "cassette_id",
                    )
                    if any(
                        getattr(view.version_tuple, field) is not None
                        for field in terminal_identity_fields
                    ):
                        raise IntegrityViolation(
                            "prepared domain VersionTuple self-reports terminal execution identity",
                            artifact_index=index,
                            rule_id=rule.rule_id,
                        )
                    prepared_expected_tuple = expected_tuple.model_copy(
                        update={field: None for field in terminal_identity_fields}
                    )
                if prepared_expected_tuple != view.version_tuple:
                    raise IntegrityViolation(
                        "prepared VersionTuple differs from its worker-checkable projection",
                        artifact_index=index,
                        rule_id=rule.rule_id,
                    )
                checked_meta = validate_domain_payload_bindings(
                    run=run,
                    outcome_policy=plan.policy,
                    outcome_rule=rule,
                    payload_schema_id=view.payload_schema_id,
                    canonical_payload=payload,
                    typed_lineage=typed,
                    projected_tuple=expected_tuple,
                    prepared_meta=view.meta,
                    related_payloads_by_rule=related_payloads_by_rule,
                )
                final_meta = producer_facts.authoritative_meta(checked_meta)
                artifact = build_artifact_v2(
                    kind=view.kind,
                    version_tuple=expected_tuple,
                    lineage=child_lineage,
                    payload_hash=payload_hash,
                    object_ref=object_ref,
                    meta=final_meta,
                    created_at=occurred_at,
                )
                report = validate_domain_artifact_producer(
                    artifact,
                    facts=producer_facts,
                    lineage_policy=lineage_policy,
                    projected_tuple=expected_tuple,
                )
                if report.status != "valid":  # pragma: no cover - ArtifactV2 is closed above
                    raise IntegrityViolation(
                        "domain Artifact producer evidence is incomplete",
                        artifact_index=index,
                        rule_id=rule.rule_id,
                    )
                stored = self._collector.add_artifact(artifact)
                ids_by_rule[rule.rule_id].append(stored.artifact_id)
                index_to_id[index] = stored.artifact_id
                roles[stored.artifact_id] = rule.role
                payloads_by_index[index] = payload
                payloads_by_rule[rule.rule_id].append(payload)
                artifacts_by_rule[rule.rule_id].append(stored)
                final_sibling_facts_by_id[stored.artifact_id] = final_sibling_fact_for(
                    run=run,
                    artifact_id=stored.artifact_id,
                    outcome_rule=rule,
                    payload_schema_id=view.payload_schema_id,
                    canonical_payload=payload,
                    payload_hash=payload_hash,
                    authoritative_meta=final_meta,
                )
                if producer_facts.execution_identity is None:
                    prepared_alias = artifact_id_v2_for(
                        kind=view.kind,
                        version_tuple=view.version_tuple,
                        lineage=view.lineage,
                        payload_hash=view.payload_hash,
                        meta=final_meta,
                    )
                    retained_alias = prepared_to_final_ids_by_rule[rule.rule_id].get(prepared_alias)
                    if retained_alias is not None and retained_alias != stored.artifact_id:
                        raise IntegrityViolation(
                            "prepared Artifact identity resolves to multiple final siblings",
                            outcome_rule_id=rule.rule_id,
                            prepared_artifact_id=prepared_alias,
                        )
                    prepared_to_final_ids_by_rule[rule.rule_id][prepared_alias] = stored.artifact_id
                siblings.setdefault(rule.rule_id, {})[stored.artifact_id] = ParentInfo(
                    artifact_id=stored.artifact_id,
                    kind=view.kind,
                    payload_schema_id=view.payload_schema_id,
                    version_tuple=expected_tuple,
                )
        return _PublishedArtifacts(
            ids_by_rule={key: tuple(value) for key, value in ids_by_rule.items()},
            index_to_id=index_to_id,
            roles=roles,
            payloads_by_index=payloads_by_index,
            payloads_by_rule={key: tuple(value) for key, value in payloads_by_rule.items()},
            artifacts_by_rule={key: tuple(value) for key, value in artifacts_by_rule.items()},
        )

    @staticmethod
    def _artifact_execution_identity(
        *,
        run: RunRecord,
        runtime: _TerminalRuntimeProjection,
    ) -> ExecutionIdentityV1:
        """Project the consumed routes that actually produced this attempt's output."""

        identity = runtime.execution_identity
        attempt_no = run.current_attempt_no
        if identity is None or attempt_no is None:
            raise IntegrityViolation(
                "LLM domain Artifact has no current-attempt execution identity"
            )
        bindings = tuple(
            binding
            for binding in identity.bindings
            if binding.attempt_no == attempt_no and binding.response_consumed
        )
        if not bindings:
            raise IntegrityViolation("LLM domain Artifact has no consumed current-attempt response")
        return build_execution_identity(
            scope="artifact",
            bindings=bindings,
            agent_graph_version=identity.agent_graph_version,
        )

    def _config_export_producer_env(
        self,
        *,
        run: RunRecord,
        rule: OutcomeArtifactRuleV1,
        payload_schema_id: str,
        payload: Mapping[str, object],
    ) -> str | None:
        """Resolve per-export environment facts from the exact frozen profile."""

        if payload_schema_id != "config-export-package@1":
            return None
        if rule.artifact_kind != "config_export":
            raise IntegrityViolation("config export schema is bound to another Artifact kind")
        try:
            export_profile = ProfileRefV1.model_validate(payload.get("export_profile"))
        except ValueError as exc:
            raise IntegrityViolation("config export payload has no exact export profile") from exc
        candidate_profiles = getattr(run.payload.params, "candidate_export_profiles", ())
        try:
            profile_index = candidate_profiles.index(export_profile)
        except ValueError as exc:
            raise IntegrityViolation(
                "config export profile is not frozen in the Run payload",
                export_profile=export_profile.model_dump(mode="json"),
            ) from exc
        expected_field_path = f"/params/candidate_export_profiles/{profile_index}"
        bindings = tuple(
            binding
            for binding in run.payload.resolved_profiles
            if binding.field_path == expected_field_path
            and binding.profile == export_profile
            and binding.expected_profile_kind == "config_export"
        )
        if len(bindings) != 1:
            raise IntegrityViolation(
                "config export lacks one exact resolved profile binding",
                export_profile=export_profile.model_dump(mode="json"),
            )
        binding = bindings[0]
        if (
            binding.catalog_version != run.payload.execution_profile_catalog_version
            or binding.catalog_digest != run.payload.execution_profile_catalog_digest
        ):
            raise IntegrityViolation("config export profile binding uses another catalog")
        catalog = self._registry.get_execution_profile_catalog(
            binding.catalog_version,
            binding.catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation("config export execution-profile catalog is not retained")
        definitions = tuple(
            definition for definition in catalog.definitions if definition.profile == export_profile
        )
        if len(definitions) != 1:
            raise IntegrityViolation("config export profile is not unique in its retained catalog")
        definition = definitions[0]
        details = definition.details
        if (
            definition.profile_kind != "config_export"
            or run.kind not in definition.compatible_run_kinds
            or payload_schema_id not in definition.output_schema_ids
            or execution_profile_payload_hash(definition) != binding.profile_payload_hash
            or not isinstance(details, ConfigExportProfileDetailsV1)
        ):
            raise IntegrityViolation("config export profile differs from its frozen Run binding")
        expected = {
            "target_environment_profile": details.target_environment_profile.model_dump(
                mode="json"
            ),
            "env_contract_version": details.env_contract_version,
            "format_schema_id": details.format_schema_id,
            "package_schema_version": details.package_schema_version,
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                raise IntegrityViolation(
                    "config export package differs from its exact profile",
                    field=field,
                )
        return details.env_contract_version

    def _publish_findings(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunResult,
        plan: PublicationPlan,
        allocations: Sequence[RuleAllocation],
        published: "_PublishedArtifacts",
        occurred_at: str,
    ) -> int:
        if not prepared.findings:
            return 0
        if plan.finding_policy is None:
            raise IntegrityViolation("Run kind has no finding-output policy but prepared findings")
        if len(prepared.findings) > plan.finding_policy.max_findings:
            raise IntegrityViolation("prepared findings exceed the policy maximum")
        rule_of_index = {
            index: allocation.plan_rule.rule.rule_id
            for allocation in allocations
            for index in allocation.artifact_indexes
        }
        planned = []
        for prepared_finding in prepared.findings:
            evidence_index = prepared_finding.evidence_artifact_index
            evidence_artifact_id = published.index_to_id[evidence_index]
            evidence_rule_id = rule_of_index[evidence_index]
            planned.append(
                plan_finding_write(
                    prepared=prepared_finding,
                    finding_policy=plan.finding_policy,
                    evidence_rule_id=evidence_rule_id,
                    evidence_artifact_id=evidence_artifact_id,
                    evidence_version_tuple=prepared.artifacts[evidence_index].version_tuple,
                    run_id=run.run_id,
                    attempt_no=attempt.attempt_no,
                    ordinal=1,
                    occurred_at=occurred_at,
                )
            )
        planned.sort(key=lambda write: (write.revision.finding_id, write.revision.revision))
        for ordinal, write in enumerate(planned, start=1):
            link = write.link.model_copy(update={"ordinal": ordinal})
            if self._collector is None:  # pragma: no cover - public guard above
                raise IntegrityViolation("Finding publication has no active draft collector")
            self._collector.add_finding(
                PlannedFindingWrite(
                    revision=write.revision,
                    expected_current_revision=write.expected_current_revision,
                    link=link,
                )
            )
        return len(planned)

    def _build_run_failure(
        self,
        *,
        run: RunRecord,
        attempt_no: int | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        evidence_ids: tuple[str, ...],
        dispositions: Sequence[RequirementDispositionV1],
        occurred_at: str,
        projection: RunManifestVersionProjectionV1,
    ) -> RunFailureV1:
        return RunFailureV1(
            run_id=run.run_id,
            attempt_no=attempt_no,
            run_kind=run.kind,
            cause_code=prepared.cause_code,
            failure_class=prepared.failure_class,
            retryable=(retry_decision.decision == "retry"),
            retry_decision=retry_decision,
            dependency=prepared.dependency,
            redacted_message=prepared.redacted_message,
            evidence_artifact_ids=evidence_ids,
            requirement_dispositions=tuple(dispositions),
            occurred_at=occurred_at,
            version_projection=projection,
        )

    def _manifest_projection(
        self,
        *,
        run: RunRecord,
        attempt_no: int | None,
        scope: str,
        transition_policy: object,
        transition_ref: object,
        extra_parents: Sequence[RunManifestParentBindingV1],
        execution_identity: ExecutionIdentityV1 | None,
        cassette_ids_by_scope: Mapping[str, str],
    ) -> RunManifestVersionProjectionV1:
        parents_by_id = {
            input_id: RunManifestParentBindingV1(
                artifact_id=input_id,
                role="input",
                publication="existing",
            )
            for input_id in run.payload.input_artifact_ids
        }
        for parent in extra_parents:
            existing = parents_by_id.get(parent.artifact_id)
            if existing is None:
                parents_by_id[parent.artifact_id] = parent
                continue
            # REPLAY's cassette is already an exact Run input. Runtime projection
            # upgrades that one binding with ``cassette_scope=replay_input``; it
            # must not append a second role for the same immutable Artifact.
            if (
                existing.role == "input"
                and existing.publication == "existing"
                and existing.cassette_scope is None
                and parent.role == "input"
                and parent.publication == "existing"
                and parent.cassette_scope == "replay_input"
            ):
                parents_by_id[parent.artifact_id] = parent
                continue
            raise IntegrityViolation(
                "manifest parent Artifact is bound more than once",
                artifact_id=parent.artifact_id,
            )
        terminal_tuple = project_manifest_version_tuple(
            policy=transition_policy,  # type: ignore[arg-type]
            manifest_scope=scope,
            llm_execution_mode=run.payload.llm_execution_mode,
            frozen_tuple=run.payload.version_tuple,
            execution_identity=execution_identity,
            cassette_ids_by_scope=cassette_ids_by_scope,
        )
        return RunManifestVersionProjectionV1(
            manifest_scope=scope,
            attempt_no=attempt_no,
            run_kind=run.kind,
            run_payload_hash=run.payload_hash,
            frozen_input_version_tuple=run.payload.version_tuple,
            terminal_version_tuple=terminal_tuple,
            version_transition_policy_ref=transition_ref,  # type: ignore[arg-type]
            parents=tuple(parents_by_id.values()),
        )

    def _publish_manifest(
        self,
        *,
        kind: str,
        payload: Mapping[str, object],
        version_tuple: VersionTuple,
        parents: Sequence[RunManifestParentBindingV1],
        expected_projection: RunManifestVersionProjectionV1,
        occurred_at: str,
        execution_identity: ExecutionIdentityV1 | None,
        replayability: str,
        llm_execution_mode: str,
    ) -> str:
        blob = canonical_json(payload).encode("utf-8")
        if self._collector is None:  # pragma: no cover - public guard above
            raise IntegrityViolation("manifest publication has no active draft collector")
        object_ref = self._collector.add_blob(blob)
        lineage = tuple(sorted({parent.artifact_id for parent in parents}))
        manifest_schema_id = "run-result@1" if kind == "run_result" else "run-failure@1"
        meta: dict[str, object] = {
            "manifest_scope": payload["version_projection"]["manifest_scope"],
            "attempt_no": payload["version_projection"]["attempt_no"],
            "payload_schema_id": manifest_schema_id,
            "replayability": replayability,
        }
        if execution_identity is not None:
            meta["execution_identity"] = execution_identity
        artifact = build_artifact_v2(
            kind=kind,
            version_tuple=version_tuple,
            lineage=lineage,
            payload_hash=object_ref.sha256,
            object_ref=object_ref,
            meta=meta,
            created_at=occurred_at,
        )
        if artifact.artifact_id in lineage:
            raise IntegrityViolation("manifest artifact references itself in its lineage")
        projection = RunManifestVersionProjectionV1.model_validate(payload["version_projection"])
        if projection != expected_projection:
            raise IntegrityViolation(
                "run manifest payload projection differs from the authoritative projection"
            )
        validate_artifact_producer(
            artifact,
            ProducerValidationContext(
                llm_execution_mode=llm_execution_mode,  # type: ignore[arg-type]
                has_llm_invocations=bool(
                    execution_identity is not None and execution_identity.bindings
                ),
                operational_observation=(replayability == "operational_observation"),
                run_manifest_projection=projection,
                expected_run_manifest_projection=expected_projection,
            ),
        )
        stored = self._collector.add_artifact(artifact)
        return stored.artifact_id

    def _domain_manifest_parents(
        self, published: "_PublishedArtifacts"
    ) -> tuple[RunManifestParentBindingV1, ...]:
        return tuple(
            RunManifestParentBindingV1(
                artifact_id=artifact_id,
                role=_role_for_manifest(role),
                publication="run_published",
            )
            for artifact_id, role in published.roles.items()
        )

    def _validated_runtime_parents(
        self,
        *,
        run: RunRecord,
        plan: PublicationPlan,
        manifest_scope: str,
        current_attempt_no: int | None,
        closed: Mapping[str, int | None],
    ) -> _TerminalRuntimeProjection:
        """Close runtime links, identity, cassette tree and transition inputs."""

        mode = run.payload.llm_execution_mode
        current_links = (
            self._ledger.prompt_links(run.run_id, attempt_no=current_attempt_no)
            if current_attempt_no is not None
            else ()
        )
        all_links = self._ledger.prompt_links(run.run_id, attempt_no=None)
        committed = {"current_attempt": len(current_links), "all_attempts": len(all_links)}
        prompt_links = current_links if manifest_scope == "attempt" else all_links

        record_shards: tuple[tuple[int, int, str], ...] = ()
        attempt_bundle_id: str | None = None
        authoritative_attempt_bundle_ids: dict[int, str] = {}
        run_bundle_id: str | None = None
        replay_input_id: str | None = None
        if mode == "record":
            record_shards = self._ledger.record_shard_links(
                run.run_id,
                attempt_no=(current_attempt_no if manifest_scope == "attempt" else None),
            )
            if current_attempt_no is not None:
                attempt_numbers = (
                    (current_attempt_no,)
                    if manifest_scope == "attempt"
                    else tuple(range(1, current_attempt_no + 1))
                )
                for attempt_no in attempt_numbers:
                    authoritative_id = self._ledger.attempt_cassette_bundle(
                        run.run_id, attempt_no=attempt_no
                    )
                    if authoritative_id is None:
                        raise IntegrityViolation(
                            "RECORD terminal publication has no authoritative attempt bundle",
                            run_id=run.run_id,
                            attempt_no=attempt_no,
                        )
                    authoritative_attempt_bundle_ids[attempt_no] = authoritative_id
                attempt_bundle_id = authoritative_attempt_bundle_ids[current_attempt_no]
            if manifest_scope == "run":
                run_bundle_id = self._ledger.run_cassette_bundle(run.run_id)
        elif mode == "replay":
            replay_input_id = self._ledger.replay_input_cassette(run.run_id)
            if replay_input_id != run.payload.cassette_artifact_id:
                raise IntegrityViolation(
                    "REPLAY ledger cassette differs from the frozen Run payload",
                    run_id=run.run_id,
                )

        runtime_ids = {
            *(link.artifact_id for link in prompt_links),
            *(artifact_id for _, _, artifact_id in record_shards),
            *closed,
        }
        runtime_ids.update(
            artifact_id
            for artifact_id in (attempt_bundle_id, run_bundle_id, replay_input_id)
            if artifact_id is not None
        )
        artifact_info = {
            artifact_id: self._runtime_parent_info(artifact_id) for artifact_id in runtime_ids
        }
        for artifact_id, attempt_no in closed.items():
            self._validate_closed_attempt_parent(
                run=run,
                artifact_id=artifact_id,
                attempt_no=attempt_no,
            )

        identity: ExecutionIdentityV1 | None = None
        if mode != "not_applicable":
            try:
                identity = ExecutionIdentityV1.model_validate(
                    self._ledger.execution_identity(
                        run.run_id,
                        attempt_no=(current_attempt_no if manifest_scope == "attempt" else None),
                    )
                )
            except ValueError as exc:
                raise IntegrityViolation(
                    "terminal execution identity is not canonical",
                    run_id=run.run_id,
                    manifest_scope=manifest_scope,
                ) from exc
            self._validate_execution_identity(
                run=run,
                identity=identity,
                manifest_scope=manifest_scope,
                current_attempt_no=current_attempt_no,
                prompt_links=prompt_links,
                artifact_info=artifact_info,
            )

        consumed_response_call_keys: frozenset[tuple[int, int]] = frozenset()
        if mode == "record":
            if identity is None:  # pragma: no cover - RECORD always loads one above
                raise IntegrityViolation("RECORD terminal publication has no execution identity")
            consumed_keys = [
                (binding.attempt_no, binding.call_ordinal)
                for binding in identity.bindings
                if binding.response_consumed
            ]
            if len(consumed_keys) != len(set(consumed_keys)):
                raise IntegrityViolation(
                    "RECORD execution identity consumes more than one route per logical call"
                )
            consumed_response_call_keys = frozenset(consumed_keys)

        parents = project_runtime_parents(
            rule_set=plan.runtime_rule_set,
            run_id=run.run_id,
            manifest_scope=manifest_scope,
            current_attempt_no=current_attempt_no,
            llm_execution_mode=mode,
            prompt_links=prompt_links,
            record_shards=record_shards,
            closed=closed,
            # The attempt bundle remains the authoritative pointer used to close
            # the current RunAttempt, and is part of run-bundle tree validation.
            # It is a manifest parent only at attempt scope; run scope projects the
            # aggregate run bundle instead.
            attempt_bundle_id=(attempt_bundle_id if manifest_scope == "attempt" else None),
            run_bundle_id=run_bundle_id,
            replay_input_id=replay_input_id,
            committed_link_counts=committed,
            artifact_info_by_id=artifact_info,
            consumed_response_call_keys=consumed_response_call_keys,
        )

        cassette_ids: dict[str, str] = {}
        if mode == "record":
            bundle_id = attempt_bundle_id if manifest_scope == "attempt" else run_bundle_id
            if bundle_id is None:  # runtime-parent cardinality normally catches this first
                raise IntegrityViolation("RECORD terminal publication has no aggregate bundle")
            aggregate = self._read_cassette_node(bundle_id)
            self._validate_record_cassette_tree(
                run=run,
                manifest_scope=manifest_scope,
                current_attempt_no=current_attempt_no,
                aggregate=aggregate,
                record_shards=record_shards,
                authoritative_attempt_bundle_ids=authoritative_attempt_bundle_ids,
                prompt_links=prompt_links,
                execution_identity=identity,
            )
            scope = "attempt_bundle" if manifest_scope == "attempt" else "run_bundle"
            cassette_ids[scope] = self._cassette_id(aggregate.artifact)
        elif mode == "replay":
            if replay_input_id is None:  # runtime-parent cardinality normally catches this first
                raise IntegrityViolation("REPLAY terminal publication has no input bundle")
            root = self._read_cassette_node(replay_input_id)
            if identity is None:  # defensive: REPLAY always obtains one above
                raise IntegrityViolation("REPLAY terminal publication has no execution identity")
            self._validate_replay_cassette_tree(
                root,
                terminal_identity=identity,
                manifest_scope=manifest_scope,
                current_attempt_no=current_attempt_no,
                prompt_links=prompt_links,
                require_complete=(plan.policy.prepared_outcome == "success"),
            )
            cassette_ids["replay_input"] = self._cassette_id(root.artifact)

        return _TerminalRuntimeProjection(
            parents=parents,
            execution_identity=identity,
            cassette_ids_by_scope=cassette_ids,
            attempt_cassette_artifact_id=(attempt_bundle_id if mode == "record" else None),
            terminal_cassette_artifact_id=(
                run_bundle_id
                if mode == "record" and manifest_scope == "run"
                else (replay_input_id if mode == "replay" and manifest_scope == "run" else None)
            ),
        )

    def _validate_execution_identity(
        self,
        *,
        run: RunRecord,
        identity: ExecutionIdentityV1,
        manifest_scope: str,
        current_attempt_no: int | None,
        prompt_links: Sequence[RunIntermediateArtifactLinkV1],
        artifact_info: Mapping[str, ParentInfo],
    ) -> None:
        plan = run.payload.execution_version_plan
        if plan is None:
            raise IntegrityViolation("LLM terminal publication has no frozen execution plan")
        expected_scope = "attempt" if manifest_scope == "attempt" else "run"
        if identity.scope != expected_scope:
            raise IntegrityViolation(
                "terminal execution identity has the wrong scope",
                expected=expected_scope,
                actual=identity.scope,
            )
        if identity.agent_graph_version != plan.agent_graph_version:
            raise IntegrityViolation("terminal execution identity escapes the frozen Agent graph")

        links_by_call = {(link.attempt_no, link.call_ordinal): link for link in prompt_links}
        nodes = {node.agent_node_id: node for node in plan.nodes}
        expected_sources = (
            {"cassette_replay"}
            if run.payload.llm_execution_mode == "replay"
            else {"online", "full_response_cache"}
        )
        for binding in identity.bindings:
            if manifest_scope == "attempt" and binding.attempt_no != current_attempt_no:
                raise IntegrityViolation(
                    "attempt execution identity contains another attempt",
                    attempt_no=binding.attempt_no,
                )
            if manifest_scope == "run" and (
                current_attempt_no is None or binding.attempt_no > current_attempt_no
            ):
                raise IntegrityViolation(
                    "run execution identity contains a future attempt",
                    attempt_no=binding.attempt_no,
                )
            link = links_by_call.get((binding.attempt_no, binding.call_ordinal))
            if link is None:
                raise IntegrityViolation(
                    "execution identity has no committed rendered-prompt link",
                    attempt_no=binding.attempt_no,
                    call_ordinal=binding.call_ordinal,
                )
            retained_attempt = self._ledger.get_attempt(run.run_id, binding.attempt_no)
            if (
                not isinstance(retained_attempt, RunAttempt)
                or retained_attempt.run_id != run.run_id
                or retained_attempt.attempt_no != binding.attempt_no
                or link.fencing_token != retained_attempt.fencing_token
                or link.call_ordinal >= retained_attempt.next_call_ordinal
            ):
                raise IntegrityViolation(
                    "rendered-prompt link differs from fenced RunAttempt authority"
                )
            node = nodes.get(binding.agent_node_id)
            if node is None:
                raise IntegrityViolation(
                    "execution identity references a node outside the frozen plan",
                    agent_node_id=binding.agent_node_id,
                )
            if (
                binding.prompt_version != node.prompt_version
                or binding.tool_version != node.tool_version
                or binding.model_snapshot not in node.allowed_model_snapshots
            ):
                raise IntegrityViolation(
                    "execution identity node/prompt/model/tool differs from the frozen plan",
                    agent_node_id=binding.agent_node_id,
                )
            if binding.execution_source not in expected_sources:
                raise IntegrityViolation(
                    "execution identity uses an execution source incompatible with the Run mode",
                    execution_source=binding.execution_source,
                )
            if (
                run.payload.llm_execution_mode in {"live", "record"}
                and binding.routing_decision_kind != "native"
            ):
                raise IntegrityViolation(
                    "LIVE/RECORD identity cannot use a legacy import routing decision"
                )
            prompt_info = artifact_info[link.artifact_id]
            if (
                prompt_info.version_tuple.prompt_version != binding.prompt_version
                or prompt_info.version_tuple.model_snapshot != binding.model_snapshot
                or prompt_info.version_tuple.agent_graph_version != identity.agent_graph_version
            ):
                raise IntegrityViolation(
                    "execution identity differs from its rendered-prompt Artifact",
                    artifact_id=link.artifact_id,
                )
            if binding.routing_decision_kind == "native":
                decision = self._ledger.get_routing_decision(binding.routing_decision_id)
                if (
                    type(decision) is not RoutingDecisionV1
                    or decision.decision_id != binding.routing_decision_id
                    or decision.run_id != run.run_id
                    or decision.attempt_no != binding.attempt_no
                    or decision.request_hash != f"sha256:{link.request_hash}"
                    or decision.fallback_index + 1 != binding.route_ordinal
                    or decision.model_snapshot != binding.model_snapshot
                    or decision.execution_source != binding.execution_source
                    or decision.budget_set_snapshot_id != run.payload.budget_set_snapshot_id
                    or decision.policy_version != plan.routing_policy_version
                    or decision.routing_policy_digest != plan.routing_policy_digest
                    or decision.catalog_version != plan.model_catalog_version
                    or decision.catalog_digest != plan.model_catalog_digest
                ):
                    raise IntegrityViolation(
                        "native invocation differs from retained RoutingDecision authority"
                    )

    def _validate_record_cassette_tree(
        self,
        *,
        run: RunRecord,
        manifest_scope: str,
        current_attempt_no: int | None,
        aggregate: _CassetteNode,
        record_shards: Sequence[tuple[int, int, str]],
        authoritative_attempt_bundle_ids: Mapping[int, str],
        prompt_links: Sequence[RunIntermediateArtifactLinkV1],
        execution_identity: ExecutionIdentityV1 | None,
    ) -> None:
        if execution_identity is None:  # defensive: RECORD always obtains one above
            raise IntegrityViolation("RECORD aggregate has no terminal execution identity")
        expected_scope = "attempt" if manifest_scope == "attempt" else "run"
        self._require_bundle_shape(
            aggregate,
            scope=expected_scope,
            run_id=run.run_id,
            attempt_no=(current_attempt_no if manifest_scope == "attempt" else None),
            ordinal=None,
        )
        if self._cassette_identity(aggregate.artifact) != execution_identity:
            raise IntegrityViolation(
                "RECORD aggregate identity differs from the authoritative terminal identity"
            )
        self._require_aggregate_lineage(aggregate)

        shard_ids_by_attempt: dict[int, list[tuple[int, str]]] = {}
        for attempt_no, ordinal, artifact_id in record_shards:
            shard_ids_by_attempt.setdefault(attempt_no, []).append((ordinal, artifact_id))

        if manifest_scope == "attempt":
            if current_attempt_no is None:  # pragma: no cover - caller already checked
                raise IntegrityViolation("attempt cassette has no attempt number")
            expected_children = tuple(
                artifact_id
                for _, artifact_id in sorted(shard_ids_by_attempt.get(current_attempt_no, ()))
            )
            if aggregate.payload.child_bundle_artifact_ids != expected_children:
                raise IntegrityViolation(
                    "attempt cassette children differ from committed record shards"
                )
            self._validate_record_shards(
                run=run,
                attempt_no=current_attempt_no,
                shard_rows=shard_ids_by_attempt.get(current_attempt_no, ()),
                aggregate_identity=execution_identity,
                prompt_links=prompt_links,
            )
            return

        expected_attempts = (
            () if current_attempt_no is None else tuple(range(1, current_attempt_no + 1))
        )
        expected_attempt_children = tuple(
            authoritative_attempt_bundle_ids[attempt_no] for attempt_no in expected_attempts
        )
        if aggregate.payload.child_bundle_artifact_ids != expected_attempt_children:
            raise IntegrityViolation(
                "run cassette children differ from authoritative attempt bundle pointers"
            )
        observed_attempts: list[int] = []
        observed_children: list[str] = []
        for attempt_artifact_id in aggregate.payload.child_bundle_artifact_ids:
            attempt = self._read_cassette_node(attempt_artifact_id)
            attempt_no = attempt.payload.attempt_no
            if attempt_no is None:  # pragma: no cover - CassetteBundleV1 closes this
                raise IntegrityViolation("attempt cassette child has no attempt number")
            self._require_bundle_shape(
                attempt,
                scope="attempt",
                run_id=run.run_id,
                attempt_no=attempt_no,
                ordinal=None,
            )
            self._require_aggregate_lineage(attempt)
            expected_identity = self._identity_subset(
                execution_identity,
                scope="attempt",
                attempt_no=attempt_no,
            )
            if self._cassette_identity(attempt.artifact) != expected_identity:
                raise IntegrityViolation("attempt cassette identity differs from its Run aggregate")
            expected_shards = tuple(
                artifact_id for _, artifact_id in sorted(shard_ids_by_attempt.get(attempt_no, ()))
            )
            if attempt.payload.child_bundle_artifact_ids != expected_shards:
                raise IntegrityViolation(
                    "attempt cassette children differ from committed record shards",
                    attempt_no=attempt_no,
                )
            self._validate_record_shards(
                run=run,
                attempt_no=attempt_no,
                shard_rows=shard_ids_by_attempt.get(attempt_no, ()),
                aggregate_identity=execution_identity,
                prompt_links=prompt_links,
            )
            observed_attempts.append(attempt_no)
            observed_children.append(attempt.artifact.artifact_id)
        if tuple(observed_attempts) != expected_attempts:
            raise IntegrityViolation(
                "run cassette does not contain every attempt bundle exactly once",
                expected=list(expected_attempts),
                actual=observed_attempts,
            )
        if tuple(observed_children) != aggregate.payload.child_bundle_artifact_ids:
            raise IntegrityViolation("run cassette child order is not canonical")
        if set(shard_ids_by_attempt).difference(expected_attempts):
            raise IntegrityViolation("record shard belongs to an unknown Run attempt")

    def _validate_record_shards(
        self,
        *,
        run: RunRecord,
        attempt_no: int,
        shard_rows: Sequence[tuple[int, str]],
        aggregate_identity: ExecutionIdentityV1,
        prompt_links: Sequence[RunIntermediateArtifactLinkV1],
    ) -> None:
        previous_ordinal = 0
        for ordinal, artifact_id in sorted(shard_rows):
            if ordinal <= previous_ordinal:
                raise IntegrityViolation("record shard ordinals are not strictly increasing")
            shard = self._read_cassette_node(artifact_id)
            self._require_bundle_shape(
                shard,
                scope="record_shard",
                run_id=run.run_id,
                attempt_no=attempt_no,
                ordinal=ordinal,
            )
            expected_identity = self._identity_subset(
                aggregate_identity,
                scope="record_shard",
                attempt_no=attempt_no,
                call_ordinal=ordinal,
            )
            if (
                not expected_identity.bindings
                or sum(binding.response_consumed for binding in expected_identity.bindings) != 1
            ):
                raise IntegrityViolation(
                    "record shard logical call has no unique consumed response",
                    attempt_no=attempt_no,
                    call_ordinal=ordinal,
                )
            if self._cassette_identity(shard.artifact) != expected_identity:
                raise IntegrityViolation(
                    "record shard identity differs from its aggregate identity",
                    artifact_id=artifact_id,
                )
            self._validate_native_record_shard(
                run=run,
                shard=shard,
                expected_identity=expected_identity,
                prompt_links=prompt_links,
            )
            previous_ordinal = ordinal

    def _validate_native_record_shard(
        self,
        *,
        run: RunRecord,
        shard: _CassetteNode,
        expected_identity: ExecutionIdentityV1,
        prompt_links: Sequence[RunIntermediateArtifactLinkV1],
    ) -> None:
        """Close a native RECORD shard against its exact request and invocation."""

        attempt_no = shard.payload.attempt_no
        ordinal = shard.payload.ordinal
        if attempt_no is None or ordinal is None:  # pragma: no cover - bundle contract closes this
            raise IntegrityViolation("native record shard lacks its attempt/call identity")
        if len(shard.payload.records) != 1 or not isinstance(
            shard.payload.records[0], CassetteRecordV2
        ):
            raise IntegrityViolation("native RECORD shard lacks one exact cassette@2 record")
        # ``_read_cassette_node`` has already re-read and parsed the entire
        # content-addressed canonical bundle.  Consequently all response,
        # observation, tool-call and transport fields below are the immutable
        # shard contents; the remaining checks bind that exact record to the
        # platform authorities that preceded response consumption.
        record = shard.payload.records[0]
        decision = record.routing_decision
        plan = run.payload.execution_version_plan
        if plan is None:
            raise IntegrityViolation("native RECORD shard has no frozen execution plan")

        links = tuple(
            link
            for link in prompt_links
            if link.attempt_no == attempt_no and link.call_ordinal == ordinal
        )
        if len(links) != 1:
            raise IntegrityViolation("native RECORD shard lacks one exact rendered-prompt link")
        link = links[0]
        if link.run_id != run.run_id:
            raise IntegrityViolation("native RECORD prompt link belongs to another Run")
        retained_attempt = self._ledger.get_attempt(run.run_id, attempt_no)
        if (
            not isinstance(retained_attempt, RunAttempt)
            or retained_attempt.run_id != run.run_id
            or retained_attempt.attempt_no != attempt_no
            or link.fencing_token != retained_attempt.fencing_token
            or link.call_ordinal >= retained_attempt.next_call_ordinal
        ):
            raise IntegrityViolation(
                "native RECORD prompt link differs from fenced RunAttempt authority"
            )
        if shard.artifact.lineage != (link.artifact_id,):
            raise IntegrityViolation("native RECORD shard lineage differs from its prompt")

        prompt = self._artifact_v2(link.artifact_id)
        if (
            prompt.kind != "source_rendered"
            or prompt.meta.get("payload_schema_id") != "source-rendered@1"
        ):
            raise IntegrityViolation("native RECORD prompt link does not resolve source_rendered")
        rendered_request = self._load_rendered_model_request(prompt)

        node = next(
            (item for item in plan.nodes if item.agent_node_id == record.agent_node_id),
            None,
        )
        if node is None or decision.model_snapshot not in node.allowed_model_snapshots:
            raise IntegrityViolation("native RECORD cassette record is outside the execution plan")
        rendered_hash = request_hash(rendered_request)
        rendered_model = canonical_model_snapshot_id(rendered_request.model_snapshot)
        if (
            rendered_hash != record.request_hash
            or rendered_hash.removeprefix("sha256:") != link.request_hash
            or rendered_request.agent_node_id != record.agent_node_id
            or rendered_request.prompt_version != node.prompt_version
            or rendered_model != decision.model_snapshot
            or prompt.version_tuple.prompt_version != rendered_request.prompt_version
            or prompt.version_tuple.model_snapshot != rendered_model
            or prompt.version_tuple.agent_graph_version != plan.agent_graph_version
        ):
            raise IntegrityViolation(
                "native RECORD rendered request differs from prompt link, record, or plan"
            )

        if (
            decision.run_id != run.run_id
            or decision.attempt_no != attempt_no
            or decision.request_hash != record.request_hash
            or decision.model_snapshot != canonical_model_snapshot_id(record.model_snapshot)
            or decision.execution_source == "cassette_replay"
            or decision.budget_set_snapshot_id != run.payload.budget_set_snapshot_id
            or decision.policy_version != plan.routing_policy_version
            or decision.routing_policy_digest != plan.routing_policy_digest
            or decision.catalog_version != plan.model_catalog_version
            or decision.catalog_digest != plan.model_catalog_digest
        ):
            raise IntegrityViolation(
                "native RECORD routing decision differs from Run, shard, or execution plan"
            )

        consumed = tuple(
            binding for binding in expected_identity.bindings if binding.response_consumed
        )
        if len(consumed) != 1:
            raise IntegrityViolation("native RECORD record does not bind one consumed route")
        binding = consumed[0]
        retained_decision = self._ledger.get_routing_decision(binding.routing_decision_id)
        if (
            type(retained_decision) is not RoutingDecisionV1
            or retained_decision.decision_id != binding.routing_decision_id
            or retained_decision != decision
        ):
            raise IntegrityViolation(
                "native RECORD record differs from retained RoutingDecision authority"
            )
        expected_transport_attempt = (
            record.transport_attempt_count if decision.execution_source == "online" else None
        )
        if (
            binding.routing_decision_kind != "native"
            or binding.routing_decision_id != decision.decision_id
            or binding.attempt_no != attempt_no
            or binding.call_ordinal != ordinal
            or binding.route_ordinal != decision.fallback_index + 1
            or binding.transport_attempt != expected_transport_attempt
            or binding.agent_node_id != record.agent_node_id
            or binding.prompt_version != node.prompt_version
            or binding.model_snapshot != decision.model_snapshot
            or binding.tool_version != node.tool_version
            or binding.execution_source != decision.execution_source
        ):
            raise IntegrityViolation(
                "native RECORD invocation differs from record, route, transport, or plan"
            )

    def _load_rendered_model_request(self, artifact: ArtifactV2) -> ModelRequestV2:
        blob = self._read_published_artifact_bytes(artifact)
        try:
            decoded = json.loads(blob.decode("utf-8"))
            if not isinstance(decoded, Mapping):
                raise ValueError("rendered request must be an object")
            rendered = parse_model_request(decoded)
        except (UnicodeDecodeError, ValueError) as exc:
            raise IntegrityViolation("native RECORD rendered request is malformed") from exc
        if not isinstance(rendered, ModelRequestV2):
            raise IntegrityViolation("native RECORD rendered prompt is not model-router@2")
        if canonical_json(rendered.model_dump(mode="json")).encode("utf-8") != blob:
            raise IntegrityViolation("native RECORD rendered request is not canonical")
        return rendered

    def _validate_replay_cassette_tree(
        self,
        root: _CassetteNode,
        *,
        terminal_identity: ExecutionIdentityV1,
        manifest_scope: str,
        current_attempt_no: int | None,
        prompt_links: Sequence[RunIntermediateArtifactLinkV1],
        require_complete: bool,
    ) -> None:
        self._require_bundle_shape(
            root,
            scope="run",
            run_id=root.payload.run_id,
            attempt_no=None,
            ordinal=None,
        )
        self._require_aggregate_lineage(root)
        root_identity = self._cassette_identity(root.artifact)
        if root_identity.scope != "run":
            raise IntegrityViolation("REPLAY input cassette identity is not run-scoped")
        if root_identity.agent_graph_version != terminal_identity.agent_graph_version:
            raise IntegrityViolation(
                "REPLAY terminal identity Agent graph differs from its input cassette"
            )
        source_bindings = (
            root_identity.bindings
            if manifest_scope == "run"
            else tuple(
                binding
                for binding in root_identity.bindings
                if binding.attempt_no == current_attempt_no
            )
        )
        if len(terminal_identity.bindings) > len(source_bindings) or (
            require_complete and len(source_bindings) != len(terminal_identity.bindings)
        ):
            raise IntegrityViolation(
                "REPLAY terminal identity route count differs from its input cassette"
            )
        semantic_fields = (
            "attempt_no",
            "call_ordinal",
            "route_ordinal",
            "routing_decision_kind",
            "agent_node_id",
            "prompt_version",
            "model_snapshot",
            "tool_version",
            "response_consumed",
        )
        for source, terminal in zip(
            source_bindings,
            terminal_identity.bindings,
            strict=False,
        ):
            if any(getattr(source, field) != getattr(terminal, field) for field in semantic_fields):
                raise IntegrityViolation(
                    "REPLAY terminal identity route differs from its input cassette"
                )
            if (
                source.routing_decision_kind == "legacy_import"
                and source.routing_decision_id != terminal.routing_decision_id
            ):
                raise IntegrityViolation(
                    "legacy REPLAY terminal identity uses another imported decision"
                )

        source_call_keys = {
            (binding.attempt_no, binding.call_ordinal) for binding in source_bindings
        }
        terminal_call_keys = {
            (binding.attempt_no, binding.call_ordinal) for binding in terminal_identity.bindings
        }
        if not terminal_call_keys.issubset(source_call_keys) or (
            require_complete and source_call_keys != terminal_call_keys
        ):
            raise IntegrityViolation("REPLAY terminal logical calls differ from its input cassette")
        source_consumed_keys = [
            (binding.attempt_no, binding.call_ordinal)
            for binding in root_identity.bindings
            if binding.response_consumed
        ]
        if len(source_consumed_keys) != len(set(source_consumed_keys)):
            raise IntegrityViolation(
                "REPLAY input identity consumes more than one route per logical call"
            )
        terminal_consumed_keys = [
            (binding.attempt_no, binding.call_ordinal)
            for binding in terminal_identity.bindings
            if binding.response_consumed
        ]
        if len(terminal_consumed_keys) != len(set(terminal_consumed_keys)):
            raise IntegrityViolation(
                "REPLAY terminal identity consumes more than one route per logical call"
            )
        prompt_hash_by_call = {
            (link.attempt_no, link.call_ordinal): link.request_hash for link in prompt_links
        }
        prompt_call_keys = set(prompt_hash_by_call)
        if (
            len(prompt_hash_by_call) != len(prompt_links)
            or not terminal_call_keys.issubset(prompt_call_keys)
            or (require_complete and prompt_call_keys != source_call_keys)
        ):
            raise IntegrityViolation(
                "REPLAY rendered-prompt links do not close terminal logical calls"
            )
        previous_attempt = 0
        observed_attempts: list[int] = []
        observed_shard_keys: list[tuple[int, int]] = []
        for attempt_id in root.payload.child_bundle_artifact_ids:
            attempt = self._read_cassette_node(attempt_id)
            attempt_no = attempt.payload.attempt_no
            if attempt_no is None or attempt_no <= previous_attempt:
                raise IntegrityViolation("REPLAY attempt bundles are not in canonical order")
            self._require_bundle_shape(
                attempt,
                scope="attempt",
                run_id=root.payload.run_id,
                attempt_no=attempt_no,
                ordinal=None,
            )
            self._require_aggregate_lineage(attempt)
            expected_attempt_identity = self._identity_subset(
                root_identity,
                scope="attempt",
                attempt_no=attempt_no,
            )
            if self._cassette_identity(attempt.artifact) != expected_attempt_identity:
                raise IntegrityViolation(
                    "REPLAY attempt cassette identity differs from its run bundle"
                )
            previous_ordinal = 0
            for shard_id in attempt.payload.child_bundle_artifact_ids:
                shard = self._read_cassette_node(shard_id)
                ordinal = shard.payload.ordinal
                if ordinal is None or ordinal <= previous_ordinal:
                    raise IntegrityViolation("REPLAY record shards are not in canonical order")
                self._require_bundle_shape(
                    shard,
                    scope="record_shard",
                    run_id=root.payload.run_id,
                    attempt_no=attempt_no,
                    ordinal=ordinal,
                )
                expected_shard_identity = self._identity_subset(
                    root_identity,
                    scope="record_shard",
                    attempt_no=attempt_no,
                    call_ordinal=ordinal,
                )
                if self._cassette_identity(shard.artifact) != expected_shard_identity:
                    raise IntegrityViolation(
                        "REPLAY record-shard identity differs from its run bundle"
                    )
                consumed = tuple(
                    binding
                    for binding in expected_shard_identity.bindings
                    if binding.response_consumed
                )
                if len(consumed) != 1 or len(shard.payload.records) != 1:
                    raise IntegrityViolation(
                        "REPLAY record shard lacks one exact consumed route/record"
                    )
                record = shard.payload.records[0]
                binding = consumed[0]
                if isinstance(record, CassetteRecordV2):
                    if (
                        binding.routing_decision_kind != "native"
                        or binding.routing_decision_id != record.routing_decision.decision_id
                        or binding.agent_node_id != record.agent_node_id
                        or binding.model_snapshot
                        != canonical_model_snapshot_id(record.model_snapshot)
                    ):
                        raise IntegrityViolation(
                            "native REPLAY record differs from its consumed route identity"
                        )
                    expected_request_hash = record.request_hash.removeprefix("sha256:")
                elif isinstance(record, CassetteRecordV1):
                    evidence = shard.payload.legacy_call_import_evidence
                    if (
                        binding.routing_decision_kind != "legacy_import"
                        or evidence is None
                        or evidence.invocation != binding
                        or evidence.request_hash != record.request_hash
                    ):
                        raise IntegrityViolation(
                            "legacy REPLAY record differs from its imported route evidence"
                        )
                    expected_request_hash = evidence.request_hash.removeprefix("sha256:")
                else:  # pragma: no cover - CassetteBundleV1 closes the union
                    raise IntegrityViolation("REPLAY record uses an unsupported wire schema")
                linked_hash = prompt_hash_by_call.get((attempt_no, ordinal))
                if linked_hash is not None and linked_hash != expected_request_hash:
                    raise IntegrityViolation(
                        "REPLAY rendered prompt hash differs from its cassette record"
                    )
                observed_shard_keys.append((attempt_no, ordinal))
                previous_ordinal = ordinal
            observed_attempts.append(attempt_no)
            previous_attempt = attempt_no

        if observed_attempts and tuple(observed_attempts) != tuple(
            range(1, observed_attempts[-1] + 1)
        ):
            raise IntegrityViolation(
                "REPLAY attempt bundles do not cover a contiguous attempt history"
            )
        identity_attempts = {binding.attempt_no for binding in root_identity.bindings}
        if not identity_attempts.issubset(observed_attempts):
            raise IntegrityViolation("REPLAY input identity references an omitted attempt bundle")
        if len(observed_shard_keys) != len(set(observed_shard_keys)) or set(
            observed_shard_keys
        ) != set(source_consumed_keys):
            raise IntegrityViolation(
                "REPLAY record-shard tree does not exactly cover consumed source calls"
            )
        selected_tree_keys = (
            set(observed_shard_keys)
            if manifest_scope == "run"
            else {key for key in observed_shard_keys if key[0] == current_attempt_no}
        )
        terminal_consumed_set = set(terminal_consumed_keys)
        if not terminal_consumed_set.issubset(selected_tree_keys) or (
            require_complete and selected_tree_keys != terminal_consumed_set
        ):
            raise IntegrityViolation(
                "REPLAY terminal consumed calls differ from its record-shard tree"
            )

    def _read_cassette_node(self, artifact_id: str) -> _CassetteNode:
        artifact = self._artifact_v2(artifact_id)
        if artifact.kind != "cassette_bundle":
            raise IntegrityViolation(
                "cassette runtime parent is not a cassette_bundle Artifact",
                artifact_id=artifact_id,
            )
        blob = self._read_published_artifact_bytes(artifact)
        try:
            decoded = json.loads(blob.decode("utf-8"))
            payload = CassetteBundleV1.model_validate(decoded)
        except (UnicodeDecodeError, ValueError) as exc:
            raise IntegrityViolation(
                "cassette runtime parent is not canonical CassetteBundleV1",
                artifact_id=artifact_id,
            ) from exc
        if canonical_json(payload.model_dump(mode="json")).encode("utf-8") != blob:
            raise IntegrityViolation(
                "cassette runtime parent bytes are not canonical",
                artifact_id=artifact_id,
            )
        expected_schema = (
            "cassette-record-shard@1" if payload.scope == "record_shard" else "cassette-bundle@1"
        )
        if artifact.meta.get("payload_schema_id") != expected_schema:
            raise IntegrityViolation(
                "cassette runtime parent schema differs from its payload scope",
                artifact_id=artifact_id,
            )
        self._cassette_id(artifact)
        self._cassette_identity(artifact)
        return _CassetteNode(artifact=artifact, payload=payload)

    @staticmethod
    def _require_bundle_shape(
        node: _CassetteNode,
        *,
        scope: str,
        run_id: str | None,
        attempt_no: int | None,
        ordinal: int | None,
    ) -> None:
        if (
            node.payload.scope != scope
            or node.payload.run_id != run_id
            or node.payload.attempt_no != attempt_no
            or node.payload.ordinal != ordinal
        ):
            raise IntegrityViolation(
                "cassette bundle scope/run/attempt/ordinal differs from its runtime binding",
                artifact_id=node.artifact.artifact_id,
            )

    @staticmethod
    def _require_aggregate_lineage(node: _CassetteNode) -> None:
        if node.artifact.lineage != tuple(sorted(node.payload.child_bundle_artifact_ids)):
            raise IntegrityViolation(
                "cassette aggregate lineage differs from its child IDs",
                artifact_id=node.artifact.artifact_id,
            )

    @staticmethod
    def _cassette_identity(artifact: ArtifactV2) -> ExecutionIdentityV1:
        identity = artifact.meta.get("execution_identity")
        if not isinstance(identity, ExecutionIdentityV1):
            raise IntegrityViolation(
                "cassette Artifact lacks a canonical execution identity",
                artifact_id=artifact.artifact_id,
            )
        return identity

    @staticmethod
    def _cassette_id(artifact: ArtifactV2) -> str:
        expected = f"sha256:{artifact.payload_hash}"
        if artifact.version_tuple.cassette_id != expected:
            raise IntegrityViolation(
                "cassette Artifact VersionTuple is not content-bound",
                artifact_id=artifact.artifact_id,
            )
        return expected

    @staticmethod
    def _identity_subset(
        identity: ExecutionIdentityV1,
        *,
        scope: str,
        attempt_no: int,
        call_ordinal: int | None = None,
    ) -> ExecutionIdentityV1:
        bindings = tuple(
            binding
            for binding in identity.bindings
            if binding.attempt_no == attempt_no
            and (call_ordinal is None or binding.call_ordinal == call_ordinal)
        )
        return build_execution_identity(
            scope=scope,  # type: ignore[arg-type]
            bindings=bindings,
            agent_graph_version=identity.agent_graph_version,
        )

    def _validate_closed_attempt_parent(
        self,
        *,
        run: RunRecord,
        artifact_id: str,
        attempt_no: int | None,
    ) -> None:
        if attempt_no is None:
            raise IntegrityViolation("closed attempt failure has no attempt number")
        artifact = self._artifact_v2(artifact_id)
        if (
            artifact.kind != "run_failure"
            or artifact.meta.get("payload_schema_id") != "run-failure@1"
            or artifact.meta.get("manifest_scope") != "attempt"
            or artifact.meta.get("attempt_no") != attempt_no
        ):
            raise IntegrityViolation(
                "closed attempt parent is not its exact attempt-scope failure manifest",
                artifact_id=artifact_id,
                attempt_no=attempt_no,
            )
        blob = self._read_published_artifact_bytes(artifact)
        try:
            decoded = json.loads(blob.decode("utf-8"))
            failure = RunFailureV1.model_validate(decoded)
        except (UnicodeDecodeError, ValueError) as exc:
            raise IntegrityViolation(
                "closed attempt parent payload is not canonical RunFailureV1",
                artifact_id=artifact_id,
            ) from exc
        if canonical_json(failure.model_dump(mode="json")).encode("utf-8") != blob:
            raise IntegrityViolation(
                "closed attempt parent payload bytes are not canonical",
                artifact_id=artifact_id,
            )
        projection = failure.version_projection
        # RunFailureV1 canonicalizes evidence ids lexicographically, while manifest
        # parents preserve typed-rule insertion order.  Compare the same canonical
        # projection rather than treating those two intentional orders as drift.
        published_evidence = tuple(
            sorted(
                parent.artifact_id
                for parent in projection.parents
                if parent.publication == "run_published" and parent.role != "input"
            )
        )
        if (
            failure.run_id != run.run_id
            or failure.run_kind != run.kind
            or failure.attempt_no != attempt_no
            or projection.manifest_scope != "attempt"
            or projection.attempt_no != attempt_no
            or projection.run_kind != run.kind
            or projection.run_payload_hash != run.payload_hash
            or projection.frozen_input_version_tuple != run.payload.version_tuple
            or projection.terminal_version_tuple != artifact.version_tuple
            or {parent.artifact_id for parent in projection.parents} != set(artifact.lineage)
            or failure.evidence_artifact_ids != published_evidence
        ):
            raise IntegrityViolation(
                "closed attempt failure payload/Run/projection/Artifact closure differs",
                artifact_id=artifact_id,
                attempt_no=attempt_no,
            )

    def _runtime_parent_info(self, artifact_id: str) -> ParentInfo:
        artifact = self._artifact_v2(artifact_id)
        schema = artifact.meta.get("payload_schema_id")
        if not isinstance(schema, str):
            raise IntegrityViolation(
                "runtime parent Artifact has no exact payload schema",
                artifact_id=artifact_id,
            )
        return ParentInfo(
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            payload_schema_id=schema,
            version_tuple=artifact.version_tuple,
        )

    def _artifact_v2(self, artifact_id: str) -> ArtifactV2:
        planned = self._planned_artifact_overlay.get(artifact_id)
        if planned is not None:
            return planned[0]
        wire = self._artifacts.get(artifact_id)
        if wire is None:
            raise IntegrityViolation(
                "runtime parent Artifact is not published",
                artifact_id=artifact_id,
            )
        try:
            parsed = wire if isinstance(wire, ArtifactV2) else parse_artifact(wire)
        except ValueError as exc:
            raise IntegrityViolation(
                "runtime parent Artifact is not canonical",
                artifact_id=artifact_id,
            ) from exc
        if not isinstance(parsed, ArtifactV2) or parsed.artifact_id != artifact_id:
            raise IntegrityViolation(
                "runtime parent must be an exact lineage@2 Artifact",
                artifact_id=artifact_id,
            )
        return parsed

    def _read_published_artifact_bytes(self, artifact: ArtifactV2) -> bytes:
        planned = self._planned_artifact_overlay.get(artifact.artifact_id)
        if planned is not None:
            if planned[0] != artifact:
                raise IntegrityViolation(
                    "planned runtime Artifact overlay differs",
                    artifact_id=artifact.artifact_id,
                )
            blob = planned[1]
        else:
            try:
                blob = self._artifacts.read_bytes(artifact.artifact_id)
            except (AttributeError, KeyError, OSError) as exc:
                raise IntegrityViolation(
                    "published runtime Artifact bytes are unavailable",
                    artifact_id=artifact.artifact_id,
                ) from exc
        if (
            sha256_lowerhex(blob) != artifact.payload_hash
            or len(blob) != artifact.object_ref.size_bytes
        ):
            raise IntegrityViolation(
                "published runtime Artifact bytes differ from its ObjectRef",
                artifact_id=artifact.artifact_id,
            )
        return blob

    @staticmethod
    def _replayability_for(run: RunRecord) -> str:
        mode = run.payload.llm_execution_mode
        if mode == "live":
            return "online_only"
        if mode in {"record", "replay"}:
            return "cassette_replay"
        if run.kind.kind == "dr.drill":
            return "operational_observation"
        return "deterministic_recompute"

    def _aggregate_closed_attempts(
        self,
        run_id: str,
        *,
        expected_closed_through: int,
        current_attempt_no: int | None,
        current_attempt_failure_id: str | None,
    ) -> dict[str, int | None]:
        aggregated: dict[str, int | None] = {}
        observed_attempts: set[int] = set()
        for closed_attempt_no, failure_id in self._ledger.closed_attempt_failures(run_id):
            if closed_attempt_no in observed_attempts:
                raise IntegrityViolation(
                    "closed attempt is represented by more than one failure manifest",
                    attempt_no=closed_attempt_no,
                )
            if failure_id in aggregated:
                raise IntegrityViolation(
                    "closed attempt failure aggregated more than once", failure_id=failure_id
                )
            observed_attempts.add(closed_attempt_no)
            aggregated[failure_id] = closed_attempt_no
        expected_attempts = set(range(1, expected_closed_through + 1))
        if observed_attempts != expected_attempts:
            raise IntegrityViolation(
                "closed attempt failure manifests do not cover every prior attempt exactly once",
                expected=sorted(expected_attempts),
                actual=sorted(observed_attempts),
            )
        if current_attempt_failure_id is not None:
            if current_attempt_no != expected_closed_through + 1:
                raise IntegrityViolation(
                    "current attempt failure does not immediately follow the closed history"
                )
            if current_attempt_failure_id in aggregated:
                raise IntegrityViolation(
                    "current attempt failure is already a closed-attempt parent",
                    failure_id=current_attempt_failure_id,
                )
            aggregated[current_attempt_failure_id] = current_attempt_no
        return aggregated

    def _input_parents(self, input_ids: Sequence[str]) -> Mapping[str, ParentInfo]:
        return {input_id: self._parent_info(input_id) for input_id in input_ids}

    def _intermediate_parents(self, run_id: str) -> Mapping[str, ParentInfo]:
        parents: dict[str, ParentInfo] = {}
        for link in self._ledger.prompt_links(run_id, attempt_no=None):
            parents[link.artifact_id] = self._parent_info(link.artifact_id)
        return parents

    def _parent_info(self, artifact_id: str) -> ParentInfo:
        wire = self._artifacts.get(artifact_id)
        if wire is None:
            raise IntegrityViolation(
                "lineage parent artifact is not published", artifact_id=artifact_id
            )
        parsed = wire if isinstance(wire, ArtifactV2) else parse_artifact(wire)
        if parsed.artifact_id != artifact_id:
            raise IntegrityViolation(
                "Artifact port returned a different lineage parent",
                requested_artifact_id=artifact_id,
                actual_artifact_id=parsed.artifact_id,
            )
        meta = getattr(parsed, "meta", {}) or {}
        schema = meta.get("payload_schema_id")
        if not isinstance(schema, str):
            raise IntegrityViolation(
                "parent artifact does not declare its payload schema", artifact_id=artifact_id
            )
        return ParentInfo(
            artifact_id=parsed.artifact_id,
            kind=parsed.kind,
            payload_schema_id=schema,
            version_tuple=parsed.version_tuple,
        )

    @staticmethod
    def _primary_rule_id(plan: PublicationPlan) -> str:
        for plan_rule in plan.plan_rules:
            if plan_rule.rule.role == "primary":
                return plan_rule.rule.rule_id
        raise IntegrityViolation("success policy has no primary artifact rule")


def _snapshots_by_id(
    snapshots: Sequence[ResolvedPolicySnapshotV1],
) -> Mapping[str, ResolvedPolicySnapshotV1]:
    return {snapshot.resolved_policy_id: snapshot for snapshot in snapshots}


def _merge_parent_sources(
    *sources: Mapping[str, ParentInfo],
) -> Mapping[str, ParentInfo]:
    merged: dict[str, ParentInfo] = {}
    for source in sources:
        for artifact_id, info in source.items():
            previous = merged.get(artifact_id)
            if previous is not None and previous != info:
                raise IntegrityViolation(
                    "authorized lineage sources disagree about one parent",
                    artifact_id=artifact_id,
                )
            merged[artifact_id] = info
    return merged


def _inject_prepared_siblings(
    *,
    child_lineage: tuple[str, ...],
    lineage_policy: object,
    siblings: Mapping[str, Mapping[str, ParentInfo]],
) -> tuple[str, ...]:
    """Complete a child's bare lineage with its minted ``prepared_rule`` siblings.

    For each ``prepared_rule`` parent rule the child declares, inject every already
    minted sibling id from ``siblings[source_rule_id]`` whose kind + payload schema
    satisfy the rule. The handler cannot content-address these siblings (their ids
    are re-derived here), so they are absent from ``child_lineage``; the topological
    walk guarantees the parent rule is minted first, so the pool is populated. Order
    is deterministic (existing ids first, then sorted injected ids); ``build_
    artifact_v2`` canonicalises the final set.
    """

    existing = set(child_lineage)
    injected: set[str] = set()
    for rule in lineage_policy.parent_rules:  # type: ignore[attr-defined]
        if rule.source != "prepared_rule" or rule.source_rule_id is None:
            continue
        for sibling_id, info in siblings.get(rule.source_rule_id, {}).items():
            if sibling_id in existing:
                continue
            if info.kind not in rule.artifact_kinds:
                continue
            if info.payload_schema_id not in rule.payload_schema_ids:
                continue
            injected.add(sibling_id)
    if not injected:
        return child_lineage
    return (*child_lineage, *sorted(injected))


def _topological_rule_order(
    allocations: Sequence[RuleAllocation], plan: PublicationPlan
) -> tuple[RuleAllocation, ...]:
    dependencies: dict[str, set[str]] = {}
    for allocation in allocations:
        rule_id = allocation.plan_rule.rule.rule_id
        lineage_policy = plan.lineage_by_rule_id[rule_id]
        dependencies[rule_id] = {
            parent.source_rule_id
            for parent in lineage_policy.parent_rules
            if parent.source == "prepared_rule" and parent.source_rule_id is not None
        }
    ordered: list[RuleAllocation] = []
    emitted: set[str] = set()
    remaining = list(allocations)
    while remaining:
        progressed = False
        for allocation in list(remaining):
            rule_id = allocation.plan_rule.rule.rule_id
            if dependencies[rule_id] <= emitted | {rule_id}:
                ordered.append(allocation)
                emitted.add(rule_id)
                remaining.remove(allocation)
                progressed = True
        if not progressed:
            raise IntegrityViolation(
                "outcome artifact rules have a cyclic prepared-rule dependency"
            )
    return tuple(ordered)


class _PublishedArtifacts:
    __slots__ = (
        "ids_by_rule",
        "index_to_id",
        "payloads_by_index",
        "payloads_by_rule",
        "artifacts_by_rule",
        "roles",
    )

    def __init__(
        self,
        *,
        ids_by_rule: Mapping[str, tuple[str, ...]],
        index_to_id: Mapping[int, str],
        roles: Mapping[str, str],
        payloads_by_index: Mapping[int, Mapping[str, object]],
        payloads_by_rule: Mapping[str, tuple[Mapping[str, object], ...]],
        artifacts_by_rule: Mapping[str, tuple[ArtifactV2, ...]],
    ) -> None:
        self.ids_by_rule = ids_by_rule
        self.index_to_id = index_to_id
        self.roles = roles
        self.payloads_by_index = payloads_by_index
        self.payloads_by_rule = payloads_by_rule
        self.artifacts_by_rule = artifacts_by_rule


__all__ = [
    "ArtifactPort",
    "AuditPort",
    "BlobStore",
    "FindingStore",
    "ManifestLedger",
    "TerminalPublisher",
    "project_runtime_parents",
]
