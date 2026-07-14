from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cassette import (
    CassetteRecordV1,
    CassetteRecordV2,
    cassette_observation_view,
    parse_cassette_record,
)
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.model_router import (
    Message,
    ModelRequestV1,
    ModelRequestV2,
    ModelResponse,
    ModelSnapshot,
    PrefixCacheDirectiveV1,
    compute_prefix_hash,
    parse_model_request,
    request_hash,
)
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    RoutingDecisionV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
)


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def _snapshot() -> ModelSnapshot:
    return ModelSnapshot(provider="openai", model="gpt-5.6-sol", snapshot_tag="2026-07-14")


def _messages() -> list[Message]:
    return [
        Message(role="system", content="stable KG prefix"),
        Message(role="user", content="repair this finding"),
    ]


def _decision(*, source: str = "online") -> RoutingDecisionV1:
    snapshot_id = canonical_model_snapshot_id(_snapshot())
    return RoutingDecisionV1.create(
        run_id="run-1",
        attempt_no=1,
        request_hash="sha256:" + "1" * 64,
        rule_id="repair-default",
        model_snapshot=snapshot_id,
        tier="best",
        reason_code="primary_rule",
        budget_set_snapshot_id="budget-set-1",
        fallback_from=None,
        fallback_index=0,
        policy_version=1,
        routing_policy_digest="2" * 64,
        catalog_version=1,
        catalog_digest="3" * 64,
        execution_source=source,
        decided_at=NOW,
    )


def test_v2_prefix_directive_does_not_change_semantic_request_hash() -> None:
    messages = _messages()
    v1 = ModelRequestV1(
        model_snapshot=_snapshot(),
        messages=messages,
        params={"temperature": 0},
        agent_node_id="repair",
        prompt_version="repair@2",
        cache_key="legacy-hint",
    )
    v2 = ModelRequestV2(
        model_snapshot=_snapshot(),
        messages=messages,
        params={"temperature": 0},
        agent_node_id="repair",
        prompt_version="repair@2",
        prefix_cache_directive=PrefixCacheDirectiveV1(
            prefix_message_count=1,
            prefix_hash=compute_prefix_hash(messages[:1]),
            provider_scope="openai",
            policy_version="prefix-policy@1",
        ),
    )

    assert request_hash(v1) == request_hash(v2)
    assert parse_model_request(v1.model_dump()).model_router_schema_version == "model-router@1"
    assert parse_model_request(v2.model_dump()).model_router_schema_version == "model-router@2"


def test_v2_prefix_must_equal_the_exact_message_prefix() -> None:
    with pytest.raises(ValidationError, match="prefix_hash"):
        ModelRequestV2(
            model_snapshot=_snapshot(),
            messages=_messages(),
            agent_node_id="repair",
            prompt_version="repair@2",
            prefix_cache_directive=PrefixCacheDirectiveV1(
                prefix_message_count=1,
                prefix_hash="sha256:" + "f" * 64,
                provider_scope="openai",
                policy_version="prefix-policy@1",
            ),
        )


def test_legacy_cassette_typed_view_preserves_unknown_and_original_bytes() -> None:
    raw = (
        '{"cassette_schema_version":"cassette@1","request_hash":"sha256:legacy",'
        '"agent_node_id":"repair","model_snapshot":{"provider":"anthropic",'
        '"model":"opus4.8","snapshot_tag":"legacy"},"response":'
        '{"response_normalized":"ok","raw_response":{},"latency_ms":0,'
        '"token_usage":{},"finish_reason":"stop","tool_calls":[]}}'
    )
    before = hashlib.sha256(raw.encode()).hexdigest()
    record = parse_cassette_record(json.loads(raw))
    view = cassette_observation_view(record, raw_payload=json.loads(raw))

    assert isinstance(record, CassetteRecordV1)
    assert view.latency.status == "unavailable"
    assert view.token_usage.status == "unavailable"
    assert view.provider_prefix_cache.status == "unavailable"
    assert hashlib.sha256(raw.encode()).hexdigest() == before


def test_legacy_alias_conflict_is_integrity_failure() -> None:
    record = CassetteRecordV1(
        request_hash="sha256:legacy",
        agent_node_id="repair",
        model_snapshot=_snapshot(),
        response=ModelResponse(
            response_normalized="ok",
            token_usage={"input": 3, "prompt_tokens": 4},
        ),
    )
    with pytest.raises(Exception, match="conflicting legacy token aliases"):
        cassette_observation_view(record, raw_payload=record.model_dump(mode="json"))


def test_v2_cassette_binds_routing_and_typed_observations() -> None:
    decision = _decision()
    request_hash_value = decision.request_hash
    record = CassetteRecordV2(
        request_hash=request_hash_value,
        agent_node_id="repair",
        model_snapshot=_snapshot(),
        routing_decision=decision,
        response_normalized="ok",
        raw_response={"id": "response-1"},
        latency=LatencyObservationV1(status="reported", provider_latency_ms=123),
        token_usage=TokenUsageObservationV1(
            status="reported",
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
        ),
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=True),
        finish_reason="stop",
        tool_calls=(),
        transport_attempt_count=2,
        transport_retry_count=1,
        recorded_at=NOW,
    )

    assert parse_cassette_record(record.model_dump()) == record
    assert cassette_observation_view(record).latency.provider_latency_ms == 123


def test_model_catalog_uses_provider_qualified_global_snapshot_ids() -> None:
    snapshot_id = canonical_model_snapshot_id(_snapshot())
    descriptor = ModelDescriptorV1(
        provider="openai",
        model_snapshot=snapshot_id,
        tier="best",
        capabilities=("reasoning", "tools"),
        context_limit=200_000,
        max_output_tokens=32_000,
        prompt_cache_support=True,
        status="active",
    )
    payload = {
        "catalog_schema_version": "model-catalog@1",
        "catalog_version": 1,
        "models": (descriptor,),
        "created_at": NOW,
    }
    catalog = ModelCatalogSnapshotV1(
        **payload,
        catalog_digest=compute_model_catalog_digest(payload),
    )
    assert catalog.models[0].model_snapshot.startswith("openai:")

    with pytest.raises(ValidationError, match="namespace"):
        ModelDescriptorV1(
            **descriptor.model_dump(exclude={"provider"}),
            provider="anthropic",
        )


def test_request_hash_formula_remains_the_frozen_m2_formula() -> None:
    request = ModelRequestV2(
        model_snapshot=_snapshot(),
        messages=_messages(),
        params={"temperature": 0},
        agent_node_id="repair",
        prompt_version="repair@2",
    )
    expected_payload = {
        "model_snapshot": request.model_snapshot.model_dump(),
        "messages": [message.model_dump() for message in request.messages],
        "tool_schema_versions": [],
        "params": request.params,
        "agent_node_id": request.agent_node_id,
        "prompt_version": request.prompt_version,
    }
    expected = "sha256:" + hashlib.sha256(canonical_json(expected_payload).encode()).hexdigest()
    assert request_hash(request) == expected
