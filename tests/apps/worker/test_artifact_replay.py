from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from gameforge.apps.worker.replay import (
    ArtifactReplayLoader,
    LegacyArtifactReplaySource,
    NativeArtifactReplaySource,
    NativeReplayCallPlan,
)
from gameforge.apps.worker.artifact_replay_bridge import ArtifactReplayModelBridge
from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecordV2
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.jobs import RunRecord, canonical_payload_hash
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.model_router import Message, ModelRequestV1
from gameforge.runtime.cassette.store import CassetteRouteKey
from gameforge.runtime.model_router.cache import ExactResponseCache
from gameforge.runtime.model_router.m4_router import M4ModelRouter
from gameforge.runtime.model_router.router import CassetteReplayMiss, RouterMode
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from tests.platform.m4c.test_replay_admission import (
    _legacy_verified_fixture,
    _native_fixture,
    _native_record_fixture,
    _native_retry_with_empty_terminal_attempt_fixture,
    _zero_attempt_native_fixture,
)


NOW = datetime(2026, 7, 16, tzinfo=UTC)


class _BombTransport:
    def complete(self, request):
        del request
        raise AssertionError("REPLAY must not call an online provider")

    def complete_with_timeout(self, request, *, timeout_s: float):
        del request, timeout_s
        raise AssertionError("REPLAY must not call an online provider")


class _DecisionAuthority:
    def __init__(self) -> None:
        self.decisions = {}

    def get_routing_decision(self, decision_id: str):
        return self.decisions.get(decision_id)

    def put(self, decision) -> None:
        self.decisions[decision.decision_id] = decision


class _MutatingBlobReader:
    def __init__(self, delegate, *, artifact_id: str) -> None:
        self._delegate = delegate
        self._artifact_id = artifact_id
        self.read_count = 0

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def read_artifact_bytes(self, artifact_id: str) -> bytes:
        if artifact_id == self._artifact_id:
            self.read_count += 1
            if self.read_count > 1:
                return b"{}"
        return self._delegate.read_artifact_bytes(artifact_id)


class _DisappearingRouteReader:
    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.read_count = 0

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def get_model_route_link(self, *args):
        self.read_count += 1
        if self.read_count > 1:
            return None
        return self._delegate.get_model_route_link(*args)


def _active_replay_run(source: RunRecord, *, payload) -> RunRecord:
    value = source.model_dump(mode="python")
    value.update(
        run_id="run:replay:worker",
        status="running",
        revision=2,
        idempotency_key="replay-worker",
        payload=payload,
        payload_hash=canonical_payload_hash(payload),
        current_attempt_no=1,
        next_attempt_no=2,
        next_fencing_token=2,
        next_event_seq=3,
        budget_set_snapshot_id=payload.budget_set_snapshot_id,
        run_budget_hold_group_id="hold:replay-worker",
        result_artifact_id=None,
        failure_artifact_id=None,
        terminal_cassette_artifact_id=None,
    )
    return RunRecord.model_validate(value)


def _route_key(decision, route, *, call_ordinal: int | None = None) -> CassetteRouteKey:
    return CassetteRouteKey(
        run_id=decision.run_id,
        attempt_no=decision.attempt_no,
        call_ordinal=route.call_ordinal if call_ordinal is None else call_ordinal,
        route_ordinal=route.route_ordinal,
        routing_decision_id=decision.decision_id,
    )


def _native_source(*, fallback_index: int = 0):
    fixture = _native_record_fixture(fallback_index=fallback_index)
    run = _active_replay_run(fixture.source, payload=fixture.replay_payload)
    decisions = _DecisionAuthority()
    source = ArtifactReplayLoader(
        fixture.reader,
        current_decision_resolver=decisions.get_routing_decision,
    ).load(run)
    assert isinstance(source, NativeArtifactReplaySource)
    plan = source.call_plan(attempt_no=1, call_ordinal=1)
    assert plan is not None
    route = plan.consumed_route
    assert route is not None
    decision = source.project_current_decision(
        route,
        attempt_no=1,
        decided_at=NOW,
    )
    decisions.put(decision)
    return fixture, run, decisions, source, route, decision


def test_native_artifact_source_drives_real_m4_replay_without_transport() -> None:
    _, _, decisions, source, route, decision = _native_source()
    router = M4ModelRouter(
        transport=_BombTransport(),
        store=source,
        cache=ExactResponseCache(),
        mode=RouterMode.REPLAY,
        retry_executor=object(),
        decision_authority=decisions,
    )

    result = router.call(
        route.request,
        decision=decision,
        cassette_route_key=_route_key(decision, route),
        deadline_utc=NOW + timedelta(seconds=10),
        recorded_at=NOW,
    )

    assert result.response_normalized == "reviewed"
    assert result.execution_source == "cassette_replay"
    assert result.routing_decision_kind == "native"
    assert result.transport_attempt_count == 0
    assert result.recorded_transport_attempt_count == 1
    assert (
        router.call(
            route.request,
            decision=decision,
            cassette_route_key=_route_key(decision, route),
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )
        == result
    )


def test_native_source_selection_is_independent_of_current_replay_attempt() -> None:
    fixture = _native_record_fixture()
    run = _active_replay_run(fixture.source, payload=fixture.replay_payload)
    decisions = _DecisionAuthority()
    source = ArtifactReplayLoader(
        fixture.reader,
        current_decision_resolver=decisions.get_routing_decision,
    ).load(run)
    assert isinstance(source, NativeArtifactReplaySource)

    plan = source.call_plan(attempt_no=2, call_ordinal=1)
    assert plan is not None
    assert source.selected_source_attempt_no == plan.attempt_no == 1
    route = plan.consumed_route
    assert route is not None
    decision = source.project_current_decision(
        route,
        attempt_no=2,
        decided_at=NOW,
    )
    decisions.put(decision)

    assert decision.attempt_no == 2
    assert isinstance(source.replay_native(_route_key(decision, route)), CassetteRecordV2)


def test_native_source_preserves_prior_attempts_but_executes_only_terminal_attempt() -> None:
    fixture = _native_retry_with_empty_terminal_attempt_fixture()
    run = _active_replay_run(fixture.source, payload=fixture.replay_payload)
    source = ArtifactReplayLoader(
        fixture.reader,
        current_decision_resolver=lambda _: None,
    ).load(run)
    assert isinstance(source, NativeArtifactReplaySource)

    assert source.call_count == 1
    assert source.attempt_numbers == (1,)
    assert source.selected_source_attempt_no == 2
    assert source.call_plan(attempt_no=1, call_ordinal=1) is None


def test_native_source_rejects_a_route_after_the_consumed_response() -> None:
    _, _, _, _, route, _ = _native_source()
    impossible_route = replace(
        route,
        invocation=route.invocation.model_copy(
            update={
                "route_ordinal": 2,
                "transport_attempt": None,
                "routing_decision_id": "decision:impossible-after-consumption",
                "execution_source": "full_response_cache",
                "response_consumed": False,
            }
        ),
        record=None,
    )
    plan = NativeReplayCallPlan(
        attempt_no=1,
        call_ordinal=1,
        routes=(route, impossible_route),
    )

    with pytest.raises(IntegrityViolation, match="route after its consumed response"):
        _ = plan.consumed_route


def test_replay_prompt_uses_handler_source_not_cassette_bundle() -> None:
    _, run, _, _, route, _ = _native_source()
    source_artifact_ids = tuple(
        artifact_id
        for artifact_id in run.payload.input_artifact_ids
        if artifact_id != run.payload.cassette_artifact_id
    )
    seen: dict[str, object] = {}

    class Publisher:
        def publish_prompt_rendered(self, request, **values):
            seen["request"] = request
            seen.update(values)
            return SimpleNamespace(link="published-link")

    bridge = object.__new__(ArtifactReplayModelBridge)
    bridge._run = run
    bridge._attempt = SimpleNamespace(attempt_no=1)
    bridge._fence = AttemptWriteFence(
        run_id=run.run_id,
        attempt_no=1,
        expected_run_revision=run.revision,
        lease_id="lease:replay-source",
        fencing_token=1,
    )
    bridge._prompt_publisher = Publisher()
    bridge._actor = AuditActor(principal_id="service:worker", principal_kind="service")
    bridge._next_call_ordinal = 1

    retained = bridge._publish_prompt(
        route.request,
        source_artifact_ids=source_artifact_ids,
        logical_call_ordinal=None,
        route_ordinal=1,
    )

    assert retained == "published-link"
    assert seen["source_artifact_ids"] == source_artifact_ids
    assert run.payload.cassette_artifact_id not in seen["source_artifact_ids"]


def test_native_route_ordinal_is_explicit_not_fallback_index_plus_one() -> None:
    _, run, decisions, source, route, decision = _native_source(fallback_index=1)

    assert route.route_ordinal == 1
    assert route.source_decision.fallback_index == 1
    assert decision.run_id == run.run_id
    assert decision.budget_set_snapshot_id == run.budget_set_snapshot_id
    assert decision.execution_source == "cassette_replay"
    assert decision.reason_code == "recorded_replay"
    assert decision.fallback_index == route.source_decision.fallback_index
    assert decision.policy_version == route.source_decision.policy_version
    assert decision.catalog_digest == route.source_decision.catalog_digest
    record = source.replay_native(_route_key(decision, route))

    assert isinstance(record, CassetteRecordV2)
    assert decisions.get_routing_decision(decision.decision_id) == decision


def test_native_replay_rejects_current_route_drift_and_extra_calls() -> None:
    _, _, decisions, source, route, decision = _native_source()
    decisions.put(decision.model_copy(update={"catalog_digest": "f" * 64}))

    with pytest.raises(IntegrityViolation, match="differs from source route"):
        source.replay_native(_route_key(decision, route))

    decisions.put(decision)
    assert source.replay_native(_route_key(decision, route, call_ordinal=2)) is CASSETTE_MISS
    with pytest.raises(CassetteReplayMiss):
        M4ModelRouter(
            transport=_BombTransport(),
            store=source,
            cache=ExactResponseCache(),
            mode=RouterMode.REPLAY,
            retry_executor=object(),
            decision_authority=decisions,
        ).call(
            route.request,
            decision=decision,
            cassette_route_key=_route_key(decision, route, call_ordinal=2),
            deadline_utc=NOW + timedelta(seconds=10),
            recorded_at=NOW,
        )


def test_native_replay_lookup_is_stateless_and_immutable() -> None:
    _, _, _, source, route, decision = _native_source()
    key = _route_key(decision, route)

    first = source.replay_native(key)
    second = source.replay_native(key)

    assert isinstance(first, CassetteRecordV2)
    assert second == first
    with pytest.raises(IntegrityViolation, match="immutable"):
        source.record_native(key, first)


def test_native_replay_is_rebuilt_from_artifact_authority_after_restart() -> None:
    fixture = _native_record_fixture()
    run = _active_replay_run(fixture.source, payload=fixture.replay_payload)
    decisions = _DecisionAuthority()
    loader = ArtifactReplayLoader(
        fixture.reader,
        current_decision_resolver=decisions.get_routing_decision,
    )

    first = loader.load(run)
    second = loader.load(run)

    assert isinstance(first, NativeArtifactReplaySource)
    assert isinstance(second, NativeArtifactReplaySource)
    assert first is not second
    assert first.proof == second.proof
    assert first.call_count == second.call_count == 1


def test_worker_reread_rejects_artifact_bytes_changed_after_admission_proof() -> None:
    fixture = _native_record_fixture()
    run = _active_replay_run(fixture.source, payload=fixture.replay_payload)
    reader = _MutatingBlobReader(fixture.reader, artifact_id=fixture.root.artifact_id)

    with pytest.raises(IntegrityViolation, match="ObjectRef/hash"):
        ArtifactReplayLoader(
            reader,
            current_decision_resolver=lambda _: None,
        ).load(run)

    assert reader.read_count == 2


def test_worker_reread_rejects_route_authority_disappearing_after_admission() -> None:
    fixture = _native_record_fixture()
    run = _active_replay_run(fixture.source, payload=fixture.replay_payload)
    reader = _DisappearingRouteReader(fixture.reader)

    with pytest.raises(IntegrityViolation, match="explicit route authority"):
        ArtifactReplayLoader(
            reader,
            current_decision_resolver=lambda _: None,
        ).load(run)

    assert reader.read_count == 2


@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out"])
def test_native_zero_call_terminal_source_is_complete_and_never_fabricates_call(
    status: str,
) -> None:
    fixture = _zero_attempt_native_fixture(status)
    run = _active_replay_run(fixture.source, payload=fixture.replay_payload)
    source = ArtifactReplayLoader(
        fixture.reader,
        current_decision_resolver=lambda _: None,
    ).load(run)
    assert isinstance(source, NativeArtifactReplaySource)

    assert source.call_count == 0
    assert (
        source.replay_native(
            CassetteRouteKey(
                run_id=run.run_id,
                attempt_no=1,
                call_ordinal=1,
                route_ordinal=1,
                routing_decision_id="decision:absent",
            )
        )
        is CASSETTE_MISS
    )


def _legacy_source(*, include_authority: bool = True):
    fixture = _legacy_verified_fixture()
    base = _native_fixture().source
    run = _active_replay_run(base, payload=fixture.replay_payload)

    def reject_native_resolution(_: str):
        raise AssertionError("verified legacy replay must not enter native routing")

    loader = ArtifactReplayLoader(
        fixture.reader,
        current_decision_resolver=reject_native_resolution,
        legacy_authority=fixture.authority if include_authority else None,
        legacy_decisions=fixture.decisions if include_authority else None,
    )
    return fixture, run, loader


def test_verified_legacy_replay_stays_on_legacy_authority_path() -> None:
    fixture, run, loader = _legacy_source()
    source = loader.load(run)
    assert isinstance(source, LegacyArtifactReplaySource)
    request = next(iter(fixture.authority.rendered_requests.values()))

    planned = source.plan(request, call_ordinal=1)
    result = source.replay(request, call_ordinal=1)

    assert planned.request == request
    assert result.response_normalized == "historical review"
    assert result.routing_decision_kind == "legacy_import"
    assert result.execution_source == "cassette_replay"
    assert result.transport_attempt_count == 0
    assert result.invocation == planned.invocation
    assert source.replay(request, call_ordinal=1) == result


def test_verified_legacy_replay_rejects_wrong_request_and_absent_call() -> None:
    fixture, run, loader = _legacy_source()
    source = loader.load(run)
    assert isinstance(source, LegacyArtifactReplaySource)
    request = next(iter(fixture.authority.rendered_requests.values()))
    assert isinstance(request, ModelRequestV1)
    changed = request.model_copy(update={"messages": [Message(role="user", content="different")]})

    with pytest.raises(IntegrityViolation, match="ordinal is absent"):
        source.replay(request, call_ordinal=2)
    with pytest.raises(IntegrityViolation, match="retained rendered request"):
        source.replay(changed, call_ordinal=1)
    assert source.replay(request, call_ordinal=1).routing_decision_kind == "legacy_import"


def test_legacy_replay_without_retained_runtime_authority_fails_closed() -> None:
    _, run, loader = _legacy_source(include_authority=False)

    with pytest.raises(DependencyUnavailable, match="authority is unavailable"):
        loader.load(run)


def test_legacy_replay_without_retained_decision_fails_closed() -> None:
    fixture, run, _ = _legacy_source()
    fixture.decisions.decisions.clear()
    loader = ArtifactReplayLoader(
        fixture.reader,
        current_decision_resolver=lambda _: None,
        legacy_authority=fixture.authority,
        legacy_decisions=fixture.decisions,
    )

    with pytest.raises(IntegrityViolation, match="decision is not retained"):
        loader.load(run)
