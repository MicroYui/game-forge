"""Hash-bound Agent token and record-time latency evidence from cassettes."""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Sequence

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    model_validator,
)

from gameforge.bench.report_contracts import (
    DistributionMetric,
    Sha256,
    StableId,
    StrictModel,
    TokenTotals,
)
from gameforge.bench.stats import percentile, percentile_bootstrap_ci
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cassette import CassetteRecord
from gameforge.contracts.model_router import ModelSnapshot

RequestHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_INPUT_KEYS = ("input_tokens", "prompt_tokens", "input")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "output")
_CACHE_READ_KEYS = ("cache_read_tokens", "cache_read")
_CACHE_WRITE_KEYS = ("cache_write_tokens", "cache_write")


def _json_value(value: Any, *, exclude: set[str] | None = None) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude=exclude or set())
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


def _content_sha256(value: Any, *, exclude: set[str] | None = None) -> str:
    payload = _json_value(value, exclude=exclude)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _normalized_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("cassette root must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts or str(path) != value:
        raise ValueError("cassette root must be a normalized relative POSIX path")
    return value


def _usage_value(
    usage: dict[str, int],
    keys: tuple[str, ...],
    *,
    required: bool,
) -> int:
    matches = [key for key in keys if key in usage]
    if len(matches) > 1:
        raise ValueError(f"ambiguous token usage aliases: {matches}")
    if not matches:
        if required:
            raise ValueError(f"token usage lacks one of {keys}")
        return 0
    value = usage[matches[0]]
    if type(value) is not int or value < 0:
        raise ValueError(f"token usage {matches[0]} must be a nonnegative integer")
    return value


def _nested_usage_value(raw_usage: Any, key: str) -> int:
    if not isinstance(raw_usage, dict) or key not in raw_usage:
        return 0
    value = raw_usage[key]
    if type(value) is not int or value < 0:
        raise ValueError(f"raw token usage {key} must be a nonnegative integer")
    return value


def normalize_tokens(record: CassetteRecord) -> TokenTotals:
    """Normalize provider aliases without treating cache subsets as extra total."""

    usage = record.response.token_usage
    if not usage:
        raise ValueError("cassette response has no token usage")
    input_tokens = _usage_value(usage, _INPUT_KEYS, required=True)
    output_tokens = _usage_value(usage, _OUTPUT_KEYS, required=True)
    cache_read = _usage_value(usage, _CACHE_READ_KEYS, required=False)
    cache_write = _usage_value(usage, _CACHE_WRITE_KEYS, required=False)

    raw_usage = record.response.raw_response.get("usage", {})
    details = raw_usage.get("input_tokens_details", {}) if isinstance(raw_usage, dict) else {}
    nested_read = _nested_usage_value(details, "cached_tokens")
    nested_write = _nested_usage_value(details, "cache_write_tokens")
    if cache_read and nested_read and cache_read != nested_read:
        raise ValueError("normalized and raw cache-read token counts disagree")
    if cache_write and nested_write and cache_write != nested_write:
        raise ValueError("normalized and raw cache-write token counts disagree")
    cache_read = cache_read or nested_read
    cache_write = cache_write or nested_write

    if "total_tokens" in usage:
        total = usage["total_tokens"]
        if type(total) is not int or total < 0:
            raise ValueError("total_tokens must be a nonnegative integer")
        if total != input_tokens + output_tokens:
            raise ValueError("provider total_tokens differs from input plus output")
    elif record.model_snapshot.provider == "anthropic":
        total = input_tokens + output_tokens + cache_read + cache_write
    else:
        total = input_tokens + output_tokens

    return TokenTotals(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reported_total_tokens=total,
    )


def _sum_tokens(values: Sequence[TokenTotals]) -> TokenTotals:
    return TokenTotals(
        input_tokens=sum(item.input_tokens for item in values),
        output_tokens=sum(item.output_tokens for item in values),
        cache_read_tokens=sum(item.cache_read_tokens for item in values),
        cache_write_tokens=sum(item.cache_write_tokens for item in values),
        reported_total_tokens=sum(item.reported_total_tokens for item in values),
    )


class SampleTrace(StrictModel):
    sample_id: StableId
    request_hashes: tuple[RequestHash, ...]


class AgentRequestSample(StrictModel):
    sample_id: StableId
    logical_request_hashes: tuple[RequestHash, ...]
    recorded_request_hashes: tuple[RequestHash, ...]
    cassette_sha256s: tuple[Sha256, ...]
    recorded_request_latencies_ms: tuple[int, ...]
    logical_requests: int = Field(ge=0)
    recorded_requests: int = Field(ge=0)
    session_cache_reuses: int = Field(ge=0)
    tokens: TokenTotals
    recorded_latency_ms: int = Field(ge=0)
    known_transport_attempts: int = Field(ge=0)
    known_transport_retries: int = Field(ge=0)
    unknown_transport_attempt_records: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_sample(self) -> AgentRequestSample:
        expected_recorded = tuple(dict.fromkeys(self.logical_request_hashes))
        if self.recorded_request_hashes != expected_recorded:
            raise ValueError("recorded request hashes must deduplicate logical order")
        if self.logical_requests != len(self.logical_request_hashes):
            raise ValueError("logical request count differs from request hashes")
        if self.recorded_requests != len(self.recorded_request_hashes):
            raise ValueError("recorded request count differs from request hashes")
        if self.session_cache_reuses != self.logical_requests - self.recorded_requests:
            raise ValueError("sample cache reuses must equal logical minus recorded requests")
        if len(self.cassette_sha256s) != self.recorded_requests:
            raise ValueError("cassette hashes must align with recorded requests")
        if len(self.recorded_request_latencies_ms) != self.recorded_requests:
            raise ValueError("latencies must align with recorded requests")
        if any(value <= 0 for value in self.recorded_request_latencies_ms):
            raise ValueError("record-time latency must be positive")
        if self.recorded_latency_ms != sum(self.recorded_request_latencies_ms):
            raise ValueError("sample latency must equal its recorded request latencies")
        if self.unknown_transport_attempt_records > self.recorded_requests:
            raise ValueError("unknown attempt records cannot exceed recorded requests")
        known_records = self.recorded_requests - self.unknown_transport_attempt_records
        if self.known_transport_attempts < known_records:
            raise ValueError("known attempts cannot be below known cassette count")
        if self.known_transport_retries > self.known_transport_attempts:
            raise ValueError("known retries cannot exceed known attempts")
        return self


class AgentWorkloadEvidence(StrictModel):
    workload_id: StableId
    model_snapshot: ModelSnapshot
    cassette_root: str
    protocol_id: NonEmptyStr
    source_evidence_sha256: Sha256
    planned_n: int = Field(gt=0)
    evaluated_n: int = Field(gt=0)
    samples: tuple[AgentRequestSample, ...]
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
    workload_sha256: Sha256

    @model_validator(mode="after")
    def validate_workload(self) -> AgentWorkloadEvidence:
        _normalized_path(self.cassette_root)
        sample_ids = tuple(item.sample_id for item in self.samples)
        if sample_ids != tuple(sorted(set(sample_ids))):
            raise ValueError("Agent cost samples must be unique and sorted")
        if self.evaluated_n != len(self.samples) or self.evaluated_n > self.planned_n:
            raise ValueError("Agent workload sample denominator is inconsistent")
        if self.tokens != _sum_tokens([item.tokens for item in self.samples]):
            raise ValueError("Agent workload token totals do not rederive")
        logical = sum(item.logical_requests for item in self.samples)
        recorded = sum(item.recorded_requests for item in self.samples)
        cache_reuses = sum(item.session_cache_reuses for item in self.samples)
        attempts = sum(item.known_transport_attempts for item in self.samples)
        retries = sum(item.known_transport_retries for item in self.samples)
        unknown = sum(item.unknown_transport_attempt_records for item in self.samples)
        if (
            self.logical_requests,
            self.recorded_requests,
            self.session_cache_reuses,
            self.known_transport_attempts,
            self.known_transport_retries,
            self.unknown_transport_attempt_records,
        ) != (logical, recorded, cache_reuses, attempts, retries, unknown):
            raise ValueError("Agent workload request counts do not rederive")
        expected_tokens = _distribution_metric(
            name="tokens_per_sample",
            unit="tokens",
            bucket="agent_cost",
            values=[float(item.tokens.reported_total_tokens) for item in self.samples],
            planned_n=self.planned_n,
        )
        expected_latency = _distribution_metric(
            name="request_latency_ms",
            unit="milliseconds",
            bucket="agent_latency",
            values=[
                float(value)
                for item in self.samples
                for value in item.recorded_request_latencies_ms
            ],
            planned_n=self.recorded_requests,
        )
        if self.tokens_per_sample != expected_tokens:
            raise ValueError("tokens-per-sample metric does not rederive")
        if self.request_latency_ms != expected_latency:
            raise ValueError("request-latency metric does not rederive")
        expected_hash = _content_sha256(self, exclude={"workload_sha256"})
        if self.workload_sha256 != expected_hash:
            raise ValueError("workload_sha256 does not bind Agent cost evidence")
        return self


class AgentCostLatencyEvidence(StrictModel):
    schema_version: Literal["agent-cost-latency-evidence@1"] = (
        "agent-cost-latency-evidence@1"
    )
    workloads: tuple[AgentWorkloadEvidence, ...]
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def validate_evidence(self) -> AgentCostLatencyEvidence:
        workload_ids = tuple(item.workload_id for item in self.workloads)
        if not workload_ids or workload_ids != tuple(sorted(set(workload_ids))):
            raise ValueError("Agent cost workloads must be nonempty, unique, and sorted")
        expected = _content_sha256(self, exclude={"evidence_sha256"})
        if self.evidence_sha256 != expected:
            raise ValueError("evidence_sha256 does not bind Agent cost evidence")
        return self


def _cassette_path(root: Path, request_hash: str) -> Path:
    return root / f"{request_hash.removeprefix('sha256:')}.json"


def _load_cassette(root: Path, request_hash: str) -> tuple[CassetteRecord, str]:
    path = _cassette_path(root, request_hash)
    if not path.is_file():
        raise ValueError(f"missing cassette for {request_hash}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("cassette root must be an object")
        response = payload.get("response")
        if isinstance(response, dict):
            usage = response.get("token_usage", {})
            if not isinstance(usage, dict) or any(
                type(value) is not int for value in usage.values()
            ):
                raise ValueError("token usage values must be integers")
            latency = response.get("latency_ms", 0)
            if type(latency) is not int:
                raise ValueError("latency_ms must be an integer")
        for field in ("transport_attempts", "transport_retries"):
            value = payload.get(field)
            if value is not None and type(value) is not int:
                raise ValueError(f"{field} must be an integer or null")
        record = CassetteRecord.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - malformed evidence fails closed
        raise ValueError(f"invalid cassette for {request_hash}: {exc}") from exc
    if record.request_hash != request_hash:
        raise ValueError(f"cassette request hash mismatch for {request_hash}")
    return record, hashlib.sha256(raw).hexdigest()


def aggregate_sample(
    trace: SampleTrace,
    cassette_root: str | Path,
    *,
    expected_model_snapshot: ModelSnapshot,
) -> AgentRequestSample:
    root = Path(cassette_root)
    recorded_hashes = tuple(dict.fromkeys(trace.request_hashes))
    records: list[CassetteRecord] = []
    cassette_sha256s: list[str] = []
    for request_hash in recorded_hashes:
        record, raw_sha256 = _load_cassette(root, request_hash)
        if record.model_snapshot != expected_model_snapshot:
            raise ValueError(f"cassette model snapshot mismatch for {request_hash}")
        if record.response.latency_ms <= 0:
            raise ValueError(f"cassette lacks positive record-time latency for {request_hash}")
        records.append(record)
        cassette_sha256s.append(raw_sha256)
    tokens = _sum_tokens([normalize_tokens(record) for record in records])
    latencies = tuple(record.response.latency_ms for record in records)
    known = [record for record in records if record.transport_attempts is not None]
    return AgentRequestSample(
        sample_id=trace.sample_id,
        logical_request_hashes=trace.request_hashes,
        recorded_request_hashes=recorded_hashes,
        cassette_sha256s=tuple(cassette_sha256s),
        recorded_request_latencies_ms=latencies,
        logical_requests=len(trace.request_hashes),
        recorded_requests=len(recorded_hashes),
        session_cache_reuses=len(trace.request_hashes) - len(recorded_hashes),
        tokens=tokens,
        recorded_latency_ms=sum(latencies),
        known_transport_attempts=sum(record.transport_attempts or 0 for record in known),
        known_transport_retries=sum(record.transport_retries or 0 for record in known),
        unknown_transport_attempt_records=len(records) - len(known),
    )


def _distribution_metric(
    *,
    name: str,
    unit: str,
    bucket: str,
    values: Sequence[float],
    planned_n: int,
) -> DistributionMetric:
    if not values:
        raise ValueError(f"{name} requires at least one measured value")
    sample = tuple(float(value) for value in values)
    interval = percentile_bootstrap_ci(sample, statistics.fmean)
    mean = statistics.fmean(sample)
    return DistributionMetric.measured(
        name=name,
        unit=unit,
        bucket=bucket,
        planned_n=planned_n,
        evaluated_n=len(sample),
        mean=mean,
        median=percentile(sample, 0.5),
        p95=percentile(sample, 0.95),
        primary_estimate=mean,
        ci_low=interval.low,
        ci_high=interval.high,
        ci_method=interval.method,
        status="measured",
    )


def aggregate_workload(
    *,
    workload_id: str,
    model_snapshot: ModelSnapshot,
    cassette_root: str | Path,
    cassette_root_ref: str,
    protocol_id: str,
    source_evidence_sha256: str,
    planned_n: int,
    traces: Sequence[SampleTrace],
) -> AgentWorkloadEvidence:
    normalized_root = _normalized_path(cassette_root_ref)
    ordered_traces = tuple(sorted(traces, key=lambda item: item.sample_id))
    if len(ordered_traces) != planned_n:
        raise ValueError("Agent workload traces must fill the planned denominator")
    if len({item.sample_id for item in ordered_traces}) != len(ordered_traces):
        raise ValueError("Agent workload traces contain duplicate sample IDs")
    samples = tuple(
        aggregate_sample(
            trace,
            cassette_root,
            expected_model_snapshot=model_snapshot,
        )
        for trace in ordered_traces
    )
    recorded_latencies = [
        float(value)
        for item in samples
        for value in item.recorded_request_latencies_ms
    ]
    payload: dict[str, Any] = {
        "workload_id": workload_id,
        "model_snapshot": model_snapshot,
        "cassette_root": normalized_root,
        "protocol_id": protocol_id,
        "source_evidence_sha256": source_evidence_sha256,
        "planned_n": planned_n,
        "evaluated_n": len(samples),
        "samples": samples,
        "tokens": _sum_tokens([item.tokens for item in samples]),
        "tokens_per_sample": _distribution_metric(
            name="tokens_per_sample",
            unit="tokens",
            bucket="agent_cost",
            values=[float(item.tokens.reported_total_tokens) for item in samples],
            planned_n=planned_n,
        ),
        "request_latency_ms": _distribution_metric(
            name="request_latency_ms",
            unit="milliseconds",
            bucket="agent_latency",
            values=recorded_latencies,
            planned_n=len(recorded_latencies),
        ),
        "logical_requests": sum(item.logical_requests for item in samples),
        "recorded_requests": sum(item.recorded_requests for item in samples),
        "session_cache_reuses": sum(item.session_cache_reuses for item in samples),
        "known_transport_attempts": sum(
            item.known_transport_attempts for item in samples
        ),
        "known_transport_retries": sum(item.known_transport_retries for item in samples),
        "unknown_transport_attempt_records": sum(
            item.unknown_transport_attempt_records for item in samples
        ),
        "monetary_status": "unavailable",
        "price_book_ref": None,
    }
    payload["workload_sha256"] = _content_sha256(payload)
    return AgentWorkloadEvidence.model_validate(payload)


def seal_agent_cost_evidence(
    workloads: Sequence[AgentWorkloadEvidence],
) -> AgentCostLatencyEvidence:
    ordered = tuple(sorted(workloads, key=lambda item: item.workload_id))
    payload = {
        "schema_version": "agent-cost-latency-evidence@1",
        "workloads": ordered,
    }
    payload["evidence_sha256"] = _content_sha256(payload)
    return AgentCostLatencyEvidence.model_validate(payload)


def canonical_evidence_bytes(evidence: AgentCostLatencyEvidence) -> bytes:
    return (canonical_json(evidence.model_dump(mode="json")) + "\n").encode("utf-8")


def write_evidence(path: str | Path, evidence: AgentCostLatencyEvidence) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_evidence_bytes(evidence))


def load_evidence(path: str | Path) -> AgentCostLatencyEvidence:
    raw = Path(path).read_bytes()
    evidence = AgentCostLatencyEvidence.model_validate_json(raw)
    if canonical_evidence_bytes(evidence) != raw:
        raise ValueError("Agent cost evidence is not canonical JSON")
    return evidence


def validate_agent_cost_evidence(
    evidence: AgentCostLatencyEvidence,
    *,
    repo_root: str | Path,
    cassette_roots: dict[str, str | Path] | None = None,
) -> None:
    roots = cassette_roots or {}
    base = Path(repo_root)
    rebuilt: list[AgentWorkloadEvidence] = []
    for workload in evidence.workloads:
        root = Path(roots.get(workload.cassette_root, base / workload.cassette_root))
        for sample in workload.samples:
            for request_hash, expected_sha in zip(
                sample.recorded_request_hashes,
                sample.cassette_sha256s,
                strict=True,
            ):
                _, actual_sha = _load_cassette(root, request_hash)
                if actual_sha != expected_sha:
                    raise ValueError(f"cassette bytes changed for {request_hash}")
        rebuilt.append(
            aggregate_workload(
                workload_id=workload.workload_id,
                model_snapshot=workload.model_snapshot,
                cassette_root=root,
                cassette_root_ref=workload.cassette_root,
                protocol_id=workload.protocol_id,
                source_evidence_sha256=workload.source_evidence_sha256,
                planned_n=workload.planned_n,
                traces=tuple(
                    SampleTrace(
                        sample_id=sample.sample_id,
                        request_hashes=sample.logical_request_hashes,
                    )
                    for sample in workload.samples
                ),
            )
        )
    if seal_agent_cost_evidence(rebuilt) != evidence:
        raise ValueError("Agent cost evidence does not rederive from cassette records")


__all__ = [
    "AgentCostLatencyEvidence",
    "AgentRequestSample",
    "AgentWorkloadEvidence",
    "SampleTrace",
    "aggregate_sample",
    "aggregate_workload",
    "canonical_evidence_bytes",
    "load_evidence",
    "normalize_tokens",
    "seal_agent_cost_evidence",
    "validate_agent_cost_evidence",
    "write_evidence",
]
