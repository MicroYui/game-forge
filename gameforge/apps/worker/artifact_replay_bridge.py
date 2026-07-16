"""Production model bridge for native and verified-legacy Artifact REPLAY."""

from __future__ import annotations

from datetime import UTC, datetime
import threading
import time
from typing import Protocol

from gameforge.apps.worker.model_bridge import (
    AgentPromptContextPublisher,
    AgentStepCostGateway,
    CallCostGateway,
    ModelCallRequest,
    ModelCallResult,
    ModelSnapshotResolver,
    PromptRenderPublisher,
    ResponseConsumptionPublisher,
)
from gameforge.apps.worker.replay import (
    ArtifactReplaySource,
    LegacyArtifactReplaySource,
    NativeArtifactReplaySource,
)
from gameforge.contracts.cassette_import import LegacyImportRoutingDecisionV1
from gameforge.contracts.errors import AttemptFenceStateRejected, IntegrityViolation
from gameforge.contracts.jobs import (
    AgentPromptPriorConsumptionV1,
    RunAttempt,
    RunIntermediateArtifactLinkV1,
    RunModelRouteLinkV1,
    RunRecord,
    RunModelResponseConsumptionV1,
    RunToolIntermediateLinkV1,
)
from gameforge.contracts.lineage import AuditActor, AuditCorrelation, AuditSubject
from gameforge.contracts.model_router import ModelRequestV1, ModelRequestV2, request_hash
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.contracts.storage import UtcClock
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.runs.commands import PromptRenderPublicationRequest
from gameforge.platform.runs.lifecycle import AttemptWriteFence, validate_attempt_write_fence
from gameforge.runtime.cassette.store import CassetteRouteKey
from gameforge.runtime.model_router.m4_router import M4ModelRouter, M4RouterResultV1
from gameforge.runtime.observability import Tracer


ReplayDecision = RoutingDecisionV1 | LegacyImportRoutingDecisionV1


def _utc(clock: UtcClock) -> datetime:
    value = clock.now_utc()
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise IntegrityViolation("replay bridge clock must return UTC")
    return value.astimezone(UTC)


class ReplayRoutePublisher(Protocol):
    def publish(
        self,
        *,
        link: RunIntermediateArtifactLinkV1,
        decision: ReplayDecision,
        actor: AuditActor,
    ) -> RunModelRouteLinkV1: ...


class WorkerReplayRoutePublisher:
    """Persist source-faithful replay route identity before response lookup."""

    def __init__(
        self,
        *,
        unit_of_work: object,
        fence: AttemptWriteFence,
        clock: UtcClock,
        audit_chain_id: str,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._fence = fence
        self._clock = clock
        self._audit_chain_id = audit_chain_id

    def publish(
        self,
        *,
        link: RunIntermediateArtifactLinkV1,
        decision: ReplayDecision,
        actor: AuditActor,
    ) -> RunModelRouteLinkV1:
        kind = "native" if isinstance(decision, RoutingDecisionV1) else "legacy_import"
        if (
            link.run_id != self._fence.run_id
            or link.attempt_no != self._fence.attempt_no
            or decision.request_hash != f"sha256:{link.request_hash}"
            or decision.execution_source != "cassette_replay"
        ):
            raise IntegrityViolation("replay route differs from its prompt/decision authority")
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            run = transaction.runs.get(link.run_id)
            attempt = transaction.runs.get_attempt(link.run_id, link.attempt_no)
            lease = transaction.runs.get_current_lease(link.run_id)
            if (
                not isinstance(run, RunRecord)
                or not isinstance(attempt, RunAttempt)
                or lease is None
            ):
                raise IntegrityViolation("replay route attempt authority disappeared")
            validate_attempt_write_fence(
                run=run,
                attempt=attempt,
                lease=lease,
                fence=self._fence,
                actor=actor,
                now=_utc(self._clock),
                allowed_statuses=frozenset({"running"}),
            )
            if run.cancel_requested_at is not None:
                raise AttemptFenceStateRejected(
                    "cancel-requested Run cannot publish a replay model route"
                )
            if isinstance(decision, RoutingDecisionV1):
                if (
                    decision.run_id != run.run_id
                    or decision.attempt_no != attempt.attempt_no
                    or transaction.cost.put_routing_decision(decision) != decision
                ):
                    raise IntegrityViolation("native replay decision differs from current Run")
            elif (
                transaction.cost.get_legacy_import_routing_decision(decision.decision_id)
                != decision
            ):
                raise IntegrityViolation("legacy replay decision authority disappeared")
            route = RunModelRouteLinkV1(
                run_id=link.run_id,
                attempt_no=link.attempt_no,
                call_ordinal=link.call_ordinal,
                route_ordinal=link.route_ordinal,
                prompt_artifact_id=link.artifact_id,
                request_hash=link.request_hash,
                routing_decision_kind=kind,
                routing_decision_id=decision.decision_id,
                fencing_token=link.fencing_token,
                published_at=link.published_at,
            )
            if transaction.runs.put_model_route_link(route) != route:
                raise IntegrityViolation("RunStore retained another replay model route")
            AuditGate(sink=transaction.audit, clock=self._clock).append(
                chain_id=self._audit_chain_id,
                actor=actor,
                initiated_by=run.initiated_by,
                action="run.model_route_decided",
                subject=AuditSubject(resource_kind="run", resource_id=run.run_id),
                correlation=AuditCorrelation(
                    request_id=None,
                    run_id=run.run_id,
                    trace_id=attempt.trace_id,
                ),
            )
            return route


class ArtifactReplayModelBridge:
    """Replay exact source routes without policy reselection or provider access."""

    def __init__(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        fence: AttemptWriteFence,
        source: ArtifactReplaySource,
        prompt_publisher: PromptRenderPublisher,
        context_publisher: AgentPromptContextPublisher,
        route_publisher: ReplayRoutePublisher,
        native_router: M4ModelRouter | None,
        cost: CallCostGateway,
        step_cost: AgentStepCostGateway,
        response_publisher: ResponseConsumptionPublisher,
        model_snapshot_resolver: ModelSnapshotResolver,
        tracer: Tracer,
        clock: UtcClock,
        worker_actor: AuditActor,
    ) -> None:
        if (
            run.payload.llm_execution_mode != "replay"
            or attempt.run_id != run.run_id
            or attempt.attempt_no != fence.attempt_no
            or attempt.fencing_token != fence.fencing_token
            or attempt.status != "running"
            or attempt.attempt_deadline_utc is None
        ):
            raise IntegrityViolation("artifact replay bridge requires an exact running REPLAY")
        if isinstance(source, NativeArtifactReplaySource) != (native_router is not None):
            raise IntegrityViolation("native replay source/router composition differs")
        self._run = run
        self._attempt = attempt
        self._fence = fence
        self._source = source
        self._prompt_publisher = prompt_publisher
        self._context_publisher = context_publisher
        self._route_publisher = route_publisher
        self._native_router = native_router
        self._cost = cost
        self._step_cost = step_cost
        self._response_publisher = response_publisher
        self._resolver = model_snapshot_resolver
        self._tracer = tracer
        self._clock = clock
        self._actor = worker_actor
        self._next_call_ordinal = attempt.next_call_ordinal
        self._last_consumption: AgentPromptPriorConsumptionV1 | None = None
        self._lock = threading.Lock()

    def resolve_model_snapshot(self, **values):
        return self._resolver.resolve_model_snapshot(**values)

    def call_model(self, request: ModelCallRequest) -> ModelCallResult:
        with self._lock:
            return self._call_serialized(request)

    def _call_serialized(self, request: ModelCallRequest) -> ModelCallResult:
        call_ordinal = self._next_call_ordinal
        expected_scope = f"run:{self._run.run_id}:attempt:{self._attempt.attempt_no}"
        if (
            request.idempotency_scope != expected_scope
            or request.idempotency_key != f"model:{call_ordinal}"
            or request.route_ordinal != 1
        ):
            raise IntegrityViolation("replay handler call identity differs from Attempt head")
        prior = (
            self._last_consumption if request.prompt_context.include_previous_consumption else None
        )
        if request.prompt_context.include_previous_consumption and prior is None:
            raise IntegrityViolation("replay prompt context requires a prior consumed response")
        context_publication = self._context_publisher.publish_agent_prompt_context(
            model_request=request.model_request,
            draft=request.prompt_context,
            target_call_ordinal=call_ordinal,
            prior_consumption=prior,
            idempotency_scope=expected_scope,
            idempotency_key=f"model:{call_ordinal}:context",
            actor=self._actor,
        )
        context_link = context_publication.link
        with self._tracer.span(
            "worker.model.call",
            attributes={
                "run_id": self._fence.run_id,
                "attempt_no": self._fence.attempt_no,
                "logical_call_ordinal": call_ordinal,
                "execution_source": "cassette_replay",
            },
        ) as span:
            if isinstance(self._source, LegacyArtifactReplaySource):
                result = self._legacy_call(
                    request,
                    call_ordinal=call_ordinal,
                    context_link=context_link,
                )
            else:
                result = self._native_call(
                    request,
                    call_ordinal=call_ordinal,
                    context_link=context_link,
                )
            latency = result.response.latency
            if latency.status == "reported":
                span.set_attribute(
                    "recorded_provider_latency_ms",
                    latency.provider_latency_ms,
                )
        self._next_call_ordinal += 1
        return result

    def _native_call(
        self,
        request: ModelCallRequest,
        *,
        call_ordinal: int,
        context_link: RunToolIntermediateLinkV1,
    ) -> ModelCallResult:
        source = self._source
        assert isinstance(source, NativeArtifactReplaySource)
        plan = source.call_plan(
            attempt_no=self._attempt.attempt_no,
            call_ordinal=call_ordinal,
        )
        if plan is None or plan.consumed_route is None:
            raise IntegrityViolation("native replay logical call is absent from source authority")
        self._require_same_semantics(request.model_request, plan.consumed_route.request)
        current_call: int | None = None
        step_reservation: object | None = None
        try:
            for route_ordinal, source_route in enumerate(plan.routes, start=1):
                current = source.project_current_decision(
                    source_route,
                    attempt_no=self._attempt.attempt_no,
                    decided_at=_utc(self._clock),
                )
                link = self._publish_prompt(
                    source_route.request,
                    source_artifact_ids=(context_link.artifact_id,),
                    logical_call_ordinal=current_call,
                    route_ordinal=route_ordinal,
                )
                if current_call is None:
                    current_call = link.call_ordinal
                    if current_call != call_ordinal:
                        raise IntegrityViolation(
                            "prompt publisher returned another replay call head"
                        )
                if step_reservation is None:
                    step_reservation = self._reserve_step(
                        source_route.request,
                        link=link,
                        deadline_utc=self._deadline(request.deadline_utc),
                    )
                self._route_publisher.publish(link=link, decision=current, actor=self._actor)
                if not source_route.invocation.response_consumed:
                    continue
                router = self._native_router
                if router is None:  # closed by constructor
                    raise IntegrityViolation("native replay router disappeared")
                reservation = self._reserve(current, source_route.request, link)
                started = time.monotonic_ns()
                try:
                    response = router.call(
                        source_route.request,
                        decision=current,
                        deadline_utc=self._deadline(request.deadline_utc),
                        recorded_at=_utc(self._clock),
                        cassette_route_key=CassetteRouteKey(
                            run_id=self._run.run_id,
                            attempt_no=self._attempt.attempt_no,
                            call_ordinal=link.call_ordinal,
                            route_ordinal=link.route_ordinal,
                            routing_decision_id=current.decision_id,
                        ),
                    )
                except BaseException:
                    self._cost.cancel_reservation(reservation=reservation)
                    raise
                consumption = self._consume(
                    link=link,
                    decision=current,
                    result=response,
                    reservation=reservation,
                    step_reservation=step_reservation,
                    wall_time_ns=max(0, time.monotonic_ns() - started),
                )
                # The response-publication UoW settled both call usage and this
                # logical Agent step. Do not charge it again on the outer path.
                step_reservation = None
                outcome = ModelCallResult(
                    response=response,
                    decision=current,
                    link=link,
                    context_link=context_link,
                    replayed=False,
                )
                self._retain_prior(
                    link=link,
                    decision=current,
                    consumption=consumption,
                )
                break
            else:
                raise IntegrityViolation("native replay call has no consumed source route")
        except BaseException as error:
            self._reconcile_incurred_step(step_reservation, primary_error=error)
            raise
        self._reconcile_incurred_step(step_reservation, primary_error=None)
        return outcome

    def _legacy_call(
        self,
        request: ModelCallRequest,
        *,
        call_ordinal: int,
        context_link: RunToolIntermediateLinkV1,
    ) -> ModelCallResult:
        source = self._source
        assert isinstance(source, LegacyArtifactReplaySource)
        planned = source.expected_call(call_ordinal=call_ordinal)
        self._require_same_semantics(request.model_request, planned.request)
        link = self._publish_prompt(
            planned.request,
            source_artifact_ids=(context_link.artifact_id,),
            logical_call_ordinal=None,
            route_ordinal=1,
        )
        step_reservation: object | None = None
        try:
            step_reservation = self._reserve_step(
                planned.request,
                link=link,
                deadline_utc=self._deadline(request.deadline_utc),
            )
            self._route_publisher.publish(
                link=link,
                decision=planned.routing_decision,
                actor=self._actor,
            )
            reservation = self._reserve(planned.routing_decision, planned.request, link)
            started = time.monotonic_ns()
            try:
                response = source.replay(planned.request, call_ordinal=call_ordinal)
            except BaseException:
                self._cost.cancel_reservation(reservation=reservation)
                raise
            consumption = self._consume(
                link=link,
                decision=planned.routing_decision,
                result=response,
                reservation=reservation,
                step_reservation=step_reservation,
                wall_time_ns=max(0, time.monotonic_ns() - started),
            )
            step_reservation = None
            outcome = ModelCallResult(
                response=response,
                decision=planned.routing_decision,  # type: ignore[arg-type]
                link=link,
                context_link=context_link,
                replayed=False,
            )
            self._retain_prior(
                link=link,
                decision=planned.routing_decision,
                consumption=consumption,
            )
        except BaseException as error:
            self._reconcile_incurred_step(step_reservation, primary_error=error)
            raise
        self._reconcile_incurred_step(step_reservation, primary_error=None)
        return outcome

    def _publish_prompt(
        self,
        model_request: ModelRequestV1 | ModelRequestV2,
        *,
        source_artifact_ids: tuple[str, ...],
        logical_call_ordinal: int | None,
        route_ordinal: int,
    ) -> RunIntermediateArtifactLinkV1:
        digest = request_hash(model_request).removeprefix("sha256:")
        result = self._prompt_publisher.publish_prompt_rendered(
            PromptRenderPublicationRequest(
                fence=self._fence,
                logical_call_ordinal=self._next_call_ordinal,
                call_ordinal=logical_call_ordinal,
                route_ordinal=route_ordinal,
                artifact_id="server-derived:source-rendered",
                request_hash=digest,
                idempotency_scope=(f"run:{self._run.run_id}:attempt:{self._attempt.attempt_no}"),
                idempotency_key=f"model:{self._next_call_ordinal}:route:{route_ordinal}",
                actor=self._actor,
            ),
            model_request=model_request,  # type: ignore[arg-type]
            # The cassette authenticates the expected rendered request; it is not
            # the raw/renderable source. Re-render the handler's exact admitted
            # input and require byte-for-byte equality with the recorded request.
            source_artifact_ids=source_artifact_ids,
        )
        return result.link

    def _reserve(
        self,
        decision: ReplayDecision,
        model_request: ModelRequestV1 | ModelRequestV2,
        link: RunIntermediateArtifactLinkV1,
    ) -> object:
        return self._cost.reserve_call(
            decision=decision,
            model_request=model_request,
            deadline_utc=self._deadline(None),
            call_ordinal=link.call_ordinal,
            route_ordinal=link.route_ordinal,
            transport_attempt=1,
        )

    def _reserve_step(
        self,
        model_request: ModelRequestV1 | ModelRequestV2,
        *,
        link: RunIntermediateArtifactLinkV1,
        deadline_utc: datetime,
    ) -> object:
        return self._step_cost.reserve_step(
            request_hash=f"sha256:{link.request_hash}",
            execution_source="cassette_replay",
            deadline_utc=deadline_utc,
            call_ordinal=link.call_ordinal,
            agent_node_id=model_request.agent_node_id,
        )

    def _reconcile_incurred_step(
        self,
        reservation: object | None,
        *,
        primary_error: BaseException | None,
    ) -> None:
        if reservation is None:
            return
        try:
            self._step_cost.reconcile_step(reservation=reservation)
        except BaseException as settlement_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "agent-step reconciliation also failed after replay logical call: "
                f"{type(settlement_error).__name__}"
            )

    def _consume(
        self,
        *,
        link: RunIntermediateArtifactLinkV1,
        decision: ReplayDecision,
        result: M4RouterResultV1,
        reservation: object,
        step_reservation: object,
        wall_time_ns: int,
    ) -> RunModelResponseConsumptionV1:
        try:
            consumption = self._response_publisher.publish_response_consumption(
                fence=self._fence,
                link=link,
                decision=decision,  # type: ignore[arg-type]
                result=result,
                record=None,
                reservation=reservation,
                step_cost=self._step_cost,
                step_reservation=step_reservation,
                wall_time_ns=wall_time_ns,
                actor=self._actor,
            )
        except BaseException as publication_error:
            try:
                self._cost.reconcile_usage(
                    reservation=reservation,
                    decision=decision,
                    result=result,
                    wall_time_ns=wall_time_ns,
                )
            except BaseException as settlement_error:
                publication_error.add_note(
                    "usage reconciliation also failed after replay response publication: "
                    f"{type(settlement_error).__name__}"
                )
            raise
        if not isinstance(consumption, RunModelResponseConsumptionV1):
            raise IntegrityViolation("replay response publication omitted consumption authority")
        return consumption

    def _retain_prior(
        self,
        *,
        link: RunIntermediateArtifactLinkV1,
        decision: ReplayDecision,
        consumption: RunModelResponseConsumptionV1,
    ) -> None:
        if consumption.response_digest is None:
            raise IntegrityViolation("new replay consumption omitted response_digest")
        self._last_consumption = AgentPromptPriorConsumptionV1(
            attempt_no=consumption.attempt_no,
            call_ordinal=consumption.call_ordinal,
            route_ordinal=consumption.route_ordinal,
            prompt_artifact_id=link.artifact_id,
            request_hash=link.request_hash,
            routing_decision_kind=(
                "legacy_import" if isinstance(decision, LegacyImportRoutingDecisionV1) else "native"
            ),
            routing_decision_id=decision.decision_id,
            execution_source=consumption.execution_source,
            reservation_group_id=consumption.reservation_group_id,
            transport_attempt=consumption.transport_attempt,
            cassette_shard_artifact_id=consumption.cassette_shard_artifact_id,
            cassette_source_artifact_id=self._run.payload.cassette_artifact_id,
            response_digest=consumption.response_digest,
        )

    def _deadline(self, requested: datetime | None) -> datetime:
        authoritative = datetime.fromisoformat(
            self._attempt.attempt_deadline_utc.replace("Z", "+00:00")  # type: ignore[union-attr]
        )
        if authoritative.tzinfo is None or authoritative.utcoffset() != UTC.utcoffset(
            authoritative
        ):
            raise IntegrityViolation("replay attempt deadline is not UTC")
        if requested is None:
            return authoritative.astimezone(UTC)
        if requested.tzinfo is None or requested.utcoffset() != UTC.utcoffset(requested):
            raise IntegrityViolation("replay call deadline is not UTC")
        return min(authoritative.astimezone(UTC), requested.astimezone(UTC))

    @staticmethod
    def _require_same_semantics(
        handler: ModelRequestV1 | ModelRequestV2,
        source: ModelRequestV1 | ModelRequestV2,
    ) -> None:
        fields = ("messages", "params", "tool_schemas", "agent_node_id", "prompt_version")
        if any(getattr(handler, field) != getattr(source, field) for field in fields):
            raise IntegrityViolation("handler request differs from replay source semantics")


__all__ = [
    "ArtifactReplayModelBridge",
    "ReplayRoutePublisher",
    "WorkerReplayRoutePublisher",
]
