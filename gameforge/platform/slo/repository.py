"""Alert head repository contract and deterministic test implementation."""

from __future__ import annotations

from threading import RLock
from typing import Protocol

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.slo import AlertInstanceV1


class AlertStateRepository(Protocol):
    def get(self, alert_instance_id: str) -> AlertInstanceV1 | None: ...

    def compare_and_swap(
        self,
        instance: AlertInstanceV1,
        *,
        expected_revision: int | None,
    ) -> AlertInstanceV1: ...


class InMemoryAlertStateRepository:
    """Thread-safe OCC adapter used by deterministic state-machine tests."""

    def __init__(self) -> None:
        self._items: dict[str, AlertInstanceV1] = {}
        self._lock = RLock()

    def get(self, alert_instance_id: str) -> AlertInstanceV1 | None:
        with self._lock:
            return self._items.get(alert_instance_id)

    def compare_and_swap(
        self,
        instance: AlertInstanceV1,
        *,
        expected_revision: int | None,
    ) -> AlertInstanceV1:
        with self._lock:
            current = self._items.get(instance.alert_instance_id)
            if current is None:
                if expected_revision is not None:
                    raise Conflict(
                        "alert revision does not match",
                        actual_revision=None,
                        expected_revision=expected_revision,
                    )
                if instance.revision != 1:
                    raise IntegrityViolation("new alert instance must start at revision 1")
            else:
                if expected_revision != current.revision:
                    raise Conflict(
                        "alert revision does not match",
                        actual_revision=current.revision,
                        expected_revision=expected_revision,
                    )
                if instance.revision != current.revision + 1:
                    raise IntegrityViolation("alert CAS must advance revision exactly once")
                if (
                    instance.alert_rule_id != current.alert_rule_id
                    or instance.dedup_key != current.dedup_key
                ):
                    raise IntegrityViolation("alert immutable identity fields changed")
            self._items[instance.alert_instance_id] = instance
            return instance


__all__ = ["AlertStateRepository", "InMemoryAlertStateRepository"]
