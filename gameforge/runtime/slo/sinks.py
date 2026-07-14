"""Deterministic in-memory and NDJSON implementations of AlertSink."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.slo import (
    AlertDeliveryResultV1,
    AlertInstanceV1,
    SLOEvaluationV1,
)


def _envelope(
    alert: AlertInstanceV1,
    evaluation: SLOEvaluationV1,
    idempotency_key: str,
) -> dict[str, Any]:
    return {
        "delivery_record_schema_version": "alert-delivery-record@1",
        "idempotency_key": idempotency_key,
        "alert": alert.model_dump(mode="json"),
        "evaluation": evaluation.model_dump(mode="json"),
    }


def _wire(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class InMemoryAlertSink:
    def __init__(self, *, fail_all: bool = False) -> None:
        self._fail_all = fail_all
        self._records: dict[str, tuple[str, dict[str, Any]]] = {}
        self._lock = RLock()

    @property
    def deliveries(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(self._records[key][1] for key in sorted(self._records))

    def deliver(
        self,
        alert: AlertInstanceV1,
        evaluation: SLOEvaluationV1,
        idempotency_key: str,
    ) -> AlertDeliveryResultV1:
        if not idempotency_key:
            raise ValueError("alert delivery idempotency_key must be non-empty")
        payload = _envelope(alert, evaluation, idempotency_key)
        digest = canonical_sha256(payload)
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                if existing[0] != digest:
                    raise IntegrityViolation("alert sink idempotency key has conflicting payload")
                return AlertDeliveryResultV1(
                    status="duplicate",
                    idempotency_key=idempotency_key,
                )
            if self._fail_all:
                return AlertDeliveryResultV1(
                    status="failed",
                    idempotency_key=idempotency_key,
                    detail="injected alert sink failure",
                )
            self._records[idempotency_key] = (digest, payload)
            return AlertDeliveryResultV1(
                status="delivered",
                idempotency_key=idempotency_key,
            )


class FileAlertSink:
    """Append canonical delivery records and recover idempotency on reopen."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, str] = {}
        self._lock = RLock()
        self._load()

    def deliver(
        self,
        alert: AlertInstanceV1,
        evaluation: SLOEvaluationV1,
        idempotency_key: str,
    ) -> AlertDeliveryResultV1:
        if not idempotency_key:
            raise ValueError("alert delivery idempotency_key must be non-empty")
        payload = _envelope(alert, evaluation, idempotency_key)
        wire = _wire(payload)
        digest = canonical_sha256(payload)
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                if existing != digest:
                    raise IntegrityViolation("alert sink idempotency key has conflicting payload")
                return AlertDeliveryResultV1(
                    status="duplicate",
                    idempotency_key=idempotency_key,
                )
            try:
                with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(wire)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                return AlertDeliveryResultV1(
                    status="failed",
                    idempotency_key=idempotency_key,
                    detail=type(exc).__name__,
                )
            self._records[idempotency_key] = digest
            return AlertDeliveryResultV1(
                status="delivered",
                idempotency_key=idempotency_key,
            )

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise IntegrityViolation("alert sink file cannot be read") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line:
                raise IntegrityViolation(
                    "alert sink contains an empty record",
                    line_number=line_number,
                )
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IntegrityViolation(
                    "alert sink contains invalid JSON",
                    line_number=line_number,
                ) from exc
            if not isinstance(payload, dict) or _wire(payload) != line:
                raise IntegrityViolation(
                    "alert sink record is not canonical JSON",
                    line_number=line_number,
                )
            key = payload.get("idempotency_key")
            if not isinstance(key, str) or not key:
                raise IntegrityViolation("alert sink record has invalid idempotency key")
            if (
                set(payload)
                != {
                    "delivery_record_schema_version",
                    "idempotency_key",
                    "alert",
                    "evaluation",
                }
                or payload["delivery_record_schema_version"] != "alert-delivery-record@1"
            ):
                raise IntegrityViolation("alert sink record has an unsupported envelope")
            try:
                alert = AlertInstanceV1.model_validate(payload["alert"])
                evaluation = SLOEvaluationV1.model_validate(payload["evaluation"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("alert sink record payload is invalid") from exc
            if _envelope(alert, evaluation, key) != payload:
                raise IntegrityViolation("alert sink record payload is not canonical DTO data")
            digest = canonical_sha256(payload)
            existing = self._records.get(key)
            if existing is not None and existing != digest:
                raise IntegrityViolation("alert sink idempotency key has conflicting records")
            self._records[key] = digest


__all__ = ["FileAlertSink", "InMemoryAlertSink"]
