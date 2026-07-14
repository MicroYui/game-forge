"""M4b persistent cost-governance wire contracts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
RequestHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
OpaqueCursor = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
MAX_COST_USAGE_PAGE_SIZE = 1000

CostDimension = Literal[
    "input_token",
    "output_token",
    "cache_read_token",
    "cache_write_token",
    "request",
    "agent_step",
    "wall_time_ns",
    "concurrent_run",
    "monetary",
]
CostUnit = Literal["token", "request", "step", "ns", "count", "currency"]
BudgetScopeKind = Literal["run", "principal", "system"]
BudgetStatus = Literal["active", "exhausted", "closed"]
ReservationScope = Literal["run_budget_hold", "attempt_call", "agent_step"]
ReservationStatus = Literal[
    "reserved",
    "reconciled",
    "held_unknown",
    "conservatively_settled",
    "late_reconciled",
    "released",
]
ExecutionSource = Literal["online", "full_response_cache", "cassette_replay"]
RoutingDecisionKind = Literal["native", "legacy_import"]
PermitStatus = Literal["active", "released", "expired"]

_DIMENSION_ORDER = {
    value: index
    for index, value in enumerate(
        (
            "input_token",
            "output_token",
            "cache_read_token",
            "cache_write_token",
            "request",
            "agent_step",
            "wall_time_ns",
            "concurrent_run",
            "monetary",
        )
    )
}
_SCOPE_ORDER = {"run": 0, "principal": 1, "system": 2}
_EXPECTED_UNITS: dict[str, str] = {
    "input_token": "token",
    "output_token": "token",
    "cache_read_token": "token",
    "cache_write_token": "token",
    "request": "request",
    "agent_step": "step",
    "wall_time_ns": "ns",
    "concurrent_run": "count",
    "monetary": "currency",
}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


class CostAmountV1(_FrozenModel):
    amount_schema_version: Literal["cost-amount@1"] = "cost-amount@1"
    dimension: CostDimension
    value: NonNegativeDecimal
    unit: CostUnit
    currency: CurrencyCode | None = None

    @model_validator(mode="after")
    def _dimension_unit(self) -> CostAmountV1:
        if self.unit != _EXPECTED_UNITS[self.dimension]:
            raise ValueError("cost unit does not match dimension")
        if (self.dimension == "monetary") != (self.currency is not None):
            raise ValueError("currency belongs exactly to monetary cost")
        return self


def _canonical_amounts(
    values: Sequence[CostAmountV1],
    *,
    allow_empty: bool = True,
    allow_concurrent_run: bool = True,
) -> tuple[CostAmountV1, ...]:
    dimensions = [item.dimension for item in values]
    if len(dimensions) != len(set(dimensions)):
        raise ValueError("cost dimensions must be unique within an amount set")
    if not allow_empty and not values:
        raise ValueError("cost amount set must be non-empty")
    if not allow_concurrent_run and "concurrent_run" in dimensions:
        raise ValueError("concurrent_run is a permit-only limit dimension")
    return tuple(sorted(values, key=lambda item: _DIMENSION_ORDER[item.dimension]))


def _validate_budget_amounts(
    limits: Sequence[CostAmountV1],
    reserved: Sequence[CostAmountV1],
    consumed: Sequence[CostAmountV1],
) -> None:
    limit_by_dimension = {item.dimension: item for item in limits}
    for collection_name, values in (("reserved", reserved), ("consumed", consumed)):
        for item in values:
            limit = limit_by_dimension.get(item.dimension)
            if limit is None:
                raise ValueError(f"{collection_name} dimension is absent from limits")
            if item.unit != limit.unit or item.currency != limit.currency:
                raise ValueError(f"{collection_name} cost identity differs from its limit")


class BudgetV1(_FrozenModel):
    budget_schema_version: Literal["budget@1"] = "budget@1"
    budget_id: NonEmptyStr
    scope_kind: BudgetScopeKind
    scope_id: NonEmptyStr
    policy_version: NonEmptyStr
    limits: tuple[CostAmountV1, ...]
    reserved: tuple[CostAmountV1, ...]
    consumed: tuple[CostAmountV1, ...]
    status: BudgetStatus
    revision: PositiveInt
    deadline_utc: datetime | None = None
    created_at: datetime

    @field_validator("limits")
    @classmethod
    def _canonical_limits(cls, value: tuple[CostAmountV1, ...]) -> tuple[CostAmountV1, ...]:
        return _canonical_amounts(value, allow_empty=False)

    @field_validator("reserved", "consumed")
    @classmethod
    def _canonical_usage(cls, value: tuple[CostAmountV1, ...]) -> tuple[CostAmountV1, ...]:
        return _canonical_amounts(value, allow_concurrent_run=False)

    @field_validator("deadline_utc", "created_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def _closed_amounts(self) -> BudgetV1:
        _validate_budget_amounts(self.limits, self.reserved, self.consumed)
        if self.deadline_utc is not None and self.deadline_utc <= self.created_at:
            raise ValueError("budget deadline must follow creation")
        return self


class BudgetSnapshotV1(_FrozenModel):
    snapshot_schema_version: Literal["budget-snapshot@1"] = "budget-snapshot@1"
    snapshot_id: NonEmptyStr
    budget_id: NonEmptyStr
    scope_kind: BudgetScopeKind
    scope_id: NonEmptyStr
    policy_version: NonEmptyStr
    budget_revision_at_freeze: PositiveInt
    limits: tuple[CostAmountV1, ...]
    reserved: tuple[CostAmountV1, ...]
    consumed: tuple[CostAmountV1, ...]
    captured_at: datetime

    @field_validator("limits")
    @classmethod
    def _canonical_limits(cls, value: tuple[CostAmountV1, ...]) -> tuple[CostAmountV1, ...]:
        return _canonical_amounts(value, allow_empty=False)

    @field_validator("reserved", "consumed")
    @classmethod
    def _canonical_usage(cls, value: tuple[CostAmountV1, ...]) -> tuple[CostAmountV1, ...]:
        return _canonical_amounts(value, allow_concurrent_run=False)

    @field_validator("captured_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _closed_amounts(self) -> BudgetSnapshotV1:
        _validate_budget_amounts(self.limits, self.reserved, self.consumed)
        return self


class BudgetSetSnapshotV1(_FrozenModel):
    set_schema_version: Literal["budget-set-snapshot@1"] = "budget-set-snapshot@1"
    budget_set_snapshot_id: NonEmptyStr
    run_id: NonEmptyStr
    selection_policy_version: NonEmptyStr
    snapshots: tuple[BudgetSnapshotV1, ...]
    captured_at: datetime

    @field_validator("snapshots")
    @classmethod
    def _canonical_snapshots(
        cls, value: tuple[BudgetSnapshotV1, ...]
    ) -> tuple[BudgetSnapshotV1, ...]:
        budget_ids = [item.budget_id for item in value]
        scopes = [(item.scope_kind, item.scope_id) for item in value]
        if not value or len(budget_ids) != len(set(budget_ids)):
            raise ValueError("budget set snapshots must be non-empty with unique budget ids")
        if len(scopes) != len(set(scopes)):
            raise ValueError("budget set may contain only one budget for a scope identity")
        return tuple(
            sorted(value, key=lambda item: (_SCOPE_ORDER[item.scope_kind], item.budget_id))
        )

    @field_validator("captured_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _run_scope(self) -> BudgetSetSnapshotV1:
        for snapshot in self.snapshots:
            if snapshot.scope_kind == "run" and snapshot.scope_id != self.run_id:
                raise ValueError("run budget scope must match budget-set run_id")
        return self


class ReservationGroupV1(_FrozenModel):
    group_schema_version: Literal["reservation-group@1"] = "reservation-group@1"
    reservation_group_id: NonEmptyStr
    scope: ReservationScope
    run_id: NonEmptyStr
    budget_set_snapshot_id: NonEmptyStr
    parent_hold_group_id: NonEmptyStr | None = None
    attempt_no: PositiveInt | None = None
    request_hash: RequestHash
    transport_attempt: PositiveInt | None = None
    fencing_token: PositiveInt | None = None
    idempotency_key: NonEmptyStr
    budget_reservation_ids: tuple[NonEmptyStr, ...]
    status: ReservationStatus
    revision: PositiveInt
    created_at: datetime
    expires_at: datetime | None = None

    @field_validator("budget_reservation_ids")
    @classmethod
    def _canonical_reservation_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or len(canonical) != len(value):
            raise ValueError("budget reservation ids must be non-empty and unique")
        return canonical

    @field_validator("created_at", "expires_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def _scope_shape(self) -> ReservationGroupV1:
        if self.scope == "run_budget_hold":
            if any(
                value is not None
                for value in (
                    self.parent_hold_group_id,
                    self.attempt_no,
                    self.transport_attempt,
                    self.fencing_token,
                )
            ):
                raise ValueError("run hold excludes parent, attempt, transport, and fencing")
        elif self.scope == "agent_step":
            if (
                self.parent_hold_group_id is None
                or self.attempt_no is None
                or self.fencing_token is None
                or self.transport_attempt is not None
            ):
                raise ValueError(
                    "agent_step requires parent/attempt/fencing and excludes transport_attempt"
                )
        elif any(
            value is None
            for value in (
                self.parent_hold_group_id,
                self.attempt_no,
                self.transport_attempt,
                self.fencing_token,
            )
        ):
            raise ValueError(
                "attempt_call requires parent, attempt, transport_attempt, and fencing"
            )
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("reservation expiry must follow creation")
        return self


class BudgetReservationV1(_FrozenModel):
    reservation_schema_version: Literal["budget-reservation@1"] = "budget-reservation@1"
    reservation_id: NonEmptyStr
    reservation_group_id: NonEmptyStr
    budget_id: NonEmptyStr
    reserved: tuple[CostAmountV1, ...]
    status: ReservationStatus
    revision: PositiveInt

    @field_validator("reserved")
    @classmethod
    def _canonical_reserved(cls, value: tuple[CostAmountV1, ...]) -> tuple[CostAmountV1, ...]:
        return _canonical_amounts(value, allow_empty=False, allow_concurrent_run=False)


class TokenUsageObservationV1(_FrozenModel):
    observation_schema_version: Literal["token-usage-observation@1"] = "token-usage-observation@1"
    status: Literal["reported", "unavailable"]
    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    cache_read_tokens: NonNegativeInt | None = None
    cache_write_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> TokenUsageObservationV1:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.total_tokens,
        )
        if self.status == "unavailable" and any(value is not None for value in values):
            raise ValueError("unavailable token usage cannot contain values")
        if self.status == "reported" and all(value is None for value in values):
            raise ValueError("reported token usage requires at least one value")
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("reported total_tokens must equal input + output when all are present")
        return self


class LatencyObservationV1(_FrozenModel):
    observation_schema_version: Literal["latency-observation@1"] = "latency-observation@1"
    status: Literal["reported", "unavailable"]
    provider_latency_ms: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> LatencyObservationV1:
        if (self.status == "reported") != (self.provider_latency_ms is not None):
            raise ValueError("reported latency requires a value; unavailable latency forbids one")
        return self


class CacheHitObservationV1(_FrozenModel):
    observation_schema_version: Literal["cache-hit-observation@1"] = "cache-hit-observation@1"
    status: Literal["reported", "unavailable"]
    hit: bool | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> CacheHitObservationV1:
        if (self.status == "reported") != (self.hit is not None):
            raise ValueError("reported cache hit requires a value; unavailable forbids one")
        return self


class MonetaryObservationV1(_FrozenModel):
    observation_schema_version: Literal["monetary-observation@1"] = "monetary-observation@1"
    status: Literal["reported", "unavailable"]
    amount: NonNegativeDecimal | None = None
    currency: CurrencyCode | None = None
    price_book_version: NonEmptyStr | None = None
    quote_effective_at: datetime | None = None

    @field_validator("quote_effective_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def _status_shape(self) -> MonetaryObservationV1:
        values = (
            self.amount,
            self.currency,
            self.price_book_version,
            self.quote_effective_at,
        )
        if self.status == "reported" and any(value is None for value in values):
            raise ValueError("reported monetary observation requires quote identity and amount")
        if self.status == "unavailable" and any(value is not None for value in values):
            raise ValueError("unavailable monetary observation cannot contain values")
        return self


class UsageEntryV1(_FrozenModel):
    usage_schema_version: Literal["usage-entry@1"] = "usage-entry@1"
    usage_id: NonEmptyStr
    reservation_group_id: NonEmptyStr
    budget_reservation_ids: tuple[NonEmptyStr, ...]
    scope: Literal["attempt_call", "agent_step"]
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    request_hash: RequestHash
    transport_attempt: PositiveInt | None = None
    execution_source: ExecutionSource
    provider_prefix_cache: CacheHitObservationV1
    retry_index: NonNegativeInt
    token_usage: TokenUsageObservationV1
    latency: LatencyObservationV1
    wall_time_ns: NonNegativeInt
    monetary: MonetaryObservationV1
    routing_decision_kind: RoutingDecisionKind | None = None
    routing_decision_id: NonEmptyStr | None = None
    fencing_token_at_reserve: PositiveInt
    adjustment_of_usage_id: NonEmptyStr | None = None
    recorded_at: datetime

    @field_validator("budget_reservation_ids")
    @classmethod
    def _canonical_reservation_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or len(canonical) != len(value):
            raise ValueError("usage reservation ids must be non-empty and unique")
        return canonical

    @field_validator("recorded_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _closed_usage_identity(self) -> UsageEntryV1:
        if self.scope == "attempt_call" and self.transport_attempt is None:
            raise ValueError("attempt_call usage requires transport_attempt")
        if self.scope == "agent_step" and self.transport_attempt is not None:
            raise ValueError("agent_step usage excludes transport_attempt")
        if (self.routing_decision_kind is None) != (self.routing_decision_id is None):
            raise ValueError("routing decision kind and id must be both present or both absent")
        if self.scope == "attempt_call" and self.routing_decision_id is None:
            raise ValueError("logical model call usage requires routing identity")
        if (
            self.routing_decision_kind == "legacy_import"
            and self.execution_source != "cassette_replay"
        ):
            raise ValueError("legacy import routing is valid only for cassette replay")
        return self


class CostUsageViewV1(_FrozenModel):
    """Public cost observation without request, routing, reservation, or fencing internals."""

    usage_schema_version: Literal["cost-usage-view@1"] = "cost-usage-view@1"
    usage_id: NonEmptyStr
    scope: Literal["attempt_call", "agent_step"]
    attempt_no: PositiveInt
    transport_attempt: PositiveInt | None = None
    execution_source: ExecutionSource
    provider_prefix_cache: CacheHitObservationV1
    retry_index: NonNegativeInt
    token_usage: TokenUsageObservationV1
    latency: LatencyObservationV1
    wall_time_ns: NonNegativeInt
    monetary: MonetaryObservationV1
    adjustment_of_usage_id: NonEmptyStr | None = None
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _scope_shape(self) -> CostUsageViewV1:
        if self.scope == "attempt_call" and self.transport_attempt is None:
            raise ValueError("attempt_call cost usage requires transport_attempt")
        if self.scope == "agent_step" and self.transport_attempt is not None:
            raise ValueError("agent_step cost usage excludes transport_attempt")
        return self


class RunCostViewV1(_FrozenModel):
    """Bounded, cursor-paged public cost view for one authorized Run."""

    view_schema_version: Literal["run-cost-view@1"] = "run-cost-view@1"
    run_id: NonEmptyStr
    budget_set: BudgetSetSnapshotV1
    usage: tuple[CostUsageViewV1, ...] = Field(max_length=MAX_COST_USAGE_PAGE_SIZE)
    next_cursor: OpaqueCursor | None = None

    @model_validator(mode="after")
    def _run_binding(self) -> RunCostViewV1:
        if self.budget_set.run_id != self.run_id:
            raise ValueError("cost view Run differs from its budget set")
        return self


class PermitGroupV1(_FrozenModel):
    group_schema_version: Literal["permit-group@1"] = "permit-group@1"
    permit_group_id: NonEmptyStr
    budget_set_snapshot_id: NonEmptyStr
    run_id: NonEmptyStr
    lease_id: NonEmptyStr
    fencing_token: PositiveInt
    permit_ids: tuple[NonEmptyStr, ...]
    status: PermitStatus
    revision: PositiveInt
    acquired_at: datetime
    expires_at: datetime

    @field_validator("permit_ids")
    @classmethod
    def _canonical_permit_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if not canonical or len(canonical) != len(value):
            raise ValueError("permit ids must be non-empty and unique")
        return canonical

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _valid_interval(self) -> PermitGroupV1:
        if self.expires_at <= self.acquired_at:
            raise ValueError("permit expiry must follow acquisition")
        return self


class ConcurrencyPermitV1(_FrozenModel):
    permit_schema_version: Literal["concurrency-permit@1"] = "concurrency-permit@1"
    permit_id: NonEmptyStr
    permit_group_id: NonEmptyStr
    budget_id: NonEmptyStr
    run_id: NonEmptyStr
    lease_id: NonEmptyStr
    fencing_token: PositiveInt
    status: PermitStatus
    revision: PositiveInt
    acquired_at: datetime
    expires_at: datetime

    @field_validator("acquired_at", "expires_at")
    @classmethod
    def _utc_timestamps(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _valid_interval(self) -> ConcurrencyPermitV1:
        if self.expires_at <= self.acquired_at:
            raise ValueError("permit expiry must follow acquisition")
        return self


class PriceQuoteV1(_FrozenModel):
    quote_schema_version: Literal["price-quote@1"] = "price-quote@1"
    price_book_version: NonEmptyStr
    provider: NonEmptyStr
    model_snapshot: NonEmptyStr
    effective_from: datetime
    effective_to: datetime | None = None
    currency: CurrencyCode
    rate_unit: PositiveInt
    input_rate: NonNegativeDecimal
    output_rate: NonNegativeDecimal
    cache_read_rate: NonNegativeDecimal | None = None
    cache_write_rate: NonNegativeDecimal | None = None

    @field_validator("effective_from", "effective_to")
    @classmethod
    def _utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def _valid_interval(self) -> PriceQuoteV1:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("price quote effective interval must be non-empty")
        return self


class PriceUnavailableV1(_FrozenModel):
    status: Literal["unavailable"] = "unavailable"
    reason_code: NonEmptyStr


class PriceBook(Protocol):
    def lookup(
        self,
        provider: str,
        model_snapshot: str,
        observed_at_utc: datetime,
    ) -> PriceQuoteV1 | PriceUnavailableV1: ...


class CostLedger(Protocol):
    def freeze_budget_set(
        self,
        budget_set: BudgetSetSnapshotV1,
        hold_group: ReservationGroupV1,
        reservations: Sequence[BudgetReservationV1],
    ) -> ReservationGroupV1: ...

    def reserve_many(
        self,
        group: ReservationGroupV1,
        reservations: Sequence[BudgetReservationV1],
    ) -> ReservationGroupV1: ...

    def reconcile_group(self, usage: UsageEntryV1) -> ReservationGroupV1: ...

    def hold_unknown_group(self, reservation_group_id: str) -> ReservationGroupV1: ...

    def settle_unknown_group(
        self, reservation_group_id: str, conservative_usage: UsageEntryV1
    ) -> ReservationGroupV1: ...

    def late_reconcile_group(self, adjustment: UsageEntryV1) -> ReservationGroupV1: ...

    def close_hold_group(self, reservation_group_id: str) -> ReservationGroupV1: ...

    def acquire_permit_group(
        self,
        group: PermitGroupV1,
        permits: Sequence[ConcurrencyPermitV1],
    ) -> PermitGroupV1: ...

    def renew_permit_group(self, group: PermitGroupV1) -> PermitGroupV1: ...

    def release_permit_group(self, group: PermitGroupV1) -> PermitGroupV1: ...

    def expire_permit_group(self, group: PermitGroupV1) -> PermitGroupV1: ...


__all__ = [
    "BudgetReservationV1",
    "BudgetSetSnapshotV1",
    "BudgetSnapshotV1",
    "BudgetV1",
    "CacheHitObservationV1",
    "ConcurrencyPermitV1",
    "CostAmountV1",
    "CostDimension",
    "CostLedger",
    "CostUsageViewV1",
    "LatencyObservationV1",
    "MonetaryObservationV1",
    "MAX_COST_USAGE_PAGE_SIZE",
    "PermitGroupV1",
    "PriceBook",
    "PriceQuoteV1",
    "PriceUnavailableV1",
    "ReservationGroupV1",
    "RunCostViewV1",
    "TokenUsageObservationV1",
    "UsageEntryV1",
]
