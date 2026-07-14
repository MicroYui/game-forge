from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.observability import (
    MetricDescriptorRefV1,
    MetricDescriptorRegistryRefV1,
)
from gameforge.contracts.slo import (
    AlertInstanceV1,
    AlertRuleV1,
    MetricPredicateV1,
    SLIDefinitionV1,
    SLODefinitionV1,
    SLOEvaluationV1,
)
from gameforge.platform.slo.alerts import AlertStateMachine
from gameforge.platform.slo.repository import InMemoryAlertStateRepository
from gameforge.runtime.slo.sinks import FileAlertSink, InMemoryAlertSink


T0 = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


@dataclass
class _Clock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _definition() -> SLODefinitionV1:
    descriptor = MetricDescriptorRefV1(
        metric_name="gameforge.checker.duration",
        descriptor_version=1,
        descriptor_digest="d" * 64,
    )
    predicate = MetricPredicateV1(
        descriptor=descriptor,
        allowed_label_matchers=(),
        comparator="lte",
        threshold=100,
        unit="ms",
    )
    return SLODefinitionV1(
        slo_id="checker-latency",
        name="Checker latency",
        sli=SLIDefinitionV1(
            metric_registry=MetricDescriptorRegistryRefV1(
                registry_version=1,
                registry_digest="e" * 64,
            ),
            eligible=predicate,
            good=predicate,
            total_aggregation="count",
            workload_profile_id="checker-baseline",
            missing_data="hold",
            late_data_grace_s=0,
            policy_version="sli@1",
        ),
        objective=0.99,
        rolling_window_s=3600,
        minimum_samples=10,
        evaluation_interval_s=60,
        effective_from=T0 - timedelta(days=1),
        policy_version="slo@1",
    )


def _rule(
    *,
    for_duration_s: int = 300,
    cooldown_s: int = 900,
    insufficient_data_action: str = "hold",
    breach_threshold: float = 1,
) -> AlertRuleV1:
    return AlertRuleV1(
        alert_rule_id="checker-latency-page",
        slo_id="checker-latency",
        breach_threshold=breach_threshold,
        for_duration_s=for_duration_s,
        severity="critical",
        dedup_key_template="{slo_id}:{severity}",
        cooldown_s=cooldown_s,
        insufficient_data_action=insufficient_data_action,
        policy_version="alert@1",
    )


def _evaluation(
    status: str,
    *,
    ratio: float | None = None,
    offset_s: int = 0,
) -> SLOEvaluationV1:
    if status == "met" and ratio is None:
        ratio = 1.0
    if status == "breached" and ratio is None:
        ratio = 0.95
    return SLOEvaluationV1.create(
        slo_id="checker-latency",
        window_start=T0 - timedelta(hours=1) + timedelta(seconds=offset_s),
        window_end=T0 + timedelta(seconds=offset_s),
        eligible_count=100,
        good_count=0 if ratio is None else round(100 * ratio),
        total_value=100,
        ratio=ratio,
        missing_count=1 if status == "insufficient_data" else 0,
        late_count=0,
        status=status,
    )


def test_alert_replays_pending_firing_and_resolved_with_cas() -> None:
    clock = _Clock(T0)
    repository = InMemoryAlertStateRepository()
    sink = InMemoryAlertSink()
    machine = AlertStateMachine(repository=repository, sink=sink, clock=clock)

    pending = machine.process(
        definition=_definition(),
        rule=_rule(),
        evaluation=_evaluation("breached"),
        expected_revision=None,
    )
    assert pending.instance is not None
    assert pending.instance.state == "pending"
    assert pending.instance.dedup_key == "checker-latency:critical"
    assert pending.delivery is None

    clock.advance(timedelta(seconds=299))
    still_pending = machine.process(
        definition=_definition(),
        rule=_rule(),
        evaluation=_evaluation("breached", offset_s=299),
        expected_revision=pending.instance.revision,
    )
    assert still_pending.instance.state == "pending"

    clock.advance(timedelta(seconds=1))
    firing = machine.process(
        definition=_definition(),
        rule=_rule(),
        evaluation=_evaluation("breached", offset_s=300),
        expected_revision=still_pending.instance.revision,
    )
    assert firing.instance.state == "firing"
    assert firing.instance.fired_at == T0 + timedelta(seconds=300)
    assert firing.instance.last_delivery_at == clock.current
    assert firing.delivery is not None and firing.delivery.status == "delivered"
    assert len(sink.deliveries) == 1

    replayed = machine.process(
        definition=_definition(),
        rule=_rule(),
        evaluation=_evaluation("breached", offset_s=300),
        expected_revision=still_pending.instance.revision,
    )
    assert replayed.instance == firing.instance
    assert replayed.delivery is None
    assert len(sink.deliveries) == 1

    clock.advance(timedelta(seconds=1))
    resolved = machine.process(
        definition=_definition(),
        rule=_rule(),
        evaluation=_evaluation("met", offset_s=301),
        expected_revision=firing.instance.revision,
    )
    assert resolved.instance.state == "resolved"
    assert resolved.instance.resolved_at == clock.current
    assert resolved.delivery is not None and resolved.delivery.status == "delivered"
    assert len(sink.deliveries) == 2

    with pytest.raises(Conflict, match="revision"):
        machine.process(
            definition=_definition(),
            rule=_rule(),
            evaluation=_evaluation("breached", offset_s=302),
            expected_revision=firing.instance.revision,
        )


def test_insufficient_actions_and_burn_threshold_are_explicit() -> None:
    clock = _Clock(T0)
    repository = InMemoryAlertStateRepository()
    sink = InMemoryAlertSink()
    machine = AlertStateMachine(repository=repository, sink=sink, clock=clock)

    held = machine.process(
        definition=_definition(),
        rule=_rule(insufficient_data_action="hold"),
        evaluation=_evaluation("insufficient_data"),
        expected_revision=None,
    )
    assert held.instance is None

    fired = machine.process(
        definition=_definition(),
        rule=_rule(for_duration_s=0, insufficient_data_action="fire"),
        evaluation=_evaluation("insufficient_data"),
        expected_revision=None,
    )
    assert fired.instance is not None and fired.instance.state == "firing"

    clock.advance(timedelta(seconds=1))
    resolved = machine.process(
        definition=_definition(),
        rule=_rule(for_duration_s=0, insufficient_data_action="resolve"),
        evaluation=_evaluation("insufficient_data", offset_s=1),
        expected_revision=fired.instance.revision,
    )
    assert resolved.instance.state == "resolved"

    other_repo = InMemoryAlertStateRepository()
    below_burn_threshold = AlertStateMachine(
        repository=other_repo,
        sink=InMemoryAlertSink(),
        clock=clock,
    ).process(
        definition=_definition(),
        rule=_rule(breach_threshold=10),
        evaluation=_evaluation("breached", ratio=0.98, offset_s=1),
        expected_revision=None,
    )
    assert below_burn_threshold.instance is None


def test_cooldown_suppresses_repeat_delivery_without_changing_firing_state() -> None:
    clock = _Clock(T0)
    repository = InMemoryAlertStateRepository()
    sink = InMemoryAlertSink()
    machine = AlertStateMachine(repository=repository, sink=sink, clock=clock)
    rule = _rule(for_duration_s=0, cooldown_s=60)

    first = machine.process(
        definition=_definition(),
        rule=rule,
        evaluation=_evaluation("breached"),
        expected_revision=None,
    )
    assert first.instance.state == "firing"
    assert len(sink.deliveries) == 1

    clock.advance(timedelta(seconds=59))
    suppressed = machine.process(
        definition=_definition(),
        rule=rule,
        evaluation=_evaluation("breached", offset_s=59),
        expected_revision=first.instance.revision,
    )
    assert suppressed.instance.state == "firing"
    assert suppressed.delivery is None
    assert len(sink.deliveries) == 1

    clock.advance(timedelta(seconds=1))
    repeated = machine.process(
        definition=_definition(),
        rule=rule,
        evaluation=_evaluation("breached", offset_s=60),
        expected_revision=suppressed.instance.revision,
    )
    assert repeated.instance.state == "firing"
    assert repeated.delivery is not None
    assert len(sink.deliveries) == 2


def test_same_evaluation_replay_never_redelivers_after_cooldown() -> None:
    clock = _Clock(T0)
    repository = InMemoryAlertStateRepository()
    sink = InMemoryAlertSink()
    machine = AlertStateMachine(repository=repository, sink=sink, clock=clock)
    rule = _rule(for_duration_s=0, cooldown_s=60)
    evaluation = _evaluation("breached")

    first = machine.process(
        definition=_definition(),
        rule=rule,
        evaluation=evaluation,
        expected_revision=None,
    )
    assert first.instance is not None
    assert first.delivery is not None and first.delivery.status == "delivered"

    clock.advance(timedelta(seconds=60))
    replayed = machine.process(
        definition=_definition(),
        rule=rule,
        evaluation=evaluation,
        expected_revision=None,
    )

    assert replayed.instance == first.instance
    assert replayed.delivery is None
    assert len(sink.deliveries) == 1


def test_sink_failure_does_not_rewrite_alert_state() -> None:
    clock = _Clock(T0)
    repository = InMemoryAlertStateRepository()
    sink = InMemoryAlertSink(fail_all=True)
    result = AlertStateMachine(repository=repository, sink=sink, clock=clock).process(
        definition=_definition(),
        rule=_rule(for_duration_s=0),
        evaluation=_evaluation("breached"),
        expected_revision=None,
    )

    assert result.instance is not None and result.instance.state == "firing"
    assert result.instance.last_delivery_at is None
    assert result.delivery is not None and result.delivery.status == "failed"
    assert repository.get(result.instance.alert_instance_id) == result.instance

    replayed = AlertStateMachine(repository=repository, sink=sink, clock=clock).process(
        definition=_definition(),
        rule=_rule(for_duration_s=0),
        evaluation=_evaluation("breached"),
        expected_revision=None,
    )
    assert replayed.instance == result.instance
    assert replayed.delivery is not None and replayed.delivery.status == "failed"


def test_in_memory_and_file_sinks_are_idempotent_and_deterministic(tmp_path: Path) -> None:
    evaluation = _evaluation("breached")
    alert = AlertInstanceV1(
        alert_instance_id="alert-1",
        alert_rule_id="rule-1",
        dedup_key="dedup-1",
        state="firing",
        pending_since=T0,
        fired_at=T0,
        last_evaluation_id=evaluation.evaluation_id,
        revision=1,
    )
    memory = InMemoryAlertSink()
    assert memory.deliver(alert, evaluation, "delivery-1").status == "delivered"
    assert memory.deliver(alert, evaluation, "delivery-1").status == "duplicate"
    with pytest.raises(IntegrityViolation, match="idempotency"):
        memory.deliver(alert.model_copy(update={"revision": 2}), evaluation, "delivery-1")

    path = tmp_path / "alerts.ndjson"
    file_sink = FileAlertSink(path)
    assert file_sink.deliver(alert, evaluation, "delivery-1").status == "delivered"
    first_bytes = path.read_bytes()
    assert first_bytes.endswith(b"\n")

    reopened = FileAlertSink(path)
    assert reopened.deliver(alert, evaluation, "delivery-1").status == "duplicate"
    assert path.read_bytes() == first_bytes
    with pytest.raises(IntegrityViolation, match="idempotency"):
        reopened.deliver(alert.model_copy(update={"revision": 2}), evaluation, "delivery-1")

    failed_path = tmp_path / "became-a-directory"
    failing = FileAlertSink(failed_path)
    failed_path.mkdir()
    failure = failing.deliver(alert, evaluation, "delivery-failure")
    assert failure.status == "failed"
    assert failure.detail == "IsADirectoryError"
