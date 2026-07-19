"""Terminal-publication adapters binding the Task-9 ``TerminalPublisher`` to SQL.

The Task-9 :class:`gameforge.platform.publication.TerminalPublisher` is the generic
terminal engine; it writes through six injected ports (``artifacts``/``blobs``/
``findings``/``ledger``/``audit`` + the workflow-effect resolver). Task 9 exercised
those ports with in-memory doubles; this module supplies the concrete, transaction-
bound production adapters the worker composition root binds into
``RunLifecycleCapabilities.publication``:

* :class:`WorkerBlobStore` — ``BlobStore`` over ``LocalObjectStore`` + a shared
  in-process location registry (the ObjectStore is keyed by ``ObjectLocation`` while
  ``BlobStore`` is keyed by ``ObjectRef``);
* :class:`WorkerBlobStager` — the only terminal-output ObjectStore writer, invoked
  between the read plan and the write UoW;
* :class:`WorkerArtifactPort` — ``ArtifactPort`` that consumes an explicit staged
  receipt, re-stats its generation, and binds/reuses an active ``ObjectBinding``
  before persisting the ``ArtifactV2`` row;
* :class:`WorkerManifestLedger` — ``ManifestLedger`` over ``SqlRunRepository``
  and exact Artifact/ObjectBinding authorities (routes, consumptions, execution
  identity, prompt links, cassette shards/bundles, failures and finding links);
* :class:`WorkerAuditPort` — ``AuditPort`` over the platform ``AuditGate``;
* :class:`WorkerCommandPublicationGateway` — the ``RunPublicationGateway`` the worker
  claim uses (records the ``attempt.leased`` claim through audit).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import threading
from copy import deepcopy
from types import SimpleNamespace
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol
from weakref import WeakKeyDictionary, WeakSet

from gameforge.apps.worker.execution_identity import build_authoritative_execution_identity
from gameforge.apps.worker.prompt_rendering import CanonicalPromptRendererAuthority
from gameforge.contracts.canonical import canonical_json, canonical_sha256, sha256_lowerhex
from gameforge.contracts.errors import (
    AttemptFenceStateRejected,
    Conflict,
    IntegrityViolation,
)
from gameforge.contracts.findings import FindingRevisionV1
from gameforge.contracts.jobs import (
    AgentPromptArtifactBindingV1,
    AgentPromptContextDraftV1,
    AgentPromptContextV1,
    AgentPromptPriorConsumptionV1,
    AgentPromptSourceMessageV1,
    MAX_RUN_MANIFEST_PARENT_BINDINGS,
    RunAttempt,
    RunEvent,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunLease,
    RunModelResponseConsumptionV1,
    RunModelRouteLinkV1,
    RunRecord,
    RunToolIntermediateLinkV1,
    validate_agent_prompt_context_kind,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    AuditSubject,
    ExecutionIdentityV1,
    ObjectBinding,
    ObjectLocation,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
)
from gameforge.contracts.model_router import (
    ModelRequestV1,
    ModelRequestV2,
    request_hash as model_request_hash,
)
from gameforge.contracts.routing import canonical_model_snapshot_id
from gameforge.contracts.storage import ObjectStat, UtcClock
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.contracts.provenance import (
    OriginRefV1,
    ProvenanceTransformationV1,
    ProvenanceV1,
    TrustLevel,
    most_conservative_trust,
)
from gameforge.platform.audit.gate import (
    AuditAppendIntent,
    AuditGate,
    PreflightedAuditBatch,
)
from gameforge.platform.lineage.validation import (
    ProducerValidationContext,
    validate_artifact_producer,
)
from gameforge.platform.runs.commands import (
    AgentPromptContextPublicationRequest,
    AgentPromptContextPublicationResult,
    PromptRenderPublicationRequest,
    PromptRenderPublicationResult,
    RunCommandService,
)
from gameforge.platform.runs.lifecycle import AttemptWriteFence, TerminalAuthorityDrift
from gameforge.platform.provenance.registry import build_source_kind_registry
from gameforge.platform.terminal_staging import (
    PreverifiedAbsentArtifactBinding,
    PreverifiedArtifactBinding,
    StagedReceipt,
    StagedTerminalPublication,
    TerminalPublicationDraft,
)
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository


_PROMPT_RENDER_OPERATION = "worker.prompt-rendered@1"
_AGENT_PROMPT_CONTEXT_OPERATION = "worker.agent-prompt-context@1"
_AGENT_PROMPT_CONTEXT_TOOL_VERSION = "agent-prompt-context@1"


@dataclass(frozen=True, slots=True)
class _ArtifactBatchState:
    """Complete immutable worker batch retained outside its opaque handle."""

    writes: tuple[
        tuple[
            ArtifactV2,
            StagedReceipt,
            PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding,
        ],
        ...,
    ]
    owner: "WorkerArtifactPort"
    artifact_repository: object
    binding_repository: object
    repository_binding_preflight: object | None
    repository_artifact_preflight: object | None
    transaction_identity: tuple[object, object] | None


_ARTIFACT_BATCH_STATE_LOCK = threading.Lock()
_ARTIFACT_BATCH_STATES: WeakKeyDictionary[object, _ArtifactBatchState] = WeakKeyDictionary()
_CONSUMED_ARTIFACT_BATCHES: WeakSet[object] = WeakSet()


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _PreflightedArtifactBatch:
    """Opaque one-shot handle with no instance authority fields."""

    def consume(
        self,
        owner: "WorkerArtifactPort",
    ) -> tuple[
        tuple[
            tuple[
                ArtifactV2,
                StagedReceipt,
                PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding,
            ],
            ...,
        ],
        object | None,
        object | None,
    ]:
        with _ARTIFACT_BATCH_STATE_LOCK:
            state = _ARTIFACT_BATCH_STATES.get(self)
            if state is None:
                raise IntegrityViolation("terminal Artifact batch lacks its trusted preflight seal")
            if self in _CONSUMED_ARTIFACT_BATCHES:
                raise IntegrityViolation(
                    "terminal Artifact batch preflight seal was already consumed"
                )
            if state.owner is not owner:
                raise IntegrityViolation(
                    "terminal Artifact batch belongs to another transaction-bound port"
                )
            if (
                owner._artifacts is not state.artifact_repository
                or owner._object_bindings is not state.binding_repository
            ):
                raise IntegrityViolation(
                    "terminal Artifact batch repository capabilities changed after preflight"
                )
            current_transaction_identity = owner._current_transaction_identity()
            retained_transaction_identity = state.transaction_identity
            if (retained_transaction_identity is None) != (
                current_transaction_identity is None
            ) or (
                retained_transaction_identity is not None
                and current_transaction_identity is not None
                and any(
                    retained is not current
                    for retained, current in zip(
                        retained_transaction_identity,
                        current_transaction_identity,
                        strict=True,
                    )
                )
            ):
                raise IntegrityViolation(
                    "terminal Artifact batch belongs to another transaction instance"
                )
            repository_preflights = (
                state.repository_binding_preflight,
                state.repository_artifact_preflight,
            )
            if (repository_preflights[0] is None) != (repository_preflights[1] is None):
                raise IntegrityViolation(
                    "terminal Artifact batch carries a partial repository preflight"
                )
            if repository_preflights[0] is not None and (
                not callable(
                    getattr(state.binding_repository, "apply_terminal_preverified_many", None)
                )
                or not callable(getattr(state.artifact_repository, "put_preflighted_many", None))
            ):
                raise IntegrityViolation(
                    "terminal Artifact repository lost its sealed apply capability"
                )
            _CONSUMED_ARTIFACT_BATCHES.add(self)
        # Consume before the first possible DML.  A later exception rolls the
        # surrounding transaction back and the authority token remains unusable.
        return (
            state.writes,
            state.repository_binding_preflight,
            state.repository_artifact_preflight,
        )


def _issue_artifact_batch(state: _ArtifactBatchState) -> _PreflightedArtifactBatch:
    handle = _PreflightedArtifactBatch()
    with _ARTIFACT_BATCH_STATE_LOCK:
        _ARTIFACT_BATCH_STATES[handle] = state
    return handle


def _same_immutable_artifact(stored: object, expected: ArtifactV2) -> bool:
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


def _utc_text(clock: UtcClock) -> str:
    value = clock.now_utc()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise IntegrityViolation("worker publication clock must return UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _immutable_artifact_wire(artifact: ArtifactV2) -> str:
    return canonical_json(artifact.model_dump(mode="json", exclude={"created_at"}))


def _stable_source_ids(source_artifact_ids: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not source_artifact_ids
        or any(not isinstance(value, str) or not value for value in source_artifact_ids)
        or source_artifact_ids != tuple(sorted(set(source_artifact_ids)))
    ):
        raise IntegrityViolation("prompt source Artifact ids must be stable-unique")
    return source_artifact_ids


class PromptSourceAuthorizationPort(Protocol):
    """Authorize one exact source set against persistent Run/attempt authority.

    Task 10 has no typed link for general tool/retrieval intermediates.  The built-in
    implementation therefore accepts only Run-creation frozen inputs.  A future
    implementation may admit a ``tool_output``/``retrieval_result`` ``source_raw``
    only after proving a committed link owned by the same fenced attempt and a
    retained versioned allowlist; absence of that proof must remain fail-closed.
    """

    def require_authorized(
        self,
        *,
        run: RunRecord,
        fence: AttemptWriteFence,
        source_artifact_ids: tuple[str, ...],
        agent_node_id: str,
        prompt_version: str,
        target_call_ordinal: int,
    ) -> None: ...


class FrozenRunInputPromptSourceAuthority:
    """Current exact authority: only immutable inputs frozen at Run admission."""

    def require_authorized(
        self,
        *,
        run: RunRecord,
        fence: AttemptWriteFence,
        source_artifact_ids: tuple[str, ...],
        agent_node_id: str,
        prompt_version: str,
        target_call_ordinal: int,
    ) -> None:
        del agent_node_id, prompt_version, target_call_ordinal
        selected = _stable_source_ids(source_artifact_ids)
        if fence.run_id != run.run_id:
            raise IntegrityViolation("prompt source authority received another Run fence")
        frozen = frozenset(run.payload.input_artifact_ids)
        unexpected = tuple(value for value in selected if value not in frozen)
        if unexpected:
            raise IntegrityViolation(
                "prompt source is neither a frozen Run input nor a typed fenced intermediate",
                source_artifact_ids=unexpected,
            )


class FencedToolPromptSourceAuthority:
    """Authorize one exact per-call context link plus frozen Run inputs.

    The link is only a fenced locator.  All semantic facts are re-read from the
    immutable ``source_raw[agent-prompt-context@1]`` Artifact and its canonical
    payload.  This keeps trust/provenance/source bindings out of a second mutable
    authority while preventing a same-Run tool output from being reused by a
    different attempt, node, prompt version, or logical call.
    """

    def __init__(
        self,
        *,
        tool_link_loader: Callable[[str, int, int], RunToolIntermediateLinkV1 | None],
        artifact_loader: Callable[[str], ArtifactV2 | None],
        payload_loader: Callable[[ArtifactV2], bytes],
        call_projection_loader: Callable[
            [AttemptWriteFence, int, int],
            tuple[RunModelRouteLinkV1, RunModelResponseConsumptionV1 | None] | None,
        ]
        | None = None,
    ) -> None:
        self._tool_link_loader = tool_link_loader
        self._artifact_loader = artifact_loader
        self._payload_loader = payload_loader
        self._call_projection_loader = call_projection_loader

    def require_authorized(
        self,
        *,
        run: RunRecord,
        fence: AttemptWriteFence,
        source_artifact_ids: tuple[str, ...],
        agent_node_id: str,
        prompt_version: str,
        target_call_ordinal: int,
    ) -> None:
        selected = _stable_source_ids(source_artifact_ids)
        if fence.run_id != run.run_id:
            raise IntegrityViolation("prompt source authority received another Run fence")
        if (
            isinstance(target_call_ordinal, bool)
            or not isinstance(target_call_ordinal, int)
            or target_call_ordinal < 1
        ):
            raise IntegrityViolation("prompt source target call ordinal must be positive")
        frozen = frozenset(run.payload.input_artifact_ids)
        dynamic = tuple(value for value in selected if value not in frozen)
        raw_link = self._tool_link_loader(
            run.run_id,
            fence.attempt_no,
            target_call_ordinal,
        )
        if raw_link is None:
            if dynamic:
                raise IntegrityViolation(
                    "prompt source is neither a frozen Run input nor a typed fenced intermediate",
                    source_artifact_ids=dynamic,
                )
            return
        try:
            link = (
                raw_link
                if isinstance(raw_link, RunToolIntermediateLinkV1)
                else RunToolIntermediateLinkV1.model_validate(raw_link)
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("prompt context link is not canonical") from exc
        if selected != (link.artifact_id,):
            raise IntegrityViolation(
                "prompt source set must be exactly its per-call context Artifact",
                expected_context_artifact_id=link.artifact_id,
                supplied_source_artifact_ids=selected,
            )
        if (
            link.run_id != run.run_id
            or link.attempt_no != fence.attempt_no
            or link.target_call_ordinal != target_call_ordinal
            or link.agent_node_id != agent_node_id
            or link.prompt_version != prompt_version
            or link.fencing_token != fence.fencing_token
        ):
            raise IntegrityViolation("prompt context link differs from the current call fence")
        artifact = self._artifact_loader(link.artifact_id)
        if not isinstance(artifact, ArtifactV2) or artifact.artifact_id != link.artifact_id:
            raise IntegrityViolation("prompt context link does not resolve its exact Artifact")
        payload = self._payload_loader(artifact)
        self._validate_context(
            run=run,
            fence=fence,
            link=link,
            artifact=artifact,
            payload=payload,
            agent_node_id=agent_node_id,
            prompt_version=prompt_version,
            target_call_ordinal=target_call_ordinal,
        )

    def _validate_context(
        self,
        *,
        run: RunRecord,
        fence: AttemptWriteFence,
        link: RunToolIntermediateLinkV1,
        artifact: ArtifactV2,
        payload: bytes,
        agent_node_id: str,
        prompt_version: str,
        target_call_ordinal: int,
    ) -> None:
        try:
            decoded = json.loads(payload)
            context = AgentPromptContextV1.model_validate(decoded)
            validate_agent_prompt_context_kind(
                agent_node_id=agent_node_id,
                context_kind=context.context_kind,
                target_call_ordinal=target_call_ordinal,
                prior_consumption=context.prior_consumption,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("prompt context payload is invalid") from exc
        canonical = canonical_json(context.model_dump(mode="json")).encode("utf-8")
        digest = sha256_lowerhex(payload)
        upstream_ids = tuple(sorted(item.artifact_id for item in context.upstream_artifacts))
        if (
            payload != canonical
            or digest != link.payload_hash
            or digest != artifact.payload_hash
            or artifact.object_ref.sha256 != digest
            or artifact.object_ref.size_bytes != len(payload)
            or artifact.kind != "source_raw"
            or artifact.version_tuple
            != VersionTuple(
                doc_version=run.payload.version_tuple.doc_version,
                tool_version=_AGENT_PROMPT_CONTEXT_TOOL_VERSION,
            )
            or artifact.meta.get("payload_schema_id") != "agent-prompt-context@1"
            or artifact.meta.get("producer_run_id") != run.run_id
            or artifact.meta.get("producer_attempt_no") != fence.attempt_no
            or artifact.meta.get("target_call_ordinal") != target_call_ordinal
            or artifact.meta.get("agent_node_id") != agent_node_id
            or artifact.meta.get("prompt_version") != prompt_version
            or context.run_id != run.run_id
            or context.attempt_no != fence.attempt_no
            or context.target_call_ordinal != target_call_ordinal
            or context.agent_node_id != agent_node_id
            or context.prompt_version != prompt_version
            or tuple(artifact.lineage) != upstream_ids
        ):
            raise IntegrityViolation("prompt context Artifact/link/payload closure differs")
        frozen = frozenset(run.payload.input_artifact_ids)
        source_bindings = tuple(
            item for item in context.upstream_artifacts if item.binding_key.startswith("source:")
        )
        prior_bindings = {
            item.binding_key: item
            for item in context.upstream_artifacts
            if item.binding_key in {"prior.prompt", "prior.cassette_source"}
        }
        if len(source_bindings) + len(prior_bindings) != len(context.upstream_artifacts):
            raise IntegrityViolation("prompt context upstream binding key is not retained")
        if any(item.artifact_id not in frozen for item in source_bindings):
            raise IntegrityViolation("prompt context source lineage escapes frozen Run inputs")
        prior = context.prior_consumption
        expected_prior_keys: set[str] = set()
        if prior is not None:
            if prior.call_ordinal != context.target_call_ordinal - 1:
                raise IntegrityViolation("prompt context prior call is not target-minus-one")
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
                or (mode == "replay" and expected_cassette_source not in frozen)
            ):
                raise IntegrityViolation(
                    "prompt context prior cassette source differs from Run mode"
                )
            if expected_cassette_source is not None:
                expected_prior_keys.add("prior.cassette_source")
        if set(prior_bindings) != expected_prior_keys:
            raise IntegrityViolation("prompt context prior direct-parent set is not exact")
        if prior is not None:
            prompt_parent = prior_bindings["prior.prompt"]
            if (
                prompt_parent.artifact_id != prior.prompt_artifact_id
                or prompt_parent.artifact_kind != "source_rendered"
                or prompt_parent.payload_schema_id != "source-rendered@1"
            ):
                raise IntegrityViolation("prompt context prior prompt parent is not exact")
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
                raise IntegrityViolation("prompt context prior cassette source parent is not exact")

        upstream_trust: list[str] = []
        for binding in context.upstream_artifacts:
            upstream = self._artifact_loader(binding.artifact_id)
            if not isinstance(upstream, ArtifactV2) or (
                upstream.artifact_id != binding.artifact_id
                or upstream.kind != binding.artifact_kind
                or upstream.meta.get("payload_schema_id") != binding.payload_schema_id
                or upstream.payload_hash != binding.payload_hash
                or upstream.object_ref.sha256 != binding.payload_hash
            ):
                raise IntegrityViolation("prompt context upstream binding is not exact")
            raw_upstream_provenance = upstream.meta.get("provenance")
            if raw_upstream_provenance is None:
                upstream_trust.append("untrusted_external")
                continue
            try:
                upstream_provenance = ProvenanceV1.model_validate(raw_upstream_provenance)
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("prompt context upstream provenance is invalid") from exc
            if (
                upstream_provenance.source_hash != upstream.payload_hash
                or upstream_provenance.parent_source_artifact_ids != tuple(upstream.lineage)
            ):
                raise IntegrityViolation("prompt context upstream provenance differs")
            _require_registered_source_provenance(
                upstream_provenance,
                label="prompt context upstream provenance",
            )
            upstream_trust.append(upstream_provenance.trust)

        try:
            provenance = ProvenanceV1.model_validate(artifact.meta.get("provenance"))
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("prompt context provenance is invalid") from exc
        _require_registered_source_provenance(
            provenance,
            required_prompt_purposes=frozenset({"context", "tool_output"}),
            label="prompt context provenance",
        )
        if (
            provenance.source_kind_registry_version != 1
            or provenance.source_kind_id != "tool_output"
            or provenance.source_hash != artifact.payload_hash
            or provenance.parent_source_artifact_ids != upstream_ids
            or provenance.trust != most_conservative_trust(tuple(upstream_trust))
        ):
            raise IntegrityViolation("prompt context provenance/hash/lineage/trust differs")
        if prior is not None:
            if prior.attempt_no != fence.attempt_no or self._call_projection_loader is None:
                raise IntegrityViolation("prompt context prior authority is unavailable")
            projection = self._call_projection_loader(
                fence,
                prior.call_ordinal,
                prior.route_ordinal,
            )
            route, consumption = projection if projection is not None else (None, None)
            if (
                route is None
                or consumption is None
                or route.prompt_artifact_id != prior.prompt_artifact_id
                or route.request_hash != prior.request_hash
                or route.routing_decision_kind != prior.routing_decision_kind
                or route.routing_decision_id != prior.routing_decision_id
                or consumption.route_ordinal != prior.route_ordinal
                or consumption.execution_source != prior.execution_source
                or consumption.reservation_group_id != prior.reservation_group_id
                or consumption.transport_attempt != prior.transport_attempt
                or consumption.cassette_shard_artifact_id != prior.cassette_shard_artifact_id
                or consumption.response_digest != prior.response_digest
            ):
                raise IntegrityViolation("prompt context prior consumption is not authoritative")


@dataclass(frozen=True, slots=True)
class PromptRenderMaterial:
    """One verified blob-first prompt handoff into the command write UoW.

    This process-local object is not an authority.  The exact idempotency binding,
    Artifact/ObjectBinding, intermediate link, and audit record are all committed by
    :class:`WorkerCommandPublicationGateway` in the command service's transaction.
    """

    run_id: str
    attempt_no: int
    logical_call_ordinal: int
    call_ordinal: int | None
    route_ordinal: int
    fence: AttemptWriteFence
    idempotency_scope: str
    idempotency_key: str
    request_hash: str
    model_request: ModelRequestV1 | ModelRequestV2
    source_artifact_ids: tuple[str, ...]
    prompt_binding_id: str
    renderer_version: str
    artifact: ArtifactV2
    receipt: StagedReceipt


class PromptRenderMaterialRegistry:
    """Thread-safe non-authoritative bridge from blob staging to a fresh UoW."""

    def __init__(self, *, max_entries: int = 4096) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("prompt material registry max_entries must be positive")
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._materials: dict[tuple[str, str], PromptRenderMaterial] = {}

    def record(self, material: PromptRenderMaterial) -> None:
        identity = (material.idempotency_scope, material.idempotency_key)
        with self._lock:
            retained = self._materials.get(identity)
            if retained is not None and (
                retained.run_id != material.run_id
                or retained.attempt_no != material.attempt_no
                or retained.logical_call_ordinal != material.logical_call_ordinal
                or retained.call_ordinal != material.call_ordinal
                or retained.route_ordinal != material.route_ordinal
                or retained.fence != material.fence
                or retained.request_hash != material.request_hash
                or retained.model_request != material.model_request
                or retained.source_artifact_ids != material.source_artifact_ids
                or retained.prompt_binding_id != material.prompt_binding_id
                or retained.renderer_version != material.renderer_version
                or _immutable_artifact_wire(retained.artifact)
                != _immutable_artifact_wire(material.artifact)
            ):
                raise Conflict(
                    "prompt idempotency key is staged for different immutable material",
                    idempotency_scope=identity[0],
                    idempotency_key=identity[1],
                )
            if retained is None and len(self._materials) >= self._max_entries:
                raise IntegrityViolation("prompt material registry capacity is exhausted")
            # An exact retry may have written another verified backend generation.
            # Retain the latest explicit receipt; publication still refuses to remap
            # an already-active immutable Artifact binding.
            self._materials[identity] = material

    def discard(self, material: PromptRenderMaterial) -> None:
        """Remove only the exact handoff instance after its synchronous UoW closes."""

        identity = (material.idempotency_scope, material.idempotency_key)
        with self._lock:
            if self._materials.get(identity) is material:
                del self._materials[identity]

    def resolve(
        self,
        *,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> PromptRenderMaterial:
        with self._lock:
            material = self._materials.get((idempotency_scope, idempotency_key))
        if material is None:
            raise IntegrityViolation(
                "canonical prompt material was not staged before publication",
                idempotency_scope=idempotency_scope,
                idempotency_key=idempotency_key,
            )
        if material.request_hash != request_hash:
            raise Conflict(
                "prompt idempotency key is bound to another request hash",
                idempotency_scope=idempotency_scope,
                idempotency_key=idempotency_key,
            )
        return material


@dataclass(frozen=True, slots=True)
class AgentPromptContextMaterial:
    run_id: str
    attempt_no: int
    target_call_ordinal: int
    fence: AttemptWriteFence
    idempotency_scope: str
    idempotency_key: str
    context: AgentPromptContextV1
    source_artifact_ids: tuple[str, ...]
    artifact: ArtifactV2
    receipt: StagedReceipt


class AgentPromptContextMaterialRegistry:
    """Bounded process handoff; committed idempotency/link rows remain authority."""

    def __init__(self, *, max_entries: int = 4096) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("Agent prompt-context material capacity must be positive")
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._materials: dict[tuple[str, str], AgentPromptContextMaterial] = {}

    def record(self, material: AgentPromptContextMaterial) -> None:
        identity = (material.idempotency_scope, material.idempotency_key)
        with self._lock:
            retained = self._materials.get(identity)
            if retained is not None and (
                retained.run_id != material.run_id
                or retained.attempt_no != material.attempt_no
                or retained.target_call_ordinal != material.target_call_ordinal
                or retained.fence != material.fence
                or retained.context != material.context
                or retained.source_artifact_ids != material.source_artifact_ids
                or _immutable_artifact_wire(retained.artifact)
                != _immutable_artifact_wire(material.artifact)
            ):
                raise Conflict(
                    "Agent prompt-context idempotency key has different staged material",
                    idempotency_scope=identity[0],
                    idempotency_key=identity[1],
                )
            if retained is None and len(self._materials) >= self._max_entries:
                raise IntegrityViolation(
                    "Agent prompt-context material registry capacity is exhausted"
                )
            self._materials[identity] = material

    def discard(self, material: AgentPromptContextMaterial) -> None:
        identity = (material.idempotency_scope, material.idempotency_key)
        with self._lock:
            if self._materials.get(identity) is material:
                del self._materials[identity]

    def resolve(
        self,
        *,
        idempotency_scope: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> AgentPromptContextMaterial:
        with self._lock:
            material = self._materials.get((idempotency_scope, idempotency_key))
        if material is None:
            raise IntegrityViolation("Agent prompt-context material was not staged")
        if material.artifact.payload_hash != payload_hash:
            raise Conflict("Agent prompt-context idempotency key has another payload hash")
        return material


class WorkerAgentPromptContextPublisher:
    """Build and stage one governed tool-output context before model routing."""

    def __init__(
        self,
        *,
        run: RunRecord,
        fence: AttemptWriteFence,
        commands: RunCommandService,
        object_store: object,
        registry: AgentPromptContextMaterialRegistry,
        clock: UtcClock,
        source_artifact_loader: Callable[[str], ArtifactV2],
    ) -> None:
        self._run = run
        self._fence = fence
        self._commands = commands
        self._object_store = object_store
        self._registry = registry
        self._clock = clock
        self._source_artifact_loader = source_artifact_loader

    def publish_agent_prompt_context(
        self,
        *,
        model_request: ModelRequestV2,
        draft: AgentPromptContextDraftV1,
        target_call_ordinal: int,
        prior_consumption: AgentPromptPriorConsumptionV1 | None,
        idempotency_scope: str,
        idempotency_key: str,
        actor: AuditActor,
    ) -> AgentPromptContextPublicationResult:
        if self._fence.run_id != self._run.run_id:
            raise IntegrityViolation("Agent prompt-context publisher has another Run fence")
        plan = self._run.payload.execution_version_plan
        node = (
            None
            if plan is None
            else next(
                (item for item in plan.nodes if item.agent_node_id == model_request.agent_node_id),
                None,
            )
        )
        if (
            self._run.payload.llm_execution_mode == "not_applicable"
            or node is None
            or node.prompt_version != model_request.prompt_version
        ):
            raise IntegrityViolation("Agent prompt-context escapes the execution plan")
        messages = tuple(
            AgentPromptSourceMessageV1(
                role=message.role,
                content=message.content,
                tool_calls=tuple(message.tool_calls),
                purpose="context" if message.role == "user" else "tool_output",
            )
            for message in model_request.messages
            if message.role != "system"
        )
        if not messages or messages != draft.messages:
            raise IntegrityViolation(
                "Agent prompt-context draft differs from exact non-system messages"
            )
        selected_source_ids = _stable_source_ids(draft.source_artifact_ids)
        frozen_inputs = frozenset(self._run.payload.input_artifact_ids)
        if any(source_id not in frozen_inputs for source_id in selected_source_ids):
            raise IntegrityViolation("Agent prompt-context draft sources escape frozen Run inputs")
        if draft.include_previous_consumption != (prior_consumption is not None):
            raise IntegrityViolation(
                "Agent prompt-context prior-consumption request is not satisfied"
            )
        try:
            validate_agent_prompt_context_kind(
                agent_node_id=model_request.agent_node_id,
                context_kind=draft.context_kind,
                target_call_ordinal=target_call_ordinal,
                prior_consumption=prior_consumption,
            )
        except ValueError as exc:
            raise IntegrityViolation("Agent prompt-context kind is not authoritative") from exc
        binding_keys: dict[str, str] = {
            source_id: f"source:{ordinal:04d}"
            for ordinal, source_id in enumerate(selected_source_ids, start=1)
        }
        if prior_consumption is not None:
            mode = self._run.payload.llm_execution_mode
            expected_cassette_source = (
                prior_consumption.cassette_shard_artifact_id
                if mode == "record"
                else self._run.payload.cassette_artifact_id
                if mode == "replay"
                else None
            )
            if (
                prior_consumption.cassette_source_artifact_id != expected_cassette_source
                or (mode == "record" and expected_cassette_source is None)
                or (mode == "replay" and expected_cassette_source not in frozen_inputs)
            ):
                raise IntegrityViolation(
                    "Agent prompt-context prior cassette source differs from Run mode"
                )
            binding_keys[prior_consumption.prompt_artifact_id] = "prior.prompt"
            if prior_consumption.cassette_source_artifact_id is not None:
                binding_keys[prior_consumption.cassette_source_artifact_id] = (
                    "prior.cassette_source"
                )
        direct_parent_ids = tuple(sorted(binding_keys))
        bindings: list[AgentPromptArtifactBindingV1] = []
        trusts: list[TrustLevel] = []
        for source_id in direct_parent_ids:
            source = self._source_artifact_loader(source_id)
            if not isinstance(source, ArtifactV2) or source.artifact_id != source_id:
                raise IntegrityViolation("Agent prompt-context source loader returned another id")
            payload_schema_id = source.meta.get("payload_schema_id")
            if (
                not isinstance(payload_schema_id, str)
                or not payload_schema_id
                or source.object_ref.sha256 != source.payload_hash
            ):
                raise IntegrityViolation("Agent prompt-context source metadata is incomplete")
            raw_provenance = source.meta.get("provenance")
            if raw_provenance is None:
                trusts.append("untrusted_external")
            else:
                try:
                    provenance = ProvenanceV1.model_validate(raw_provenance)
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "Agent prompt-context source provenance is invalid"
                    ) from exc
                if (
                    provenance.source_hash != source.payload_hash
                    or provenance.parent_source_artifact_ids != tuple(source.lineage)
                ):
                    raise IntegrityViolation(
                        "Agent prompt-context source provenance differs from its Artifact"
                    )
                source_registry = build_source_kind_registry()
                source_definition = (
                    source_registry.get(provenance.source_kind_id)
                    if provenance.source_kind_registry_version == source_registry.registry_version
                    else None
                )
                if (
                    source_definition is None
                    or provenance.trust not in source_definition.allowed_trust_levels
                ):
                    raise IntegrityViolation(
                        "Agent prompt-context source trust escapes its registry"
                    )
                trusts.append(provenance.trust)
            bindings.append(
                AgentPromptArtifactBindingV1(
                    binding_key=binding_keys[source_id],
                    artifact_id=source.artifact_id,
                    artifact_kind=source.kind,
                    payload_schema_id=payload_schema_id,
                    payload_hash=source.payload_hash,
                )
            )
        context = AgentPromptContextV1(
            context_kind=draft.context_kind,
            run_id=self._run.run_id,
            attempt_no=self._fence.attempt_no,
            target_call_ordinal=target_call_ordinal,
            agent_node_id=model_request.agent_node_id,
            prompt_version=model_request.prompt_version,
            messages=messages,
            upstream_artifacts=tuple(bindings),
            semantic_bindings=draft.semantic_bindings,
            prior_consumption=prior_consumption,
        )
        payload = canonical_json(context.model_dump(mode="json")).encode("utf-8")
        stored = self._object_store.put_verified(payload)  # type: ignore[attr-defined]
        staged_stat = self._object_store.stat(stored.location)  # type: ignore[attr-defined]
        if staged_stat.ref != stored.ref or staged_stat.location != stored.location:
            raise IntegrityViolation("agent prompt context staging returned another generation")
        input_hash = sha256_lowerhex(
            canonical_json(
                {
                    "messages": [item.model_dump(mode="json") for item in context.messages],
                    "upstream_artifacts": [
                        binding.model_dump(mode="json") for binding in context.upstream_artifacts
                    ],
                    "semantic_bindings": [
                        binding.model_dump(mode="json") for binding in context.semantic_bindings
                    ],
                    "prior_consumption": (
                        None
                        if context.prior_consumption is None
                        else context.prior_consumption.model_dump(mode="json")
                    ),
                }
            ).encode("utf-8")
        )
        doc_version = self._run.payload.version_tuple.doc_version
        if not isinstance(doc_version, str) or not doc_version:
            raise IntegrityViolation(
                "Agent prompt-context Run has no frozen doc_version to inherit"
            )
        context_registry = build_source_kind_registry()
        context_definition = context_registry.get("tool_output")
        context_trust = most_conservative_trust(tuple(trusts))
        if (
            context_definition is None
            or context_trust not in context_definition.allowed_trust_levels
            or "tool_output" not in context_definition.allowed_prompt_purposes
        ):
            raise IntegrityViolation("Agent prompt-context tool-output provenance is forbidden")
        provenance = ProvenanceV1(
            source_kind_registry_version=context_registry.registry_version,
            source_kind_id="tool_output",
            origin_ref=OriginRefV1(
                opaque_source_id=(
                    f"agent-prompt-context:{self._run.run_id}:"
                    f"{self._fence.attempt_no}:{target_call_ordinal}"
                ),
                source_revision=stored.ref.sha256,
            ),
            parent_source_artifact_ids=direct_parent_ids,
            connector_id=_AGENT_PROMPT_CONTEXT_TOOL_VERSION,
            connector_version="1",
            trust=context_trust,
            source_hash=stored.ref.sha256,
            transformations=(
                ProvenanceTransformationV1(
                    tool_version=_AGENT_PROMPT_CONTEXT_TOOL_VERSION,
                    input_hash=input_hash,
                    output_hash=stored.ref.sha256,
                ),
            ),
        )
        artifact = build_artifact_v2(
            kind="source_raw",
            version_tuple=VersionTuple(
                doc_version=doc_version,
                tool_version=_AGENT_PROMPT_CONTEXT_TOOL_VERSION,
            ),
            lineage=direct_parent_ids,
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": "agent-prompt-context@1",
                "provenance": provenance.model_dump(mode="json"),
                "producer_run_id": self._run.run_id,
                "producer_attempt_no": self._fence.attempt_no,
                "target_call_ordinal": target_call_ordinal,
                "agent_node_id": model_request.agent_node_id,
                "prompt_version": model_request.prompt_version,
                "replayability": (
                    "online_only"
                    if self._run.payload.llm_execution_mode == "live"
                    else "cassette_replay"
                ),
            },
            created_at=_utc_text(self._clock),
        )
        validate_artifact_producer(
            artifact,
            ProducerValidationContext(
                expected_versions={"doc_version": doc_version},
                llm_execution_mode=self._run.payload.llm_execution_mode,
                tool_output=True,
            ),
        )
        material = AgentPromptContextMaterial(
            run_id=self._run.run_id,
            attempt_no=self._fence.attempt_no,
            target_call_ordinal=target_call_ordinal,
            fence=self._fence,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            context=context,
            source_artifact_ids=direct_parent_ids,
            artifact=artifact,
            receipt=StagedReceipt(
                slot=f"agent-prompt-context:{self._fence.attempt_no}:{target_call_ordinal}",
                ref=stored.ref,
                location=stored.location,
                verified_at=staged_stat.verified_at,
                generation_verification_token=(staged_stat.generation_verification_token),
            ),
        )
        self._registry.record(material)
        try:
            result = self._commands.publish_agent_prompt_context(
                AgentPromptContextPublicationRequest(
                    fence=self._fence,
                    target_call_ordinal=target_call_ordinal,
                    artifact_id=artifact.artifact_id,
                    payload_hash=artifact.payload_hash,
                    agent_node_id=model_request.agent_node_id,
                    prompt_version=model_request.prompt_version,
                    idempotency_scope=idempotency_scope,
                    idempotency_key=idempotency_key,
                    actor=actor,
                )
            )
            if (
                result.link.artifact_id != artifact.artifact_id
                or result.link.payload_hash != artifact.payload_hash
            ):
                raise IntegrityViolation(
                    "Agent prompt-context command returned another Artifact link"
                )
            return result
        finally:
            self._registry.discard(material)


class WorkerPromptRenderPublisher:
    """Stage a canonical ``ModelRequestV2`` then invoke the fenced command path."""

    def __init__(
        self,
        *,
        run: RunRecord,
        fence: AttemptWriteFence,
        commands: RunCommandService,
        object_store: object,
        registry: PromptRenderMaterialRegistry,
        clock: UtcClock,
        source_artifact_loader: Callable[[str], ArtifactV2],
        source_payload_loader: Callable[[ArtifactV2], bytes],
        prompt_renderer: CanonicalPromptRendererAuthority,
        source_authority: PromptSourceAuthorizationPort | None = None,
    ) -> None:
        self._run = run
        self._fence = fence
        self._commands = commands
        self._object_store = object_store
        self._registry = registry
        self._clock = clock
        self._source_artifact_loader = source_artifact_loader
        self._source_payload_loader = source_payload_loader
        self._prompt_renderer = prompt_renderer
        self._source_authority = source_authority or FrozenRunInputPromptSourceAuthority()

    def require_replay_source_semantics(
        self,
        *,
        handler_request: ModelRequestV1 | ModelRequestV2,
        source_request: ModelRequestV1 | ModelRequestV2,
    ) -> None:
        self._prompt_renderer.require_replay_source_semantics(
            handler_request=handler_request,
            source_request=source_request,
        )

    def publish_prompt_rendered(
        self,
        request: PromptRenderPublicationRequest,
        *,
        model_request: ModelRequestV1 | ModelRequestV2,
        source_artifact_ids: tuple[str, ...],
    ) -> PromptRenderPublicationResult:
        if request.fence != self._fence or request.fence.run_id != self._run.run_id:
            raise IntegrityViolation("prompt publisher received another attempt fence")
        plan = self._run.payload.execution_version_plan
        if plan is None or self._run.payload.llm_execution_mode == "not_applicable":
            raise IntegrityViolation("prompt publication requires an LLM execution plan")
        node = next(
            (item for item in plan.nodes if item.agent_node_id == model_request.agent_node_id),
            None,
        )
        model_id = canonical_model_snapshot_id(model_request.model_snapshot)
        if (
            node is None
            or node.prompt_version != model_request.prompt_version
            or model_id not in node.allowed_model_snapshots
        ):
            raise IntegrityViolation("rendered request escapes the frozen execution plan")

        selected_source_ids = _stable_source_ids(source_artifact_ids)
        self._source_authority.require_authorized(
            run=self._run,
            fence=self._fence,
            source_artifact_ids=selected_source_ids,
            agent_node_id=model_request.agent_node_id,
            prompt_version=model_request.prompt_version,
            target_call_ordinal=request.logical_call_ordinal,
        )
        source_artifacts: list[ArtifactV2] = []
        for source_artifact_id in selected_source_ids:
            source_artifact = self._source_artifact_loader(source_artifact_id)
            if source_artifact.artifact_id != source_artifact_id:
                raise IntegrityViolation(
                    "prompt source loader returned another immutable Artifact",
                    source_artifact_id=source_artifact_id,
                )
            source_artifacts.append(source_artifact)
        self._prompt_renderer.require_source_metadata_bounds(
            agent_node_id=model_request.agent_node_id,
            prompt_version=model_request.prompt_version,
            source_artifacts=tuple(source_artifacts),
        )
        sources: list[tuple[ArtifactV2, bytes]] = []
        for source_artifact in source_artifacts:
            source_payload = self._source_payload_loader(source_artifact)
            if sha256_lowerhex(source_payload) != source_artifact.payload_hash:
                raise IntegrityViolation(
                    "prompt source loader returned bytes for another immutable Artifact",
                    source_artifact_id=source_artifact.artifact_id,
                )
            sources.append((source_artifact, source_payload))
        canonical_render = self._prompt_renderer.require_model_request(
            model_request=model_request,
            sources=tuple(sources),
        )
        if canonical_render.agent_tool_version != node.tool_version:
            raise IntegrityViolation(
                "prompt tool schema authority differs from the frozen execution plan"
            )
        payload = canonical_json(model_request.model_dump(mode="json")).encode("utf-8")
        expected_hash = model_request_hash(model_request).removeprefix("sha256:")
        if request.request_hash != expected_hash:
            raise IntegrityViolation("prompt publication request hash differs from its payload")
        stored = self._object_store.put_verified(payload)  # type: ignore[attr-defined]
        staged_stat = self._object_store.stat(stored.location)  # type: ignore[attr-defined]
        if staged_stat.ref != stored.ref or staged_stat.location != stored.location:
            raise IntegrityViolation("rendered prompt staging returned another generation")
        artifact = build_artifact_v2(
            kind="source_rendered",
            version_tuple=VersionTuple(
                doc_version=canonical_render.inherited_doc_version,
                prompt_version=model_request.prompt_version,
                agent_graph_version=plan.agent_graph_version,
                tool_version=canonical_render.renderer_version,
            ),
            lineage=selected_source_ids,
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": "source-rendered@1",
                "renderer_version": canonical_render.renderer_version,
                "prompt_binding_id": canonical_render.binding_id,
                "agent_tool_version": canonical_render.agent_tool_version,
                "request_configuration_digest": (canonical_render.request_configuration_digest),
                "sanitizer_version": "canonical-model-request@1",
                "replayability": (
                    "online_only"
                    if self._run.payload.llm_execution_mode == "live"
                    else "cassette_replay"
                ),
                "provenance": canonical_render.provenance_for_output(stored.ref.sha256).model_dump(
                    mode="json"
                ),
                "prompt_parts": [
                    part.model_dump(mode="json") for part in canonical_render.prompt_parts
                ],
                "producer_run_id": self._run.run_id,
                "producer_attempt_no": self._fence.attempt_no,
                "logical_call_ordinal": request.logical_call_ordinal,
                "route_ordinal": request.route_ordinal,
            },
            created_at=_utc_text(self._clock),
        )
        validate_artifact_producer(
            artifact,
            ProducerValidationContext(
                expected_versions={
                    "doc_version": canonical_render.inherited_doc_version,
                    "prompt_version": model_request.prompt_version,
                    "agent_graph_version": plan.agent_graph_version,
                },
                llm_execution_mode=self._run.payload.llm_execution_mode,
                rendered_prompt_evidence=True,
            ),
        )
        material = PromptRenderMaterial(
            run_id=self._run.run_id,
            attempt_no=self._fence.attempt_no,
            logical_call_ordinal=request.logical_call_ordinal,
            call_ordinal=request.call_ordinal,
            route_ordinal=request.route_ordinal,
            fence=self._fence,
            idempotency_scope=request.idempotency_scope,
            idempotency_key=request.idempotency_key,
            request_hash=expected_hash,
            model_request=model_request,
            source_artifact_ids=selected_source_ids,
            prompt_binding_id=canonical_render.binding_id,
            renderer_version=canonical_render.renderer_version,
            artifact=artifact,
            receipt=StagedReceipt(
                slot=f"prompt:{self._fence.attempt_no}:{request.idempotency_key}",
                ref=stored.ref,
                location=stored.location,
                verified_at=staged_stat.verified_at,
                generation_verification_token=(staged_stat.generation_verification_token),
            ),
        )
        self._registry.record(material)
        try:
            result = self._commands.publish_prompt_rendered(
                request.model_copy(update={"artifact_id": artifact.artifact_id})
            )
        finally:
            # The command call above consumes the material synchronously in its
            # fresh write UoW.  A failed call is retried by restaging exact bytes;
            # retaining process-local material would only leak cross-attempt state.
            self._registry.discard(material)
        if result.link.artifact_id != artifact.artifact_id:
            raise IntegrityViolation("prompt command retained another rendered Artifact")
        return result


def _read_bounded_stream(
    stream: object,
    *,
    max_bytes: int,
    label: str,
    object_key: str,
) -> bytes:
    """Read through legal binary short reads without crossing one hard byte cap."""

    read = getattr(stream, "read", None)
    if not callable(read):
        raise IntegrityViolation(f"{label} stream is not readable", object_key=object_key)
    payload = bytearray()
    remaining = max_bytes
    while remaining > 0:
        chunk = read(remaining)
        if chunk == b"":
            break
        if not isinstance(chunk, bytes):
            raise IntegrityViolation(
                f"{label} stream returned a non-byte chunk",
                object_key=object_key,
            )
        if len(chunk) > remaining:
            raise IntegrityViolation(
                f"{label} stream exceeded its requested read bound",
                object_key=object_key,
            )
        payload.extend(chunk)
        remaining -= len(chunk)
    return bytes(payload)


class WorkerBlobStore:
    """Read and verify the exact prepared ObjectStore generation carried by outcome."""

    def __init__(self, object_store: object) -> None:
        self._object_store = object_store

    def read(self, object_ref: ObjectRef, location: ObjectLocation) -> bytes:
        stat = self._object_store.stat(location)  # type: ignore[attr-defined]
        if stat.ref != object_ref or stat.location != location:
            raise IntegrityViolation(
                "prepared blob generation differs from its exact ObjectRef",
                object_key=object_ref.key,
            )
        with self._object_store.open(location) as stream:  # type: ignore[attr-defined]
            payload = _read_bounded_stream(
                stream,
                max_bytes=object_ref.size_bytes + 1,
                label="prepared blob",
                object_key=object_ref.key,
            )
        if len(payload) != object_ref.size_bytes:
            raise IntegrityViolation(
                "prepared blob stream size differs from its exact ObjectRef",
                object_key=object_ref.key,
            )
        return payload


class WorkerBlobStager:
    """Materialize complete terminal drafts outside the database UoW.

    A partial upload failure intentionally leaves only verified, unbound objects for
    GC.  No receipt is returned until every material of every draft has been written
    and ``stat`` has reproduced the exact ref/generation pair.
    """

    def __init__(self, object_store: object) -> None:
        self._object_store = object_store

    def stage(
        self, drafts: tuple[TerminalPublicationDraft, ...]
    ) -> tuple[StagedTerminalPublication, ...]:
        staged: list[StagedTerminalPublication] = []
        staged_by_ref: dict[
            tuple[str, str, int],
            tuple[ObjectRef, ObjectLocation, ObjectStat],
        ] = {}
        for draft in drafts:
            slots: set[str] = set()
            receipts: list[StagedReceipt] = []
            for material in draft.materials:
                if material.slot in slots:
                    raise IntegrityViolation(
                        "terminal publication draft contains a duplicate blob slot",
                        slot=material.slot,
                    )
                slots.add(material.slot)
                ref_key = (
                    material.expected_ref.key,
                    material.expected_ref.sha256,
                    material.expected_ref.size_bytes,
                )
                retained = staged_by_ref.get(ref_key)
                if retained is None:
                    stored = self._object_store.put_verified(material.payload)  # type: ignore[attr-defined]
                    if stored.ref != material.expected_ref:
                        raise IntegrityViolation(
                            "ObjectStore staged a different content-addressed ref",
                            slot=material.slot,
                        )
                    stat = self._object_store.stat(stored.location)  # type: ignore[attr-defined]
                    if stat.ref != stored.ref or stat.location != stored.location:
                        raise IntegrityViolation(
                            "ObjectStore stat differs from the staged generation",
                            slot=material.slot,
                        )
                    retained = (stored.ref, stored.location, stat)
                    staged_by_ref[ref_key] = retained
                stored_ref, stored_location, stat = retained
                receipts.append(
                    StagedReceipt(
                        slot=material.slot,
                        ref=stored_ref,
                        location=stored_location,
                        verified_at=stat.verified_at,
                        generation_verification_token=(stat.generation_verification_token),
                    )
                )
            staged.append(
                StagedTerminalPublication(
                    projection_digest=draft.projection_digest,
                    receipts=tuple(receipts),
                )
            )
        return tuple(staged)


class WorkerArtifactPort:
    """The publisher's ``ArtifactPort`` that binds each blob before its row is written.

    ``SqlArtifactRepository.put`` requires an active ``ObjectBinding`` for an
    ``ArtifactV2``'s ``object_ref``. ``put_staged`` consumes the explicit receipt
    produced outside the UoW; it never guesses a new output generation from the
    process-local prepared-blob registry. An already-active binding is retained only
    when it names that exact staged generation; another generation is never silently
    remapped. A retired generation may be reactivated only by exact revision CAS.
    """

    def __init__(
        self,
        *,
        artifacts: object,
        object_bindings: object,
        object_store: object,
        allow_legacy_test_repositories: bool = False,
    ) -> None:
        self._artifacts = artifacts
        self._object_bindings = object_bindings
        self._object_store = object_store
        self._allow_legacy_test_repositories = allow_legacy_test_repositories
        self._preflight_bindings: dict[
            str, PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding
        ] = {}

    def get(self, artifact_id: str) -> object | None:
        return self._artifacts.get(artifact_id)  # type: ignore[attr-defined]

    def _current_transaction_identity(self) -> tuple[object, object] | None:
        """Return the shared SQL session/transaction capability when available."""

        retained: tuple[object, object] | None = None
        for repository in (self._artifacts, self._object_bindings):
            if not isinstance(
                repository,
                (SqlArtifactRepository, SqlObjectBindingRepository),
            ):
                # Production UnitOfWork capabilities are opaque lifecycle-bound
                # wrappers.  The WorkerArtifactPort instance itself is created per
                # transaction and is the owner identity; never pierce the wrapper
                # to read a non-callable private session.
                continue
            session = repository._session
            get_nested = getattr(session, "get_nested_transaction", None)
            get_transaction = getattr(session, "get_transaction", None)
            transaction = (get_nested() if callable(get_nested) else None) or (
                get_transaction() if callable(get_transaction) else None
            )
            if transaction is None:
                raise IntegrityViolation(
                    "terminal Artifact batch requires an active repository transaction"
                )
            identity = (session, transaction)
            if retained is None:
                retained = identity
            elif retained[0] is not session or retained[1] is not transaction:
                raise IntegrityViolation(
                    "Artifact and ObjectBinding repositories use different transactions"
                )
        return retained

    def preflight_binding(
        self, artifact: ArtifactV2
    ) -> PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding:
        """Fully verify a retained active generation outside the write UoW."""

        if artifact.object_ref.key in self._preflight_bindings:
            return self._preflight_bindings[artifact.object_ref.key]
        try:
            binding = self._object_bindings.resolve(  # type: ignore[attr-defined]
                artifact.object_ref,
            )
        except FileNotFoundError:
            absent = PreverifiedAbsentArtifactBinding(object_ref=artifact.object_ref)
            self._preflight_bindings[artifact.object_ref.key] = absent
            return absent
        stat = self._object_store.stat(binding.location)  # type: ignore[attr-defined]
        if (
            binding.object_ref != artifact.object_ref
            or binding.status != "active"
            or stat.ref != artifact.object_ref
            or stat.location != binding.location
        ):
            raise IntegrityViolation(
                "retained Artifact binding failed read-phase verification",
                artifact_id=artifact.artifact_id,
            )
        retained = PreverifiedArtifactBinding(binding=binding, stat=stat)
        self._preflight_bindings[artifact.object_ref.key] = retained
        return retained

    def _resolve_or_bind_receipt(self, receipt: StagedReceipt) -> object:
        """Bind a new generation or retain the exact already-active generation.

        ``ObjectBindingRepository.resolve`` deliberately hides retired rows.  Its
        frozen contract exposes the retained revision through the conflict raised by
        ``bind_preverified(..., expected_revision=None)``; use that revision exactly
        once to reactivate the staged generation.  ``bind_preverified``
        performs a lightweight exact-generation recheck under the DB writer boundary,
        closing the staging/GC race without rehashing payload bytes.
        """

        if receipt.verified_at is None:
            raise IntegrityViolation(
                "staged receipt lacks its preverified ObjectStore timestamp",
                slot=receipt.slot,
            )
        staged_stat = ObjectStat(
            ref=receipt.ref,
            location=receipt.location,
            verified_at=receipt.verified_at,
            generation_verification_token=receipt.generation_verification_token,
        )
        try:
            active = self._object_bindings.resolve(  # type: ignore[attr-defined]
                receipt.ref,
                store_id=receipt.location.store_id,
            )
        except FileNotFoundError:
            pass
        else:
            return self._retain_exact_staged_binding(
                active,
                staged_stat=staged_stat,
                slot=receipt.slot,
            )

        try:
            binding = self._object_bindings.bind_preverified(  # type: ignore[attr-defined]
                staged_stat,
                None,
            )
        except Conflict as conflict:
            context = conflict.context
            actual_revision = context.get("actual_revision")
            actual_status = context.get("actual_status")
            if (
                context.get("object_key") != receipt.ref.key
                or context.get("store_id") != receipt.location.store_id
                or not isinstance(actual_revision, int)
                or isinstance(actual_revision, bool)
                or actual_revision < 1
                or actual_status not in {"active", "retired"}
            ):
                raise IntegrityViolation(
                    "ObjectBinding conflict did not identify an exact retained revision",
                    slot=receipt.slot,
                    object_key=receipt.ref.key,
                    store_id=receipt.location.store_id,
                ) from conflict
            if actual_status == "active":
                try:
                    active = self._object_bindings.resolve(  # type: ignore[attr-defined]
                        receipt.ref,
                        store_id=receipt.location.store_id,
                    )
                except FileNotFoundError as exc:
                    raise TerminalAuthorityDrift(
                        "an active ObjectBinding won then disappeared during staging",
                        object_key=receipt.ref.key,
                        store_id=receipt.location.store_id,
                    ) from exc
                if active.revision != actual_revision:
                    raise TerminalAuthorityDrift(
                        "the active ObjectBinding changed during terminal staging",
                        object_key=receipt.ref.key,
                        store_id=receipt.location.store_id,
                    ) from conflict
                return self._retain_exact_staged_binding(
                    active,
                    staged_stat=staged_stat,
                    slot=receipt.slot,
                )
            binding = self._object_bindings.bind_preverified(  # type: ignore[attr-defined]
                staged_stat,
                actual_revision,
            )
        if binding.location != receipt.location:
            raise IntegrityViolation(
                "ObjectBinding repository returned another staged generation",
                slot=receipt.slot,
                object_key=receipt.ref.key,
            )
        return binding

    def _bind_planned_absent_receipt(self, receipt: StagedReceipt) -> object:
        """Bind only if the planning-phase absence proof is still current."""

        try:
            self._object_bindings.resolve(  # type: ignore[attr-defined]
                receipt.ref,
                store_id=receipt.location.store_id,
            )
        except FileNotFoundError:
            pass
        else:
            raise TerminalAuthorityDrift(
                "an active ObjectBinding appeared after terminal planning",
                object_key=receipt.ref.key,
                store_id=receipt.location.store_id,
            )
        return self._resolve_or_bind_receipt(receipt)

    def _retain_exact_staged_binding(
        self,
        active: object,
        *,
        staged_stat: ObjectStat,
        slot: str,
    ) -> object:
        """Accept idempotency only for the receipt's exact active generation."""

        if (
            getattr(active, "status", None) != "active"
            or getattr(active, "object_ref", None) != staged_stat.ref
        ):
            raise IntegrityViolation(
                "resolved ObjectBinding is not active and exact",
                slot=slot,
                object_key=staged_stat.ref.key,
            )
        if getattr(active, "location", None) != staged_stat.location:
            raise TerminalAuthorityDrift(
                "an active ObjectBinding points at another staged generation",
                object_key=staged_stat.ref.key,
                store_id=staged_stat.location.store_id,
            )
        revision = getattr(active, "revision", None)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise IntegrityViolation(
                "resolved ObjectBinding has no canonical revision",
                slot=slot,
                object_key=staged_stat.ref.key,
            )
        verified = self._object_bindings.bind_preverified(  # type: ignore[attr-defined]
            staged_stat,
            revision,
        )
        if verified != active:
            raise IntegrityViolation(
                "exact staged ObjectBinding verification changed its authority",
                slot=slot,
                object_key=staged_stat.ref.key,
            )
        return verified

    def _retain_preverified_binding(
        self,
        retained: PreverifiedArtifactBinding,
        *,
        artifact_id: str,
    ) -> object:
        try:
            current = self._object_bindings.resolve(  # type: ignore[attr-defined]
                retained.binding.object_ref,
                store_id=retained.binding.location.store_id,
            )
        except FileNotFoundError as exc:
            raise TerminalAuthorityDrift(
                "retained ObjectBinding disappeared after terminal planning",
                artifact_id=artifact_id,
            ) from exc
        if current != retained.binding:
            raise TerminalAuthorityDrift(
                "retained ObjectBinding changed after terminal planning",
                artifact_id=artifact_id,
            )
        verified = self._object_bindings.bind_preverified(  # type: ignore[attr-defined]
            retained.stat,
            retained.binding.revision,
        )
        if verified != retained.binding:
            raise IntegrityViolation(
                "retained ObjectBinding verification changed its authority",
                artifact_id=artifact_id,
            )
        return verified

    @staticmethod
    def _receipt_stat(receipt: StagedReceipt) -> ObjectStat:
        if receipt.verified_at is None:
            raise IntegrityViolation(
                "staged receipt lacks its preverified ObjectStore timestamp",
                slot=receipt.slot,
            )
        return ObjectStat(
            ref=receipt.ref,
            location=receipt.location,
            verified_at=receipt.verified_at,
            generation_verification_token=receipt.generation_verification_token,
        )

    def preflight_staged_many(
        self,
        writes: Sequence[
            tuple[
                ArtifactV2,
                StagedReceipt,
                PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding,
            ]
        ],
    ) -> _PreflightedArtifactBatch:
        """Check every binding/Artifact identity before the first write.

        This method is called after ``BEGIN IMMEDIATE`` but performs no mutation.
        SQLite therefore pins the checked rows for the following batch write while
        ObjectGc is excluded by the same writer boundary. The binding repository
        verifies every generation for the batch before issuing its first DML.
        """

        normalized = tuple(writes)
        refs: dict[str, ObjectRef] = {}
        artifacts_by_id: dict[str, ArtifactV2] = {}
        for artifact, receipt, planned in normalized:
            planned_ref = (
                planned.binding.object_ref
                if isinstance(planned, PreverifiedArtifactBinding)
                else planned.object_ref
            )
            if (
                not isinstance(artifact, ArtifactV2)
                or receipt.ref != artifact.object_ref
                or planned_ref != artifact.object_ref
            ):
                raise IntegrityViolation(
                    "terminal Artifact batch differs from its sealed receipt/binding plan",
                    artifact_id=getattr(artifact, "artifact_id", None),
                    slot=receipt.slot,
                )
            retained_ref = refs.setdefault(artifact.object_ref.key, artifact.object_ref)
            if retained_ref != artifact.object_ref:
                raise IntegrityViolation(
                    "terminal Artifact batch repeats an object key with another ref",
                    object_key=artifact.object_ref.key,
                )
            retained_artifact = artifacts_by_id.get(artifact.artifact_id)
            if retained_artifact is None:
                artifacts_by_id[artifact.artifact_id] = artifact
            elif retained_artifact is not artifact and not _same_immutable_artifact(
                retained_artifact,
                artifact,
            ):
                raise IntegrityViolation(
                    "terminal Artifact batch repeats an id with different content",
                    artifact_id=artifact.artifact_id,
                )

        preflight_repository_bindings = getattr(
            self._object_bindings,
            "preflight_terminal_preverified_many",
            None,
        )
        apply_repository_bindings = getattr(
            self._object_bindings,
            "apply_terminal_preverified_many",
            None,
        )
        preflight_repository_artifacts = getattr(
            self._artifacts,
            "preflight_put_many",
            None,
        )
        apply_repository_artifacts = getattr(
            self._artifacts,
            "put_preflighted_many",
            None,
        )
        repository_capabilities = (
            callable(preflight_repository_bindings),
            callable(apply_repository_bindings),
            callable(preflight_repository_artifacts),
            callable(apply_repository_artifacts),
        )
        if any(repository_capabilities) and not all(repository_capabilities):
            raise IntegrityViolation("terminal Artifact repository preflight capability is partial")
        if not all(repository_capabilities) and not self._allow_legacy_test_repositories:
            raise IntegrityViolation(
                "terminal Artifact repository preflight/apply capability is required"
            )

        repository_binding_preflight = None
        repository_artifact_preflight = None
        if all(repository_capabilities):
            unique_binding_writes: dict[str, tuple[ObjectStat, ObjectBinding | None]] = {}
            for artifact, receipt, planned in normalized:
                request = (
                    (planned.stat, planned.binding)
                    if isinstance(planned, PreverifiedArtifactBinding)
                    else (self._receipt_stat(receipt), None)
                )
                retained = unique_binding_writes.setdefault(
                    artifact.object_ref.key,
                    request,
                )
                if retained != request:
                    raise IntegrityViolation(
                        "terminal Artifact batch carries conflicting binding proofs",
                        object_key=artifact.object_ref.key,
                    )
            try:
                repository_binding_preflight = preflight_repository_bindings(  # type: ignore[operator]
                    tuple(unique_binding_writes.values())
                )
            except Conflict as exc:
                raise TerminalAuthorityDrift(
                    "terminal ObjectBinding authority changed during preflight"
                ) from exc
            repository_artifact_preflight = preflight_repository_artifacts(  # type: ignore[operator]
                tuple(artifact for artifact, _receipt, _planned in normalized),
                binding_preflight=repository_binding_preflight,
            )
        else:
            resolve_many = getattr(self._object_bindings, "resolve_many", None)
            if callable(resolve_many):
                active_by_key = resolve_many(tuple(refs.values()))
            else:
                active_by_key = {}
                for ref in refs.values():
                    try:
                        active_by_key[ref.key] = self._object_bindings.resolve(ref)
                    except FileNotFoundError:
                        active_by_key[ref.key] = None

            get_many = getattr(self._artifacts, "get_many", None)
            if callable(get_many):
                existing_by_id = get_many(tuple(artifacts_by_id))
            else:
                existing_by_id = {
                    artifact_id: self._artifacts.get(artifact_id) for artifact_id in artifacts_by_id
                }

            for artifact, receipt, planned in normalized:
                active = active_by_key.get(artifact.object_ref.key)
                if isinstance(planned, PreverifiedArtifactBinding):
                    if active != planned.binding:
                        raise TerminalAuthorityDrift(
                            "retained ObjectBinding changed after terminal planning",
                            artifact_id=artifact.artifact_id,
                        )
                else:
                    if active is not None:
                        raise TerminalAuthorityDrift(
                            "an active ObjectBinding appeared after terminal planning",
                            object_key=artifact.object_ref.key,
                        )
                    self._receipt_stat(receipt)

                existing = existing_by_id.get(artifact.artifact_id)
                if existing is not None and not _same_immutable_artifact(existing, artifact):
                    raise IntegrityViolation(
                        "Artifact id is already bound to different immutable content",
                        artifact_id=artifact.artifact_id,
                    )
                if existing is not None and isinstance(
                    planned,
                    PreverifiedAbsentArtifactBinding,
                ):
                    raise IntegrityViolation(
                        "retained Artifact has no read-phase active ObjectBinding",
                        artifact_id=artifact.artifact_id,
                        object_key=artifact.object_ref.key,
                    )

        return _issue_artifact_batch(
            _ArtifactBatchState(
                writes=deepcopy(normalized),
                owner=self,
                artifact_repository=self._artifacts,
                binding_repository=self._object_bindings,
                repository_binding_preflight=repository_binding_preflight,
                repository_artifact_preflight=repository_artifact_preflight,
                transaction_identity=self._current_transaction_identity(),
            )
        )

    def put_preflighted_many(
        self,
        batch: _PreflightedArtifactBatch,
    ) -> tuple[ArtifactV2, ...]:
        """Apply one preflighted Artifact aggregate with one repository flush."""

        if not isinstance(batch, _PreflightedArtifactBatch):
            raise IntegrityViolation("terminal Artifact batch lacks its trusted preflight seal")
        (
            writes,
            repository_binding_preflight,
            repository_artifact_preflight,
        ) = batch.consume(self)
        if (repository_binding_preflight is None) != (repository_artifact_preflight is None):
            raise IntegrityViolation(
                "terminal Artifact batch carries a partial repository preflight"
            )
        if repository_binding_preflight is not None:
            apply_bindings = getattr(
                self._object_bindings,
                "apply_terminal_preverified_many",
                None,
            )
            apply_artifacts = getattr(
                self._artifacts,
                "put_preflighted_many",
                None,
            )
            if not callable(apply_bindings) or not callable(apply_artifacts):
                raise IntegrityViolation(
                    "terminal Artifact repository lost its sealed apply capability"
                )
            try:
                apply_bindings(repository_binding_preflight)
            except Conflict as exc:
                raise TerminalAuthorityDrift(
                    "terminal ObjectBinding authority changed after preflight"
                ) from exc
            stored = tuple(apply_artifacts(repository_artifact_preflight))
            if len(stored) != len(writes):
                raise IntegrityViolation("Artifact repository returned another batch cardinality")
            return stored

        if not self._allow_legacy_test_repositories:
            raise IntegrityViolation(
                "terminal Artifact batch lacks its required repository preflight"
            )

        unique_binding_writes: dict[
            str, tuple[ObjectStat, ObjectBinding | None, str, StagedReceipt]
        ] = {}
        for artifact, receipt, planned in writes:
            if isinstance(planned, PreverifiedArtifactBinding):
                request = (
                    planned.stat,
                    planned.binding,
                    artifact.artifact_id,
                    receipt,
                )
            else:
                request = (
                    self._receipt_stat(receipt),
                    None,
                    artifact.artifact_id,
                    receipt,
                )
            retained = unique_binding_writes.setdefault(artifact.object_ref.key, request)
            if retained[:2] != request[:2]:
                raise IntegrityViolation(
                    "terminal Artifact batch carries conflicting binding proofs",
                    object_key=artifact.object_ref.key,
                )

        bind_many = getattr(self._object_bindings, "bind_terminal_preverified_many", None)
        if callable(bind_many):
            try:
                bindings = tuple(
                    bind_many(
                        tuple(
                            (stat, expected)
                            for stat, expected, _artifact_id, _receipt in (
                                unique_binding_writes.values()
                            )
                        )
                    )
                )
            except Conflict as exc:
                raise TerminalAuthorityDrift(
                    "terminal ObjectBinding authority changed after preflight"
                ) from exc
            if len(bindings) != len(unique_binding_writes):
                raise IntegrityViolation(
                    "ObjectBinding repository returned another batch cardinality"
                )
        else:
            for stat, expected, artifact_id, receipt in unique_binding_writes.values():
                if expected is None:
                    self._bind_planned_absent_receipt(receipt)
                else:
                    self._retain_preverified_binding(
                        PreverifiedArtifactBinding(binding=expected, stat=stat),
                        artifact_id=artifact_id,
                    )

        put_many = getattr(self._artifacts, "put_many", None)
        if callable(put_many):
            stored = tuple(put_many(tuple(item[0] for item in writes)))
        else:
            stored = tuple(self._artifacts.put(item[0]) for item in writes)
        if len(stored) != len(writes):
            raise IntegrityViolation("Artifact repository returned another batch cardinality")
        for retained, (expected, _receipt, _planned) in zip(
            stored,
            writes,
            strict=True,
        ):
            if not _same_immutable_artifact(retained, expected):
                raise IntegrityViolation(
                    "Artifact store returned a different immutable Artifact",
                    expected_artifact_id=expected.artifact_id,
                )
        return stored

    def put_staged(
        self,
        artifact: ArtifactV2,
        receipt: StagedReceipt,
        retained_binding: (
            PreverifiedArtifactBinding | PreverifiedAbsentArtifactBinding | None
        ) = None,
    ) -> ArtifactV2:
        """Bind one explicit verified receipt, then persist its immutable Artifact.

        The receipt location is never recovered from process-local key lookup. Its
        exact stat was produced by the immediately preceding staging phase; the DB
        UoW performs a generation-identity recheck plus binding/revision CAS but does
        not reopen or hash ObjectStore payload bytes.
        """

        if receipt.ref != artifact.object_ref:
            raise IntegrityViolation(
                "staged receipt ObjectRef differs from the Artifact",
                slot=receipt.slot,
                artifact_id=artifact.artifact_id,
            )
        existing_artifact = self._artifacts.get(artifact.artifact_id)  # type: ignore[attr-defined]
        if existing_artifact is not None:
            if not isinstance(existing_artifact, ArtifactV2) or canonical_json(
                existing_artifact.model_dump(mode="json", exclude={"created_at"})
            ) != canonical_json(artifact.model_dump(mode="json", exclude={"created_at"})):
                raise IntegrityViolation(
                    "Artifact id is already bound to different immutable content",
                    artifact_id=artifact.artifact_id,
                )
            binding = (
                self._resolve_or_bind_receipt(receipt)
                if retained_binding is None
                else self._bind_planned_absent_receipt(receipt)
                if isinstance(retained_binding, PreverifiedAbsentArtifactBinding)
                else self._retain_preverified_binding(
                    retained_binding,
                    artifact_id=artifact.artifact_id,
                )
            )
            if binding.status != "active" or binding.object_ref != artifact.object_ref:
                raise IntegrityViolation(
                    "retained Artifact ObjectBinding is not active and exact",
                    artifact_id=artifact.artifact_id,
                )
            # The newly staged location is a safe GC-eligible orphan.  Idempotent
            # publication must not remap an already-published immutable Artifact.
            return existing_artifact

        binding = (
            self._resolve_or_bind_receipt(receipt)
            if retained_binding is None
            else self._bind_planned_absent_receipt(receipt)
            if isinstance(retained_binding, PreverifiedAbsentArtifactBinding)
            else self._retain_preverified_binding(
                retained_binding,
                artifact_id=artifact.artifact_id,
            )
        )
        if binding.object_ref != receipt.ref or binding.status != "active":
            raise IntegrityViolation(
                "ObjectBinding repository returned another active binding",
                slot=receipt.slot,
                artifact_id=artifact.artifact_id,
            )
        return self._artifacts.put(artifact)  # type: ignore[attr-defined]

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self._artifacts.get(artifact_id)  # type: ignore[attr-defined]
        object_ref = getattr(artifact, "object_ref", None)
        if not isinstance(object_ref, ObjectRef):
            raise IntegrityViolation(
                "published runtime Artifact has no ObjectRef",
                artifact_id=artifact_id,
            )
        binding = self._object_bindings.resolve(object_ref)  # type: ignore[attr-defined]
        with self._object_store.open(binding.location) as stream:  # type: ignore[attr-defined]
            payload = _read_bounded_stream(
                stream,
                max_bytes=object_ref.size_bytes + 1,
                label="runtime Artifact",
                object_key=object_ref.key,
            )
        if len(payload) != object_ref.size_bytes or sha256_lowerhex(payload) != object_ref.sha256:
            raise IntegrityViolation(
                "runtime Artifact bytes differ from immutable ObjectRef",
                artifact_id=artifact_id,
            )
        return payload


class WorkerManifestLedger:
    """The publisher's ``ManifestLedger`` over ``SqlRunRepository``.

    All route/consumption/prompt/cassette pointers are re-read from DB and their
    Artifact bytes through exact active ObjectBindings; no process-local cassette
    cursor or deferred RECORD/REPLAY stub participates in terminal identity.
    """

    def __init__(
        self,
        runs: object,
        routing: object,
        *,
        artifacts: object | None = None,
        object_bindings: object | None = None,
        object_store: object | None = None,
    ) -> None:
        self._runs = runs
        self._routing = routing
        self._artifacts = artifacts
        self._object_bindings = object_bindings
        self._object_store = object_store
        self._terminal_authority_digests: dict[str, str] = {}
        self._terminal_attempt_authority_digests: dict[str, str] = {}
        self._runs_by_id: dict[str, RunRecord] = {}
        self._attempts_by_key: dict[tuple[str, int], RunAttempt | None] = {}
        self._prompt_links_by_run: dict[str, tuple[RunIntermediateArtifactLinkV1, ...]] = {}
        self._tool_links_by_run: dict[str, tuple[RunToolIntermediateLinkV1, ...]] = {}
        self._routes_by_run: dict[str, tuple[RunModelRouteLinkV1, ...]] = {}
        self._consumptions_by_run: dict[str, tuple[RunModelResponseConsumptionV1, ...]] = {}
        self._routes_by_key: dict[tuple[str, int, int, int], RunModelRouteLinkV1] = {}
        self._consumptions_by_key: dict[
            tuple[str, int, int, int], RunModelResponseConsumptionV1
        ] = {}
        self._closed_by_run: dict[str, tuple[tuple[int, str], ...]] = {}
        self._native_decisions: dict[str, RoutingDecisionV1 | None] = {}
        self._legacy_decisions: dict[str, object] = {}
        self._reservation_groups_by_attempt: dict[tuple[str, int], tuple[object, ...]] = {}

    @staticmethod
    def _project_authority_value(value: object) -> object:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        if isinstance(value, Mapping):
            return {
                str(key): WorkerManifestLedger._project_authority_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (tuple, list)):
            return tuple(WorkerManifestLedger._project_authority_value(item) for item in value)
        return value

    def _resolve_projection_dependencies(
        self,
        *,
        run_id: str,
        attempts: tuple[RunAttempt, ...],
        routes: tuple[RunModelRouteLinkV1, ...],
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        Mapping[str, object],
        Mapping[str, object],
        tuple[tuple[int, tuple[object, ...]], ...],
    ]:
        native_ids = tuple(
            dict.fromkeys(
                route.routing_decision_id
                for route in routes
                if route.routing_decision_kind == "native"
            )
        )
        legacy_ids = tuple(
            dict.fromkeys(
                route.routing_decision_id
                for route in routes
                if route.routing_decision_kind == "legacy_import"
            )
        )
        get_native = getattr(self._routing, "get_routing_decisions_many", None)
        get_legacy = getattr(self._routing, "get_legacy_import_routing_decisions_many", None)
        if native_ids and not callable(get_native):
            raise IntegrityViolation("terminal authority lacks native decision batch reads")
        if legacy_ids and not callable(get_legacy):
            raise IntegrityViolation("terminal authority lacks legacy decision batch reads")
        native = {} if not native_ids else get_native(native_ids)
        legacy = {} if not legacy_ids else get_legacy(legacy_ids)
        if any(native.get(decision_id) is None for decision_id in native_ids) or any(
            legacy.get(decision_id) is None for decision_id in legacy_ids
        ):
            raise IntegrityViolation("terminal runtime authority lost a routing decision")
        project_groups = getattr(self._routing, "terminal_attempt_reservation_groups", None)
        if attempts and not callable(project_groups):
            raise IntegrityViolation("terminal authority lacks attempt reservation projections")
        reservation_groups = (
            ()
            if not attempts
            else project_groups(
                run_id=run_id,
                attempt_nos=tuple(attempt.attempt_no for attempt in attempts),
                limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
            )
        )
        return native_ids, legacy_ids, native, legacy, tuple(reservation_groups)

    @classmethod
    def _projection_digest(
        cls,
        *,
        projection: object,
        native_ids: tuple[str, ...],
        legacy_ids: tuple[str, ...],
        native: Mapping[str, object],
        legacy: Mapping[str, object],
        reservation_groups: tuple[tuple[int, tuple[object, ...]], ...],
    ) -> str:
        project_value = cls._project_authority_value
        return canonical_sha256(
            {
                "run": project_value(projection.run),  # type: ignore[attr-defined]
                "attempts": project_value(projection.attempts),  # type: ignore[attr-defined]
                "prompt_links": project_value(projection.prompt_links),  # type: ignore[attr-defined]
                "tool_links": project_value(projection.tool_links),  # type: ignore[attr-defined]
                "model_routes": project_value(projection.model_routes),  # type: ignore[attr-defined]
                "model_consumptions": project_value(  # type: ignore[attr-defined]
                    projection.model_consumptions
                ),
                "closed_attempt_failures": projection.closed_attempt_failures,  # type: ignore[attr-defined]
                "native_decisions": tuple(
                    (decision_id, project_value(native[decision_id])) for decision_id in native_ids
                ),
                "legacy_decisions": tuple(
                    (decision_id, project_value(legacy[decision_id])) for decision_id in legacy_ids
                ),
                "attempt_reservation_groups": project_value(reservation_groups),
            }
        )

    def terminal_authority_digest(self, run_id: str) -> str:
        """Hash one bounded, DB-only terminal authority snapshot.

        Immutable Artifact bytes are fully verified while the read-phase plan is
        built.  The fresh ``BEGIN IMMEDIATE`` snapshot only needs to prove that the
        mutable rows which selected runtime parents/execution identity did not
        change.  This deliberately performs no ObjectStore access or payload
        decoding while the SQLite writer is held.
        """

        retained = self._terminal_authority_digests.get(run_id)
        if retained is not None:
            return retained
        project = getattr(self._runs, "terminal_authority_projection", None)
        if not callable(project):
            raise IntegrityViolation("terminal authority lacks bounded Run projection")
        projection = project(
            run_id,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
        )
        run = projection.run
        attempts = projection.attempts
        prompt_links = projection.prompt_links
        tool_links = projection.tool_links
        routes = projection.model_routes
        consumptions = projection.model_consumptions
        closed = projection.closed_attempt_failures

        native_ids, legacy_ids, native, legacy, reservation_groups = (
            self._resolve_projection_dependencies(
                run_id=run_id,
                attempts=attempts,
                routes=routes,
            )
        )
        digest = self._projection_digest(
            projection=projection,
            native_ids=native_ids,
            legacy_ids=legacy_ids,
            native=native,
            legacy=legacy,
            reservation_groups=reservation_groups,
        )
        self._runs_by_id[run_id] = run
        self._attempts_by_key.update(
            {(run_id, attempt.attempt_no): attempt for attempt in attempts}
        )
        self._prompt_links_by_run[run_id] = prompt_links
        self._tool_links_by_run[run_id] = tool_links
        self._routes_by_run[run_id] = routes
        self._routes_by_key.update(
            {
                (run_id, route.attempt_no, route.call_ordinal, route.route_ordinal): route
                for route in routes
            }
        )
        self._consumptions_by_run[run_id] = consumptions
        self._consumptions_by_key.update(
            {
                (run_id, item.attempt_no, item.call_ordinal, item.route_ordinal): item
                for item in consumptions
            }
        )
        self._closed_by_run[run_id] = closed
        self._native_decisions.update(native)
        self._legacy_decisions.update(legacy)
        self._reservation_groups_by_attempt.update(
            {(run_id, attempt_no): groups for attempt_no, groups in reservation_groups}
        )
        self._terminal_authority_digests[run_id] = digest
        return digest

    def terminal_attempt_authority_digest(self, run_id: str) -> str:
        """Bind a retry draft to current-attempt mutable authority only."""

        return self._attempt_authority_digest(run_id, retain=True)

    def fresh_terminal_attempt_authority_digest(self, run_id: str) -> str:
        """Re-read current-attempt authority inside the terminal write UoW."""

        return self._attempt_authority_digest(run_id, retain=False)

    def _attempt_authority_digest(self, run_id: str, *, retain: bool) -> str:
        if retain:
            retained = self._terminal_attempt_authority_digests.get(run_id)
            if retained is not None:
                return retained
        project = getattr(self._runs, "terminal_attempt_authority_projection", None)
        if callable(project):
            projection = project(
                run_id,
                limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
            )
        else:
            full_project = getattr(self._runs, "terminal_authority_projection", None)
            if not callable(full_project):
                raise IntegrityViolation("terminal authority lacks bounded Run projection")
            full = full_project(
                run_id,
                limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
            )
            attempt_no = full.run.current_attempt_no
            projection = SimpleNamespace(
                run=full.run,
                attempts=tuple(
                    attempt for attempt in full.attempts if attempt.attempt_no == attempt_no
                ),
                prompt_links=tuple(
                    link for link in full.prompt_links if link.attempt_no == attempt_no
                ),
                tool_links=tuple(link for link in full.tool_links if link.attempt_no == attempt_no),
                model_routes=tuple(
                    route for route in full.model_routes if route.attempt_no == attempt_no
                ),
                model_consumptions=tuple(
                    item for item in full.model_consumptions if item.attempt_no == attempt_no
                ),
                closed_attempt_failures=(),
            )
        attempts = tuple(projection.attempts)
        routes = tuple(projection.model_routes)
        native_ids, legacy_ids, native, legacy, reservation_groups = (
            self._resolve_projection_dependencies(
                run_id=run_id,
                attempts=attempts,
                routes=routes,
            )
        )
        digest = self._projection_digest(
            projection=projection,
            native_ids=native_ids,
            legacy_ids=legacy_ids,
            native=native,
            legacy=legacy,
            reservation_groups=reservation_groups,
        )
        if retain:
            self._runs_by_id[run_id] = projection.run
            self._attempts_by_key.update(
                {(run_id, attempt.attempt_no): attempt for attempt in attempts}
            )
            self._prompt_links_by_run[run_id] = tuple(projection.prompt_links)
            self._tool_links_by_run[run_id] = tuple(projection.tool_links)
            self._routes_by_run[run_id] = routes
            self._routes_by_key.update(
                {
                    (run_id, route.attempt_no, route.call_ordinal, route.route_ordinal): route
                    for route in routes
                }
            )
            consumptions = tuple(projection.model_consumptions)
            self._consumptions_by_run[run_id] = consumptions
            self._consumptions_by_key.update(
                {
                    (run_id, item.attempt_no, item.call_ordinal, item.route_ordinal): item
                    for item in consumptions
                }
            )
            self._native_decisions.update(native)
            self._legacy_decisions.update(legacy)
            self._reservation_groups_by_attempt.update(
                {(run_id, attempt_no): groups for attempt_no, groups in reservation_groups}
            )
            self._terminal_attempt_authority_digests[run_id] = digest
        return digest

    def prompt_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]:
        cached = self._prompt_links_by_run.get(run_id)
        if cached is not None:
            return (
                cached
                if attempt_no is None
                else tuple(link for link in cached if link.attempt_no == attempt_no)
            )
        result = self._runs.list_prompt_render_links(  # type: ignore[attr-defined]
            run_id,
            attempt_no=attempt_no,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
        )
        if attempt_no is None:
            self._prompt_links_by_run[run_id] = result
        return result  # type: ignore[no-any-return]

    def preflight_finding_links_many(
        self,
        links: Sequence[RunFindingLinkV1],
        *,
        planned_findings: Sequence[FindingRevisionV1] = (),
        planned_artifact_ids: Sequence[str] = (),
    ) -> object:
        preflight = getattr(self._runs, "preflight_finding_links_many", None)
        if not callable(preflight):
            raise IntegrityViolation("Run ledger lacks Finding-link batch preflight")
        return preflight(
            links,
            planned_findings=planned_findings,
            planned_artifact_ids=planned_artifact_ids,
        )

    def put_preflighted_finding_links_many(self, seal: object) -> tuple[RunFindingLinkV1, ...]:
        publish = getattr(self._runs, "put_preflighted_finding_links_many", None)
        if not callable(publish):
            raise IntegrityViolation("Run ledger lacks preflighted Finding-link publication")
        return tuple(publish(seal))

    def preflight_complete_attempt_success(self, **kwargs: object) -> object:
        return self._preflight_run_terminal("preflight_complete_attempt_success", kwargs)

    def preflight_close_attempt_for_retry(self, **kwargs: object) -> object:
        return self._preflight_run_terminal("preflight_close_attempt_for_retry", kwargs)

    def preflight_close_attempt_terminal(self, **kwargs: object) -> object:
        return self._preflight_run_terminal("preflight_close_attempt_terminal", kwargs)

    def preflight_terminate_inactive_run(self, **kwargs: object) -> object:
        return self._preflight_run_terminal("preflight_terminate_inactive_run", kwargs)

    def _preflight_run_terminal(
        self,
        operation: str,
        kwargs: Mapping[str, object],
    ) -> object:
        preflight = getattr(self._runs, operation, None)
        if not callable(preflight):
            raise IntegrityViolation("Run ledger lacks terminal closure preflight")
        return preflight(**kwargs)

    def apply_preflighted_terminal_closure(self, seal: object) -> object:
        apply = getattr(self._runs, "apply_preflighted_terminal_closure", None)
        if not callable(apply):
            raise IntegrityViolation("Run ledger lacks terminal closure apply")
        return apply(seal)

    def tool_intermediate_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunToolIntermediateLinkV1, ...]:
        cached = self._tool_links_by_run.get(run_id)
        if cached is not None:
            return (
                cached
                if attempt_no is None
                else tuple(link for link in cached if link.attempt_no == attempt_no)
            )
        result = self._runs.list_tool_intermediate_links(  # type: ignore[attr-defined]
            run_id,
            attempt_no=attempt_no,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
        )
        if attempt_no is None:
            self._tool_links_by_run[run_id] = result
        return result  # type: ignore[no-any-return]

    def closed_attempt_failures(self, run_id: str) -> tuple[tuple[int, str], ...]:
        cached = self._closed_by_run.get(run_id)
        if cached is not None:
            return cached
        result = self._runs.list_closed_attempt_failures(run_id)  # type: ignore[attr-defined]
        self._closed_by_run[run_id] = result
        return result  # type: ignore[no-any-return]

    def put_finding_link(self, link: RunFindingLinkV1) -> RunFindingLinkV1:
        return self._runs.put_finding_link(link)  # type: ignore[attr-defined,no-any-return]

    def execution_identity(self, run_id: str, *, attempt_no: int | None) -> ExecutionIdentityV1:
        run = self._runs_by_id.get(run_id) or self._runs.get(run_id)  # type: ignore[attr-defined]
        if not isinstance(run, RunRecord):
            raise IntegrityViolation("terminal execution identity Run is unavailable")
        if self._artifacts is None or self._object_bindings is None or self._object_store is None:
            raise IntegrityViolation("terminal execution identity blob authority is unavailable")
        transaction = SimpleNamespace(
            runs=self,
            cost=self,
            artifacts=self._artifacts,
            object_bindings=self._object_bindings,
        )
        return build_authoritative_execution_identity(
            transaction=transaction,
            object_store=self._object_store,
            run=run,
            attempt_no=attempt_no,
            scope="run" if attempt_no is None else "attempt",
        )

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        key = (run_id, attempt_no)
        if key in self._attempts_by_key:
            return self._attempts_by_key[key]
        attempt = self._runs.get_attempt(run_id, attempt_no)  # type: ignore[attr-defined]
        self._attempts_by_key[key] = attempt
        return attempt  # type: ignore[no-any-return]

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None:
        if decision_id in self._native_decisions:
            return self._native_decisions[decision_id]
        return self._routing.get_routing_decision(decision_id)  # type: ignore[attr-defined,no-any-return]

    def get_legacy_import_routing_decision(self, decision_id: str) -> object | None:
        if decision_id in self._legacy_decisions:
            return self._legacy_decisions[decision_id]
        return self._routing.get_legacy_import_routing_decision(  # type: ignore[attr-defined,no-any-return]
            decision_id
        )

    def list_attempt_reservation_groups(
        self,
        *,
        run_id: str,
        attempt_no: int,
    ) -> tuple[object, ...]:
        cached = self._reservation_groups_by_attempt.get((run_id, attempt_no))
        if cached is not None:
            return cached
        return self._routing.list_attempt_reservation_groups(  # type: ignore[attr-defined,no-any-return]
            run_id=run_id,
            attempt_no=attempt_no,
        )

    def get_model_route_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelRouteLinkV1 | None:
        cached = self._routes_by_run.get(run_id)
        if cached is not None:
            return self._routes_by_key.get((run_id, attempt_no, call_ordinal, route_ordinal))
        return self._runs.get_model_route_link(  # type: ignore[attr-defined,no-any-return]
            run_id,
            attempt_no,
            call_ordinal,
            route_ordinal,
        )

    def get_model_response_consumption(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelResponseConsumptionV1 | None:
        cached = self._consumptions_by_run.get(run_id)
        if cached is not None:
            return self._consumptions_by_key.get((run_id, attempt_no, call_ordinal, route_ordinal))
        return self._runs.get_model_response_consumption(  # type: ignore[attr-defined,no-any-return]
            run_id,
            attempt_no,
            call_ordinal,
            route_ordinal,
        )

    def model_route_links(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
    ) -> tuple[RunModelRouteLinkV1, ...]:
        cached = self._routes_by_run.get(run_id)
        if cached is not None:
            return (
                cached
                if attempt_no is None
                else tuple(route for route in cached if route.attempt_no == attempt_no)
            )
        result = self._runs.list_model_route_links(  # type: ignore[attr-defined]
            run_id,
            attempt_no=attempt_no,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
        )
        if attempt_no is None:
            self._routes_by_run[run_id] = result
            self._routes_by_key.update(
                {
                    (run_id, route.attempt_no, route.call_ordinal, route.route_ordinal): route
                    for route in result
                }
            )
        return result  # type: ignore[no-any-return]

    def list_model_route_links(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
    ) -> tuple[RunModelRouteLinkV1, ...]:
        return self.model_route_links(run_id, attempt_no=attempt_no)

    def model_response_consumptions(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
    ) -> tuple[RunModelResponseConsumptionV1, ...]:
        cached = self._consumptions_by_run.get(run_id)
        if cached is not None:
            return (
                cached
                if attempt_no is None
                else tuple(item for item in cached if item.attempt_no == attempt_no)
            )
        result = self._runs.list_model_response_consumptions(  # type: ignore[attr-defined]
            run_id,
            attempt_no=attempt_no,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
        )
        if attempt_no is None:
            self._consumptions_by_run[run_id] = result
            self._consumptions_by_key.update(
                {
                    (run_id, item.attempt_no, item.call_ordinal, item.route_ordinal): item
                    for item in result
                }
            )
        return result  # type: ignore[no-any-return]

    def list_model_response_consumptions(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
    ) -> tuple[RunModelResponseConsumptionV1, ...]:
        return self.model_response_consumptions(run_id, attempt_no=attempt_no)

    def record_shard_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[tuple[int, int, str], ...]:
        rows = tuple(
            (
                consumption.attempt_no,
                consumption.call_ordinal,
                consumption.cassette_shard_artifact_id,
            )
            for consumption in self.model_response_consumptions(
                run_id,
                attempt_no=attempt_no,
            )
            if consumption.cassette_shard_artifact_id is not None
        )
        keys = tuple((row[0], row[1]) for row in rows)
        if len(keys) != len(set(keys)):
            raise IntegrityViolation("logical model call has multiple RECORD shard authorities")
        return tuple(sorted(rows))

    def attempt_cassette_bundle(self, run_id: str, *, attempt_no: int) -> str | None:
        attempt = self.get_attempt(run_id, attempt_no)
        return None if attempt is None else attempt.cassette_bundle_artifact_id

    def run_cassette_bundle(self, run_id: str) -> str | None:
        run = self._runs_by_id.get(run_id) or self._runs.get(run_id)  # type: ignore[attr-defined]
        if run is None:
            return None
        if not isinstance(run, RunRecord):
            raise IntegrityViolation("RunStore returned an invalid RunRecord")
        return run.terminal_cassette_artifact_id

    def replay_input_cassette(self, run_id: str) -> str | None:
        run = self._runs_by_id.get(run_id) or self._runs.get(run_id)  # type: ignore[attr-defined]
        if run is None:
            return None
        if not isinstance(run, RunRecord):
            raise IntegrityViolation("RunStore returned an invalid RunRecord")
        return run.payload.cassette_artifact_id


class WorkerAuditPort:
    """The publisher's ``AuditPort`` over the platform ``AuditGate`` run-audit chain."""

    def __init__(self, *, audit_gate: AuditGate, chain_id: str) -> None:
        self._audit_gate = audit_gate
        self._chain_id = chain_id
        self._terminal_batch: PreflightedAuditBatch | None = None
        self._terminal_batch_applied = False
        self._deferred_records: tuple[
            tuple[
                str,
                str,
                AuditActor,
                str | None,
                AuditActor,
                str,
                str | None,
                str | None,
            ],
            ...,
        ] = ()
        self._deferred_index = 0

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
    ) -> None:
        if self._terminal_batch is not None:
            if not self._terminal_batch_applied:
                raise IntegrityViolation(
                    "terminal lifecycle Audit was consumed before its batch was applied"
                )
            if self._deferred_index >= len(self._deferred_records):
                raise IntegrityViolation(
                    "terminal lifecycle Audit is unexpected or was already consumed"
                )
            expected = self._deferred_records[self._deferred_index]
            actual = (
                action,
                run.run_id,
                run.initiated_by,
                artifact_id,
                actor,
                occurred_at,
                request_id,
                trace_id,
            )
            if actual != expected:
                raise IntegrityViolation(
                    "terminal lifecycle Audit differs from its preflighted record"
                )
            self._deferred_index += 1
            return

        del occurred_at  # AuditGate stamps the authoritative ts from its own clock.
        self._audit_gate.append(
            chain_id=self._chain_id,
            actor=actor,
            initiated_by=run.initiated_by,
            action=action,
            subject=AuditSubject(
                resource_kind="run",
                resource_id=run.run_id,
                artifact_id=artifact_id,
            ),
            correlation=AuditCorrelation(
                request_id=request_id,
                run_id=run.run_id,
                trace_id=trace_id,
            ),
        )

    def preflight_records(self, records: Sequence[object]) -> PreflightedAuditBatch:
        """Lock the chain head once and freeze all terminal records in input order."""

        if self._terminal_batch is not None:
            raise IntegrityViolation("terminal Audit batch was already preflighted")
        intents: list[AuditAppendIntent] = []
        deferred_records: list[
            tuple[
                str,
                str,
                AuditActor,
                str | None,
                AuditActor,
                str,
                str | None,
                str | None,
            ]
        ] = []
        saw_deferred = False
        for record in records:
            action = getattr(record, "action", None)
            run = getattr(record, "run", None)
            artifact_id = getattr(record, "artifact_id", None)
            actor = getattr(record, "actor", None)
            occurred_at = getattr(record, "occurred_at", None)
            deferred = getattr(record, "deferred", None)
            request_id = getattr(record, "request_id", None)
            trace_id = getattr(record, "trace_id", None)
            chain_id = getattr(record, "chain_id", None)
            append_intent = getattr(record, "append_intent", None)
            if (
                not isinstance(action, str)
                or not isinstance(run, RunRecord)
                or artifact_id is not None
                and not isinstance(artifact_id, str)
                or not isinstance(actor, AuditActor)
                or not isinstance(occurred_at, str)
                or not isinstance(deferred, bool)
                or request_id is not None
                and not isinstance(request_id, str)
                or trace_id is not None
                and not isinstance(trace_id, str)
                or chain_id is not None
                and not isinstance(chain_id, str)
                or append_intent is not None
                and not isinstance(append_intent, AuditAppendIntent)
            ):
                raise IntegrityViolation("terminal Audit intent is invalid")
            if saw_deferred and not deferred:
                raise IntegrityViolation("terminal publication Audit cannot follow lifecycle Audit")
            saw_deferred = saw_deferred or deferred
            if append_intent is None:
                intents.append(
                    AuditAppendIntent(
                        actor=actor,
                        initiated_by=run.initiated_by,
                        action=action,
                        subject=AuditSubject(
                            resource_kind="run",
                            resource_id=run.run_id,
                            artifact_id=artifact_id,
                        ),
                        correlation=AuditCorrelation(
                            request_id=request_id,
                            run_id=run.run_id,
                            trace_id=trace_id,
                        ),
                    )
                )
            else:
                if (
                    deferred
                    or chain_id != self._chain_id
                    or append_intent.action != action
                    or append_intent.actor != actor
                    or append_intent.initiated_by != run.initiated_by
                    or append_intent.subject.artifact_id != artifact_id
                    or append_intent.correlation
                    != AuditCorrelation(
                        request_id=request_id,
                        run_id=run.run_id,
                        trace_id=trace_id,
                    )
                ):
                    raise IntegrityViolation(
                        "merged workflow Audit intent differs from terminal authority"
                    )
                intents.append(append_intent)
            if deferred:
                deferred_records.append(
                    (
                        action,
                        run.run_id,
                        run.initiated_by,
                        artifact_id,
                        actor,
                        occurred_at,
                        request_id,
                        trace_id,
                    )
                )
        prepared = self._audit_gate.prepare_batch(
            chain_id=self._chain_id,
            intents=tuple(intents),
            require_batch=True,
        )
        self._terminal_batch = prepared
        self._deferred_records = tuple(deferred_records)

        def require_complete_terminal_audit() -> None:
            if not self._terminal_batch_applied:
                raise IntegrityViolation("terminal Audit batch was not applied before commit")
            if self._deferred_index != len(self._deferred_records):
                raise IntegrityViolation(
                    "terminal lifecycle Audit records were not completely consumed"
                )

        self._audit_gate.register_before_commit_guard(require_complete_terminal_audit)
        return prepared

    def apply_preflighted_records(self, prepared: object) -> None:
        if not isinstance(prepared, PreflightedAuditBatch):
            raise IntegrityViolation("terminal Audit batch was not authority-issued")
        if prepared is not self._terminal_batch or self._terminal_batch_applied:
            raise IntegrityViolation("terminal Audit batch is foreign or already applied")
        self._audit_gate.apply_prepared_batch(prepared)
        self._terminal_batch_applied = True


class WorkerCommandPublicationGateway:
    """Transaction-bound claim audit and canonical rendered-prompt publisher."""

    def __init__(
        self,
        *,
        audit_gate: AuditGate,
        chain_id: str,
        runs: object,
        artifacts: object,
        object_bindings: object,
        object_store: object,
        idempotency: object,
        prompt_materials: PromptRenderMaterialRegistry,
        context_materials: AgentPromptContextMaterialRegistry,
    ) -> None:
        self._audit_gate = audit_gate
        self._chain_id = chain_id
        self._runs = runs
        self._artifacts = artifacts
        self._artifact_port = WorkerArtifactPort(
            artifacts=artifacts,
            object_bindings=object_bindings,
            object_store=object_store,
        )
        self._idempotency = idempotency
        self._prompt_materials = prompt_materials
        self._context_materials = context_materials

    def record_run_created(
        self,
        *,
        run: RunRecord,
        event: RunEvent,
        request_id: str | None = None,
    ) -> None:
        del request_id
        self._append(action="run.queued", run=run, event=event, actor=run.initiated_by)

    def record_run_claimed(
        self,
        *,
        previous: RunRecord,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        del previous, attempt, lease
        self._append(action="run.attempt_leased", run=run, event=event, actor=actor)

    def record_run_terminal(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        event: RunEvent,
        actor: AuditActor,
        request_id: str | None = None,
    ) -> None:
        del attempt
        self._append(
            action="run.terminal",
            run=run,
            event=event,
            actor=actor,
            request_id=request_id,
        )

    def record_command_submitted(
        self,
        *,
        run: RunRecord,
        record: object,
        events: tuple[RunEvent, ...],
        actor: AuditActor,
        request_id: str | None = None,
    ) -> None:
        del record
        self._append(
            action="run.command_submitted",
            run=run,
            event=events[-1] if events else None,
            actor=actor,
            request_id=request_id,
        )

    def record_command_completed(
        self,
        *,
        run: RunRecord,
        record: object,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        del record
        self._append(action="run.command_completed", run=run, event=event, actor=actor)

    def get_agent_prompt_context_replay(
        self,
        *,
        idempotency_scope: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> RunToolIntermediateLinkV1 | None:
        material = self._context_materials.resolve(
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        replay = self._idempotency.get_result(  # type: ignore[attr-defined]
            scope=idempotency_scope,
            operation=_AGENT_PROMPT_CONTEXT_OPERATION,
            key=idempotency_key,
            request_hash=payload_hash,
        )
        if replay is None:
            return None
        if set(replay) != {"artifact_id", "link"}:
            raise IntegrityViolation("stored Agent prompt-context replay has invalid shape")
        try:
            link = RunToolIntermediateLinkV1.model_validate(replay["link"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("stored Agent prompt-context link is invalid") from exc
        if (
            replay["artifact_id"] != material.artifact.artifact_id
            or link.run_id != material.run_id
            or link.attempt_no != material.attempt_no
            or link.target_call_ordinal != material.target_call_ordinal
            or link.artifact_id != material.artifact.artifact_id
            or link.payload_hash != payload_hash
            or link.agent_node_id != material.context.agent_node_id
            or link.prompt_version != material.context.prompt_version
            or link.fencing_token != material.fence.fencing_token
        ):
            raise IntegrityViolation(
                "stored Agent prompt-context replay differs from staged material"
            )
        retained = self._artifacts.get(link.artifact_id)  # type: ignore[attr-defined]
        if not isinstance(retained, ArtifactV2) or (
            _immutable_artifact_wire(retained) != _immutable_artifact_wire(material.artifact)
        ):
            raise IntegrityViolation(
                "Agent prompt-context replay does not resolve its immutable Artifact"
            )
        return link

    def publish_agent_prompt_context(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        link: RunToolIntermediateLinkV1,
        idempotency_scope: str,
        idempotency_key: str,
        payload_hash: str,
        actor: AuditActor,
    ) -> RunToolIntermediateLinkV1:
        material = self._context_materials.resolve(
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )
        if run.cancel_requested_at is not None:
            raise AttemptFenceStateRejected(
                "cancel-requested Run cannot publish Agent prompt context"
            )
        context = material.context
        upstream_ids = tuple(sorted(item.artifact_id for item in context.upstream_artifacts))
        if (
            material.run_id != run.run_id
            or material.attempt_no != attempt.attempt_no
            or material.fence.expected_run_revision != run.revision
            or material.fence.fencing_token != attempt.fencing_token
            or link.run_id != material.run_id
            or link.attempt_no != material.attempt_no
            or link.target_call_ordinal != material.target_call_ordinal
            or link.artifact_id != material.artifact.artifact_id
            or link.payload_hash != payload_hash
            or link.agent_node_id != context.agent_node_id
            or link.prompt_version != context.prompt_version
            or link.fencing_token != material.fence.fencing_token
            or material.source_artifact_ids != upstream_ids
            or tuple(material.artifact.lineage) != upstream_ids
            or material.artifact.payload_hash != payload_hash
            or material.artifact.object_ref.sha256 != payload_hash
            or context.run_id != run.run_id
            or context.attempt_no != attempt.attempt_no
            or context.target_call_ordinal != link.target_call_ordinal
            or context.agent_node_id != link.agent_node_id
            or context.prompt_version != link.prompt_version
            or material.artifact.version_tuple
            != VersionTuple(
                doc_version=run.payload.version_tuple.doc_version,
                tool_version=_AGENT_PROMPT_CONTEXT_TOOL_VERSION,
            )
        ):
            raise IntegrityViolation("Agent prompt-context link differs from staged material")
        retained_artifact = self._artifact_port.put_staged(
            material.artifact,
            material.receipt,
        )
        if retained_artifact.artifact_id != link.artifact_id:
            raise IntegrityViolation("Agent prompt-context publisher returned another Artifact")
        retained_link = self._runs.put_tool_intermediate_link(link)  # type: ignore[attr-defined]
        if retained_link != link:
            raise IntegrityViolation("RunStore retained another tool intermediate link")
        response = {"artifact_id": link.artifact_id, "link": link.model_dump(mode="json")}
        retained_response = self._idempotency.put_result(  # type: ignore[attr-defined]
            scope=idempotency_scope,
            operation=_AGENT_PROMPT_CONTEXT_OPERATION,
            key=idempotency_key,
            request_hash=payload_hash,
            resource_kind="source_raw",
            resource_id=link.artifact_id,
            response=response,
        )
        if retained_response != response:
            raise IntegrityViolation(
                "Agent prompt-context idempotency authority retained another response"
            )
        self._audit_gate.append(
            chain_id=self._chain_id,
            actor=actor,
            initiated_by=run.initiated_by,
            action="run.agent_prompt_context_published",
            subject=AuditSubject(
                resource_kind="run",
                resource_id=run.run_id,
                artifact_id=link.artifact_id,
            ),
            correlation=AuditCorrelation(
                request_id=None,
                run_id=run.run_id,
                trace_id=attempt.trace_id,
            ),
        )
        return retained_link

    def get_prompt_replay(
        self,
        *,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> RunIntermediateArtifactLinkV1 | None:
        material = self._prompt_materials.resolve(
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        replay = self._idempotency.get_result(  # type: ignore[attr-defined]
            scope=idempotency_scope,
            operation=_PROMPT_RENDER_OPERATION,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is None:
            return None
        if set(replay) != {"artifact_id", "link"}:
            raise IntegrityViolation("stored prompt idempotency response has an invalid shape")
        try:
            link = RunIntermediateArtifactLinkV1.model_validate(replay["link"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("stored prompt idempotency link is invalid") from exc
        if (
            replay["artifact_id"] != material.artifact.artifact_id
            or link.artifact_id != material.artifact.artifact_id
            or link.run_id != material.run_id
            or link.attempt_no != material.attempt_no
            or link.call_ordinal != material.logical_call_ordinal
            or link.route_ordinal != material.route_ordinal
            or (material.call_ordinal is not None and link.call_ordinal != material.call_ordinal)
            or link.request_hash != request_hash
            or link.fencing_token != material.fence.fencing_token
        ):
            raise IntegrityViolation(
                "stored prompt idempotency response differs from staged immutable material"
            )
        retained = self._artifacts.get(link.artifact_id)  # type: ignore[attr-defined]
        if not isinstance(retained, ArtifactV2) or (
            _immutable_artifact_wire(retained) != _immutable_artifact_wire(material.artifact)
        ):
            raise IntegrityViolation(
                "prompt replay does not resolve its exact immutable Artifact",
                artifact_id=link.artifact_id,
            )
        return link

    def publish_prompt_rendered(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        link: RunIntermediateArtifactLinkV1,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
        actor: AuditActor,
    ) -> RunIntermediateArtifactLinkV1:
        material = self._prompt_materials.resolve(
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if run.cancel_requested_at is not None:
            raise AttemptFenceStateRejected("cancel-requested Run cannot publish a new prompt")
        if (
            material.run_id != run.run_id
            or material.attempt_no != attempt.attempt_no
            or material.fence.expected_run_revision != run.revision
            or material.fence.fencing_token != attempt.fencing_token
            or link.run_id != material.run_id
            or link.attempt_no != material.attempt_no
            or link.call_ordinal != material.logical_call_ordinal
            or link.route_ordinal != material.route_ordinal
            or (material.call_ordinal is not None and link.call_ordinal != material.call_ordinal)
            or link.artifact_id != material.artifact.artifact_id
            or link.request_hash != material.request_hash
            or link.fencing_token != material.fence.fencing_token
            or material.artifact.lineage != material.source_artifact_ids
        ):
            raise IntegrityViolation("prompt link differs from staged canonical material")
        self._validate_prompt_plan(run=run, material=material)

        stored_artifact = self._artifact_port.put_staged(
            material.artifact,
            material.receipt,
        )
        if stored_artifact.artifact_id != link.artifact_id:
            raise IntegrityViolation("prompt Artifact publisher returned another identity")
        stored_link = self._runs.put_intermediate_link(link)  # type: ignore[attr-defined]
        if stored_link != link:
            raise IntegrityViolation("RunStore retained another prompt link")
        response = {"artifact_id": link.artifact_id, "link": link.model_dump(mode="json")}
        retained_response = self._idempotency.put_result(  # type: ignore[attr-defined]
            scope=idempotency_scope,
            operation=_PROMPT_RENDER_OPERATION,
            key=idempotency_key,
            request_hash=request_hash,
            resource_kind="source_rendered",
            resource_id=link.artifact_id,
            response=response,
        )
        if retained_response != response:
            raise IntegrityViolation("prompt idempotency authority retained another response")
        self._audit_gate.append(
            chain_id=self._chain_id,
            actor=actor,
            initiated_by=run.initiated_by,
            action="run.prompt_rendered",
            subject=AuditSubject(
                resource_kind="run",
                resource_id=run.run_id,
                artifact_id=link.artifact_id,
            ),
            correlation=AuditCorrelation(
                request_id=None,
                run_id=run.run_id,
                trace_id=attempt.trace_id,
            ),
        )
        return stored_link

    @staticmethod
    def _validate_prompt_plan(*, run: RunRecord, material: PromptRenderMaterial) -> None:
        plan = run.payload.execution_version_plan
        request = material.model_request
        if plan is None or run.payload.llm_execution_mode == "not_applicable":
            raise IntegrityViolation("rendered prompt Run has no LLM execution plan")
        node = next(
            (item for item in plan.nodes if item.agent_node_id == request.agent_node_id),
            None,
        )
        model_id = canonical_model_snapshot_id(request.model_snapshot)
        if (
            node is None
            or node.prompt_version != request.prompt_version
            or model_id not in node.allowed_model_snapshots
            or not isinstance(material.artifact.version_tuple.doc_version, str)
            or not material.artifact.version_tuple.doc_version
            or material.artifact.version_tuple.prompt_version != request.prompt_version
            or material.artifact.version_tuple.model_snapshot is not None
            or material.artifact.version_tuple.agent_graph_version != plan.agent_graph_version
            or material.artifact.version_tuple.tool_version != material.renderer_version
            or material.artifact.meta.get("payload_schema_id") != "source-rendered@1"
            or material.artifact.meta.get("renderer_version") != material.renderer_version
            or material.artifact.meta.get("prompt_binding_id") != material.prompt_binding_id
            or material.artifact.meta.get("agent_tool_version") != node.tool_version
            or material.artifact.meta.get("producer_run_id") != run.run_id
            or material.artifact.meta.get("producer_attempt_no") != material.attempt_no
            or material.artifact.meta.get("logical_call_ordinal") != material.logical_call_ordinal
            or material.artifact.meta.get("route_ordinal") != material.route_ordinal
            or not isinstance(material.artifact.meta.get("request_configuration_digest"), str)
            or material.artifact.meta.get("sanitizer_version") != "canonical-model-request@1"
            or material.artifact.meta.get("replayability")
            != ("online_only" if run.payload.llm_execution_mode == "live" else "cassette_replay")
            or not isinstance(material.artifact.meta.get("provenance"), Mapping)
            or not isinstance(material.artifact.meta.get("prompt_parts"), list)
            or not material.artifact.meta.get("prompt_parts")
        ):
            raise IntegrityViolation("rendered prompt Artifact escapes the frozen execution plan")

    def _append(
        self,
        *,
        action: str,
        run: RunRecord,
        event: RunEvent | None,
        actor: AuditActor,
        request_id: str | None = None,
    ) -> None:
        self._audit_gate.append(
            chain_id=self._chain_id,
            actor=actor,
            initiated_by=run.initiated_by,
            action=action,
            subject=AuditSubject(resource_kind="run", resource_id=run.run_id),
            correlation=AuditCorrelation(
                request_id=request_id,
                run_id=run.run_id,
                trace_id=event.trace_id if event is not None else None,
            ),
        )


class WorkerCommandTerminalPublicationGateway:
    """Combine command audit hooks with the staged terminal engine.

    Claim/command/prompt surfaces remain owned by ``WorkerCommandPublicationGateway``;
    inactive cancellation's plan/commit/preflight surfaces delegate to the exact
    transaction-bound ``TerminalPublisher`` used by lifecycle publication.
    """

    def __init__(self, *, commands: WorkerCommandPublicationGateway, terminal: object) -> None:
        self._commands = commands
        self._terminal = terminal
        self._terminal_command_correlation: AuditCorrelation | None = None

    def record_run_created(self, **kwargs: object) -> None:
        self._commands.record_run_created(**kwargs)  # type: ignore[arg-type]

    def record_run_claimed(self, **kwargs: object) -> None:
        self._commands.record_run_claimed(**kwargs)  # type: ignore[arg-type]

    def record_command_submitted(self, **kwargs: object) -> None:
        events = kwargs.get("events")
        event = events[-1] if isinstance(events, tuple) and events else None
        correlation = self._terminal_command_correlation
        self._terminal.record_command_submitted(  # type: ignore[attr-defined]
            **kwargs,
            trace_id=(
                correlation.trace_id
                if correlation is not None
                else getattr(event, "trace_id", None)
            ),
        )

    def record_command_completed(self, **kwargs: object) -> None:
        self._commands.record_command_completed(**kwargs)  # type: ignore[arg-type]

    def record_run_terminal(self, **kwargs: object) -> None:
        event = kwargs.get("event")
        correlation = self._terminal_command_correlation
        self._terminal.record_run_terminal(  # type: ignore[attr-defined]
            **kwargs,
            trace_id=(
                correlation.trace_id
                if correlation is not None
                else getattr(event, "trace_id", None)
            ),
        )
        self._terminal_command_correlation = None

    def get_prompt_replay(self, **kwargs: object) -> object:
        return self._commands.get_prompt_replay(**kwargs)

    def get_agent_prompt_context_replay(self, **kwargs: object) -> object:
        return self._commands.get_agent_prompt_context_replay(**kwargs)

    def publish_agent_prompt_context(self, **kwargs: object) -> object:
        return self._commands.publish_agent_prompt_context(**kwargs)

    def publish_prompt_rendered(self, **kwargs: object) -> object:
        return self._commands.publish_prompt_rendered(**kwargs)

    def preflight_outcome(self, **kwargs: object) -> object:
        return self._terminal.preflight_outcome(**kwargs)  # type: ignore[attr-defined]

    def preflight_complete_attempt_success(self, **kwargs: object) -> object:
        return self._terminal.preflight_complete_attempt_success(  # type: ignore[attr-defined]
            **kwargs
        )

    def preflight_close_attempt_for_retry(self, **kwargs: object) -> object:
        return self._terminal.preflight_close_attempt_for_retry(  # type: ignore[attr-defined]
            **kwargs
        )

    def preflight_close_attempt_terminal(self, **kwargs: object) -> object:
        return self._terminal.preflight_close_attempt_terminal(  # type: ignore[attr-defined]
            **kwargs
        )

    def preflight_terminate_inactive_run(self, **kwargs: object) -> object:
        return self._terminal.preflight_terminate_inactive_run(  # type: ignore[attr-defined]
            **kwargs
        )

    def apply_preflighted_terminal_closure(self, seal: object) -> object:
        return self._terminal.apply_preflighted_terminal_closure(  # type: ignore[attr-defined]
            seal
        )

    def plan_run_failure(self, **kwargs: object) -> object:
        return self._terminal.plan_run_failure(**kwargs)  # type: ignore[attr-defined]

    def commit_planned_run_failure(self, draft: object, staged: object, **kwargs: object) -> object:
        correlation = kwargs.get("command_audit_correlation")
        if correlation is not None and not isinstance(correlation, AuditCorrelation):
            raise IntegrityViolation("terminal command Audit correlation is invalid")
        if correlation is not None and self._terminal_command_correlation is not None:
            raise IntegrityViolation("terminal command Audit correlation is already active")
        result = self._terminal.commit_planned_run_failure(  # type: ignore[attr-defined]
            draft,
            staged,
            **kwargs,
        )
        self._terminal_command_correlation = correlation
        return result


__all__ = [
    "AgentPromptContextMaterial",
    "AgentPromptContextMaterialRegistry",
    "FencedToolPromptSourceAuthority",
    "FrozenRunInputPromptSourceAuthority",
    "PromptRenderMaterial",
    "PromptRenderMaterialRegistry",
    "PromptSourceAuthorizationPort",
    "WorkerArtifactPort",
    "WorkerAuditPort",
    "WorkerBlobStager",
    "WorkerBlobStore",
    "WorkerCommandPublicationGateway",
    "WorkerCommandTerminalPublicationGateway",
    "WorkerManifestLedger",
    "WorkerAgentPromptContextPublisher",
    "WorkerPromptRenderPublisher",
]
