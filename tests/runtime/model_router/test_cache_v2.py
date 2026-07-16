from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import (
    Message,
    ModelRequestV2,
    ModelSnapshot,
    PrefixCacheDirectiveV1,
    compute_prefix_hash,
    request_hash,
)
from gameforge.runtime.model_router.cache import (
    ExactResponseCache,
    ExactResponseCacheEntry,
    ResponseCacheBinding,
)


NOW = datetime(2026, 7, 14, tzinfo=UTC)
MODEL_ID = "openai:sha256:" + "1" * 64


def _request(suffix: str) -> ModelRequestV2:
    messages = [
        Message(role="system", content="stable KG prefix"),
        Message(role="user", content=suffix),
    ]
    return ModelRequestV2(
        model_snapshot=ModelSnapshot(
            provider="openai",
            model="gpt-5.6-sol",
            snapshot_tag="2026-07-14",
        ),
        messages=messages,
        agent_node_id="repair",
        prompt_version="repair@2",
        prefix_cache_directive=PrefixCacheDirectiveV1(
            prefix_message_count=1,
            prefix_hash=compute_prefix_hash(messages[:1]),
            provider_scope="openai",
            policy_version="prefix@1",
        ),
    )


def _binding(request: ModelRequestV2, **changes: object) -> ResponseCacheBinding:
    values: dict[str, object] = {
        "request_hash": request_hash(request),
        "model_snapshot": MODEL_ID,
        "catalog_version": 1,
        "catalog_digest": "2" * 64,
        "policy_version": 1,
        "routing_policy_digest": "3" * 64,
    }
    values.update(changes)
    return ResponseCacheBinding(**values)


def _entry(binding: ResponseCacheBinding, text: str = "fixed") -> ExactResponseCacheEntry:
    payload = {
        "response_normalized": text,
        "raw_response": {"id": "response-1"},
        "finish_reason": "stop",
        "tool_calls": (),
    }
    return ExactResponseCacheEntry(
        binding=binding,
        response_normalized=text,
        raw_response=payload["raw_response"],
        finish_reason="stop",
        tool_calls=(),
        token_usage=TokenUsageObservationV1(
            status="reported", input_tokens=10, output_tokens=2, total_tokens=12
        ),
        latency=LatencyObservationV1(status="reported", provider_latency_ms=120),
        provider_prefix_cache=CacheHitObservationV1(status="reported", hit=True),
        original_execution_source="online",
        original_transport_attempt_count=1,
        original_transport_retry_count=0,
        response_digest=canonical_sha256(payload),
        recorded_at=NOW,
    )


def test_full_response_cache_requires_the_complete_request_hash() -> None:
    first = _request("repair quest A")
    second = _request("repair quest B")
    assert first.prefix_cache_directive.prefix_hash == second.prefix_cache_directive.prefix_hash
    assert request_hash(first) != request_hash(second)

    cache = ExactResponseCache()
    cache.put(_entry(_binding(first)))
    assert cache.get(_binding(first)) is not None
    assert cache.get(_binding(second)) is None


def test_cache_rejects_stale_catalog_policy_or_model_provenance() -> None:
    request = _request("repair")
    cache = ExactResponseCache()
    cache.put(_entry(_binding(request)))

    for changed in (
        {"catalog_digest": "4" * 64},
        {"routing_policy_digest": "5" * 64},
        {"model_snapshot": "openai:sha256:" + "6" * 64},
    ):
        with pytest.raises(IntegrityViolation, match="provenance"):
            cache.get(_binding(request, **changed))


def test_cache_identity_is_immutable_and_idempotent() -> None:
    request = _request("repair")
    binding = _binding(request)
    entry = _entry(binding)
    cache = ExactResponseCache()
    assert cache.put(entry) == entry
    assert cache.put(entry) == entry
    with pytest.raises(IntegrityViolation, match="conflicting"):
        cache.put(_entry(binding, text="different"))


def test_cache_entry_digest_covers_exact_response_payload() -> None:
    binding = _binding(_request("repair"))
    payload = _entry(binding).model_dump(mode="python")
    payload["response_digest"] = "0" * 64
    with pytest.raises(ValueError, match="response_digest"):
        ExactResponseCacheEntry.model_validate(payload)
