from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.benchmark import (
    BenchmarkAggregateInputBindingV1,
    BenchmarkCaseExecutionV1,
    BenchmarkDatasetBindingV1,
    BenchmarkEvaluatorPolicyRefV1,
    BenchmarkMetricPolicyV1,
    BenchmarkMetricRefV1,
    BenchmarkOrderKeyV1,
    BenchmarkOrderingPolicyV1,
    BenchmarkPartitionV1,
    BenchmarkSamplingPolicyV1,
    BenchmarkSpecV1,
    sampled_partition_cases,
)
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef


def _spec(*partitions: BenchmarkPartitionV1) -> BenchmarkSpecV1:
    return BenchmarkSpecV1(
        dataset=BenchmarkDatasetBindingV1(
            artifact_id="artifact:dataset",
            payload_hash="a" * 64,
        ),
        evaluator_profile=ProfileRefV1(profile_id="builtin.bench_evaluator", version=1),
        evaluator_policy=BenchmarkEvaluatorPolicyRefV1(
            policy_id="bench-evaluator",
            policy_version=3,
            policy_digest="b" * 64,
        ),
        metric_policy=BenchmarkMetricPolicyV1(
            policy_id="bench-metrics",
            policy_version=2,
            metrics=(
                BenchmarkMetricRefV1(metric_id="false-positive-rate", metric_version=1),
                BenchmarkMetricRefV1(metric_id="bug-detection-rate", metric_version=1),
            ),
        ),
        sampling_policy=BenchmarkSamplingPolicyV1(
            policy_id="bench-sampling",
            policy_version=4,
            strategy="all",
            minimum_repetitions=1,
            maximum_repetitions=10,
            seed_derivation_version="subseed@1",
        ),
        ordering_policy=BenchmarkOrderingPolicyV1(
            policy_id="bench-ordering",
            policy_version=2,
            keys=(
                BenchmarkOrderKeyV1(
                    field_path="/partition_id",
                    direction="ascending",
                    nulls="forbidden",
                ),
                BenchmarkOrderKeyV1(
                    field_path="/case_id",
                    direction="ascending",
                    nulls="forbidden",
                ),
            ),
        ),
        partitions=partitions,
    )


def _partition(partition_id: str, *cases: tuple[str, str]) -> BenchmarkPartitionV1:
    return BenchmarkPartitionV1(
        partition_id=partition_id,
        cases=tuple(
            BenchmarkCaseExecutionV1(case_id=case_id, execution_mode=mode)
            for case_id, mode in cases
        ),
    )


def test_benchmark_spec_canonicalizes_unordered_sets_but_preserves_order_policy() -> None:
    spec = _spec(
        _partition("z", ("z2", "agent"), ("z1", "deterministic")),
        _partition("a", ("a1", "deterministic")),
    )

    assert tuple(item.partition_id for item in spec.partitions) == ("a", "z")
    assert tuple(item.case_id for item in spec.partitions[1].cases) == ("z1", "z2")
    assert tuple(item.metric_id for item in spec.metric_policy.metrics) == (
        "bug-detection-rate",
        "false-positive-rate",
    )
    assert tuple(item.field_path for item in spec.ordering_policy.keys) == (
        "/partition_id",
        "/case_id",
    )
    assert spec.selected_partitions(()) == spec.partitions
    assert tuple(item.partition_id for item in spec.selected_partitions(("z",))) == ("z",)


def test_benchmark_spec_rejects_case_identity_reused_across_partitions() -> None:
    with pytest.raises(ValidationError, match="globally unique"):
        _spec(
            _partition("a", ("same", "deterministic")),
            _partition("b", ("same", "agent")),
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"minimum_repetitions": 3, "maximum_repetitions": 2}, "range is inverted"),
        ({"strategy": "all", "sample_size_per_partition": 1}, "forbids sample_size"),
        (
            {"strategy": "seeded_without_replacement", "sample_size_per_partition": None},
            "requires it",
        ),
        (
            {"strategy": "deterministic_prefix", "sample_size_per_partition": None},
            "requires it",
        ),
    ],
)
def test_benchmark_sampling_policy_rejects_ambiguous_shapes(
    updates: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "policy_id": "sampling",
        "policy_version": 1,
        "strategy": "all",
        "sample_size_per_partition": None,
        "minimum_repetitions": 1,
        "maximum_repetitions": 2,
        "seed_derivation_version": "subseed@1",
    }
    values.update(updates)

    with pytest.raises(ValidationError, match=message):
        BenchmarkSamplingPolicyV1.model_validate(values)


def test_benchmark_spec_rejects_sampling_beyond_any_partition() -> None:
    values = _spec(_partition("small", ("case:1", "deterministic"))).model_dump(mode="python")
    values["sampling_policy"] = {
        **values["sampling_policy"],
        "strategy": "deterministic_prefix",
        "sample_size_per_partition": 2,
    }

    with pytest.raises(ValidationError, match="sample size exceeds"):
        BenchmarkSpecV1.model_validate(values)


def test_aggregate_product_limit_is_checked_before_coverage_expansion() -> None:
    values = _spec(
        _partition(
            "cases",
            ("case:1", "deterministic"),
            ("case:2", "deterministic"),
        )
    ).model_dump(mode="python")
    values["aggregate_repetition_count"] = 100_000
    values["aggregate_inputs"] = [
        BenchmarkAggregateInputBindingV1(
            case_id="case:1",
            partition_id="cases",
            execution_mode="deterministic",
            replication_index=0,
            artifact_id="artifact:result",
            payload_hash="c" * 64,
            payload_size_bytes=1,
            artifact_kind="checker_run",
            payload_schema_id="checker-report@1",
            producer_run_id="run:checker:1",
            producer_run_kind=RunKindRef(kind="checker.run", version=1),
            producer_run_payload_hash="d" * 64,
            producer_attempt_no=1,
            producer_result_artifact_id="artifact:producer-result",
            producer_result_payload_hash="e" * 64,
            producer_seed_binding={"relation": "seed_independent"},
            producer_root_seed=None,
            producer_seed_derivation_version=None,
            producer_resolved_profiles=(),
            dataset_artifact_id="artifact:dataset",
            evaluator_profile=ProfileRefV1(
                profile_id="builtin.bench_evaluator",
                version=1,
            ),
            run_kind=RunKindRef(kind="bench.run", version=1),
            root_seed=None,
            execution_seed=None,
            seed_derivation_version="subseed@1",
        ).model_dump(mode="python")
    ]

    with pytest.raises(ValidationError, match="cover every case replication"):
        BenchmarkSpecV1.model_validate(values)


def test_seeded_sampling_ranks_by_the_frozen_subseed_v1_vector() -> None:
    values = _spec(
        _partition(
            "seeded",
            *((f"case:{index}", "deterministic") for index in range(8)),
        )
    ).model_dump(mode="python")
    values["sampling_policy"] = {
        **values["sampling_policy"],
        "strategy": "seeded_without_replacement",
        "sample_size_per_partition": 3,
    }
    spec = BenchmarkSpecV1.model_validate(values)

    sampled = sampled_partition_cases(spec, ("seeded",), root_seed=0)

    assert tuple(case.case_id for _, case in sampled) == ("case:7", "case:5", "case:6")


def test_aggregate_binding_freezes_complete_producer_and_benchmark_seed_provenance() -> None:
    binding = BenchmarkAggregateInputBindingV1(
        case_id="case:1",
        partition_id="cases",
        execution_mode="deterministic",
        replication_index=0,
        artifact_id="artifact:result",
        payload_hash="c" * 64,
        payload_size_bytes=1,
        artifact_kind="checker_run",
        payload_schema_id="checker-report@1",
        producer_run_id="run:checker:1",
        producer_run_kind=RunKindRef(kind="checker.run", version=1),
        producer_run_payload_hash="d" * 64,
        producer_attempt_no=1,
        producer_result_artifact_id="artifact:producer-result",
        producer_result_payload_hash="e" * 64,
        producer_seed_binding={"relation": "seed_independent"},
        producer_root_seed=None,
        producer_seed_derivation_version=None,
        producer_resolved_profiles=(),
        dataset_artifact_id="artifact:dataset",
        evaluator_profile=ProfileRefV1(
            profile_id="builtin.bench_evaluator",
            version=1,
        ),
        run_kind=RunKindRef(kind="bench.run", version=1),
        root_seed=7,
        execution_seed=6360663870362977205,
        seed_derivation_version="subseed@1",
    )
    values = _spec(_partition("cases", ("case:1", "deterministic"))).model_dump(mode="python")
    values["aggregate_repetition_count"] = 1
    values["aggregate_inputs"] = [binding.model_dump(mode="python")]
    spec = BenchmarkSpecV1.model_validate(values)

    assert spec.aggregate_inputs == (binding,)

    forged = binding.model_copy(update={"execution_seed": binding.execution_seed + 1})
    values["aggregate_inputs"] = [forged.model_dump(mode="python")]
    with pytest.raises(ValidationError, match="execution seed"):
        BenchmarkSpecV1.model_validate(values)


def test_seeded_aggregate_binding_cannot_relabel_one_producer_under_another_child_seed() -> None:
    execution_seed = 6360663870362977205
    common = {
        "case_id": "case:1",
        "partition_id": "cases",
        "execution_mode": "deterministic",
        "replication_index": 0,
        "artifact_id": "artifact:result",
        "payload_hash": "c" * 64,
        "payload_size_bytes": 1,
        "artifact_kind": "simulation_run",
        "payload_schema_id": "simulation-result@1",
        "producer_run_id": "run:simulation:1",
        "producer_run_kind": RunKindRef(kind="simulation.run", version=1),
        "producer_run_payload_hash": "d" * 64,
        "producer_attempt_no": 1,
        "producer_result_artifact_id": "artifact:producer-result",
        "producer_result_payload_hash": "e" * 64,
        "producer_seed_binding": {"relation": "bench_child"},
        "producer_seed_derivation_version": "subseed@1",
        "producer_resolved_profiles": (),
        "dataset_artifact_id": "artifact:dataset",
        "evaluator_profile": ProfileRefV1(
            profile_id="builtin.bench_evaluator",
            version=1,
        ),
        "run_kind": RunKindRef(kind="bench.run", version=1),
        "root_seed": 7,
        "execution_seed": execution_seed,
        "seed_derivation_version": "subseed@1",
    }

    exact = BenchmarkAggregateInputBindingV1(
        **common,
        producer_root_seed=execution_seed,
    )
    assert exact.producer_root_seed == exact.execution_seed

    with pytest.raises(ValidationError, match="producer seed"):
        BenchmarkAggregateInputBindingV1(
            **common,
            producer_root_seed=execution_seed + 1,
        )


def test_fixed_case_producer_seed_coexists_with_benchmark_child_seed() -> None:
    execution_seed = 6360663870362977205

    binding = BenchmarkAggregateInputBindingV1(
        case_id="case:1",
        partition_id="cases",
        execution_mode="deterministic",
        replication_index=0,
        artifact_id="artifact:result",
        payload_hash="c" * 64,
        payload_size_bytes=1,
        artifact_kind="simulation_run",
        payload_schema_id="simulation-result@1",
        producer_run_id="run:simulation:1",
        producer_run_kind=RunKindRef(kind="simulation.run", version=1),
        producer_run_payload_hash="d" * 64,
        producer_attempt_no=1,
        producer_result_artifact_id="artifact:producer-result",
        producer_result_payload_hash="e" * 64,
        producer_seed_binding={"relation": "fixed_case_seed"},
        producer_root_seed=23,
        producer_seed_derivation_version="subseed@1",
        producer_resolved_profiles=(),
        dataset_artifact_id="artifact:dataset",
        evaluator_profile=ProfileRefV1(
            profile_id="builtin.bench_evaluator",
            version=1,
        ),
        run_kind=RunKindRef(kind="bench.run", version=1),
        root_seed=7,
        execution_seed=execution_seed,
        seed_derivation_version="subseed@1",
    )

    assert binding.producer_root_seed == 23
    assert binding.execution_seed == execution_seed
