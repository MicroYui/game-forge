"""Typed M4 transport observations and the legacy transport compatibility adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelRequestV2, ModelResponse


class TransportResponseV2(BaseModel):
    """Provider response with explicit reported-vs-unavailable observations."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    response_normalized: str
    raw_response: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    latency: LatencyObservationV1
    token_usage: TokenUsageObservationV1
    provider_prefix_cache: CacheHitObservationV1


class TypedLlmTransport(Protocol):
    def complete(self, request: ModelRequestV2) -> TransportResponseV2: ...


class LegacyTypedTransportAdapter:
    """Map the v1 transport shape without inventing observations it never supplied."""

    def __init__(self, transport: object) -> None:
        complete = getattr(transport, "complete", None)
        if not callable(complete):
            raise TypeError("legacy transport must provide complete(request)")
        self._transport = transport

    def complete(self, request: ModelRequestV2) -> TransportResponseV2:
        response = self._transport.complete(request)  # type: ignore[attr-defined]
        return _map_legacy_response(response)

    def complete_with_timeout(
        self,
        request: ModelRequestV2,
        *,
        timeout_s: float,
    ) -> TransportResponseV2:
        if timeout_s <= 0:
            raise TimeoutError("transport deadline has elapsed")
        complete = getattr(self._transport, "complete_with_timeout", None)
        response = (
            complete(request, timeout_s=timeout_s)
            if callable(complete)
            else self._transport.complete(request)  # type: ignore[attr-defined]
        )
        return _map_legacy_response(response)


def _map_legacy_response(response: object) -> TransportResponseV2:
    if not isinstance(response, ModelResponse):
        raise IntegrityViolation("legacy transport returned an unsupported response type")
    latency = (
        LatencyObservationV1(
            status="reported",
            provider_latency_ms=response.latency_ms,
        )
        if response.latency_ms > 0
        else LatencyObservationV1(status="unavailable")
    )
    return TransportResponseV2(
        response_normalized=response.response_normalized,
        raw_response=response.raw_response,
        finish_reason=response.finish_reason,
        tool_calls=tuple(response.tool_calls),
        latency=latency,
        token_usage=_legacy_token_usage(response.token_usage),
        provider_prefix_cache=_provider_prefix_cache_observation(response.raw_response),
    )


def _provider_prefix_cache_observation(
    raw_response: Mapping[str, object],
) -> CacheHitObservationV1:
    observations: list[bool] = []
    usage = raw_response.get("usage")
    if isinstance(usage, Mapping):
        details = usage.get("input_tokens_details")
        if isinstance(details, Mapping) and "cached_tokens" in details:
            observations.append(
                _reported_cache_tokens(details["cached_tokens"], field="cached_tokens") > 0
            )
        if "cache_read_input_tokens" in usage:
            observations.append(
                _reported_cache_tokens(
                    usage["cache_read_input_tokens"],
                    field="cache_read_input_tokens",
                )
                > 0
            )

    copilot_usage = raw_response.get("copilot_usage")
    if isinstance(copilot_usage, Mapping):
        token_details = copilot_usage.get("token_details")
        if isinstance(token_details, list):
            for item in token_details:
                if isinstance(item, Mapping) and item.get("token_type") == "cache_read":
                    observations.append(
                        _reported_cache_tokens(
                            item.get("token_count"),
                            field="cache_read token_count",
                        )
                        > 0
                    )

    if not observations:
        return CacheHitObservationV1(status="unavailable")
    if len(set(observations)) != 1:
        raise IntegrityViolation("provider prefix cache observations conflict")
    return CacheHitObservationV1(status="reported", hit=observations[0])


def _reported_cache_tokens(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IntegrityViolation(
            "provider prefix cache observation is invalid",
            field=field,
        )
    return value


_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "input", "prompt_tokens"),
    "output_tokens": ("output_tokens", "output", "completion_tokens"),
    "cache_read_tokens": ("cache_read_tokens", "cache_read"),
    "cache_write_tokens": ("cache_write_tokens", "cache_write"),
    "total_tokens": ("total_tokens",),
}


def _legacy_token_usage(raw: Mapping[str, object]) -> TokenUsageObservationV1:
    if not raw:
        return TokenUsageObservationV1(status="unavailable")
    normalized: dict[str, int] = {}
    for field_name, aliases in _TOKEN_ALIASES.items():
        values: list[int] = []
        for alias in aliases:
            if alias not in raw:
                continue
            value = raw[alias]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IntegrityViolation("legacy transport reported invalid token usage")
            values.append(value)
        if values and len(set(values)) != 1:
            raise IntegrityViolation("legacy transport token aliases conflict")
        if values:
            normalized[field_name] = values[0]
    if not normalized:
        return TokenUsageObservationV1(status="unavailable")
    if (
        "total_tokens" not in normalized
        and "input_tokens" in normalized
        and "output_tokens" in normalized
    ):
        normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    try:
        return TokenUsageObservationV1(status="reported", **normalized)
    except ValueError as exc:
        raise IntegrityViolation("legacy transport token usage is inconsistent") from exc


__all__ = [
    "LegacyTypedTransportAdapter",
    "TransportResponseV2",
    "TypedLlmTransport",
]
