"""Bounded object-generation collection with transactional reference rechecks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Protocol, runtime_checkable

from gameforge.contracts.errors import (
    CursorExpired,
    CursorInvalid,
    IntegrityViolation,
    RetentionActive,
)
from gameforge.contracts.lineage import ObjectLocation, ObjectRef
from gameforge.contracts.storage import (
    GcCandidate,
    GcCollectionResult,
    ObjectStore,
    PageCursorV1,
    PageV1,
    UnitOfWork,
    UtcClock,
)
from gameforge.runtime.persistence.object_bindings import ObjectReferenceState


@runtime_checkable
class RecoveryPins(Protocol):
    """Narrow M4a boundary for recovery-owned object locations."""

    def is_pinned(self, ref: ObjectRef, location: ObjectLocation) -> bool: ...


class NoRecoveryPins:
    """Explicit local policy used before M4e supplies a RecoveryCatalog adapter."""

    __slots__ = ()

    def is_pinned(self, ref: ObjectRef, location: ObjectLocation) -> bool:
        del ref, location
        return False


@dataclass(frozen=True, slots=True)
class _Sweep:
    safe_before: datetime
    expires_at: datetime


def _parse_utc(value: str, *, field_name: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a valid UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _require_now(clock: UtcClock) -> datetime:
    now = clock.now_utc()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("GC clock must provide a timezone-aware UTC timestamp")
    return now.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _location_key(location: ObjectLocation) -> tuple[str, str, str]:
    return location.store_id, location.key, location.backend_generation


class ObjectGcService:
    """Collect only generations proven unreferenced at the deletion boundary."""

    def __init__(
        self,
        *,
        objects: ObjectStore,
        unit_of_work: UnitOfWork,
        recovery_pins: RecoveryPins,
        clock: UtcClock,
        minimum_safe_age: timedelta,
    ) -> None:
        if not isinstance(minimum_safe_age, timedelta) or minimum_safe_age <= timedelta(0):
            raise ValueError("minimum_safe_age must be a positive timedelta")
        if not isinstance(recovery_pins, RecoveryPins):
            raise TypeError("recovery_pins must implement RecoveryPins")
        self._objects = objects
        self._unit_of_work = unit_of_work
        self._recovery_pins = recovery_pins
        self._clock = clock
        self._minimum_safe_age = minimum_safe_age
        self._sweeps: dict[str, _Sweep] = {}
        self._sweep_lock = RLock()

    def plan(
        self,
        cursor: PageCursorV1 | None,
        safe_before: str,
    ) -> PageV1[GcCandidate]:
        now = _require_now(self._clock)
        cutoff = _parse_utc(safe_before, field_name="safe_before")
        if cutoff > now - self._minimum_safe_age:
            raise ValueError("safe_before violates the configured minimum safe age")

        if cursor is not None:
            self._require_sweep(cursor.snapshot_id, cutoff=cutoff, now=now)

        object_page = self._objects.list_versions(cursor)
        if cursor is not None and object_page.read_snapshot_id != cursor.snapshot_id:
            raise IntegrityViolation("ObjectStore returned a different read snapshot")
        expires_at = self._record_or_verify_sweep(
            object_page.read_snapshot_id,
            cutoff=cutoff,
            expires_at_text=object_page.expires_at,
            now=now,
        )

        candidates: list[GcCandidate] = []
        with self._unit_of_work.begin() as transaction:
            states = transaction.object_bindings.reference_states(object_page.items)
            for stat in object_page.items:
                try:
                    verified_at = _parse_utc(stat.verified_at, field_name="verified_at")
                except ValueError as exc:
                    raise IntegrityViolation(
                        "ObjectStore returned an invalid verification timestamp",
                        store_id=stat.location.store_id,
                        key=stat.location.key,
                        backend_generation=stat.location.backend_generation,
                    ) from exc
                if verified_at >= cutoff:
                    continue

                state = states.get(_location_key(stat.location))
                if state is None:
                    raise IntegrityViolation(
                        "object reference-state query omitted a requested generation",
                        store_id=stat.location.store_id,
                        key=stat.location.key,
                        backend_generation=stat.location.backend_generation,
                    )
                if state.exact_active_binding:
                    continue
                if self._recovery_pins.is_pinned(stat.ref, stat.location):
                    continue
                if state.artifact_referenced and not state.any_active_binding_for_ref:
                    continue
                candidates.append(
                    GcCandidate(
                        location=stat.location,
                        object_ref=stat.ref,
                        observed_at=_utc_text(now),
                    )
                )

        return PageV1[GcCandidate](
            read_snapshot_id=object_page.read_snapshot_id,
            items=tuple(candidates),
            next_cursor=object_page.next_cursor,
            expires_at=_utc_text(expires_at),
        )

    def collect(self, candidate: GcCandidate) -> GcCollectionResult:
        now = _require_now(self._clock)
        with self._unit_of_work.begin() as transaction:
            try:
                stat = self._objects.stat(candidate.location)
            except FileNotFoundError:
                return "retained_generation_changed"

            if stat.location != candidate.location:
                raise IntegrityViolation("GC candidate location no longer matches ObjectStore stat")
            if candidate.object_ref is not None and stat.ref != candidate.object_ref:
                raise IntegrityViolation(
                    "GC candidate ObjectRef no longer matches ObjectStore stat"
                )

            states = transaction.object_bindings.reference_states((stat,))
            state = states.get(_location_key(stat.location))
            if state is None:
                raise IntegrityViolation("object reference-state query omitted the GC candidate")
            if state.exact_active_binding:
                return "retained_referenced"
            if self._recovery_pins.is_pinned(stat.ref, stat.location):
                return "retained_referenced"
            if state.artifact_referenced and not state.any_active_binding_for_ref:
                return "retained_referenced"

            try:
                verified_at = _parse_utc(stat.verified_at, field_name="verified_at")
                retention_until = (
                    None
                    if stat.retention_until is None
                    else _parse_utc(stat.retention_until, field_name="retention_until")
                )
            except ValueError as exc:
                raise IntegrityViolation(
                    "ObjectStore returned invalid GC retention timestamps"
                ) from exc
            if verified_at >= now - self._minimum_safe_age:
                return "retention_active"
            if retention_until is not None and retention_until > now:
                return "retention_active"

            try:
                deleted = self._objects.delete_if_generation(stat.location)
            except RetentionActive:
                return "retention_active"
            return "deleted" if deleted else "retained_generation_changed"

    def _require_sweep(
        self,
        snapshot_id: str,
        *,
        cutoff: datetime,
        now: datetime,
    ) -> _Sweep:
        with self._sweep_lock:
            self._prune_sweeps(now)
            sweep = self._sweeps.get(snapshot_id)
            if sweep is None:
                raise CursorExpired("GC read snapshot is no longer retained")
            if sweep.safe_before != cutoff:
                raise CursorInvalid("GC cursor cutoff differs from the original sweep")
            return sweep

    def _record_or_verify_sweep(
        self,
        snapshot_id: str,
        *,
        cutoff: datetime,
        expires_at_text: str,
        now: datetime,
    ) -> datetime:
        try:
            expires_at = _parse_utc(expires_at_text, field_name="expires_at")
        except ValueError as exc:
            raise IntegrityViolation("ObjectStore returned an invalid page expiration") from exc
        if now >= expires_at:
            raise CursorExpired("GC read snapshot has expired")
        with self._sweep_lock:
            self._prune_sweeps(now)
            existing = self._sweeps.get(snapshot_id)
            if existing is not None and existing.safe_before != cutoff:
                raise CursorInvalid("GC cursor cutoff differs from the original sweep")
            self._sweeps[snapshot_id] = _Sweep(
                safe_before=cutoff,
                expires_at=expires_at,
            )
        return expires_at

    def _prune_sweeps(self, now: datetime) -> None:
        expired = [
            snapshot_id for snapshot_id, sweep in self._sweeps.items() if now >= sweep.expires_at
        ]
        for snapshot_id in expired:
            del self._sweeps[snapshot_id]


__all__ = [
    "NoRecoveryPins",
    "ObjectGcService",
    "ObjectReferenceState",
    "RecoveryPins",
]
