"""Production Bench ports execute real M3 oracles and strict report composition."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gameforge.apps.worker.bench import WorkerBenchPorts, _evaluate_predicate
from gameforge.bench.bases import clean_base
from gameforge.bench.inject import inject
from gameforge.bench.metrics import default_constraints
from gameforge.bench.report_contracts import BenchReport, canonical_report_bytes
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.benchmark import (
    BenchmarkAgentResponseExecutorV1,
    BenchmarkBinaryMetricDefinitionV1,
    BenchmarkBinaryMetricTargetV1,
    BenchmarkCaseExecutionV1,
    BenchmarkCleanOracleFpExecutorV1,
    BenchmarkDatasetBindingV1,
    BenchmarkDatasetCaseV1,
    BenchmarkDatasetPartitionV1,
    BenchmarkDatasetV1,
    BenchmarkEqualsPredicateV1,
    BenchmarkMetricPolicyV1,
    BenchmarkMetricRefV1,
    BenchmarkOrderKeyV1,
    BenchmarkOrderingPolicyV1,
    BenchmarkPartitionV1,
    BenchmarkSamplingPolicyV1,
    BenchmarkSeededDetectionExecutorV1,
    BenchmarkSimulationExecutionV1,
    BenchmarkSpecV1,
    build_builtin_benchmark_evaluator_policy,
)
from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.lineage import (
    ArtifactV2,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_key_for_sha256,
)
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.run_handlers.bench import (
    BENCH_SEED_DERIVATION_VERSION,
    BenchAggregateInputExpectationV1,
    BenchCaseEvaluationRequestV1,
    BenchCaseResultV1,
    BenchCaseSpecV1,
)
from gameforge.spine.ir.snapshot import Snapshot


class _Artifacts:
    def __init__(self) -> None:
        self.artifacts: dict[str, ArtifactV2] = {}
        self.blobs: dict[str, bytes] = {}
        self.read_calls: list[str] = []

    def put(
        self,
        *,
        kind: str,
        schema: str,
        payload: object,
        lineage: tuple[str, ...] = (),
    ) -> ArtifactV2:
        blob = canonical_json(payload).encode("utf-8")
        digest = sha256_lowerhex(blob)
        ref = ObjectRef(key=object_key_for_sha256(digest), sha256=digest, size_bytes=len(blob))
        artifact = build_artifact_v2(
            kind=kind,  # type: ignore[arg-type]
            version_tuple=VersionTuple(tool_version="test@1"),
            lineage=lineage,
            payload_hash=digest,
            object_ref=ref,
            meta={"payload_schema_id": schema},
        )
        self.artifacts[artifact.artifact_id] = artifact
        self.blobs[artifact.artifact_id] = blob
        return artifact

    def load_artifact(self, artifact_id: str) -> ArtifactV2:
        return self.artifacts[artifact_id]

    def read_bytes_bounded(self, artifact_id: str, *, max_bytes: int) -> bytes:
        self.read_calls.append(artifact_id)
        blob = self.blobs[artifact_id]
        if len(blob) > max_bytes:
            raise AssertionError("oversized fixture must never be read")
        return blob


def _digest_constraints(constraints) -> str:
    return sha256_lowerhex(
        canonical_json([item.model_dump(mode="json") for item in constraints]).encode()
    )


def _authority(*, maximum_repetitions: int = 1):
    registry = build_builtin_registry()
    catalog = registry.list_execution_profile_catalogs()[0]
    evaluator = next(item for item in catalog.definitions if item.profile_kind == "bench_evaluator")
    constraints = tuple(item for item in default_constraints() if not item.has_llm_predicate())
    sample = inject(clean_base(), DefectClass.dangling_reference, seed=1)
    snapshot_payload = sample.snapshot.content_payload
    metric = BenchmarkMetricRefV1(metric_id="bdr-dangling", metric_version=1)
    unexpected_metric = BenchmarkMetricRefV1(metric_id="constraint-fp", metric_version=1)
    case = BenchmarkDatasetCaseV1(
        case_id="case:seeded",
        execution_mode="deterministic",
        executor=BenchmarkSeededDetectionExecutorV1(
            snapshot_payload=snapshot_payload,
            snapshot_id=sample.snapshot.snapshot_id,
            snapshot_payload_hash=sha256_lowerhex(canonical_json(snapshot_payload).encode()),
            constraints=constraints,
            constraints_digest=_digest_constraints(constraints),
            defect_class="dangling_reference",
            expected_finding_bucket="deterministic",
            injected_entities=tuple(sample.ground_truth.injected_entities),
            needs_navigation=sample.needs_nav,
            simulation=None,
        ),
        metric_refs=(metric, unexpected_metric),
    )
    template = Path("scenarios/bench/bench-report.json").read_text(encoding="utf-8")
    dataset = BenchmarkDatasetV1(
        partitions=(BenchmarkDatasetPartitionV1(partition_id="seeded", cases=(case,)),),
        binary_metrics=(
            BenchmarkBinaryMetricDefinitionV1(
                metric=metric,
                target=BenchmarkBinaryMetricTargetV1(
                    collection="seeded",
                    name="bdr",
                    defect_class="dangling_reference",
                    bucket="deterministic",
                ),
                result_pointer="/metrics/detected",
                positive_value=True,
                updates_power=True,
            ),
            BenchmarkBinaryMetricDefinitionV1(
                metric=unexpected_metric,
                target=BenchmarkBinaryMetricTargetV1(
                    collection="false_positives",
                    name="constraint_fp",
                    bucket="constraint_fp",
                ),
                result_pointer="/metrics/unexpected_taxonomy_finding",
                positive_value=True,
            ),
        ),
        report_template_utf8=template,
        report_template_sha256=sha256_lowerhex(template.encode()),
    )
    artifacts = _Artifacts()
    dataset_artifact = artifacts.put(
        kind="bench_dataset", schema="bench-dataset@1", payload=dataset.model_dump(mode="json")
    )
    spec = BenchmarkSpecV1(
        dataset=BenchmarkDatasetBindingV1(
            artifact_id=dataset_artifact.artifact_id,
            payload_hash=dataset_artifact.payload_hash,
        ),
        evaluator_profile=evaluator.profile,
        evaluator_policy=build_builtin_benchmark_evaluator_policy().ref,
        metric_policy=BenchmarkMetricPolicyV1(
            policy_id="test.metrics",
            policy_version=1,
            metrics=(metric, unexpected_metric),
        ),
        sampling_policy=BenchmarkSamplingPolicyV1(
            policy_id="test.sampling",
            policy_version=1,
            strategy="all",
            minimum_repetitions=1,
            maximum_repetitions=maximum_repetitions,
            seed_derivation_version="subseed@1",
        ),
        ordering_policy=BenchmarkOrderingPolicyV1(
            policy_id="test.order",
            policy_version=1,
            keys=(
                BenchmarkOrderKeyV1(
                    field_path="/executor/defect_class",
                    direction="descending",
                    nulls="forbidden",
                ),
            ),
        ),
        partitions=(
            BenchmarkPartitionV1(
                partition_id="seeded",
                cases=(
                    BenchmarkCaseExecutionV1(
                        case_id=case.case_id, execution_mode=case.execution_mode
                    ),
                ),
            ),
        ),
    )
    spec_artifact = artifacts.put(
        kind="benchmark_spec",
        schema="benchmark-spec@1",
        payload=spec.model_dump(mode="json"),
        lineage=(dataset_artifact.artifact_id,),
    )
    return registry, catalog, evaluator.profile, artifacts, dataset_artifact, spec_artifact


def test_production_bench_executes_real_oracle_and_composes_strict_report() -> None:
    registry, catalog, profile, artifacts, dataset_artifact, spec_artifact = _authority()
    ports = WorkerBenchPorts(registry=registry, artifacts=artifacts)
    cases = ports.load_cases(
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        partition_ids=("seeded",),
        root_seed=None,
        seed_derivation_version=BENCH_SEED_DERIVATION_VERSION,
        evaluator_profile=profile,
        execution_profile_catalog_version=catalog.catalog_version,
        execution_profile_catalog_digest=catalog.catalog_digest,
        repetition_count=1,
        execution_scope="execute_cases",
    )
    request = BenchCaseEvaluationRequestV1(
        case=cases[0],
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        evaluator_profile=profile,
        run_kind=RunKindRef(kind="bench.run", version=1),
        root_seed=None,
        replication_index=0,
        execution_seed=None,
        seed_derivation_version="subseed@1",
        case_ordinal=0,
        result_ordinal=0,
        agent_source_suffix=None,
    )
    verdict = ports.evaluate(request, agent_invoker=None)
    assert verdict.status == "pass"
    result = BenchCaseResultV1(
        case_id=cases[0].case_id,
        partition_id=cases[0].partition_id,
        mode=cases[0].mode,
        dataset_artifact_id=request.dataset_artifact_id,
        benchmark_spec_artifact_id=request.benchmark_spec_artifact_id,
        evaluator_profile=profile,
        run_kind=request.run_kind,
        root_seed=None,
        replication_index=0,
        execution_seed=None,
        seed_derivation_version="subseed@1",
        case_ordinal=0,
        result_ordinal=0,
        agent_source_suffix=None,
        model_call_ordinals=(),
        status=verdict.status,
        metrics=verdict.metrics,
    )
    blob = ports.compose_execute(
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        partition_ids=("seeded",),
        case_results=(result,),
        seed=None,
        evaluator_profile=profile,
        execution_profile_catalog_version=catalog.catalog_version,
        execution_profile_catalog_digest=catalog.catalog_digest,
    )
    report = BenchReport.model_validate_json(blob)
    assert canonical_report_bytes(report) == blob
    metric = next(
        item for item in report.seeded if item.defect_class is DefectClass.dangling_reference
    )
    assert (metric.evaluated_n, metric.k) == (1, 1)
    unexpected = next(item for item in report.false_positives if item.name == "constraint_fp")
    assert (unexpected.evaluated_n, unexpected.k) == (1, 0)


def test_clean_structural_case_without_economy_does_not_run_simulation() -> None:
    constraints = tuple(default_constraints()[:1])
    snapshot = Snapshot(entities={}, relations={})
    executor = BenchmarkCleanOracleFpExecutorV1(
        snapshot_payload=snapshot.content_payload,
        snapshot_id=snapshot.snapshot_id,
        snapshot_payload_hash=sha256_lowerhex(canonical_json(snapshot.content_payload).encode()),
        constraints=constraints,
        constraints_digest=_digest_constraints(constraints),
        simulation=None,
        failure_buckets=("deterministic", "unproven"),
    )
    assert executor.simulation is None
    report = WorkerBenchPorts._run_oracle_pipeline(executor, execution_seed=None)
    assert report.simulation_findings == []


def test_checker_case_work_is_rejected_before_constraint_compilation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constraints = tuple(default_constraints()[:2])
    snapshot = Snapshot(entities={}, relations={})
    executor = BenchmarkCleanOracleFpExecutorV1(
        snapshot_payload=snapshot.content_payload,
        snapshot_id=snapshot.snapshot_id,
        snapshot_payload_hash=sha256_lowerhex(canonical_json(snapshot.content_payload).encode()),
        constraints=constraints,
        constraints_digest=_digest_constraints(constraints),
        max_checker_work_units=1,
        simulation=None,
        failure_buckets=("deterministic", "unproven"),
    )

    def unexpected_compile(_constraints: object) -> object:
        raise AssertionError("checker constraints compiled before their work precheck")

    monkeypatch.setattr("gameforge.apps.worker.bench.compile_all", unexpected_compile)

    with pytest.raises(IntegrityViolation, match="work budget"):
        WorkerBenchPorts._run_oracle_pipeline(executor, execution_seed=None)


def test_checker_case_work_is_accumulated_before_worker_execution() -> None:
    registry, catalog, profile, artifacts, dataset_artifact, spec_artifact = _authority()
    dataset = BenchmarkDatasetV1.model_validate_json(artifacts.blobs[dataset_artifact.artifact_id])
    original = dataset.partitions[0].cases[0]
    assert isinstance(original.executor, BenchmarkSeededDetectionExecutorV1)
    bounded_executor = original.executor.model_copy(update={"max_checker_work_units": 5_000})
    first = original.model_copy(update={"executor": bounded_executor})
    second = first.model_copy(update={"case_id": "case:seeded:2"})
    dataset = BenchmarkDatasetV1.model_validate(
        dataset.model_copy(
            update={
                "partitions": (
                    BenchmarkDatasetPartitionV1(
                        partition_id="seeded",
                        cases=(first, second),
                    ),
                )
            }
        ).model_dump(mode="json")
    )
    dataset_artifact = artifacts.put(
        kind="bench_dataset",
        schema="bench-dataset@1",
        payload=dataset.model_dump(mode="json"),
    )
    spec = BenchmarkSpecV1.model_validate_json(artifacts.blobs[spec_artifact.artifact_id])
    spec = BenchmarkSpecV1.model_validate(
        spec.model_copy(
            update={
                "dataset": BenchmarkDatasetBindingV1(
                    artifact_id=dataset_artifact.artifact_id,
                    payload_hash=dataset_artifact.payload_hash,
                ),
                "resource_limits": spec.resource_limits.model_copy(
                    update={"max_checker_work_units_total": 8_000}
                ),
                "partitions": (
                    BenchmarkPartitionV1(
                        partition_id="seeded",
                        cases=(
                            BenchmarkCaseExecutionV1(
                                case_id=first.case_id,
                                execution_mode="deterministic",
                            ),
                            BenchmarkCaseExecutionV1(
                                case_id=second.case_id,
                                execution_mode="deterministic",
                            ),
                        ),
                    ),
                ),
            }
        ).model_dump(mode="json")
    )
    spec_artifact = artifacts.put(
        kind="benchmark_spec",
        schema="benchmark-spec@1",
        payload=spec.model_dump(mode="json"),
        lineage=(dataset_artifact.artifact_id,),
    )
    ports = WorkerBenchPorts(registry=registry, artifacts=artifacts)

    with pytest.raises(IntegrityViolation, match="Run-total work limit"):
        ports.load_cases(
            dataset_artifact_id=dataset_artifact.artifact_id,
            benchmark_spec_artifact_id=spec_artifact.artifact_id,
            partition_ids=("seeded",),
            root_seed=None,
            seed_derivation_version=BENCH_SEED_DERIVATION_VERSION,
            evaluator_profile=profile,
            execution_profile_catalog_version=catalog.catalog_version,
            execution_profile_catalog_digest=catalog.catalog_digest,
            repetition_count=1,
            execution_scope="execute_cases",
        )


def test_pure_deterministic_case_cannot_fake_independent_repetitions() -> None:
    registry, catalog, profile, artifacts, dataset_artifact, spec_artifact = _authority(
        maximum_repetitions=2
    )
    ports = WorkerBenchPorts(registry=registry, artifacts=artifacts)

    with pytest.raises(IntegrityViolation, match="require one repetition"):
        ports.load_cases(
            dataset_artifact_id=dataset_artifact.artifact_id,
            benchmark_spec_artifact_id=spec_artifact.artifact_id,
            partition_ids=("seeded",),
            root_seed=None,
            seed_derivation_version=BENCH_SEED_DERIVATION_VERSION,
            evaluator_profile=profile,
            execution_profile_catalog_version=catalog.catalog_version,
            execution_profile_catalog_digest=catalog.catalog_digest,
            repetition_count=2,
            execution_scope="execute_cases",
        )


def test_benchmark_simulation_rejects_valid_but_unbounded_work_before_execution() -> None:
    snapshot = Snapshot(entities={}, relations={})
    executor = BenchmarkCleanOracleFpExecutorV1(
        snapshot_payload=snapshot.content_payload,
        snapshot_id=snapshot.snapshot_id,
        snapshot_payload_hash=sha256_lowerhex(canonical_json(snapshot.content_payload).encode()),
        constraints=(),
        constraints_digest=_digest_constraints(()),
        simulation=BenchmarkSimulationExecutionV1(
            seed_policy="fixed",
            fixed_seed=1,
            agents=100_000,
            ticks=1_000_000,
        ),
        failure_buckets=("simulation",),
    )

    with pytest.raises(IntegrityViolation, match="work budget"):
        WorkerBenchPorts._run_oracle_pipeline(executor, execution_seed=None)


def test_agent_json_oracle_is_strictly_typed() -> None:
    executor = BenchmarkAgentResponseExecutorV1(
        prompts=("judge",),
        response_format="json",
        oracle=BenchmarkEqualsPredicateV1(operator="equals", expected=1),
    )
    case = BenchmarkDatasetCaseV1(
        case_id="case:agent",
        execution_mode="agent",
        executor=executor,
        metric_refs=(BenchmarkMetricRefV1(metric_id="agent", metric_version=1),),
    )
    request = BenchCaseEvaluationRequestV1(
        case=BenchCaseSpecV1(
            case_id=case.case_id,
            partition_id="agent",
            mode="agent",
            prompt="judge",
            payload=case.model_dump(mode="json"),
        ),
        dataset_artifact_id="artifact:dataset",
        benchmark_spec_artifact_id="artifact:spec",
        evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
        run_kind=RunKindRef(kind="bench.run", version=1),
        root_seed=7,
        replication_index=0,
        execution_seed=1,
        seed_derivation_version="subseed@1",
        case_ordinal=0,
        result_ordinal=0,
        agent_source_suffix="sha256:" + "a" * 64,
    )
    ports = WorkerBenchPorts(registry=build_builtin_registry(), artifacts=_Artifacts())

    verdict = ports.evaluate(
        request,
        agent_invoker=lambda _prompt, *, source_suffix: "1",
    )
    assert verdict.status == "pass"
    assert verdict.metrics == {"response_valid": True, "oracle_passed": True}
    assert not _evaluate_predicate(
        BenchmarkEqualsPredicateV1(operator="equals", expected={"a": None}),
        {},
    )


@pytest.mark.parametrize(
    "response",
    (
        "{",
        "NaN",
        '{"v":false,"v":true}',
        "1e400",
    ),
)
def test_agent_invalid_json_response_is_a_measured_case_failure(response: str) -> None:
    executor = BenchmarkAgentResponseExecutorV1(
        prompts=("judge",),
        response_format="json",
        oracle=BenchmarkEqualsPredicateV1(operator="equals", expected=1),
    )
    case = BenchmarkDatasetCaseV1(
        case_id="case:agent",
        execution_mode="agent",
        executor=executor,
        metric_refs=(BenchmarkMetricRefV1(metric_id="agent", metric_version=1),),
    )
    request = BenchCaseEvaluationRequestV1(
        case=BenchCaseSpecV1(
            case_id=case.case_id,
            partition_id="agent",
            mode="agent",
            prompt="judge",
            payload=case.model_dump(mode="json"),
        ),
        dataset_artifact_id="artifact:dataset",
        benchmark_spec_artifact_id="artifact:spec",
        evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
        run_kind=RunKindRef(kind="bench.run", version=1),
        root_seed=7,
        replication_index=0,
        execution_seed=1,
        seed_derivation_version="subseed@1",
        case_ordinal=0,
        result_ordinal=0,
        agent_source_suffix="sha256:" + "a" * 64,
    )
    ports = WorkerBenchPorts(registry=build_builtin_registry(), artifacts=_Artifacts())

    verdict = ports.evaluate(
        request,
        agent_invoker=lambda _prompt, *, source_suffix: response,
    )

    assert verdict.status == "fail"
    assert verdict.metrics == {"response_valid": False, "oracle_passed": False}


def test_agent_provider_failure_still_fails_the_benchmark_run() -> None:
    executor = BenchmarkAgentResponseExecutorV1(
        prompts=("judge",),
        response_format="json",
        oracle=BenchmarkEqualsPredicateV1(operator="equals", expected=1),
    )
    case = BenchmarkDatasetCaseV1(
        case_id="case:agent",
        execution_mode="agent",
        executor=executor,
        metric_refs=(BenchmarkMetricRefV1(metric_id="agent", metric_version=1),),
    )
    request = BenchCaseEvaluationRequestV1(
        case=BenchCaseSpecV1(
            case_id=case.case_id,
            partition_id="agent",
            mode="agent",
            prompt="judge",
            payload=case.model_dump(mode="json"),
        ),
        dataset_artifact_id="artifact:dataset",
        benchmark_spec_artifact_id="artifact:spec",
        evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
        run_kind=RunKindRef(kind="bench.run", version=1),
        root_seed=7,
        replication_index=0,
        execution_seed=1,
        seed_derivation_version="subseed@1",
        case_ordinal=0,
        result_ordinal=0,
        agent_source_suffix="sha256:" + "a" * 64,
    )
    ports = WorkerBenchPorts(registry=build_builtin_registry(), artifacts=_Artifacts())

    def unavailable(_prompt: str, *, source_suffix: str) -> str:
        del source_suffix
        raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        ports.evaluate(request, agent_invoker=unavailable)


def test_clean_fp_buckets_must_exactly_cover_every_selected_oracle() -> None:
    constraints = tuple(default_constraints()[:1])
    snapshot = Snapshot(entities={}, relations={})
    common = {
        "snapshot_payload": snapshot.content_payload,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_payload_hash": sha256_lowerhex(canonical_json(snapshot.content_payload).encode()),
        "constraints": constraints,
        "constraints_digest": _digest_constraints(constraints),
        "simulation": BenchmarkSimulationExecutionV1(
            seed_policy="fixed",
            fixed_seed=1,
            agents=1,
            ticks=1,
        ),
    }

    checker_only = {**common, "simulation": None}
    with pytest.raises(ValidationError, match="exactly cover selected oracles"):
        BenchmarkCleanOracleFpExecutorV1(
            **checker_only,
            failure_buckets=("unproven",),
        )
    with pytest.raises(ValidationError, match="exactly cover selected oracles"):
        BenchmarkCleanOracleFpExecutorV1(
            **common,
            failure_buckets=("deterministic", "simulation"),
        )
    executor = BenchmarkCleanOracleFpExecutorV1(
        **common,
        failure_buckets=("deterministic", "simulation", "unproven"),
    )
    assert executor.failure_buckets == ("deterministic", "simulation", "unproven")


def test_aggregate_size_mismatch_is_rejected_before_any_blob_read() -> None:
    registry, catalog, profile, artifacts, dataset_artifact, spec_artifact = _authority()
    ports = WorkerBenchPorts(registry=registry, artifacts=artifacts)
    cases = ports.load_cases(
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        partition_ids=("seeded",),
        root_seed=None,
        seed_derivation_version="subseed@1",
        evaluator_profile=profile,
        execution_profile_catalog_version=catalog.catalog_version,
        execution_profile_catalog_digest=catalog.catalog_digest,
        repetition_count=1,
        execution_scope="execute_cases",
    )
    request = BenchCaseEvaluationRequestV1(
        case=cases[0],
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        evaluator_profile=profile,
        run_kind=RunKindRef(kind="bench.run", version=1),
        root_seed=None,
        replication_index=0,
        execution_seed=None,
        seed_derivation_version="subseed@1",
        case_ordinal=0,
        result_ordinal=0,
        agent_source_suffix=None,
    )
    result_artifact = artifacts.put(
        kind="checker_run",
        schema="checker-report@1",
        payload={"payload_schema_version": "checker-report@1"},
    )
    reads_before = tuple(artifacts.read_calls)
    expectation = BenchAggregateInputExpectationV1(
        artifact_id=result_artifact.artifact_id,
        payload_hash=result_artifact.payload_hash,
        payload_size_bytes=result_artifact.object_ref.size_bytes - 1,
        artifact_kind="checker_run",
        payload_schema_id="checker-report@1",
    )

    with pytest.raises(IntegrityViolation, match="differs from its binding"):
        ports.load_verified(expectation=expectation, request=request)
    assert tuple(artifacts.read_calls) == reads_before


def test_ordering_policy_uses_non_case_id_field() -> None:
    registry, catalog, profile, artifacts, dataset_artifact, spec_artifact = _authority()
    dataset = BenchmarkDatasetV1.model_validate_json(artifacts.blobs[dataset_artifact.artifact_id])
    original = dataset.partitions[0].cases[0]
    assert isinstance(original.executor, BenchmarkSeededDetectionExecutorV1)
    second = original.model_copy(
        update={
            "case_id": "case:a",
            "executor": original.executor.model_copy(update={"needs_navigation": True}),
        }
    )
    dataset = dataset.model_copy(
        update={
            "partitions": (
                BenchmarkDatasetPartitionV1(partition_id="seeded", cases=(original, second)),
            )
        }
    )
    dataset = BenchmarkDatasetV1.model_validate(dataset.model_dump(mode="json"))
    dataset_artifact = artifacts.put(
        kind="bench_dataset",
        schema="bench-dataset@1",
        payload=dataset.model_dump(mode="json"),
    )
    spec = BenchmarkSpecV1.model_validate_json(artifacts.blobs[spec_artifact.artifact_id])
    spec = spec.model_copy(
        update={
            "dataset": BenchmarkDatasetBindingV1(
                artifact_id=dataset_artifact.artifact_id,
                payload_hash=dataset_artifact.payload_hash,
            ),
            "ordering_policy": BenchmarkOrderingPolicyV1(
                policy_id="test.order.non-id",
                policy_version=1,
                keys=(
                    BenchmarkOrderKeyV1(
                        field_path="/executor/needs_navigation",
                        direction="ascending",
                        nulls="forbidden",
                    ),
                ),
            ),
            "partitions": (
                BenchmarkPartitionV1(
                    partition_id="seeded",
                    cases=(
                        BenchmarkCaseExecutionV1(
                            case_id=original.case_id, execution_mode="deterministic"
                        ),
                        BenchmarkCaseExecutionV1(
                            case_id=second.case_id, execution_mode="deterministic"
                        ),
                    ),
                ),
            ),
        }
    )
    spec = BenchmarkSpecV1.model_validate(spec.model_dump(mode="json"))
    spec_artifact = artifacts.put(
        kind="benchmark_spec",
        schema="benchmark-spec@1",
        payload=spec.model_dump(mode="json"),
        lineage=(dataset_artifact.artifact_id,),
    )
    ports = WorkerBenchPorts(registry=registry, artifacts=artifacts)

    cases = ports.load_cases(
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        partition_ids=("seeded",),
        root_seed=None,
        seed_derivation_version="subseed@1",
        evaluator_profile=profile,
        execution_profile_catalog_version=catalog.catalog_version,
        execution_profile_catalog_digest=catalog.catalog_digest,
        repetition_count=1,
        execution_scope="execute_cases",
    )

    assert [case.case_id for case in cases] == ["case:seeded", "case:a"]


def test_dataset_rejects_two_power_drivers_for_one_defect_class() -> None:
    _, _, _, artifacts, dataset_artifact, _ = _authority()
    dataset = BenchmarkDatasetV1.model_validate_json(artifacts.blobs[dataset_artifact.artifact_id])
    duplicate = BenchmarkBinaryMetricDefinitionV1(
        metric=BenchmarkMetricRefV1(metric_id="other-power", metric_version=1),
        target=BenchmarkBinaryMetricTargetV1(
            collection="external.development",
            name="external_bdr",
            defect_class="dangling_reference",
            bucket="external_development",
        ),
        result_pointer="/metrics/detected",
        positive_value=True,
        updates_power=True,
    )

    with pytest.raises(ValidationError, match="multiple power-driving"):
        BenchmarkDatasetV1(
            partitions=dataset.partitions,
            binary_metrics=(*dataset.binary_metrics, duplicate),
            report_template_utf8=dataset.report_template_utf8,
            report_template_sha256=dataset.report_template_sha256,
        )
