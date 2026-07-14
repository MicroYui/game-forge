"""Transaction-bound persistence for immutable SLO authority and alert heads."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import typed_canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation, QueryTooBroad
from gameforge.contracts.slo import (
    AlertInstanceV1,
    AlertRuleV1,
    SLODefinitionV1,
    SLOEvaluationV1,
    WorkloadProfileV1,
)
from gameforge.runtime.persistence.models import (
    AlertInstanceRow,
    AlertRuleRow,
    SLODefinitionRow,
    SLOEvaluationRow,
    WorkloadProfileRow,
)


_ModelT = TypeVar("_ModelT", bound=BaseModel)
MAX_EVALUATION_QUERY_ITEMS = 10_000
MAX_SLO_DEFINITION_QUERY_ITEMS = 10_000


def _canonical_model(value: object, model_type: type[_ModelT], *, label: str) -> _ModelT:
    if type(value) is not model_type:
        raise IntegrityViolation(f"{label} requires an exact {model_type.__name__}")
    try:
        wire = value.model_dump(mode="json")  # type: ignore[union-attr]
        parsed = model_type.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} wire is invalid") from exc
    if typed_canonical_json(parsed.model_dump(mode="json")) != typed_canonical_json(wire):
        raise IntegrityViolation(f"{label} wire is noncanonical")
    return parsed


def _parse_payload(
    payload: object,
    model_type: type[_ModelT],
    *,
    label: str,
    identity: str,
) -> _ModelT:
    if not isinstance(payload, dict):
        raise IntegrityViolation(f"stored {label} payload is not an object", identity=identity)
    try:
        parsed = model_type.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"stored {label} is invalid", identity=identity) from exc
    if typed_canonical_json(parsed.model_dump(mode="json")) != typed_canonical_json(payload):
        raise IntegrityViolation(f"stored {label} payload is noncanonical", identity=identity)
    return parsed


def _require_projection(
    row: object,
    model: BaseModel,
    fields: Sequence[str],
    *,
    label: str,
    identity: str,
) -> None:
    wire = model.model_dump(mode="json")
    for field_name in fields:
        if getattr(row, field_name) != wire[field_name]:
            raise IntegrityViolation(
                f"stored {label} projection differs from its payload",
                identity=identity,
                field=field_name,
            )


def _same_model(left: BaseModel, right: BaseModel) -> bool:
    return typed_canonical_json(left.model_dump(mode="json")) == typed_canonical_json(
        right.model_dump(mode="json")
    )


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_EVALUATION_QUERY_ITEMS:
        raise QueryTooBroad(
            "SLO evaluation query limit is outside the supported range",
            max_limit=MAX_EVALUATION_QUERY_ITEMS,
        )
    return limit


class SqlSloRepository:
    """Share the owning UoW Session and never commit independently."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def put_workload_profile(self, profile: WorkloadProfileV1) -> WorkloadProfileV1:
        canonical = _canonical_model(profile, WorkloadProfileV1, label="workload profile")
        existing = self.get_workload_profile(canonical.profile_id)
        if existing is not None:
            if not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "workload profile id has different authoritative content",
                    profile_id=canonical.profile_id,
                )
            return existing
        wire = canonical.model_dump(mode="json")
        self._session.add(
            WorkloadProfileRow(
                profile_id=canonical.profile_id,
                dataset_artifact_id=canonical.dataset_artifact_id,
                entity_count=canonical.entity_count,
                relation_count=canonical.relation_count,
                constraint_count=canonical.constraint_count,
                task_count=canonical.task_count,
                concurrency=canonical.concurrency,
                environment_fingerprint=canonical.environment_fingerprint,
                payload=wire,
            )
        )
        self._flush("workload profile", profile_id=canonical.profile_id)
        return canonical

    def get_workload_profile(self, profile_id: str) -> WorkloadProfileV1 | None:
        row = self._session.get(WorkloadProfileRow, profile_id)
        if row is None:
            return None
        parsed = _parse_payload(
            row.payload,
            WorkloadProfileV1,
            label="workload profile",
            identity=profile_id,
        )
        _require_projection(
            row,
            parsed,
            (
                "profile_id",
                "dataset_artifact_id",
                "entity_count",
                "relation_count",
                "constraint_count",
                "task_count",
                "concurrency",
                "environment_fingerprint",
            ),
            label="workload profile",
            identity=profile_id,
        )
        return parsed

    def put_definition(self, definition: SLODefinitionV1) -> SLODefinitionV1:
        canonical = _canonical_model(definition, SLODefinitionV1, label="SLO definition")
        profile_id = canonical.sli.workload_profile_id
        if self.get_workload_profile(profile_id) is None:
            raise IntegrityViolation(
                "SLO definition references an unavailable workload profile",
                workload_profile_id=profile_id,
            )
        existing = self.get_definition(canonical.slo_id)
        if existing is not None:
            if not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "SLO definition id has different authoritative content",
                    slo_id=canonical.slo_id,
                )
            return existing
        wire = canonical.model_dump(mode="json")
        self._session.add(
            SLODefinitionRow(
                slo_id=canonical.slo_id,
                workload_profile_id=profile_id,
                name=canonical.name,
                policy_version=canonical.policy_version,
                effective_from=wire["effective_from"],
                rolling_window_s=canonical.rolling_window_s,
                evaluation_interval_s=canonical.evaluation_interval_s,
                payload=wire,
            )
        )
        self._flush("SLO definition", slo_id=canonical.slo_id)
        return canonical

    def get_definition(self, slo_id: str) -> SLODefinitionV1 | None:
        row = self._session.get(SLODefinitionRow, slo_id)
        if row is None:
            return None
        return self._definition_from_row(row)

    def list_live_definitions(
        self,
        *,
        limit: int = MAX_SLO_DEFINITION_QUERY_ITEMS,
    ) -> tuple[SLODefinitionV1, ...]:
        """List all v1 definitions; v1 has no retired lifecycle state."""

        if isinstance(limit, bool) or not 1 <= limit <= MAX_SLO_DEFINITION_QUERY_ITEMS:
            raise QueryTooBroad(
                "SLO definition reconciliation limit is outside the supported range",
                max_limit=MAX_SLO_DEFINITION_QUERY_ITEMS,
            )
        rows = self._session.scalars(
            select(SLODefinitionRow).order_by(SLODefinitionRow.slo_id).limit(limit + 1)
        ).all()
        if len(rows) > limit:
            raise QueryTooBroad(
                "SLO definition reconciliation exceeds the configured limit",
                max_definitions=limit,
            )
        return tuple(self._definition_from_row(row) for row in rows)

    def _definition_from_row(self, row: SLODefinitionRow) -> SLODefinitionV1:
        slo_id = row.slo_id
        parsed = _parse_payload(
            row.payload,
            SLODefinitionV1,
            label="SLO definition",
            identity=slo_id,
        )
        _require_projection(
            row,
            parsed,
            (
                "slo_id",
                "name",
                "policy_version",
                "effective_from",
                "rolling_window_s",
                "evaluation_interval_s",
            ),
            label="SLO definition",
            identity=slo_id,
        )
        if row.workload_profile_id != parsed.sli.workload_profile_id:
            raise IntegrityViolation(
                "stored SLO definition workload projection differs from its payload",
                slo_id=slo_id,
            )
        return parsed

    def put_rule(self, rule: AlertRuleV1) -> AlertRuleV1:
        canonical = _canonical_model(rule, AlertRuleV1, label="alert rule")
        if self.get_definition(canonical.slo_id) is None:
            raise IntegrityViolation(
                "alert rule references an unavailable SLO definition",
                slo_id=canonical.slo_id,
            )
        existing = self.get_rule(canonical.alert_rule_id)
        if existing is not None:
            if not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "alert rule id has different authoritative content",
                    alert_rule_id=canonical.alert_rule_id,
                )
            return existing
        wire = canonical.model_dump(mode="json")
        self._session.add(
            AlertRuleRow(
                alert_rule_id=canonical.alert_rule_id,
                slo_id=canonical.slo_id,
                severity=canonical.severity,
                policy_version=canonical.policy_version,
                payload=wire,
            )
        )
        self._flush("alert rule", alert_rule_id=canonical.alert_rule_id)
        return canonical

    def get_rule(self, alert_rule_id: str) -> AlertRuleV1 | None:
        row = self._session.get(AlertRuleRow, alert_rule_id)
        if row is None:
            return None
        parsed = _parse_payload(
            row.payload,
            AlertRuleV1,
            label="alert rule",
            identity=alert_rule_id,
        )
        _require_projection(
            row,
            parsed,
            ("alert_rule_id", "slo_id", "severity", "policy_version"),
            label="alert rule",
            identity=alert_rule_id,
        )
        return parsed

    def put_evaluation(self, evaluation: SLOEvaluationV1) -> SLOEvaluationV1:
        canonical = _canonical_model(evaluation, SLOEvaluationV1, label="SLO evaluation")
        if self.get_definition(canonical.slo_id) is None:
            raise IntegrityViolation(
                "SLO evaluation references an unavailable SLO definition",
                slo_id=canonical.slo_id,
            )
        existing = self.get_evaluation(canonical.evaluation_id)
        if existing is not None:
            if not _same_model(existing, canonical):
                raise IntegrityViolation(
                    "SLO evaluation id has different authoritative content",
                    evaluation_id=canonical.evaluation_id,
                )
            return existing
        wire = canonical.model_dump(mode="json")
        self._session.add(
            SLOEvaluationRow(
                evaluation_id=canonical.evaluation_id,
                slo_id=canonical.slo_id,
                window_start=wire["window_start"],
                window_end=wire["window_end"],
                status=canonical.status,
                payload=wire,
            )
        )
        self._flush("SLO evaluation", evaluation_id=canonical.evaluation_id)
        return canonical

    def get_evaluation(self, evaluation_id: str) -> SLOEvaluationV1 | None:
        row = self._session.get(SLOEvaluationRow, evaluation_id)
        if row is None:
            return None
        return self._evaluation_from_row(row)

    def list_evaluations(
        self,
        *,
        slo_id: str,
        limit: int = 100,
    ) -> tuple[SLOEvaluationV1, ...]:
        rows = self._session.scalars(
            select(SLOEvaluationRow)
            .where(SLOEvaluationRow.slo_id == slo_id)
            .order_by(
                SLOEvaluationRow.window_start,
                SLOEvaluationRow.window_end,
                SLOEvaluationRow.evaluation_id,
            )
            .limit(_validate_limit(limit))
        ).all()
        return tuple(self._evaluation_from_row(row) for row in rows)

    def get(self, alert_instance_id: str) -> AlertInstanceV1 | None:
        row = self._session.get(AlertInstanceRow, alert_instance_id)
        if row is None:
            return None
        parsed = _parse_payload(
            row.payload,
            AlertInstanceV1,
            label="alert instance",
            identity=alert_instance_id,
        )
        _require_projection(
            row,
            parsed,
            (
                "alert_instance_id",
                "alert_rule_id",
                "dedup_key",
                "state",
                "last_evaluation_id",
                "revision",
            ),
            label="alert instance",
            identity=alert_instance_id,
        )
        return parsed

    def compare_and_swap(
        self,
        instance: AlertInstanceV1,
        *,
        expected_revision: int | None,
    ) -> AlertInstanceV1:
        canonical = _canonical_model(instance, AlertInstanceV1, label="alert instance")
        rule = self.get_rule(canonical.alert_rule_id)
        if rule is None:
            raise IntegrityViolation(
                "alert instance references an unavailable alert rule",
                alert_rule_id=canonical.alert_rule_id,
            )
        evaluation = self.get_evaluation(canonical.last_evaluation_id)
        if evaluation is None:
            raise IntegrityViolation(
                "alert instance references an unavailable SLO evaluation",
                evaluation_id=canonical.last_evaluation_id,
            )
        if evaluation.slo_id != rule.slo_id:
            raise IntegrityViolation(
                "alert instance evaluation does not belong to its rule SLO",
                evaluation_id=canonical.last_evaluation_id,
                alert_rule_id=canonical.alert_rule_id,
            )

        current = self.get(canonical.alert_instance_id)
        if current is None:
            if expected_revision is not None:
                self._raise_revision_conflict(canonical, expected_revision, None)
            if canonical.revision != 1:
                raise IntegrityViolation("new alert instance must start at revision 1")
            collision = self._session.scalar(
                select(AlertInstanceRow.alert_instance_id).where(
                    AlertInstanceRow.alert_rule_id == canonical.alert_rule_id,
                    AlertInstanceRow.dedup_key == canonical.dedup_key,
                )
            )
            if collision is not None:
                raise IntegrityViolation(
                    "alert rule and dedup key already identify another instance",
                    alert_rule_id=canonical.alert_rule_id,
                    dedup_key=canonical.dedup_key,
                )
            wire = canonical.model_dump(mode="json")
            self._session.add(
                AlertInstanceRow(
                    alert_instance_id=canonical.alert_instance_id,
                    alert_rule_id=canonical.alert_rule_id,
                    dedup_key=canonical.dedup_key,
                    state=canonical.state,
                    last_evaluation_id=canonical.last_evaluation_id,
                    revision=canonical.revision,
                    payload=wire,
                )
            )
            self._flush("alert instance", alert_instance_id=canonical.alert_instance_id)
            return canonical

        if expected_revision != current.revision:
            self._raise_revision_conflict(canonical, expected_revision, current.revision)
        if canonical.revision != current.revision + 1:
            raise IntegrityViolation("alert CAS must advance revision exactly once")
        if (
            canonical.alert_rule_id != current.alert_rule_id
            or canonical.dedup_key != current.dedup_key
        ):
            raise IntegrityViolation("alert immutable identity fields changed")
        wire = canonical.model_dump(mode="json")
        result = self._session.execute(
            update(AlertInstanceRow)
            .where(
                AlertInstanceRow.alert_instance_id == canonical.alert_instance_id,
                AlertInstanceRow.revision == expected_revision,
            )
            .values(
                state=canonical.state,
                last_evaluation_id=canonical.last_evaluation_id,
                revision=canonical.revision,
                payload=wire,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get(canonical.alert_instance_id)
            self._raise_revision_conflict(
                canonical,
                expected_revision,
                None if actual is None else actual.revision,
            )
        self._session.expire_all()
        return canonical

    def _evaluation_from_row(self, row: SLOEvaluationRow) -> SLOEvaluationV1:
        parsed = _parse_payload(
            row.payload,
            SLOEvaluationV1,
            label="SLO evaluation",
            identity=row.evaluation_id,
        )
        _require_projection(
            row,
            parsed,
            ("evaluation_id", "slo_id", "window_start", "window_end", "status"),
            label="SLO evaluation",
            identity=row.evaluation_id,
        )
        return parsed

    @staticmethod
    def _raise_revision_conflict(
        instance: AlertInstanceV1,
        expected_revision: int | None,
        actual_revision: int | None,
    ) -> None:
        raise Conflict(
            "alert revision does not match",
            alert_instance_id=instance.alert_instance_id,
            expected_revision=expected_revision,
            actual_revision=actual_revision,
        )

    def _flush(self, label: str, **context: Any) -> None:
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise IntegrityViolation(f"{label} could not be persisted", **context) from exc


__all__ = [
    "MAX_EVALUATION_QUERY_ITEMS",
    "MAX_SLO_DEFINITION_QUERY_ITEMS",
    "SqlSloRepository",
]
