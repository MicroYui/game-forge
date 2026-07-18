"""Exact policy/catalog routing and immutable per-route authority publication."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import AttemptFenceStateRejected, IntegrityViolation
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    RunAttempt,
    RunIntermediateArtifactLinkV1,
    RunModelRouteLinkV1,
    RunRecord,
)
from gameforge.contracts.lineage import AuditActor, AuditCorrelation, AuditSubject
from gameforge.contracts.model_router import ModelRequestV2, request_hash
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.contracts.storage import UtcClock
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.cost_policy.routing import (
    RouteRequest,
    RouteSelection,
    RoutingPolicyService,
)
from gameforge.platform.runs.lifecycle import (
    AttemptWriteFence,
    validate_attempt_write_fence,
)


@dataclass(frozen=True, slots=True)
class PreparedWorkerRoute:
    request: RouteRequest
    selection: RouteSelection
    catalog_version: int
    catalog_digest: str
    policy_version: int
    routing_policy_digest: str
    routing_service: RoutingPolicyService = field(repr=False, compare=False)

    @property
    def model_snapshot_id(self) -> str:
        return self.selection.descriptor.model_snapshot

    @property
    def max_output_tokens(self) -> int:
        return self.request.max_output_tokens


class ResourceDomainResolver(Protocol):
    def resolve(self, *, run: RunRecord, transaction: object) -> DomainScope: ...


class PersistedArtifactResourceDomainResolver:
    """Reuse the exact server-resolved domain frozen by Run admission."""

    def resolve(self, *, run: RunRecord, transaction: object) -> DomainScope:
        del transaction
        domain = run.resource_domain_scope
        if not isinstance(domain, DomainScope):
            raise IntegrityViolation("LLM Run lacks the exact admission-resolved resource domain")
        return domain


def _now(clock: UtcClock) -> datetime:
    value = clock.now_utc()
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise IntegrityViolation("worker routing clock must return UTC")
    return value.astimezone(UTC)


def _requested_output_tokens(
    request: ModelRequestV2,
    *,
    allowed_model_snapshots: tuple[str, ...],
    service: RoutingPolicyService,
) -> int:
    has_current = "max_output_tokens" in request.params
    has_legacy = "max_tokens" in request.params
    if has_current and has_legacy:
        raise IntegrityViolation("model route has ambiguous output-token bounds")
    if not has_current and not has_legacy:
        by_id = {item.model_snapshot: item for item in service.catalog.models}
        bounds = {
            by_id[model_id].max_output_tokens
            for model_id in allowed_model_snapshots
            if model_id in by_id
        }
        if len(bounds) != 1 or len(
            [model_id for model_id in allowed_model_snapshots if model_id in by_id]
        ) != len(allowed_model_snapshots):
            raise IntegrityViolation(
                "output-token bound is neither explicit nor uniquely derivable from catalog"
            )
        return next(iter(bounds))
    value = request.params["max_output_tokens" if has_current else "max_tokens"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise IntegrityViolation("model route has no positive output-token bound")
    return value


class WorkerRoutingDecider:
    """Select before rendering; atomically retain decision + route after rendering."""

    def __init__(
        self,
        *,
        unit_of_work: object,
        run: RunRecord,
        attempt: RunAttempt,
        fence: AttemptWriteFence,
        actor: AuditActor,
        clock: UtcClock,
        audit_chain_id: str,
        domain_resolver: ResourceDomainResolver,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._run = run
        self._attempt = attempt
        self._fence = fence
        self._actor = actor
        self._clock = clock
        self._audit_chain_id = audit_chain_id
        self._domain_resolver = domain_resolver

    def prepare(self, model_request: ModelRequestV2) -> PreparedWorkerRoute:
        with self._unit_of_work.begin_read() as transaction:  # type: ignore[attr-defined]
            run, attempt = self._fresh_authority(transaction)
            plan, service = self._service(transaction=transaction, run=run)
            node = next(
                (item for item in plan.nodes if item.agent_node_id == model_request.agent_node_id),
                None,
            )
            if node is None or node.prompt_version != model_request.prompt_version:
                raise IntegrityViolation("routing request escapes the frozen execution plan")
            domain = self._domain_resolver.resolve(run=run, transaction=transaction)
            max_output_tokens = _requested_output_tokens(
                model_request,
                allowed_model_snapshots=node.allowed_model_snapshots,
                service=service,
            )
            params = dict(model_request.params)
            if "max_output_tokens" not in params and "max_tokens" not in params:
                params["max_output_tokens"] = max_output_tokens
            bounded_request = model_request.model_copy(update={"params": params})
            route_request = RouteRequest(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                task_kind=model_request.agent_node_id,
                domain_scope=domain,
                budget_set_snapshot_id=run.budget_set_snapshot_id,
                remaining_budget=transaction.cost.remaining_hold_amounts(  # type: ignore[attr-defined]
                    run.run_budget_hold_group_id
                ),
                context_tokens=len(
                    canonical_json(bounded_request.model_dump(mode="json")).encode("utf-8")
                ),
                max_output_tokens=max_output_tokens,
            )
            selection = service.select(route_request)
            self._validate_selection(node.allowed_model_snapshots, selection)
            return PreparedWorkerRoute(
                request=route_request,
                selection=selection,
                catalog_version=service.catalog.catalog_version,
                catalog_digest=service.catalog.catalog_digest,
                policy_version=service.policy.policy_version,
                routing_policy_digest=service.policy.routing_policy_digest,
                routing_service=service,
            )

    def next_fallback(self, previous: PreparedWorkerRoute) -> PreparedWorkerRoute:
        with self._unit_of_work.begin_read() as transaction:  # type: ignore[attr-defined]
            run, _ = self._fresh_authority(transaction)
            plan = run.payload.execution_version_plan
            if plan is None:
                raise IntegrityViolation("fallback route lost its frozen execution plan")
            service = previous.routing_service
            selection = service.next_fallback(previous.selection, request=previous.request)
            node = next(
                (item for item in plan.nodes if item.agent_node_id == previous.request.task_kind),
                None,
            )
            if node is None:
                raise IntegrityViolation("fallback route node left the frozen plan")
            self._validate_selection(node.allowed_model_snapshots, selection)
            return PreparedWorkerRoute(
                request=previous.request,
                selection=selection,
                catalog_version=service.catalog.catalog_version,
                catalog_digest=service.catalog.catalog_digest,
                policy_version=service.policy.policy_version,
                routing_policy_digest=service.policy.routing_policy_digest,
                routing_service=service,
            )

    def decide_and_record(
        self,
        model_request: ModelRequestV2,
        *,
        prepared: PreparedWorkerRoute,
        link: RunIntermediateArtifactLinkV1,
        execution_source: Literal["online", "full_response_cache", "cassette_replay"],
        decided_at: datetime,
    ) -> RoutingDecisionV1:
        del decided_at  # The transaction's UTC authority stamps the decision.
        if prepared.model_snapshot_id != link_request_model(model_request):
            raise IntegrityViolation("rendered request differs from prepared route model")
        if link.request_hash != request_hash(model_request).removeprefix("sha256:"):
            raise IntegrityViolation("rendered prompt link differs from prepared route")
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            run, attempt = self._fresh_authority(transaction)
            service = prepared.routing_service
            decision = service.decide_and_record(
                prepared.request,
                model_request=model_request,
                repository=transaction.cost,
                execution_source=execution_source,
                decided_at=_now(self._clock),
                selection=prepared.selection,
            )
            route = RunModelRouteLinkV1(
                run_id=run.run_id,
                attempt_no=attempt.attempt_no,
                call_ordinal=link.call_ordinal,
                route_ordinal=link.route_ordinal,
                prompt_artifact_id=link.artifact_id,
                request_hash=link.request_hash,
                routing_decision_kind="native",
                routing_decision_id=decision.decision_id,
                fencing_token=link.fencing_token,
                published_at=link.published_at,
            )
            if transaction.runs.put_model_route_link(route) != route:
                raise IntegrityViolation("RunStore retained another immutable model route")
            AuditGate(sink=transaction.audit, clock=self._clock).append(
                chain_id=self._audit_chain_id,
                actor=self._actor,
                initiated_by=run.initiated_by,
                action="run.model_route_decided",
                subject=AuditSubject(resource_kind="run", resource_id=run.run_id),
                correlation=AuditCorrelation(
                    request_id=None,
                    run_id=run.run_id,
                    trace_id=attempt.trace_id,
                ),
            )
            return decision

    def _fresh_authority(self, transaction: object) -> tuple[RunRecord, RunAttempt]:
        runs = transaction.runs  # type: ignore[attr-defined]
        authority = runs.get_attempt_write_authority(self._fence)
        if authority is None:
            raise IntegrityViolation("worker routing authority disappeared")
        run, attempt, lease = authority
        validate_attempt_write_fence(
            run=run,
            attempt=attempt,
            lease=lease,
            fence=self._fence,
            actor=self._actor,
            now=_now(self._clock),
            allowed_statuses=frozenset({"running"}),
        )
        if run.cancel_requested_at is not None:
            raise AttemptFenceStateRejected("cancel-requested Run cannot select a model route")
        return run, attempt

    @staticmethod
    def _service(*, transaction: object, run: RunRecord):
        plan = run.payload.execution_version_plan
        if plan is None or run.payload.llm_execution_mode == "not_applicable":
            raise IntegrityViolation("model routing requires a frozen execution plan")
        catalog = transaction.cost.get_model_catalog(  # type: ignore[attr-defined]
            plan.model_catalog_version,
            plan.model_catalog_digest,
        )
        policy = transaction.cost.get_routing_policy(  # type: ignore[attr-defined]
            plan.routing_policy_version,
            plan.routing_policy_digest,
        )
        if catalog is None or policy is None:
            raise IntegrityViolation("exact model catalog/routing policy history is unavailable")
        return plan, RoutingPolicyService(catalog=catalog, policy=policy)

    @staticmethod
    def _validate_selection(allowed: tuple[str, ...], selection: RouteSelection) -> None:
        if selection.descriptor.model_snapshot not in allowed:
            raise IntegrityViolation("routing policy selected a model outside the node plan")


def link_request_model(request: ModelRequestV2) -> str:
    return canonical_model_snapshot_id(request.model_snapshot)


__all__ = ["PreparedWorkerRoute", "WorkerRoutingDecider"]
