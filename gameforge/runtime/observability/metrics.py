"""Registry-bound metric handles for local deterministic telemetry."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation, QueryTooBroad
from gameforge.contracts.observability import (
    Counter,
    Gauge,
    HistogramMetricSampleV1,
    Histogram,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricPointV1,
    MetricQueryV1,
    MetricQueryStore,
    MetricSeriesV1,
    ScalarMetricSampleV1,
)
from gameforge.contracts.storage import UtcClock


class MetricRegistrySink:
    """Emit raw points only through exact descriptors frozen at readiness."""

    __slots__ = (
        "_clock",
        "_descriptors",
        "_dropped_count",
        "_id_generator",
        "_registry",
        "_store",
    )

    def __init__(
        self,
        *,
        registry: MetricDescriptorRegistryV1,
        store: MetricQueryStore,
        clock: UtcClock,
        id_generator: Callable[[], str],
    ) -> None:
        self._registry = registry
        self._descriptors = {
            (item.metric_name, item.descriptor_version, item.descriptor_digest): item
            for item in registry.descriptors
        }
        self._store = store
        self._clock = clock
        self._id_generator = id_generator
        self._dropped_count = 0

    @property
    def registry_ref(self) -> MetricDescriptorRegistryRefV1:
        return self._registry.ref

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def _drop(self) -> None:
        self._dropped_count = min(self._dropped_count + 1, (1 << 63) - 1)

    def counter(self, descriptor: MetricDescriptorRefV1) -> Counter:
        return _MetricHandle(self, self._resolve(descriptor, "counter"), "counter")

    def histogram(self, descriptor: MetricDescriptorRefV1) -> Histogram:
        return _MetricHandle(self, self._resolve(descriptor, "histogram"), "histogram")

    def gauge(self, descriptor: MetricDescriptorRefV1) -> Gauge:
        return _MetricHandle(self, self._resolve(descriptor, "gauge"), "gauge")

    def _resolve(self, ref: MetricDescriptorRefV1, expected_type: str) -> MetricDescriptorV1:
        key = (ref.metric_name, ref.descriptor_version, ref.descriptor_digest)
        descriptor = self._descriptors.get(key)
        if descriptor is None:
            raise IntegrityViolation("metric descriptor ref is not in the frozen registry")
        if descriptor.metric_type != expected_type:
            raise IntegrityViolation(
                "metric handle type differs from descriptor",
                expected=expected_type,
                actual=descriptor.metric_type,
            )
        return descriptor

    def _record(
        self,
        descriptor: MetricDescriptorV1,
        metric_type: str,
        value: float,
        labels: Mapping[str, str],
    ) -> None:
        if not math.isfinite(value):
            raise IntegrityViolation("metric value must be finite")
        expected_labels = set(descriptor.label_keys)
        if set(labels) != expected_labels:
            raise IntegrityViolation(
                "metric label keys differ from exact descriptor",
                expected=sorted(expected_labels),
                actual=sorted(labels),
            )
        if any(not isinstance(item, str) or not item for item in labels.values()):
            raise IntegrityViolation("metric label values must be non-empty strings")
        try:
            point = MetricPointV1(
                point_id=self._id_generator(),
                descriptor=descriptor.ref,
                metric_type=metric_type,
                ts_utc=self._clock.now_utc(),
                value=value,
                labels=dict(labels),
            )
        except Exception:
            self._drop()
            return
        try:
            self._store.record(point)
        except IntegrityViolation:
            raise
        except Exception:
            self._drop()


class _MetricHandle:
    __slots__ = ("_descriptor", "_metric_type", "_sink")

    def __init__(
        self,
        sink: MetricRegistrySink,
        descriptor: MetricDescriptorV1,
        metric_type: str,
    ) -> None:
        self._sink = sink
        self._descriptor = descriptor
        self._metric_type = metric_type

    def add(self, value: float, *, labels: Mapping[str, str]) -> None:
        if self._metric_type != "counter":
            raise IntegrityViolation("add is valid only for counter handles")
        self._sink._record(self._descriptor, self._metric_type, value, labels)

    def record(self, value: float, *, labels: Mapping[str, str]) -> None:
        if self._metric_type != "histogram":
            raise IntegrityViolation("record is valid only for histogram handles")
        self._sink._record(self._descriptor, self._metric_type, value, labels)

    def set(self, value: float, *, labels: Mapping[str, str]) -> None:
        if self._metric_type != "gauge":
            raise IntegrityViolation("set is valid only for gauge handles")
        self._sink._record(self._descriptor, self._metric_type, value, labels)


def metric_descriptor_key(ref: MetricDescriptorRefV1) -> tuple[str, int, str]:
    return (ref.metric_name, ref.descriptor_version, ref.descriptor_digest)


def validate_metric_query_shape(
    query: MetricQueryV1,
    descriptors_by_ref: Mapping[tuple[str, int, str], MetricDescriptorV1],
) -> tuple[MetricDescriptorV1, ...]:
    descriptors: list[MetricDescriptorV1] = []
    for ref in query.descriptor_refs:
        descriptor = descriptors_by_ref.get(metric_descriptor_key(ref))
        if descriptor is None:
            raise IntegrityViolation("metric query references an unknown descriptor")
        descriptors.append(descriptor)
    common_labels = set(descriptors[0].label_keys)
    for descriptor in descriptors[1:]:
        common_labels.intersection_update(descriptor.label_keys)
    if any(matcher.key not in common_labels for matcher in query.label_matchers):
        raise IntegrityViolation("metric matcher is not declared by every descriptor")
    return tuple(descriptors)


def aggregate_metric_points(
    points: Iterable[MetricPointV1],
    descriptors_by_ref: Mapping[tuple[str, int, str], MetricDescriptorV1],
    query: MetricQueryV1,
) -> tuple[MetricSeriesV1, ...]:
    """Aggregate raw points under the frozen epoch-aligned query semantics."""

    validate_metric_query_shape(query, descriptors_by_ref)
    requested = {metric_descriptor_key(ref) for ref in query.descriptor_refs}
    grouped: dict[
        tuple[tuple[str, int, str], str],
        list[MetricPointV1],
    ] = defaultdict(list)
    for point in points:
        descriptor_key = metric_descriptor_key(point.descriptor)
        if descriptor_key not in requested:
            continue
        if not (query.time_range.start_utc <= point.ts_utc < query.time_range.end_utc):
            continue
        if not _matches(point, query):
            continue
        grouped[(descriptor_key, canonical_json(point.labels))].append(point)

    result: list[MetricSeriesV1] = []
    for (descriptor_key, _), series_points in grouped.items():
        descriptor = descriptors_by_ref[descriptor_key]
        series_points.sort(key=lambda item: (item.ts_utc, item.point_id))
        buckets: dict[datetime, list[MetricPointV1]] = defaultdict(list)
        for point in series_points:
            epoch_s = math.floor(point.ts_utc.timestamp())
            bucket_s = epoch_s - epoch_s % query.resolution_s
            buckets[datetime.fromtimestamp(bucket_s, tz=UTC)].append(point)
        if descriptor.metric_type == "histogram":
            samples = []
            for bucket_ts in sorted(buckets):
                values = [item.value for item in buckets[bucket_ts]]
                counts = tuple(
                    sum(value <= bound for value in values)
                    for bound in descriptor.histogram_bucket_bounds
                ) + (len(values),)
                samples.append(
                    HistogramMetricSampleV1(
                        ts_utc=bucket_ts,
                        count=len(values),
                        sum=_finite_sum_or_none(values),
                        cumulative_bucket_counts=counts,
                    )
                )
            series = MetricSeriesV1(
                descriptor=descriptor.ref,
                metric_name=descriptor.metric_name,
                metric_type=descriptor.metric_type,
                unit=descriptor.unit,
                labels=series_points[0].labels,
                bucket_bounds=descriptor.histogram_bucket_bounds,
                histogram_points=tuple(samples),
            )
        else:
            scalar_samples = []
            for bucket_ts in sorted(buckets):
                bucket_points = buckets[bucket_ts]
                value = (
                    sum(item.value for item in bucket_points)
                    if descriptor.metric_type == "counter"
                    else bucket_points[-1].value
                )
                if not math.isfinite(value):
                    raise QueryTooBroad(
                        "counter aggregate is non-finite; narrow the query resolution"
                    )
                scalar_samples.append(ScalarMetricSampleV1(ts_utc=bucket_ts, value=value))
            series = MetricSeriesV1(
                descriptor=descriptor.ref,
                metric_name=descriptor.metric_name,
                metric_type=descriptor.metric_type,
                unit=descriptor.unit,
                labels=series_points[0].labels,
                scalar_points=tuple(scalar_samples),
            )
        result.append(series)
    result.sort(
        key=lambda item: (
            item.descriptor.metric_name,
            item.descriptor.descriptor_version,
            item.descriptor.descriptor_digest,
            canonical_json(item.labels),
        )
    )
    return tuple(result)


def _matches(point: MetricPointV1, query: MetricQueryV1) -> bool:
    for matcher in query.label_matchers:
        value = point.labels[matcher.key]
        if matcher.operation == "eq" and value != matcher.values[0]:
            return False
        if matcher.operation == "in" and value not in matcher.values:
            return False
    return True


def _finite_sum_or_none(values: Iterable[float]) -> float | None:
    try:
        total = math.fsum(values)
    except OverflowError:
        return None
    return total if math.isfinite(total) else None


__all__ = [
    "MetricRegistrySink",
    "aggregate_metric_points",
    "metric_descriptor_key",
    "validate_metric_query_shape",
]
