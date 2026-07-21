"""Fixed operational metrics shared by the API and worker composition roots.

This module deliberately stays small: the descriptors are immutable deployment
authority, while the emitted labels are closed low-cardinality projections.  Run,
trace, span, artifact, and principal identities belong in traces/logs and never in
metric labels.
"""

from __future__ import annotations

import secrets
from typing import Protocol

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.observability import (
    MetricDescriptorRegistryRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricPointV1,
)
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.observability.metrics import MetricRegistrySink


BUILTIN_OPERATIONAL_METRIC_REGISTRY_DIGEST = (
    "e73fa4e59df4d75e9582e999d9f8c4ba079a2920a79e4cc5b6a2eb0fa3bc1a64"
)

API_REQUEST_COUNT = MetricDescriptorV1(
    metric_name="gameforge.api.request.count",
    descriptor_version=1,
    metric_type="counter",
    unit="request",
    label_keys=("method", "status_class"),
    histogram_bucket_bounds=(),
    series_limit=64,
    descriptor_digest="2b193946c6683d35d5baf8a55e2c86c76221456b5f9a5f5225489df751f3a891",
)

WORKER_ATTEMPT_COUNT = MetricDescriptorV1(
    metric_name="gameforge.worker.attempt.count",
    descriptor_version=1,
    metric_type="counter",
    unit="count",
    label_keys=("phase", "run_kind"),
    histogram_bucket_bounds=(),
    series_limit=128,
    descriptor_digest="43e6fe78e91abe3116d27fc997684dfff7827864a7af0b9b1f84d7cf3327102b",
)

BUILTIN_OPERATIONAL_METRIC_REGISTRY = MetricDescriptorRegistryV1(
    registry_version=1,
    descriptors=(API_REQUEST_COUNT, WORKER_ATTEMPT_COUNT),
    global_series_limit=192,
    registry_digest=BUILTIN_OPERATIONAL_METRIC_REGISTRY_DIGEST,
)

_HTTP_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)
_RUN_KINDS = frozenset(
    {
        "artifact.migrate",
        "bench.run",
        "checker.run",
        "constraint_proposal.propose",
        "constraint_proposal.validate",
        "dr.drill",
        "generation.propose",
        "patch.repair",
        "patch.validate",
        "playtest.run",
        "review.run",
        "rollback.validate",
        "simulation.run",
        "task_suite.derive",
    }
)
_ATTEMPT_PHASES = frozenset({"started", "terminal_published"})
_MAX_DROPPED = (1 << 63) - 1


class OperationalMetricStore(Protocol):
    @property
    def metric_registry_ref(self) -> MetricDescriptorRegistryRefV1: ...

    def register_metric_registry(self, registry: MetricDescriptorRegistryV1) -> None: ...

    def record(self, point: MetricPointV1) -> None: ...


class OperationalMetricsPort(Protocol):
    def record_api_request(self, *, method: str, status_code: int) -> None: ...

    def record_worker_attempt(self, *, run_kind: str, phase: str) -> None: ...


class BuiltinOperationalMetrics:
    """Best-effort handles over the one frozen built-in registry."""

    __slots__ = ("_api_requests", "_local_dropped", "_sink", "_worker_attempts")

    def __init__(self, sink: MetricRegistrySink) -> None:
        self._sink = sink
        self._api_requests = sink.counter(API_REQUEST_COUNT.ref)
        self._worker_attempts = sink.counter(WORKER_ATTEMPT_COUNT.ref)
        self._local_dropped = 0

    @property
    def registry_ref(self) -> MetricDescriptorRegistryRefV1:
        return self._sink.registry_ref

    @property
    def dropped_count(self) -> int:
        return min(self._sink.dropped_count + self._local_dropped, _MAX_DROPPED)

    def record_api_request(self, *, method: str, status_code: int) -> None:
        normalized_method = method.upper() if isinstance(method, str) else "OTHER"
        if normalized_method not in _HTTP_METHODS:
            normalized_method = "OTHER"
        normalized_status = (
            f"{status_code // 100}xx"
            if isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
            else "OTHER"
        )
        self._emit(
            self._api_requests.add,
            labels={"method": normalized_method, "status_class": normalized_status},
        )

    def record_worker_attempt(
        self,
        *,
        run_kind: str,
        phase: str,
    ) -> None:
        normalized_kind = run_kind if run_kind in _RUN_KINDS else "other"
        normalized_phase = phase if phase in _ATTEMPT_PHASES else "other"
        self._emit(
            self._worker_attempts.add,
            labels={"phase": normalized_phase, "run_kind": normalized_kind},
        )

    def _emit(self, operation, *, labels: dict[str, str]) -> None:
        try:
            operation(1, labels=labels)
        except Exception:
            # Telemetry is explicitly best-effort. Contract/registry installation
            # fails closed at startup; a point-export failure never changes an HTTP
            # response or an authoritative Run transition.
            self._local_dropped = min(self._local_dropped + 1, _MAX_DROPPED)


def install_builtin_operational_metrics(
    *,
    store: OperationalMetricStore,
    clock: UtcClock,
) -> BuiltinOperationalMetrics:
    """Idempotently install and bind the exact API/worker registry."""

    store.register_metric_registry(BUILTIN_OPERATIONAL_METRIC_REGISTRY)
    if store.metric_registry_ref != BUILTIN_OPERATIONAL_METRIC_REGISTRY.ref:
        raise IntegrityViolation("active metric registry differs from the built-in authority")
    sink = MetricRegistrySink(
        registry=BUILTIN_OPERATIONAL_METRIC_REGISTRY,
        store=store,
        clock=clock,
        id_generator=lambda: f"metric-point:{secrets.token_hex(16)}",
    )
    return BuiltinOperationalMetrics(sink)


__all__ = [
    "API_REQUEST_COUNT",
    "BUILTIN_OPERATIONAL_METRIC_REGISTRY",
    "BUILTIN_OPERATIONAL_METRIC_REGISTRY_DIGEST",
    "BuiltinOperationalMetrics",
    "OperationalMetricsPort",
    "WORKER_ATTEMPT_COUNT",
    "install_builtin_operational_metrics",
]
