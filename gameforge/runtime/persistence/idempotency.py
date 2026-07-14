"""Transaction-bound persistence for completed idempotent command results."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from gameforge.contracts.errors import IdempotencyConflict, IntegrityViolation
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.persistence.models import IdempotencyRecordRow


def _require_identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_request_hash(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("request_hash must be 64 lowercase hexadecimal characters")
    return value


def _utc_text(clock: UtcClock) -> str:
    try:
        now = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("idempotency repository clock must return UTC") from exc
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("idempotency repository clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_timestamp(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise IntegrityViolation("stored idempotency timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrityViolation("stored idempotency timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset() != timedelta(0):
        raise IntegrityViolation("stored idempotency timestamp is not UTC")


def _copy_json(value: object, *, stored: bool, path: str) -> Any:
    def fail(detail: str) -> None:
        if stored:
            raise IntegrityViolation(detail)
        raise ValueError(detail)

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            fail(f"{path} must contain only finite JSON numbers")
        return value
    if isinstance(value, list):
        return [
            _copy_json(item, stored=stored, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                fail(f"{path} object keys must be strings")
            result[key] = _copy_json(item, stored=stored, path=f"{path}.{key}")
        return result
    fail(f"{path} must be a JSON value")
    raise AssertionError("unreachable")


def _copy_response(value: object, *, stored: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        if stored:
            raise IntegrityViolation("stored idempotency response must be a JSON object")
        raise ValueError("response must be a JSON object")
    copied = _copy_json(value, stored=stored, path="response")
    if not isinstance(copied, dict):  # pragma: no cover - guarded above
        raise AssertionError("response normalization did not produce an object")
    return copied


class SqlIdempotencyRepository:
    """Persist only completed results; the owning UnitOfWork decides commit."""

    def __init__(self, session: Session, *, clock: UtcClock) -> None:
        if session.get_bind().dialect.name != "sqlite":
            raise ValueError("SqlIdempotencyRepository requires a SQLite session")
        self._session = session
        self._clock = clock

    def get_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        identity = self._identity(scope=scope, operation=operation, key=key)
        expected_hash = _require_request_hash(request_hash)
        row = self._session.get(IdempotencyRecordRow, identity)
        if row is None:
            return None
        response = self._validated_response(row, expected_identity=identity)
        if row.request_hash != expected_hash:
            raise IdempotencyConflict(
                "idempotency key is already bound to a different request",
                scope=identity[0],
                operation=identity[1],
                key=identity[2],
                expected_request_hash=expected_hash,
                actual_request_hash=row.request_hash,
            )
        return response

    def put_result(
        self,
        *,
        scope: str,
        operation: str,
        key: str,
        request_hash: str,
        resource_kind: str,
        resource_id: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        identity = self._identity(scope=scope, operation=operation, key=key)
        expected_hash = _require_request_hash(request_hash)
        selected_resource_kind = _require_identifier(
            resource_kind,
            field_name="resource_kind",
        )
        selected_resource_id = _require_identifier(resource_id, field_name="resource_id")
        canonical_response = _copy_response(response, stored=False)

        replay = self.get_result(
            scope=identity[0],
            operation=identity[1],
            key=identity[2],
            request_hash=expected_hash,
        )
        if replay is not None:
            return replay

        now = _utc_text(self._clock)
        result = self._session.execute(
            sqlite_insert(IdempotencyRecordRow)
            .values(
                scope=identity[0],
                operation=identity[1],
                key=identity[2],
                request_hash=expected_hash,
                resource_kind=selected_resource_kind,
                resource_id=selected_resource_id,
                created_at=now,
                updated_at=now,
                response=canonical_response,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    IdempotencyRecordRow.scope,
                    IdempotencyRecordRow.operation,
                    IdempotencyRecordRow.key,
                ]
            )
        )
        if result.rowcount == 1:
            return _copy_response(canonical_response, stored=True)

        self._session.expire_all()
        replay = self.get_result(
            scope=identity[0],
            operation=identity[1],
            key=identity[2],
            request_hash=expected_hash,
        )
        if replay is None:
            raise IntegrityViolation(
                "idempotency result insert conflicted without a retained result",
                scope=identity[0],
                operation=identity[1],
                key=identity[2],
            )
        return replay

    @staticmethod
    def _identity(*, scope: str, operation: str, key: str) -> tuple[str, str, str]:
        return (
            _require_identifier(scope, field_name="scope"),
            _require_identifier(operation, field_name="operation"),
            _require_identifier(key, field_name="key"),
        )

    @staticmethod
    def _validated_response(
        row: IdempotencyRecordRow,
        *,
        expected_identity: tuple[str, str, str],
    ) -> dict[str, Any]:
        actual_identity = (row.scope, row.operation, row.key)
        if actual_identity != expected_identity:
            raise IntegrityViolation("stored idempotency identity differs from its lookup key")
        try:
            _require_request_hash(row.request_hash)
        except ValueError as exc:
            raise IntegrityViolation("stored idempotency request_hash is invalid") from exc
        if not isinstance(row.resource_kind, str) or not row.resource_kind:
            raise IntegrityViolation("stored idempotency resource_kind is invalid")
        if not isinstance(row.resource_id, str) or not row.resource_id:
            raise IntegrityViolation("stored idempotency resource_id is invalid")
        _validate_timestamp(row.created_at)
        _validate_timestamp(row.updated_at)
        return _copy_response(row.response, stored=True)


__all__ = ["SqlIdempotencyRepository"]
