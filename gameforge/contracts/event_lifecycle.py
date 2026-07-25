"""Typed lifecycle attributes for permanent and limited-time game content."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)


BoundedTimestamp = Annotated[str, StringConstraints(min_length=1, max_length=128)]
BoundedTimezone = Annotated[str, StringConstraints(min_length=1, max_length=128)]
PositiveDays = Annotated[int, Field(ge=1, le=36_600)]
NonNegativeDays = Annotated[int, Field(ge=0, le=36_600)]


def parse_aware_datetime(value: str, *, field_name: str) -> datetime:
    """Parse one ISO-8601 instant and reject wall-clock-only values."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit UTC offset")
    return parsed


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be an IANA timezone name") from exc
    return value


class _FrozenLifecycleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AbsoluteEventAvailabilityV1(_FrozenLifecycleModel):
    availability_schema_version: Literal["event-availability@1"] = "event-availability@1"
    schedule_kind: Literal["absolute"] = "absolute"
    start_at: BoundedTimestamp
    gameplay_end_at: BoundedTimestamp
    reward_claim_end_at: BoundedTimestamp
    timezone: BoundedTimezone
    expiration_policy: Literal["hide_from_active_content"] = "hide_from_active_content"

    _timezone_name = field_validator("timezone")(_validate_timezone)

    @model_validator(mode="after")
    def _ordered_windows(self) -> "AbsoluteEventAvailabilityV1":
        start = parse_aware_datetime(self.start_at, field_name="start_at")
        gameplay_end = parse_aware_datetime(
            self.gameplay_end_at,
            field_name="gameplay_end_at",
        )
        reward_claim_end = parse_aware_datetime(
            self.reward_claim_end_at,
            field_name="reward_claim_end_at",
        )
        if gameplay_end <= start:
            raise ValueError("gameplay end must follow event start")
        if reward_claim_end < gameplay_end:
            raise ValueError("reward claim end cannot precede gameplay end")
        return self


class RelativeEventAvailabilityV1(_FrozenLifecycleModel):
    availability_schema_version: Literal["event-availability@1"] = "event-availability@1"
    schedule_kind: Literal["relative"] = "relative"
    duration_days: PositiveDays
    reward_claim_grace_days: NonNegativeDays
    timezone: BoundedTimezone | None = None
    expiration_policy: Literal["hide_from_active_content"] = "hide_from_active_content"

    @field_validator("timezone")
    @classmethod
    def _optional_timezone_name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_timezone(value)


EventAvailabilityV1: TypeAlias = Annotated[
    AbsoluteEventAvailabilityV1 | RelativeEventAvailabilityV1,
    Field(discriminator="schedule_kind"),
]
EVENT_AVAILABILITY_ADAPTER = TypeAdapter(EventAvailabilityV1)


__all__ = [
    "AbsoluteEventAvailabilityV1",
    "EVENT_AVAILABILITY_ADAPTER",
    "EventAvailabilityV1",
    "RelativeEventAvailabilityV1",
    "parse_aware_datetime",
]
