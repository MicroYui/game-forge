from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gameforge.contracts.cassette import CassetteRecordV2
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.runtime.cassette.store import CassetteRouteKey, CassetteStore


NOW = datetime(2026, 7, 14, tzinfo=UTC)
REQUEST_HASH = "sha256:" + "1" * 64


def _record() -> CassetteRecordV2:
    snapshot = ModelSnapshot(
        provider="openai",
        model="gpt-5.6-sol",
        snapshot_tag="2026-07-14",
    )
    decision = RoutingDecisionV1.create(
        run_id="run-1",
        attempt_no=1,
        request_hash=REQUEST_HASH,
        rule_id="repair",
        model_snapshot=canonical_model_snapshot_id(snapshot),
        tier="best",
        reason_code="primary_rule",
        budget_set_snapshot_id="budget-set-1",
        fallback_from=None,
        fallback_index=0,
        policy_version=1,
        routing_policy_digest="2" * 64,
        catalog_version=1,
        catalog_digest="3" * 64,
        execution_source="online",
        decided_at=NOW,
    )
    return CassetteRecordV2(
        request_hash=REQUEST_HASH,
        agent_node_id="repair",
        model_snapshot=snapshot,
        routing_decision=decision,
        response_normalized="fixed",
        raw_response={"id": "response-1"},
        latency=LatencyObservationV1(status="reported", provider_latency_ms=120),
        token_usage=TokenUsageObservationV1(
            status="reported", input_tokens=10, output_tokens=2, total_tokens=12
        ),
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=True),
        finish_reason="stop",
        tool_calls=(),
        transport_attempt_count=1,
        transport_retry_count=0,
        recorded_at=NOW,
    )


def _route_key(record: CassetteRecordV2, *, call_ordinal: int = 1) -> CassetteRouteKey:
    return CassetteRouteKey(
        run_id=record.routing_decision.run_id,
        attempt_no=record.routing_decision.attempt_no,
        call_ordinal=call_ordinal,
        route_ordinal=1,
        routing_decision_id=record.routing_decision.decision_id,
    )


def test_store_round_trips_v2_by_explicit_discriminator_and_hash(tmp_path) -> None:
    store = CassetteStore(tmp_path)
    record = _record()
    store.record(record.request_hash, record)
    assert store.replay(record.request_hash) == record


def test_store_rejects_record_key_disagreement(tmp_path) -> None:
    with pytest.raises(IntegrityViolation, match="request hash"):
        CassetteStore(tmp_path).record("sha256:" + "f" * 64, _record())


def test_store_rejects_unknown_discriminator_in_existing_wire(tmp_path) -> None:
    record = _record()
    path = tmp_path / f"{record.request_hash.split(':', 1)[1]}.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text('{"cassette_schema_version":"cassette@999"}', encoding="utf-8")
    with pytest.raises(IntegrityViolation, match="unsupported"):
        CassetteStore(tmp_path).replay(record.request_hash)


def test_native_store_is_route_scoped_and_immutable(tmp_path) -> None:
    store = CassetteStore(tmp_path)
    record = _record()
    first = _route_key(record, call_ordinal=1)
    second = _route_key(record, call_ordinal=2)

    store.record_native(first, record)
    store.record_native(second, record)

    assert store.replay_native(first) == record
    assert store.replay_native(second) == record
    assert len(tuple((tmp_path / "native").glob("*.json"))) == 2

    conflicting = record.model_copy(update={"response_normalized": "different"})
    with pytest.raises(IntegrityViolation, match="conflicting content"):
        store.record_native(first, conflicting)


def test_native_store_rejects_route_key_decision_mismatch(tmp_path) -> None:
    record = _record()
    mismatched = _route_key(record).model_copy(
        update={"routing_decision_id": "routing-decision:sha256:" + "f" * 64}
    )

    with pytest.raises(IntegrityViolation, match="route key"):
        CassetteStore(tmp_path).record_native(mismatched, record)
