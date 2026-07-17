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
from types import SimpleNamespace
from collections.abc import Callable, Mapping
from typing import Protocol

from gameforge.apps.worker.execution_identity import build_authoritative_execution_identity
from gameforge.apps.worker.prompt_rendering import CanonicalPromptRendererAuthority
from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.errors import (
    AttemptFenceStateRejected,
    Conflict,
    IntegrityViolation,
)
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
    ObjectLocation,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
)
from gameforge.contracts.model_router import (
    ModelRequestV1,
    ModelRequestV2,
    parse_model_request,
    request_hash as model_request_hash,
)
from gameforge.contracts.routing import canonical_model_snapshot_id
from gameforge.contracts.storage import UtcClock
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.contracts.provenance import (
    OriginRefV1,
    ProvenanceTransformationV1,
    ProvenanceV1,
    TrustLevel,
    most_conservative_trust,
)
from gameforge.platform.audit.gate import AuditGate
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
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.platform.provenance.registry import build_source_kind_registry
from gameforge.platform.terminal_staging import (
    StagedReceipt,
    StagedTerminalPublication,
    TerminalPublicationDraft,
)


_PROMPT_RENDER_OPERATION = "worker.prompt-rendered@1"
_AGENT_PROMPT_CONTEXT_OPERATION = "worker.agent-prompt-context@1"
_AGENT_PROMPT_CONTEXT_TOOL_VERSION = "agent-prompt-context@1"


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
        route_link_loader: Callable[[str, int, int, int], RunModelRouteLinkV1 | None] | None = None,
        response_consumption_loader: Callable[
            [str, int, int, int], RunModelResponseConsumptionV1 | None
        ]
        | None = None,
    ) -> None:
        self._tool_link_loader = tool_link_loader
        self._artifact_loader = artifact_loader
        self._payload_loader = payload_loader
        self._route_link_loader = route_link_loader
        self._response_consumption_loader = response_consumption_loader

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
            if self._route_link_loader is None or self._response_consumption_loader is None:
                raise IntegrityViolation("prompt context prior authority is unavailable")
            route = self._route_link_loader(
                run.run_id,
                prior.attempt_no,
                prior.call_ordinal,
                prior.route_ordinal,
            )
            consumption = self._response_consumption_loader(
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
        stat = self._object_store.stat(stored.location)  # type: ignore[attr-defined]
        if stat.ref != stored.ref or stat.location != stored.location:
            raise IntegrityViolation("staged Agent prompt-context failed exact stat")
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
        stat = self._object_store.stat(stored.location)  # type: ignore[attr-defined]
        if stat.ref != stored.ref or stat.location != stored.location:
            raise IntegrityViolation("staged rendered prompt failed exact ObjectStore stat")
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
            return stream.read()


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
                receipts.append(
                    StagedReceipt(
                        slot=material.slot,
                        ref=stored.ref,
                        location=stored.location,
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
    process-local prepared-blob registry. Existing immutable Artifacts keep their
    retained active binding and leave a newly staged duplicate generation to GC.
    """

    def __init__(
        self,
        *,
        artifacts: object,
        object_bindings: object,
        object_store: object,
    ) -> None:
        self._artifacts = artifacts
        self._object_bindings = object_bindings
        self._object_store = object_store

    def get(self, artifact_id: str) -> object | None:
        return self._artifacts.get(artifact_id)  # type: ignore[attr-defined]

    def _resolve_or_bind_receipt(self, receipt: StagedReceipt) -> object:
        """Retain an active same-store binding or CAS-reactivate a retired row.

        ``ObjectBindingRepository.resolve`` deliberately hides retired rows.  Its
        frozen contract exposes the retained revision through the conflict raised by
        ``bind_verified(..., expected_revision=None)``; use that revision exactly
        once to reactivate the staged generation.  An active row won by a concurrent
        publisher is resolved and retained instead of being remapped.
        """

        try:
            return self._object_bindings.resolve(  # type: ignore[attr-defined]
                receipt.ref,
                store_id=receipt.location.store_id,
            )
        except FileNotFoundError:
            pass

        try:
            binding = self._object_bindings.bind_verified(  # type: ignore[attr-defined]
                receipt.ref,
                receipt.location,
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
                    return self._object_bindings.resolve(  # type: ignore[attr-defined]
                        receipt.ref,
                        store_id=receipt.location.store_id,
                    )
                except FileNotFoundError as exc:
                    raise Conflict(
                        "active ObjectBinding changed before it could be retained",
                        object_key=receipt.ref.key,
                        store_id=receipt.location.store_id,
                        expected_revision=actual_revision,
                    ) from exc
            binding = self._object_bindings.bind_verified(  # type: ignore[attr-defined]
                receipt.ref,
                receipt.location,
                actual_revision,
            )
        if binding.location != receipt.location:
            raise IntegrityViolation(
                "ObjectBinding repository returned another staged generation",
                slot=receipt.slot,
                object_key=receipt.ref.key,
            )
        return binding

    def put_staged(self, artifact: ArtifactV2, receipt: StagedReceipt) -> ArtifactV2:
        """Bind one explicit verified receipt, then persist its immutable Artifact.

        The receipt location is never recovered from the process-local key registry.
        ``stat`` is repeated inside the fresh write UoW so a substituted/deleted
        generation fails before either ObjectBinding or Artifact authority is written.
        """

        if receipt.ref != artifact.object_ref:
            raise IntegrityViolation(
                "staged receipt ObjectRef differs from the Artifact",
                slot=receipt.slot,
                artifact_id=artifact.artifact_id,
            )
        stat = self._object_store.stat(receipt.location)  # type: ignore[attr-defined]
        if stat.ref != receipt.ref or stat.location != receipt.location:
            raise IntegrityViolation(
                "staged receipt stat differs from its exact ref/generation",
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
            binding = self._resolve_or_bind_receipt(receipt)
            retained_stat = self._object_store.stat(binding.location)  # type: ignore[attr-defined]
            if (
                binding.status != "active"
                or binding.object_ref != artifact.object_ref
                or retained_stat.ref != artifact.object_ref
                or retained_stat.location != binding.location
            ):
                raise IntegrityViolation(
                    "retained Artifact ObjectBinding is not readable and exact",
                    artifact_id=artifact.artifact_id,
                )
            # The newly staged location is a safe GC-eligible orphan.  Idempotent
            # publication must not remap an already-published immutable Artifact.
            return existing_artifact

        binding = self._resolve_or_bind_receipt(receipt)
        retained_stat = self._object_store.stat(binding.location)  # type: ignore[attr-defined]
        if (
            binding.object_ref != receipt.ref
            or binding.status != "active"
            or retained_stat.ref != receipt.ref
            or retained_stat.location != binding.location
        ):
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
            payload = stream.read(object_ref.size_bytes + 1)
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

    def prompt_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]:
        return self._runs.list_prompt_render_links(  # type: ignore[attr-defined,no-any-return]
            run_id,
            attempt_no=attempt_no,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
        )

    def tool_intermediate_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunToolIntermediateLinkV1, ...]:
        return self._runs.list_tool_intermediate_links(  # type: ignore[attr-defined,no-any-return]
            run_id,
            attempt_no=attempt_no,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
        )

    def closed_attempt_failures(self, run_id: str) -> tuple[tuple[int, str], ...]:
        return self._runs.list_closed_attempt_failures(run_id)  # type: ignore[attr-defined]

    def put_finding_link(self, link: RunFindingLinkV1) -> RunFindingLinkV1:
        return self._runs.put_finding_link(link)  # type: ignore[attr-defined,no-any-return]

    def execution_identity(self, run_id: str, *, attempt_no: int | None) -> ExecutionIdentityV1:
        run = self._runs.get(run_id)  # type: ignore[attr-defined]
        if not isinstance(run, RunRecord):
            raise IntegrityViolation("terminal execution identity Run is unavailable")
        if self._artifacts is None or self._object_bindings is None or self._object_store is None:
            raise IntegrityViolation("terminal execution identity blob authority is unavailable")
        transaction = SimpleNamespace(
            runs=self._runs,
            cost=self._routing,
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
        return self._runs.get_attempt(run_id, attempt_no)  # type: ignore[attr-defined,no-any-return]

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None:
        return self._routing.get_routing_decision(decision_id)  # type: ignore[attr-defined,no-any-return]

    def get_model_route_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelRouteLinkV1 | None:
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
        return self._runs.list_model_route_links(  # type: ignore[attr-defined,no-any-return]
            run_id,
            attempt_no=attempt_no,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
        )

    def model_response_consumptions(
        self,
        run_id: str,
        *,
        attempt_no: int | None,
    ) -> tuple[RunModelResponseConsumptionV1, ...]:
        return self._runs.list_model_response_consumptions(  # type: ignore[attr-defined,no-any-return]
            run_id,
            attempt_no=attempt_no,
            limit=MAX_RUN_MANIFEST_PARENT_BINDINGS,
        )

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
        run = self._runs.get(run_id)  # type: ignore[attr-defined]
        if run is None:
            return None
        if not isinstance(run, RunRecord):
            raise IntegrityViolation("RunStore returned an invalid RunRecord")
        return run.terminal_cassette_artifact_id

    def replay_input_cassette(self, run_id: str) -> str | None:
        run = self._runs.get(run_id)  # type: ignore[attr-defined]
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

    def record(
        self,
        *,
        action: str,
        run: RunRecord,
        artifact_id: str | None,
        actor: AuditActor,
        occurred_at: str,
    ) -> None:
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
            correlation=AuditCorrelation(request_id=None, run_id=run.run_id, trace_id=None),
        )


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
        prompt_renderer: CanonicalPromptRendererAuthority,
        prompt_source_authority: PromptSourceAuthorizationPort | None = None,
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
        self._object_store = object_store
        self._idempotency = idempotency
        self._prompt_materials = prompt_materials
        self._context_materials = context_materials
        self._prompt_renderer = prompt_renderer
        self._prompt_source_authority = (
            prompt_source_authority or FrozenRunInputPromptSourceAuthority()
        )

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
        run = self._runs.get(link.run_id)  # type: ignore[attr-defined]
        attempt = self._runs.get_attempt(link.run_id, link.attempt_no)  # type: ignore[attr-defined]
        if not isinstance(run, RunRecord) or not isinstance(attempt, RunAttempt):
            raise IntegrityViolation("Agent prompt-context Run or attempt disappeared")
        if run.cancel_requested_at is not None:
            raise AttemptFenceStateRejected(
                "cancel-requested Run cannot publish Agent prompt context"
            )
        context = material.context
        try:
            validate_agent_prompt_context_kind(
                agent_node_id=context.agent_node_id,
                context_kind=context.context_kind,
                target_call_ordinal=context.target_call_ordinal,
                prior_consumption=context.prior_consumption,
            )
        except ValueError as exc:
            raise IntegrityViolation("Agent prompt-context kind is not authoritative") from exc
        upstream_ids = tuple(sorted(item.artifact_id for item in context.upstream_artifacts))
        if (
            link.run_id != material.run_id
            or link.attempt_no != material.attempt_no
            or link.target_call_ordinal != material.target_call_ordinal
            or link.artifact_id != material.artifact.artifact_id
            or link.payload_hash != payload_hash
            or link.agent_node_id != context.agent_node_id
            or link.prompt_version != context.prompt_version
            or link.fencing_token != material.fence.fencing_token
            or material.source_artifact_ids != upstream_ids
            or tuple(material.artifact.lineage) != upstream_ids
            or material.artifact.version_tuple
            != VersionTuple(
                doc_version=run.payload.version_tuple.doc_version,
                tool_version=_AGENT_PROMPT_CONTEXT_TOOL_VERSION,
            )
        ):
            raise IntegrityViolation("Agent prompt-context link differs from staged material")
        frozen = frozenset(run.payload.input_artifact_ids)
        source_binding_ids = tuple(
            item.artifact_id
            for item in context.upstream_artifacts
            if item.binding_key.startswith("source:")
        )
        if any(source_id not in frozen for source_id in source_binding_ids):
            raise IntegrityViolation("Agent prompt-context draft lineage escapes frozen Run inputs")
        trusts: list[TrustLevel] = []
        for binding in context.upstream_artifacts:
            source = self._artifacts.get(binding.artifact_id)  # type: ignore[attr-defined]
            if not isinstance(source, ArtifactV2) or (
                source.artifact_id != binding.artifact_id
                or source.kind != binding.artifact_kind
                or source.meta.get("payload_schema_id") != binding.payload_schema_id
                or source.payload_hash != binding.payload_hash
                or source.object_ref.sha256 != binding.payload_hash
            ):
                raise IntegrityViolation("Agent prompt-context upstream binding changed")
            raw_provenance = source.meta.get("provenance")
            if raw_provenance is None:
                trusts.append("untrusted_external")
            else:
                try:
                    source_provenance = ProvenanceV1.model_validate(raw_provenance)
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "Agent prompt-context upstream provenance is invalid"
                    ) from exc
                if (
                    source_provenance.source_hash != source.payload_hash
                    or source_provenance.parent_source_artifact_ids != tuple(source.lineage)
                ):
                    raise IntegrityViolation("Agent prompt-context upstream provenance changed")
                _require_registered_source_provenance(
                    source_provenance,
                    label="Agent prompt-context upstream provenance",
                )
                trusts.append(source_provenance.trust)
        try:
            provenance = ProvenanceV1.model_validate(material.artifact.meta.get("provenance"))
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Agent prompt-context provenance is invalid") from exc
        _require_registered_source_provenance(
            provenance,
            required_prompt_purposes=frozenset({"context", "tool_output"}),
            label="Agent prompt-context provenance",
        )
        if (
            provenance.source_kind_registry_version != 1
            or provenance.source_kind_id != "tool_output"
            or provenance.parent_source_artifact_ids != upstream_ids
            or provenance.source_hash != payload_hash
            or provenance.trust != most_conservative_trust(tuple(trusts))
        ):
            raise IntegrityViolation("Agent prompt-context provenance closure differs")
        prior = context.prior_consumption
        if prior is not None:
            prompt = self._runs.get_intermediate_link(  # type: ignore[attr-defined]
                run.run_id,
                prior.attempt_no,
                prior.call_ordinal,
                prior.route_ordinal,
            )
            route = self._runs.get_model_route_link(  # type: ignore[attr-defined]
                run.run_id,
                prior.attempt_no,
                prior.call_ordinal,
                prior.route_ordinal,
            )
            consumption = self._runs.get_model_response_consumption(  # type: ignore[attr-defined]
                run.run_id,
                prior.attempt_no,
                prior.call_ordinal,
                prior.route_ordinal,
            )
            if (
                prompt is None
                or route is None
                or consumption is None
                or prompt.artifact_id != prior.prompt_artifact_id
                or prompt.request_hash != prior.request_hash
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
                raise IntegrityViolation(
                    "Agent prompt-context prior response consumption is not authoritative"
                )
            by_key = {item.binding_key: item for item in context.upstream_artifacts}
            expected_prior_keys = {"prior.prompt"}
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
                    "Agent prompt-context prior cassette source differs from Run mode"
                )
            if expected_cassette_source is not None:
                expected_prior_keys.add("prior.cassette_source")
            actual_prior_keys = {key for key in by_key if not key.startswith("source:")}
            if (
                actual_prior_keys != expected_prior_keys
                or by_key["prior.prompt"].artifact_id != prior.prompt_artifact_id
                or (
                    expected_cassette_source is not None
                    and (
                        by_key["prior.cassette_source"].artifact_id != expected_cassette_source
                        or by_key["prior.cassette_source"].artifact_kind != "cassette_bundle"
                        or by_key["prior.cassette_source"].payload_schema_id
                        != ("cassette-bundle@1" if mode == "replay" else "cassette-record-shard@1")
                    )
                )
            ):
                raise IntegrityViolation(
                    "Agent prompt-context prior Artifact lineage is incomplete"
                )
        elif any(not item.binding_key.startswith("source:") for item in context.upstream_artifacts):
            raise IntegrityViolation(
                "Agent prompt-context without prior consumption has prior parents"
            )
        try:
            with self._object_store.open(material.receipt.location) as stream:  # type: ignore[attr-defined]
                staged_payload = stream.read(material.artifact.object_ref.size_bytes + 1)
            parsed = AgentPromptContextV1.model_validate(json.loads(staged_payload))
            validate_agent_prompt_context_kind(
                agent_node_id=parsed.agent_node_id,
                context_kind=parsed.context_kind,
                target_call_ordinal=parsed.target_call_ordinal,
                prior_consumption=parsed.prior_consumption,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("staged Agent prompt-context payload is invalid") from exc
        canonical = canonical_json(parsed.model_dump(mode="json")).encode("utf-8")
        if (
            parsed != context
            or staged_payload != canonical
            or len(staged_payload) != material.artifact.object_ref.size_bytes
            or sha256_lowerhex(staged_payload) != payload_hash
        ):
            raise IntegrityViolation("staged Agent prompt-context bytes differ")
        validate_artifact_producer(
            material.artifact,
            ProducerValidationContext(
                expected_versions={
                    "doc_version": run.payload.version_tuple.doc_version,
                },
                llm_execution_mode=run.payload.llm_execution_mode,
                tool_output=True,
            ),
        )
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
        run = self._runs.get(link.run_id)  # type: ignore[attr-defined]
        attempt = self._runs.get_attempt(link.run_id, link.attempt_no)  # type: ignore[attr-defined]
        if not isinstance(run, RunRecord) or not isinstance(attempt, RunAttempt):
            raise IntegrityViolation("prompt publication Run or attempt disappeared")
        if run.cancel_requested_at is not None:
            raise AttemptFenceStateRejected("cancel-requested Run cannot publish a new prompt")
        self._prompt_source_authority.require_authorized(
            run=run,
            fence=material.fence,
            source_artifact_ids=material.source_artifact_ids,
            agent_node_id=material.model_request.agent_node_id,
            prompt_version=material.model_request.prompt_version,
            target_call_ordinal=material.logical_call_ordinal,
        )
        if (
            link.run_id != material.run_id
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
        source_artifacts: list[ArtifactV2] = []
        for source_artifact_id in material.source_artifact_ids:
            source_artifact = self._artifacts.get(source_artifact_id)  # type: ignore[attr-defined]
            if not isinstance(source_artifact, ArtifactV2):
                raise IntegrityViolation(
                    "rendered prompt references an unavailable source parent",
                    parent_artifact_id=source_artifact_id,
                )
            source_artifacts.append(source_artifact)
        self._prompt_renderer.require_source_metadata_bounds(
            agent_node_id=material.model_request.agent_node_id,
            prompt_version=material.model_request.prompt_version,
            source_artifacts=tuple(source_artifacts),
        )
        sources: list[tuple[ArtifactV2, bytes]] = []
        for source_artifact in source_artifacts:
            source_payload = self._artifact_port.read_bytes(source_artifact.artifact_id)
            if sha256_lowerhex(source_payload) != source_artifact.payload_hash:
                raise IntegrityViolation(
                    "rendered prompt source bytes differ from its Artifact",
                    parent_artifact_id=source_artifact.artifact_id,
                )
            sources.append((source_artifact, source_payload))
        canonical_render = self._prompt_renderer.require_model_request(
            model_request=material.model_request,
            sources=tuple(sources),
        )
        expected_provenance = canonical_render.provenance_for_output(
            material.artifact.payload_hash
        ).model_dump(mode="json")
        if (
            material.prompt_binding_id != canonical_render.binding_id
            or material.renderer_version != canonical_render.renderer_version
            or material.artifact.meta.get("prompt_binding_id") != canonical_render.binding_id
            or material.artifact.meta.get("renderer_version") != canonical_render.renderer_version
            or material.artifact.meta.get("agent_tool_version")
            != canonical_render.agent_tool_version
            or material.artifact.meta.get("request_configuration_digest")
            != canonical_render.request_configuration_digest
            or material.artifact.meta.get("producer_run_id") != run.run_id
            or material.artifact.meta.get("producer_attempt_no") != attempt.attempt_no
            or material.artifact.meta.get("logical_call_ordinal") != link.call_ordinal
            or material.artifact.meta.get("route_ordinal") != link.route_ordinal
            or material.artifact.meta.get("provenance") != expected_provenance
            or material.artifact.meta.get("prompt_parts")
            != [part.model_dump(mode="json") for part in canonical_render.prompt_parts]
        ):
            raise IntegrityViolation("rendered prompt PromptPart provenance differs")
        self._validate_prompt_plan(run=run, material=material)
        plan = run.payload.execution_version_plan
        if plan is None:  # closed by _validate_prompt_plan; keep the type boundary exact
            raise IntegrityViolation("rendered prompt Run plan disappeared")
        node = next(
            (
                item
                for item in plan.nodes
                if item.agent_node_id == material.model_request.agent_node_id
            ),
            None,
        )
        if node is None or canonical_render.agent_tool_version != node.tool_version:
            raise IntegrityViolation(
                "prompt tool schema authority differs from the frozen execution plan"
            )
        validate_artifact_producer(
            material.artifact,
            ProducerValidationContext(
                expected_versions={
                    "doc_version": canonical_render.inherited_doc_version,
                    "prompt_version": material.model_request.prompt_version,
                    "agent_graph_version": plan.agent_graph_version,
                },
                llm_execution_mode=run.payload.llm_execution_mode,
                rendered_prompt_evidence=True,
            ),
        )
        try:
            with self._object_store.open(material.receipt.location) as stream:  # type: ignore[attr-defined]
                payload = stream.read()
            decoded = json.loads(payload)
            if not isinstance(decoded, Mapping):
                raise ValueError("rendered request must be an object")
            parsed = parse_model_request(decoded)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("staged rendered prompt payload is invalid") from exc
        canonical = canonical_json(parsed.model_dump(mode="json")).encode("utf-8")
        if (
            parsed != material.model_request
            or payload != canonical
            or model_request_hash(parsed).removeprefix("sha256:") != request_hash
        ):
            raise IntegrityViolation("staged rendered prompt differs from its canonical request")

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

    def publish_run_failure(self, **_: object) -> None:
        raise IntegrityViolation(
            "run-failure publication flows through the lifecycle terminal publisher"
        )

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

    def record_run_created(self, **kwargs: object) -> None:
        self._commands.record_run_created(**kwargs)  # type: ignore[arg-type]

    def record_run_claimed(self, **kwargs: object) -> None:
        self._commands.record_run_claimed(**kwargs)  # type: ignore[arg-type]

    def record_command_submitted(self, **kwargs: object) -> None:
        self._commands.record_command_submitted(**kwargs)  # type: ignore[arg-type]

    def record_command_completed(self, **kwargs: object) -> None:
        self._commands.record_command_completed(**kwargs)  # type: ignore[arg-type]

    def record_run_terminal(self, **kwargs: object) -> None:
        self._commands.record_run_terminal(**kwargs)  # type: ignore[arg-type]

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

    def plan_run_failure(self, **kwargs: object) -> object:
        return self._terminal.plan_run_failure(**kwargs)  # type: ignore[attr-defined]

    def publish_run_failure(self, **kwargs: object) -> object:
        # This direct surface is deliberately fail-closed in TerminalPublisher;
        # staged command composition calls plan -> external stage -> commit.
        return self._terminal.publish_run_failure(**kwargs)  # type: ignore[attr-defined]

    def commit(self, fresh_draft: object, staged: object) -> object:
        return self._terminal.commit(fresh_draft, staged)  # type: ignore[attr-defined]

    def commit_many(self, publications: object) -> object:
        return self._terminal.commit_many(publications)  # type: ignore[attr-defined]


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
