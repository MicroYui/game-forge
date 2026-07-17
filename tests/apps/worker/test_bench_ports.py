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
    BenchmarkAggregateInputBindingV1,
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
from gameforge.contracts.jobs import (
    CheckerRunPayloadV1,
    GraphSelectionV1,
    RunManifestVersionProjectionV1,
    RunResultSummaryV1,
    RunResultV1,
    SimulationRunPayloadV1,
    VersionTransitionPolicyRefV1,
    canonical_payload_hash,
    run_kind_definition_digest,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_key_for_sha256,
)
from gameforge.contracts.seeds import derive_subseed_v1
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.run_handlers.bench import (
    BENCH_SEED_DERIVATION_VERSION,
    BenchAggregateInputExpectationV1,
    BenchCaseEvaluationRequestV1,
    BenchCaseResultV1,
    BenchCaseSpecV1,
)
from gameforge.spine.ir.snapshot import Snapshot
from tests.platform.m4c.handler_support import build_envelope, build_run_record


class _Artifacts:
    def __init__(self) -> None:
        self.artifacts: dict[str, ArtifactV2] = {}
        self.blobs: dict[str, bytes] = {}
        self.runs = {}
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

    def load_run(self, run_id: str):
        return self.runs[run_id]


def _aggregate_expectation(
    *,
    registry,
    artifacts: _Artifacts,
    artifact: ArtifactV2,
    dataset_artifact_id: str,
    benchmark_spec_artifact_id: str,
    evaluator_profile: ProfileRefV1,
    payload_size_bytes: int | None = None,
    root_seed: int | None = None,
    producer_seed: int | None = None,
    dataset_case: BenchmarkDatasetCaseV1 | None = None,
) -> BenchAggregateInputExpectationV1:
    if dataset_case is None:
        dataset = BenchmarkDatasetV1.model_validate_json(artifacts.blobs[dataset_artifact_id])
        dataset_case = next(
            case
            for partition in dataset.partitions
            for case in partition.cases
            if case.case_id == "case:seeded"
        )
    if producer_seed is None:
        producer_kind = RunKindRef(kind="checker.run", version=1)
        producer_params = CheckerRunPayloadV1(
            snapshot_artifact_id="artifact:snapshot",
            selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
            checker_profile=ProfileRefV1(profile_id="builtin.checker", version=1),
            checker_ids=(),
            defect_classes=(),
        )
        profile_requirements = (
            (
                "/params/checker_profile",
                producer_params.checker_profile,
                "checker",
            ),
        )
        outcome_code = "checker_completed"
    else:
        producer_kind = RunKindRef(kind="simulation.run", version=1)
        producer_params = SimulationRunPayloadV1(
            snapshot_artifact_id="artifact:snapshot",
            constraint_snapshot_artifact_id=None,
            scenario_artifact_id=None,
            simulation_profile=ProfileRefV1(profile_id="builtin.simulation", version=1),
            workload_profile=ProfileRefV1(profile_id="builtin.workload", version=1),
            replication_count=1,
            horizon_steps=1,
        )
        profile_requirements = (
            (
                "/params/simulation_profile",
                producer_params.simulation_profile,
                "simulation",
            ),
            (
                "/params/workload_profile",
                producer_params.workload_profile,
                "workload",
            ),
        )
        outcome_code = "simulation_completed"
    definition = registry.get_run_kind(producer_kind)
    assert definition is not None
    catalog = max(
        registry.list_execution_profile_catalogs(),
        key=lambda item: item.catalog_version,
    )
    resolved_profiles = tuple(
        registry.resolve_execution_profile(
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
            field_path=field_path,
            profile=profile,
            expected_profile_kind=profile_kind,
        )
        for field_path, profile, profile_kind in profile_requirements
    )
    policy_bindings, schema_bindings = registry.resolve_required_run_bindings(
        definition=definition,
        resolved_profiles=resolved_profiles,
    )
    envelope = build_envelope(
        params=producer_params,
        resolved_profiles=resolved_profiles,
        seed=producer_seed,
    ).model_copy(
        update={
            "execution_profile_catalog_version": catalog.catalog_version,
            "execution_profile_catalog_digest": catalog.catalog_digest,
            "policy_bindings": policy_bindings,
            "schema_bindings": schema_bindings,
        }
    )
    producer_run_id = f"run:producer:{len(artifacts.runs) + 1}"
    run = build_run_record(envelope, producer_kind, run_id=producer_run_id).model_copy(
        update={"run_kind_definition_digest": run_kind_definition_digest(definition)}
    )
    projection = RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=1,
        run_kind=producer_kind,
        run_payload_hash=run.payload_hash,
        frozen_input_version_tuple=envelope.version_tuple,
        terminal_version_tuple=artifact.version_tuple,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="test-transition",
            policy_version=1,
            digest="a" * 64,
        ),
        parents=(),
    )
    result = RunResultV1(
        run_id=producer_run_id,
        attempt_no=1,
        run_kind=producer_kind,
        primary_artifact_id=artifact.artifact_id,
        produced_artifact_ids=(artifact.artifact_id,),
        finding_count=0,
        outcome_code=outcome_code,
        summary=RunResultSummaryV1(
            outcome_code=outcome_code,
            primary_artifact_kind=artifact.kind,
            produced_artifact_count=1,
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=projection,
    )
    result_artifact = artifacts.put(
        kind="run_result",
        schema="run-result@1",
        payload=result.model_dump(mode="json"),
        lineage=(artifact.artifact_id,),
    )
    artifacts.runs[producer_run_id] = run.model_copy(
        update={
            "status": "succeeded",
            "result_artifact_id": result_artifact.artifact_id,
            "concurrency_permit_group_id": None,
        }
    )
    binding = BenchmarkAggregateInputBindingV1(
        case_id="case:seeded",
        partition_id="seeded",
        execution_mode="deterministic",
        replication_index=0,
        artifact_id=artifact.artifact_id,
        payload_hash=artifact.payload_hash,
        payload_size_bytes=(
            artifact.object_ref.size_bytes if payload_size_bytes is None else payload_size_bytes
        ),
        artifact_kind=artifact.kind,
        payload_schema_id=str(artifact.meta["payload_schema_id"]),
        producer_run_id=producer_run_id,
        producer_run_kind=producer_kind,
        producer_run_payload_hash=run.payload_hash,
        producer_attempt_no=1,
        producer_result_artifact_id=result_artifact.artifact_id,
        producer_result_payload_hash=result_artifact.payload_hash,
        producer_seed_binding={
            "relation": "seed_independent" if producer_seed is None else "bench_child"
        },
        producer_root_seed=envelope.seed,
        producer_seed_derivation_version=definition.seed_derivation_version,
        producer_resolved_profiles=envelope.resolved_profiles,
        dataset_artifact_id=dataset_artifact_id,
        evaluator_profile=evaluator_profile,
        run_kind=RunKindRef(kind="bench.run", version=1),
        root_seed=root_seed,
        execution_seed=(
            None
            if root_seed is None
            else derive_subseed_v1(
                root_seed=root_seed,
                run_kind=RunKindRef(kind="bench.run", version=1),
                profile=evaluator_profile,
                case_id="case:seeded",
                replication_index=0,
            )
        ),
        seed_derivation_version="subseed@1",
    )
    return BenchAggregateInputExpectationV1(
        binding=binding,
        benchmark_spec_artifact_id=benchmark_spec_artifact_id,
        dataset_case=dataset_case,
    )


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


def test_fixed_benchmark_simulation_keeps_its_seed_with_a_bench_child_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gameforge.apps.worker.bench as bench_module

    snapshot = Snapshot(entities={}, relations={})
    executor = BenchmarkCleanOracleFpExecutorV1(
        snapshot_payload=snapshot.content_payload,
        snapshot_id=snapshot.snapshot_id,
        snapshot_payload_hash=sha256_lowerhex(canonical_json(snapshot.content_payload).encode()),
        constraints=(),
        constraints_digest=_digest_constraints(()),
        simulation=BenchmarkSimulationExecutionV1(
            seed_policy="fixed",
            fixed_seed=23,
            agents=1,
            ticks=1,
        ),
        failure_buckets=("simulation",),
    )
    seen_seeds: list[int] = []
    real_run = bench_module.EconomySimulator.run

    def capture_seed(self, *args, seed: int, **kwargs):
        seen_seeds.append(seed)
        return real_run(self, *args, seed=seed, **kwargs)

    monkeypatch.setattr(bench_module.EconomySimulator, "run", capture_seed)

    WorkerBenchPorts._run_oracle_pipeline(executor, execution_seed=999)

    assert seen_seeds == [23]


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
    assert cases[0].case_id == "case:seeded"
    result_artifact = artifacts.put(
        kind="checker_run",
        schema="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:test",
            "findings": [],
        },
    )
    expectation = _aggregate_expectation(
        registry=registry,
        artifacts=artifacts,
        artifact=result_artifact,
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        evaluator_profile=profile,
        payload_size_bytes=result_artifact.object_ref.size_bytes - 1,
    )
    reads_before = tuple(artifacts.read_calls)

    with pytest.raises(IntegrityViolation, match="differs from its binding"):
        ports.load_verified(expectation=expectation)
    assert tuple(artifacts.read_calls) == reads_before


def test_aggregate_reader_authenticates_complete_producer_run_result_authority() -> None:
    registry, _, profile, artifacts, dataset_artifact, spec_artifact = _authority()
    result_artifact = artifacts.put(
        kind="checker_run",
        schema="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:test",
            "findings": [],
        },
    )
    expectation = _aggregate_expectation(
        registry=registry,
        artifacts=artifacts,
        artifact=result_artifact,
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        evaluator_profile=profile,
    )

    verified = WorkerBenchPorts(registry=registry, artifacts=artifacts).load_verified(
        expectation=expectation
    )

    assert verified.identity.producer_run_id == expectation.binding.producer_run_id
    assert (
        verified.identity.producer_result_payload_hash
        == expectation.binding.producer_result_payload_hash
    )
    assert verified.blob == artifacts.blobs[result_artifact.artifact_id]


def test_aggregate_reader_rejects_forged_producer_run_payload_hash() -> None:
    registry, _, profile, artifacts, dataset_artifact, spec_artifact = _authority()
    result_artifact = artifacts.put(
        kind="checker_run",
        schema="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:test",
            "findings": [],
        },
    )
    expectation = _aggregate_expectation(
        registry=registry,
        artifacts=artifacts,
        artifact=result_artifact,
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        evaluator_profile=profile,
    )
    forged = BenchAggregateInputExpectationV1(
        binding=expectation.binding.model_copy(update={"producer_run_payload_hash": "f" * 64}),
        benchmark_spec_artifact_id=expectation.benchmark_spec_artifact_id,
        dataset_case=expectation.dataset_case,
    )

    with pytest.raises(IntegrityViolation, match="producer Run differs"):
        WorkerBenchPorts(registry=registry, artifacts=artifacts).load_verified(expectation=forged)


def test_seedless_aggregate_producer_requires_exact_deterministic_profile_authority() -> None:
    registry, _, profile, artifacts, dataset_artifact, spec_artifact = _authority()
    result_artifact = artifacts.put(
        kind="checker_run",
        schema="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:test",
            "findings": [],
        },
    )
    expectation = _aggregate_expectation(
        registry=registry,
        artifacts=artifacts,
        artifact=result_artifact,
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        evaluator_profile=profile,
    )
    producer_run_id = expectation.binding.producer_run_id
    run = artifacts.runs[producer_run_id]
    unbound_payload = run.payload.model_copy(update={"resolved_profiles": ()})
    unbound_payload_hash = canonical_payload_hash(unbound_payload)
    artifacts.runs[producer_run_id] = run.model_copy(
        update={
            "payload": unbound_payload,
            "payload_hash": unbound_payload_hash,
        }
    )
    forged = BenchAggregateInputExpectationV1(
        binding=expectation.binding.model_copy(
            update={
                "producer_run_payload_hash": unbound_payload_hash,
                "producer_resolved_profiles": (),
            }
        ),
        benchmark_spec_artifact_id=expectation.benchmark_spec_artifact_id,
        dataset_case=expectation.dataset_case,
    )

    with pytest.raises(IntegrityViolation, match="required profile binding"):
        WorkerBenchPorts(registry=registry, artifacts=artifacts).load_verified(expectation=forged)


def test_seeded_aggregate_reader_rejects_relabeling_one_producer_under_another_seed() -> None:
    registry, _, profile, artifacts, dataset_artifact, spec_artifact = _authority()
    original_execution_seed = derive_subseed_v1(
        root_seed=7,
        run_kind=RunKindRef(kind="bench.run", version=1),
        profile=profile,
        case_id="case:seeded",
        replication_index=0,
    )
    result_artifact = artifacts.put(
        kind="simulation_run",
        schema="simulation-result@1",
        payload={"payload_schema_version": "simulation-result@1"},
    )
    expectation = _aggregate_expectation(
        registry=registry,
        artifacts=artifacts,
        artifact=result_artifact,
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        evaluator_profile=profile,
        root_seed=7,
        producer_seed=original_execution_seed,
    )
    forged_execution_seed = derive_subseed_v1(
        root_seed=8,
        run_kind=RunKindRef(kind="bench.run", version=1),
        profile=profile,
        case_id="case:seeded",
        replication_index=0,
    )
    forged = BenchAggregateInputExpectationV1(
        binding=expectation.binding.model_copy(
            update={
                "root_seed": 8,
                "execution_seed": forged_execution_seed,
            }
        ),
        benchmark_spec_artifact_id=expectation.benchmark_spec_artifact_id,
        dataset_case=expectation.dataset_case,
    )

    with pytest.raises(IntegrityViolation, match="producer Run differs"):
        WorkerBenchPorts(registry=registry, artifacts=artifacts).load_verified(expectation=forged)


def test_aggregate_reader_rejects_result_manifest_without_bound_artifact_lineage() -> None:
    registry, _, profile, artifacts, dataset_artifact, spec_artifact = _authority()
    result_artifact = artifacts.put(
        kind="checker_run",
        schema="checker-report@1",
        payload={
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot:test",
            "findings": [],
        },
    )
    expectation = _aggregate_expectation(
        registry=registry,
        artifacts=artifacts,
        artifact=result_artifact,
        dataset_artifact_id=dataset_artifact.artifact_id,
        benchmark_spec_artifact_id=spec_artifact.artifact_id,
        evaluator_profile=profile,
    )
    manifest_id = expectation.binding.producer_result_artifact_id
    artifacts.artifacts[manifest_id] = artifacts.artifacts[manifest_id].model_copy(
        update={"lineage": ()}
    )

    with pytest.raises(IntegrityViolation, match="does not authorize"):
        WorkerBenchPorts(registry=registry, artifacts=artifacts).load_verified(
            expectation=expectation
        )


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
