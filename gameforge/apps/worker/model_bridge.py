"""The per-call M4b execution bridge the worker hands to each executor.

One :class:`WorkerModelBridge` is built per fenced attempt (the runner's
``model_bridge_factory``) and drives every LLM invocation through the M4b
router/cassette/cost/trace seams in the exact §7.A order:

  publish ``source_rendered`` (BEFORE the call, fenced + idempotent) ->
  record the routing decision -> reserve cost -> execute the one persisted
  decision on the ``M4ModelRouter`` (recording the cassette shard in RECORD;
  failing closed on a REPLAY miss) -> reconcile the typed usage.

The bridge never touches the DB or ObjectStore directly: prompt publication,
routing persistence, and cost reservation/reconciliation all go through injected
transaction-bound ports. A stale worker whose prompt publication is fenced out
raises before any reserve or provider call, so it can neither start a new reserve
nor publish a business result; its already-persisted reservation still settles
via the attempt-close / reaper path. REPLAY misses propagate ``CassetteReplayMiss``
so the executor / terminal policy fails the attempt closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import threading
import time
from collections.abc import Callable
from typing import Literal, Protocol

from gameforge.contracts.cassette import CassetteRecordV2
from gameforge.contracts.cassette_import import LegacyImportRoutingDecisionV1
from gameforge.contracts.errors import (
    DependencyUnavailable,
    IntegrityViolation,
    PermanentDependencyFailure,
    QuotaExceeded,
)
from gameforge.contracts.jobs import (
    AgentPromptContextDraftV1,
    AgentPromptPriorConsumptionV1,
    RunAttempt,
    RunIntermediateArtifactLinkV1,
    RunModelResponseConsumptionV1,
    RunRecord,
    RunToolIntermediateLinkV1,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.model_router import (
    ModelBridgeCallRequestV1,
    ModelRequestV1,
    ModelRequestV2,
    ModelSnapshot,
    request_hash,
)
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.contracts.storage import UtcClock
from gameforge.platform.runs.commands import (
    AgentPromptContextPublicationResult,
    PromptRenderPublicationRequest,
    PromptRenderPublicationResult,
)
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.runtime.cassette.store import CassetteRouteKey
from gameforge.runtime.model_router.m4_router import (
    M4ModelRouter,
    M4RouterResultV1,
    ProviderRouteFailure,
)
from gameforge.runtime.observability.trace import Tracer
from gameforge.runtime.reliability.retry import RetryAttemptResult


ExecutionSource = Literal["online", "full_response_cache", "cassette_replay"]


class PromptRenderPublisher(Protocol):
    """Fenced ``source_rendered`` publication (``RunCommandService`` surface)."""

    def require_replay_source_semantics(
        self,
        *,
        handler_request: ModelRequestV1 | ModelRequestV2,
        source_request: ModelRequestV1 | ModelRequestV2,
    ) -> None: ...

    def publish_prompt_rendered(
        self,
        request: PromptRenderPublicationRequest,
        *,
        model_request: ModelRequestV2,
        source_artifact_ids: tuple[str, ...],
    ) -> PromptRenderPublicationResult: ...


class AgentPromptContextPublisher(Protocol):
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
    ) -> AgentPromptContextPublicationResult: ...


class PreparedRoute(Protocol):
    @property
    def model_snapshot_id(self) -> str: ...

    @property
    def max_output_tokens(self) -> int: ...


class RoutingDecider(Protocol):
    """Select + persist exactly one native routing decision for a rendered call.

    Implementations bind ``RouteRequest.task_kind`` to the exact
    ``model_request.agent_node_id``.  The routing discriminator is an Agent node,
    not the enclosing Run kind; this is the same identity closed by the admitted
    ``ExecutionVersionPlanV1``.
    """

    def prepare(self, model_request: ModelRequestV2) -> PreparedRoute: ...

    def next_fallback(self, previous: PreparedRoute) -> PreparedRoute: ...

    def decide_and_record(
        self,
        model_request: ModelRequestV2,
        *,
        prepared: PreparedRoute,
        link: RunIntermediateArtifactLinkV1,
        execution_source: ExecutionSource,
        decided_at: datetime,
    ) -> RoutingDecisionV1: ...


class CallCostGateway(Protocol):
    """Reserve-before-use + typed-usage reconciliation for one model call."""

    def reserve_call(
        self,
        *,
        decision: RoutingDecisionV1,
        model_request: ModelRequestV2,
        deadline_utc: datetime,
        call_ordinal: int,
        route_ordinal: int,
        transport_attempt: int,
    ) -> object: ...

    def reconcile_usage(
        self,
        *,
        reservation: object,
        decision: RoutingDecisionV1,
        result: M4RouterResultV1,
        wall_time_ns: int,
    ) -> None: ...

    def settle_failed_transport(
        self,
        *,
        reservation: object,
        decision: RoutingDecisionV1,
        wall_time_ns: int,
    ) -> None: ...

    def cancel_reservation(self, *, reservation: object) -> None: ...


class AgentStepCostGateway(Protocol):
    """Charge one logical Agent node invocation independent of route retries."""

    def reserve_step(
        self,
        *,
        request_hash: str,
        execution_source: ExecutionSource,
        deadline_utc: datetime,
        call_ordinal: int,
        agent_node_id: str,
    ) -> object: ...

    def reconcile_step(self, *, reservation: object) -> None: ...

    def reconcile_step_in_transaction(
        self,
        *,
        transaction: object,
        reservation: object,
    ) -> object: ...


class ResponseConsumptionPublisher(Protocol):
    """Atomically reconcile usage, consume a route, and optionally publish RECORD."""

    def publish_response_consumption(
        self,
        *,
        fence: AttemptWriteFence,
        link: RunIntermediateArtifactLinkV1,
        decision: RoutingDecisionV1,
        result: M4RouterResultV1,
        record: CassetteRecordV2 | None,
        reservation: object,
        step_cost: AgentStepCostGateway,
        step_reservation: object,
        wall_time_ns: int,
        actor: AuditActor,
    ) -> RunModelResponseConsumptionV1: ...


class ModelSnapshotResolver(Protocol):
    """Resolve a plan's opaque model ID through exact retained authorities."""

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot: ...


ModelCallRequest = ModelBridgeCallRequestV1


@dataclass(frozen=True, slots=True)
class ModelCallResult:
    response: M4RouterResultV1
    decision: RoutingDecisionV1 | LegacyImportRoutingDecisionV1
    link: RunIntermediateArtifactLinkV1
    context_link: RunToolIntermediateLinkV1
    replayed: bool


class WorkerModelBridge:
    def __init__(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        fence: AttemptWriteFence,
        execution_source: ExecutionSource,
        prompt_publisher: PromptRenderPublisher,
        context_publisher: AgentPromptContextPublisher,
        decider: RoutingDecider,
        router: M4ModelRouter,
        cost: CallCostGateway,
        step_cost: AgentStepCostGateway,
        model_snapshot_resolver: ModelSnapshotResolver,
        tracer: Tracer,
        clock: UtcClock,
        worker_actor: AuditActor,
        response_publisher: ResponseConsumptionPublisher | None = None,
        execution_source_selector: (
            Callable[[ModelRequestV2, PreparedRoute], ExecutionSource] | None
        ) = None,
    ) -> None:
        if (
            attempt.run_id != run.run_id
            or attempt.attempt_no != fence.attempt_no
            or attempt.fencing_token != fence.fencing_token
            or attempt.status != "running"
            or attempt.attempt_deadline_utc is None
        ):
            raise IntegrityViolation("model bridge requires the exact running RunAttempt")
        self._run = run
        self._attempt = attempt
        self._fence = fence
        self._execution_source = execution_source
        self._prompt_publisher = prompt_publisher
        self._context_publisher = context_publisher
        self._decider = decider
        self._router = router
        self._cost = cost
        self._step_cost = step_cost
        self._model_snapshot_resolver = model_snapshot_resolver
        self._tracer = tracer
        self._clock = clock
        self._worker_actor = worker_actor
        self._response_publisher = response_publisher
        self._execution_source_selector = execution_source_selector
        self._call_lock = threading.Lock()
        self._next_call_ordinal = attempt.next_call_ordinal
        self._last_consumption: AgentPromptPriorConsumptionV1 | None = None

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot:
        return self._model_snapshot_resolver.resolve_model_snapshot(
            catalog_version=catalog_version,
            catalog_digest=catalog_digest,
            model_snapshot_id=model_snapshot_id,
        )

    def call_model(self, request: ModelCallRequest) -> ModelCallResult:
        with self._call_lock:
            return self._call_model_serialized(request)

    def _call_model_serialized(self, request: ModelCallRequest) -> ModelCallResult:
        self._validate_request_plan(request.model_request)
        expected_scope = f"run:{self._run.run_id}:attempt:{self._attempt.attempt_no}"
        expected_key = f"model:{self._next_call_ordinal}"
        if (
            request.idempotency_scope != expected_scope
            or request.idempotency_key != expected_key
            or request.route_ordinal != 1
        ):
            raise IntegrityViolation(
                "handler model-call identity is not the server-derived logical call head"
            )
        if self._response_publisher is None:
            raise IntegrityViolation(
                "model execution requires atomic response-consumption publication"
            )
        route_ordinal = 1
        call_ordinal: int | None = None
        logical_call_ordinal = self._next_call_ordinal
        prior_consumption = (
            self._last_consumption if request.prompt_context.include_previous_consumption else None
        )
        if request.prompt_context.include_previous_consumption and prior_consumption is None:
            raise IntegrityViolation(
                "Agent prompt context requires a prior consumed model response"
            )
        context_publication = self._context_publisher.publish_agent_prompt_context(
            model_request=request.model_request,
            draft=request.prompt_context,
            target_call_ordinal=logical_call_ordinal,
            prior_consumption=prior_consumption,
            idempotency_scope=expected_scope,
            idempotency_key=f"{expected_key}:context",
            actor=self._worker_actor,
        )
        context_link = context_publication.link
        if (
            context_link.run_id != self._run.run_id
            or context_link.attempt_no != self._attempt.attempt_no
            or context_link.target_call_ordinal != logical_call_ordinal
            or context_link.agent_node_id != request.model_request.agent_node_id
            or context_link.prompt_version != request.model_request.prompt_version
            or context_link.fencing_token != self._fence.fencing_token
        ):
            raise IntegrityViolation("Agent prompt context publisher returned another call")
        prepared = self._decider.prepare(request.model_request)
        effective_deadline = self._effective_deadline(request.deadline_utc)
        step_reservation: object | None = None
        try:
            with self._tracer.span(
                "worker.model.call",
                attributes={
                    "run_id": self._fence.run_id,
                    "attempt_no": self._fence.attempt_no,
                    "logical_call_ordinal": logical_call_ordinal,
                },
            ):
                while True:
                    routed_request = self._request_for_route(request.model_request, prepared)
                    self._validate_request_plan(routed_request)
                    execution_source = (
                        self._execution_source_selector(routed_request, prepared)
                        if self._execution_source_selector is not None
                        else self._execution_source
                    )
                    if self._execution_source_selector is not None and execution_source not in {
                        "online",
                        "full_response_cache",
                    }:
                        raise IntegrityViolation(
                            "live/RECORD execution-source selector returned an invalid source"
                        )
                    rendered_hash = request_hash(routed_request).removeprefix("sha256:")
                    publication = self._prompt_publisher.publish_prompt_rendered(
                        PromptRenderPublicationRequest(
                            fence=self._fence,
                            logical_call_ordinal=logical_call_ordinal,
                            call_ordinal=call_ordinal,
                            route_ordinal=route_ordinal,
                            artifact_id="server-derived:source-rendered",
                            request_hash=rendered_hash,
                            idempotency_scope=expected_scope,
                            idempotency_key=f"{expected_key}:route:{route_ordinal}",
                            actor=self._worker_actor,
                        ),
                        model_request=routed_request,
                        source_artifact_ids=(context_link.artifact_id,),
                    )
                    link = publication.link
                    if link.route_ordinal != route_ordinal:
                        raise IntegrityViolation("prompt publisher returned another route ordinal")
                    if call_ordinal is None:
                        if link.call_ordinal != logical_call_ordinal:
                            raise IntegrityViolation(
                                "prompt publisher returned another logical call ordinal"
                            )
                        call_ordinal = link.call_ordinal
                        # The prompt commit consumed the authoritative call head even if
                        # every subsequent route fails.
                        self._next_call_ordinal += 1
                    elif link.call_ordinal != call_ordinal:
                        raise IntegrityViolation("fallback prompt escaped its logical call")

                    if step_reservation is None:
                        # Prompt evidence is authoritative first. One logical Agent
                        # node step is then admitted before its first route/provider;
                        # fallback and transport retries reuse this same step.
                        step_reservation = self._step_cost.reserve_step(
                            request_hash=f"sha256:{link.request_hash}",
                            execution_source=execution_source,
                            deadline_utc=effective_deadline,
                            call_ordinal=link.call_ordinal,
                            agent_node_id=routed_request.agent_node_id,
                        )
                    decision = self._decider.decide_and_record(
                        routed_request,
                        prepared=prepared,
                        link=link,
                        execution_source=execution_source,
                        decided_at=self._now(),
                    )
                    try:
                        result, consumption = self._execute_route(
                            request=routed_request,
                            link=link,
                            decision=decision,
                            effective_deadline=effective_deadline,
                            step_reservation=step_reservation,
                        )
                    except ProviderRouteFailure as failure:
                        if not self._may_fallback(
                            failure,
                            execution_source=decision.execution_source,
                        ):
                            raise self._project_provider_failure(
                                failure,
                                decision=decision,
                            ) from failure
                        try:
                            prepared = self._decider.next_fallback(prepared)
                        except DependencyUnavailable as exhausted:
                            raise self._project_provider_failure(
                                failure,
                                decision=decision,
                                operation_code="model.route_fallback",
                                classifier_code="routing_fallback_exhausted",
                            ) from exhausted
                        route_ordinal += 1
                        continue
                    # The response-publication UoW settled this token together
                    # with call usage, shard/consumption, and audit.  Clearing it
                    # prevents the standalone incurred-error path from charging
                    # the same logical step a second time.
                    step_reservation = None
                    outcome = ModelCallResult(
                        response=result,
                        decision=decision,
                        link=link,
                        context_link=context_link,
                        replayed=publication.replayed,
                    )
                    if consumption.response_digest is None:
                        raise IntegrityViolation(
                            "new response consumption omitted its response digest"
                        )
                    self._last_consumption = AgentPromptPriorConsumptionV1(
                        attempt_no=consumption.attempt_no,
                        call_ordinal=consumption.call_ordinal,
                        route_ordinal=consumption.route_ordinal,
                        prompt_artifact_id=link.artifact_id,
                        request_hash=link.request_hash,
                        routing_decision_kind="native",
                        routing_decision_id=decision.decision_id,
                        execution_source=consumption.execution_source,
                        reservation_group_id=consumption.reservation_group_id,
                        transport_attempt=consumption.transport_attempt,
                        cassette_shard_artifact_id=(consumption.cassette_shard_artifact_id),
                        cassette_source_artifact_id=(
                            consumption.cassette_shard_artifact_id
                            if self._run.payload.llm_execution_mode == "record"
                            else None
                        ),
                        response_digest=consumption.response_digest,
                    )
                    break
        except BaseException as error:
            self._reconcile_incurred_step(step_reservation, primary_error=error)
            raise
        self._reconcile_incurred_step(step_reservation, primary_error=None)
        return outcome

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
                "agent-step reconciliation also failed after logical call: "
                f"{type(settlement_error).__name__}"
            )

    def _execute_route(
        self,
        *,
        request: ModelRequestV2,
        link: RunIntermediateArtifactLinkV1,
        decision: RoutingDecisionV1,
        effective_deadline: datetime,
        step_reservation: object,
    ) -> tuple[M4RouterResultV1, RunModelResponseConsumptionV1]:
        if self._response_publisher is None:  # closed by caller; keeps type narrow
            raise IntegrityViolation("response publisher disappeared")
        reservations: dict[int, object] = {}
        durations: dict[int, int] = {}
        transport_spans: dict[int, object] = {}

        def close_transport_span(
            transport_attempt: int,
            *,
            succeeded: bool,
            reason_code: str | None = None,
            error: BaseException | None = None,
        ) -> None:
            span = transport_spans.pop(transport_attempt, None)
            if span is None:
                return
            try:
                span.set_attribute("succeeded", succeeded)  # type: ignore[attr-defined]
                if reason_code is not None:
                    span.set_attribute("reason_code", reason_code)  # type: ignore[attr-defined]
                if not succeeded:
                    span.set_status("error")  # type: ignore[attr-defined]
                    if error is not None:
                        span.record_error(error)  # type: ignore[attr-defined]
            finally:
                span.__exit__(None, None, None)  # type: ignore[attr-defined]

        def reserve_transport(transport_attempt: int) -> None:
            if decision.execution_source == "online":
                span = self._tracer.span(
                    "worker.model.transport",
                    attributes={
                        "run_id": self._fence.run_id,
                        "attempt_no": self._fence.attempt_no,
                        "call_ordinal": link.call_ordinal,
                        "route_ordinal": link.route_ordinal,
                        "transport_attempt": transport_attempt,
                    },
                )
                span.__enter__()
                transport_spans[transport_attempt] = span
            try:
                reservations[transport_attempt] = self._cost.reserve_call(
                    decision=decision,
                    model_request=request,
                    deadline_utc=effective_deadline,
                    call_ordinal=link.call_ordinal,
                    route_ordinal=link.route_ordinal,
                    transport_attempt=transport_attempt,
                )
            except BaseException as error:
                close_transport_span(
                    transport_attempt,
                    succeeded=False,
                    reason_code="reserve_rejected",
                    error=error,
                )
                raise

        def observe_transport(observation: RetryAttemptResult) -> None:
            durations[observation.attempt_no] = observation.duration_ns
            try:
                if not observation.succeeded:
                    retained = reservations.pop(observation.attempt_no, None)
                    if retained is None:
                        raise IntegrityViolation(
                            "failed transport has no reserve-before-use reservation",
                            transport_attempt=observation.attempt_no,
                        )
                    self._cost.settle_failed_transport(
                        reservation=retained,
                        decision=decision,
                        wall_time_ns=observation.duration_ns,
                    )
            except BaseException as error:
                close_transport_span(
                    observation.attempt_no,
                    succeeded=False,
                    reason_code="transport_settlement_failed",
                    error=error,
                )
                raise
            close_transport_span(
                observation.attempt_no,
                succeeded=observation.succeeded,
                reason_code=(
                    None
                    if observation.classification is None
                    else observation.classification.reason_code
                ),
            )

        def reconcile_late_success(
            result: M4RouterResultV1,
            observation: RetryAttemptResult,
        ) -> None:
            retained = reservations.pop(observation.attempt_no, None)
            if retained is None:
                raise IntegrityViolation(
                    "late transport success has no reserve-before-use reservation",
                    transport_attempt=observation.attempt_no,
                )
            self._cost.reconcile_usage(
                reservation=retained,
                decision=decision,
                result=result,
                wall_time_ns=observation.duration_ns,
            )

        def cancel_transport(transport_attempt: int) -> None:
            retained = reservations.pop(transport_attempt, None)
            if retained is None:
                # Admission may fail before reserve_call returns a token. In that
                # case there is no worker-owned reservation to release and the
                # original admission error must remain authoritative.
                close_transport_span(
                    transport_attempt,
                    succeeded=False,
                    reason_code="reservation_not_created",
                )
                return
            try:
                self._cost.cancel_reservation(reservation=retained)
            except BaseException as error:
                close_transport_span(
                    transport_attempt,
                    succeeded=False,
                    reason_code="reservation_cancel_failed",
                    error=error,
                )
                raise
            close_transport_span(
                transport_attempt,
                succeeded=False,
                reason_code="transport_cancelled_before_start",
            )

        local_started_ns: int | None = None
        if decision.execution_source != "online":
            reserve_transport(1)
            local_started_ns = time.monotonic_ns()
        route_key = CassetteRouteKey(
            run_id=self._fence.run_id,
            attempt_no=self._fence.attempt_no,
            call_ordinal=link.call_ordinal,
            route_ordinal=link.route_ordinal,
            routing_decision_id=decision.decision_id,
        )
        try:
            result = self._router.call(
                request,
                decision=decision,
                deadline_utc=effective_deadline,
                recorded_at=self._now(),
                cassette_route_key=route_key,
                attempt_admission=(
                    reserve_transport if decision.execution_source == "online" else None
                ),
                attempt_cancellation=(
                    cancel_transport if decision.execution_source == "online" else None
                ),
                attempt_observer=(
                    observe_transport if decision.execution_source == "online" else None
                ),
                late_success_observer=(
                    reconcile_late_success if decision.execution_source == "online" else None
                ),
            )
        except DependencyUnavailable as error:
            if decision.execution_source != "online":
                retained = reservations.pop(1, None)
                if retained is not None:
                    self._cost.cancel_reservation(reservation=retained)
            if "breaker_state" in error.context:
                raise DependencyUnavailable(
                    "model provider circuit breaker rejected the call",
                    dependency_kind="model_provider",
                    dependency_id=f"model-provider:{decision.model_snapshot}",
                    operation_code="model.complete",
                    classifier_code="circuit_breaker_open",
                ) from error
            raise
        except BaseException:
            if decision.execution_source != "online":
                retained = reservations.pop(1, None)
                if retained is not None:
                    self._cost.cancel_reservation(reservation=retained)
            raise
        settled_attempt = result.transport_attempt_count or 1
        reservation = reservations.get(settled_attempt)
        if reservation is None:
            raise IntegrityViolation(
                "model result has no matching reserve-before-use reservation",
                transport_attempt=settled_attempt,
            )
        wall_time_ns = durations.get(settled_attempt)
        if wall_time_ns is None:
            if local_started_ns is None:
                raise IntegrityViolation("online model result omitted transport duration")
            wall_time_ns = max(0, time.monotonic_ns() - local_started_ns)
        record = (
            _record_from_result(
                request=request,
                decision=decision,
                result=result,
                recorded_at=self._now(),
            )
            if self._run.payload.llm_execution_mode == "record"
            else None
        )
        try:
            consumption = self._response_publisher.publish_response_consumption(
                fence=self._fence,
                link=link,
                decision=decision,
                result=result,
                record=record,
                reservation=reservation,
                step_cost=self._step_cost,
                step_reservation=step_reservation,
                wall_time_ns=wall_time_ns,
                actor=self._worker_actor,
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
                    "usage reconciliation also failed after response publication: "
                    f"{type(settlement_error).__name__}"
                )
            raise
        if result.execution_source == "online":
            try:
                self._router.commit_response_cache(
                    request,
                    decision=decision,
                    result=result,
                    recorded_at=self._now(),
                )
            except Exception:
                # This bounded process cache is not an authority. The response,
                # shard, usage and consumption are already atomically committed;
                # cache capacity/provenance failure cannot reverse that outcome.
                pass
        if not isinstance(consumption, RunModelResponseConsumptionV1):
            raise IntegrityViolation(
                "response publisher did not return exact consumption authority"
            )
        return result, consumption

    def _request_for_route(
        self,
        original: ModelRequestV2,
        prepared: PreparedRoute,
    ) -> ModelRequestV2:
        plan = self._run.payload.execution_version_plan
        if plan is None:
            raise IntegrityViolation("selected model route has no execution plan")
        snapshot = self.resolve_model_snapshot(
            catalog_version=plan.model_catalog_version,
            catalog_digest=plan.model_catalog_digest,
            model_snapshot_id=prepared.model_snapshot_id,
        )
        params = dict(original.params)
        has_output = "max_output_tokens" in params
        has_legacy_output = "max_tokens" in params
        if has_output and has_legacy_output:
            raise IntegrityViolation("model request has ambiguous output-token bounds")
        if not has_output and not has_legacy_output:
            params["max_output_tokens"] = prepared.max_output_tokens
        selected = original.model_copy(update={"model_snapshot": snapshot, "params": params})
        # Revalidate cross-field constraints (including prefix-cache provider scope)
        # after replacing the handler's non-authoritative initial snapshot.
        return ModelRequestV2.model_validate(selected.model_dump(mode="python"))

    def _may_fallback(
        self,
        failure: ProviderRouteFailure,
        *,
        execution_source: ExecutionSource,
    ) -> bool:
        if execution_source != "online":
            return False
        classification = failure.classification
        return (
            classification.failure_kind == "transient_infrastructure" and classification.retryable
        )

    @staticmethod
    def _project_provider_failure(
        failure: ProviderRouteFailure,
        *,
        decision: RoutingDecisionV1,
        operation_code: str = "model.complete",
        classifier_code: str | None = None,
    ) -> BaseException:
        """Seal provider transport details into the frozen worker failure vocabulary."""

        classification = failure.classification
        code = classifier_code or classification.reason_code
        if code in {"local_transport_integrity", "local_transport_request_invalid"}:
            return IntegrityViolation("model transport local request integrity failed")
        if classification.failure_kind == "quota":
            return QuotaExceeded(
                "model provider quota was exhausted",
                classifier_code=code,
            )
        response = getattr(failure.cause, "response", None)
        raw_status = getattr(response, "status_code", None)
        status = (
            raw_status
            if isinstance(raw_status, int)
            and not isinstance(raw_status, bool)
            and 100 <= raw_status <= 599
            else None
        )
        context: dict[str, object] = {
            "dependency_kind": "model_provider",
            "dependency_id": f"model-provider:{decision.model_snapshot}",
            "operation_code": operation_code,
            "classifier_code": code,
        }
        if status is not None:
            context["upstream_status_code"] = status
        if classification.retry_after_s is not None:
            context["retry_after_ms"] = classification.retry_after_s * 1000
        if classification.failure_kind == "transient_infrastructure":
            return DependencyUnavailable(
                "model provider is temporarily unavailable",
                **context,
            )
        return PermanentDependencyFailure(
            "model provider permanently rejected the operation",
            **context,
        )

    def _validate_request_plan(self, request: ModelRequestV2) -> None:
        mode = self._run.payload.llm_execution_mode
        plan = self._run.payload.execution_version_plan
        if mode == "not_applicable" or plan is None:
            raise IntegrityViolation("Run does not admit model execution")
        expected_sources = (
            {"cassette_replay"}
            if mode == "replay"
            else {"online", "full_response_cache"}
            if mode == "record"
            else {"online", "full_response_cache"}
        )
        if self._execution_source not in expected_sources:
            raise IntegrityViolation("model execution source differs from the frozen Run mode")
        node = next(
            (item for item in plan.nodes if item.agent_node_id == request.agent_node_id),
            None,
        )
        if node is None:
            raise IntegrityViolation("model request node escapes the frozen execution plan")
        model_id = canonical_model_snapshot_id(request.model_snapshot)
        if (
            request.prompt_version != node.prompt_version
            or model_id not in node.allowed_model_snapshots
        ):
            raise IntegrityViolation(
                "model request prompt/model escapes the frozen execution plan",
                agent_node_id=request.agent_node_id,
            )
        if (
            plan.model_catalog_version <= 0
            or plan.routing_policy_version <= 0
            or not plan.model_catalog_digest
            or not plan.routing_policy_digest
        ):
            raise IntegrityViolation("execution plan lacks exact routing authorities")

    def _now(self) -> datetime:
        value = self._clock.now_utc()
        if value.tzinfo is None:
            raise ValueError("worker model bridge clock must return timezone-aware UTC")
        return value.astimezone(UTC)

    def _effective_deadline(self, requested: datetime | None) -> datetime:
        try:
            authoritative = datetime.fromisoformat(
                self._attempt.attempt_deadline_utc.replace("Z", "+00:00")  # type: ignore[union-attr]
            )
        except ValueError as exc:
            raise IntegrityViolation("RunAttempt deadline is invalid") from exc
        if authoritative.tzinfo is None or authoritative.utcoffset() != UTC.utcoffset(
            authoritative
        ):
            raise IntegrityViolation("RunAttempt deadline is not UTC")
        authoritative = authoritative.astimezone(UTC)
        if requested is None:
            return authoritative
        if requested.tzinfo is None or requested.utcoffset() != UTC.utcoffset(requested):
            raise ValueError("model call deadline must be timezone-aware UTC")
        return min(authoritative, requested.astimezone(UTC))


def _record_from_result(
    *,
    request: ModelRequestV2,
    decision: RoutingDecisionV1,
    result: M4RouterResultV1,
    recorded_at: datetime,
) -> CassetteRecordV2:
    attempts = (
        result.transport_attempt_count
        if result.execution_source == "online"
        else result.recorded_transport_attempt_count
    )
    retries = (
        result.transport_retry_count
        if result.execution_source == "online"
        else result.recorded_transport_retry_count
    )
    if attempts is None or retries is None or attempts < 1 or retries != attempts - 1:
        raise IntegrityViolation("RECORD response has no exact originating transport count")
    return CassetteRecordV2(
        request_hash=request_hash(request),
        agent_node_id=request.agent_node_id,
        model_snapshot=request.model_snapshot,
        routing_decision=decision,
        response_normalized=result.response_normalized,
        raw_response=result.raw_response,
        latency=result.latency,
        token_usage=result.token_usage,
        provider_prefix_cache=result.provider_prefix_cache,
        finish_reason=result.finish_reason,
        tool_calls=result.tool_calls,
        transport_attempt_count=attempts,
        transport_retry_count=retries,
        recorded_at=recorded_at,
    )


__all__ = [
    "AgentPromptContextPublisher",
    "AgentStepCostGateway",
    "CallCostGateway",
    "ExecutionSource",
    "ModelCallRequest",
    "ModelCallResult",
    "ModelSnapshotResolver",
    "PromptRenderPublisher",
    "ResponseConsumptionPublisher",
    "RoutingDecider",
    "WorkerModelBridge",
]
