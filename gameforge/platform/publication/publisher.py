"""The generic terminal publication engine.

``TerminalPublisher`` is the concrete
:class:`gameforge.platform.runs.lifecycle.RunLifecyclePublicationGateway`.  The Run
lifecycle service selects the unique policy per scope and owns cost/event closure.
This engine first produces a pure, complete ``TerminalPublicationDraft`` from a
short authority snapshot.  A separate stager writes every content-addressed blob
after that snapshot closes.  The single write UoW then rechecks fresh authority
only through compact lifecycle/runtime selectors; it does not rebuild or hash the
complete outcome.  The planned commit surface set-preflights exact staged
receipts and affected rows before publishing Artifact/Finding/workflow/manifest/
audit authority atomically with bounded batch writes.

The commit surface has no ObjectStore write capability. Active final failure is one
aggregate projection: the run manifest validates the planned attempt manifest via
an exact in-memory overlay, both are staged and sealed together, and the planned
aggregate commit validates both compact authority digests before the first DB write.
"""

from __future__ import annotations

import json
from collections import ChainMap, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from gameforge.contracts.canonical import (
    canonical_json,
    canonical_sha256,
    sha256_lowerhex,
    typed_canonical_json,
)
from gameforge.contracts.cassette import CassetteRecordV1, CassetteRecordV2
from gameforge.contracts.cassette_import import CassetteBundleV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    EnvironmentProfileDetailsV1,
    PlaytestPlannerProfileConfigV2,
    ProfileRefV1,
    TaskSuiteDerivationProfileConfigV2,
    execution_profile_payload_hash,
)
from gameforge.contracts.findings import Finding, FindingRevisionV1
from gameforge.contracts.jobs import (
    AgentPromptContextV1,
    CheckerRunPayloadV1,
    ConstraintValidationPayloadV1,
    MAX_RUN_MANIFEST_PARENT_BINDINGS,
    OutcomeArtifactRuleV1,
    OutcomeArtifactPolicyV1,
    PreparedRunFailure,
    PreparedRunOutcome,
    PreparedRunResult,
    PatchValidationPayloadV1,
    PlaytestRunPayloadV1,
    ReviewRunPayloadV1,
    RequirementDispositionV1,
    ResolvedPolicySnapshotV1,
    RetryDecisionV1,
    RunAttempt,
    RunFailureV1,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunModelResponseConsumptionV1,
    RunModelRouteLinkV1,
    RunRecord,
    RunResultSummaryV1,
    RunResultV1,
    RunToolIntermediateLinkV1,
    validate_agent_prompt_context_kind,
    RollbackValidationPayloadV1,
    SimulationRunPayloadV1,
    TaskSuiteDerivePayloadV1,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    ExecutionIdentityV1,
    InvocationVersionBindingV1,
    ObjectRef,
    VersionTuple,
    artifact_id_v2_for,
    build_artifact_v2,
    build_execution_identity,
    object_ref_for_bytes,
    parse_artifact,
)
from gameforge.contracts.model_router import ModelRequestV2, parse_model_request, request_hash
from gameforge.contracts.playtest import (
    CompletionOracleRegistryV1,
    PlaytestTraceV1,
    ScenarioSpecV1,
    TaskSuiteV1,
    playtest_resource_upper_bounds,
    resolve_completion_oracle,
)
from gameforge.platform.playtest_payload_schemas import PlaytestPayloadValidationService
from gameforge.platform.run_handlers.playtest import allowed_action_kinds
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.contracts.provenance import ProvenanceV1, most_conservative_trust
from gameforge.platform.publication.effects import (
    AgentDraftWorkflowPort,
    AutoApplyValidationPort,
    PreflightedWorkflowEffect,
    PreparedWorkflowEffect,
    WorkflowEffectContext,
    apply_preflighted_workflow_effect,
    commit_prepared_workflow_effect,
    preflight_prepared_workflow_effect,
    prepare_workflow_effect,
)
from gameforge.platform.audit.gate import AuditAppendIntent
from gameforge.platform.approvals.validation import (
    ValidationCompletionApprovalRepository,
    payload_subject_kind,
    validate_current_subject_binding,
    validate_immutable_subject_binding,
    validate_strict_superseded_subject_binding,
)
from gameforge.platform.publication.findings import PlannedFindingWrite, plan_finding_write
from gameforge.platform.run_handlers.base import finding_to_payload
from gameforge.platform.publication.lineage import (
    LineageParentSources,
    ParentInfo,
    TypedLineage,
    project_typed_lineage,
    resolve_child_payload_references,
)
from gameforge.platform.provenance.registry import build_source_kind_registry
from gameforge.platform.publication.planner import (
    PublicationPlan,
    PublicationRegistry,
    build_publication_plan,
    resolve_definition,
)
from gameforge.platform.publication.payload_binding import (
    FinalSiblingFact,
    bind_final_payload_references,
    expected_typed_run_parent_ids,
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
    PreverifiedAbsentArtifactBinding,
    PreverifiedArtifactBinding,
    StagedReceipt,
    StagedTerminalPublication,
    TerminalPublicationDraft,
    _TerminalPublicationState,
    _terminal_publication_state,
    consume_terminal_publications,
    deep_freeze_value,
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
    TerminalAuthorityDrift,
    validate_prepared_failure,
)


_CASSETTE_TOOL_VERSION = "cassette@1"


class ArtifactPort(Protocol):
    def get(self, artifact_id: str) -> object | None: ...

    def preflight_binding(
        self, artifact: ArtifactV2
    ) -> PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding:
        """Fully verify an existing active binding outside the write UoW."""
        ...

    def put_staged(
        self,
        artifact: ArtifactV2,
        receipt: StagedReceipt,
        retained_binding: (
            PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding | None
        ) = None,
    ) -> ArtifactV2:
        """Bind the explicit staged generation and persist the Artifact atomically."""
        ...

    def read_bytes(self, artifact_id: str) -> bytes:
        """Read the exact bytes of one already-published Artifact."""
        ...

    def preflight_staged_many(
        self,
        writes: Sequence[
            tuple[
                ArtifactV2,
                StagedReceipt,
                PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding,
            ]
        ],
    ) -> object:
        """Validate an entire staged aggregate without issuing a DB write."""
        ...

    def put_preflighted_many(self, batch: object) -> tuple[ArtifactV2, ...]:
        """Persist only a trusted batch returned by ``preflight_staged_many``."""
        ...


class BlobStore(Protocol):
    def read(self, object_ref: ObjectRef, location: object) -> bytes: ...


class FindingStore(Protocol):
    def put(
        self, revision: FindingRevisionV1, *, expected_current_revision: int | None
    ) -> FindingRevisionV1: ...

    def preflight_put_many(
        self,
        writes: Sequence[tuple[FindingRevisionV1, int | None]],
    ) -> object: ...

    def put_preflighted_many(self, seal: object) -> tuple[FindingRevisionV1, ...]: ...


class ManifestLedger(Protocol):
    def terminal_authority_digest(self, run_id: str) -> str:
        """Digest every mutable runtime row that can affect a terminal plan."""
        ...

    def prompt_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]: ...

    def tool_intermediate_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunToolIntermediateLinkV1, ...]: ...

    def closed_attempt_failures(self, run_id: str) -> tuple[tuple[int, str], ...]: ...

    def put_finding_link(self, link: RunFindingLinkV1) -> RunFindingLinkV1: ...

    def preflight_finding_links_many(
        self,
        links: Sequence[RunFindingLinkV1],
        *,
        planned_findings: Sequence[FindingRevisionV1] = (),
        planned_artifact_ids: Sequence[str] = (),
    ) -> object: ...

    def put_preflighted_finding_links_many(
        self,
        seal: object,
    ) -> tuple[RunFindingLinkV1, ...]: ...

    def execution_identity(self, run_id: str, *, attempt_no: int | None) -> ExecutionIdentityV1:
        """Authoritative identity for one attempt, or the whole Run when null."""
        ...

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        """Read the exact transaction-bound Attempt head used to fence prompt links."""
        ...

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None:
        """Read one reserve-before-use native routing decision exactly."""
        ...

    def get_model_route_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelRouteLinkV1 | None: ...

    def get_model_response_consumption(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelResponseConsumptionV1 | None: ...

    def model_route_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunModelRouteLinkV1, ...]:
        """Enumerate every persisted route in the selected terminal scope."""
        ...

    def model_response_consumptions(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunModelResponseConsumptionV1, ...]:
        """Enumerate every consumed route in the selected terminal scope."""
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

    def preflight_complete_attempt_success(self, **kwargs: object) -> object:
        """Seal the success lifecycle closure before terminal publication writes."""
        ...

    def preflight_close_attempt_for_retry(self, **kwargs: object) -> object:
        """Seal a retry lifecycle closure before terminal publication writes."""
        ...

    def preflight_close_attempt_terminal(self, **kwargs: object) -> object:
        """Seal an active terminal lifecycle closure before publication writes."""
        ...

    def preflight_terminate_inactive_run(self, **kwargs: object) -> object:
        """Seal an inactive terminal lifecycle closure before publication writes."""
        ...

    def apply_preflighted_terminal_closure(self, seal: object) -> object:
        """Consume a lifecycle seal using DML only."""
        ...


@dataclass(frozen=True, slots=True)
class AuditPublicationIntent:
    """Immutable terminal-audit intent handed to a transaction-bound adapter."""

    action: str
    run: RunRecord
    artifact_id: str | None
    actor: AuditActor
    occurred_at: str
    deferred: bool = False
    request_id: str | None = None
    trace_id: str | None = None
    chain_id: str | None = None
    append_intent: AuditAppendIntent | None = None


class AuditPort(Protocol):
    def record(
        self,
        *,
        action: str,
        run: RunRecord,
        artifact_id: str | None,
        actor: AuditActor,
        occurred_at: str,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> None: ...

    def preflight_records(
        self,
        records: Sequence[AuditPublicationIntent],
    ) -> object: ...

    def apply_preflighted_records(self, prepared: object) -> None: ...


def _require_registered_source_provenance(
    provenance: ProvenanceV1,
    *,
    required_prompt_purposes: frozenset[str] = frozenset(),
    label: str,
) -> None:
    registry = build_source_kind_registry()
    definition = (
        registry.get(provenance.source_kind_id)
        if provenance.source_kind_registry_version == registry.registry_version
        else None
    )
    if (
        definition is None
        or provenance.trust not in definition.allowed_trust_levels
        or not required_prompt_purposes.issubset(definition.allowed_prompt_purposes)
    ):
        raise IntegrityViolation(f"{label} escapes the source-kind registry")


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
    tool_intermediate_links: Sequence[RunToolIntermediateLinkV1] = (),
    record_shards: Sequence[tuple[int, int, str]],
    closed: Mapping[str, int | None],
    attempt_bundle_id: str | None,
    run_bundle_id: str | None,
    replay_input_id: str | None,
    committed_link_counts: Mapping[object, int],
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
        if len(bindings) >= MAX_RUN_MANIFEST_PARENT_BINDINGS:
            raise IntegrityViolation("runtime parent projection exceeds its hard cap")
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

    prompt_keys: list[tuple[int, int, int]] = []
    for link in prompt_links:
        if link.run_id != run_id:
            raise IntegrityViolation("prompt link belongs to another Run")
        if manifest_scope == "attempt" and link.attempt_no != current_attempt_no:
            raise IntegrityViolation("attempt manifest contains another attempt's prompt link")
        if manifest_scope == "run" and (
            current_attempt_no is None or link.attempt_no > current_attempt_no
        ):
            raise IntegrityViolation("run manifest contains a future prompt link")
        prompt_keys.append((link.attempt_no, link.call_ordinal, link.route_ordinal))
        append_parent(
            artifact_id=link.artifact_id,
            source="published_intermediate",
            role="intermediate",
            publication="run_published",
            attempt_no=link.attempt_no,
            ordinal=link.call_ordinal,
        )
    if len(prompt_keys) != len(set(prompt_keys)):
        raise IntegrityViolation("runtime prompt links contain a duplicate call route")

    context_keys: list[tuple[int, int]] = []
    for link in tool_intermediate_links:
        if link.run_id != run_id:
            raise IntegrityViolation("prompt-context link belongs to another Run")
        if manifest_scope == "attempt" and link.attempt_no != current_attempt_no:
            raise IntegrityViolation(
                "attempt manifest contains another attempt's prompt-context link"
            )
        if manifest_scope == "run" and (
            current_attempt_no is None or link.attempt_no > current_attempt_no
        ):
            raise IntegrityViolation("run manifest contains a future prompt-context link")
        info = artifact_info_by_id.get(link.artifact_id)
        if info is None or info.payload_hash != link.payload_hash:
            raise IntegrityViolation(
                "prompt-context link payload hash differs from its immutable Artifact",
                artifact_id=link.artifact_id,
            )
        context_keys.append((link.attempt_no, link.target_call_ordinal))
        append_parent(
            artifact_id=link.artifact_id,
            source="published_intermediate",
            role="intermediate",
            publication="run_published",
            attempt_no=link.attempt_no,
            ordinal=link.target_call_ordinal,
        )
    if len(context_keys) != len(set(context_keys)):
        raise IntegrityViolation("runtime prompt-context links contain a duplicate target call")

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
        prompt_call_keys = {
            (attempt_no, call_ordinal) for attempt_no, call_ordinal, _ in prompt_keys
        }
        if not consumed_response_call_keys.issubset(prompt_call_keys):
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

    scoped_count_name = "current_attempt" if manifest_scope == "attempt" else "all_attempts"
    exact_link_counts = dict(committed_link_counts)
    exact_link_counts[("prompt_rendered", scoped_count_name)] = len(prompt_links)
    exact_link_counts[("agent_prompt_context", scoped_count_name)] = len(tool_intermediate_links)
    validate_runtime_parents(
        rule_set=rule_set,
        manifest_scope=manifest_scope,
        llm_execution_mode=llm_execution_mode,
        parents=projected,
        committed_link_counts=exact_link_counts,
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
class _TaskSuiteTerminalAuthority:
    """Run-frozen TaskSuite authorities shared by every child in one draft."""

    run_id: str
    run_payload_hash: str
    environment_details: EnvironmentProfileDetailsV1
    derivation_config: TaskSuiteDerivationProfileConfigV2
    oracle_registry: CompletionOracleRegistryV1
    validator: PlaytestPayloadValidationService


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

    return isinstance(stored, ArtifactV2) and (
        stored.artifact_id,
        stored.lineage_schema_version,
        stored.kind,
        stored.version_tuple,
        stored.lineage,
        stored.payload_hash,
        stored.object_ref,
        stored.meta,
    ) == (
        expected.artifact_id,
        expected.lineage_schema_version,
        expected.kind,
        expected.version_tuple,
        expected.lineage,
        expected.payload_hash,
        expected.object_ref,
        expected.meta,
    )


@dataclass(frozen=True, slots=True)
class _ArtifactWrite:
    slot: str
    artifact: ArtifactV2
    retained_binding: PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding | None = None

    def terminal_seal(self) -> "_ArtifactWrite":
        retained = self.retained_binding
        return _ArtifactWrite(
            slot=self.slot,
            artifact=deep_freeze_value(self.artifact),  # type: ignore[arg-type]
            retained_binding=(
                None
                if retained is None
                else PreverifiedArtifactBinding(
                    binding=deep_freeze_value(retained.binding),  # type: ignore[arg-type]
                    stat=deep_freeze_value(retained.stat),  # type: ignore[arg-type]
                )
                if isinstance(retained, PreverifiedArtifactBinding)
                else PreverifiedAbsentArtifactBinding(
                    object_ref=ObjectRef.model_validate(retained.object_ref.model_dump(mode="json"))
                )
            ),
        )

    def terminal_projection(self) -> Mapping[str, object]:
        return _operation_projection(self)


@dataclass(frozen=True, slots=True)
class _FindingWrite:
    planned: PlannedFindingWrite

    def terminal_seal(self) -> "_FindingWrite":
        return _FindingWrite(
            planned=PlannedFindingWrite(
                revision=deep_freeze_value(self.planned.revision),  # type: ignore[arg-type]
                expected_current_revision=self.planned.expected_current_revision,
                link=deep_freeze_value(self.planned.link),  # type: ignore[arg-type]
            )
        )

    def terminal_projection(self) -> Mapping[str, object]:
        return _operation_projection(self)


@dataclass(frozen=True, slots=True)
class _WorkflowWrite:
    effect_key: str
    context: WorkflowEffectContext
    prepared: PreparedWorkflowEffect

    def terminal_seal(self) -> "_WorkflowWrite":
        context = self.context
        return _WorkflowWrite(
            effect_key=self.effect_key,
            context=replace(
                context,
                run=deep_freeze_value(context.run),  # type: ignore[arg-type]
                policy=deep_freeze_value(context.policy),  # type: ignore[arg-type]
                approvals=None,
                actor=deep_freeze_value(context.actor),  # type: ignore[arg-type]
                published_primary_payload=deep_freeze_value(context.published_primary_payload),
                published_artifact_ids_by_rule=deep_freeze_value(
                    context.published_artifact_ids_by_rule
                ),
                published_payloads_by_rule=deep_freeze_value(context.published_payloads_by_rule),
                published_artifacts_by_rule=deep_freeze_value(context.published_artifacts_by_rule),
                agent_drafts=None,
                auto_apply=None,
            ),
            prepared=self.prepared,
        )

    def terminal_projection(self) -> Mapping[str, object]:
        return _operation_projection(self)


@dataclass(frozen=True, slots=True)
class _AuditWrite:
    action: str
    run: RunRecord
    artifact_id: str | None
    actor: AuditActor
    occurred_at: str
    request_id: str | None = None
    trace_id: str | None = None

    def terminal_seal(self) -> "_AuditWrite":
        return _AuditWrite(
            action=self.action,
            run=deep_freeze_value(self.run),  # type: ignore[arg-type]
            artifact_id=self.artifact_id,
            actor=deep_freeze_value(self.actor),  # type: ignore[arg-type]
            occurred_at=self.occurred_at,
            request_id=self.request_id,
            trace_id=self.trace_id,
        )

    def terminal_projection(self) -> Mapping[str, object]:
        return _operation_projection(self)

    def publication_intent(self) -> AuditPublicationIntent:
        return AuditPublicationIntent(
            action=self.action,
            run=self.run,
            artifact_id=self.artifact_id,
            actor=self.actor,
            occurred_at=self.occurred_at,
            request_id=self.request_id,
            trace_id=self.trace_id,
        )


def _terminal_lifecycle_audit_actions(
    publication_kinds: tuple[str, ...],
) -> tuple[str, ...]:
    actions_by_publication = {
        ("run_result",): ("run.terminal",),
        ("attempt_failure",): ("run.attempt_closed",),
        ("attempt_failure", "run_failure"): (
            "run.attempt_closed",
            "run.terminal",
        ),
        ("run_failure",): ("run.terminal",),
    }
    actions = actions_by_publication.get(publication_kinds)
    if actions is None:
        raise IntegrityViolation(
            "terminal publication aggregate has no lifecycle Audit projection",
            publication_kinds=publication_kinds,
        )
    return actions


def _terminal_audit_intents(
    *,
    publication_kinds: tuple[str, ...],
    audit_operations_by_publication: tuple[tuple[_AuditWrite, ...], ...],
    command_audit_correlation: AuditCorrelation | None = None,
) -> tuple[AuditPublicationIntent, ...]:
    if len(publication_kinds) != len(audit_operations_by_publication) or any(
        len(operations) != 1 for operations in audit_operations_by_publication
    ):
        raise IntegrityViolation(
            "each terminal publication requires exactly one publication Audit intent"
        )
    operations = tuple(operations[0] for operations in audit_operations_by_publication)
    anchor = operations[0]
    if any(
        operation.run.run_id != anchor.run.run_id
        or operation.run.initiated_by != anchor.run.initiated_by
        or operation.actor != anchor.actor
        or operation.occurred_at != anchor.occurred_at
        or operation.request_id != anchor.request_id
        or operation.trace_id != anchor.trace_id
        for operation in operations[1:]
    ):
        raise IntegrityViolation("terminal publication aggregate Audit identities are inconsistent")
    immediate = tuple(operation.publication_intent() for operation in operations)
    command_deferred: tuple[AuditPublicationIntent, ...] = ()
    lifecycle_request_id = anchor.request_id
    lifecycle_trace_id = anchor.trace_id
    if command_audit_correlation is not None:
        if publication_kinds != ("run_failure",):
            raise IntegrityViolation(
                "command Audit correlation requires one single inactive Run failure"
            )
        if command_audit_correlation.run_id != anchor.run.run_id:
            raise IntegrityViolation("command Audit correlation differs from the terminal Run")
        lifecycle_request_id = command_audit_correlation.request_id
        lifecycle_trace_id = command_audit_correlation.trace_id
        immediate = tuple(
            replace(
                intent,
                request_id=lifecycle_request_id,
                trace_id=lifecycle_trace_id,
            )
            for intent in immediate
        )
        command_deferred = (
            AuditPublicationIntent(
                action="run.command_submitted",
                run=anchor.run,
                artifact_id=None,
                actor=anchor.actor,
                occurred_at=anchor.occurred_at,
                deferred=True,
                request_id=lifecycle_request_id,
                trace_id=lifecycle_trace_id,
            ),
        )
    deferred = tuple(
        AuditPublicationIntent(
            action=action,
            run=anchor.run,
            artifact_id=None,
            actor=anchor.actor,
            occurred_at=anchor.occurred_at,
            deferred=True,
            request_id=lifecycle_request_id,
            trace_id=lifecycle_trace_id,
        )
        for action in _terminal_lifecycle_audit_actions(publication_kinds)
    )
    return (*immediate, *command_deferred, *deferred)


def _workflow_audit_intents(
    *,
    workflow_seals: Sequence[PreflightedWorkflowEffect],
    workflow_operations: Sequence["_WorkflowWrite"],
) -> tuple[AuditPublicationIntent, ...]:
    """Project approval/workflow audit into the terminal's single chain batch."""

    if len(workflow_seals) != len(workflow_operations):
        raise IntegrityViolation("workflow Audit preflight cardinality differs")
    projected: list[AuditPublicationIntent] = []
    retained_chain_id: str | None = None
    for seal, operation in zip(workflow_seals, workflow_operations, strict=True):
        audit_batch = seal.audit_intents_for_terminal_merge(operation.context)
        if audit_batch is None:
            continue
        if retained_chain_id is None:
            retained_chain_id = audit_batch.chain_id
        elif audit_batch.chain_id != retained_chain_id:
            raise IntegrityViolation("terminal workflow Audit spans multiple chains")
        context = operation.context
        for intent in audit_batch.intents:
            if (
                intent.actor != context.actor
                or intent.initiated_by != context.run.initiated_by
                or intent.correlation.run_id != context.run.run_id
            ):
                raise IntegrityViolation(
                    "workflow Audit intent differs from terminal Run authority"
                )
            projected.append(
                AuditPublicationIntent(
                    action=intent.action,
                    run=context.run,
                    artifact_id=intent.subject.artifact_id,
                    actor=intent.actor,
                    occurred_at=context.occurred_at,
                    request_id=intent.correlation.request_id,
                    trace_id=intent.correlation.trace_id,
                    chain_id=audit_batch.chain_id,
                    append_intent=intent,
                )
            )
    return tuple(projected)


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


def _planning_subject_digest(
    publication_kind: str,
    *,
    run: RunRecord,
    attempt: RunAttempt | None,
    prepared: PreparedRunResult | PreparedRunFailure,
    policy: OutcomeArtifactPolicyV1,
    retry_decision: RetryDecisionV1 | None,
    attempt_failure_artifact_id: str | None,
    occurred_at: str,
    actor: AuditActor,
) -> str:
    """Bind a draft to the compact lifecycle selector used again at commit.

    The complete prepared outcome and policy closure are already sealed into the
    operation/result projection before staging.  Re-serializing up to 10,000
    Findings and 1,025 prepared Artifacts while SQLite holds ``BEGIN IMMEDIATE``
    would add no authority: mutable Run/Attempt/runtime rows are independently
    covered by ``runtime_authority_digest``.  Keep this second digest deliberately
    compact and bind only the caller-visible selector, identity, timestamp and
    actor needed to prove the write phase is committing the same terminal branch.
    """

    if isinstance(prepared, PreparedRunResult):
        prepared_selector: Mapping[str, object] = {
            "prepared_schema_version": prepared.prepared_schema_version,
            "run_id": prepared.run_id,
            "attempt_no": prepared.attempt_no,
            "run_kind": prepared.run_kind.model_dump(mode="json"),
            "outcome_code": prepared.summary.outcome_code,
            "primary_artifact_kind": prepared.summary.primary_artifact_kind,
            "prepared_domain_artifact_count": (prepared.summary.prepared_domain_artifact_count),
            "prepared_finding_count": prepared.summary.prepared_finding_count,
        }
    else:
        prepared_selector = {
            "prepared_schema_version": prepared.prepared_schema_version,
            "run_id": prepared.run_id,
            "attempt_no": prepared.attempt_no,
            "run_kind": prepared.run_kind.model_dump(mode="json"),
            "cause_code": prepared.cause_code,
            "failure_class": prepared.failure_class,
            "intrinsic_retry_eligible": prepared.intrinsic_retry_eligible,
            "classifier": prepared.classifier.model_dump(mode="json"),
        }

    policy_selector = {
        "policy_schema_version": policy.policy_schema_version,
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "outcome_code": policy.outcome_code,
        "prepared_outcome": policy.prepared_outcome,
        "publication_scope": policy.publication_scope,
        "attempt_terminal_status": policy.attempt_terminal_status,
        "run_status_after_publication": policy.run_status_after_publication,
        "failure_class": policy.failure_class,
        "retry_disposition": policy.retry_disposition,
        "workflow_effect_key": policy.workflow_effect_key,
        "version_transition_policy_ref": (
            policy.version_transition_policy_ref.model_dump(mode="json")
        ),
    }

    return canonical_sha256(
        {
            "publication_kind": publication_kind,
            "run_id": run.run_id,
            "run_kind": run.kind.model_dump(mode="json"),
            "attempt_no": None if attempt is None else attempt.attempt_no,
            "prepared_selector": prepared_selector,
            "policy_selector": policy_selector,
            "retry_decision": _model_projection(retry_decision),
            "attempt_failure_artifact_id": attempt_failure_artifact_id,
            "occurred_at": occurred_at,
            "actor": _model_projection(actor),
        }
    )


def _operation_projection(operation: object) -> Mapping[str, object]:
    if isinstance(operation, _ArtifactWrite):
        return {
            "operation": "artifact.put_staged",
            "slot": operation.slot,
            "artifact": operation.artifact.model_dump(mode="json"),
            "retained_binding": (
                None
                if operation.retained_binding is None
                else {
                    "binding": operation.retained_binding.binding.model_dump(mode="json"),
                    "stat": operation.retained_binding.stat.model_dump(mode="json"),
                }
                if isinstance(operation.retained_binding, PreverifiedArtifactBinding)
                else {
                    "absent": True,
                    "object_ref": operation.retained_binding.object_ref.model_dump(mode="json"),
                }
            ),
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
            "prepared_workflow": operation.prepared.canonical_projection(),
        }
    if isinstance(operation, _AuditWrite):
        return {
            "operation": "audit.record",
            "action": operation.action,
            "run": operation.run.model_dump(mode="json"),
            "artifact_id": operation.artifact_id,
            "actor": operation.actor.model_dump(mode="json"),
            "occurred_at": operation.occurred_at,
            "request_id": operation.request_id,
            "trace_id": operation.trace_id,
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
        planning_subject_digest: str | None = None,
        runtime_authority_digest: str | None = None,
        artifact_preflight: Callable[
            [ArtifactV2], PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding
        ]
        | None = None,
    ) -> None:
        self.publication_kind = publication_kind
        self.run_id = run_id
        self.attempt_no = attempt_no
        self.occurred_at = occurred_at
        self.planning_subject_digest = planning_subject_digest
        self.runtime_authority_digest = runtime_authority_digest
        self._artifact_preflight = artifact_preflight
        self.materials: list[BlobMaterial] = []
        self.operations: list[object] = []
        self._pending_slots_by_ref: dict[str, deque[str]] = {}
        self._materials_by_slot: dict[str, BlobMaterial] = {}

    def add_blob(self, payload: bytes) -> ObjectRef:
        expected_ref = object_ref_for_bytes(payload)
        slot = f"blob:{len(self.materials) + 1:04d}"
        material = BlobMaterial(slot=slot, payload=payload, expected_ref=expected_ref)
        self.materials.append(material)
        self._materials_by_slot[slot] = material
        self._pending_slots_by_ref.setdefault(expected_ref.key, deque()).append(slot)
        return expected_ref

    def add_artifact(self, artifact: ArtifactV2) -> ArtifactV2:
        pending = self._pending_slots_by_ref.get(artifact.object_ref.key)
        if not pending:
            raise IntegrityViolation(
                "planned Artifact has no exact blob material",
                artifact_id=artifact.artifact_id,
            )
        slot = pending.popleft()
        material = self._materials_by_slot.get(slot)
        if material is None or material.expected_ref != artifact.object_ref:
            raise IntegrityViolation(
                "planned Artifact differs from its blob material",
                artifact_id=artifact.artifact_id,
                slot=slot,
            )
        retained_binding = (
            None if self._artifact_preflight is None else self._artifact_preflight(artifact)
        )
        self.operations.append(
            _ArtifactWrite(
                slot=slot,
                artifact=artifact,
                retained_binding=retained_binding,
            )
        )
        return artifact

    def add_finding(self, planned: PlannedFindingWrite) -> None:
        self.operations.append(_FindingWrite(planned=planned))

    def add_workflow(self, effect_key: str, context: WorkflowEffectContext) -> None:
        self.operations.append(
            _WorkflowWrite(
                effect_key=effect_key,
                context=context,
                prepared=prepare_workflow_effect(effect_key, context),
            )
        )

    def add_audit(
        self,
        *,
        action: str,
        run: RunRecord,
        artifact_id: str | None,
        actor: AuditActor,
        occurred_at: str,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.operations.append(
            _AuditWrite(
                action=action,
                run=run,
                artifact_id=artifact_id,
                actor=actor,
                occurred_at=occurred_at,
                request_id=request_id,
                trace_id=trace_id,
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
        base: dict[str, object] = {
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
        if self.planning_subject_digest is not None:
            base["planning_subject_digest"] = self.planning_subject_digest
            base["runtime_authority_digest"] = self.runtime_authority_digest
        return TerminalPublicationDraft(
            publication_kind=self.publication_kind,
            run_id=self.run_id,
            attempt_no=self.attempt_no,
            occurred_at=self.occurred_at,
            planning_subject_digest=self.planning_subject_digest,
            runtime_authority_digest=self.runtime_authority_digest,
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
        playtest_payload_validator: PlaytestPayloadValidationService | None = None,
    ) -> None:
        self._registry = registry
        self._artifacts = artifacts
        self._blobs = blobs
        self._findings = findings
        self._ledger = ledger
        self._audit = audit
        self._producer_facts = producer_facts
        self._payload_decoders = dict(payload_decoders or {})
        self._playtest_payload_validator = playtest_payload_validator
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
        self._artifact_cache: dict[str, ArtifactV2] = {}
        self._published_blob_cache: dict[str, bytes] = {}
        self._runtime_parent_cache: dict[str, ParentInfo] = {}
        self._attempt_cache: dict[tuple[str, int], RunAttempt | None] = {}

    def _attempt_authority(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        key = (run_id, attempt_no)
        if key not in self._attempt_cache:
            self._attempt_cache[key] = self._ledger.get_attempt(run_id, attempt_no)
        return self._attempt_cache[key]

    def _initial_runtime_authority_digest(
        self,
        run_id: str,
        *,
        publication_kind: str,
    ) -> str | None:
        resolver = (
            getattr(self._ledger, "terminal_attempt_authority_digest", None)
            if publication_kind == "attempt_failure"
            else None
        )
        if not callable(resolver):
            resolver = getattr(self._ledger, "terminal_authority_digest", None)
        if not callable(resolver):
            # Direct plan/stage/commit unit ports intentionally have no persistent
            # runtime authority.  Lifecycle production commits use the required
            # prepared-commit surface below and fail closed when it is absent.
            return None
        digest = resolver(run_id)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise IntegrityViolation("terminal runtime authority digest is not canonical")
        return digest

    def _fresh_runtime_authority_digest(
        self,
        run_id: str,
        *,
        publication_kind: str,
    ) -> str:
        resolver = (
            getattr(self._ledger, "fresh_terminal_attempt_authority_digest", None)
            if publication_kind == "attempt_failure"
            else None
        )
        if publication_kind == "attempt_failure" and not callable(resolver):
            resolver = getattr(self._ledger, "terminal_attempt_authority_digest", None)
        if not callable(resolver):
            resolver = getattr(self._ledger, "terminal_authority_digest", None)
        digest = None if not callable(resolver) else resolver(run_id)
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise IntegrityViolation("terminal runtime authority digest is not canonical")
        if digest is None:
            raise IntegrityViolation(
                "terminal publication lacks fresh runtime authority projection",
                run_id=run_id,
            )
        return digest

    def _rebind_operations_for_commit(
        self,
        draft: TerminalPublicationDraft,
        *,
        expected_kind: str,
        expected_run_id: str,
        expected_attempt_no: int | None,
        expected_subject_digest: str,
        fresh_runtime_authority_digest: str,
    ) -> tuple[object, ...]:
        """Rebind only transaction capabilities after exact fresh-facts checks.

        Immutable blobs, schema/exporter/shaper validation and the complete
        operation projection were already produced in the read snapshot and bound
        to the staged receipts.  The write UoW compares the exact lifecycle subject
        plus the bounded runtime-authority digest, then replaces only opaque
        workflow ports with this transaction's capabilities.  Those ports are
        intentionally absent from the canonical operation projection.
        """

        try:
            state = _terminal_publication_state(draft, expected_phase="sealed")
        except IntegrityViolation as exc:
            raise TerminalAuthorityDrift(
                "fresh terminal authority differs from the staged immutable plan",
                publication_kind=expected_kind,
                run_id=expected_run_id,
            ) from exc
        if (
            state.publication_kind != expected_kind
            or state.run_id != expected_run_id
            or state.attempt_no != expected_attempt_no
            or state.planning_subject_digest != expected_subject_digest
            or state.runtime_authority_digest != fresh_runtime_authority_digest
        ):
            raise TerminalAuthorityDrift(
                "fresh terminal authority differs from the staged immutable plan",
                publication_kind=expected_kind,
                run_id=expected_run_id,
            )
        rebound_operations = tuple(
            replace(
                operation,
                context=replace(
                    operation.context,
                    approvals=self._approvals,
                    agent_drafts=self._agent_drafts,
                    auto_apply=self._auto_apply,
                ),
            )
            if isinstance(operation, _WorkflowWrite)
            else operation
            for operation in state.operations
        )
        # Only three opaque transaction capabilities are replaced above.  They are
        # deliberately excluded from ``_operation_projection``; every canonical
        # field remains the exact read-phase object.  Do not reconstruct the draft
        # here: ``TerminalPublicationDraft.__post_init__`` would rehash the complete
        # operation projection and every blob while SQLite's writer lock is held.
        return rebound_operations

    # ---------------------------------------------------------- three phases
    def _collect_draft(
        self,
        *,
        publication_kind: str,
        runtime_authority_scope: Literal["attempt", "run"] = "run",
        run_id: str,
        attempt_no: int | None,
        occurred_at: str,
        planning_subject_digest: str,
        publish: object,
    ) -> TerminalPublicationDraft:
        if self._collector is not None:
            raise IntegrityViolation("terminal publication planning cannot be nested")
        collector = _PublicationCollector(
            publication_kind=publication_kind,
            run_id=run_id,
            attempt_no=attempt_no,
            occurred_at=occurred_at,
            planning_subject_digest=planning_subject_digest,
            runtime_authority_digest=self._initial_runtime_authority_digest(
                run_id,
                publication_kind=(
                    "attempt_failure"
                    if runtime_authority_scope == "attempt"
                    else publication_kind
                    if publication_kind != "attempt_failure"
                    else "run_failure"
                ),
            ),
            artifact_preflight=getattr(self._artifacts, "preflight_binding", None),
        )
        self._collector = collector
        previous_overlay = self._planned_artifact_overlay
        # Planning may synthesize content-addressed cassette aggregates which later
        # operations in the same draft must read as if published.  Keep that
        # in-memory authority strictly draft-local; the active-failure aggregate
        # explicitly supplies the first draft's immutable operations to the second.
        self._planned_artifact_overlay = dict(previous_overlay)
        try:
            result = publish()  # type: ignore[operator]
            return collector.freeze(result)
        finally:
            self._planned_artifact_overlay = previous_overlay
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
            planning_subject_digest=_planning_subject_digest(
                "run_result",
                run=run,
                attempt=attempt,
                prepared=prepared,
                policy=policy,
                retry_decision=None,
                attempt_failure_artifact_id=None,
                occurred_at=occurred_at,
                actor=actor,
            ),
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
            runtime_authority_scope=("attempt" if retry_decision.decision == "retry" else "run"),
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            occurred_at=occurred_at,
            planning_subject_digest=_planning_subject_digest(
                "attempt_failure",
                run=run,
                attempt=attempt,
                prepared=prepared,
                policy=policy,
                retry_decision=retry_decision,
                attempt_failure_artifact_id=None,
                occurred_at=occurred_at,
                actor=actor,
            ),
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
            planning_subject_digest=_planning_subject_digest(
                "run_failure",
                run=run,
                attempt=attempt,
                prepared=prepared,
                policy=policy,
                retry_decision=retry_decision,
                attempt_failure_artifact_id=attempt_failure_artifact_id,
                occurred_at=occurred_at,
                actor=actor,
            ),
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

    def preflight_complete_attempt_success(self, **kwargs: object) -> object:
        return self._ledger.preflight_complete_attempt_success(**kwargs)

    def preflight_close_attempt_for_retry(self, **kwargs: object) -> object:
        return self._ledger.preflight_close_attempt_for_retry(**kwargs)

    def preflight_close_attempt_terminal(self, **kwargs: object) -> object:
        return self._ledger.preflight_close_attempt_terminal(**kwargs)

    def preflight_terminate_inactive_run(self, **kwargs: object) -> object:
        return self._ledger.preflight_terminate_inactive_run(**kwargs)

    def apply_preflighted_terminal_closure(self, seal: object) -> object:
        return self._ledger.apply_preflighted_terminal_closure(seal)

    def commit_planned_run_result(
        self,
        draft: TerminalPublicationDraft,
        staged: StagedTerminalPublication,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunResult,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> object:
        fresh_runtime = self._fresh_runtime_authority_digest(
            run.run_id,
            publication_kind="run_result",
        )
        rebound_operations = self._rebind_operations_for_commit(
            draft,
            expected_kind="run_result",
            expected_run_id=run.run_id,
            expected_attempt_no=attempt.attempt_no,
            expected_subject_digest=_planning_subject_digest(
                "run_result",
                run=run,
                attempt=attempt,
                prepared=prepared,
                policy=policy,
                retry_decision=None,
                attempt_failure_artifact_id=None,
                occurred_at=occurred_at,
                actor=actor,
            ),
            fresh_runtime_authority_digest=fresh_runtime,
        )
        return self._commit_planned_many(((draft, staged, rebound_operations),))[0]

    def commit_planned_active_failure_aggregate(
        self,
        drafts: tuple[TerminalPublicationDraft, ...],
        staged: tuple[StagedTerminalPublication, ...],
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        attempt_policy: OutcomeArtifactPolicyV1,
        run_policy: OutcomeArtifactPolicyV1 | None,
        occurred_at: str,
        actor: AuditActor,
    ) -> tuple[object, ...]:
        expected_count = 1 if retry_decision.decision == "retry" else 2
        if len(drafts) != expected_count or len(staged) != expected_count:
            raise TerminalAuthorityDrift(
                "fresh failure selector differs from the staged aggregate",
                run_id=run.run_id,
                expected_count=expected_count,
                staged_count=len(staged),
            )
        if (run_policy is None) != (expected_count == 1):
            raise IntegrityViolation("active failure aggregate policy count is inconsistent")
        aggregate_fresh_runtime = self._fresh_runtime_authority_digest(
            run.run_id,
            publication_kind=("attempt_failure" if expected_count == 1 else "run_failure"),
        )
        rebound_operations: list[tuple[object, ...]] = []
        rebound_operations.append(
            self._rebind_operations_for_commit(
                drafts[0],
                expected_kind="attempt_failure",
                expected_run_id=run.run_id,
                expected_attempt_no=attempt.attempt_no,
                expected_subject_digest=_planning_subject_digest(
                    "attempt_failure",
                    run=run,
                    attempt=attempt,
                    prepared=prepared,
                    policy=attempt_policy,
                    retry_decision=retry_decision,
                    attempt_failure_artifact_id=None,
                    occurred_at=occurred_at,
                    actor=actor,
                ),
                fresh_runtime_authority_digest=aggregate_fresh_runtime,
            )
        )
        if expected_count == 2:
            attempt_result = drafts[0].result
            if not isinstance(attempt_result, AttemptFailurePublication) or run_policy is None:
                raise IntegrityViolation("staged attempt failure has another result projection")
            rebound_operations.append(
                self._rebind_operations_for_commit(
                    drafts[1],
                    expected_kind="run_failure",
                    expected_run_id=run.run_id,
                    expected_attempt_no=attempt.attempt_no,
                    expected_subject_digest=_planning_subject_digest(
                        "run_failure",
                        run=run,
                        attempt=attempt,
                        prepared=prepared,
                        policy=run_policy,
                        retry_decision=retry_decision,
                        attempt_failure_artifact_id=attempt_result.failure_artifact_id,
                        occurred_at=occurred_at,
                        actor=actor,
                    ),
                    fresh_runtime_authority_digest=aggregate_fresh_runtime,
                )
            )
        return self._commit_planned_many(
            tuple(
                (draft, staged_publication, operations)
                for draft, staged_publication, operations in zip(
                    drafts,
                    staged,
                    rebound_operations,
                    strict=True,
                )
            )
        )

    def commit_planned_run_failure(
        self,
        draft: TerminalPublicationDraft,
        staged: StagedTerminalPublication,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        attempt_failure_artifact_id: str | None,
        occurred_at: str,
        actor: AuditActor,
        command_audit_correlation: AuditCorrelation | None = None,
    ) -> object:
        fresh_runtime = self._fresh_runtime_authority_digest(
            run.run_id,
            publication_kind="run_failure",
        )
        rebound_operations = self._rebind_operations_for_commit(
            draft,
            expected_kind="run_failure",
            expected_run_id=run.run_id,
            expected_attempt_no=attempt.attempt_no if attempt is not None else None,
            expected_subject_digest=_planning_subject_digest(
                "run_failure",
                run=run,
                attempt=attempt,
                prepared=prepared,
                policy=policy,
                retry_decision=retry_decision,
                attempt_failure_artifact_id=attempt_failure_artifact_id,
                occurred_at=occurred_at,
                actor=actor,
            ),
            fresh_runtime_authority_digest=fresh_runtime,
        )
        return self._commit_planned_many(
            ((draft, staged, rebound_operations),),
            command_audit_correlation=command_audit_correlation,
        )[0]

    def _commit_planned_many(
        self,
        publications: tuple[
            tuple[TerminalPublicationDraft, StagedTerminalPublication, tuple[object, ...]],
            ...,
        ],
        *,
        command_audit_correlation: AuditCorrelation | None = None,
    ) -> tuple[object, ...]:
        """Commit already sealed plans without re-reading or rehashing blob payloads.

        The lifecycle-only ``commit_planned_*`` surface receives drafts created by
        the trusted read-phase planner and receipts created by the trusted stager.
        Fresh lifecycle and runtime digests were compared immediately before this
        call.  Validate the compact digest/slot/ref closure for the whole aggregate
        before its first write, then execute the retained operations.  The general
        ``commit`` surface below keeps its deeper mutation checks for direct callers.
        """

        validated: list[tuple[tuple[object, ...], Mapping[str, StagedReceipt]]] = []
        publication_kinds: list[str] = []
        results: list[object] = []
        for draft, staged, operations in publications:
            state = _terminal_publication_state(draft, expected_phase="sealed")
            if staged.projection_digest != state.projection_digest:
                raise IntegrityViolation(
                    "staged terminal projection differs from its sealed plan",
                    run_id=state.run_id,
                )
            receipts = {receipt.slot: receipt for receipt in staged.receipts}
            materials = {material.slot: material for material in state.materials}
            if len(receipts) != len(staged.receipts) or set(receipts) != set(materials):
                raise IntegrityViolation(
                    "staged receipt slots do not exactly close the sealed plan",
                    run_id=state.run_id,
                )
            artifact_slots = tuple(
                operation.slot for operation in operations if isinstance(operation, _ArtifactWrite)
            )
            if len(artifact_slots) != len(set(artifact_slots)) or set(artifact_slots) != set(
                materials
            ):
                raise IntegrityViolation(
                    "sealed plan blob slots do not map one-to-one onto Artifact writes",
                    run_id=state.run_id,
                )
            for slot, material in materials.items():
                if receipts[slot].ref != material.expected_ref:
                    raise IntegrityViolation(
                        "staged receipt ref differs from the sealed plan material",
                        slot=slot,
                    )
            validated.append((operations, receipts))
            publication_kinds.append(state.publication_kind)
            results.append(deep_freeze_value(state.result))

        preflight_artifacts = getattr(self._artifacts, "preflight_staged_many", None)
        put_preflighted_artifacts = getattr(self._artifacts, "put_preflighted_many", None)
        if callable(preflight_artifacts) != callable(put_preflighted_artifacts):
            raise IntegrityViolation("Artifact batch preflight capability is partial")
        if not callable(preflight_artifacts) or not callable(put_preflighted_artifacts):
            raise IntegrityViolation(
                "planned terminal publication requires Artifact batch preflight/apply"
            )
        if callable(preflight_artifacts):
            artifact_writes: list[
                tuple[
                    ArtifactV2,
                    StagedReceipt,
                    PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding,
                ]
            ] = []
            for operations, receipts in validated:
                for operation in operations:
                    if not isinstance(operation, _ArtifactWrite):
                        continue
                    receipt = receipts[operation.slot]
                    planned_binding = operation.retained_binding
                    if not isinstance(
                        planned_binding,
                        (PreverifiedArtifactBinding, PreverifiedAbsentArtifactBinding),
                    ):
                        raise IntegrityViolation(
                            "production terminal Artifact lacks a read-phase binding proof",
                            artifact_id=operation.artifact.artifact_id,
                        )
                    artifact_writes.append((operation.artifact, receipt, planned_binding))
            artifact_batch = preflight_artifacts(tuple(artifact_writes))

            finding_operations = tuple(
                operation
                for operations, _receipts in validated
                for operation in operations
                if isinstance(operation, _FindingWrite)
            )
            preflight_findings = getattr(self._findings, "preflight_put_many", None)
            put_preflighted_findings = getattr(self._findings, "put_preflighted_many", None)
            preflight_links = getattr(self._ledger, "preflight_finding_links_many", None)
            put_preflighted_links = getattr(
                self._ledger,
                "put_preflighted_finding_links_many",
                None,
            )
            finding_capabilities = (
                callable(preflight_findings),
                callable(put_preflighted_findings),
                callable(preflight_links),
                callable(put_preflighted_links),
            )
            if finding_operations and not all(finding_capabilities):
                raise IntegrityViolation("Finding batch preflight capability is incomplete")
            if any(finding_capabilities) and not all(finding_capabilities):
                raise IntegrityViolation("Finding batch preflight capability is partial")
            finding_seal = None
            link_seal = None
            if finding_operations:
                finding_seal = preflight_findings(  # type: ignore[operator]
                    tuple(
                        (
                            operation.planned.revision,
                            operation.planned.expected_current_revision,
                        )
                        for operation in finding_operations
                    )
                )
                link_seal = preflight_links(  # type: ignore[operator]
                    tuple(operation.planned.link for operation in finding_operations),
                    planned_findings=tuple(
                        operation.planned.revision for operation in finding_operations
                    ),
                    planned_artifact_ids=tuple(
                        dict.fromkeys(
                            operation.artifact.artifact_id
                            for operations, _receipts in validated
                            for operation in operations
                            if isinstance(operation, _ArtifactWrite)
                        )
                    ),
                )

            workflow_operations = tuple(
                operation
                for operations, _receipts in validated
                for operation in operations
                if isinstance(operation, _WorkflowWrite)
            )
            workflow_seals = tuple(
                preflight_prepared_workflow_effect(
                    operation.prepared,
                    operation.context,
                    merge_audit_into_terminal_batch=True,
                )
                for operation in workflow_operations
            )

            audit_operations_by_publication = tuple(
                tuple(operation for operation in operations if isinstance(operation, _AuditWrite))
                for operations, _receipts in validated
            )
            preflight_audits = getattr(self._audit, "preflight_records", None)
            apply_preflighted_audits = getattr(
                self._audit,
                "apply_preflighted_records",
                None,
            )
            if not callable(preflight_audits) or not callable(apply_preflighted_audits):
                raise IntegrityViolation(
                    "production terminal Audit batch preflight capability is required"
                )
            workflow_audit_intents = _workflow_audit_intents(
                workflow_seals=workflow_seals,
                workflow_operations=workflow_operations,
            )
            audit_batch = preflight_audits(
                (
                    *workflow_audit_intents,
                    *_terminal_audit_intents(
                        publication_kinds=tuple(publication_kinds),
                        audit_operations_by_publication=audit_operations_by_publication,
                        command_audit_correlation=command_audit_correlation,
                    ),
                )
            )

            consume_terminal_publications(
                tuple(draft for draft, _staged, _operations in publications),
                expected_phase="sealed",
            )
            stored_artifacts = put_preflighted_artifacts(artifact_batch)
            if len(stored_artifacts) != len(artifact_writes):
                raise IntegrityViolation("Artifact batch publisher returned another cardinality")
            for stored, (expected, _receipt, _binding) in zip(
                stored_artifacts,
                artifact_writes,
                strict=True,
            ):
                if not _same_immutable_artifact(stored, expected):
                    raise IntegrityViolation(
                        "Artifact batch publisher returned another immutable Artifact",
                        artifact_id=expected.artifact_id,
                    )
            if finding_seal is not None and link_seal is not None:
                stored_findings = put_preflighted_findings(finding_seal)  # type: ignore[operator]
                expected_findings = tuple(
                    operation.planned.revision for operation in finding_operations
                )
                if stored_findings != expected_findings:
                    raise IntegrityViolation(
                        "Finding batch publisher returned another immutable revision"
                    )
                stored_links = put_preflighted_links(link_seal)  # type: ignore[operator]
                expected_links = tuple(operation.planned.link for operation in finding_operations)
                if stored_links != expected_links:
                    raise IntegrityViolation(
                        "Finding-link batch publisher returned another immutable link"
                    )
            for workflow_seal, workflow_operation in zip(
                workflow_seals,
                workflow_operations,
                strict=True,
            ):
                apply_preflighted_workflow_effect(
                    workflow_seal,
                    workflow_operation.context,
                )
            apply_preflighted_audits(audit_batch)
        return tuple(results)

    def commit(
        self,
        fresh_draft: TerminalPublicationDraft,
        staged: StagedTerminalPublication,
    ) -> object:
        """Commit one compatibility/test draft without any ObjectStore write.

        Production lifecycle drafts carry ``runtime_authority_digest`` and are
        rejected by this surface; they must use ``commit_planned_*`` so complete
        outcome projection work stays outside the SQLite writer lock. Direct test
        callers without persistent runtime authority retain the deeper mutation
        checks here.
        """

        return self.commit_many(((fresh_draft, staged),))[0]

    def commit_many(
        self,
        publications: tuple[tuple[TerminalPublicationDraft, StagedTerminalPublication], ...],
    ) -> tuple[object, ...]:
        """Validate a compatibility/test aggregate before its first DB write."""

        source_states = tuple(
            _terminal_publication_state(draft, expected_phase="draft")
            for draft, _staged in publications
        )
        if any(state.runtime_authority_digest is not None for state in source_states):
            raise IntegrityViolation(
                "authority-bound terminal drafts require the planned commit surface"
            )
        validated: list[tuple[_TerminalPublicationState, Mapping[str, StagedReceipt]]] = []
        for state, (_fresh_draft, staged) in zip(source_states, publications, strict=True):
            current_operation_projection = tuple(
                _operation_projection(operation) for operation in state.operations
            )
            current_result_projection = _model_projection(state.result)
            if (
                current_operation_projection != state.operation_projection
                or current_result_projection != state.result_projection
            ):
                raise IntegrityViolation(
                    "terminal draft operations/result mutated after projection",
                    run_id=state.run_id,
                )
            if staged.projection_digest != state.projection_digest:
                raise IntegrityViolation(
                    "fresh terminal projection differs from the staged projection",
                    run_id=state.run_id,
                )
            receipts = {receipt.slot: receipt for receipt in staged.receipts}
            materials = {material.slot: material for material in state.materials}
            if set(receipts) != set(materials):
                raise IntegrityViolation(
                    "staged receipt slots do not exactly close the fresh draft",
                    run_id=state.run_id,
                )
            artifact_slots = tuple(
                operation.slot
                for operation in state.operations
                if isinstance(operation, _ArtifactWrite)
            )
            if len(artifact_slots) != len(set(artifact_slots)) or set(artifact_slots) != set(
                materials
            ):
                raise IntegrityViolation(
                    "fresh draft blob slots do not map one-to-one onto Artifact writes",
                    run_id=state.run_id,
                )
            for slot, material in materials.items():
                receipt = receipts[slot]
                if receipt.ref != material.expected_ref:
                    raise IntegrityViolation(
                        "staged receipt ref differs from the fresh draft material",
                        slot=slot,
                    )
            validated.append((state, receipts))

        consume_terminal_publications(
            tuple(fresh_draft for fresh_draft, _staged in publications),
            expected_phase="draft",
        )
        for state, receipts in validated:
            self._commit_operations(state.operations, receipts=receipts)
        return tuple(deep_freeze_value(state.result) for state, _ in validated)

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
                stored = self._artifacts.put_staged(
                    operation.artifact,
                    receipt,
                    operation.retained_binding,
                )
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
                commit_prepared_workflow_effect(
                    operation.prepared,
                    operation.context,
                )
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

    def record_command_submitted(self, **kwargs: object) -> None:
        self._record_event("run.command_submitted", kwargs)

    def record_run_terminal(self, **kwargs: object) -> None:
        self._record_event("run.terminal", kwargs)

    def _record_event(self, action: str, kwargs: Mapping[str, object]) -> None:
        run = kwargs.get("run")
        actor = kwargs.get("actor")
        event = kwargs.get("event")
        events = kwargs.get("events")
        if event is None and isinstance(events, tuple) and events:
            event = events[-1]
        attempt = kwargs.get("attempt")
        occurred_at = getattr(event, "occurred_at", None) if event is not None else None
        request_id = kwargs.get("request_id")
        trace_id = kwargs.get("trace_id")
        if request_id is not None and not isinstance(request_id, str):
            raise IntegrityViolation("terminal Audit request correlation is invalid")
        if trace_id is not None and not isinstance(trace_id, str):
            raise IntegrityViolation("terminal Audit trace correlation is invalid")
        if trace_id is None:
            event_trace_id = getattr(event, "trace_id", None)
            attempt_trace_id = getattr(attempt, "trace_id", None)
            trace_id = event_trace_id if event_trace_id is not None else attempt_trace_id
            if trace_id is not None and not isinstance(trace_id, str):
                raise IntegrityViolation("terminal Audit trace authority is invalid")
        if isinstance(run, RunRecord) and isinstance(actor, AuditActor):
            self._audit.record(
                action=action,
                run=run,
                artifact_id=None,
                actor=actor,
                occurred_at=occurred_at or run.updated_at,
                request_id=request_id,
                trace_id=trace_id,
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
            outcome_code=policy.outcome_code,
            require_complete=True,
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
            trace_id=attempt.trace_id,
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
            outcome_code=policy.outcome_code,
            require_complete=False,
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
            trace_id=attempt.trace_id,
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
            outcome_code=policy.outcome_code,
            require_complete=False,
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
            trace_id=attempt.trace_id if attempt is not None else None,
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
        authorized_run_parents = _merge_parent_sources(run_inputs, run_intermediates)
        siblings: dict[str, dict[str, ParentInfo]] = {}
        sibling_candidates: dict[str, ParentInfo] = {}
        available_references: Mapping[str, ParentInfo] = ChainMap(
            sibling_candidates,
            authorized_run_parents,
        )
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
        task_suite_prepared_total_bytes = (
            sum(view.object_ref.size_bytes for view in views)
            if isinstance(run.payload.params, TaskSuiteDerivePayloadV1)
            else None
        )
        task_suite_authority = (
            self._task_suite_terminal_authority(run)
            if isinstance(run.payload.params, TaskSuiteDerivePayloadV1)
            else None
        )
        authoritative_parent_payload_cache: dict[str, Mapping[str, object]] = {}

        for allocation in _topological_rule_order(allocations, plan):
            rule = allocation.plan_rule.rule
            lineage_policy = plan.lineage_by_rule_id[rule.rule_id]
            ids_by_rule.setdefault(rule.rule_id, [])
            payloads_by_rule.setdefault(rule.rule_id, [])
            artifacts_by_rule.setdefault(rule.rule_id, [])
            prepared_to_final_ids_by_rule.setdefault(rule.rule_id, {})
            child_payload_references = any(
                parent.source == "child_payload_reference" for parent in lineage_policy.parent_rules
            )
            prepared_source_rule_ids = tuple(
                dict.fromkeys(
                    parent.source_rule_id
                    for parent in lineage_policy.parent_rules
                    if parent.source == "prepared_rule" and parent.source_rule_id is not None
                )
            )
            prepared_sibling_sources = {
                source_rule_id: siblings.get(source_rule_id, {})
                for source_rule_id in prepared_source_rule_ids
            }
            for index in allocation.artifact_indexes:
                view = by_index[index]
                payload = dict(view.payload)
                object_ref = view.object_ref
                payload_hash = view.payload_hash
                child_references, referenced_ids = resolve_child_payload_references(
                    policy=lineage_policy,
                    child_payload=payload,
                    available_parents=(available_references if child_payload_references else {}),
                )
                sources = LineageParentSources(
                    run_inputs=run_inputs,
                    run_intermediates=run_intermediates,
                    prepared_siblings=prepared_sibling_sources,
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
                child_lineage = _inject_run_intermediates(
                    child_lineage=child_lineage,
                    lineage_policy=lineage_policy,
                    run_intermediates=run_intermediates,
                )
                child_lineage = tuple(dict.fromkeys((*child_lineage, *referenced_ids)))
                typed = project_typed_lineage(
                    policy=lineage_policy,
                    child_kind=view.kind,
                    child_payload_schema_id=view.payload_schema_id,
                    child_lineage=child_lineage,
                    sources=sources,
                    expected_parent_ids_by_role=expected_typed_run_parent_ids(
                        run=run,
                        policy=plan.policy,
                        rule=rule,
                        policy_parent_roles=frozenset(
                            parent.parent_role for parent in lineage_policy.parent_rules
                        ),
                    ),
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
                producer_env_contract_version = self._domain_producer_env(
                    run=run,
                    rule=rule,
                    payload_schema_id=view.payload_schema_id,
                    payload=payload,
                    typed_lineage=typed,
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
                self._validate_task_suite_payload_authority(
                    run=run,
                    payload_schema_id=view.payload_schema_id,
                    payload=payload,
                    prepared_batch_total_bytes=task_suite_prepared_total_bytes,
                    authority=task_suite_authority,
                )
                self._validate_playtest_profile_authority(
                    run=run,
                    payload_schema_id=view.payload_schema_id,
                    payload=payload,
                    producer_identity=producer_identity,
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
                authoritative_parent_payloads: dict[str, Mapping[str, object]] = {}
                if isinstance(
                    run.payload.params,
                    (
                        CheckerRunPayloadV1,
                        SimulationRunPayloadV1,
                        PlaytestRunPayloadV1,
                        ReviewRunPayloadV1,
                        PatchValidationPayloadV1,
                        ConstraintValidationPayloadV1,
                    ),
                ):
                    if isinstance(run.payload.params, PlaytestRunPayloadV1):
                        parent_roles = ("task_suite", "selected_scenarios")
                    elif isinstance(run.payload.params, SimulationRunPayloadV1):
                        parent_roles = ("constraint", "scenario")
                    elif isinstance(run.payload.params, ConstraintValidationPayloadV1):
                        parent_roles = ("proposal",)
                    else:
                        # Checker, Review checker/simulation companions, and Patch
                        # validation checker/simulation companions all prove their
                        # exact constraint application against the retained parent
                        # payload.  Supplying only typed parent metadata here leaves
                        # that proof unreachable at the real Terminal boundary.
                        parent_roles = ("constraint",)
                    for role in parent_roles:
                        for parent in typed.parents_by_role.get(role, ()):
                            parent_payload = authoritative_parent_payload_cache.get(
                                parent.artifact_id
                            )
                            if parent_payload is None:
                                parent_payload = self._read_authoritative_parent_payload(parent)
                                authoritative_parent_payload_cache[parent.artifact_id] = (
                                    parent_payload
                                )
                            authoritative_parent_payloads[parent.artifact_id] = parent_payload
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
                    authoritative_parent_payloads=authoritative_parent_payloads,
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
                sibling = ParentInfo(
                    artifact_id=stored.artifact_id,
                    kind=view.kind,
                    payload_schema_id=view.payload_schema_id,
                    version_tuple=expected_tuple,
                    payload_hash=stored.payload_hash,
                )
                retained_parent = authorized_run_parents.get(stored.artifact_id)
                retained_sibling = sibling_candidates.get(stored.artifact_id)
                if (
                    retained_parent is not None
                    and retained_parent != sibling
                    or retained_sibling is not None
                    and retained_sibling != sibling
                ):
                    raise IntegrityViolation(
                        "authorized lineage sources disagree about one parent",
                        artifact_id=stored.artifact_id,
                    )
                siblings.setdefault(rule.rule_id, {})[stored.artifact_id] = sibling
                sibling_candidates[stored.artifact_id] = sibling
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

    def _domain_producer_env(
        self,
        *,
        run: RunRecord,
        rule: OutcomeArtifactRuleV1,
        payload_schema_id: str,
        payload: Mapping[str, object],
        typed_lineage: TypedLineage,
    ) -> str | None:
        """Resolve child-local environment facts from retained authorities."""

        if (
            rule.artifact_kind == "regression_evidence"
            and payload_schema_id == "regression-evidence@1"
        ):
            suites = tuple(typed_lineage.parents_by_role.get("regression_suite", ()))
            if len(suites) > 1:
                raise IntegrityViolation(
                    "regression evidence resolves more than one suite environment"
                )
            if suites:
                env_contract_version = suites[0].version_tuple.env_contract_version
                if env_contract_version is None:
                    raise IntegrityViolation(
                        "regression suite lacks its environment contract version"
                    )
                return env_contract_version

            if isinstance(run.payload.params, PatchValidationPayloadV1):
                configs = tuple(typed_lineage.parents_by_role.get("candidate_config", ()))
                config_envs = {artifact.version_tuple.env_contract_version for artifact in configs}
                if None in config_envs:
                    raise IntegrityViolation(
                        "patch validation candidate config lacks its environment contract"
                    )
                if len(config_envs) > 1:
                    raise IntegrityViolation(
                        "patch validation candidate configs disagree on environment contract"
                    )
                if config_envs:
                    return next(iter(config_envs))

            # Rollback uses this rule both for target-only deterministic
            # dimensions and for optional per-suite children.  Suite authority
            # above takes precedence; otherwise the exact target is authoritative.
            targets = tuple(typed_lineage.parents_by_role.get("target", ()))
            if len(targets) > 1:
                raise IntegrityViolation(
                    "regression evidence resolves more than one target environment"
                )
            if targets:
                return targets[0].version_tuple.env_contract_version
            return None

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

    def _task_suite_terminal_authority(
        self,
        run: RunRecord,
    ) -> _TaskSuiteTerminalAuthority:
        params = run.payload.params
        if not isinstance(params, TaskSuiteDerivePayloadV1):
            raise IntegrityViolation("TaskSuite payload is bound to another Run kind")
        if run.resource_domain_scope is None:
            raise IntegrityViolation("TaskSuite Run lacks a resolved resource domain")
        validator = self._playtest_payload_validator
        if validator is None:
            raise IntegrityViolation("TaskSuite publication lacks payload-schema authority")
        catalog = self._registry.get_execution_profile_catalog(
            run.payload.execution_profile_catalog_version,
            run.payload.execution_profile_catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation("TaskSuite execution-profile catalog is not retained")

        def exact_definition(field_path: str, expected_kind: str):
            bindings = tuple(
                item
                for item in run.payload.resolved_profiles
                if item.field_path == field_path and item.expected_profile_kind == expected_kind
            )
            if len(bindings) != 1:
                raise IntegrityViolation("TaskSuite payload lacks one exact profile binding")
            binding = bindings[0]
            definitions = tuple(
                item for item in catalog.definitions if item.profile == binding.profile
            )
            if (
                len(definitions) != 1
                or binding.catalog_version != catalog.catalog_version
                or binding.catalog_digest != catalog.catalog_digest
                or definitions[0].profile_kind != expected_kind
                or run.kind not in definitions[0].compatible_run_kinds
                or execution_profile_payload_hash(definitions[0]) != binding.profile_payload_hash
            ):
                raise IntegrityViolation("TaskSuite exact profile binding is invalid")
            return definitions[0]

        environment_definition = exact_definition(
            "/params/environment_profile",
            "environment",
        )
        derivation_definition = exact_definition(
            "/params/derivation_profile",
            "task_suite_derivation",
        )
        if (
            derivation_definition.config_schema_id != "task_suite_derivation-profile-config@2"
            or not {"scenario-spec@1", "task-suite@1"}.issubset(
                derivation_definition.output_schema_ids
            )
            or not isinstance(environment_definition.details, EnvironmentProfileDetailsV1)
        ):
            raise IntegrityViolation("TaskSuite derivation profile lacks Task 12 authority")
        try:
            derivation_config = TaskSuiteDerivationProfileConfigV2.model_validate(
                derivation_definition.config
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("TaskSuite derivation profile config is invalid") from exc
        if (
            environment_definition.profile != params.environment_profile
            or derivation_definition.profile != params.derivation_profile
            or derivation_config.target_environment_profile != params.environment_profile
            or derivation_config.completion_oracle_registry_version
            != params.completion_oracle_registry_ref.registry_version
            or derivation_config.completion_oracle_registry_digest
            != params.completion_oracle_registry_ref.digest
        ):
            raise IntegrityViolation("TaskSuite payload differs from its exact profile closure")
        oracle_registry = validator.registry.get_completion_oracle_registry(
            params.completion_oracle_registry_ref
        )
        if oracle_registry is None:
            raise IntegrityViolation("TaskSuite completion-oracle registry is unavailable")
        return _TaskSuiteTerminalAuthority(
            run_id=run.run_id,
            run_payload_hash=run.payload_hash,
            environment_details=environment_definition.details,
            derivation_config=derivation_config,
            oracle_registry=oracle_registry,
            validator=validator,
        )

    def _validate_task_suite_payload_authority(
        self,
        *,
        run: RunRecord,
        payload_schema_id: str,
        payload: Mapping[str, object],
        prepared_batch_total_bytes: int | None,
        authority: _TaskSuiteTerminalAuthority | None = None,
    ) -> None:
        """Re-run exact reset/oracle schemas at the Prepared→Artifact boundary."""

        if payload_schema_id not in {"scenario-spec@1", "task-suite@1"}:
            return
        params = run.payload.params
        assert isinstance(params, TaskSuiteDerivePayloadV1)
        resolved = authority or self._task_suite_terminal_authority(run)
        if resolved.run_id != run.run_id or resolved.run_payload_hash != run.payload_hash:
            raise IntegrityViolation("TaskSuite authority cache belongs to another Run payload")
        environment_details = resolved.environment_details
        derivation_config = resolved.derivation_config
        oracle_registry = resolved.oracle_registry
        validator = resolved.validator
        if (
            prepared_batch_total_bytes is None
            or prepared_batch_total_bytes < 1
            or prepared_batch_total_bytes > derivation_config.max_total_prepared_artifact_bytes
        ):
            raise IntegrityViolation("TaskSuite payload differs from its exact profile closure")
        reset_schema_id = environment_details.contract.reset_schema_id

        if payload_schema_id == "scenario-spec@1":
            try:
                scenario = ScenarioSpecV1.model_validate(payload)
            except ValueError as exc:
                raise IntegrityViolation("ScenarioSpec payload is invalid") from exc
            if scenario.reset_binding.reset_schema_id != reset_schema_id:
                raise IntegrityViolation("ScenarioSpec reset schema differs from the environment")
            if scenario.domain_scope != run.resource_domain_scope:
                raise IntegrityViolation("ScenarioSpec domain differs from the Run authority")
            validator.validate_exact_contextual(
                schema_id=reset_schema_id,
                purpose="scenario_reset",
                payload=scenario.reset_binding.payload,
                context={
                    "expected_scenario_id": scenario.scenario_id,
                    "expected_config_export_artifact_id": scenario.config_export_artifact_id,
                },
            )
            return

        try:
            suite = TaskSuiteV1.model_validate(payload)
        except ValueError as exc:
            raise IntegrityViolation("TaskSuite payload is invalid") from exc
        if (
            suite.suite_profile != params.derivation_profile
            or suite.environment_profile != params.environment_profile
            or suite.completion_oracle_registry_ref != params.completion_oracle_registry_ref
            or len(suite.episodes) > derivation_config.max_scenarios
            or any(episode.domain_scope != run.resource_domain_scope for episode in suite.episodes)
        ):
            raise IntegrityViolation("TaskSuite payload differs from its exact Run closure")
        for episode in suite.episodes:
            if episode.reset_binding.reset_schema_id != reset_schema_id:
                raise IntegrityViolation("TaskSuite reset schema differs from the environment")
            validator.validate_exact(
                schema_id=reset_schema_id,
                purpose="scenario_reset",
                payload=episode.reset_binding.payload,
            )
            try:
                oracle_definition = resolve_completion_oracle(
                    oracle_registry,
                    params.completion_oracle_registry_ref,
                    episode.completion_oracle,
                )
            except ValueError as exc:
                raise IntegrityViolation("TaskSuite completion oracle is not exact") from exc
            validator.validate_exact(
                schema_id=oracle_definition.params_schema_id,
                purpose="completion_oracle_params",
                payload=episode.completion_oracle.params,
            )

    def _validate_playtest_profile_authority(
        self,
        *,
        run: RunRecord,
        payload_schema_id: str,
        payload: Mapping[str, object],
        producer_identity: ExecutionIdentityV1 | None,
    ) -> None:
        """Re-derive Playtest behavior/resources from the exact retained profile."""

        if payload_schema_id != "playtest-trace@1":
            return
        if not isinstance(run.payload.params, PlaytestRunPayloadV1):
            raise IntegrityViolation("playtest trace is bound to another Run payload")
        try:
            trace = PlaytestTraceV1.model_validate(payload)
        except ValueError as exc:
            raise IntegrityViolation("playtest trace payload is invalid") from exc
        bindings = tuple(
            binding
            for binding in run.payload.resolved_profiles
            if binding.field_path == "/params/planner_policy"
            and binding.profile == run.payload.params.planner_policy
            and binding.expected_profile_kind == "playtest_planner"
        )
        if len(bindings) != 1:
            raise IntegrityViolation("playtest trace lacks one exact planner profile binding")
        binding = bindings[0]
        if (
            binding.catalog_version != run.payload.execution_profile_catalog_version
            or binding.catalog_digest != run.payload.execution_profile_catalog_digest
        ):
            raise IntegrityViolation("playtest planner binding uses another catalog")
        catalog = self._registry.get_execution_profile_catalog(
            binding.catalog_version,
            binding.catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation("playtest execution-profile catalog is not retained")
        definitions = tuple(
            definition
            for definition in catalog.definitions
            if definition.profile == binding.profile
        )
        if len(definitions) != 1:
            raise IntegrityViolation("playtest planner profile is not unique in its catalog")
        definition = definitions[0]
        if (
            definition.profile_kind != "playtest_planner"
            or definition.handler_key != "builtin_playtest_planner_profile@2"
            or definition.config_schema_id != "playtest_planner-profile-config@2"
            or run.kind not in definition.compatible_run_kinds
            or payload_schema_id not in definition.output_schema_ids
            or execution_profile_payload_hash(definition) != binding.profile_payload_hash
        ):
            raise IntegrityViolation("playtest planner differs from its frozen Run binding")
        if (
            trace.planner_policy != run.payload.params.planner_policy
            or trace.environment_profile != run.payload.params.environment_profile
            or trace.execution_envelope.planner_profile_payload_hash != binding.profile_payload_hash
        ):
            raise IntegrityViolation("playtest trace profile refs differ from the Run payload")
        trace_episode_bindings = tuple(
            (episode.episode_id, episode.scenario_spec_artifact_id) for episode in trace.episodes
        )
        run_episode_bindings = tuple(
            (episode.episode_id, episode.scenario_spec_artifact_id)
            for episode in run.payload.params.episodes
        )
        if (
            trace.config_artifact_id != run.payload.params.config_artifact_id
            or trace.constraint_snapshot_artifact_id
            != run.payload.params.constraint_snapshot_artifact_id
            or trace.task_suite_artifact_id != run.payload.params.task_suite_artifact_id
            or trace.interaction_mode != run.payload.params.interaction_mode
            or trace.requested_max_steps_per_episode != run.payload.params.max_steps_per_episode
            or trace.seed != run.payload.seed
            or trace.env_contract_version != run.payload.version_tuple.env_contract_version
            or trace_episode_bindings != run_episode_bindings
        ):
            raise IntegrityViolation("playtest trace differs from its frozen Run inputs")
        environment_bindings = tuple(
            item
            for item in run.payload.resolved_profiles
            if item.field_path == "/params/environment_profile"
            and item.profile == run.payload.params.environment_profile
            and item.expected_profile_kind == "environment"
        )
        if len(run.payload.resolved_profiles) != 2 or len(environment_bindings) != 1:
            raise IntegrityViolation("playtest trace lacks one exact environment profile binding")
        environment_binding = environment_bindings[0]
        if (
            environment_binding.catalog_version != binding.catalog_version
            or environment_binding.catalog_digest != binding.catalog_digest
        ):
            raise IntegrityViolation("playtest environment binding uses another catalog")
        environment_definitions = tuple(
            candidate
            for candidate in catalog.definitions
            if candidate.profile == environment_binding.profile
        )
        if len(environment_definitions) != 1:
            raise IntegrityViolation("playtest environment profile is not unique in its catalog")
        environment_definition = environment_definitions[0]
        if (
            environment_definition.profile_kind != "environment"
            or environment_definition.handler_key != "builtin_environment_profile@1"
            or environment_definition.config_schema_id != "environment-profile-config@1"
            or run.kind not in environment_definition.compatible_run_kinds
            or payload_schema_id not in environment_definition.output_schema_ids
            or execution_profile_payload_hash(environment_definition)
            != environment_binding.profile_payload_hash
            or not isinstance(environment_definition.details, EnvironmentProfileDetailsV1)
        ):
            raise IntegrityViolation("playtest environment differs from its frozen Run binding")
        environment_contract = environment_definition.details.contract
        action_validator = self._playtest_payload_validator
        if action_validator is None:
            raise IntegrityViolation("playtest publication lacks action-schema authority")
        if trace.env_contract_version != environment_contract.env_contract_version:
            raise IntegrityViolation("playtest trace environment contract is stale")
        allowed_kinds = allowed_action_kinds(trace.interaction_mode)
        for episode in trace.episodes:
            for record in episode.action_trace:
                validated_action = action_validator.validate_exact(
                    schema_id=environment_contract.action_schema_id,
                    purpose="environment_action",
                    payload=record.action,
                )
                if (
                    not isinstance(validated_action, dict)
                    or validated_action.get("kind") not in allowed_kinds
                ):
                    raise IntegrityViolation(
                        "playtest trace action differs from its environment authority"
                    )
        try:
            config = PlaytestPlannerProfileConfigV2.model_validate(definition.config)
            bounds = playtest_resource_upper_bounds(
                config,
                episode_count=len(trace.episodes),
                max_steps_per_episode=trace.requested_max_steps_per_episode,
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("playtest trace exceeds its exact planner profile") from exc
        envelope = trace.execution_envelope
        if producer_identity is None or producer_identity.scope != "artifact":
            raise IntegrityViolation("playtest trace lacks its exact execution identity")
        consumed_call_count = len(
            {
                (binding.attempt_no, binding.call_ordinal)
                for binding in producer_identity.bindings
                if binding.response_consumed
            }
        )
        if (
            trace.planner_memory_mode != config.memory_mode
            or bounds
            != (
                envelope.total_step_limit,
                envelope.model_call_upper_bound,
                envelope.total_trace_byte_upper_bound,
            )
            or envelope.actual_model_calls > config.max_total_model_calls
            or envelope.total_action_trace_bytes > config.max_total_trace_bytes
            or envelope.actual_model_calls != consumed_call_count
        ):
            raise IntegrityViolation("playtest trace differs from its exact planner profile")

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
        prepared_by_index: dict[int, list[object]] = {}
        for item in prepared.findings:
            prepared_by_index.setdefault(item.evidence_artifact_index, []).append(
                json.loads(canonical_json(item.payload.model_dump(mode="json")))
            )
        embedded_fields = (
            "findings",
            "deterministic_findings",
            "llm_assisted_findings",
            "simulation_findings",
            "unproven_findings",
        )
        finding_bearing_schemas = frozenset(
            {
                "checker-report@1",
                "simulation-result@1",
                "review@1",
                "regression-evidence@1",
            }
        )
        for index, payload in published.payloads_by_index.items():
            payload_schema_id = prepared.artifacts[index].payload_schema_id
            if plan.finding_policy is None or payload_schema_id not in finding_bearing_schemas:
                continue
            actual: list[object] = []
            containers = [payload]
            detail = payload.get("detail")
            if isinstance(detail, Mapping):
                containers.append(detail)
            has_embedded = False
            for container in containers:
                for field_name in embedded_fields:
                    raw = container.get(field_name)
                    if raw is None:
                        continue
                    has_embedded = True
                    if not isinstance(raw, (list, tuple)):
                        raise IntegrityViolation(
                            "published embedded Finding collection is invalid",
                            field=field_name,
                        )
                    for value in raw:
                        try:
                            finding = Finding.model_validate(value)
                        except (TypeError, ValueError) as exc:
                            raise IntegrityViolation(
                                "published embedded Finding is invalid",
                                field=field_name,
                            ) from exc
                        if finding.producer_run_id != run.run_id:
                            raise IntegrityViolation(
                                "embedded Finding producer differs from current Run"
                            )
                        if (
                            run.kind.kind == "patch.validate"
                            and payload.get("dimension") == "checker"
                            and finding.source == "llm"
                            and finding.oracle_type == "llm-assisted"
                            and finding.status == "unproven"
                            and finding.producer_id == "llm-routed"
                            and finding.defect_class == "llm_assisted_predicate"
                            and finding.constraint_id is not None
                        ):
                            # This is the exact fail-closed placeholder for a DSL
                            # predicate outside deterministic checker authority. It
                            # remains embedded evidence (and keeps the dimension
                            # unproven), but the validation Finding policy correctly
                            # forbids publishing LLM judgment as a Finding row. The
                            # payload binder already re-proved the constraint id and
                            # LLM predicate against the retained parent DSL.
                            continue
                        actual.append(
                            json.loads(
                                canonical_json(
                                    finding_to_payload(
                                        finding,
                                        producer_run_id=run.run_id,
                                    ).model_dump(mode="json")
                                )
                            )
                        )
            expected = (
                [
                    json.loads(canonical_json(item.payload.model_dump(mode="json")))
                    for item in prepared.findings
                ]
                if payload_schema_id == "review@1"
                else prepared_by_index.get(index, [])
            )
            if not has_embedded:
                if expected:
                    raise IntegrityViolation(
                        "PreparedFinding has no exact embedded Finding closure",
                        evidence_artifact_index=index,
                    )
                continue
            if sorted(map(typed_canonical_json, actual)) != sorted(
                map(typed_canonical_json, expected)
            ):
                raise IntegrityViolation(
                    "embedded Findings differ from PreparedFinding closure",
                    evidence_artifact_index=index,
                    embedded=tuple(sorted(map(typed_canonical_json, actual))),
                    prepared=tuple(sorted(map(typed_canonical_json, expected))),
                )
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
        if len(parents_by_id) > MAX_RUN_MANIFEST_PARENT_BINDINGS:
            raise IntegrityViolation("manifest parent projection exceeds its hard cap")
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
        outcome_code: str,
        require_complete: bool,
    ) -> _TerminalRuntimeProjection:
        """Close runtime links, identity, cassette tree and transition inputs."""

        mode = run.payload.llm_execution_mode
        all_links = self._ledger.prompt_links(run.run_id, attempt_no=None)
        current_links = (
            tuple(link for link in all_links if link.attempt_no == current_attempt_no)
            if current_attempt_no is not None
            else ()
        )
        all_tool_links = self._ledger.tool_intermediate_links(run.run_id, attempt_no=None)
        current_tool_links = (
            tuple(link for link in all_tool_links if link.attempt_no == current_attempt_no)
            if current_attempt_no is not None
            else ()
        )
        committed = {
            ("prompt_rendered", "current_attempt"): len(current_links),
            ("prompt_rendered", "all_attempts"): len(all_links),
            ("agent_prompt_context", "current_attempt"): len(current_tool_links),
            ("agent_prompt_context", "all_attempts"): len(all_tool_links),
        }
        prompt_links = current_links if manifest_scope == "attempt" else all_links
        tool_links = current_tool_links if manifest_scope == "attempt" else all_tool_links
        for link in tool_links:
            self._validate_tool_context_parent(run=run, link=link)

        identity: ExecutionIdentityV1 | None = None
        route_links: tuple[RunModelRouteLinkV1, ...] = ()
        response_consumptions: tuple[RunModelResponseConsumptionV1, ...] = ()
        if mode != "not_applicable":
            selected_attempt = current_attempt_no if manifest_scope == "attempt" else None
            route_links = self._ledger.model_route_links(
                run.run_id,
                attempt_no=selected_attempt,
            )
            response_consumptions = self._ledger.model_response_consumptions(
                run.run_id,
                attempt_no=selected_attempt,
            )
            try:
                identity = ExecutionIdentityV1.model_validate(
                    self._ledger.execution_identity(
                        run.run_id,
                        attempt_no=selected_attempt,
                    )
                )
            except ValueError as exc:
                raise IntegrityViolation(
                    "terminal execution identity is not canonical",
                    run_id=run.run_id,
                    manifest_scope=manifest_scope,
                ) from exc
            prompt_info = {
                link.artifact_id: self._runtime_parent_info(link.artifact_id)
                for link in prompt_links
            }
            self._validate_execution_identity(
                run=run,
                identity=identity,
                manifest_scope=manifest_scope,
                current_attempt_no=current_attempt_no,
                prompt_links=prompt_links,
                artifact_info=prompt_info,
                route_links=route_links,
                response_consumptions=response_consumptions,
                require_complete=require_complete,
            )

        record_shards: tuple[tuple[int, int, str], ...] = ()
        attempt_bundle_id: str | None = None
        authoritative_attempt_bundle_ids: dict[int, str] = {}
        run_bundle_id: str | None = None
        replay_input_id: str | None = None
        if mode == "record":
            if identity is None:  # pragma: no cover - RECORD loads one above
                raise IntegrityViolation("RECORD terminal publication has no execution identity")
            record_shards = self._ledger.record_shard_links(
                run.run_id,
                attempt_no=(current_attempt_no if manifest_scope == "attempt" else None),
            )
            attempt_numbers = (
                ()
                if current_attempt_no is None
                else (
                    (current_attempt_no,)
                    if manifest_scope == "attempt"
                    else tuple(range(1, current_attempt_no + 1))
                )
            )
            for attempt_no in attempt_numbers:
                authoritative_id = self._ledger.attempt_cassette_bundle(
                    run.run_id,
                    attempt_no=attempt_no,
                )
                if authoritative_id is None:
                    retained_attempt = self._attempt_authority(run.run_id, attempt_no)
                    may_be_planned_now = (
                        attempt_no == current_attempt_no
                        and isinstance(retained_attempt, RunAttempt)
                        and retained_attempt.status in {"leased", "running"}
                    )
                    if not may_be_planned_now:
                        raise IntegrityViolation(
                            "closed RECORD attempt has no authoritative cassette bundle",
                            run_id=run.run_id,
                            attempt_no=attempt_no,
                        )
                    attempt_identity = (
                        identity
                        if manifest_scope == "attempt"
                        else self._identity_subset(
                            identity,
                            scope="attempt",
                            attempt_no=attempt_no,
                        )
                    )
                    child_ids = tuple(
                        artifact_id
                        for shard_attempt_no, _, artifact_id in sorted(record_shards)
                        if shard_attempt_no == attempt_no
                    )
                    authoritative_id = self._plan_record_cassette_bundle(
                        payload=CassetteBundleV1(
                            scope="attempt",
                            run_id=run.run_id,
                            attempt_no=attempt_no,
                            outcome_code=outcome_code,
                            child_bundle_artifact_ids=child_ids,
                        ),
                        identity=attempt_identity,
                    ).artifact_id
                authoritative_attempt_bundle_ids[attempt_no] = authoritative_id
            if current_attempt_no is not None:
                attempt_bundle_id = authoritative_attempt_bundle_ids[current_attempt_no]
            if manifest_scope == "run":
                run_bundle_id = self._ledger.run_cassette_bundle(run.run_id)
                if run_bundle_id is None:
                    run_bundle_id = self._plan_record_cassette_bundle(
                        payload=CassetteBundleV1(
                            scope="run",
                            run_id=run.run_id,
                            outcome_code=outcome_code,
                            child_bundle_artifact_ids=tuple(
                                authoritative_attempt_bundle_ids[attempt_no]
                                for attempt_no in attempt_numbers
                            ),
                        ),
                        identity=identity,
                    ).artifact_id
        elif mode == "replay":
            replay_input_id = self._ledger.replay_input_cassette(run.run_id)
            if replay_input_id != run.payload.cassette_artifact_id:
                raise IntegrityViolation(
                    "REPLAY ledger cassette differs from the frozen Run payload",
                    run_id=run.run_id,
                )

        runtime_ids = {
            *(link.artifact_id for link in prompt_links),
            *(link.artifact_id for link in tool_links),
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
            tool_intermediate_links=tool_links,
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
                outcome_code=outcome_code,
                require_complete=require_complete,
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
                run=run,
                terminal_identity=identity,
                manifest_scope=manifest_scope,
                current_attempt_no=current_attempt_no,
                prompt_links=prompt_links,
                require_complete=require_complete,
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

    def _plan_record_cassette_bundle(
        self,
        *,
        payload: CassetteBundleV1,
        identity: ExecutionIdentityV1,
    ) -> ArtifactV2:
        """Plan one immutable aggregate inside the terminal publication draft.

        Record shards are already published by response consumption. Attempt and
        Run aggregates, however, are terminal authority and must be staged with the
        matching manifest then inserted by the same terminal UoW.  An exact overlay
        lets an active-failure run draft reuse the attempt draft's not-yet-committed
        aggregate without precommitting it or staging a duplicate operation.
        """

        if self._collector is None:
            raise IntegrityViolation("cassette aggregate planning requires an active draft")
        encoded = canonical_json(payload.model_dump(mode="json")).encode("utf-8")
        object_ref = object_ref_for_bytes(encoded)
        artifact = build_artifact_v2(
            kind="cassette_bundle",
            version_tuple=VersionTuple(
                prompt_version=identity.prompt_projection.tuple_value,
                model_snapshot=identity.model_projection.tuple_value,
                agent_graph_version=identity.agent_graph_version,
                tool_version=_CASSETTE_TOOL_VERSION,
                cassette_id=f"sha256:{object_ref.sha256}",
            ),
            lineage=payload.child_bundle_artifact_ids,
            payload_hash=object_ref.sha256,
            object_ref=object_ref,
            meta={
                "payload_schema_id": "cassette-bundle@1",
                "execution_identity": identity,
                "replayability": "cassette_replay",
            },
            created_at=self._collector.occurred_at,
        )
        retained = self._planned_artifact_overlay.get(artifact.artifact_id)
        if retained is not None:
            if not _same_immutable_artifact(retained[0], artifact) or retained[1] != encoded:
                raise IntegrityViolation(
                    "planned cassette aggregate differs from retained overlay",
                    artifact_id=artifact.artifact_id,
                )
            return retained[0]

        staged_ref = self._collector.add_blob(encoded)
        if staged_ref != object_ref:  # pragma: no cover - shared canonical helper
            raise IntegrityViolation("cassette aggregate ObjectRef derivation is inconsistent")
        stored = self._collector.add_artifact(artifact)
        self._planned_artifact_overlay[stored.artifact_id] = (stored, encoded)
        return stored

    def _validate_execution_identity(
        self,
        *,
        run: RunRecord,
        identity: ExecutionIdentityV1,
        manifest_scope: str,
        current_attempt_no: int | None,
        prompt_links: Sequence[RunIntermediateArtifactLinkV1],
        artifact_info: Mapping[str, ParentInfo],
        route_links: Sequence[RunModelRouteLinkV1],
        response_consumptions: Sequence[RunModelResponseConsumptionV1],
        require_complete: bool,
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

        links_by_route = {
            (link.attempt_no, link.call_ordinal, link.route_ordinal): link for link in prompt_links
        }
        if len(links_by_route) != len(prompt_links):
            raise IntegrityViolation("rendered-prompt links contain a duplicate call route")
        route_keys = tuple(
            (route.attempt_no, route.call_ordinal, route.route_ordinal) for route in route_links
        )
        if route_keys != tuple(sorted(route_keys)) or len(route_keys) != len(set(route_keys)):
            raise IntegrityViolation("model-route authority is not canonical and unique")
        routes_by_key = dict(zip(route_keys, route_links, strict=True))
        consumption_keys = tuple(
            (item.attempt_no, item.call_ordinal, item.route_ordinal)
            for item in response_consumptions
        )
        if consumption_keys != tuple(sorted(consumption_keys)) or len(consumption_keys) != len(
            set(consumption_keys)
        ):
            raise IntegrityViolation("response-consumption authority is not canonical and unique")
        consumptions_by_key = dict(zip(consumption_keys, response_consumptions, strict=True))
        identity_keys = tuple(
            (binding.attempt_no, binding.call_ordinal, binding.route_ordinal)
            for binding in identity.bindings
        )
        if identity_keys != route_keys:
            raise IntegrityViolation(
                "terminal execution identity does not enumerate exact model-route authority"
            )
        consumed_identity_keys = tuple(
            (binding.attempt_no, binding.call_ordinal, binding.route_ordinal)
            for binding in identity.bindings
            if binding.response_consumed
        )
        if consumed_identity_keys != consumption_keys:
            raise IntegrityViolation(
                "terminal execution identity does not enumerate exact response-consumption authority"
            )
        route_key_set = set(route_keys)
        prompt_key_set = set(links_by_route)
        if not route_key_set.issubset(prompt_key_set):
            raise IntegrityViolation(
                "terminal prompts differ from exact model-route authority",
                require_complete=require_complete,
            )
        if require_complete:
            if current_attempt_no is None:
                raise IntegrityViolation("successful terminal identity has no current attempt")
            current_route_keys = {key for key in route_key_set if key[0] == current_attempt_no}
            current_prompt_keys = {key for key in prompt_key_set if key[0] == current_attempt_no}
            if current_prompt_keys != current_route_keys:
                raise IntegrityViolation(
                    "successful current-attempt prompts differ from exact route authority"
                )
            logical_calls = {
                binding.call_ordinal
                for binding in identity.bindings
                if binding.attempt_no == current_attempt_no
            }
            consumed_calls = {
                binding.call_ordinal
                for binding in identity.bindings
                if binding.attempt_no == current_attempt_no and binding.response_consumed
            }
            if logical_calls != consumed_calls:
                raise IntegrityViolation(
                    "successful current attempt has an unconsumed logical call"
                )
        nodes = {node.agent_node_id: node for node in plan.nodes}
        expected_sources = (
            {"cassette_replay"}
            if run.payload.llm_execution_mode == "replay"
            else {"online", "full_response_cache"}
        )
        attempts_by_no: dict[int, RunAttempt | None] = {}
        prompt_artifacts: dict[str, ArtifactV2] = {}
        routing_decisions: dict[str, RoutingDecisionV1 | None] = {}
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
            link = links_by_route.get(
                (binding.attempt_no, binding.call_ordinal, binding.route_ordinal)
            )
            if link is None:
                raise IntegrityViolation(
                    "execution identity has no committed rendered-prompt link",
                    attempt_no=binding.attempt_no,
                    call_ordinal=binding.call_ordinal,
                )
            if binding.attempt_no not in attempts_by_no:
                attempts_by_no[binding.attempt_no] = self._attempt_authority(
                    run.run_id,
                    binding.attempt_no,
                )
            retained_attempt = attempts_by_no[binding.attempt_no]
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
            key = (binding.attempt_no, binding.call_ordinal, binding.route_ordinal)
            route = routes_by_key[key]
            consumption = consumptions_by_key.get(key)
            if (
                not isinstance(route, RunModelRouteLinkV1)
                or route.run_id != run.run_id
                or route.prompt_artifact_id != link.artifact_id
                or route.request_hash != link.request_hash
                or route.routing_decision_kind != binding.routing_decision_kind
                or route.routing_decision_id != binding.routing_decision_id
                or route.fencing_token != link.fencing_token
            ):
                raise IntegrityViolation(
                    "execution identity differs from exact model-route authority"
                )
            if binding.response_consumed:
                expected_shard = run.payload.llm_execution_mode == "record"
                if (
                    not isinstance(consumption, RunModelResponseConsumptionV1)
                    or consumption.run_id != run.run_id
                    or consumption.execution_source != binding.execution_source
                    or consumption.transport_attempt != binding.transport_attempt
                    or (consumption.cassette_shard_artifact_id is not None) != expected_shard
                ):
                    raise IntegrityViolation(
                        "execution identity differs from response-consumption authority"
                    )
            elif consumption is not None:
                raise IntegrityViolation(
                    "unconsumed execution route has response-consumption authority"
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
            prompt_artifact = prompt_artifacts.get(link.artifact_id)
            if prompt_artifact is None:
                prompt_artifact = self._artifact_v2(link.artifact_id)
                prompt_artifacts[link.artifact_id] = prompt_artifact
            renderer_version = prompt_artifact.meta.get("renderer_version")
            if (
                prompt_info.version_tuple.prompt_version != binding.prompt_version
                or prompt_info.version_tuple.model_snapshot is not None
                or prompt_info.version_tuple.agent_graph_version != identity.agent_graph_version
                or not isinstance(renderer_version, str)
                or prompt_info.version_tuple.tool_version != renderer_version
            ):
                raise IntegrityViolation(
                    "execution identity differs from its rendered-prompt Artifact",
                    artifact_id=link.artifact_id,
                )
            if binding.routing_decision_kind == "native":
                if binding.routing_decision_id not in routing_decisions:
                    routing_decisions[binding.routing_decision_id] = (
                        self._ledger.get_routing_decision(binding.routing_decision_id)
                    )
                decision = routing_decisions[binding.routing_decision_id]
                if (
                    type(decision) is not RoutingDecisionV1
                    or decision.decision_id != binding.routing_decision_id
                    or decision.run_id != run.run_id
                    or decision.attempt_no != binding.attempt_no
                    or decision.request_hash != f"sha256:{link.request_hash}"
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
        outcome_code: str,
        require_complete: bool,
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
        if aggregate.payload.outcome_code != outcome_code:
            raise IntegrityViolation(
                "RECORD aggregate outcome differs from terminal policy",
                artifact_id=aggregate.artifact.artifact_id,
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
            if require_complete:
                self._require_complete_record_identity(
                    execution_identity,
                    attempt_no=current_attempt_no,
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
            retained_attempt = self._attempt_authority(run.run_id, attempt_no)
            if (
                attempt_no == current_attempt_no
                and isinstance(retained_attempt, RunAttempt)
                and retained_attempt.status in {"leased", "running"}
                and attempt.payload.outcome_code != outcome_code
            ):
                raise IntegrityViolation(
                    "current attempt cassette outcome differs from terminal policy",
                    attempt_no=attempt_no,
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
        if require_complete:
            if current_attempt_no is None:
                raise IntegrityViolation("successful RECORD Run has no current attempt")
            self._require_complete_record_identity(
                execution_identity,
                attempt_no=current_attempt_no,
            )

    @staticmethod
    def _require_complete_record_identity(
        identity: ExecutionIdentityV1,
        *,
        attempt_no: int,
    ) -> None:
        logical_calls = {
            binding.call_ordinal
            for binding in identity.bindings
            if binding.attempt_no == attempt_no
        }
        consumed_calls = {
            binding.call_ordinal
            for binding in identity.bindings
            if binding.attempt_no == attempt_no and binding.response_consumed
        }
        if logical_calls != consumed_calls:
            raise IntegrityViolation(
                "successful RECORD attempt contains an incomplete call",
                attempt_no=attempt_no,
            )

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

        consumed = tuple(
            binding for binding in expected_identity.bindings if binding.response_consumed
        )
        if len(consumed) != 1:
            raise IntegrityViolation("native RECORD record does not bind one consumed route")
        binding = consumed[0]

        links = tuple(
            link
            for link in prompt_links
            if link.attempt_no == attempt_no
            and link.call_ordinal == ordinal
            and link.route_ordinal == binding.route_ordinal
        )
        if len(links) != 1:
            raise IntegrityViolation("native RECORD shard lacks one exact rendered-prompt link")
        link = links[0]
        if link.run_id != run.run_id:
            raise IntegrityViolation("native RECORD prompt link belongs to another Run")
        retained_attempt = self._attempt_authority(run.run_id, attempt_no)
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
        expected_prompt_lineage = (link.artifact_id,)
        if shard.artifact.lineage != expected_prompt_lineage:
            raise IntegrityViolation(
                "native RECORD shard lineage differs from its prompt route closure"
            )
        consumption = self._ledger.get_model_response_consumption(
            run.run_id,
            attempt_no,
            ordinal,
            binding.route_ordinal,
        )
        if (
            not isinstance(consumption, RunModelResponseConsumptionV1)
            or consumption.cassette_shard_artifact_id != shard.artifact.artifact_id
        ):
            raise IntegrityViolation(
                "native RECORD shard differs from response-consumption authority"
            )

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
        renderer_version = prompt.meta.get("renderer_version")
        if (
            rendered_hash != record.request_hash
            or rendered_hash.removeprefix("sha256:") != link.request_hash
            or rendered_request.agent_node_id != record.agent_node_id
            or rendered_request.prompt_version != node.prompt_version
            or rendered_model != decision.model_snapshot
            or prompt.version_tuple.prompt_version != rendered_request.prompt_version
            or prompt.version_tuple.model_snapshot is not None
            or prompt.version_tuple.agent_graph_version != plan.agent_graph_version
            or not isinstance(renderer_version, str)
            or prompt.version_tuple.tool_version != renderer_version
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
        run: RunRecord,
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
        prompt_hash_by_route = {
            (link.attempt_no, link.call_ordinal, link.route_ordinal): link.request_hash
            for link in prompt_links
        }
        if len(prompt_hash_by_route) != len(prompt_links):
            raise IntegrityViolation("REPLAY rendered-prompt links contain a duplicate route")
        previous_attempt = 0
        observed_attempts: list[int] = []
        observed_shard_keys: list[tuple[int, int]] = []
        source_request_hashes: dict[tuple[int, int, int], str] = {}
        source_native_decisions: dict[tuple[int, int, int], RoutingDecisionV1] = {}
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
                    source_native_decisions[(attempt_no, ordinal, binding.route_ordinal)] = (
                        record.routing_decision
                    )
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
                source_request_hashes[(attempt_no, ordinal, binding.route_ordinal)] = (
                    expected_request_hash
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

        selected_source_attempt_no = observed_attempts[-1] if observed_attempts else None
        selected_bindings = tuple(
            binding
            for binding in root_identity.bindings
            if binding.attempt_no == selected_source_attempt_no
        )
        self._require_consumed_route_is_terminal(selected_bindings)
        current_attempts = {binding.attempt_no for binding in terminal_identity.bindings} | {
            link.attempt_no for link in prompt_links
        }
        if current_attempt_no is not None:
            current_attempts.add(current_attempt_no)
        for attempt_no in sorted(current_attempts):
            current_bindings = tuple(
                binding
                for binding in terminal_identity.bindings
                if binding.attempt_no == attempt_no
            )
            current_prompts = tuple(
                sorted(
                    (link for link in prompt_links if link.attempt_no == attempt_no),
                    key=lambda link: (link.call_ordinal, link.route_ordinal),
                )
            )
            complete_attempt = require_complete and attempt_no == current_attempt_no
            self._validate_replay_attempt_prefix(
                run=run,
                root=root,
                selected_source_attempt_no=selected_source_attempt_no,
                selected_bindings=selected_bindings,
                current_attempt_no=attempt_no,
                current_bindings=current_bindings,
                current_prompts=current_prompts,
                source_request_hashes=source_request_hashes,
                source_native_decisions=source_native_decisions,
                require_complete=complete_attempt,
            )

    @staticmethod
    def _require_consumed_route_is_terminal(
        bindings: Sequence[InvocationVersionBindingV1],
    ) -> None:
        calls: dict[int, list[InvocationVersionBindingV1]] = {}
        for binding in bindings:
            calls.setdefault(binding.call_ordinal, []).append(binding)
        for routes in calls.values():
            consumed = tuple(route for route in routes if route.response_consumed)
            if consumed and consumed[0] != routes[-1]:
                raise IntegrityViolation(
                    "REPLAY source call has a route after its consumed response"
                )

    def _validate_replay_attempt_prefix(
        self,
        *,
        run: RunRecord,
        root: _CassetteNode,
        selected_source_attempt_no: int | None,
        selected_bindings: tuple[InvocationVersionBindingV1, ...],
        current_attempt_no: int,
        current_bindings: tuple[InvocationVersionBindingV1, ...],
        current_prompts: tuple[RunIntermediateArtifactLinkV1, ...],
        source_request_hashes: dict[tuple[int, int, int], str],
        source_native_decisions: dict[tuple[int, int, int], RoutingDecisionV1],
        require_complete: bool,
    ) -> None:
        if selected_source_attempt_no is None:
            if current_bindings or current_prompts:
                raise IntegrityViolation("zero-attempt REPLAY source cannot satisfy a model call")
            return
        if len(current_bindings) > len(selected_bindings) or (
            require_complete and len(current_bindings) != len(selected_bindings)
        ):
            raise IntegrityViolation(
                "REPLAY current attempt route count differs from selected source identity"
            )
        semantic_fields = (
            "call_ordinal",
            "route_ordinal",
            "routing_decision_kind",
            "agent_node_id",
            "prompt_version",
            "model_snapshot",
            "tool_version",
        )
        for source, current in zip(selected_bindings, current_bindings, strict=False):
            if any(getattr(source, field) != getattr(current, field) for field in semantic_fields):
                raise IntegrityViolation(
                    "REPLAY current attempt is not a stable prefix of selected source identity"
                )
            if current.response_consumed and not source.response_consumed:
                raise IntegrityViolation(
                    "REPLAY current attempt consumed an unavailable source response"
                )
            if require_complete and current.response_consumed != source.response_consumed:
                raise IntegrityViolation(
                    "successful REPLAY attempt did not fully consume selected source identity"
                )
            if source.routing_decision_kind == "legacy_import":
                if source.routing_decision_id != current.routing_decision_id:
                    raise IntegrityViolation(
                        "legacy REPLAY current attempt uses another imported decision"
                    )
                continue
            source_key = (
                selected_source_attempt_no,
                source.call_ordinal,
                source.route_ordinal,
            )
            source_decision = source_native_decisions.get(source_key)
            if source_decision is None:
                source_decision = self._ledger.get_routing_decision(source.routing_decision_id)
            current_decision = self._ledger.get_routing_decision(current.routing_decision_id)
            self._require_native_replay_decision_projection(
                run=run,
                root=root,
                selected_source_attempt_no=selected_source_attempt_no,
                current_attempt_no=current_attempt_no,
                source_binding=source,
                current_binding=current,
                source_decision=source_decision,
                current_decision=current_decision,
            )
            source_request_hashes.setdefault(
                source_key,
                source_decision.request_hash.removeprefix("sha256:"),
            )

        selected_prompt_keys = tuple(
            (binding.call_ordinal, binding.route_ordinal) for binding in selected_bindings
        )
        current_prompt_keys = tuple(
            (link.call_ordinal, link.route_ordinal) for link in current_prompts
        )
        if current_prompt_keys != selected_prompt_keys[: len(current_prompt_keys)] or (
            require_complete and current_prompt_keys != selected_prompt_keys
        ):
            raise IntegrityViolation(
                "REPLAY current attempt prompts are not a stable selected-source prefix"
            )
        for link in current_prompts:
            source_key = (
                selected_source_attempt_no,
                link.call_ordinal,
                link.route_ordinal,
            )
            expected_hash = source_request_hashes.get(source_key)
            if expected_hash is None:
                source_binding = next(
                    (
                        binding
                        for binding in selected_bindings
                        if binding.call_ordinal == link.call_ordinal
                        and binding.route_ordinal == link.route_ordinal
                    ),
                    None,
                )
                if source_binding is not None and source_binding.routing_decision_kind == "native":
                    decision = self._ledger.get_routing_decision(source_binding.routing_decision_id)
                    if isinstance(decision, RoutingDecisionV1):
                        expected_hash = decision.request_hash.removeprefix("sha256:")
                        source_request_hashes[source_key] = expected_hash
            if link.request_hash != expected_hash:
                raise IntegrityViolation(
                    "REPLAY current rendered prompt differs from selected source request"
                )

    @staticmethod
    def _require_native_replay_decision_projection(
        *,
        run: RunRecord,
        root: _CassetteNode,
        selected_source_attempt_no: int,
        current_attempt_no: int,
        source_binding: InvocationVersionBindingV1,
        current_binding: InvocationVersionBindingV1,
        source_decision: RoutingDecisionV1 | None,
        current_decision: RoutingDecisionV1 | None,
    ) -> None:
        semantic_fields = (
            "request_hash",
            "rule_id",
            "model_snapshot",
            "tier",
            "fallback_from",
            "fallback_index",
            "policy_version",
            "routing_policy_digest",
            "catalog_version",
            "catalog_digest",
        )
        if (
            not isinstance(source_decision, RoutingDecisionV1)
            or not isinstance(current_decision, RoutingDecisionV1)
            or source_decision.decision_id != source_binding.routing_decision_id
            or source_decision.run_id != root.payload.run_id
            or source_decision.attempt_no != selected_source_attempt_no
            or current_decision.decision_id != current_binding.routing_decision_id
            or current_decision.run_id != run.run_id
            or current_decision.attempt_no != current_attempt_no
            or current_decision.execution_source != "cassette_replay"
            or current_decision.reason_code != "recorded_replay"
            or current_decision.budget_set_snapshot_id != run.payload.budget_set_snapshot_id
            or any(
                getattr(source_decision, field) != getattr(current_decision, field)
                for field in semantic_fields
            )
        ):
            raise IntegrityViolation(
                "native REPLAY current decision differs from selected source route"
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
            and (scope != "record_shard" or binding.response_consumed)
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
        cached = self._runtime_parent_cache.get(artifact_id)
        if cached is not None:
            return cached
        artifact = self._artifact_v2(artifact_id)
        schema = artifact.meta.get("payload_schema_id")
        if not isinstance(schema, str):
            raise IntegrityViolation(
                "runtime parent Artifact has no exact payload schema",
                artifact_id=artifact_id,
            )
        info = ParentInfo(
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            payload_schema_id=schema,
            version_tuple=artifact.version_tuple,
            payload_hash=artifact.payload_hash,
        )
        self._runtime_parent_cache[artifact_id] = info
        return info

    def _validate_tool_context_parent(
        self,
        *,
        run: RunRecord,
        link: RunToolIntermediateLinkV1,
    ) -> None:
        """Re-read the complete typed context closure before manifest projection."""

        attempt = self._attempt_authority(run.run_id, link.attempt_no)
        plan = run.payload.execution_version_plan
        node = (
            None
            if plan is None
            else next(
                (item for item in plan.nodes if item.agent_node_id == link.agent_node_id),
                None,
            )
        )
        if (
            run.payload.llm_execution_mode == "not_applicable"
            or attempt is None
            or attempt.fencing_token != link.fencing_token
            or node is None
            or node.prompt_version != link.prompt_version
        ):
            raise IntegrityViolation("prompt-context link escapes retained Run/attempt authority")

        artifact = self._artifact_v2(link.artifact_id)
        blob = self._read_published_artifact_bytes(artifact)
        try:
            decoded = json.loads(blob)
            context = AgentPromptContextV1.model_validate(decoded)
            validate_agent_prompt_context_kind(
                agent_node_id=link.agent_node_id,
                context_kind=context.context_kind,
                target_call_ordinal=link.target_call_ordinal,
                prior_consumption=context.prior_consumption,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("prompt-context payload is invalid") from exc
        canonical = canonical_json(context.model_dump(mode="json")).encode("utf-8")
        digest = sha256_lowerhex(blob)
        upstream_ids = tuple(sorted(item.artifact_id for item in context.upstream_artifacts))
        if (
            blob != canonical
            or digest != link.payload_hash
            or digest != artifact.payload_hash
            or artifact.object_ref.sha256 != digest
            or artifact.object_ref.size_bytes != len(blob)
            or artifact.kind != "source_raw"
            or artifact.version_tuple
            != VersionTuple(
                doc_version=run.payload.version_tuple.doc_version,
                tool_version="agent-prompt-context@1",
            )
            or artifact.meta.get("payload_schema_id") != "agent-prompt-context@1"
            or artifact.meta.get("producer_run_id") != run.run_id
            or artifact.meta.get("producer_attempt_no") != link.attempt_no
            or artifact.meta.get("target_call_ordinal") != link.target_call_ordinal
            or artifact.meta.get("agent_node_id") != link.agent_node_id
            or artifact.meta.get("prompt_version") != link.prompt_version
            or context.run_id != run.run_id
            or context.attempt_no != link.attempt_no
            or context.target_call_ordinal != link.target_call_ordinal
            or context.agent_node_id != link.agent_node_id
            or context.prompt_version != link.prompt_version
            or tuple(artifact.lineage) != upstream_ids
        ):
            raise IntegrityViolation("prompt-context Artifact/link/payload closure differs")
        source_bindings = tuple(
            item for item in context.upstream_artifacts if item.binding_key.startswith("source:")
        )
        prior_bindings = {
            item.binding_key: item
            for item in context.upstream_artifacts
            if item.binding_key in {"prior.prompt", "prior.cassette_source"}
        }
        if len(source_bindings) + len(prior_bindings) != len(context.upstream_artifacts):
            raise IntegrityViolation("prompt-context upstream binding key is not retained")
        if any(item.artifact_id not in run.payload.input_artifact_ids for item in source_bindings):
            raise IntegrityViolation("prompt-context source lineage escapes frozen Run inputs")

        prior = context.prior_consumption
        expected_prior_keys: set[str] = set()
        if prior is not None:
            if prior.call_ordinal != context.target_call_ordinal - 1:
                raise IntegrityViolation("prompt-context prior call is not target-minus-one")
            expected_prior_keys.add("prior.prompt")
            mode = run.payload.llm_execution_mode
            expected_cassette_source = (
                prior.cassette_shard_artifact_id
                if mode == "record"
                else run.payload.cassette_artifact_id
                if mode == "replay"
                else None
            )
            if (
                prior.cassette_source_artifact_id != expected_cassette_source
                or (mode == "record" and expected_cassette_source is None)
                or (
                    mode == "replay"
                    and expected_cassette_source not in run.payload.input_artifact_ids
                )
            ):
                raise IntegrityViolation(
                    "prompt-context prior cassette source differs from Run mode"
                )
            if expected_cassette_source is not None:
                expected_prior_keys.add("prior.cassette_source")
        if set(prior_bindings) != expected_prior_keys:
            raise IntegrityViolation("prompt-context prior direct-parent set is not exact")
        if prior is not None:
            prompt_parent = prior_bindings["prior.prompt"]
            if (
                prompt_parent.artifact_id != prior.prompt_artifact_id
                or prompt_parent.artifact_kind != "source_rendered"
                or prompt_parent.payload_schema_id != "source-rendered@1"
            ):
                raise IntegrityViolation("prompt-context prior prompt parent is not exact")
            cassette_parent = prior_bindings.get("prior.cassette_source")
            if cassette_parent is not None and (
                cassette_parent.artifact_id != prior.cassette_source_artifact_id
                or cassette_parent.artifact_kind != "cassette_bundle"
                or cassette_parent.payload_schema_id
                != (
                    "cassette-bundle@1"
                    if run.payload.llm_execution_mode == "replay"
                    else "cassette-record-shard@1"
                )
            ):
                raise IntegrityViolation("prompt-context prior cassette source parent is not exact")

        upstream_trust: list[str] = []
        for binding in context.upstream_artifacts:
            upstream = self._artifact_v2(binding.artifact_id)
            schema = upstream.meta.get("payload_schema_id")
            if (
                upstream.kind != binding.artifact_kind
                or schema != binding.payload_schema_id
                or upstream.payload_hash != binding.payload_hash
                or upstream.object_ref.sha256 != binding.payload_hash
            ):
                raise IntegrityViolation("prompt-context upstream binding is not exact")
            raw_upstream_provenance = upstream.meta.get("provenance")
            if raw_upstream_provenance is None:
                upstream_trust.append("untrusted_external")
                continue
            try:
                upstream_provenance = ProvenanceV1.model_validate(raw_upstream_provenance)
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("prompt-context upstream provenance is invalid") from exc
            if (
                upstream_provenance.source_hash != upstream.payload_hash
                or upstream_provenance.parent_source_artifact_ids != tuple(upstream.lineage)
            ):
                raise IntegrityViolation("prompt-context upstream provenance differs")
            _require_registered_source_provenance(
                upstream_provenance,
                label="prompt-context upstream provenance",
            )
            upstream_trust.append(upstream_provenance.trust)

        try:
            provenance = ProvenanceV1.model_validate(artifact.meta.get("provenance"))
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("prompt-context provenance is invalid") from exc
        _require_registered_source_provenance(
            provenance,
            required_prompt_purposes=frozenset({"context", "tool_output"}),
            label="prompt-context provenance",
        )
        if (
            provenance.source_kind_registry_version != 1
            or provenance.source_kind_id != "tool_output"
            or provenance.source_hash != artifact.payload_hash
            or provenance.parent_source_artifact_ids != upstream_ids
            or provenance.trust != most_conservative_trust(tuple(upstream_trust))
        ):
            raise IntegrityViolation("prompt-context provenance/hash/lineage/trust differs")

        validate_artifact_producer(
            artifact,
            ProducerValidationContext(
                expected_versions={
                    "doc_version": run.payload.version_tuple.doc_version,
                },
                llm_execution_mode=run.payload.llm_execution_mode,
                tool_output=True,
            ),
        )

        if prior is not None:
            route = self._ledger.get_model_route_link(
                run.run_id,
                prior.attempt_no,
                prior.call_ordinal,
                prior.route_ordinal,
            )
            consumption = self._ledger.get_model_response_consumption(
                run.run_id,
                prior.attempt_no,
                prior.call_ordinal,
                prior.route_ordinal,
            )
            if (
                route is None
                or consumption is None
                or route.prompt_artifact_id != prior.prompt_artifact_id
                or route.request_hash != prior.request_hash
                or route.routing_decision_kind != prior.routing_decision_kind
                or route.routing_decision_id != prior.routing_decision_id
                or consumption.execution_source != prior.execution_source
                or consumption.reservation_group_id != prior.reservation_group_id
                or consumption.transport_attempt != prior.transport_attempt
                or consumption.cassette_shard_artifact_id != prior.cassette_shard_artifact_id
                or consumption.response_digest != prior.response_digest
            ):
                raise IntegrityViolation("prompt-context prior consumption is not authoritative")

    def _artifact_v2(self, artifact_id: str) -> ArtifactV2:
        planned = self._planned_artifact_overlay.get(artifact_id)
        if planned is not None:
            return planned[0]
        cached = self._artifact_cache.get(artifact_id)
        if cached is not None:
            return cached
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
        self._artifact_cache[artifact_id] = parsed
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
            cached = self._published_blob_cache.get(artifact.artifact_id)
            if cached is not None:
                return cached
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
        if planned is None:
            self._published_blob_cache[artifact.artifact_id] = blob
        return blob

    def _read_authoritative_parent_payload(
        self,
        parent: ParentInfo,
    ) -> Mapping[str, object]:
        """Schema-decode one exact committed semantic parent without side effects."""

        artifact = self._artifact_v2(parent.artifact_id)
        schema = artifact.meta.get("payload_schema_id")
        if (
            artifact.kind != parent.kind
            or artifact.version_tuple != parent.version_tuple
            or schema != parent.payload_schema_id
        ):
            raise IntegrityViolation(
                "authoritative semantic parent differs from its typed lineage facts",
                artifact_id=parent.artifact_id,
            )
        blob = self._read_published_artifact_bytes(artifact)
        return decode_and_validate_artifact_payload(
            payload_schema_id=parent.payload_schema_id,
            blob=blob,
            external_decoders=self._payload_decoders,
        )

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
            payload_hash=parsed.payload_hash,
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


def _inject_run_intermediates(
    *,
    child_lineage: tuple[str, ...],
    lineage_policy: object,
    run_intermediates: Mapping[str, ParentInfo],
) -> tuple[str, ...]:
    """Inject every committed Run intermediate selected by the child's policy.

    Rendered prompts are content-addressed and committed before the worker can
    prepare its result, so a handler cannot safely predict their Artifact IDs.
    The terminal transaction re-reads the Run's durable intermediate links and
    completes only the direct-parent roles whose kind/schema allowlists match.
    Injecting all matches also makes role cardinality fail closed if retained
    authority exceeds a policy maximum.
    """

    existing = set(child_lineage)
    injected: set[str] = set()
    for rule in lineage_policy.parent_rules:  # type: ignore[attr-defined]
        if rule.source != "run_intermediate":
            continue
        for artifact_id, info in run_intermediates.items():
            if artifact_id in existing:
                continue
            if info.kind not in rule.artifact_kinds:
                continue
            if info.payload_schema_id not in rule.payload_schema_ids:
                continue
            injected.add(artifact_id)
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
