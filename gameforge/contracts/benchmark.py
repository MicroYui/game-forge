"""Typed, content-addressable benchmark execution specification contracts.

``benchmark-spec@1`` is the immutable authority that binds a Bench Run to one
exact dataset and describes case execution mode per partition.  Admission derives
whether model execution is required from the selected cases; callers never get to
replace that authority with a boolean such as ``contains_agent_cases``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.execution_profiles import ProfileRefV1


MAX_BENCHMARK_PARTITIONS = 1_024
MAX_BENCHMARK_CASES_PER_PARTITION = 4_096
MAX_BENCHMARK_METRICS = 256
MAX_BENCHMARK_ORDER_KEYS = 32
MAX_BENCHMARK_POLICY_VERSION = 2_147_483_647
MAX_BENCHMARK_REPETITIONS = 100_000

BoundedId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
JsonPointer = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4_096, pattern=r"^/(?:[^~]|~[01])*$"),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(ge=1, le=MAX_BENCHMARK_POLICY_VERSION)]
RepetitionCount = Annotated[int, Field(ge=1, le=MAX_BENCHMARK_REPETITIONS)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class BenchmarkDatasetBindingV1(_FrozenModel):
    """Exact immutable dataset selected by this specification."""

    binding_schema_version: Literal["benchmark-dataset-binding@1"] = "benchmark-dataset-binding@1"
    artifact_id: BoundedId
    payload_hash: Sha256Hex
    payload_schema_id: Literal["bench-dataset@1"] = "bench-dataset@1"


class BenchmarkCaseExecutionV1(_FrozenModel):
    """One dataset case's execution class within a partition."""

    case_id: BoundedId
    execution_mode: Literal["deterministic", "agent"]


class BenchmarkPartitionV1(_FrozenModel):
    partition_id: BoundedId
    cases: tuple[BenchmarkCaseExecutionV1, ...] = Field(
        min_length=1,
        max_length=MAX_BENCHMARK_CASES_PER_PARTITION,
    )

    @field_validator("cases")
    @classmethod
    def _canonical_cases(
        cls, value: tuple[BenchmarkCaseExecutionV1, ...]
    ) -> tuple[BenchmarkCaseExecutionV1, ...]:
        identities = [item.case_id for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("benchmark partition case ids must be unique")
        return tuple(sorted(value, key=lambda item: item.case_id))


class BenchmarkMetricRefV1(_FrozenModel):
    metric_id: BoundedId
    metric_version: PositiveInt


class BenchmarkMetricPolicyV1(_FrozenModel):
    policy_id: BoundedId
    policy_version: PositiveInt
    metrics: tuple[BenchmarkMetricRefV1, ...] = Field(
        min_length=1,
        max_length=MAX_BENCHMARK_METRICS,
    )

    @field_validator("metrics")
    @classmethod
    def _canonical_metrics(
        cls, value: tuple[BenchmarkMetricRefV1, ...]
    ) -> tuple[BenchmarkMetricRefV1, ...]:
        identities = [(item.metric_id, item.metric_version) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("benchmark metric refs must be unique")
        return tuple(sorted(value, key=lambda item: (item.metric_id, item.metric_version)))


class BenchmarkSamplingPolicyV1(_FrozenModel):
    policy_id: BoundedId
    policy_version: PositiveInt
    strategy: Literal["all", "deterministic_prefix", "seeded_without_replacement"]
    sample_size_per_partition: PositiveInt | None = None
    minimum_repetitions: RepetitionCount
    maximum_repetitions: RepetitionCount
    seed_derivation_version: BoundedId

    @model_validator(mode="after")
    def _closed_shape(self) -> "BenchmarkSamplingPolicyV1":
        if self.minimum_repetitions > self.maximum_repetitions:
            raise ValueError("benchmark sampling repetition range is inverted")
        if (self.strategy == "all") != (self.sample_size_per_partition is None):
            raise ValueError("all-case sampling forbids sample_size; bounded sampling requires it")
        return self


class BenchmarkOrderKeyV1(_FrozenModel):
    field_path: JsonPointer
    direction: Literal["ascending", "descending"]
    nulls: Literal["first", "last", "forbidden"]


class BenchmarkOrderingPolicyV1(_FrozenModel):
    policy_id: BoundedId
    policy_version: PositiveInt
    keys: tuple[BenchmarkOrderKeyV1, ...] = Field(
        min_length=1,
        max_length=MAX_BENCHMARK_ORDER_KEYS,
    )

    @field_validator("keys")
    @classmethod
    def _unique_keys(
        cls, value: tuple[BenchmarkOrderKeyV1, ...]
    ) -> tuple[BenchmarkOrderKeyV1, ...]:
        paths = [item.field_path for item in value]
        if len(paths) != len(set(paths)):
            raise ValueError("benchmark ordering field paths must be unique")
        # Key order is semantic and is therefore intentionally preserved.
        return value


class BenchmarkEvaluatorPolicyRefV1(_FrozenModel):
    policy_id: BoundedId
    policy_version: PositiveInt
    policy_digest: Sha256Hex


class BenchmarkSpecV1(_FrozenModel):
    """Complete immutable ``benchmark-spec@1`` admission authority."""

    benchmark_spec_schema_version: Literal["benchmark-spec@1"] = "benchmark-spec@1"
    dataset: BenchmarkDatasetBindingV1
    evaluator_profile: ProfileRefV1
    evaluator_policy: BenchmarkEvaluatorPolicyRefV1
    metric_policy: BenchmarkMetricPolicyV1
    sampling_policy: BenchmarkSamplingPolicyV1
    ordering_policy: BenchmarkOrderingPolicyV1
    partitions: tuple[BenchmarkPartitionV1, ...] = Field(
        min_length=1,
        max_length=MAX_BENCHMARK_PARTITIONS,
    )

    @field_validator("partitions")
    @classmethod
    def _canonical_partitions(
        cls, value: tuple[BenchmarkPartitionV1, ...]
    ) -> tuple[BenchmarkPartitionV1, ...]:
        identities = [item.partition_id for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("benchmark partition ids must be unique")
        return tuple(sorted(value, key=lambda item: item.partition_id))

    @model_validator(mode="after")
    def _global_case_identity(self) -> "BenchmarkSpecV1":
        case_ids = [case.case_id for partition in self.partitions for case in partition.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("benchmark case ids must be globally unique")
        sample_size = self.sampling_policy.sample_size_per_partition
        if sample_size is not None and any(
            sample_size > len(partition.cases) for partition in self.partitions
        ):
            raise ValueError("benchmark sample size exceeds a partition case count")
        return self

    def selected_partitions(
        self, partition_ids: tuple[str, ...]
    ) -> tuple[BenchmarkPartitionV1, ...]:
        """Resolve a canonical selection; an empty request explicitly means all."""

        if not partition_ids:
            return self.partitions
        by_id = {item.partition_id: item for item in self.partitions}
        missing = tuple(partition_id for partition_id in partition_ids if partition_id not in by_id)
        if missing:
            raise KeyError(missing)
        return tuple(by_id[partition_id] for partition_id in partition_ids)


__all__ = [
    "BenchmarkCaseExecutionV1",
    "BenchmarkDatasetBindingV1",
    "BenchmarkEvaluatorPolicyRefV1",
    "BenchmarkMetricPolicyV1",
    "BenchmarkMetricRefV1",
    "BenchmarkOrderKeyV1",
    "BenchmarkOrderingPolicyV1",
    "BenchmarkPartitionV1",
    "BenchmarkSamplingPolicyV1",
    "BenchmarkSpecV1",
]
