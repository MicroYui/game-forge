"""M4-native typed model routing over exact cache and cassette authorities."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecordV2
from gameforge.contracts.cost import (
    CacheHitObservationV1,
    ExecutionSource,
    LatencyObservationV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import InvocationVersionBindingV1
from gameforge.contracts.model_router import ModelRequestV1, ModelRequestV2, request_hash
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.runtime.cassette.store import CassetteRouteKey, CassetteStore
from gameforge.runtime.cassette.legacy_import import VerifiedLegacyReplaySource
from gameforge.runtime.model_router.cache import (
    ExactResponseCache,
    ExactResponseCacheEntry,
    ResponseCacheBinding,
)
from gameforge.runtime.model_router.router import CassetteReplayMiss, RouterMode
from gameforge.runtime.model_router.typed_transport import (
    TransportResponseV2,
    TypedLlmTransport,
)
from gameforge.runtime.reliability.breaker import BreakerPermit, CircuitBreaker
from gameforge.runtime.reliability.retry import RetryAttemptResult, RetryExecutor


class M4RouterResultV1(BaseModel):
    """One logical model-call result with its authoritative execution source."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    result_schema_version: Literal["model-router-result@1"] = "model-router-result@1"
    response_normalized: str
    raw_response: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str
    tool_calls: tuple[dict[str, Any], ...]
    latency: LatencyObservationV1
    token_usage: TokenUsageObservationV1
    provider_prefix_cache: CacheHitObservationV1
    execution_source: ExecutionSource
    routing_decision_kind: Literal["native", "legacy_import"] = "native"
    routing_decision_id: str
    invocation: InvocationVersionBindingV1 | None = None
    transport_attempt_count: int = Field(ge=0)
    transport_retry_count: int = Field(ge=0)
    recorded_transport_attempt_count: int | None = Field(default=None, ge=1)
    recorded_transport_retry_count: int | None = Field(default=None, ge=0)

    @field_validator("transport_retry_count")
    @classmethod
    def _retry_count_is_bounded(cls, value: int, info) -> int:
        attempts = info.data.get("transport_attempt_count")
        if attempts == 0 and value != 0:
            raise ValueError("zero-attempt result cannot contain transport retries")
        if attempts and value != attempts - 1:
            raise ValueError("transport retries must equal attempts - 1")
        return value

    @field_validator("recorded_transport_retry_count")
    @classmethod
    def _recorded_retry_count_is_bounded(cls, value: int | None, info) -> int | None:
        attempts = info.data.get("recorded_transport_attempt_count")
        if (attempts is None) != (value is None):
            raise ValueError("recorded transport attempts and retries must appear together")
        if attempts is not None and value != attempts - 1:
            raise ValueError("recorded transport retries must equal attempts - 1")
        return value


class RoutingDecisionAuthority(Protocol):
    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None: ...


class PrefixCacheAdmission(Protocol):
    def validate(self, request: ModelRequestV2, decision: RoutingDecisionV1) -> None: ...


class M4ModelRouter:
    """Execute one already-persisted native routing decision.

    Selection and decision persistence belong to ``RoutingPolicyService``. This
    class consumes that immutable decision and refuses to infer or silently
    change its source, model, catalog, policy, cache, or replay evidence.
    """

    def __init__(
        self,
        *,
        transport: TypedLlmTransport,
        store: CassetteStore,
        cache: ExactResponseCache,
        mode: RouterMode,
        retry_executor: RetryExecutor[TransportResponseV2],
        decision_authority: RoutingDecisionAuthority,
        prefix_cache_admission: PrefixCacheAdmission | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        attempt_admission: Callable[[int], None] | None = None,
        attempt_cancellation: Callable[[int], None] | None = None,
        attempt_observer: Callable[[RetryAttemptResult], None] | None = None,
    ) -> None:
        self._transport = transport
        self._store = store
        self._cache = cache
        self._mode = mode
        self._retry = retry_executor
        self._decision_authority = decision_authority
        self._prefix_cache_admission = prefix_cache_admission
        self._breaker = circuit_breaker
        self._attempt_admission = attempt_admission
        self._attempt_cancellation = attempt_cancellation
        self._attempt_observer = attempt_observer

    def call(
        self,
        request: ModelRequestV2,
        *,
        decision: RoutingDecisionV1,
        deadline_utc: datetime,
        recorded_at: datetime,
        cassette_route_key: CassetteRouteKey | None = None,
    ) -> M4RouterResultV1:
        recorded_at = _require_utc(recorded_at, field_name="recorded_at")
        _require_utc(deadline_utc, field_name="deadline_utc")
        self._resolve_committed_decision(decision)
        self._validate_request_decision(request, decision)
        if decision.execution_source == "online" and request.prefix_cache_directive is not None:
            if self._prefix_cache_admission is None:
                raise IntegrityViolation(
                    "provider prefix caching requires explicit catalog/policy admission"
                )
            self._prefix_cache_admission.validate(request, decision)

        if decision.execution_source == "full_response_cache":
            if self._mode is RouterMode.REPLAY:
                raise IntegrityViolation("REPLAY is cassette-authoritative, not cache-backed")
            return self._from_cache(request, decision)
        if decision.execution_source == "cassette_replay":
            if self._mode is not RouterMode.REPLAY:
                raise IntegrityViolation("cassette replay decision requires REPLAY mode")
            return self._from_cassette(request, decision, cassette_route_key)
        if self._mode is RouterMode.REPLAY:
            raise IntegrityViolation("REPLAY requires a cassette_replay routing decision")

        response, attempts = self._complete_online(request, deadline_utc=deadline_utc)
        if self._mode is RouterMode.RECORD:
            self._record_v2(
                request=request,
                decision=decision,
                response=response,
                attempts=attempts,
                recorded_at=recorded_at,
                route_key=cassette_route_key,
            )
        self._cache.put(
            _cache_entry(
                request=request,
                decision=decision,
                response=response,
                recorded_at=recorded_at,
            )
        )
        return _result(
            response,
            decision=decision,
            execution_source="online",
            attempts=attempts,
        )

    def _from_cache(
        self,
        request: ModelRequestV2,
        decision: RoutingDecisionV1,
    ) -> M4RouterResultV1:
        entry = self._cache.get(_cache_binding(request, decision))
        if entry is None:
            raise IntegrityViolation(
                "exact full-response cache miss",
                request_hash=request_hash(request),
            )
        return M4RouterResultV1(
            response_normalized=entry.response_normalized,
            raw_response=entry.raw_response,
            finish_reason=entry.finish_reason,
            tool_calls=entry.tool_calls,
            latency=LatencyObservationV1(status="unavailable"),
            token_usage=TokenUsageObservationV1(status="reported", total_tokens=0),
            provider_prefix_cache=CacheHitObservationV1(status="unavailable"),
            execution_source="full_response_cache",
            routing_decision_id=decision.decision_id,
            transport_attempt_count=0,
            transport_retry_count=0,
        )

    def _from_cassette(
        self,
        request: ModelRequestV2,
        decision: RoutingDecisionV1,
        route_key: CassetteRouteKey | None,
    ) -> M4RouterResultV1:
        digest = request_hash(request)
        if route_key is None:
            raise IntegrityViolation("M4 REPLAY requires an exact cassette route key")
        record = self._store.replay_native(route_key)
        if record is CASSETTE_MISS:
            raise CassetteReplayMiss(digest)
        self._validate_replay_record(request, decision, record)
        response = TransportResponseV2(
            response_normalized=record.response_normalized,
            raw_response=record.raw_response,
            finish_reason=record.finish_reason,
            tool_calls=record.tool_calls,
            latency=record.latency,
            token_usage=record.token_usage,
            provider_prefix_cache=record.provider_prefix_cache,
        )
        return _result(
            response,
            decision=decision,
            execution_source="cassette_replay",
            attempts=0,
            recorded_attempts=record.transport_attempt_count,
        )

    def _complete_online(
        self,
        request: ModelRequestV2,
        *,
        deadline_utc: datetime,
    ) -> tuple[TransportResponseV2, int]:
        permits: dict[int, BreakerPermit] = {}
        attempts_started = 0

        def admit(attempt_no: int) -> None:
            permit = self._breaker.before_call() if self._breaker is not None else None
            if permit is not None:
                permits[attempt_no] = permit
            try:
                if self._attempt_admission is not None:
                    self._attempt_admission(attempt_no)
            except BaseException:
                retained = permits.pop(attempt_no, None)
                if retained is not None and self._breaker is not None:
                    self._breaker.cancel(retained)
                raise

        def cancel(attempt_no: int) -> None:
            permit = permits.pop(attempt_no, None)
            if permit is not None and self._breaker is not None:
                self._breaker.cancel(permit)
            if self._attempt_cancellation is not None:
                self._attempt_cancellation(attempt_no)

        def complete(_: int) -> TransportResponseV2:
            nonlocal attempts_started
            attempts_started += 1
            complete_with_timeout = getattr(self._transport, "complete_with_timeout", None)
            if callable(complete_with_timeout):
                timeout_s = self._retry.remaining_deadline_s(deadline_utc)
                return complete_with_timeout(request, timeout_s=timeout_s)
            return self._transport.complete(request)

        def observe(result: RetryAttemptResult) -> None:
            permit = permits.pop(result.attempt_no, None)
            if permit is not None and self._breaker is not None:
                if result.succeeded:
                    self._breaker.record_success(permit)
                else:
                    assert result.classification is not None
                    self._breaker.record_failure(permit, result.classification)
            if self._attempt_observer is not None:
                self._attempt_observer(result)

        response = self._retry.run(
            complete,
            idempotent=True,
            deadline_utc=deadline_utc,
            reserve_attempt=admit,
            cancel_attempt=cancel,
            observe_attempt=observe,
        )
        return response, attempts_started

    def _record_v2(
        self,
        *,
        request: ModelRequestV2,
        decision: RoutingDecisionV1,
        response: TransportResponseV2,
        attempts: int,
        recorded_at: datetime,
        route_key: CassetteRouteKey | None,
    ) -> None:
        if route_key is None:
            raise IntegrityViolation("M4 RECORD requires an exact cassette route key")
        record = CassetteRecordV2(
            request_hash=request_hash(request),
            agent_node_id=request.agent_node_id,
            model_snapshot=request.model_snapshot,
            routing_decision=decision,
            response_normalized=response.response_normalized,
            raw_response=response.raw_response,
            latency=response.latency,
            token_usage=response.token_usage,
            provider_prefix_cache=response.provider_prefix_cache,
            finish_reason=response.finish_reason,
            tool_calls=response.tool_calls,
            transport_attempt_count=attempts,
            transport_retry_count=attempts - 1,
            recorded_at=recorded_at,
        )
        self._store.record_native(route_key, record)

    def _resolve_committed_decision(self, decision: RoutingDecisionV1) -> None:
        persisted = self._decision_authority.get_routing_decision(decision.decision_id)
        if persisted is None:
            raise IntegrityViolation(
                "routing decision is not committed in the authority",
                decision_id=decision.decision_id,
            )
        if type(persisted) is not RoutingDecisionV1 or persisted != decision:
            raise IntegrityViolation(
                "routing decision differs from committed authority content",
                decision_id=decision.decision_id,
            )

    @staticmethod
    def _validate_request_decision(
        request: ModelRequestV2,
        decision: RoutingDecisionV1,
    ) -> None:
        if decision.request_hash != request_hash(request):
            raise IntegrityViolation("routing decision request hash differs")
        if decision.model_snapshot != canonical_model_snapshot_id(request.model_snapshot):
            raise IntegrityViolation("routing decision model snapshot differs")

    @staticmethod
    def _validate_replay_record(
        request: ModelRequestV2,
        decision: RoutingDecisionV1,
        record: CassetteRecordV2,
    ) -> None:
        if record.agent_node_id != request.agent_node_id:
            raise IntegrityViolation("cassette agent binding differs")
        if record.model_snapshot != request.model_snapshot:
            raise IntegrityViolation("cassette model binding differs")
        fields = (
            "request_hash",
            "rule_id",
            "model_snapshot",
            "tier",
            "fallback_from",
            "fallback_index",
            "policy_version",
            "routing_policy_digest",
            "catalog_version",
            "catalog_digest",
        )
        if any(
            getattr(record.routing_decision, field) != getattr(decision, field) for field in fields
        ):
            raise IntegrityViolation("cassette routing evidence differs from replay decision")


class VerifiedLegacyReplayRouter:
    """Execute only an authority-verified historical cassette import."""

    def __init__(
        self,
        *,
        source: VerifiedLegacyReplaySource,
        expected_import_id: str,
    ) -> None:
        if not expected_import_id or source.import_id != expected_import_id:
            raise IntegrityViolation("verified legacy replay import identity differs")
        self._source = source

    def call(
        self,
        request: ModelRequestV1,
        *,
        call_ordinal: int,
    ) -> M4RouterResultV1:
        call = self._source.replay(request, call_ordinal=call_ordinal)
        response = call.record.response
        observation = call.observation
        return M4RouterResultV1(
            response_normalized=response.response_normalized,
            raw_response=response.raw_response,
            finish_reason=response.finish_reason,
            tool_calls=tuple(response.tool_calls),
            latency=observation.latency,
            token_usage=observation.token_usage,
            provider_prefix_cache=observation.provider_prefix_cache,
            execution_source="cassette_replay",
            routing_decision_kind="legacy_import",
            routing_decision_id=call.routing_decision.decision_id,
            invocation=call.invocation,
            transport_attempt_count=0,
            transport_retry_count=0,
            recorded_transport_attempt_count=call.recorded_transport_attempt_count,
            recorded_transport_retry_count=call.recorded_transport_retry_count,
        )


def _cache_binding(
    request: ModelRequestV2,
    decision: RoutingDecisionV1,
) -> ResponseCacheBinding:
    return ResponseCacheBinding(
        request_hash=request_hash(request),
        model_snapshot=decision.model_snapshot,
        catalog_version=decision.catalog_version,
        catalog_digest=decision.catalog_digest,
        policy_version=decision.policy_version,
        routing_policy_digest=decision.routing_policy_digest,
    )


def _cache_entry(
    *,
    request: ModelRequestV2,
    decision: RoutingDecisionV1,
    response: TransportResponseV2,
    recorded_at: datetime,
) -> ExactResponseCacheEntry:
    payload = {
        "response_normalized": response.response_normalized,
        "raw_response": response.raw_response,
        "finish_reason": response.finish_reason,
        "tool_calls": response.tool_calls,
    }
    return ExactResponseCacheEntry(
        binding=_cache_binding(request, decision),
        response_normalized=response.response_normalized,
        raw_response=response.raw_response,
        finish_reason=response.finish_reason,
        tool_calls=response.tool_calls,
        token_usage=response.token_usage,
        latency=response.latency,
        provider_prefix_cache=response.provider_prefix_cache,
        original_execution_source="online",
        response_digest=canonical_sha256(payload),
        recorded_at=recorded_at,
    )


def _result(
    response: TransportResponseV2,
    *,
    decision: RoutingDecisionV1,
    execution_source: ExecutionSource,
    attempts: int,
    recorded_attempts: int | None = None,
) -> M4RouterResultV1:
    return M4RouterResultV1(
        response_normalized=response.response_normalized,
        raw_response=response.raw_response,
        finish_reason=response.finish_reason,
        tool_calls=response.tool_calls,
        latency=response.latency,
        token_usage=response.token_usage,
        provider_prefix_cache=response.provider_prefix_cache,
        execution_source=execution_source,
        routing_decision_id=decision.decision_id,
        transport_attempt_count=attempts,
        transport_retry_count=max(0, attempts - 1),
        recorded_transport_attempt_count=recorded_attempts,
        recorded_transport_retry_count=(
            None if recorded_attempts is None else recorded_attempts - 1
        ),
    )


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


__all__ = [
    "M4ModelRouter",
    "M4RouterResultV1",
    "PrefixCacheAdmission",
    "RoutingDecisionAuthority",
    "VerifiedLegacyReplayRouter",
]
