"""Rebuild model execution identity from immutable platform authorities."""

from __future__ import annotations

from dataclasses import dataclass

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cassette_import import LegacyImportRoutingDecisionV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    RunModelRouteLinkV1,
    RunRecord,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    ExecutionIdentityV1,
    InvocationVersionBindingV1,
    ObjectRef,
    build_execution_identity,
)
from gameforge.contracts.model_router import (
    ModelRequestV1,
    ModelRequestV2,
    parse_model_request,
    request_hash,
)
import json
from collections.abc import Mapping
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id


@dataclass(frozen=True, slots=True)
class PendingResponseConsumption:
    """The final route being committed in the caller's current write UoW."""

    call_ordinal: int
    route_ordinal: int
    execution_source: str
    transport_attempt: int | None


def _read_rendered_request(
    *,
    transaction: object,
    object_store: object,
    route: RunModelRouteLinkV1,
) -> tuple[ArtifactV2, ModelRequestV1 | ModelRequestV2]:
    artifact = transaction.artifacts.get(route.prompt_artifact_id)  # type: ignore[attr-defined]
    if (
        not isinstance(artifact, ArtifactV2)
        or artifact.kind != "source_rendered"
        or artifact.meta.get("payload_schema_id") != "source-rendered@1"
        or not isinstance(artifact.object_ref, ObjectRef)
    ):
        raise IntegrityViolation("model route prompt is not a published source_rendered")
    binding = transaction.object_bindings.resolve(artifact.object_ref)  # type: ignore[attr-defined]
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
    return artifact, rendered


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


def _failed_route_transport_attempt(
    *,
    transaction: object,
    route: RunModelRouteLinkV1,
) -> int | None:
    attempts = tuple(
        group.transport_attempt
        for group in transaction.cost.list_attempt_reservation_groups(  # type: ignore[attr-defined]
            run_id=route.run_id,
            attempt_no=route.attempt_no,
        )
        if group.scope == "attempt_call"
        and group.request_hash == f"sha256:{route.request_hash}"
        and f"model-route:{route.routing_decision_id}:call:{route.call_ordinal}:"
        in (group.idempotency_key)
        and f":route:{route.route_ordinal}:" in group.idempotency_key
        and group.transport_attempt is not None
        and group.status != "released"
    )
    # A route is immutable immediately before reserve-before-use. If admission
    # itself rejects, no transport happened and the exact value is null.
    return max(attempts, default=None)


def build_authoritative_execution_identity(
    *,
    transaction: object,
    object_store: object,
    run: RunRecord,
    attempt_no: int | None,
    scope: str,
    pending: PendingResponseConsumption | None = None,
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
    routes = transaction.runs.list_model_route_links(  # type: ignore[attr-defined]
        run.run_id,
        attempt_no=attempt_no,
    )
    if scope == "record_shard":
        if attempt_no is None or pending is None:
            raise IntegrityViolation("record-shard identity requires one pending call")
        call_routes = tuple(route for route in routes if route.call_ordinal == pending.call_ordinal)
        if not call_routes or call_routes[-1].route_ordinal != pending.route_ordinal:
            raise IntegrityViolation("pending response is not the final committed route")
        routes = tuple(
            route for route in call_routes if route.route_ordinal == pending.route_ordinal
        )
        if len(routes) != 1:
            raise IntegrityViolation("pending response has no unique committed route")
    consumptions = {
        (item.attempt_no, item.call_ordinal, item.route_ordinal): item
        for item in transaction.runs.list_model_response_consumptions(  # type: ignore[attr-defined]
            run.run_id,
            attempt_no=attempt_no,
        )
    }
    if pending is not None:
        pending_key = (attempt_no, pending.call_ordinal, pending.route_ordinal)
        if pending_key in consumptions:
            raise IntegrityViolation("pending response route was already consumed")

    bindings: list[InvocationVersionBindingV1] = []
    pending_seen = False
    for route in routes:
        artifact, rendered = _read_rendered_request(
            transaction=transaction,
            object_store=object_store,
            route=route,
        )
        node = next(
            (item for item in plan.nodes if item.agent_node_id == rendered.agent_node_id),
            None,
        )
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
                _failed_route_transport_attempt(transaction=transaction, route=route)
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
    "build_authoritative_execution_identity",
]
