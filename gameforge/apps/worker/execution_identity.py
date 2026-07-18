"""Rebuild model execution identity from immutable platform authorities."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cassette_import import LegacyImportRoutingDecisionV1
from gameforge.contracts.cost import ReservationGroupV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    RunModelResponseConsumptionV1,
    RunModelRouteLinkV1,
    RunRecord,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    ExecutionIdentityV1,
    InvocationVersionBindingV1,
    ObjectBinding,
    ObjectRef,
    build_execution_identity,
)
from gameforge.contracts.model_router import (
    ModelRequestV1,
    ModelRequestV2,
    parse_model_request,
    request_hash,
)
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.runtime.persistence.runs import ModelCallWriteAuthority


@dataclass(frozen=True, slots=True)
class PendingResponseConsumption:
    """The final route being committed in the caller's current write UoW."""

    call_ordinal: int
    route_ordinal: int
    execution_source: str
    transport_attempt: int | None


@dataclass(frozen=True, slots=True)
class RenderedRequestAuthority:
    """Blob-verified prompt material prepared before the write transaction."""

    route: RunModelRouteLinkV1
    artifact: ArtifactV2
    rendered: ModelRequestV1 | ModelRequestV2


def prepare_rendered_request_authority(
    *,
    transaction: object,
    object_store: object,
    route: RunModelRouteLinkV1,
) -> RenderedRequestAuthority:
    """Read and hash one rendered prompt outside the SQLite writer boundary."""

    artifact = transaction.artifacts.get(route.prompt_artifact_id)  # type: ignore[attr-defined]
    if (
        not isinstance(artifact, ArtifactV2)
        or artifact.kind != "source_rendered"
        or artifact.meta.get("payload_schema_id") != "source-rendered@1"
        or not isinstance(artifact.object_ref, ObjectRef)
    ):
        raise IntegrityViolation("model route prompt is not a published source_rendered")
    binding = transaction.object_bindings.resolve(artifact.object_ref)  # type: ignore[attr-defined]
    if (
        not isinstance(binding, ObjectBinding)
        or binding.object_ref != artifact.object_ref
        or binding.status != "active"
    ):
        raise IntegrityViolation("model route prompt has no active ObjectBinding")
    try:
        with object_store.open(binding.location) as stream:  # type: ignore[attr-defined]
            payload = stream.read()
        decoded = json.loads(payload)
        if not isinstance(decoded, Mapping):
            raise ValueError("rendered request must be an object")
        rendered = parse_model_request(decoded)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IntegrityViolation("model route rendered request is unreadable") from exc
    canonical = canonical_json(rendered.model_dump(mode="json")).encode("utf-8")
    if (
        payload != canonical
        or artifact.payload_hash != artifact.object_ref.sha256
        or request_hash(rendered).removeprefix("sha256:") != route.request_hash
    ):
        raise IntegrityViolation("model route rendered request differs from its authority")
    return RenderedRequestAuthority(
        route=route,
        artifact=artifact,
        rendered=rendered,
    )


def _route_decision(
    *,
    transaction: object,
    route: RunModelRouteLinkV1,
) -> RoutingDecisionV1 | LegacyImportRoutingDecisionV1:
    if route.routing_decision_kind == "native":
        decision = transaction.cost.get_routing_decision(  # type: ignore[attr-defined]
            route.routing_decision_id
        )
    else:
        decision = transaction.cost.get_legacy_import_routing_decision(  # type: ignore[attr-defined]
            route.routing_decision_id
        )
    if not isinstance(decision, (RoutingDecisionV1, LegacyImportRoutingDecisionV1)):
        raise IntegrityViolation("model route lost its exact routing decision")
    if (
        decision.decision_id != route.routing_decision_id
        or decision.request_hash != f"sha256:{route.request_hash}"
    ):
        raise IntegrityViolation("model route differs from its exact routing decision")
    return decision


def _reservation_route_key(
    group: ReservationGroupV1,
) -> tuple[tuple[int, str, str, int, int], int] | None:
    """Parse the frozen attempt-call idempotency authority exactly once."""

    if group.scope != "attempt_call" or group.status == "released":
        return None
    if group.attempt_no is None or group.request_hash is None or group.transport_attempt is None:
        raise IntegrityViolation("attempt-call reservation omits its route authority")
    prefix = "model-route:"
    marker = ":call:"
    if not group.idempotency_key.startswith(prefix) or marker not in group.idempotency_key:
        raise IntegrityViolation("attempt-call reservation idempotency is malformed")
    decision_id, suffix = group.idempotency_key[len(prefix) :].split(marker, 1)
    parts = suffix.split(":")
    if not decision_id or len(parts) != 5 or parts[1] != "route" or parts[3] != "transport":
        raise IntegrityViolation("attempt-call reservation idempotency is malformed")
    try:
        call_ordinal = int(parts[0])
        route_ordinal = int(parts[2])
        transport_attempt = int(parts[4])
    except ValueError as exc:
        raise IntegrityViolation("attempt-call reservation idempotency is malformed") from exc
    if (
        call_ordinal < 1
        or route_ordinal < 1
        or transport_attempt < 1
        or transport_attempt != group.transport_attempt
    ):
        raise IntegrityViolation("attempt-call reservation idempotency is inconsistent")
    return (
        (
            group.attempt_no,
            group.request_hash,
            decision_id,
            call_ordinal,
            route_ordinal,
        ),
        transport_attempt,
    )


def _failed_route_transport_attempts(
    *,
    transaction: object,
    routes: tuple[RunModelRouteLinkV1, ...],
) -> dict[tuple[int, str, str, int, int], int]:
    """Index reservation evidence once instead of rescanning it for every route."""

    attempts_by_route: dict[tuple[int, str, str, int, int], int] = {}
    for attempt_no in dict.fromkeys(route.attempt_no for route in routes):
        groups = transaction.cost.list_attempt_reservation_groups(  # type: ignore[attr-defined]
            run_id=routes[0].run_id,
            attempt_no=attempt_no,
        )
        for group in groups:
            if not isinstance(group, ReservationGroupV1):
                raise IntegrityViolation("attempt reservation authority is invalid")
            parsed = _reservation_route_key(group)
            if parsed is None:
                continue
            key, transport_attempt = parsed
            attempts_by_route[key] = max(attempts_by_route.get(key, 0), transport_attempt)
    return attempts_by_route


def build_authoritative_execution_identity(
    *,
    transaction: object,
    object_store: object,
    run: RunRecord,
    attempt_no: int | None,
    scope: str,
    pending: PendingResponseConsumption | None = None,
    call_authority: ModelCallWriteAuthority | None = None,
    rendered_request_authority: RenderedRequestAuthority | None = None,
) -> ExecutionIdentityV1:
    """LEFT JOIN routes to consumption and rebuild every invocation binding.

    ``pending`` is used only while atomically inserting a response consumption: the
    final route is treated as consumed for the staged RECORD shard even though the
    consumption row intentionally does not exist until that same write UoW commits.
    """

    if scope not in {"record_shard", "attempt", "run"}:
        raise IntegrityViolation("worker execution identity scope is unsupported")
    plan = run.payload.execution_version_plan
    if plan is None or run.payload.llm_execution_mode == "not_applicable":
        raise IntegrityViolation("model execution identity requires a frozen plan")
    if scope == "record_shard":
        if attempt_no is None or pending is None or call_authority is None:
            raise IntegrityViolation("record-shard identity requires one pending call")
        prompt_links = call_authority.prompt_links
        route_links = call_authority.route_links
        if (
            call_authority.run != run
            or call_authority.attempt.run_id != run.run_id
            or call_authority.attempt.attempt_no != attempt_no
            or call_authority.consumption is not None
            or len(prompt_links) != pending.route_ordinal
            or len(route_links) != pending.route_ordinal
        ):
            raise IntegrityViolation("record-shard logical-call authority is incomplete")
        prompt = prompt_links[-1]
        route = route_links[-1]
        if (
            prompt.call_ordinal != pending.call_ordinal
            or prompt.route_ordinal != pending.route_ordinal
            or route.call_ordinal != pending.call_ordinal
            or route.route_ordinal != pending.route_ordinal
            or route.prompt_artifact_id != prompt.artifact_id
            or route.request_hash != prompt.request_hash
        ):
            raise IntegrityViolation("pending response is not the final committed route")
        routes = (route,)
        consumptions: dict[tuple[int, int, int], RunModelResponseConsumptionV1] = {}
        failed_transport_attempts: dict[tuple[int, str, str, int, int], int] = {}
    else:
        if call_authority is not None or rendered_request_authority is not None:
            raise IntegrityViolation("aggregate identity received logical-call authority")
        routes = transaction.runs.list_model_route_links(  # type: ignore[attr-defined]
            run.run_id,
            attempt_no=attempt_no,
        )
        consumptions = {
            (item.attempt_no, item.call_ordinal, item.route_ordinal): item
            for item in transaction.runs.list_model_response_consumptions(  # type: ignore[attr-defined]
                run.run_id,
                attempt_no=attempt_no,
            )
        }
        failed_transport_attempts = _failed_route_transport_attempts(
            transaction=transaction,
            routes=routes,
        )
    if pending is not None:
        pending_key = (attempt_no, pending.call_ordinal, pending.route_ordinal)
        if pending_key in consumptions:
            raise IntegrityViolation("pending response route was already consumed")
    nodes_by_id = {node.agent_node_id: node for node in plan.nodes}
    if len(nodes_by_id) != len(plan.nodes):
        raise IntegrityViolation("frozen execution plan repeats an Agent node")

    bindings: list[InvocationVersionBindingV1] = []
    pending_seen = False
    for route in routes:
        if rendered_request_authority is None:
            prepared_rendered = prepare_rendered_request_authority(
                transaction=transaction,
                object_store=object_store,
                route=route,
            )
            artifact, rendered = prepared_rendered.artifact, prepared_rendered.rendered
        else:
            if rendered_request_authority.route != route:
                raise IntegrityViolation("prepared rendered request belongs to another route")
            artifact = rendered_request_authority.artifact
            rendered = rendered_request_authority.rendered
        node = nodes_by_id.get(rendered.agent_node_id)
        rendered_model = canonical_model_snapshot_id(rendered.model_snapshot)
        renderer_version = artifact.meta.get("renderer_version")
        agent_tool_version = artifact.meta.get("agent_tool_version")
        if (
            node is None
            or rendered.prompt_version != node.prompt_version
            or rendered_model not in node.allowed_model_snapshots
            or artifact.version_tuple.prompt_version != rendered.prompt_version
            or artifact.version_tuple.model_snapshot is not None
            or artifact.version_tuple.agent_graph_version != plan.agent_graph_version
            or not isinstance(renderer_version, str)
            or artifact.version_tuple.tool_version != renderer_version
            or agent_tool_version != node.tool_version
        ):
            raise IntegrityViolation("model route prompt escapes the frozen execution plan")
        decision = _route_decision(transaction=transaction, route=route)
        if decision.model_snapshot != rendered_model or (
            isinstance(decision, RoutingDecisionV1)
            and (
                decision.run_id != run.run_id
                or decision.attempt_no != route.attempt_no
                or decision.catalog_version != plan.model_catalog_version
                or decision.catalog_digest != plan.model_catalog_digest
                or decision.policy_version != plan.routing_policy_version
                or decision.routing_policy_digest != plan.routing_policy_digest
            )
        ):
            raise IntegrityViolation("model route decision escapes its frozen execution plan")

        key = (route.attempt_no, route.call_ordinal, route.route_ordinal)
        consumption = consumptions.get(key)
        is_pending = pending is not None and key == (
            attempt_no,
            pending.call_ordinal,
            pending.route_ordinal,
        )
        if is_pending:
            pending_seen = True
            execution_source = pending.execution_source
            transport_attempt = pending.transport_attempt
            consumed = True
        elif consumption is not None:
            execution_source = consumption.execution_source
            transport_attempt = consumption.transport_attempt
            consumed = True
        else:
            execution_source = decision.execution_source
            transport_attempt = (
                failed_transport_attempts.get(
                    (
                        route.attempt_no,
                        f"sha256:{route.request_hash}",
                        route.routing_decision_id,
                        route.call_ordinal,
                        route.route_ordinal,
                    )
                )
                if execution_source == "online"
                else None
            )
            consumed = False
        if execution_source != decision.execution_source:
            raise IntegrityViolation("response execution source differs from route decision")
        bindings.append(
            InvocationVersionBindingV1(
                attempt_no=route.attempt_no,
                call_ordinal=route.call_ordinal,
                route_ordinal=route.route_ordinal,
                transport_attempt=transport_attempt,
                routing_decision_kind=route.routing_decision_kind,
                routing_decision_id=route.routing_decision_id,
                agent_node_id=rendered.agent_node_id,
                prompt_version=rendered.prompt_version,
                model_snapshot=rendered_model,
                tool_version=node.tool_version,
                execution_source=execution_source,
                response_consumed=consumed,
            )
        )
    if pending is not None and not pending_seen:
        raise IntegrityViolation("pending response has no committed model route")
    return build_execution_identity(
        scope=scope,  # type: ignore[arg-type]
        bindings=bindings,
        agent_graph_version=plan.agent_graph_version,
    )


__all__ = [
    "PendingResponseConsumption",
    "RenderedRequestAuthority",
    "build_authoritative_execution_identity",
    "prepare_rendered_request_authority",
]
