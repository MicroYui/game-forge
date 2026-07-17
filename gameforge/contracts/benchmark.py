"""Typed, content-addressable benchmark execution specification contracts.

``benchmark-spec@1`` is the immutable authority that binds a Bench Run to one
exact dataset and describes case execution mode per partition.  Admission derives
whether model execution is required from the selected cases; callers never get to
replace that authority with a boolean such as ``contains_agent_cases``.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.execution_profiles import (
    MAX_CHECKER_WORK_UNITS_V1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.seeds import SUBSEED_DERIVATION_VERSION_V1, derive_subseed_v1


MAX_BENCHMARK_PARTITIONS = 1_024
MAX_BENCHMARK_CASES_PER_PARTITION = 4_096
MAX_BENCHMARK_METRICS = 256
MAX_BENCHMARK_ORDER_KEYS = 32
MAX_BENCHMARK_POLICY_VERSION = 2_147_483_647
MAX_BENCHMARK_REPETITIONS = 100_000
MAX_BENCHMARK_DATASET_CASES = 16_384
MAX_BENCHMARK_CASE_EXECUTIONS = 100_000
MAX_BENCHMARK_REPORT_BYTES = 8 * 1024 * 1024
MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT = 8 * 1024 * 1024
MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL = 64 * 1024 * 1024
MAX_BENCHMARK_CASE_INPUT_BYTES = 256 * 1024
MAX_BENCHMARK_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_BENCHMARK_AGENT_TURNS = 32
# A route can render one prompt for the primary plus three frozen fallbacks and
# retain one consumed response shard. Across a three-attempt safety envelope,
# 64 logical calls consume at most 64 * 5 * 3 = 960 call parents, leaving 64
# manifest slots for bundles, closed failures, dataset/spec, and the final report.
MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL = 64
MAX_BENCHMARK_CASE_METRICS = 64
MAX_BENCHMARK_RESULT_METRIC_FIELDS = 64
MAX_BENCHMARK_RESULT_METRICS_BYTES = 64 * 1024
MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL = 64 * 1024 * 1024
MAX_BENCHMARK_CHECKER_WORK_UNITS = MAX_CHECKER_WORK_UNITS_V1
MAX_BENCHMARK_SIMULATION_WORK_UNITS = 10_000_000

BENCHMARK_AGGREGATE_RESULT_SCHEMAS: dict[str, tuple[str, ...]] = {
    "checker_run": ("checker-report@1",),
    "simulation_run": ("simulation-result@1",),
    "playtest_trace": ("playtest-trace@1",),
    "review_report": ("review@1",),
    "run_result": ("run-result@1",),
    "validation_evidence": ("evidence-set@1",),
    "regression_evidence": ("regression-evidence@1",),
}

BoundedId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
BoundedPrompt = Annotated[str, StringConstraints(min_length=1, max_length=65_536)]
JsonPointer = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4_096, pattern=r"^/(?:[^~]|~[01])*$"),
]
RootJsonPointer = Annotated[
    str,
    StringConstraints(max_length=4_096, pattern=r"^(?:|/(?:[^~]|~[01])*)$"),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SnapshotId = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(ge=1, le=MAX_BENCHMARK_POLICY_VERSION)]
RepetitionCount = Annotated[int, Field(ge=1, le=MAX_BENCHMARK_REPETITIONS)]
Uint64 = Annotated[int, Field(strict=True, ge=0, le=(1 << 64) - 1)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _bounded_json(value: JsonValue, *, field_name: str) -> JsonValue:
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > MAX_BENCHMARK_CASE_INPUT_BYTES:
        raise ValueError(f"{field_name} exceeds the benchmark case byte limit")
    return value


class BenchmarkEqualsPredicateV1(_FrozenModel):
    predicate_schema_version: Literal["benchmark-json-predicate@1"] = "benchmark-json-predicate@1"
    operator: Literal["equals", "not_equals"]
    actual_pointer: RootJsonPointer = ""
    expected: JsonValue

    @field_validator("expected")
    @classmethod
    def _expected(cls, value: JsonValue) -> JsonValue:
        return _bounded_json(value, field_name="benchmark predicate expected value")


class BenchmarkContainsPredicateV1(_FrozenModel):
    predicate_schema_version: Literal["benchmark-json-predicate@1"] = "benchmark-json-predicate@1"
    operator: Literal["contains", "not_contains"]
    actual_pointer: RootJsonPointer = ""
    expected: JsonValue

    @field_validator("expected")
    @classmethod
    def _expected(cls, value: JsonValue) -> JsonValue:
        return _bounded_json(value, field_name="benchmark predicate expected member")


class BenchmarkNumericPredicateV1(_FrozenModel):
    predicate_schema_version: Literal["benchmark-json-predicate@1"] = "benchmark-json-predicate@1"
    operator: Literal["less_than", "less_or_equal", "greater_than", "greater_or_equal"]
    actual_pointer: RootJsonPointer = ""
    expected: float = Field(allow_inf_nan=False)


class BenchmarkExistencePredicateV1(_FrozenModel):
    predicate_schema_version: Literal["benchmark-json-predicate@1"] = "benchmark-json-predicate@1"
    operator: Literal["exists", "not_exists"]
    actual_pointer: RootJsonPointer


BenchmarkJsonPredicateV1: TypeAlias = Annotated[
    BenchmarkEqualsPredicateV1
    | BenchmarkContainsPredicateV1
    | BenchmarkNumericPredicateV1
    | BenchmarkExistencePredicateV1,
    Field(discriminator="operator"),
]


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


class BenchmarkBinaryMetricTargetV1(_FrozenModel):
    """Exact BenchReport binary-metric identity updated by one metric definition."""

    collection: Literal[
        "seeded",
        "false_positives",
        "agent",
        "external.development",
        "external.verification",
        "narrative.bdr",
        "hed.dispositions",
    ]
    name: BoundedId
    bucket: BoundedId
    defect_class: BoundedId | None = None


class BenchmarkBinaryMetricDefinitionV1(_FrozenModel):
    metric: BenchmarkMetricRefV1
    target: BenchmarkBinaryMetricTargetV1
    result_pointer: RootJsonPointer
    positive_value: JsonValue
    updates_power: bool = False

    @field_validator("positive_value")
    @classmethod
    def _positive_value(cls, value: JsonValue) -> JsonValue:
        if isinstance(value, (dict, list)):
            raise ValueError("binary benchmark metric positive value must be a JSON scalar")
        return _bounded_json(value, field_name="benchmark metric positive value")

    @model_validator(mode="after")
    def _power_shape(self) -> "BenchmarkBinaryMetricDefinitionV1":
        if self.result_pointer != "/status" and not self.result_pointer.startswith("/metrics/"):
            raise ValueError("benchmark metric result pointer must select status or metrics")
        if self.updates_power and self.target.defect_class is None:
            raise ValueError("power-driving benchmark metric requires a defect class")
        return self


BenchmarkDefectClassV1: TypeAlias = Literal[
    "dangling_reference",
    "missing_drop_source",
    "unreachable_target",
    "cyclic_dependency",
    "dead_quest",
    "unsatisfiable_completion",
    "reward_out_of_range",
    "prob_sum_ne_1",
    "non_monotonic_curve",
    "gacha_expectation_violation",
    "economy_collapse",
    "character_violation",
    "spoiler",
    "faction_violation",
    "uniqueness_violation",
]

BenchmarkDeterministicDefectClassV1: TypeAlias = Literal[
    "dangling_reference",
    "missing_drop_source",
    "unreachable_target",
    "cyclic_dependency",
    "dead_quest",
    "unsatisfiable_completion",
    "reward_out_of_range",
    "prob_sum_ne_1",
    "non_monotonic_curve",
    "gacha_expectation_violation",
    "economy_collapse",
]


class BenchmarkSimulationExecutionV1(_FrozenModel):
    simulation_schema_version: Literal["benchmark-simulation-execution@1"] = (
        "benchmark-simulation-execution@1"
    )
    seed_policy: Literal["fixed", "run_subseed"]
    fixed_seed: int | None = Field(default=None, ge=0, le=(1 << 64) - 1)
    agents: int = Field(ge=1, le=100_000)
    ticks: int = Field(ge=1, le=1_000_000)
    max_work_units: int = Field(
        default=MAX_BENCHMARK_SIMULATION_WORK_UNITS,
        ge=1,
        le=MAX_BENCHMARK_SIMULATION_WORK_UNITS,
    )

    @model_validator(mode="after")
    def _seed_shape(self) -> "BenchmarkSimulationExecutionV1":
        if (self.seed_policy == "fixed") != (self.fixed_seed is not None):
            raise ValueError("fixed simulation seed is present exactly for fixed policy")
        return self


class BenchmarkSeededDetectionExecutorV1(_FrozenModel):
    executor_kind: Literal["seeded_detection"] = "seeded_detection"
    snapshot_payload: dict[str, JsonValue]
    snapshot_id: SnapshotId
    snapshot_payload_hash: Sha256Hex
    constraints: tuple[Constraint, ...] = Field(max_length=4_096)
    constraints_digest: Sha256Hex
    max_checker_work_units: int = Field(
        default=MAX_BENCHMARK_CHECKER_WORK_UNITS,
        ge=1,
        le=MAX_BENCHMARK_CHECKER_WORK_UNITS,
    )
    defect_class: BenchmarkDeterministicDefectClassV1
    expected_finding_bucket: Literal["deterministic", "simulation"]
    unexpected_taxonomy_finding_policy: Literal["fail_case"] = "fail_case"
    injected_entities: tuple[BoundedId, ...] = Field(min_length=1, max_length=4_096)
    needs_navigation: bool
    simulation: "BenchmarkSimulationExecutionV1 | None" = None

    @field_validator("injected_entities")
    @classmethod
    def _entities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if len(canonical) != len(value):
            raise ValueError("benchmark injected entity ids must be unique")
        return canonical

    @model_validator(mode="after")
    def _sealed_input(self) -> "BenchmarkSeededDetectionExecutorV1":
        snapshot_bytes = canonical_json(self.snapshot_payload).encode("utf-8")
        if not snapshot_bytes or len(snapshot_bytes) > MAX_BENCHMARK_SNAPSHOT_BYTES:
            raise ValueError("benchmark snapshot payload exceeds its byte limit")
        if sha256(snapshot_bytes).hexdigest() != self.snapshot_payload_hash:
            raise ValueError("benchmark snapshot payload hash differs from its bytes")
        constraints_payload = [item.model_dump(mode="json") for item in self.constraints]
        if sha256(canonical_json(constraints_payload).encode("utf-8")).hexdigest() != (
            self.constraints_digest
        ):
            raise ValueError("benchmark constraint digest differs from its exact constraints")
        if self.defect_class == "economy_collapse":
            if self.simulation is None:
                raise ValueError("economy-collapse benchmark case requires simulation")
            if self.constraints:
                raise ValueError("economy-collapse benchmark case forbids checker oracles")
            if self.expected_finding_bucket != "simulation":
                raise ValueError("economy-collapse benchmark finding bucket must be simulation")
        elif not self.constraints:
            raise ValueError("deterministic seeded benchmark case requires checker constraints")
        else:
            if self.simulation is not None:
                raise ValueError("checker benchmark case forbids simulation oracles")
            if any(item.has_llm_predicate() for item in self.constraints):
                raise ValueError("deterministic seeded benchmark forbids LLM-routed constraints")
            if self.expected_finding_bucket != "deterministic":
                raise ValueError("checker benchmark finding bucket must be deterministic")
        return self


class BenchmarkCleanOracleFpExecutorV1(_FrozenModel):
    executor_kind: Literal["clean_oracle_fp"] = "clean_oracle_fp"
    snapshot_payload: dict[str, JsonValue]
    snapshot_id: SnapshotId
    snapshot_payload_hash: Sha256Hex
    constraints: tuple[Constraint, ...] = Field(max_length=4_096)
    constraints_digest: Sha256Hex
    max_checker_work_units: int = Field(
        default=MAX_BENCHMARK_CHECKER_WORK_UNITS,
        ge=1,
        le=MAX_BENCHMARK_CHECKER_WORK_UNITS,
    )
    needs_navigation: bool = False
    simulation: "BenchmarkSimulationExecutionV1 | None" = None
    failure_buckets: tuple[Literal["deterministic", "simulation", "unproven"], ...] = Field(
        min_length=1, max_length=3
    )

    @field_validator("failure_buckets")
    @classmethod
    def _failure_buckets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if len(canonical) != len(value):
            raise ValueError("clean benchmark failure buckets must be unique")
        return canonical

    @model_validator(mode="after")
    def _sealed_input(self) -> "BenchmarkCleanOracleFpExecutorV1":
        snapshot_bytes = canonical_json(self.snapshot_payload).encode("utf-8")
        if not snapshot_bytes or len(snapshot_bytes) > MAX_BENCHMARK_SNAPSHOT_BYTES:
            raise ValueError("benchmark snapshot payload exceeds its byte limit")
        if sha256(snapshot_bytes).hexdigest() != self.snapshot_payload_hash:
            raise ValueError("benchmark snapshot payload hash differs from its bytes")
        constraints_payload = [item.model_dump(mode="json") for item in self.constraints]
        if sha256(canonical_json(constraints_payload).encode("utf-8")).hexdigest() != (
            self.constraints_digest
        ):
            raise ValueError("benchmark constraint digest differs from its exact constraints")
        if not self.constraints and self.simulation is None:
            raise ValueError("clean benchmark case must select at least one real oracle")
        expected_failure_buckets: set[str] = set()
        if self.constraints:
            # A checker can emit either proved deterministic findings or
            # fail-closed unproven findings.  Clean-oracle FP accounting must
            # observe both; selecting only one would hide part of the oracle.
            expected_failure_buckets.update(("deterministic", "unproven"))
        if self.simulation is not None:
            expected_failure_buckets.add("simulation")
        if set(self.failure_buckets) != expected_failure_buckets:
            raise ValueError("clean benchmark failure buckets must exactly cover selected oracles")
        return self


class BenchmarkAgentResponseExecutorV1(_FrozenModel):
    executor_kind: Literal["agent_response"] = "agent_response"
    prompts: tuple[BoundedPrompt, ...] = Field(min_length=1, max_length=MAX_BENCHMARK_AGENT_TURNS)
    response_format: Literal["text", "json"]
    oracle: BenchmarkJsonPredicateV1


BenchmarkCaseExecutorV1: TypeAlias = Annotated[
    BenchmarkSeededDetectionExecutorV1
    | BenchmarkCleanOracleFpExecutorV1
    | BenchmarkAgentResponseExecutorV1,
    Field(discriminator="executor_kind"),
]


class BenchmarkDatasetCaseV1(_FrozenModel):
    """One executable checker/simulation or bounded Agent benchmark case."""

    case_id: BoundedId
    execution_mode: Literal["deterministic", "agent"]
    executor: BenchmarkCaseExecutorV1
    aggregate_oracle: BenchmarkJsonPredicateV1 | None = None
    metric_refs: tuple[BenchmarkMetricRefV1, ...] = Field(
        min_length=1,
        max_length=MAX_BENCHMARK_CASE_METRICS,
    )

    @field_validator("metric_refs")
    @classmethod
    def _metric_refs(
        cls, value: tuple[BenchmarkMetricRefV1, ...]
    ) -> tuple[BenchmarkMetricRefV1, ...]:
        keys = [(item.metric_id, item.metric_version) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("benchmark case metric refs must be unique")
        return tuple(sorted(value, key=lambda item: (item.metric_id, item.metric_version)))

    @model_validator(mode="after")
    def _execution_shape(self) -> "BenchmarkDatasetCaseV1":
        is_agent = isinstance(self.executor, BenchmarkAgentResponseExecutorV1)
        if is_agent != (self.execution_mode == "agent"):
            raise ValueError("benchmark case execution mode differs from its executor")
        return self


class BenchmarkDatasetPartitionV1(_FrozenModel):
    partition_id: BoundedId
    cases: tuple[BenchmarkDatasetCaseV1, ...] = Field(
        min_length=1,
        max_length=MAX_BENCHMARK_CASES_PER_PARTITION,
    )

    @field_validator("cases")
    @classmethod
    def _canonical_cases(
        cls, value: tuple[BenchmarkDatasetCaseV1, ...]
    ) -> tuple[BenchmarkDatasetCaseV1, ...]:
        ids = [item.case_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark dataset partition case ids must be unique")
        return tuple(sorted(value, key=lambda item: item.case_id))


class BenchmarkDatasetV1(_FrozenModel):
    """Portable, fully executable ``bench-dataset@1`` payload.

    The report template carries the non-selected historical evidence sections. The
    production composer only mutates binary metrics that are explicitly bound by
    ``binary_metrics`` and recomputes their power rows; all bytes remain covered by
    the dataset Artifact hash.
    """

    benchmark_dataset_schema_version: Literal["bench-dataset@1"] = "bench-dataset@1"
    partitions: tuple[BenchmarkDatasetPartitionV1, ...] = Field(
        min_length=1,
        max_length=MAX_BENCHMARK_PARTITIONS,
    )
    binary_metrics: tuple[BenchmarkBinaryMetricDefinitionV1, ...] = Field(
        min_length=1,
        max_length=MAX_BENCHMARK_METRICS,
    )
    report_template_utf8: str
    report_template_sha256: Sha256Hex

    @field_validator("partitions")
    @classmethod
    def _canonical_partitions(
        cls, value: tuple[BenchmarkDatasetPartitionV1, ...]
    ) -> tuple[BenchmarkDatasetPartitionV1, ...]:
        ids = [item.partition_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark dataset partition ids must be unique")
        return tuple(sorted(value, key=lambda item: item.partition_id))

    @field_validator("binary_metrics")
    @classmethod
    def _canonical_metric_definitions(
        cls, value: tuple[BenchmarkBinaryMetricDefinitionV1, ...]
    ) -> tuple[BenchmarkBinaryMetricDefinitionV1, ...]:
        refs = [(item.metric.metric_id, item.metric.metric_version) for item in value]
        targets = [
            (
                item.target.collection,
                item.target.name,
                item.target.defect_class,
                item.target.bucket,
            )
            for item in value
        ]
        if len(refs) != len(set(refs)) or len(targets) != len(set(targets)):
            raise ValueError("benchmark metric definitions must have unique refs and targets")
        power_classes = [item.target.defect_class for item in value if item.updates_power]
        if len(power_classes) != len(set(power_classes)):
            raise ValueError("benchmark defect class has multiple power-driving metrics")
        return tuple(
            sorted(value, key=lambda item: (item.metric.metric_id, item.metric.metric_version))
        )

    @model_validator(mode="after")
    def _closed_dataset(self) -> "BenchmarkDatasetV1":
        cases = [case for partition in self.partitions for case in partition.cases]
        case_ids = [case.case_id for case in cases]
        if len(case_ids) > MAX_BENCHMARK_DATASET_CASES:
            raise ValueError("benchmark dataset exceeds the global case limit")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("benchmark dataset case ids must be globally unique")
        metric_refs = {
            (item.metric.metric_id, item.metric.metric_version) for item in self.binary_metrics
        }
        referenced = {
            (ref.metric_id, ref.metric_version) for case in cases for ref in case.metric_refs
        }
        if referenced != metric_refs:
            raise ValueError("benchmark dataset cases and metric definitions differ")
        template = self.report_template_utf8.encode("utf-8")
        if not template or len(template) > MAX_BENCHMARK_REPORT_BYTES:
            raise ValueError("benchmark report template exceeds the frozen byte limit")
        if sha256(template).hexdigest() != self.report_template_sha256:
            raise ValueError("benchmark report template digest differs from its exact bytes")
        return self


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


class BenchmarkAggregateSchemaBindingV1(_FrozenModel):
    artifact_kind: Literal[
        "checker_run",
        "simulation_run",
        "playtest_trace",
        "review_report",
        "run_result",
        "validation_evidence",
        "regression_evidence",
    ]
    payload_schema_ids: tuple[BoundedId, ...] = Field(min_length=1)

    @field_validator("payload_schema_ids")
    @classmethod
    def _schema_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if len(canonical) != len(value):
            raise ValueError("benchmark aggregate schema ids must be unique")
        return canonical


class BenchmarkEvaluatorPolicyV1(_FrozenModel):
    """Executable policy embedded in the immutable bench-evaluator profile."""

    policy_schema_version: Literal["benchmark-evaluator-policy@1"] = "benchmark-evaluator-policy@1"
    policy_id: BoundedId
    policy_version: PositiveInt
    dataset_schema_id: Literal["bench-dataset@1"] = "bench-dataset@1"
    predicate_schema_version: Literal["benchmark-json-predicate@1"] = "benchmark-json-predicate@1"
    aggregate_schemas: tuple[BenchmarkAggregateSchemaBindingV1, ...]
    max_case_executions: int = Field(ge=1, le=MAX_BENCHMARK_CASE_EXECUTIONS)
    max_prepared_report_bytes: int = Field(ge=1, le=MAX_BENCHMARK_REPORT_BYTES)
    max_aggregate_input_bytes_per_artifact: int = Field(
        ge=1, le=MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT
    )
    max_aggregate_input_bytes_total: int = Field(ge=1, le=MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL)
    max_checker_work_units_total: int = Field(
        default=MAX_BENCHMARK_CHECKER_WORK_UNITS,
        ge=1,
        le=MAX_BENCHMARK_CHECKER_WORK_UNITS,
    )
    max_simulation_work_units_total: int = Field(ge=1, le=MAX_BENCHMARK_SIMULATION_WORK_UNITS)
    max_result_metrics_bytes_total: int = Field(
        default=MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL,
        ge=1,
        le=MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL,
    )
    max_agent_model_calls_total: int = Field(
        default=MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL,
        ge=1,
        le=MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL,
    )
    policy_digest: Sha256Hex

    @field_validator("aggregate_schemas")
    @classmethod
    def _aggregate_schemas(
        cls, value: tuple[BenchmarkAggregateSchemaBindingV1, ...]
    ) -> tuple[BenchmarkAggregateSchemaBindingV1, ...]:
        kinds = [item.artifact_kind for item in value]
        if len(kinds) != len(set(kinds)):
            raise ValueError("benchmark evaluator aggregate kinds must be unique")
        return tuple(sorted(value, key=lambda item: item.artifact_kind))

    @model_validator(mode="after")
    def _closed_policy(self) -> "BenchmarkEvaluatorPolicyV1":
        expected = tuple(
            BenchmarkAggregateSchemaBindingV1(
                artifact_kind=kind,  # type: ignore[arg-type]
                payload_schema_ids=schemas,
            )
            for kind, schemas in sorted(BENCHMARK_AGGREGATE_RESULT_SCHEMAS.items())
        )
        if self.aggregate_schemas != expected:
            raise ValueError("benchmark evaluator policy has incomplete aggregate schemas")
        payload = self.model_dump(mode="json", exclude={"policy_digest"})
        if sha256(canonical_json(payload).encode("utf-8")).hexdigest() != self.policy_digest:
            raise ValueError("benchmark evaluator policy digest differs from its payload")
        return self

    @property
    def ref(self) -> BenchmarkEvaluatorPolicyRefV1:
        return BenchmarkEvaluatorPolicyRefV1(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            policy_digest=self.policy_digest,
        )


class BenchmarkEvaluatorProfileConfigV1(_FrozenModel):
    config_schema_version: Literal["bench_evaluator-profile-config@1"] = (
        "bench_evaluator-profile-config@1"
    )
    policy: BenchmarkEvaluatorPolicyV1


def build_builtin_benchmark_evaluator_policy() -> BenchmarkEvaluatorPolicyV1:
    payload = {
        "policy_schema_version": "benchmark-evaluator-policy@1",
        "policy_id": "builtin.bench_evaluator",
        "policy_version": 1,
        "dataset_schema_id": "bench-dataset@1",
        "predicate_schema_version": "benchmark-json-predicate@1",
        "aggregate_schemas": [
            {
                "artifact_kind": kind,
                "payload_schema_ids": list(schemas),
            }
            for kind, schemas in sorted(BENCHMARK_AGGREGATE_RESULT_SCHEMAS.items())
        ],
        "max_case_executions": MAX_BENCHMARK_CASE_EXECUTIONS,
        "max_prepared_report_bytes": MAX_BENCHMARK_REPORT_BYTES,
        "max_aggregate_input_bytes_per_artifact": (
            MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT
        ),
        "max_aggregate_input_bytes_total": MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL,
        "max_checker_work_units_total": MAX_BENCHMARK_CHECKER_WORK_UNITS,
        "max_simulation_work_units_total": MAX_BENCHMARK_SIMULATION_WORK_UNITS,
        "max_result_metrics_bytes_total": MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL,
        "max_agent_model_calls_total": MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL,
    }
    return BenchmarkEvaluatorPolicyV1(
        **payload,
        policy_digest=sha256(canonical_json(payload).encode("utf-8")).hexdigest(),
    )


class BenchmarkResourceLimitsV1(_FrozenModel):
    max_case_executions: int = Field(ge=1, le=MAX_BENCHMARK_CASE_EXECUTIONS)
    max_prepared_report_bytes: int = Field(ge=1, le=MAX_BENCHMARK_REPORT_BYTES)
    max_aggregate_input_bytes_per_artifact: int = Field(
        ge=1, le=MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT
    )
    max_aggregate_input_bytes_total: int = Field(ge=1, le=MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL)
    max_checker_work_units_total: int = Field(
        default=MAX_BENCHMARK_CHECKER_WORK_UNITS,
        ge=1,
        le=MAX_BENCHMARK_CHECKER_WORK_UNITS,
    )
    max_simulation_work_units_total: int = Field(ge=1, le=MAX_BENCHMARK_SIMULATION_WORK_UNITS)
    max_result_metrics_bytes_total: int = Field(
        default=MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL,
        ge=1,
        le=MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL,
    )
    max_agent_model_calls_total: int = Field(
        default=MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL,
        ge=1,
        le=MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL,
    )


class BenchmarkAggregateProducerSeedBindingV1(_FrozenModel):
    """Versioned meaning of the producer Run seed for one aggregate input."""

    producer_seed_binding_schema_version: Literal["benchmark-aggregate-producer-seed-binding@1"] = (
        "benchmark-aggregate-producer-seed-binding@1"
    )
    relation: Literal["seed_independent", "bench_child", "fixed_case_seed"]


class BenchmarkAggregateInputBindingV1(_FrozenModel):
    """Exact pre-existing result Artifact bound to one case replication."""

    binding_schema_version: Literal["benchmark-aggregate-input-binding@1"] = (
        "benchmark-aggregate-input-binding@1"
    )
    case_id: BoundedId
    partition_id: BoundedId
    execution_mode: Literal["deterministic", "agent"]
    replication_index: int = Field(ge=0, le=MAX_BENCHMARK_REPETITIONS - 1)
    artifact_id: BoundedId
    payload_hash: Sha256Hex
    payload_size_bytes: int = Field(ge=1, le=MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT)
    artifact_kind: Literal[
        "checker_run",
        "simulation_run",
        "playtest_trace",
        "review_report",
        "run_result",
        "validation_evidence",
        "regression_evidence",
    ]
    payload_schema_id: BoundedId
    producer_run_id: BoundedId
    producer_run_kind: RunKindRef
    producer_run_payload_hash: Sha256Hex
    producer_attempt_no: int = Field(strict=True, ge=1)
    producer_result_artifact_id: BoundedId
    producer_result_payload_hash: Sha256Hex
    producer_seed_binding: BenchmarkAggregateProducerSeedBindingV1
    producer_root_seed: Uint64 | None = None
    producer_seed_derivation_version: BoundedId | None = None
    producer_resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...] = Field(
        max_length=1_024
    )
    dataset_artifact_id: BoundedId
    evaluator_profile: ProfileRefV1
    run_kind: RunKindRef
    root_seed: Uint64 | None = None
    execution_seed: Uint64 | None = None
    seed_derivation_version: Literal["subseed@1"] = SUBSEED_DERIVATION_VERSION_V1

    @field_validator("producer_resolved_profiles")
    @classmethod
    def _canonical_producer_profiles(
        cls, value: tuple[ResolvedExecutionProfileBindingV1, ...]
    ) -> tuple[ResolvedExecutionProfileBindingV1, ...]:
        field_paths = [item.field_path for item in value]
        if len(field_paths) != len(set(field_paths)):
            raise ValueError("benchmark aggregate producer profile field paths must be unique")
        return tuple(sorted(value, key=lambda item: item.field_path))

    @model_validator(mode="after")
    def _kind_schema_pair(self) -> "BenchmarkAggregateInputBindingV1":
        if self.payload_schema_id not in BENCHMARK_AGGREGATE_RESULT_SCHEMAS[self.artifact_kind]:
            raise ValueError("benchmark aggregate input kind/schema pair is unsupported")
        if self.run_kind != RunKindRef(kind="bench.run", version=1):
            raise ValueError("benchmark aggregate execution Run kind must be bench.run@1")
        if (self.root_seed is None) != (self.execution_seed is None):
            raise ValueError("benchmark aggregate root and execution seed must be present together")
        if self.root_seed is not None:
            expected_seed = derive_subseed_v1(
                root_seed=self.root_seed,
                run_kind=self.run_kind,
                profile=self.evaluator_profile,
                case_id=self.case_id,
                replication_index=self.replication_index,
            )
            if self.execution_seed != expected_seed:
                raise ValueError("benchmark aggregate execution seed differs from subseed@1")
        relation = self.producer_seed_binding.relation
        if relation == "seed_independent" and self.producer_root_seed is not None:
            raise ValueError("seed-independent benchmark aggregate producer must not carry a seed")
        if relation == "bench_child" and (
            self.producer_root_seed is None
            or self.producer_root_seed != self.execution_seed
            or self.producer_seed_derivation_version is None
        ):
            raise ValueError("benchmark-child producer seed must equal the child execution seed")
        if relation == "fixed_case_seed" and (
            self.producer_root_seed is None or self.producer_seed_derivation_version is None
        ):
            raise ValueError("fixed-case producer seed requires an exact seeded producer")
        return self


def validate_benchmark_aggregate_producer_seed_authority(
    binding: BenchmarkAggregateInputBindingV1,
    dataset_case: BenchmarkDatasetCaseV1,
) -> None:
    """Cross-check one producer seed relation against the exact dataset case."""

    if (
        binding.case_id != dataset_case.case_id
        or binding.execution_mode != dataset_case.execution_mode
    ):
        raise ValueError("benchmark aggregate producer seed authority has the wrong case")
    simulation = getattr(dataset_case.executor, "simulation", None)
    relation = binding.producer_seed_binding.relation
    if simulation is None:
        if relation == "fixed_case_seed":
            raise ValueError("fixed-case producer seed requires an exact fixed simulation case")
        return
    expected_relation = "fixed_case_seed" if simulation.seed_policy == "fixed" else "bench_child"
    if relation != expected_relation:
        raise ValueError("benchmark aggregate producer seed relation differs from its exact case")
    if (
        expected_relation == "fixed_case_seed"
        and binding.producer_root_seed != simulation.fixed_seed
    ):
        raise ValueError("fixed-case producer seed differs from the exact dataset seed")


class BenchmarkSpecV1(_FrozenModel):
    """Complete immutable ``benchmark-spec@1`` admission authority."""

    benchmark_spec_schema_version: Literal["benchmark-spec@1"] = "benchmark-spec@1"
    dataset: BenchmarkDatasetBindingV1
    evaluator_profile: ProfileRefV1
    evaluator_policy: BenchmarkEvaluatorPolicyRefV1
    metric_policy: BenchmarkMetricPolicyV1
    sampling_policy: BenchmarkSamplingPolicyV1
    ordering_policy: BenchmarkOrderingPolicyV1
    resource_limits: BenchmarkResourceLimitsV1 = BenchmarkResourceLimitsV1(
        max_case_executions=MAX_BENCHMARK_CASE_EXECUTIONS,
        max_prepared_report_bytes=MAX_BENCHMARK_REPORT_BYTES,
        max_aggregate_input_bytes_per_artifact=(MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT),
        max_aggregate_input_bytes_total=MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL,
        max_checker_work_units_total=MAX_BENCHMARK_CHECKER_WORK_UNITS,
        max_simulation_work_units_total=MAX_BENCHMARK_SIMULATION_WORK_UNITS,
        max_result_metrics_bytes_total=MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL,
        max_agent_model_calls_total=MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL,
    )
    aggregate_repetition_count: RepetitionCount | None = None
    aggregate_inputs: tuple[BenchmarkAggregateInputBindingV1, ...] = Field(
        default=(),
        max_length=MAX_BENCHMARK_CASE_EXECUTIONS,
    )
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

    @field_validator("aggregate_inputs")
    @classmethod
    def _canonical_aggregate_inputs(
        cls, value: tuple[BenchmarkAggregateInputBindingV1, ...]
    ) -> tuple[BenchmarkAggregateInputBindingV1, ...]:
        return tuple(sorted(value, key=lambda item: (item.case_id, item.replication_index)))

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
        if bool(self.aggregate_inputs) != (self.aggregate_repetition_count is not None):
            raise ValueError(
                "benchmark aggregate inputs and repetition count must be present together"
            )
        if self.aggregate_inputs:
            assert self.aggregate_repetition_count is not None
            artifact_ids = [item.artifact_id for item in self.aggregate_inputs]
            if len(artifact_ids) != len(set(artifact_ids)):
                raise ValueError(
                    "benchmark aggregate input Artifact and execution identities must be unique"
                )
            expected_count = len(case_ids) * self.aggregate_repetition_count
            if (
                expected_count > self.resource_limits.max_case_executions
                or expected_count > MAX_BENCHMARK_CASE_EXECUTIONS
                or expected_count != len(self.aggregate_inputs)
            ):
                raise ValueError(
                    "benchmark aggregate inputs must cover every case replication exactly"
                )
            ordered_case_ids = tuple(sorted(case_ids))
            expected_identities = (
                (case_id, replication_index)
                for case_id in ordered_case_ids
                for replication_index in range(self.aggregate_repetition_count)
            )
            if any(
                (item.case_id, item.replication_index) != expected
                for item, expected in zip(self.aggregate_inputs, expected_identities, strict=True)
            ):
                raise ValueError(
                    "benchmark aggregate inputs must cover every case replication exactly"
                )
            case_authority = {
                case.case_id: (partition.partition_id, case.execution_mode)
                for partition in self.partitions
                for case in partition.cases
            }
            if any(
                (item.partition_id, item.execution_mode) != case_authority[item.case_id]
                for item in self.aggregate_inputs
            ):
                raise ValueError(
                    "benchmark aggregate input partition or execution mode differs from its case"
                )
            if any(
                item.dataset_artifact_id != self.dataset.artifact_id
                for item in self.aggregate_inputs
            ):
                raise ValueError("benchmark aggregate input dataset differs from the typed spec")
            if any(
                item.evaluator_profile != self.evaluator_profile for item in self.aggregate_inputs
            ):
                raise ValueError(
                    "benchmark aggregate input evaluator profile differs from the typed spec"
                )
            if any(
                item.seed_derivation_version != self.sampling_policy.seed_derivation_version
                for item in self.aggregate_inputs
            ):
                raise ValueError(
                    "benchmark aggregate input seed derivation differs from the sampling policy"
                )
            root_seeds = {item.root_seed for item in self.aggregate_inputs}
            if len(root_seeds) != 1:
                raise ValueError("benchmark aggregate inputs must share one exact root seed")
            if any(
                item.payload_size_bytes
                > self.resource_limits.max_aggregate_input_bytes_per_artifact
                for item in self.aggregate_inputs
            ):
                raise ValueError("benchmark aggregate input exceeds the spec per-item limit")
            if (
                sum(item.payload_size_bytes for item in self.aggregate_inputs)
                > self.resource_limits.max_aggregate_input_bytes_total
            ):
                raise ValueError("benchmark aggregate inputs exceed the spec total byte limit")
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


def sampled_partition_cases(
    spec: BenchmarkSpecV1,
    partition_ids: tuple[str, ...],
    *,
    root_seed: int | None,
) -> tuple[tuple[str, BenchmarkCaseExecutionV1], ...]:
    """Apply the frozen per-partition sampling policy without dataset defaults."""

    selected = spec.selected_partitions(partition_ids)
    strategy = spec.sampling_policy.strategy
    sample_size = spec.sampling_policy.sample_size_per_partition
    sampled: list[tuple[str, BenchmarkCaseExecutionV1]] = []
    for partition in selected:
        cases = partition.cases
        if strategy == "all":
            chosen = cases
        elif strategy == "deterministic_prefix":
            assert sample_size is not None
            chosen = cases[:sample_size]
        else:
            if root_seed is None:
                raise ValueError("seeded benchmark sampling requires a root seed")
            assert sample_size is not None
            ranked = sorted(
                cases,
                key=lambda case: (
                    derive_subseed_v1(
                        root_seed=root_seed,
                        run_kind=RunKindRef(kind="bench.run", version=1),
                        profile=spec.evaluator_profile,
                        case_id=case.case_id,
                        replication_index=0,
                    ),
                    case.case_id,
                ),
            )
            chosen = tuple(ranked[:sample_size])
        sampled.extend((partition.partition_id, case) for case in chosen)
    return tuple(sampled)


__all__ = [
    "BENCHMARK_AGGREGATE_RESULT_SCHEMAS",
    "MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT",
    "MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL",
    "MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL",
    "MAX_BENCHMARK_CASE_EXECUTIONS",
    "MAX_BENCHMARK_CHECKER_WORK_UNITS",
    "MAX_BENCHMARK_REPORT_BYTES",
    "MAX_BENCHMARK_RESULT_METRIC_FIELDS",
    "MAX_BENCHMARK_RESULT_METRICS_BYTES",
    "MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL",
    "MAX_BENCHMARK_SIMULATION_WORK_UNITS",
    "BenchmarkAggregateInputBindingV1",
    "BenchmarkAggregateProducerSeedBindingV1",
    "BenchmarkBinaryMetricDefinitionV1",
    "BenchmarkBinaryMetricTargetV1",
    "BenchmarkCaseExecutionV1",
    "BenchmarkDatasetCaseV1",
    "BenchmarkDatasetBindingV1",
    "BenchmarkDatasetPartitionV1",
    "BenchmarkDatasetV1",
    "BenchmarkAgentResponseExecutorV1",
    "BenchmarkCleanOracleFpExecutorV1",
    "BenchmarkSeededDetectionExecutorV1",
    "BenchmarkSimulationExecutionV1",
    "BenchmarkEvaluatorPolicyV1",
    "BenchmarkEvaluatorPolicyRefV1",
    "BenchmarkEvaluatorProfileConfigV1",
    "BenchmarkJsonPredicateV1",
    "BenchmarkMetricPolicyV1",
    "BenchmarkMetricRefV1",
    "BenchmarkOrderKeyV1",
    "BenchmarkOrderingPolicyV1",
    "BenchmarkPartitionV1",
    "BenchmarkResourceLimitsV1",
    "BenchmarkSamplingPolicyV1",
    "BenchmarkSpecV1",
    "build_builtin_benchmark_evaluator_policy",
    "sampled_partition_cases",
    "validate_benchmark_aggregate_producer_seed_authority",
]
