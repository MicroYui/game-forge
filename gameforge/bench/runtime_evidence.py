"""Hash-bound wall-clock evidence for the deterministic seeded pipeline."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import statistics
import time
from collections import Counter
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Sequence

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    model_validator,
)

from gameforge.bench.corpus import Corpus, build_corpus
from gameforge.bench.metrics import default_constraints, run_pipeline
from gameforge.bench.report_contracts import (
    DistributionMetric,
    Sha256,
    StableId,
    StrictModel,
    VersionRef,
)
from gameforge.bench.stats import percentile, percentile_bootstrap_ci
from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.versions import TOOL_VERSION
from gameforge.spine.dsl.compile import compile_all

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RuntimeBucket = Literal["deterministic", "simulation", "clean"]

_REQUIRED_PACKAGES = ("clingo", "pydantic", "z3-solver")


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


def constraints_sha256(constraints: Sequence[Constraint]) -> str:
    """Bind normalized constraint content, independent of paths and input order."""

    ordered = tuple(sorted(constraints, key=lambda item: item.id))
    ids = tuple(item.id for item in ordered)
    if not ordered or len(ids) != len(set(ids)):
        raise ValueError("runtime constraints must be nonempty with unique IDs")
    payload = [
        item.model_dump(mode="json", by_alias=True, exclude_none=False)
        for item in ordered
    ]
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class RuntimeEnvironment(StrictModel):
    system: NonEmptyStr
    release: NonEmptyStr
    machine: NonEmptyStr
    python_implementation: NonEmptyStr
    python_version: NonEmptyStr
    cpu_count: int | None = Field(default=None, ge=1)
    perf_counter_resolution_ns: int = Field(gt=0)
    tool_version: NonEmptyStr
    package_versions: tuple[VersionRef, ...]

    @model_validator(mode="after")
    def validate_versions(self) -> RuntimeEnvironment:
        components = tuple(item.component for item in self.package_versions)
        if components != tuple(sorted(set(components))):
            raise ValueError("runtime package versions must be unique and sorted")
        if not set(_REQUIRED_PACKAGES).issubset(components):
            raise ValueError("runtime environment lacks required solver versions")
        return self


class RuntimeSample(StrictModel):
    sample_id: StableId
    defect_class: DefectClass | None = None
    bucket: RuntimeBucket
    elapsed_ns: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_bucket(self) -> RuntimeSample:
        if self.bucket == "clean":
            if self.defect_class is not None:
                raise ValueError("clean runtime sample cannot name a defect class")
            return self
        if self.defect_class is None:
            raise ValueError("defect runtime sample requires a defect class")
        expected = CLASS_META[self.defect_class].bucket.value
        if expected == Bucket.llm_assisted.value or self.bucket != expected:
            raise ValueError("runtime sample bucket differs from deterministic taxonomy")
        return self


def _runtime_distribution(samples: Sequence[RuntimeSample]) -> DistributionMetric:
    values = tuple(item.elapsed_ns / 1_000_000 for item in samples)
    if not values:
        raise ValueError("runtime evidence requires at least one measured sample")
    interval = percentile_bootstrap_ci(values, statistics.fmean)
    mean = statistics.fmean(values)
    return DistributionMetric.measured(
        name="deterministic_pipeline_runtime_ms",
        unit="milliseconds",
        bucket="deterministic_runtime",
        planned_n=len(values),
        evaluated_n=len(values),
        mean=mean,
        median=percentile(values, 0.5),
        p95=percentile(values, 0.95),
        primary_estimate=mean,
        ci_low=interval.low,
        ci_high=interval.high,
        ci_method=interval.method,
        status="measured",
    )


class DeterministicRuntimeEvidence(StrictModel):
    schema_version: Literal["deterministic-runtime-evidence@1"] = (
        "deterministic-runtime-evidence@1"
    )
    workload_id: Literal["seeded-checker-sim-pipeline"] = (
        "seeded-checker-sim-pipeline"
    )
    seed: int
    per_class_n: dict[DefectClass, int]
    distinct_clean_n: int = Field(gt=0)
    constraints_sha256: Sha256
    setup_elapsed_ns: int = Field(gt=0)
    samples: tuple[RuntimeSample, ...]
    per_sample_ms: DistributionMetric
    environment: RuntimeEnvironment
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def validate_evidence(self) -> DeterministicRuntimeEvidence:
        if set(self.per_class_n) != set(DefectClass):
            raise ValueError("runtime per_class_n must cover the complete taxonomy")
        if any(value < 0 for value in self.per_class_n.values()):
            raise ValueError("runtime per-class counts cannot be negative")
        narrative = {
            defect
            for defect in DefectClass
            if CLASS_META[defect].bucket is Bucket.llm_assisted
        }
        if any(self.per_class_n[defect] != 0 for defect in narrative):
            raise ValueError("narrative classes cannot enter deterministic runtime evidence")

        sample_ids = tuple(item.sample_id for item in self.samples)
        if not sample_ids or len(sample_ids) != len(set(sample_ids)):
            raise ValueError("runtime sample IDs must be nonempty and unique")
        counts = Counter(
            item.defect_class for item in self.samples if item.defect_class is not None
        )
        if any(counts[defect] != self.per_class_n[defect] for defect in DefectClass):
            raise ValueError("runtime per-class counts do not rederive from samples")
        clean_count = sum(item.bucket == "clean" for item in self.samples)
        if clean_count != self.distinct_clean_n:
            raise ValueError("runtime clean denominator does not rederive")

        class_order = {defect: index for index, defect in enumerate(DefectClass)}

        def order_key(item: RuntimeSample) -> tuple[int, str]:
            if item.defect_class is None:
                return len(class_order), item.sample_id
            return class_order[item.defect_class], item.sample_id

        if self.samples != tuple(sorted(self.samples, key=order_key)):
            raise ValueError("runtime samples must use deterministic taxonomy order")
        if self.per_sample_ms != _runtime_distribution(self.samples):
            raise ValueError("runtime distribution does not rederive from samples")
        expected_hash = _content_sha256(self, exclude={"evidence_sha256"})
        if self.evidence_sha256 != expected_hash:
            raise ValueError("evidence_sha256 does not bind runtime evidence")
        return self


def capture_runtime_environment() -> RuntimeEnvironment:
    resolution_ns = max(
        1,
        round(time.get_clock_info("perf_counter").resolution * 1_000_000_000),
    )
    return RuntimeEnvironment(
        system=platform.system(),
        release=platform.release(),
        machine=platform.machine(),
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        cpu_count=os.cpu_count(),
        perf_counter_resolution_ns=resolution_ns,
        tool_version=TOOL_VERSION,
        package_versions=tuple(
            VersionRef(component=name, version=package_version(name))
            for name in sorted(_REQUIRED_PACKAGES)
        ),
    )


def _positive_elapsed(start: int, end: int, *, label: str) -> int:
    if type(start) is not int or type(end) is not int:
        raise ValueError(f"{label} clock values must be integers")
    elapsed = end - start
    if elapsed <= 0:
        raise ValueError(f"{label} elapsed time must be positive")
    return elapsed


def measure_runtime(
    corpus: Corpus,
    constraints: Sequence[Constraint],
    *,
    seed: int = 0,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    environment: RuntimeEnvironment | None = None,
) -> DeterministicRuntimeEvidence:
    """Compile once, then time each non-narrative sample without a warmup."""

    normalized_constraints = tuple(constraints)
    constraint_hash = constraints_sha256(normalized_constraints)
    setup_start = clock_ns()
    checkers = compile_all(list(normalized_constraints))
    setup_elapsed = _positive_elapsed(
        setup_start,
        clock_ns(),
        label="checker setup",
    )

    samples: list[RuntimeSample] = []
    per_class_n = dict.fromkeys(DefectClass, 0)
    indexes: Counter[DefectClass] = Counter()
    for injected in corpus.samples:
        defect = injected.ground_truth.defect_class
        bucket = CLASS_META[defect].bucket
        if bucket is Bucket.llm_assisted:
            continue
        index = indexes[defect]
        indexes[defect] += 1
        started = clock_ns()
        run_pipeline(
            injected.snapshot,
            checkers,
            needs_nav=injected.needs_nav,
        )
        elapsed = _positive_elapsed(started, clock_ns(), label="pipeline sample")
        samples.append(
            RuntimeSample(
                sample_id=f"{defect.value}-{index:04d}",
                defect_class=defect,
                bucket=bucket.value,
                elapsed_ns=elapsed,
            )
        )
        per_class_n[defect] += 1

    distinct_clean = {snapshot.snapshot_id: snapshot for snapshot in corpus.clean}
    for index, snapshot_id in enumerate(sorted(distinct_clean)):
        started = clock_ns()
        run_pipeline(distinct_clean[snapshot_id], checkers, needs_nav=False)
        elapsed = _positive_elapsed(started, clock_ns(), label="clean pipeline sample")
        samples.append(
            RuntimeSample(
                sample_id=f"clean-{index:04d}",
                defect_class=None,
                bucket="clean",
                elapsed_ns=elapsed,
            )
        )

    measured_samples = tuple(samples)
    payload: dict[str, Any] = {
        "schema_version": "deterministic-runtime-evidence@1",
        "workload_id": "seeded-checker-sim-pipeline",
        "seed": seed,
        "per_class_n": per_class_n,
        "distinct_clean_n": len(distinct_clean),
        "constraints_sha256": constraint_hash,
        "setup_elapsed_ns": setup_elapsed,
        "samples": measured_samples,
        "per_sample_ms": _runtime_distribution(measured_samples),
        "environment": environment or capture_runtime_environment(),
    }
    payload["evidence_sha256"] = _content_sha256(payload)
    return DeterministicRuntimeEvidence.model_validate(payload)


def measure_seeded_runtime(
    *,
    seed: int = 0,
    per_class_n: dict[DefectClass, int] | None = None,
    n_clean: int = 40,
    constraints_path: str = "scenarios/constraints",
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> DeterministicRuntimeEvidence:
    corpus = build_corpus(seed=seed, per_class_n=per_class_n, n_clean=n_clean)
    constraints = default_constraints(constraints_path)
    return measure_runtime(
        corpus,
        constraints,
        seed=seed,
        clock_ns=clock_ns,
    )


def canonical_runtime_evidence_bytes(
    evidence: DeterministicRuntimeEvidence,
) -> bytes:
    return (canonical_json(evidence.model_dump(mode="json")) + "\n").encode("utf-8")


def write_runtime_evidence(
    path: str | Path,
    evidence: DeterministicRuntimeEvidence,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_runtime_evidence_bytes(evidence))


def load_runtime_evidence(path: str | Path) -> DeterministicRuntimeEvidence:
    raw = Path(path).read_bytes()
    evidence = DeterministicRuntimeEvidence.model_validate_json(raw)
    if canonical_runtime_evidence_bytes(evidence) != raw:
        raise ValueError("runtime evidence is not canonical JSON")
    return evidence


def validate_runtime_evidence(
    evidence: DeterministicRuntimeEvidence,
    *,
    constraints: Sequence[Constraint],
) -> None:
    if evidence.constraints_sha256 != constraints_sha256(constraints):
        raise ValueError("runtime evidence constraints hash mismatch")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--constraints", default="scenarios/constraints")
    args = parser.parse_args(argv)
    evidence = measure_seeded_runtime(
        seed=args.seed,
        constraints_path=args.constraints,
    )
    write_runtime_evidence(args.output, evidence)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by measured CLI run
    raise SystemExit(main())


__all__ = [
    "DeterministicRuntimeEvidence",
    "RuntimeEnvironment",
    "RuntimeSample",
    "canonical_runtime_evidence_bytes",
    "capture_runtime_environment",
    "constraints_sha256",
    "load_runtime_evidence",
    "main",
    "measure_runtime",
    "measure_seeded_runtime",
    "validate_runtime_evidence",
    "write_runtime_evidence",
]
