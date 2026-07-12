"""Strict, hash-bound contracts for human edit distance evidence."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.bench.hed.delta import (
    AtomicDelta,
    DeltaKind,
    symmetric_difference_distance,
)
from gameforge.bench.stats import percentile, percentile_bootstrap_ci
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.findings import Finding, Patch
from gameforge.contracts.model_router import ModelSnapshot

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RequestHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

HedOutcomeStatus = Literal["evaluated", "agent_unusable", "protocol_failure"]
HedDisposition = Literal["unchanged", "edited", "unusable", "protocol_failure"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _json_value(value: Any, *, exclude: set[str] | None = None) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude=exclude or set())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        excluded = exclude or set()
        return {
            key: _json_value(item)
            for key, item in value.items()
            if key not in excluded
        }
    return value


def content_sha256(value: Any, *, exclude: set[str] | None = None) -> str:
    payload = _json_value(value, exclude=exclude)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_float(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    if not value.startswith("f:"):
        raise ValueError("metric float string must use the canonical f: prefix")
    raw = value.removeprefix("f:")
    try:
        decimal = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("metric contains an invalid canonical float") from exc
    canonical = format(decimal.normalize(), "f")
    if raw != canonical or not decimal.is_finite():
        raise ValueError("metric float string is not canonical and finite")
    return float(decimal)


def _delta_key(delta: AtomicDeltaModel) -> tuple[str, str, str, str, str]:
    return (
        delta.kind,
        delta.target,
        delta.field or "",
        delta.old_json or "",
        delta.new_json or "",
    )


class AtomicDeltaModel(_StrictModel):
    """JSON contract for the immutable semantic delta dataclass."""

    kind: DeltaKind
    target: NonEmptyStr
    field: NonEmptyStr | None = None
    old_json: str | None = None
    new_json: str | None = None

    @field_validator("old_json", "new_json")
    @classmethod
    def validate_canonical_json(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("delta value must be valid canonical JSON") from exc
        if canonical_json(parsed) != value:
            raise ValueError("delta value must use canonical JSON")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> AtomicDeltaModel:
        if self.kind in {"add_entity", "add_relation"}:
            if self.field is not None or self.old_json is not None or self.new_json is None:
                raise ValueError("add delta requires only new_json")
        elif self.kind in {"delete_entity", "delete_relation"}:
            if self.field is not None or self.old_json is None or self.new_json is not None:
                raise ValueError("delete delta requires only old_json")
        elif self.kind == "set_entity_attr":
            if self.field is None or self.old_json == self.new_json:
                raise ValueError("set delta requires a field and a changed value")
        return self

    @classmethod
    def from_delta(cls, delta: AtomicDelta) -> AtomicDeltaModel:
        return cls(
            kind=delta.kind,
            target=delta.target,
            field=delta.field,
            old_json=delta.old_json,
            new_json=delta.new_json,
        )

    def to_delta(self) -> AtomicDelta:
        return AtomicDelta(
            kind=self.kind,
            target=self.target,
            field=self.field,
            old_json=self.old_json,
            new_json=self.new_json,
        )


def _normalize_deltas(values: Any) -> tuple[AtomicDeltaModel, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError("semantic deltas must be an ordered sequence")
    normalized: list[AtomicDeltaModel] = []
    for value in values:
        if isinstance(value, AtomicDeltaModel):
            normalized.append(value)
        elif isinstance(value, AtomicDelta):
            normalized.append(AtomicDeltaModel.from_delta(value))
        else:
            normalized.append(AtomicDeltaModel.model_validate(value))
    return tuple(normalized)


def _validate_delta_order(
    values: tuple[AtomicDeltaModel, ...],
    field_name: str,
) -> None:
    keys = tuple(_delta_key(item) for item in values)
    if len(keys) != len(set(keys)):
        raise ValueError(f"{field_name} must contain unique deltas")
    if keys != tuple(sorted(keys)):
        raise ValueError(f"{field_name} must be sorted")


class HedCaseOutcome(_StrictModel):
    case_id: StableId
    external_case_evidence_sha256: Sha256
    protocol_sha256: Sha256
    status: HedOutcomeStatus
    disposition: HedDisposition
    before_snapshot_id: NonEmptyStr
    human_target_snapshot_id: NonEmptyStr
    target_finding: Finding
    request_hashes: tuple[RequestHash, ...]
    search_steps: int = Field(ge=0, le=4)
    patch: Patch | None = None
    patch_sha256: Sha256 | None = None
    passed_verification: bool
    agent_target_snapshot_id: NonEmptyStr | None = None
    human_delta: tuple[AtomicDeltaModel, ...]
    agent_delta: tuple[AtomicDeltaModel, ...]
    raw_distance: int | None = Field(default=None, ge=0)
    normalized_distance: float | None = Field(default=None, ge=0.0, le=1.0)
    failure_reason: NonEmptyStr | None = None
    outcome_sha256: Sha256

    @field_validator("human_delta", "agent_delta", mode="before")
    @classmethod
    def normalize_delta_values(cls, value: Any) -> tuple[AtomicDeltaModel, ...]:
        return _normalize_deltas(value)

    @field_validator("normalized_distance", mode="before")
    @classmethod
    def parse_canonical_distance(cls, value: Any) -> Any:
        return _canonical_float(value)

    @model_validator(mode="after")
    def validate_outcome(self) -> HedCaseOutcome:
        _validate_delta_order(self.human_delta, "human_delta")
        _validate_delta_order(self.agent_delta, "agent_delta")
        if not self.human_delta:
            raise ValueError("HED outcome requires a nonempty upstream human delta")
        if self.search_steps != len(self.request_hashes):
            raise ValueError("search_steps must equal the recorded request count")
        if self.target_finding.snapshot_id != self.before_snapshot_id:
            raise ValueError("target Finding must belong to the before snapshot")
        if (
            self.target_finding.oracle_type != "deterministic"
            or self.target_finding.status != "confirmed"
        ):
            raise ValueError("HED target Finding must be confirmed and deterministic")

        if self.patch is None:
            if self.patch_sha256 is not None:
                raise ValueError("patch_sha256 requires a retained Patch")
        else:
            if self.patch_sha256 != content_sha256(self.patch):
                raise ValueError("patch_sha256 does not bind the retained Patch")
            if self.patch.base_snapshot_id != self.before_snapshot_id:
                raise ValueError("retained Patch must target the exact before snapshot")
            if self.patch.produced_by != "agent":
                raise ValueError("HED Agent Patch must be produced_by=agent")
            if self.target_finding.id not in self.patch.expected_to_fix:
                raise ValueError("retained Patch must bind the target Finding")

        if self.status == "protocol_failure":
            if self.disposition != "protocol_failure":
                raise ValueError("protocol_failure requires its matching disposition")
            if self.passed_verification:
                raise ValueError("protocol_failure cannot pass verification")
            if self.agent_target_snapshot_id is not None or self.agent_delta:
                raise ValueError("protocol_failure cannot carry an Agent target")
            if self.raw_distance is not None or self.normalized_distance is not None:
                raise ValueError("protocol_failure cannot carry a fake distance")
            if self.failure_reason is None:
                raise ValueError("protocol_failure requires a failure_reason")
        else:
            expected_raw, expected_normalized = symmetric_difference_distance(
                tuple(item.to_delta() for item in self.agent_delta),
                tuple(item.to_delta() for item in self.human_delta),
            )
            if self.raw_distance != expected_raw or self.normalized_distance is None:
                raise ValueError("stored HED distance does not rederive from deltas")
            if abs(self.normalized_distance - expected_normalized) > 1e-12:
                raise ValueError("stored normalized HED does not rederive from deltas")

            if self.status == "evaluated":
                expected_disposition = "unchanged" if expected_raw == 0 else "edited"
                if self.disposition != expected_disposition:
                    raise ValueError("evaluated disposition does not match HED distance")
                if not self.passed_verification:
                    raise ValueError("evaluated Agent target must have passed verification")
                if self.patch is None or self.agent_target_snapshot_id is None:
                    raise ValueError("evaluated outcome requires Patch and Agent target")
                if not self.agent_delta:
                    raise ValueError("evaluated Agent target requires a semantic delta")
                if self.failure_reason is not None:
                    raise ValueError("evaluated outcome cannot carry a failure_reason")
            else:
                if self.disposition != "unusable":
                    raise ValueError("agent_unusable requires disposition=unusable")
                if self.passed_verification:
                    raise ValueError("agent_unusable cannot pass verification")
                if self.patch is None:
                    raise ValueError("agent_unusable must retain the final Patch")
                if self.agent_target_snapshot_id is not None or self.agent_delta:
                    raise ValueError("agent_unusable must score an empty Agent delta")
                if self.failure_reason is None:
                    raise ValueError("agent_unusable requires a failure_reason")

        expected_hash = content_sha256(self, exclude={"outcome_sha256"})
        if self.outcome_sha256 != expected_hash:
            raise ValueError("outcome_sha256 does not bind HED case outcome")
        return self


class HedMetric(_StrictModel):
    planned_n: Literal[8] = 8
    evaluated_n: int = Field(ge=0, le=8)
    mean_normalized_distance: float | None = Field(default=None, ge=0.0, le=1.0)
    median_normalized_distance: float | None = Field(default=None, ge=0.0, le=1.0)
    primary_estimate: float | None = Field(default=None, ge=0.0, le=1.0)
    ci_low: float | None = Field(default=None, ge=0.0, le=1.0)
    ci_high: float | None = Field(default=None, ge=0.0, le=1.0)
    ci_method: Literal["percentile-bootstrap95"] | None = None
    mean_raw_distance: float | None = Field(default=None, ge=0.0)
    median_raw_distance: float | None = Field(default=None, ge=0.0)
    unchanged_count: int = Field(ge=0, le=8)
    edited_count: int = Field(ge=0, le=8)
    unusable_count: int = Field(ge=0, le=8)
    protocol_failure_count: int = Field(ge=0, le=8)

    @field_validator(
        "mean_normalized_distance",
        "median_normalized_distance",
        "primary_estimate",
        "ci_low",
        "ci_high",
        "mean_raw_distance",
        "median_raw_distance",
        mode="before",
    )
    @classmethod
    def parse_canonical_floats(cls, value: Any) -> Any:
        return _canonical_float(value)

    @model_validator(mode="after")
    def validate_counts_and_statistics(self) -> HedMetric:
        count_total = (
            self.unchanged_count
            + self.edited_count
            + self.unusable_count
            + self.protocol_failure_count
        )
        if count_total != self.planned_n:
            raise ValueError("HED metric counts must cover planned_n")
        if self.evaluated_n != self.planned_n - self.protocol_failure_count:
            raise ValueError("evaluated_n must include every measured non-protocol outcome")
        values = (
            self.mean_normalized_distance,
            self.median_normalized_distance,
            self.primary_estimate,
            self.ci_low,
            self.ci_high,
            self.mean_raw_distance,
            self.median_raw_distance,
        )
        if self.evaluated_n == 0:
            if any(value is not None for value in values) or self.ci_method is not None:
                raise ValueError("empty HED metric cannot carry distribution estimates")
        else:
            if any(value is None for value in values) or self.ci_method is None:
                raise ValueError("measured HED metric requires complete statistics")
            if self.primary_estimate != self.mean_normalized_distance:
                raise ValueError("HED primary_estimate must be mean normalized distance")
            if self.ci_low is not None and self.ci_high is not None:
                if not self.ci_low <= self.primary_estimate <= self.ci_high:  # type: ignore[operator]
                    raise ValueError("HED confidence interval must contain the estimate")
        return self


def derive_hed_metric(outcomes: Sequence[HedCaseOutcome]) -> HedMetric:
    rows = tuple(outcomes)
    if len(rows) != 8:
        raise ValueError("HED evidence requires exactly eight outcomes")
    dispositions = [item.disposition for item in rows]
    measured = [item for item in rows if item.status != "protocol_failure"]
    normalized = [
        item.normalized_distance
        for item in measured
        if item.normalized_distance is not None
    ]
    raw = [float(item.raw_distance) for item in measured if item.raw_distance is not None]
    if len(normalized) != len(measured) or len(raw) != len(measured):
        raise ValueError("every non-protocol HED outcome must carry a distance")

    if normalized:
        interval = percentile_bootstrap_ci(normalized, statistics.fmean)
        mean_normalized = statistics.fmean(normalized)
        median_normalized = percentile(normalized, 0.5)
        mean_raw = statistics.fmean(raw)
        median_raw = percentile(raw, 0.5)
        if not all(
            math.isfinite(value)
            for value in (
                mean_normalized,
                median_normalized,
                mean_raw,
                median_raw,
                interval.low,
                interval.high,
            )
        ):
            raise ValueError("derived HED statistics must be finite")
        return HedMetric(
            evaluated_n=len(measured),
            mean_normalized_distance=mean_normalized,
            median_normalized_distance=median_normalized,
            primary_estimate=mean_normalized,
            ci_low=interval.low,
            ci_high=interval.high,
            ci_method=interval.method,
            mean_raw_distance=mean_raw,
            median_raw_distance=median_raw,
            unchanged_count=dispositions.count("unchanged"),
            edited_count=dispositions.count("edited"),
            unusable_count=dispositions.count("unusable"),
            protocol_failure_count=dispositions.count("protocol_failure"),
        )

    return HedMetric(
        evaluated_n=0,
        unchanged_count=0,
        edited_count=0,
        unusable_count=0,
        protocol_failure_count=8,
    )


class HedEvidenceManifest(_StrictModel):
    schema_version: Literal["hed-evidence@1"] = "hed-evidence@1"
    protocol_sha256: Sha256
    external_manifest_sha256: Sha256
    model_snapshot: ModelSnapshot
    outcomes: tuple[HedCaseOutcome, ...]
    metric: HedMetric
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def validate_manifest(self) -> HedEvidenceManifest:
        if len(self.outcomes) != 8:
            raise ValueError("HED evidence requires exactly eight outcomes")
        case_ids = tuple(item.case_id for item in self.outcomes)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("HED outcomes must have unique case IDs")
        if case_ids != tuple(sorted(case_ids)):
            raise ValueError("HED outcomes must be sorted by case ID")
        if any(item.protocol_sha256 != self.protocol_sha256 for item in self.outcomes):
            raise ValueError("outcome protocol_sha256 differs from evidence protocol")
        expected_snapshot = ModelSnapshot(
            provider="openai",
            model="gpt-5.6-sol",
            snapshot_tag="pre-m4@1",
        )
        if self.model_snapshot != expected_snapshot:
            raise ValueError("HED evidence requires openai/gpt-5.6-sol/pre-m4@1")
        if self.metric != derive_hed_metric(self.outcomes):
            raise ValueError("HED metric does not rederive from outcomes")
        expected_hash = content_sha256(self, exclude={"evidence_sha256"})
        if self.evidence_sha256 != expected_hash:
            raise ValueError("evidence_sha256 does not bind HED evidence")
        return self


def seal_outcome(**values: Any) -> HedCaseOutcome:
    payload = dict(values)
    payload.pop("outcome_sha256", None)
    payload.pop("disposition", None)
    payload.pop("patch_sha256", None)
    payload.pop("raw_distance", None)
    payload.pop("normalized_distance", None)
    human_delta = _normalize_deltas(payload.get("human_delta", ()))
    agent_delta = _normalize_deltas(payload.get("agent_delta", ()))
    payload["human_delta"] = human_delta
    payload["agent_delta"] = agent_delta
    patch = payload.get("patch")
    payload["patch_sha256"] = content_sha256(patch) if patch is not None else None

    status = payload["status"]
    if status == "protocol_failure":
        payload["disposition"] = "protocol_failure"
        payload["raw_distance"] = None
        payload["normalized_distance"] = None
    else:
        raw, normalized = symmetric_difference_distance(
            tuple(item.to_delta() for item in agent_delta),
            tuple(item.to_delta() for item in human_delta),
        )
        payload["raw_distance"] = raw
        payload["normalized_distance"] = normalized
        if status == "agent_unusable":
            payload["disposition"] = "unusable"
        else:
            payload["disposition"] = "unchanged" if raw == 0 else "edited"
    payload.setdefault("failure_reason", None)
    payload["outcome_sha256"] = content_sha256(payload)
    return HedCaseOutcome.model_validate(payload)


def seal_evidence_manifest(**values: Any) -> HedEvidenceManifest:
    payload = dict(values)
    payload.pop("evidence_sha256", None)
    payload.setdefault("schema_version", "hed-evidence@1")
    outcomes = tuple(payload["outcomes"])
    payload["outcomes"] = outcomes
    payload["metric"] = derive_hed_metric(outcomes)
    payload["evidence_sha256"] = content_sha256(payload)
    return HedEvidenceManifest.model_validate(payload)


def canonical_evidence_bytes(manifest: HedEvidenceManifest) -> bytes:
    return (canonical_json(manifest.model_dump(mode="json")) + "\n").encode("utf-8")


def load_evidence(path: str | Path) -> HedEvidenceManifest:
    raw = Path(path).read_bytes()
    manifest = HedEvidenceManifest.model_validate_json(raw)
    if canonical_evidence_bytes(manifest) != raw:
        raise ValueError("HED evidence is not canonical JSON")
    return manifest


def write_evidence(path: str | Path, manifest: HedEvidenceManifest) -> None:
    Path(path).write_bytes(canonical_evidence_bytes(manifest))


def validate_evidence_manifest(
    manifest: HedEvidenceManifest,
    *,
    protocol: Any,
    external_manifest: Any,
) -> None:
    """Rebind evidence to the exact frozen protocol and external denominator."""

    if manifest.protocol_sha256 != protocol.protocol_sha256:
        raise ValueError("HED evidence protocol_sha256 mismatch")
    if manifest.external_manifest_sha256 != external_manifest.manifest_sha256:
        raise ValueError("HED evidence external_manifest_sha256 mismatch")
    if manifest.model_snapshot != protocol.model_snapshot:
        raise ValueError("HED evidence model snapshot differs from protocol")
    expected_ids = tuple(sorted(item.spec.case_id for item in external_manifest.cases))
    actual_ids = tuple(item.case_id for item in manifest.outcomes)
    if actual_ids != expected_ids or actual_ids != protocol.external_case_ids:
        raise ValueError("HED evidence denominator differs from frozen cases")
    external_by_id = {
        item.spec.case_id: item.evidence_sha256 for item in external_manifest.cases
    }
    for outcome in manifest.outcomes:
        if outcome.external_case_evidence_sha256 != external_by_id[outcome.case_id]:
            raise ValueError(f"external case evidence hash mismatch for {outcome.case_id}")
    if manifest.metric != derive_hed_metric(manifest.outcomes):
        raise ValueError("HED metric does not rederive from outcomes")
    if manifest.evidence_sha256 != content_sha256(
        manifest,
        exclude={"evidence_sha256"},
    ):
        raise ValueError("evidence_sha256 does not bind HED evidence")


__all__ = [
    "AtomicDeltaModel",
    "HedCaseOutcome",
    "HedDisposition",
    "HedEvidenceManifest",
    "HedMetric",
    "HedOutcomeStatus",
    "canonical_evidence_bytes",
    "content_sha256",
    "derive_hed_metric",
    "load_evidence",
    "seal_evidence_manifest",
    "seal_outcome",
    "validate_evidence_manifest",
    "write_evidence",
]
