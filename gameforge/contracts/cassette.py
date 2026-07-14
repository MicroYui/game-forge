"""Versioned cassette records and typed legacy observation views."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelResponse, ModelSnapshot
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.contracts.versions import CASSETTE_SCHEMA_VERSION


class CassetteRecord(BaseModel):
    cassette_schema_version: str = CASSETTE_SCHEMA_VERSION
    request_hash: str
    agent_node_id: str
    model_snapshot: ModelSnapshot
    response: ModelResponse
    transport_attempts: int | None = Field(default=None, ge=1)
    transport_retries: int | None = Field(default=None, ge=0)
    recorded_at: str | None = None

    @model_validator(mode="after")
    def validate_transport_attempts(self) -> CassetteRecord:
        attempts_missing = self.transport_attempts is None
        retries_missing = self.transport_retries is None
        if attempts_missing != retries_missing:
            raise ValueError("cassette transport attempts and retries must appear together")
        if (
            self.transport_attempts is not None
            and self.transport_retries != self.transport_attempts - 1
        ):
            raise ValueError("cassette transport retries must equal attempts - 1")
        return self


CassetteRecordV1 = CassetteRecord


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
RequestHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class CassetteRecordV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    cassette_schema_version: Literal["cassette@2"] = "cassette@2"
    request_hash: RequestHash
    agent_node_id: NonEmptyStr
    model_snapshot: ModelSnapshot
    routing_decision: RoutingDecisionV1
    response_normalized: str
    raw_response: dict[str, Any]
    latency: LatencyObservationV1
    token_usage: TokenUsageObservationV1
    provider_prefix_cache: CacheHitObservationV1
    finish_reason: str
    tool_calls: tuple[dict[str, Any], ...]
    transport_attempt_count: int = Field(ge=1)
    transport_retry_count: int = Field(ge=0)
    recorded_at: datetime | None = None

    @field_validator("recorded_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("recorded_at must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def _closed_record(self) -> CassetteRecordV2:
        if self.transport_retry_count != self.transport_attempt_count - 1:
            raise ValueError("cassette transport retries must equal attempts - 1")
        if self.routing_decision.request_hash != self.request_hash:
            raise ValueError("cassette routing decision request hash differs from record")
        if self.routing_decision.model_snapshot != canonical_model_snapshot_id(self.model_snapshot):
            raise ValueError("cassette structured model snapshot differs from routing decision")
        return self


class CassetteObservationViewV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    view_schema_version: Literal["cassette-observation-view@1"] = "cassette-observation-view@1"
    latency: LatencyObservationV1
    token_usage: TokenUsageObservationV1
    provider_prefix_cache: CacheHitObservationV1
    transport_attempt_count: int | None = Field(default=None, ge=1)
    transport_retry_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _attempt_pair(self) -> CassetteObservationViewV1:
        missing = self.transport_attempt_count is None
        if missing != (self.transport_retry_count is None):
            raise ValueError("transport attempt observations appear together")
        if (
            self.transport_attempt_count is not None
            and self.transport_retry_count != self.transport_attempt_count - 1
        ):
            raise ValueError("transport retries must equal attempts - 1")
        return self


_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "input", "prompt_tokens"),
    "output_tokens": ("output_tokens", "output", "completion_tokens"),
    "cache_read_tokens": ("cache_read_tokens", "cache_read"),
    "cache_write_tokens": ("cache_write_tokens", "cache_write"),
    "total_tokens": ("total_tokens",),
}


def _legacy_token_observation(raw_usage: object) -> TokenUsageObservationV1:
    if not isinstance(raw_usage, Mapping) or not raw_usage:
        return TokenUsageObservationV1(status="unavailable")
    normalized: dict[str, int] = {}
    for field_name, aliases in _TOKEN_ALIASES.items():
        observed: list[int] = []
        for alias in aliases:
            if alias not in raw_usage:
                continue
            raw_value = raw_usage[alias]
            if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value < 0:
                raise IntegrityViolation("invalid legacy token usage value", alias=alias)
            observed.append(raw_value)
        if observed and len(set(observed)) != 1:
            raise IntegrityViolation(
                "conflicting legacy token aliases",
                field_name=field_name,
            )
        if observed:
            normalized[field_name] = observed[0]
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
        raise IntegrityViolation("invalid legacy token usage observation") from exc


def cassette_observation_view(
    record: CassetteRecord | CassetteRecordV2,
    *,
    raw_payload: Mapping[str, Any] | None = None,
) -> CassetteObservationViewV1:
    if isinstance(record, CassetteRecordV2):
        return CassetteObservationViewV1(
            latency=record.latency,
            token_usage=record.token_usage,
            provider_prefix_cache=record.provider_prefix_cache,
            transport_attempt_count=record.transport_attempt_count,
            transport_retry_count=record.transport_retry_count,
        )

    raw_response: Mapping[str, Any]
    if raw_payload is None:
        raw_response = record.response.model_dump(mode="json")
    else:
        candidate = raw_payload.get("response")
        raw_response = candidate if isinstance(candidate, Mapping) else {}
    raw_latency = raw_response.get("latency_ms")
    latency = (
        LatencyObservationV1(status="reported", provider_latency_ms=raw_latency)
        if isinstance(raw_latency, int) and not isinstance(raw_latency, bool) and raw_latency > 0
        else LatencyObservationV1(status="unavailable")
    )
    token_usage = _legacy_token_observation(raw_response.get("token_usage"))
    return CassetteObservationViewV1(
        latency=latency,
        token_usage=token_usage,
        provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
        transport_attempt_count=record.transport_attempts,
        transport_retry_count=record.transport_retries,
    )


def parse_cassette_record(payload: Mapping[str, Any]) -> CassetteRecord | CassetteRecordV2:
    version = payload.get("cassette_schema_version")
    if version == CASSETTE_SCHEMA_VERSION:
        return CassetteRecord.model_validate(payload)
    if version == "cassette@2":
        return CassetteRecordV2.model_validate(payload)
    raise ValueError(f"unsupported cassette schema version: {version!r}")


class _CassetteMiss:
    """Sentinel returned by CassetteStore.replay when no record exists."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "CASSETTE_MISS"


CASSETTE_MISS = _CassetteMiss()


__all__ = [
    "CASSETTE_MISS",
    "CassetteObservationViewV1",
    "CassetteRecord",
    "CassetteRecordV1",
    "CassetteRecordV2",
    "cassette_observation_view",
    "parse_cassette_record",
]
