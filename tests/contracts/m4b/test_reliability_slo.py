from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from gameforge.contracts.observability import (
    MetricDescriptorRefV1,
    MetricDescriptorRegistryRefV1,
    MetricLabelMatcherV1,
)
from gameforge.contracts.reliability import (
    CircuitBreakerConfigV1,
    FailureClassificationV1,
    RetryPolicyV1,
)
from gameforge.contracts.slo import (
    AlertInstanceV1,
    AlertRuleV1,
    MetricPredicateV1,
    SLIDefinitionV1,
    SLODefinitionV1,
    SLOEvaluationV1,
    WorkloadProfileV1,
)


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def _descriptor_ref() -> MetricDescriptorRefV1:
    return MetricDescriptorRefV1(
        metric_name="gameforge.provider.request.duration",
        descriptor_version=1,
        descriptor_digest="1" * 64,
    )


def test_failure_classification_cannot_retry_product_or_unproven_outcomes() -> None:
    transient = FailureClassificationV1(
        failure_kind="transient_infrastructure",
        retryable=True,
        counts_for_breaker=True,
        idempotency_required=True,
        reason_code="gateway_timeout",
        retry_after_s=2,
    )
    assert transient.counts_for_breaker

    with pytest.raises(ValidationError, match="infrastructure"):
        FailureClassificationV1(
            failure_kind="solver_unproven",
            retryable=False,
            counts_for_breaker=True,
            idempotency_required=False,
            reason_code="unproven",
        )


def test_retry_and_breaker_configs_are_versioned_and_bounded() -> None:
    retry = RetryPolicyV1(
        policy_version="retry@1",
        failure_classifier_version="failure-classifier@1",
        max_attempts=4,
        initial_backoff_ms=100,
        max_backoff_ms=1000,
        multiplier=2,
        jitter_ratio=0.1,
    )
    breaker = CircuitBreakerConfigV1(
        config_version="breaker@1",
        rolling_window_s=60,
        minimum_samples=5,
        failure_threshold=0.5,
        open_cooldown_s=30,
        half_open_max_concurrent_probes=1,
        half_open_success_threshold=2,
    )
    assert retry.max_backoff_ms >= retry.initial_backoff_ms
    assert breaker.half_open_success_threshold >= breaker.half_open_max_concurrent_probes


def test_slo_definition_requires_exact_metric_refs_threshold_and_window() -> None:
    predicate = MetricPredicateV1(
        descriptor=_descriptor_ref(),
        allowed_label_matchers=(
            MetricLabelMatcherV1(key="execution_source", operation="eq", values=("online",)),
        ),
        comparator="lte",
        threshold=500,
        unit="ms",
    )
    sli = SLIDefinitionV1(
        metric_registry=MetricDescriptorRegistryRefV1(
            registry_version=1,
            registry_digest="2" * 64,
        ),
        eligible=predicate,
        good=predicate,
        total_aggregation="count",
        workload_profile_id="provider-online",
        missing_data="hold",
        late_data_grace_s=60,
        policy_version="sli@1",
    )
    slo = SLODefinitionV1(
        slo_id="provider-latency",
        name="Provider request latency",
        sli=sli,
        objective=0.99,
        rolling_window_s=3600,
        minimum_samples=10,
        evaluation_interval_s=60,
        effective_from=NOW,
        policy_version="slo@1",
    )
    assert slo.rolling_window_s == 3600

    with pytest.raises(ValidationError):
        SLODefinitionV1(**slo.model_dump(exclude={"rolling_window_s"}), rolling_window_s=0)

    for non_finite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError, match="finite"):
            SLODefinitionV1(
                **slo.model_dump(exclude={"objective"}),
                objective=non_finite,
            )


def test_slo_evaluation_and_alert_state_times_are_closed() -> None:
    evaluation = SLOEvaluationV1.create(
        slo_id="provider-latency",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        eligible_count=100,
        good_count=95,
        total_value=100,
        ratio=0.95,
        missing_count=0,
        late_count=0,
        status="breached",
    )
    rule = AlertRuleV1(
        alert_rule_id="provider-latency-page",
        slo_id=evaluation.slo_id,
        breach_threshold=1,
        for_duration_s=300,
        severity="critical",
        dedup_key_template="provider-latency",
        cooldown_s=900,
        insufficient_data_action="hold",
        policy_version="alert@1",
    )
    instance = AlertInstanceV1(
        alert_instance_id="alert-1",
        alert_rule_id=rule.alert_rule_id,
        dedup_key="provider-latency",
        state="pending",
        pending_since=NOW,
        last_evaluation_id=evaluation.evaluation_id,
        revision=1,
    )
    assert instance.pending_since == NOW

    with pytest.raises(ValidationError, match="pending"):
        AlertInstanceV1(
            **instance.model_dump(exclude={"pending_since"}),
            pending_since=None,
        )


@pytest.mark.parametrize("field", ("total_value", "ratio"))
@pytest.mark.parametrize("non_finite", (float("nan"), float("inf"), float("-inf")))
def test_slo_evaluation_rejects_non_finite_wire_values(
    field: str,
    non_finite: float,
) -> None:
    evaluation = SLOEvaluationV1.create(
        slo_id="provider-latency",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        eligible_count=100,
        good_count=95,
        total_value=100,
        ratio=0.95,
        missing_count=0,
        late_count=0,
        status="breached",
    )

    with pytest.raises(ValidationError, match="finite"):
        SLOEvaluationV1.model_validate({**evaluation.model_dump(mode="python"), field: non_finite})


@pytest.mark.parametrize("non_finite", (float("nan"), float("inf"), float("-inf")))
def test_alert_rule_rejects_non_finite_breach_threshold(non_finite: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        AlertRuleV1(
            alert_rule_id="provider-latency-page",
            slo_id="provider-latency",
            breach_threshold=non_finite,
            for_duration_s=300,
            severity="critical",
            dedup_key_template="provider-latency",
            cooldown_s=900,
            insufficient_data_action="hold",
            policy_version="alert@1",
        )


@pytest.mark.parametrize(
    ("eligible_count", "good_count", "ratio"),
    (
        (100, 0, 1.0),
        (0, 0, 1.0),
    ),
)
def test_slo_evaluation_ratio_is_derived_from_eligible_and_good_counts(
    eligible_count: int,
    good_count: int,
    ratio: float,
) -> None:
    with pytest.raises(ValidationError, match="ratio|eligible"):
        SLOEvaluationV1.create(
            slo_id="provider-latency",
            window_start=NOW - timedelta(hours=1),
            window_end=NOW,
            eligible_count=eligible_count,
            good_count=good_count,
            total_value=eligible_count,
            ratio=ratio,
            missing_count=0,
            late_count=0,
            status="met",
        )


def test_workload_profile_binds_measured_shape() -> None:
    profile = WorkloadProfileV1(
        profile_id="regression-50",
        dataset_artifact_id="artifact-dataset",
        entity_count=1000,
        relation_count=2500,
        constraint_count=100,
        task_count=50,
        concurrency=2,
        environment_fingerprint="3" * 64,
    )
    assert profile.task_count == 50
