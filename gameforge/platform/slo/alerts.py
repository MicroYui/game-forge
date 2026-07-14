"""Replayable alert state transitions with repository revision CAS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from string import Formatter

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.slo import (
    AlertDeliveryResultV1,
    AlertInstanceV1,
    AlertRuleV1,
    AlertSink,
    SLODefinitionV1,
    SLOEvaluationV1,
)
from gameforge.contracts.storage import UtcClock
from gameforge.platform.slo.repository import AlertStateRepository


@dataclass(frozen=True, slots=True)
class AlertTransitionResult:
    instance: AlertInstanceV1 | None
    delivery: AlertDeliveryResultV1 | None


class AlertStateMachine:
    """Apply one exact evaluation and publish alert transitions idempotently."""

    def __init__(
        self,
        *,
        repository: AlertStateRepository,
        sink: AlertSink,
        clock: UtcClock,
    ) -> None:
        self._repository = repository
        self._sink = sink
        self._clock = clock

    def process(
        self,
        *,
        definition: SLODefinitionV1,
        rule: AlertRuleV1,
        evaluation: SLOEvaluationV1,
        expected_revision: int | None,
    ) -> AlertTransitionResult:
        now = _require_utc(self._clock.now_utc())
        self._validate_bindings(definition, rule, evaluation)
        dedup_key = _render_dedup_key(rule, definition)
        instance_id = "alert-instance:sha256:" + canonical_sha256(
            {"alert_rule_id": rule.alert_rule_id, "dedup_key": dedup_key}
        )
        current = self._repository.get(instance_id)
        if current is not None and current.last_evaluation_id == evaluation.evaluation_id:
            return self._resume_idempotent_delivery(
                current=current,
                evaluation=evaluation,
                now=now,
            )
        actual_revision = None if current is None else current.revision
        if actual_revision != expected_revision:
            raise Conflict(
                "alert revision does not match",
                actual_revision=actual_revision,
                expected_revision=expected_revision,
            )

        signal = _signal(definition, rule, evaluation)
        candidate = _transition(
            current=current,
            instance_id=instance_id,
            rule=rule,
            dedup_key=dedup_key,
            evaluation=evaluation,
            signal=signal,
            now=now,
        )
        if candidate is None:
            return AlertTransitionResult(instance=None, delivery=None)
        committed = self._repository.compare_and_swap(
            candidate,
            expected_revision=actual_revision,
        )

        if not _delivery_due(current, committed, rule=rule, now=now):
            return AlertTransitionResult(instance=committed, delivery=None)
        return self._deliver_committed(
            committed=committed,
            evaluation=evaluation,
            now=now,
        )

    def _resume_idempotent_delivery(
        self,
        *,
        current: AlertInstanceV1,
        evaluation: SLOEvaluationV1,
        now: datetime,
    ) -> AlertTransitionResult:
        if not _idempotent_delivery_due(current):
            return AlertTransitionResult(instance=current, delivery=None)
        return self._deliver_committed(
            committed=current,
            evaluation=evaluation,
            now=now,
        )

    def _deliver_committed(
        self,
        *,
        committed: AlertInstanceV1,
        evaluation: SLOEvaluationV1,
        now: datetime,
    ) -> AlertTransitionResult:
        idempotency_key = "alert-delivery:sha256:" + canonical_sha256(
            {
                "alert_instance_id": committed.alert_instance_id,
                "revision": committed.revision,
                "state": committed.state,
                "evaluation_id": evaluation.evaluation_id,
            }
        )
        delivery = self._sink.deliver(committed, evaluation, idempotency_key)
        if delivery.status not in {"delivered", "duplicate"}:
            return AlertTransitionResult(instance=committed, delivery=delivery)

        delivered = committed.model_copy(
            update={"last_delivery_at": now, "revision": committed.revision + 1}
        )
        delivered = self._repository.compare_and_swap(
            delivered,
            expected_revision=committed.revision,
        )
        return AlertTransitionResult(instance=delivered, delivery=delivery)

    @staticmethod
    def _validate_bindings(
        definition: SLODefinitionV1,
        rule: AlertRuleV1,
        evaluation: SLOEvaluationV1,
    ) -> None:
        if rule.slo_id != definition.slo_id or evaluation.slo_id != definition.slo_id:
            raise IntegrityViolation("alert rule, definition, and evaluation SLO IDs differ")
        if evaluation.status == "insufficient_data":
            if evaluation.ratio is not None:
                raise IntegrityViolation("insufficient SLO evaluation cannot report a ratio")
            return
        assert evaluation.ratio is not None
        expected_status = "met" if evaluation.ratio >= definition.objective else "breached"
        if evaluation.status != expected_status:
            raise IntegrityViolation("SLO evaluation status differs from frozen objective")


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise IntegrityViolation("alert clock must return timezone-aware UTC")
    return value.astimezone(UTC)


def _render_dedup_key(rule: AlertRuleV1, definition: SLODefinitionV1) -> str:
    values = {
        "alert_rule_id": rule.alert_rule_id,
        "slo_id": definition.slo_id,
        "severity": rule.severity,
        "policy_version": rule.policy_version,
        "workload_profile_id": definition.sli.workload_profile_id,
    }
    try:
        fields = {field for _, field, _, _ in Formatter().parse(rule.dedup_key_template) if field}
    except ValueError as exc:
        raise IntegrityViolation("alert dedup template is invalid") from exc
    unknown = fields - values.keys()
    if unknown:
        raise IntegrityViolation(
            "alert dedup template contains an unknown field",
            unknown_fields=sorted(unknown),
        )
    try:
        rendered = rule.dedup_key_template.format_map(values)
    except (KeyError, ValueError) as exc:
        raise IntegrityViolation("alert dedup template is invalid") from exc
    if not rendered:
        raise IntegrityViolation("alert dedup key cannot be empty")
    return rendered


def _signal(
    definition: SLODefinitionV1,
    rule: AlertRuleV1,
    evaluation: SLOEvaluationV1,
) -> str:
    if evaluation.status == "insufficient_data":
        return rule.insufficient_data_action
    if evaluation.status == "met":
        return "resolve"
    assert evaluation.ratio is not None
    allowed_bad = 1.0 - definition.objective
    observed_bad = 1.0 - evaluation.ratio
    burn_rate = (
        float("inf")
        if allowed_bad == 0 and observed_bad > 0
        else (0.0 if allowed_bad == 0 else observed_bad / allowed_bad)
    )
    return "fire" if burn_rate >= rule.breach_threshold else "resolve"


def _transition(
    *,
    current: AlertInstanceV1 | None,
    instance_id: str,
    rule: AlertRuleV1,
    dedup_key: str,
    evaluation: SLOEvaluationV1,
    signal: str,
    now: datetime,
) -> AlertInstanceV1 | None:
    if current is None:
        if signal in {"hold", "resolve"}:
            return None
        state = "firing" if rule.for_duration_s == 0 else "pending"
        return AlertInstanceV1(
            alert_instance_id=instance_id,
            alert_rule_id=rule.alert_rule_id,
            dedup_key=dedup_key,
            state=state,
            pending_since=now,
            fired_at=now if state == "firing" else None,
            last_evaluation_id=evaluation.evaluation_id,
            revision=1,
        )

    update: dict[str, object] = {
        "last_evaluation_id": evaluation.evaluation_id,
        "revision": current.revision + 1,
    }
    if signal == "hold":
        return current.model_copy(update=update)
    if signal == "resolve":
        if current.state == "resolved":
            return current.model_copy(update=update)
        update.update({"state": "resolved", "resolved_at": now})
        return current.model_copy(update=update)

    if current.state == "resolved":
        state = "firing" if rule.for_duration_s == 0 else "pending"
        update.update(
            {
                "state": state,
                "pending_since": now,
                "fired_at": now if state == "firing" else None,
                "resolved_at": None,
            }
        )
        return current.model_copy(update=update)
    if current.state == "pending":
        assert current.pending_since is not None
        if now >= current.pending_since + timedelta(seconds=rule.for_duration_s):
            update.update({"state": "firing", "fired_at": now})
        return current.model_copy(update=update)
    return current.model_copy(update=update)


def _delivery_due(
    previous: AlertInstanceV1 | None,
    current: AlertInstanceV1,
    *,
    rule: AlertRuleV1,
    now: datetime,
) -> bool:
    if current.state == "pending":
        return False
    previous_state = None if previous is None else previous.state
    if current.state == "resolved":
        return previous_state == "firing"
    if previous_state != "firing":
        return True
    if current.last_delivery_at is None:
        return True
    return now >= current.last_delivery_at + timedelta(seconds=rule.cooldown_s)


def _idempotent_delivery_due(
    current: AlertInstanceV1,
) -> bool:
    if current.state == "pending":
        return False
    if current.state == "resolved":
        return (
            current.fired_at is not None
            and current.resolved_at is not None
            and (current.last_delivery_at is None or current.last_delivery_at < current.resolved_at)
        )
    if current.last_delivery_at is None:
        return True
    return False


__all__ = ["AlertStateMachine", "AlertTransitionResult"]
