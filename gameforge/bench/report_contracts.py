"""Strict contracts for the authoritative GameForge BenchReport v2 model."""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.bench.taxonomy import CLASS_META, DefectClass
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.spine.stats import wilson_ci

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
MetricStatus = Literal[
    "pending",
    "measured",
    "underpowered",
    "inconclusive",
    "failed",
]
MeasuredStatus = Literal["measured", "underpowered", "inconclusive"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


def _complete_or_empty(values: tuple[float | str | None, ...]) -> tuple[bool, bool]:
    present = tuple(value is not None for value in values)
    return all(present), not any(present)


class BinaryMetric(StrictModel):
    name: StableId
    defect_class: DefectClass | None = None
    bucket: StableId
    planned_n: int = Field(ge=0)
    evaluated_n: int = Field(ge=0)
    k: int = Field(ge=0)
    rate: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    ci_method: NonEmptyStr | None = None
    status: MetricStatus
    protocol_id: NonEmptyStr | None = None
    evidence_ref: StableId | None = None

    @field_validator("rate", "ci_low", "ci_high", mode="before")
    @classmethod
    def parse_canonical_floats(cls, value: Any) -> Any:
        return _canonical_float(value)

    @classmethod
    def pending(
        cls,
        *,
        name: str,
        bucket: str,
        planned_n: int,
        defect_class: DefectClass | None = None,
        protocol_id: str | None = None,
        evidence_ref: str | None = None,
    ) -> BinaryMetric:
        return cls(
            name=name,
            defect_class=defect_class,
            bucket=bucket,
            planned_n=planned_n,
            evaluated_n=0,
            k=0,
            status="pending",
            protocol_id=protocol_id,
            evidence_ref=evidence_ref,
        )

    @classmethod
    def wilson(
        cls,
        *,
        name: str,
        bucket: str,
        planned_n: int,
        evaluated_n: int,
        k: int,
        status: MeasuredStatus,
        defect_class: DefectClass | None = None,
        protocol_id: str | None = None,
        evidence_ref: str | None = None,
    ) -> BinaryMetric:
        low, high = wilson_ci(k, evaluated_n)
        return cls(
            name=name,
            defect_class=defect_class,
            bucket=bucket,
            planned_n=planned_n,
            evaluated_n=evaluated_n,
            k=k,
            rate=k / evaluated_n if evaluated_n else 0.0,
            ci_low=low,
            ci_high=high,
            ci_method="wilson95",
            status=status,
            protocol_id=protocol_id,
            evidence_ref=evidence_ref,
        )

    @model_validator(mode="after")
    def validate_metric(self) -> BinaryMetric:
        if self.evaluated_n > self.planned_n:
            raise ValueError("evaluated_n cannot exceed planned_n")
        if self.k > self.evaluated_n:
            raise ValueError("binary metric k cannot exceed evaluated_n")
        values = (self.rate, self.ci_low, self.ci_high, self.ci_method)
        complete, empty = _complete_or_empty(values)
        if self.status in {"pending", "failed"}:
            if self.status == "pending" and (self.evaluated_n != 0 or self.k != 0):
                raise ValueError("pending binary metric must be unevaluated")
            if not empty:
                raise ValueError("pending/failed binary metric cannot carry estimates")
            return self
        if self.evaluated_n == 0 or not complete:
            raise ValueError("measured binary metric requires a denominator and estimates")
        assert self.rate is not None
        assert self.ci_low is not None
        assert self.ci_high is not None
        if not all(math.isfinite(value) for value in (self.rate, self.ci_low, self.ci_high)):
            raise ValueError("binary metric estimates must be finite")
        if not 0.0 <= self.rate <= 1.0:
            raise ValueError("binary metric rate must be within [0, 1]")
        if not 0.0 <= self.ci_low <= self.ci_high <= 1.0:
            raise ValueError("binary metric interval must be ordered within [0, 1]")
        expected_rate = self.k / self.evaluated_n
        if abs(self.rate - expected_rate) > 1e-12:
            raise ValueError("binary metric rate does not equal k/evaluated_n")
        if self.ci_method == "wilson95":
            expected_low, expected_high = wilson_ci(self.k, self.evaluated_n)
            if (
                abs(self.ci_low - expected_low) > 1e-12
                or abs(self.ci_high - expected_high) > 1e-12
            ):
                raise ValueError("binary metric does not use the Wilson95 interval")
        return self


class DistributionMetric(StrictModel):
    name: StableId
    unit: StableId
    bucket: StableId
    planned_n: int = Field(ge=0)
    evaluated_n: int = Field(ge=0)
    mean: float | None = None
    median: float | None = None
    p95: float | None = None
    primary_estimate: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    ci_method: NonEmptyStr | None = None
    status: MetricStatus
    protocol_id: NonEmptyStr | None = None
    evidence_ref: StableId | None = None

    @field_validator(
        "mean",
        "median",
        "p95",
        "primary_estimate",
        "ci_low",
        "ci_high",
        mode="before",
    )
    @classmethod
    def parse_canonical_floats(cls, value: Any) -> Any:
        return _canonical_float(value)

    @classmethod
    def pending(
        cls,
        *,
        name: str,
        unit: str,
        bucket: str,
        planned_n: int,
        protocol_id: str | None = None,
        evidence_ref: str | None = None,
    ) -> DistributionMetric:
        return cls(
            name=name,
            unit=unit,
            bucket=bucket,
            planned_n=planned_n,
            evaluated_n=0,
            status="pending",
            protocol_id=protocol_id,
            evidence_ref=evidence_ref,
        )

    @classmethod
    def measured(
        cls,
        *,
        name: str,
        unit: str,
        bucket: str,
        planned_n: int,
        evaluated_n: int,
        mean: float,
        median: float,
        p95: float,
        primary_estimate: float,
        ci_low: float,
        ci_high: float,
        ci_method: str,
        status: MeasuredStatus,
        protocol_id: str | None = None,
        evidence_ref: str | None = None,
    ) -> DistributionMetric:
        return cls(
            name=name,
            unit=unit,
            bucket=bucket,
            planned_n=planned_n,
            evaluated_n=evaluated_n,
            mean=mean,
            median=median,
            p95=p95,
            primary_estimate=primary_estimate,
            ci_low=ci_low,
            ci_high=ci_high,
            ci_method=ci_method,
            status=status,
            protocol_id=protocol_id,
            evidence_ref=evidence_ref,
        )

    @model_validator(mode="after")
    def validate_metric(self) -> DistributionMetric:
        if self.evaluated_n > self.planned_n:
            raise ValueError("evaluated_n cannot exceed planned_n")
        values = (
            self.mean,
            self.median,
            self.p95,
            self.primary_estimate,
            self.ci_low,
            self.ci_high,
            self.ci_method,
        )
        complete, empty = _complete_or_empty(values)
        if self.status in {"pending", "failed"}:
            if self.status == "pending" and self.evaluated_n != 0:
                raise ValueError("pending distribution metric must be unevaluated")
            if not empty:
                raise ValueError("pending/failed distribution cannot carry estimates")
            return self
        if self.evaluated_n == 0 or not complete:
            raise ValueError("measured distribution requires a denominator and estimates")
        numeric = (
            self.mean,
            self.median,
            self.p95,
            self.primary_estimate,
            self.ci_low,
            self.ci_high,
        )
        if any(value is None or not math.isfinite(value) for value in numeric):
            raise ValueError("distribution estimates must be finite")
        assert self.median is not None
        assert self.p95 is not None
        assert self.ci_low is not None
        assert self.ci_high is not None
        if self.p95 < self.median:
            raise ValueError("distribution p95 cannot be below its median")
        if self.ci_low > self.ci_high:
            raise ValueError("distribution interval must be ordered")
        return self


class PowerMetric(StrictModel):
    defect_class: DefectClass
    bucket: StableId
    evaluated_n: int = Field(gt=0)
    achieved_half_width: float = Field(ge=0.0, le=1.0)
    target_half_width: float = Field(gt=0.0, le=1.0)
    status: Literal["measured", "underpowered"]
    evidence_ref: StableId | None = None

    @field_validator("achieved_half_width", "target_half_width", mode="before")
    @classmethod
    def parse_canonical_floats(cls, value: Any) -> Any:
        return _canonical_float(value)

    @model_validator(mode="after")
    def validate_status(self) -> PowerMetric:
        expected = (
            "measured"
            if self.achieved_half_width <= self.target_half_width
            else "underpowered"
        )
        if self.status != expected:
            raise ValueError("power status does not match achieved half-width")
        return self


def _normalized_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("evidence path must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts or str(path) != value:
        raise ValueError("evidence path must be a normalized relative POSIX path")
    return value


class EvidenceArtifactRef(StrictModel):
    evidence_id: StableId
    path: str
    sha256: Sha256 | None = None
    schema_version: NonEmptyStr
    available: bool

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _normalized_path(value)

    @model_validator(mode="after")
    def validate_availability(self) -> EvidenceArtifactRef:
        if self.available != (self.sha256 is not None):
            raise ValueError("available evidence requires a hash and unavailable evidence forbids one")
        return self


class VersionRef(StrictModel):
    component: StableId
    version: NonEmptyStr
    sha256: Sha256 | None = None


class TokenTotals(StrictModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    reported_total_tokens: int = Field(ge=0)


class ExternalSection(StrictModel):
    source_id: StableId
    repository: NonEmptyStr
    manifest_sha256: Sha256
    reader_version: NonEmptyStr
    adapter_version: NonEmptyStr
    mapping_spec_sha256: Sha256
    total_cases: int = Field(gt=0)
    qualified_cases: int = Field(ge=0)
    development: tuple[BinaryMetric, ...]
    verification: tuple[BinaryMetric, ...]
    after_oracle_fp: BinaryMetric
    evidence_ref: StableId

    @model_validator(mode="after")
    def validate_counts(self) -> ExternalSection:
        if self.qualified_cases > self.total_cases:
            raise ValueError("external qualified cases cannot exceed total cases")
        return self


class NarrativeSection(StrictModel):
    model_snapshot: ModelSnapshot
    protocol_sha256: Sha256
    corpus_manifest_sha256: Sha256
    bdr: tuple[BinaryMetric, ...]
    clean_fp: BinaryMetric
    evidence_ref: StableId


class HedSection(StrictModel):
    model_snapshot: ModelSnapshot
    normalized_distance: DistributionMetric
    raw_distance: DistributionMetric
    dispositions: tuple[BinaryMetric, ...]
    evidence_ref: StableId


class QaSection(StrictModel):
    scope: Literal["single-participant-eight-session-case-study"]
    protocol_sha256: Sha256
    paired_saved_minutes: DistributionMetric
    paired_saved_fraction: DistributionMetric
    manual_success: BinaryMetric
    assisted_success: BinaryMetric
    conclusion: Literal["pending", "savings", "inconclusive", "negative", "failed"]
    evidence_ref: StableId | None = None

    @model_validator(mode="after")
    def validate_pending_state(self) -> QaSection:
        metrics = (
            self.paired_saved_minutes,
            self.paired_saved_fraction,
            self.manual_success,
            self.assisted_success,
        )
        all_pending = all(metric.status == "pending" for metric in metrics)
        if (self.conclusion == "pending") != all_pending:
            raise ValueError("QA pending conclusion and metric states must agree")
        if self.conclusion == "pending" and self.evidence_ref is not None:
            raise ValueError("pending QA cannot claim a measured evidence ref")
        return self


class AgentCostWorkload(StrictModel):
    workload_id: StableId
    model_snapshot: ModelSnapshot
    planned_n: int = Field(gt=0)
    evaluated_n: int = Field(gt=0)
    tokens: TokenTotals
    tokens_per_sample: DistributionMetric
    request_latency_ms: DistributionMetric
    logical_requests: int = Field(ge=0)
    recorded_requests: int = Field(ge=0)
    session_cache_reuses: int = Field(ge=0)
    known_transport_attempts: int = Field(ge=0)
    known_transport_retries: int = Field(ge=0)
    unknown_transport_attempt_records: int = Field(ge=0)
    monetary_status: Literal["unavailable"] = "unavailable"
    price_book_ref: None = None
    evidence_ref: StableId

    @model_validator(mode="after")
    def validate_counts(self) -> AgentCostWorkload:
        if self.evaluated_n > self.planned_n:
            raise ValueError("Agent cost evaluated_n cannot exceed planned_n")
        if self.recorded_requests > self.logical_requests:
            raise ValueError("recorded requests cannot exceed logical requests")
        if self.session_cache_reuses != self.logical_requests - self.recorded_requests:
            raise ValueError("session cache reuses must equal logical minus recorded requests")
        if self.unknown_transport_attempt_records > self.recorded_requests:
            raise ValueError("unknown attempt records cannot exceed recorded requests")
        known_records = self.recorded_requests - self.unknown_transport_attempt_records
        if self.known_transport_attempts < known_records:
            raise ValueError("known transport attempts cannot be below known records")
        if self.known_transport_retries > self.known_transport_attempts:
            raise ValueError("known retries cannot exceed known attempts")
        if self.tokens_per_sample.evaluated_n != self.evaluated_n:
            raise ValueError("token distribution denominator differs from workload samples")
        if self.request_latency_ms.evaluated_n != self.recorded_requests:
            raise ValueError("latency distribution denominator differs from recorded requests")
        return self


class AgentCostSection(StrictModel):
    workloads: tuple[AgentCostWorkload, ...]
    evidence_ref: StableId


class DeterministicRuntimeSection(StrictModel):
    workload_id: StableId
    setup_ms: float = Field(ge=0.0)
    per_sample_ms: DistributionMetric
    environment_sha256: Sha256
    evidence_ref: StableId

    @field_validator("setup_ms", mode="before")
    @classmethod
    def parse_setup_ms(cls, value: Any) -> Any:
        return _canonical_float(value)


class CostLatencySection(StrictModel):
    agent: AgentCostSection
    deterministic: DeterministicRuntimeSection


class BenchMeta(StrictModel):
    seed: int
    corpus_size: int = Field(ge=0)
    report_builder_version: StableId
    generated_at: str | None = None


def _metric_identity(metric: BinaryMetric) -> tuple[str, str, str]:
    return (metric.name, metric.defect_class.value if metric.defect_class else "", metric.bucket)


class BenchReport(StrictModel):
    schema_version: Literal["bench-report@2"] = "bench-report@2"
    seeded: tuple[BinaryMetric, ...]
    false_positives: tuple[BinaryMetric, ...]
    agent: tuple[BinaryMetric, ...]
    power: tuple[PowerMetric, ...]
    external: ExternalSection
    narrative: NarrativeSection
    hed: HedSection
    qa: QaSection
    cost_latency: CostLatencySection
    versions: tuple[VersionRef, ...]
    evidence: tuple[EvidenceArtifactRef, ...]
    meta: BenchMeta

    def to_json(self) -> str:
        return canonical_report_bytes(self).decode("utf-8")

    @model_validator(mode="after")
    def validate_report(self) -> BenchReport:
        all_class_metrics = (*self.seeded, *self.narrative.bdr)
        classes = tuple(metric.defect_class for metric in all_class_metrics)
        if len(classes) != len(DefectClass) or set(classes) != set(DefectClass):
            raise ValueError("BenchReport must contain all 15 defect classes exactly once")
        for metric in all_class_metrics:
            if metric.defect_class is None:
                raise ValueError("per-class BDR metric requires a defect class")
            if metric.bucket != CLASS_META[metric.defect_class].bucket.value:
                raise ValueError("per-class BDR metric bucket differs from taxonomy")
        power_classes = tuple(metric.defect_class for metric in self.power)
        if len(power_classes) != len(DefectClass) or set(power_classes) != set(DefectClass):
            raise ValueError("BenchReport power rows must cover all 15 defect classes")
        for rows in (
            self.seeded,
            self.false_positives,
            self.agent,
            self.external.development,
            self.external.verification,
            self.narrative.bdr,
            self.hed.dispositions,
        ):
            identities = tuple(_metric_identity(metric) for metric in rows)
            if len(identities) != len(set(identities)):
                raise ValueError("BenchReport contains duplicate metric identities")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("BenchReport evidence refs must be unique")
        version_components = tuple(item.component for item in self.versions)
        if len(version_components) != len(set(version_components)):
            raise ValueError("BenchReport version components must be unique")
        known_evidence = set(evidence_ids)
        referenced = _evidence_references(self)
        unknown = sorted(referenced - known_evidence)
        if unknown:
            raise ValueError(f"BenchReport contains unknown evidence ref: {unknown[0]}")
        return self


def _evidence_references(report: BenchReport) -> set[str]:
    refs: set[str] = set()

    def add(value: str | None) -> None:
        if value is not None:
            refs.add(value)

    for metric in (
        *report.seeded,
        *report.false_positives,
        *report.agent,
        *report.external.development,
        *report.external.verification,
        report.external.after_oracle_fp,
        *report.narrative.bdr,
        report.narrative.clean_fp,
        report.hed.normalized_distance,
        report.hed.raw_distance,
        *report.hed.dispositions,
        report.qa.paired_saved_minutes,
        report.qa.paired_saved_fraction,
        report.qa.manual_success,
        report.qa.assisted_success,
        report.cost_latency.deterministic.per_sample_ms,
    ):
        add(metric.evidence_ref)
    for power in report.power:
        add(power.evidence_ref)
    add(report.external.evidence_ref)
    add(report.narrative.evidence_ref)
    add(report.hed.evidence_ref)
    add(report.qa.evidence_ref)
    add(report.cost_latency.agent.evidence_ref)
    add(report.cost_latency.deterministic.evidence_ref)
    for workload in report.cost_latency.agent.workloads:
        add(workload.evidence_ref)
        add(workload.tokens_per_sample.evidence_ref)
        add(workload.request_latency_ms.evidence_ref)
    return refs


def canonical_report_bytes(report: BenchReport) -> bytes:
    return (canonical_json(report.model_dump(mode="json")) + "\n").encode("utf-8")


def write_bench_report(path: str | Path, report: BenchReport) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_report_bytes(report))


def load_bench_report(path: str | Path) -> BenchReport:
    raw = Path(path).read_bytes()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("BenchReport must be valid bench-report@2 JSON") from exc
    if payload.get("schema_version") != "bench-report@2":
        raise ValueError("unsupported BenchReport schema; expected bench-report@2")
    report = BenchReport.model_validate(payload)
    if canonical_report_bytes(report) != raw:
        raise ValueError("bench-report@2 JSON is not canonical")
    return report


__all__ = [
    "AgentCostSection",
    "AgentCostWorkload",
    "BenchMeta",
    "BenchReport",
    "BinaryMetric",
    "CostLatencySection",
    "DeterministicRuntimeSection",
    "DistributionMetric",
    "EvidenceArtifactRef",
    "ExternalSection",
    "HedSection",
    "MetricStatus",
    "NarrativeSection",
    "PowerMetric",
    "QaSection",
    "StrictModel",
    "TokenTotals",
    "VersionRef",
    "canonical_report_bytes",
    "load_bench_report",
    "write_bench_report",
]
