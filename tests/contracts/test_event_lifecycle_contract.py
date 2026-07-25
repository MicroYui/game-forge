from __future__ import annotations

from pydantic import ValidationError
import pytest

from gameforge.contracts.event_lifecycle import (
    AbsoluteEventAvailabilityV1,
    RelativeEventAvailabilityV1,
)


def test_absolute_event_availability_freezes_gameplay_claim_and_expiry_order() -> None:
    availability = AbsoluteEventAvailabilityV1(
        start_at="2026-08-01T10:00:00+08:00",
        gameplay_end_at="2026-08-15T10:00:00+08:00",
        reward_claim_end_at="2026-08-18T10:00:00+08:00",
        timezone="Asia/Shanghai",
    )

    assert availability.expiration_policy == "hide_from_active_content"
    with pytest.raises(ValidationError, match="reward claim end"):
        AbsoluteEventAvailabilityV1(
            start_at="2026-08-01T10:00:00+08:00",
            gameplay_end_at="2026-08-15T10:00:00+08:00",
            reward_claim_end_at="2026-08-14T10:00:00+08:00",
            timezone="Asia/Shanghai",
        )


def test_relative_event_availability_retains_duration_without_inventing_dates() -> None:
    availability = RelativeEventAvailabilityV1(
        duration_days=14,
        reward_claim_grace_days=3,
    )

    assert availability.schedule_kind == "relative"
    assert availability.duration_days == 14
    assert availability.reward_claim_grace_days == 3
    assert availability.timezone is None
    assert not hasattr(availability, "start_at")
