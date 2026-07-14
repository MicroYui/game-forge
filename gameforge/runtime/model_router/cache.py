"""Session-local exact full-response cache with closed provenance bindings."""

from __future__ import annotations

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

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.errors import IntegrityViolation


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
RequestHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ResponseCacheBinding(_FrozenModel):
    binding_schema_version: Literal["response-cache-binding@1"] = "response-cache-binding@1"
    request_hash: RequestHash
    model_snapshot: NonEmptyStr
    catalog_version: Annotated[int, Field(gt=0)]
    catalog_digest: Sha256Hex
    policy_version: Annotated[int, Field(gt=0)]
    routing_policy_digest: Sha256Hex


class ExactResponseCacheEntry(_FrozenModel):
    entry_schema_version: Literal["exact-response-cache-entry@1"] = "exact-response-cache-entry@1"
    binding: ResponseCacheBinding
    response_normalized: str
    raw_response: dict[str, Any]
    finish_reason: str
    tool_calls: tuple[dict[str, Any], ...]
    token_usage: TokenUsageObservationV1
    latency: LatencyObservationV1
    provider_prefix_cache: CacheHitObservationV1
    original_execution_source: Literal["online", "cassette_replay"]
    response_digest: Sha256Hex
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("cache record timestamp must be timezone-aware UTC")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _digest_matches(self) -> ExactResponseCacheEntry:
        expected = canonical_sha256(
            {
                "response_normalized": self.response_normalized,
                "raw_response": self.raw_response,
                "finish_reason": self.finish_reason,
                "tool_calls": self.tool_calls,
            }
        )
        if self.response_digest != expected:
            raise ValueError("response_digest does not match exact response payload")
        return self


class ExactResponseCache:
    """Bounded by one router session; never performs semantic/prefix lookup."""

    def __init__(self, *, max_entries: int = 1024) -> None:
        if isinstance(max_entries, bool) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._entries: dict[str, ExactResponseCacheEntry] = {}

    def get(self, binding: ResponseCacheBinding) -> ExactResponseCacheEntry | None:
        existing = self._entries.get(binding.request_hash)
        if existing is None:
            return None
        if existing.binding != binding:
            raise IntegrityViolation(
                "exact response cache provenance differs from requested binding",
                request_hash=binding.request_hash,
            )
        return existing

    def put(self, entry: ExactResponseCacheEntry) -> ExactResponseCacheEntry:
        existing = self._entries.get(entry.binding.request_hash)
        if existing is not None:
            if existing != entry:
                raise IntegrityViolation(
                    "exact response cache identity has conflicting payload",
                    request_hash=entry.binding.request_hash,
                )
            return existing
        if len(self._entries) >= self._max_entries:
            raise IntegrityViolation("exact response cache entry limit is exhausted")
        self._entries[entry.binding.request_hash] = entry
        return entry


__all__ = [
    "ExactResponseCache",
    "ExactResponseCacheEntry",
    "ResponseCacheBinding",
]
