"""Deterministic SLO evaluation over exact versioned metric queries."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.observability import (
    HistogramMetricSampleV1,
    MetricDescriptorRefV1,
    MetricDescriptorRegistryV1,
    MetricDescriptorV1,
    MetricLabelMatcherV1,
    MetricQueryStore,
    MetricQueryV1,
    MetricSeriesV1,
    ScalarMetricSampleV1,
    TimeRangeV1,
)
from gameforge.contracts.slo import (
    MetricPredicateV1,
    SLODefinitionV1,
    SLOEvaluationV1,
    WorkloadProfileV1,
)
from gameforge.contracts.storage import UtcClock


_ONLINE_SCOPED_PREFIXES = ("gameforge.provider.", "gameforge.service.")


@dataclass(frozen=True, slots=True)
class SLOEvaluatorLimits:
    """Bound every operational query issued for one evaluation."""

    max_points_per_page: int = 10_000
    max_series_per_page: int = 500

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_points_per_page, bool)
            or isinstance(self.max_series_per_page, bool)
            or self.max_points_per_page <= 0
            or self.max_series_per_page <= 0
        ):
            raise ValueError("SLO evaluator query limits must be positive")


@dataclass(frozen=True, slots=True)
class _PredicateObservation:
    count: int
    total: float
    histogram: HistogramMetricSampleV1 | None = None


ObservationKey = tuple[str, int, str, str, str]


class SLOEvaluator:
    """Evaluate one closed rolling window without mutable metric aliases."""

    def __init__(
        self,
        *,
        metric_store: MetricQueryStore,
        metric_registry: MetricDescriptorRegistryV1,
        clock: UtcClock,
        limits: SLOEvaluatorLimits | None = None,
    ) -> None:
        self._metric_store = metric_store
        self._registry = metric_registry
        self._clock = clock
        self._limits = limits or SLOEvaluatorLimits()
        self._descriptors = {
            _ref_key(descriptor.ref): descriptor for descriptor in metric_registry.descriptors
        }

    def evaluate(
        self,
        *,
        definition: SLODefinitionV1,
        workload_profile: WorkloadProfileV1,
        window_end: datetime,
        late_count: int = 0,
    ) -> SLOEvaluationV1:
        """Return a content-addressed evaluation for a closed SLO window.

        `late_count` is supplied by the ingestion scheduler because MetricPointV1
        intentionally records event time, not an arrival timestamp. Inventing an
        arrival time from event time would make late-data accounting dishonest.
        """

        now = _require_utc(self._clock.now_utc(), field="SLO evaluator clock")
        end = _require_utc(window_end, field="SLO window_end")
        if isinstance(late_count, bool) or not isinstance(late_count, int) or late_count < 0:
            raise ValueError("late_count must be a non-negative integer")
        self._validate_bindings(definition, workload_profile)

        rolling_start = end - timedelta(seconds=definition.rolling_window_s)
        start = max(rolling_start, definition.effective_from)
        if start >= end:
            return SLOEvaluationV1.create(
                slo_id=definition.slo_id,
                window_start=rolling_start,
                window_end=end,
                eligible_count=0,
                good_count=0,
                total_value=0,
                ratio=None,
                missing_count=workload_profile.task_count or 0,
                late_count=late_count,
                status="insufficient_data",
            )

        eligible = self._query_predicate(
            definition=definition,
            predicate=definition.sli.eligible,
            start=start,
            end=end,
            require_total=definition.sli.total_aggregation == "sum",
        )
        good = self._query_predicate(
            definition=definition,
            predicate=definition.sli.good,
            start=start,
            end=end,
            require_total=False,
        )
        observed_eligible = sum(item.count for item in eligible.values())
        if definition.sli.eligible.descriptor == definition.sli.good.descriptor:
            observed_good = sum(
                _intersection_count(
                    eligible.get(key),
                    item,
                    descriptor=self._descriptors[_ref_key(definition.sli.eligible.descriptor)],
                    eligible_predicate=definition.sli.eligible,
                    good_predicate=definition.sli.good,
                )
                for key, item in good.items()
            )
        else:
            observed_good = sum(item.count for item in good.values())
            if observed_good > observed_eligible:
                raise IntegrityViolation(
                    "SLO good metric exceeds its independently observed eligible metric",
                    slo_id=definition.slo_id,
                )

        expected_samples = workload_profile.task_count or 0
        missing_count = max(0, expected_samples - observed_eligible)
        effective_eligible = observed_eligible
        if definition.sli.missing_data == "bad":
            effective_eligible += missing_count

        if definition.sli.total_aggregation == "count":
            total_value = float(effective_eligible)
        elif (
            self._descriptors[_ref_key(definition.sli.eligible.descriptor)].metric_type == "counter"
        ):
            total_value = float(effective_eligible)
        else:
            try:
                total_value = math.fsum(item.total for item in eligible.values())
            except OverflowError as exc:
                raise IntegrityViolation(
                    "SLO total aggregation overflowed",
                    slo_id=definition.slo_id,
                ) from exc
            if not math.isfinite(total_value):
                raise IntegrityViolation(
                    "SLO total aggregation must be finite",
                    slo_id=definition.slo_id,
                )

        grace_complete = now >= end + timedelta(seconds=definition.sli.late_data_grace_s)
        held_for_missing = definition.sli.missing_data == "hold" and missing_count > 0
        enough_samples = effective_eligible >= definition.minimum_samples
        if not grace_complete or held_for_missing or not enough_samples or effective_eligible == 0:
            ratio = None
            status = "insufficient_data"
        else:
            ratio = observed_good / effective_eligible
            status = "met" if ratio >= definition.objective else "breached"

        return SLOEvaluationV1.create(
            slo_id=definition.slo_id,
            window_start=rolling_start,
            window_end=end,
            eligible_count=effective_eligible,
            good_count=observed_good,
            total_value=total_value,
            ratio=ratio,
            missing_count=missing_count,
            late_count=late_count,
            status=status,
        )

    def _validate_bindings(
        self,
        definition: SLODefinitionV1,
        workload_profile: WorkloadProfileV1,
    ) -> None:
        if definition.sli.metric_registry != self._registry.ref:
            raise IntegrityViolation("SLO references a different exact metric registry")
        if definition.sli.workload_profile_id != workload_profile.profile_id:
            raise IntegrityViolation("SLO workload profile binding differs")
        for predicate in (definition.sli.eligible, definition.sli.good):
            descriptor = self._descriptors.get(_ref_key(predicate.descriptor))
            if descriptor is None:
                raise IntegrityViolation("SLO predicate references an unknown exact descriptor")
            if descriptor.unit != predicate.unit:
                raise IntegrityViolation("SLO predicate unit differs from exact descriptor")
            matcher_keys = {matcher.key for matcher in predicate.allowed_label_matchers}
            if not matcher_keys.issubset(descriptor.label_keys):
                raise IntegrityViolation("SLO predicate matcher is absent from exact descriptor")
            _require_exact_matcher(
                predicate.allowed_label_matchers,
                key="workload_profile_id",
                value=workload_profile.profile_id,
                detail="SLO metric query must bind the exact workload profile",
            )
            if "execution_source" in descriptor.label_keys or descriptor.metric_name.startswith(
                _ONLINE_SCOPED_PREFIXES
            ):
                if "execution_source" not in descriptor.label_keys:
                    raise IntegrityViolation(
                        "online provider/service SLO descriptor lacks execution_source"
                    )
                _require_exact_matcher(
                    predicate.allowed_label_matchers,
                    key="execution_source",
                    value="online",
                    detail="online provider/service SLO must exclude replay observations",
                )

    def _query_predicate(
        self,
        *,
        definition: SLODefinitionV1,
        predicate: MetricPredicateV1,
        start: datetime,
        end: datetime,
        require_total: bool,
    ) -> dict[ObservationKey, _PredicateObservation]:
        descriptor = self._descriptors[_ref_key(predicate.descriptor)]
        cursor: str | None = None
        observations: dict[ObservationKey, _PredicateObservation] = {}
        authz_fingerprint = canonical_sha256(
            {
                "purpose": "slo-evaluation",
                "slo_id": definition.slo_id,
                "policy_version": definition.policy_version,
            }
        )
        while True:
            query = MetricQueryV1(
                descriptor_refs=(predicate.descriptor,),
                time_range=TimeRangeV1(start_utc=start, end_utc=end),
                resolution_s=definition.evaluation_interval_s,
                label_matchers=predicate.allowed_label_matchers,
                max_points=self._limits.max_points_per_page,
                cursor=cursor,
                series_limit=self._limits.max_series_per_page,
                authz_fingerprint=authz_fingerprint,
            )
            page = self._metric_store.query_metrics(query)
            for series in page.series:
                self._consume_series(
                    observations,
                    descriptor,
                    predicate,
                    series,
                    total_aggregation=definition.sli.total_aggregation,
                    require_total=require_total,
                )
            cursor = page.next_cursor
            if cursor is None:
                return observations

    @staticmethod
    def _consume_series(
        target: dict[ObservationKey, _PredicateObservation],
        descriptor: MetricDescriptorV1,
        predicate: MetricPredicateV1,
        series: MetricSeriesV1,
        total_aggregation: str,
        require_total: bool,
    ) -> None:
        label_key = canonical_json(series.labels)
        if series.scalar_points is not None:
            for sample in series.scalar_points:
                observation = _scalar_observation(
                    sample,
                    predicate,
                    descriptor=descriptor,
                    total_aggregation=total_aggregation,
                )
                if observation.count:
                    target[_observation_key(descriptor.ref, label_key, sample.ts_utc)] = observation
            return
        assert series.histogram_points is not None
        for sample in series.histogram_points:
            observation = _histogram_observation(
                sample,
                descriptor=descriptor,
                predicate=predicate,
                total_aggregation=total_aggregation,
                require_total=require_total,
            )
            if observation.count:
                target[_observation_key(descriptor.ref, label_key, sample.ts_utc)] = observation


def _ref_key(ref: MetricDescriptorRefV1) -> tuple[str, int, str]:
    return (ref.metric_name, ref.descriptor_version, ref.descriptor_digest)


def _observation_key(
    ref: MetricDescriptorRefV1,
    labels: str,
    ts: datetime,
) -> ObservationKey:
    return (*_ref_key(ref), labels, ts.isoformat())


def _require_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise IntegrityViolation(f"{field} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _require_exact_matcher(
    matchers: tuple[MetricLabelMatcherV1, ...],
    *,
    key: str,
    value: str,
    detail: str,
) -> None:
    matcher = next((item for item in matchers if item.key == key), None)
    if matcher is None or matcher.operation != "eq" or matcher.values != (value,):
        raise IntegrityViolation(detail)


def _compare(value: float, predicate: MetricPredicateV1) -> bool:
    threshold = predicate.threshold
    if predicate.comparator == "lt":
        return value < threshold
    if predicate.comparator == "lte":
        return value <= threshold
    if predicate.comparator == "eq":
        return value == threshold
    if predicate.comparator == "gte":
        return value >= threshold
    return value > threshold


def _scalar_observation(
    sample: ScalarMetricSampleV1,
    predicate: MetricPredicateV1,
    *,
    descriptor: MetricDescriptorV1,
    total_aggregation: str,
) -> _PredicateObservation:
    if not _compare(sample.value, predicate):
        return _PredicateObservation(0, 0)
    if total_aggregation == "sum" and descriptor.metric_type == "counter":
        if sample.value < 0 or not sample.value.is_integer():
            raise IntegrityViolation(
                "counter-backed SLO sum must produce a non-negative integer event count"
            )
        return _PredicateObservation(int(sample.value), sample.value)
    return _PredicateObservation(1, sample.value)


def _histogram_observation(
    sample: HistogramMetricSampleV1,
    *,
    descriptor: MetricDescriptorV1,
    predicate: MetricPredicateV1,
    total_aggregation: str,
    require_total: bool,
) -> _PredicateObservation:
    try:
        boundary_index = descriptor.histogram_bucket_bounds.index(predicate.threshold)
    except ValueError as exc:
        raise IntegrityViolation(
            "histogram SLO threshold must equal an exact frozen bucket boundary"
        ) from exc
    cumulative = sample.cumulative_bucket_counts[boundary_index]
    if predicate.comparator == "lte":
        count = cumulative
    elif predicate.comparator == "gt":
        count = sample.count - cumulative
    else:
        raise IntegrityViolation(
            "histogram SLO predicates support only exact lte/gt bucket semantics"
        )
    if total_aggregation == "sum" and require_total:
        if count != sample.count:
            raise IntegrityViolation(
                "histogram sum cannot be derived for a predicate-selected subset"
            )
        if sample.sum is None:
            raise IntegrityViolation("histogram SLO sum is unavailable")
        total = float(sample.sum)
    elif count != sample.count:
        total = float(count)
    else:
        total = float(sample.sum or 0)
    return _PredicateObservation(count, total, sample)


def _intersection_count(
    eligible: _PredicateObservation | None,
    good: _PredicateObservation,
    *,
    descriptor: MetricDescriptorV1,
    eligible_predicate: MetricPredicateV1,
    good_predicate: MetricPredicateV1,
) -> int:
    if eligible is None:
        return 0
    if eligible.histogram is None or good.histogram is None:
        return min(eligible.count, good.count)
    if eligible.histogram != good.histogram:
        raise IntegrityViolation("SLO histogram queries returned inconsistent exact samples")
    sample = eligible.histogram
    eligible_bins = _histogram_selected_bins(descriptor, eligible_predicate)
    good_bins = _histogram_selected_bins(descriptor, good_predicate)
    selected = eligible_bins.intersection(good_bins)
    per_bucket = _histogram_bucket_counts(sample)
    return sum(per_bucket[index] for index in selected)


def _histogram_selected_bins(
    descriptor: MetricDescriptorV1,
    predicate: MetricPredicateV1,
) -> set[int]:
    boundary_index = descriptor.histogram_bucket_bounds.index(predicate.threshold)
    all_bins = set(range(len(descriptor.histogram_bucket_bounds) + 1))
    if predicate.comparator == "lte":
        return set(range(boundary_index + 1))
    if predicate.comparator == "gt":
        return all_bins - set(range(boundary_index + 1))
    raise IntegrityViolation("histogram SLO predicates support only exact lte/gt bucket semantics")


def _histogram_bucket_counts(sample: HistogramMetricSampleV1) -> tuple[int, ...]:
    cumulative = sample.cumulative_bucket_counts
    result = [cumulative[0]]
    result.extend(right - left for left, right in zip(cumulative, cumulative[1:]))
    return tuple(result)


__all__ = ["SLOEvaluator", "SLOEvaluatorLimits"]
