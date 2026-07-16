"""Agent-step admission around native and verified-legacy Artifact REPLAY."""

from __future__ import annotations

from datetime import timedelta

import pytest

from gameforge.apps.worker.artifact_replay_bridge import ArtifactReplayModelBridge
from gameforge.apps.worker.model_bridge import ModelCallRequest
from gameforge.apps.worker.replay import (
    ArtifactReplayLoader,
    LegacyArtifactReplaySource,
    NativeArtifactReplaySource,
)
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.jobs import RunAttempt, RunIntermediateArtifactLinkV1
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.runs.commands import PromptRenderPublicationResult
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.runtime.model_router.cache import ExactResponseCache
from gameforge.runtime.model_router.m4_router import M4ModelRouter
from gameforge.runtime.model_router.router import RouterMode
from gameforge.runtime.observability import Tracer
from tests.apps.worker.test_artifact_replay import (
    NOW,
    _BombTransport,
    _DecisionAuthority,
    _active_replay_run,
    _legacy_source,
    _native_source,
)
from tests.platform.m4c.test_replay_admission import (
    _native_retry_with_empty_terminal_attempt_fixture,
)


WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")


class _PromptPublisher:
    def __init__(self, order: list[str], *, error: BaseException | None = None) -> None:
        self._order = order
        self._error = error

    def publish_prompt_rendered(self, request, **values):
        del values
        self._order.append("publish_prompt")
        if self._error is not None:
            raise self._error
        return PromptRenderPublicationResult(
            link=RunIntermediateArtifactLinkV1(
                run_id=request.fence.run_id,
                attempt_no=request.fence.attempt_no,
                call_ordinal=request.logical_call_ordinal,
                route_ordinal=request.route_ordinal,
                artifact_id=request.artifact_id,
                role="prompt_rendered",
                request_hash=request.request_hash,
                fencing_token=request.fence.fencing_token,
                published_at="2026-07-16T00:00:00Z",
            ),
            replayed=False,
        )


class _RoutePublisher:
    def __init__(self, order: list[str], decision_authority=None) -> None:
        self._order = order
        self._decision_authority = decision_authority

    def publish(self, **values):
        self._order.append("publish_route")
        if self._decision_authority is not None:
            self._decision_authority.put(values["decision"])
        return values


class _CallCost:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.reservations: list[object] = []

    def reserve_call(self, **values):
        self._order.append("reserve_call")
        self.reservations.append(values)
        return values

    def cancel_reservation(self, **values) -> None:
        del values
        self._order.append("cancel_call")

    def reconcile_usage(self, **values) -> None:
        del values
        self._order.append("reconcile_call")

    def settle_failed_transport(self, **values) -> None:
        del values
        self._order.append("settle_call")


class _StepCost:
    def __init__(
        self,
        order: list[str],
        *,
        reserve_error: BaseException | None = None,
        reconcile_error: BaseException | None = None,
    ) -> None:
        self._order = order
        self._reserve_error = reserve_error
        self._reconcile_error = reconcile_error
        self.reservations: list[object] = []
        self.reconciliations: list[object] = []
        self.in_transaction: list[object] = []
        self.standalone: list[object] = []

    def reserve_step(self, **values):
        self._order.append("reserve_step")
        if self._reserve_error is not None:
            raise self._reserve_error
        self.reservations.append(values)
        return values

    def reconcile_step(self, **values) -> None:
        self._order.append("reconcile_step")
        self.reconciliations.append(values)
        self.standalone.append(values["reservation"])
        if self._reconcile_error is not None:
            raise self._reconcile_error

    def reconcile_step_in_transaction(self, **values) -> object:
        self._order.append("reconcile_step")
        self.reconciliations.append(values)
        self.in_transaction.append(values["reservation"])
        if self._reconcile_error is not None:
            raise self._reconcile_error
        return type("Settlement", (), {"status": "reconciled"})()


class _ResponsePublisher:
    def __init__(self, order: list[str], *, error: BaseException | None = None) -> None:
        self._order = order
        self._error = error

    def publish_response_consumption(self, **values) -> None:
        self._order.append("publish_response")
        if self._error is not None:
            raise self._error
        values["step_cost"].reconcile_step_in_transaction(
            transaction=self,
            reservation=values["step_reservation"],
        )


class _Resolver:
    def resolve_model_snapshot(self, **values):
        raise AssertionError(f"replay bridge unexpectedly resolved a model: {values}")


class _TraceExporter:
    def __init__(self) -> None:
        self.spans = []

    def export(self, spans) -> None:
        self.spans.extend(spans)


class _TickingMonotonicClock:
    def __init__(self) -> None:
        self.value = 0

    def now_ns(self) -> int:
        self.value += 11
        return self.value


class _UtcClock:
    def now_utc(self):
        return NOW


def _tracer(exporter: _TraceExporter | None = None) -> Tracer:
    return Tracer(
        exporter=exporter or _TraceExporter(),
        utc_clock=_UtcClock(),
        monotonic_clock=_TickingMonotonicClock(),
    )


def _attempt_and_fence(run, *, attempt_no: int = 1):
    attempt = RunAttempt(
        run_id=run.run_id,
        attempt_no=attempt_no,
        status="running",
        fencing_token=attempt_no,
        worker_principal_id=WORKER.principal_id,
        next_call_ordinal=1,
        started_at="2026-07-16T00:00:00Z",
        attempt_deadline_utc="2026-07-16T00:30:00Z",
    )
    fence = AttemptWriteFence(
        run_id=run.run_id,
        attempt_no=attempt_no,
        expected_run_revision=run.revision,
        lease_id=f"lease:replay:{attempt_no}",
        fencing_token=attempt_no,
    )
    return attempt, fence


def _request(run, model_request, *, attempt_no: int = 1) -> ModelCallRequest:
    return ModelCallRequest(
        model_request=model_request,
        source_artifact_ids=(run.payload.input_artifact_ids[0],),
        idempotency_scope=f"run:{run.run_id}:attempt:{attempt_no}",
        idempotency_key="model:1",
        deadline_utc=NOW + timedelta(minutes=1),
    )


def _native_bridge(
    order,
    *,
    prompt=None,
    step=None,
    response=None,
    attempt_no: int = 1,
    tracer: Tracer | None = None,
):
    _, run, decisions, source, route, _ = _native_source()
    if attempt_no != run.current_attempt_no:
        run = run.model_copy(
            update={
                "current_attempt_no": attempt_no,
                "next_attempt_no": attempt_no + 1,
                "next_fencing_token": attempt_no + 1,
            }
        )
    attempt, fence = _attempt_and_fence(run, attempt_no=attempt_no)
    call_cost = _CallCost(order)
    step_cost = step or _StepCost(order)
    bridge = ArtifactReplayModelBridge(
        run=run,
        attempt=attempt,
        fence=fence,
        source=source,
        prompt_publisher=prompt or _PromptPublisher(order),
        route_publisher=_RoutePublisher(order, decisions),
        native_router=M4ModelRouter(
            transport=_BombTransport(),
            store=source,
            cache=ExactResponseCache(),
            mode=RouterMode.REPLAY,
            retry_executor=object(),
            decision_authority=decisions,
        ),
        cost=call_cost,
        step_cost=step_cost,
        response_publisher=response or _ResponsePublisher(order),
        model_snapshot_resolver=_Resolver(),
        tracer=tracer or _tracer(),
        clock=type("Clock", (), {"now_utc": lambda self: NOW})(),
        worker_actor=WORKER,
    )
    return bridge, run, route, call_cost, step_cost


def test_native_replay_charges_one_step_before_route_and_response() -> None:
    order: list[str] = []
    bridge, run, route, call_cost, step_cost = _native_bridge(order)

    result = bridge.call_model(_request(run, route.request))

    assert result.response.execution_source == "cassette_replay"
    assert order == [
        "publish_prompt",
        "reserve_step",
        "publish_route",
        "reserve_call",
        "publish_response",
        "reconcile_step",
    ]
    assert len(call_cost.reservations) == 1
    assert len(step_cost.reservations) == len(step_cost.reconciliations) == 1
    assert step_cost.in_transaction == step_cost.reservations
    assert step_cost.standalone == []


def test_native_replay_attempt_two_restarts_selected_source_call_one() -> None:
    order: list[str] = []
    bridge, run, route, _, _ = _native_bridge(order, attempt_no=2)

    result = bridge.call_model(_request(run, route.request, attempt_no=2))

    assert result.link.attempt_no == result.decision.attempt_no == 2
    assert result.link.call_ordinal == 1
    assert result.response.execution_source == "cassette_replay"


def test_verified_legacy_replay_charges_one_step_without_native_route_selection() -> None:
    fixture, run, loader = _legacy_source()
    source = loader.load(run)
    assert isinstance(source, LegacyArtifactReplaySource)
    planned = source.expected_call(call_ordinal=1)
    attempt, fence = _attempt_and_fence(run)
    order: list[str] = []
    step_cost = _StepCost(order)
    bridge = ArtifactReplayModelBridge(
        run=run,
        attempt=attempt,
        fence=fence,
        source=source,
        prompt_publisher=_PromptPublisher(order),
        route_publisher=_RoutePublisher(order),
        native_router=None,
        cost=_CallCost(order),
        step_cost=step_cost,
        response_publisher=_ResponsePublisher(order),
        model_snapshot_resolver=_Resolver(),
        tracer=_tracer(),
        clock=type("Clock", (), {"now_utc": lambda self: NOW})(),
        worker_actor=WORKER,
    )

    result = bridge.call_model(_request(run, planned.request))

    assert result.response.execution_source == "cassette_replay"
    assert result.decision == planned.routing_decision
    assert order == [
        "publish_prompt",
        "reserve_step",
        "publish_route",
        "reserve_call",
        "publish_response",
        "reconcile_step",
    ]
    assert len(step_cost.reservations) == len(step_cost.reconciliations) == 1
    assert step_cost.in_transaction == step_cost.reservations
    assert step_cost.standalone == []
    assert fixture.authority is not None


def test_verified_legacy_replay_attempt_two_restarts_imported_call_one() -> None:
    _, run, loader = _legacy_source()
    source = loader.load(run)
    assert isinstance(source, LegacyArtifactReplaySource)
    planned = source.expected_call(call_ordinal=1)
    run = run.model_copy(
        update={
            "current_attempt_no": 2,
            "next_attempt_no": 3,
            "next_fencing_token": 3,
        }
    )
    attempt, fence = _attempt_and_fence(run, attempt_no=2)
    order: list[str] = []
    bridge = ArtifactReplayModelBridge(
        run=run,
        attempt=attempt,
        fence=fence,
        source=source,
        prompt_publisher=_PromptPublisher(order),
        route_publisher=_RoutePublisher(order),
        native_router=None,
        cost=_CallCost(order),
        step_cost=_StepCost(order),
        response_publisher=_ResponsePublisher(order),
        model_snapshot_resolver=_Resolver(),
        tracer=_tracer(),
        clock=_UtcClock(),
        worker_actor=WORKER,
    )

    result = bridge.call_model(_request(run, planned.request, attempt_no=2))

    assert result.link.attempt_no == 2
    assert result.link.call_ordinal == 1
    assert result.decision == planned.routing_decision


def test_native_replay_rejects_call_when_terminal_source_attempt_has_no_calls() -> None:
    fixture = _native_retry_with_empty_terminal_attempt_fixture()
    run = _active_replay_run(fixture.source, payload=fixture.replay_payload)
    decisions = _DecisionAuthority()
    source = ArtifactReplayLoader(
        fixture.reader,
        current_decision_resolver=decisions.get_routing_decision,
    ).load(run)
    assert isinstance(source, NativeArtifactReplaySource)
    earlier_request = next(iter(source._calls.values())).consumed_route
    assert earlier_request is not None
    attempt, fence = _attempt_and_fence(run)
    order: list[str] = []
    bridge = ArtifactReplayModelBridge(
        run=run,
        attempt=attempt,
        fence=fence,
        source=source,
        prompt_publisher=_PromptPublisher(order),
        route_publisher=_RoutePublisher(order, decisions),
        native_router=M4ModelRouter(
            transport=_BombTransport(),
            store=source,
            cache=ExactResponseCache(),
            mode=RouterMode.REPLAY,
            retry_executor=object(),
            decision_authority=decisions,
        ),
        cost=_CallCost(order),
        step_cost=_StepCost(order),
        response_publisher=_ResponsePublisher(order),
        model_snapshot_resolver=_Resolver(),
        tracer=_tracer(),
        clock=_UtcClock(),
        worker_actor=WORKER,
    )

    with pytest.raises(IntegrityViolation, match="absent from source authority"):
        bridge.call_model(_request(run, earlier_request.request))

    assert order == []


def test_native_replay_emits_only_local_logical_call_span() -> None:
    order: list[str] = []
    exporter = _TraceExporter()
    bridge, run, route, _, _ = _native_bridge(
        order,
        tracer=_tracer(exporter),
    )

    bridge.call_model(_request(run, route.request))

    assert [span.name for span in exporter.spans] == ["worker.model.call"]
    span = exporter.spans[0]
    assert span.duration_ns == 11
    assert span.attributes["execution_source"] == "cassette_replay"
    assert span.attributes["recorded_provider_latency_ms"] == 10
    assert "transport_attempt" not in span.attributes
    assert "provider_latency_ms" not in span.attributes


def test_legacy_replay_emits_only_local_logical_call_span() -> None:
    _, run, loader = _legacy_source()
    source = loader.load(run)
    assert isinstance(source, LegacyArtifactReplaySource)
    planned = source.expected_call(call_ordinal=1)
    attempt, fence = _attempt_and_fence(run)
    order: list[str] = []
    exporter = _TraceExporter()
    bridge = ArtifactReplayModelBridge(
        run=run,
        attempt=attempt,
        fence=fence,
        source=source,
        prompt_publisher=_PromptPublisher(order),
        route_publisher=_RoutePublisher(order),
        native_router=None,
        cost=_CallCost(order),
        step_cost=_StepCost(order),
        response_publisher=_ResponsePublisher(order),
        model_snapshot_resolver=_Resolver(),
        tracer=_tracer(exporter),
        clock=_UtcClock(),
        worker_actor=WORKER,
    )

    bridge.call_model(_request(run, planned.request))

    assert [span.name for span in exporter.spans] == ["worker.model.call"]
    assert exporter.spans[0].duration_ns == 11
    assert exporter.spans[0].attributes["recorded_provider_latency_ms"] == 42
    assert "provider_latency_ms" not in exporter.spans[0].attributes


def test_replay_stale_prompt_never_reserves_step_or_call() -> None:
    order: list[str] = []
    bridge, run, route, call_cost, step_cost = _native_bridge(
        order,
        prompt=_PromptPublisher(order, error=Conflict("stale prompt fence")),
    )

    with pytest.raises(Conflict, match="stale prompt fence"):
        bridge.call_model(_request(run, route.request))

    assert order == ["publish_prompt"]
    assert step_cost.reservations == []
    assert call_cost.reservations == []


def test_replay_step_rejection_prevents_route_and_call() -> None:
    order: list[str] = []
    step_cost = _StepCost(order, reserve_error=Conflict("step fence lost"))
    bridge, run, route, call_cost, _ = _native_bridge(order, step=step_cost)

    with pytest.raises(Conflict, match="step fence lost"):
        bridge.call_model(_request(run, route.request))

    assert order == ["publish_prompt", "reserve_step"]
    assert call_cost.reservations == []


def test_replay_publication_and_step_settlement_double_failure_preserves_primary() -> None:
    order: list[str] = []
    step_cost = _StepCost(
        order,
        reconcile_error=RuntimeError("step ledger unavailable"),
    )
    bridge, run, route, _, _ = _native_bridge(
        order,
        step=step_cost,
        response=_ResponsePublisher(order, error=Conflict("response fence lost")),
    )

    with pytest.raises(Conflict, match="response fence lost") as captured:
        bridge.call_model(_request(run, route.request))

    assert order[-3:] == ["publish_response", "reconcile_call", "reconcile_step"]
    assert any("agent-step reconciliation also failed" in note for note in captured.value.__notes__)
