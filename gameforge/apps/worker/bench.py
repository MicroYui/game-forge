"""Production GameForge-Bench ports for ``bench_runner@1``.

The dataset is executable authority, not an answer sheet: deterministic cases
re-run the M3 checker/simulation pipeline, Agent cases use the handler's fenced
model bridge and a deterministic response oracle, and aggregate cases consume
only spec-bound content-addressed Artifacts.
"""

from __future__ import annotations

import hmac
import json
import math
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Mapping, Protocol

from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.bench.inject import GroundTruth
from gameforge.bench.metrics import detects
from gameforge.bench.report_contracts import (
    BenchMeta,
    BenchReport,
    BinaryMetric,
    PowerMetric,
    canonical_report_bytes,
)
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.benchmark import (
    MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL,
    MAX_BENCHMARK_CASE_EXECUTIONS,
    MAX_BENCHMARK_CHECKER_WORK_UNITS,
    MAX_BENCHMARK_REPORT_BYTES,
    MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL,
    MAX_BENCHMARK_SIMULATION_WORK_UNITS,
    BenchmarkAgentResponseExecutorV1,
    BenchmarkCleanOracleFpExecutorV1,
    BenchmarkDatasetCaseV1,
    BenchmarkDatasetV1,
    BenchmarkEvaluatorProfileConfigV1,
    BenchmarkJsonPredicateV1,
    BenchmarkOrderKeyV1,
    BenchmarkSeededDetectionExecutorV1,
    BenchmarkSpecV1,
    sampled_partition_cases,
)
from gameforge.contracts.canonical import typed_canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.lineage import ArtifactV2
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.publication.payload_schema import (
    MAX_PREPARED_ARTIFACT_BYTES,
    decode_and_validate_artifact_payload,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.run_handlers.bench import (
    AgentCaseInvoker,
    BenchAggregateCaseResultV1,
    BenchAggregateInputExpectationV1,
    BenchAggregateInputIdentityV1,
    BenchCaseEvaluationRequestV1,
    BenchCaseResultV1,
    BenchCaseSpecV1,
    BenchCaseVerdictV1,
    BenchVerifiedAggregateInputV1,
)
from gameforge.platform.run_handlers.checker import validate_checker_work_budget
from gameforge.platform.run_handlers.simulation import (
    validate_economy_simulation_work_budget,
)
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings


_MISSING = object()
_BENCH_RUN_KIND = RunKindRef(kind="bench.run", version=1)


class BenchArtifactReader(Protocol):
    def load_artifact(self, artifact_id: str) -> ArtifactV2: ...

    def read_bytes_bounded(self, artifact_id: str, *, max_bytes: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _BenchAuthority:
    dataset: BenchmarkDatasetV1
    spec: BenchmarkSpecV1
    report_template: BenchReport
    evaluator_profile_stochastic: bool


@dataclass(frozen=True, slots=True)
class _ResultProjection:
    case_id: str
    partition_id: str
    replication_index: int
    status: str
    metrics: Mapping[str, object]


def _resolve_pointer(document: object, pointer: str) -> object:
    if pointer == "":
        return document
    current = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, (tuple, list)):
            if not token.isdecimal() or str(int(token)) != token:
                return _MISSING
            index = int(token)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _evaluate_predicate(predicate: BenchmarkJsonPredicateV1, actual: object) -> bool:
    value = _resolve_pointer(actual, predicate.actual_pointer)
    operator = predicate.operator
    if operator == "exists":
        return value is not _MISSING
    if operator == "not_exists":
        return value is _MISSING
    if value is _MISSING:
        return False
    if operator == "equals":
        return typed_canonical_json(value) == typed_canonical_json(predicate.expected)
    if operator == "not_equals":
        return typed_canonical_json(value) != typed_canonical_json(predicate.expected)
    if operator in {"contains", "not_contains"}:
        expected = predicate.expected
        contains = False
        if isinstance(value, str) and isinstance(expected, str):
            contains = expected in value
        elif isinstance(value, (tuple, list)):
            encoded_expected = typed_canonical_json(expected)
            contains = any(typed_canonical_json(item) == encoded_expected for item in value)
        elif isinstance(value, Mapping) and isinstance(expected, str):
            contains = expected in value
        return contains if operator == "contains" else not contains
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
    ):
        return False
    expected_number = predicate.expected
    if operator == "less_than":
        return value < expected_number
    if operator == "less_or_equal":
        return value <= expected_number
    if operator == "greater_than":
        return value > expected_number
    if operator == "greater_or_equal":
        return value >= expected_number
    raise IntegrityViolation("benchmark predicate uses an unknown operator")


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key is forbidden: {key}")
        value[key] = item
    return value


class WorkerBenchPorts:
    """One immutable production adapter implementing all four Bench ports."""

    def __init__(
        self,
        *,
        registry: ImmutablePlatformRegistry,
        artifacts: BenchArtifactReader,
    ) -> None:
        self._registry = registry
        self._artifacts = artifacts

    # ------------------------------------------------------------------ load
    def load_cases(
        self,
        *,
        dataset_artifact_id: str,
        benchmark_spec_artifact_id: str,
        partition_ids: tuple[str, ...],
        root_seed: int | None,
        seed_derivation_version: str,
        evaluator_profile: ProfileRefV1,
        execution_profile_catalog_version: int,
        execution_profile_catalog_digest: str,
        repetition_count: int,
        execution_scope: str,
    ) -> tuple[BenchCaseSpecV1, ...]:
        authority = self._load_authority(
            dataset_artifact_id=dataset_artifact_id,
            benchmark_spec_artifact_id=benchmark_spec_artifact_id,
            evaluator_profile=evaluator_profile,
            catalog_version=execution_profile_catalog_version,
            catalog_digest=execution_profile_catalog_digest,
        )
        if seed_derivation_version != authority.spec.sampling_policy.seed_derivation_version:
            raise IntegrityViolation("benchmark handler and spec seed derivation differ")
        if not (
            authority.spec.sampling_policy.minimum_repetitions
            <= repetition_count
            <= authority.spec.sampling_policy.maximum_repetitions
        ):
            raise IntegrityViolation("benchmark repetitions differ from the sampling policy")
        cases = self._ordered_cases(
            authority,
            partition_ids=partition_ids,
            root_seed=root_seed,
        )
        if len(cases) * repetition_count > authority.spec.resource_limits.max_case_executions:
            raise IntegrityViolation("benchmark case replications exceed the spec resource limit")
        total_checker_work_units = 0
        total_simulation_work_units = 0
        total_agent_model_calls = 0
        for _, spec_case in cases:
            dataset_case = self._dataset_case(authority.dataset, spec_case.case_id)
            if isinstance(dataset_case.executor, BenchmarkAgentResponseExecutorV1):
                if execution_scope == "execute_cases":
                    total_agent_model_calls += len(dataset_case.executor.prompts) * repetition_count
                    if (
                        total_agent_model_calls
                        > authority.spec.resource_limits.max_agent_model_calls_total
                    ):
                        raise IntegrityViolation(
                            "benchmark Agent calls exceed the spec Run-total limit"
                        )
                continue
            executor = dataset_case.executor
            snapshot = None
            if executor.constraints:
                if (
                    executor.max_checker_work_units
                    > authority.spec.resource_limits.max_checker_work_units_total
                ):
                    raise IntegrityViolation(
                        "benchmark checker work limit exceeds the spec resource limit"
                    )
                try:
                    snapshot = snapshot_from_canonical_view(executor.snapshot_payload)
                    per_replication_checker_work = validate_checker_work_budget(
                        snapshot=snapshot,
                        execution_count=len(executor.constraints),
                        max_work_units=executor.max_checker_work_units,
                    )
                except (IntegrityViolation, TypeError, ValueError, KeyError) as exc:
                    raise IntegrityViolation(
                        "benchmark checker workload exceeds its frozen work budget"
                    ) from exc
                total_checker_work_units += per_replication_checker_work * repetition_count
                if (
                    total_checker_work_units
                    > authority.spec.resource_limits.max_checker_work_units_total
                ):
                    raise IntegrityViolation(
                        "benchmark checkers exceed the spec Run-total work limit"
                    )
            simulation = getattr(dataset_case.executor, "simulation", None)
            if simulation is None:
                if dataset_case.execution_mode == "deterministic" and repetition_count != 1:
                    raise IntegrityViolation(
                        "pure deterministic benchmark cases require one repetition"
                    )
                continue
            if (
                simulation.max_work_units
                > authority.spec.resource_limits.max_simulation_work_units_total
            ):
                raise IntegrityViolation(
                    "benchmark simulation work limit exceeds the spec resource limit"
                )
            try:
                if snapshot is None:
                    snapshot = snapshot_from_canonical_view(executor.snapshot_payload)
                model = EconomyModel.from_snapshot(snapshot)
                per_replication_work = validate_economy_simulation_work_budget(
                    model,
                    n_agents=simulation.agents,
                    n_ticks=simulation.ticks,
                    replication_count=1,
                    max_work_units=simulation.max_work_units,
                )
            except (TypeError, ValueError, KeyError) as exc:
                raise IntegrityViolation(
                    "benchmark simulation workload authority is invalid"
                ) from exc
            total_simulation_work_units += per_replication_work * repetition_count
            if (
                total_simulation_work_units
                > authority.spec.resource_limits.max_simulation_work_units_total
            ):
                raise IntegrityViolation(
                    "benchmark simulations exceed the spec Run-total work limit"
                )
            if simulation.seed_policy == "run_subseed":
                if not authority.evaluator_profile_stochastic or root_seed is None:
                    raise IntegrityViolation(
                        "run-subseed benchmark simulation requires a stochastic profile"
                    )
            elif (
                authority.evaluator_profile_stochastic
                or root_seed is not None
                or repetition_count != 1
            ):
                raise IntegrityViolation(
                    "fixed-seed benchmark simulation requires a deterministic one-shot Run"
                )

        aggregate_by_case: dict[str, tuple[BenchAggregateInputExpectationV1, ...]] = {}
        if execution_scope == "aggregate_results":
            if authority.spec.aggregate_repetition_count != repetition_count:
                raise IntegrityViolation(
                    "benchmark aggregate repetition count differs from the spec"
                )
            selected_ids = {case.case_id for _, case in cases}
            for case_id in selected_ids:
                dataset_case = self._dataset_case(authority.dataset, case_id)
                if dataset_case.aggregate_oracle is None:
                    raise IntegrityViolation("aggregate benchmark case has no frozen oracle")
                bindings = tuple(
                    item for item in authority.spec.aggregate_inputs if item.case_id == case_id
                )
                if tuple(item.replication_index for item in bindings) != tuple(
                    range(repetition_count)
                ):
                    raise IntegrityViolation(
                        "aggregate benchmark case lacks exact replication bindings"
                    )
                aggregate_by_case[case_id] = tuple(
                    BenchAggregateInputExpectationV1(
                        artifact_id=item.artifact_id,
                        payload_hash=item.payload_hash,
                        payload_size_bytes=item.payload_size_bytes,
                        artifact_kind=item.artifact_kind,
                        payload_schema_id=item.payload_schema_id,
                    )
                    for item in bindings
                )
        elif execution_scope != "execute_cases":
            raise IntegrityViolation("benchmark execution scope is unknown")

        resolved: list[BenchCaseSpecV1] = []
        for partition_id, spec_case in cases:
            dataset_case = self._dataset_case(authority.dataset, spec_case.case_id)
            prompt = (
                dataset_case.executor.prompts[0]
                if isinstance(dataset_case.executor, BenchmarkAgentResponseExecutorV1)
                else ""
            )
            resolved.append(
                BenchCaseSpecV1(
                    case_id=dataset_case.case_id,
                    partition_id=partition_id,
                    mode=dataset_case.execution_mode,
                    prompt=prompt,
                    payload=dataset_case.model_dump(mode="json"),
                    aggregate_inputs=aggregate_by_case.get(dataset_case.case_id, ()),
                    result_metrics_bytes_total_limit=(
                        authority.spec.resource_limits.max_result_metrics_bytes_total
                    ),
                    agent_prompt_count=(
                        len(dataset_case.executor.prompts)
                        if isinstance(
                            dataset_case.executor,
                            BenchmarkAgentResponseExecutorV1,
                        )
                        else 0
                    ),
                    agent_model_calls_total_limit=(
                        authority.spec.resource_limits.max_agent_model_calls_total
                    ),
                )
            )
        return tuple(resolved)

    def _load_authority(
        self,
        *,
        dataset_artifact_id: str,
        benchmark_spec_artifact_id: str,
        evaluator_profile: ProfileRefV1,
        catalog_version: int,
        catalog_digest: str,
    ) -> _BenchAuthority:
        dataset_artifact, dataset_payload = self._read_payload(
            dataset_artifact_id,
            artifact_kind="bench_dataset",
            payload_schema_id="bench-dataset@1",
        )
        spec_artifact, spec_payload = self._read_payload(
            benchmark_spec_artifact_id,
            artifact_kind="benchmark_spec",
            payload_schema_id="benchmark-spec@1",
        )
        try:
            dataset = BenchmarkDatasetV1.model_validate(dataset_payload)
            spec = BenchmarkSpecV1.model_validate(spec_payload)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("benchmark dataset/spec authority is invalid") from exc
        if (
            spec.dataset.artifact_id != dataset_artifact.artifact_id
            or not hmac.compare_digest(spec.dataset.payload_hash, dataset_artifact.payload_hash)
            or spec.dataset.payload_schema_id != "bench-dataset@1"
            or set(spec_artifact.lineage) != {dataset_artifact.artifact_id}
        ):
            raise IntegrityViolation("benchmark spec does not bind the exact dataset")
        if spec.evaluator_profile != evaluator_profile:
            raise IntegrityViolation("benchmark evaluator profile differs from the spec")
        if self._partition_shape(dataset) != self._partition_shape(spec):
            raise IntegrityViolation("benchmark dataset/spec case authority differs")
        dataset_metrics = tuple(item.metric for item in dataset.binary_metrics)
        if dataset_metrics != spec.metric_policy.metrics:
            raise IntegrityViolation("benchmark dataset/spec metric authority differs")

        catalog = self._registry.get_execution_profile_catalog(catalog_version, catalog_digest)
        if catalog is None:
            raise IntegrityViolation("benchmark execution-profile catalog is unavailable")
        definition = next(
            (item for item in catalog.definitions if item.profile == evaluator_profile),
            None,
        )
        if definition is None or definition.profile_kind != "bench_evaluator":
            raise IntegrityViolation("benchmark evaluator profile is unavailable")
        try:
            config = BenchmarkEvaluatorProfileConfigV1.model_validate(definition.config)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("benchmark evaluator profile config is invalid") from exc
        if config.policy.ref != spec.evaluator_policy:
            raise IntegrityViolation("benchmark evaluator policy differs from the spec")
        if (
            spec.resource_limits.max_case_executions > config.policy.max_case_executions
            or spec.resource_limits.max_case_executions > MAX_BENCHMARK_CASE_EXECUTIONS
            or spec.resource_limits.max_prepared_report_bytes
            > config.policy.max_prepared_report_bytes
            or spec.resource_limits.max_prepared_report_bytes > MAX_BENCHMARK_REPORT_BYTES
            or spec.resource_limits.max_aggregate_input_bytes_per_artifact
            > config.policy.max_aggregate_input_bytes_per_artifact
            or spec.resource_limits.max_aggregate_input_bytes_total
            > config.policy.max_aggregate_input_bytes_total
            or spec.resource_limits.max_checker_work_units_total
            > config.policy.max_checker_work_units_total
            or spec.resource_limits.max_checker_work_units_total > MAX_BENCHMARK_CHECKER_WORK_UNITS
            or spec.resource_limits.max_simulation_work_units_total
            > config.policy.max_simulation_work_units_total
            or spec.resource_limits.max_simulation_work_units_total
            > MAX_BENCHMARK_SIMULATION_WORK_UNITS
            or spec.resource_limits.max_result_metrics_bytes_total
            > config.policy.max_result_metrics_bytes_total
            or spec.resource_limits.max_result_metrics_bytes_total
            > MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL
            or spec.resource_limits.max_agent_model_calls_total
            > config.policy.max_agent_model_calls_total
            or spec.resource_limits.max_agent_model_calls_total
            > MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL
        ):
            raise IntegrityViolation("benchmark spec resource limits exceed evaluator policy")

        template_bytes = dataset.report_template_utf8.encode("utf-8")
        if len(template_bytes) > spec.resource_limits.max_prepared_report_bytes:
            raise IntegrityViolation("benchmark report template exceeds the spec byte limit")
        try:
            template_raw = json.loads(template_bytes.decode("utf-8"))
            report_template = BenchReport.model_validate(template_raw)
        except (UnicodeError, TypeError, ValueError) as exc:
            raise IntegrityViolation("benchmark report template is invalid") from exc
        if canonical_report_bytes(report_template) != template_bytes:
            raise IntegrityViolation("benchmark report template is not canonical")
        for metric in dataset.binary_metrics:
            self._locate_metric(report_template, metric.target)
        metric_definitions = {
            (item.metric.metric_id, item.metric.metric_version): item
            for item in dataset.binary_metrics
        }
        for partition in dataset.partitions:
            for case in partition.cases:
                if not isinstance(case.executor, BenchmarkSeededDetectionExecutorV1):
                    continue
                expected_targets = tuple(
                    metric_definitions[(ref.metric_id, ref.metric_version)]
                    for ref in case.metric_refs
                    if metric_definitions[(ref.metric_id, ref.metric_version)].target.defect_class
                    == case.executor.defect_class
                    and metric_definitions[(ref.metric_id, ref.metric_version)].target.bucket
                    == case.executor.expected_finding_bucket
                )
                if len(expected_targets) != 1 or not expected_targets[0].updates_power:
                    raise IntegrityViolation(
                        "seeded benchmark case lacks one exact power-driving BDR metric"
                    )

        authority = _BenchAuthority(
            dataset=dataset,
            spec=spec,
            report_template=report_template,
            evaluator_profile_stochastic=definition.stochastic,
        )
        return authority

    def _read_payload(
        self,
        artifact_id: str,
        *,
        artifact_kind: str,
        payload_schema_id: str,
    ) -> tuple[ArtifactV2, Mapping[str, object]]:
        artifact = self._artifacts.load_artifact(artifact_id)
        if (
            artifact.artifact_id != artifact_id
            or artifact.kind != artifact_kind
            or artifact.meta.get("payload_schema_id") != payload_schema_id
        ):
            raise IntegrityViolation("benchmark Artifact envelope differs from its authority")
        blob = self._artifacts.read_bytes_bounded(
            artifact_id, max_bytes=MAX_PREPARED_ARTIFACT_BYTES
        )
        from hashlib import sha256

        if not hmac.compare_digest(sha256(blob).hexdigest(), artifact.payload_hash):
            raise IntegrityViolation("benchmark Artifact bytes differ from payload_hash")
        payload = decode_and_validate_artifact_payload(
            payload_schema_id=payload_schema_id,
            blob=blob,
        )
        return artifact, payload

    @staticmethod
    def _partition_shape(value: BenchmarkDatasetV1 | BenchmarkSpecV1) -> tuple[object, ...]:
        return tuple(
            (
                partition.partition_id,
                tuple((case.case_id, case.execution_mode) for case in partition.cases),
            )
            for partition in value.partitions
        )

    @staticmethod
    def _dataset_case(dataset: BenchmarkDatasetV1, case_id: str) -> BenchmarkDatasetCaseV1:
        for partition in dataset.partitions:
            for case in partition.cases:
                if case.case_id == case_id:
                    return case
        raise IntegrityViolation("benchmark sampled case is absent from the dataset")

    def _ordered_cases(
        self,
        authority: _BenchAuthority,
        *,
        partition_ids: tuple[str, ...],
        root_seed: int | None,
    ) -> tuple[tuple[str, object], ...]:
        sampled = sampled_partition_cases(
            authority.spec,
            partition_ids,
            root_seed=root_seed,
        )
        rows: list[tuple[str, object, Mapping[str, object]]] = []
        for partition_id, spec_case in sampled:
            case = self._dataset_case(authority.dataset, spec_case.case_id)
            view = {
                "partition_id": partition_id,
                **case.model_dump(mode="json"),
            }
            rows.append((partition_id, spec_case, view))

        keys = authority.spec.ordering_policy.keys
        resolved: list[tuple[object, ...]] = []
        for _, _, view in rows:
            values: list[object] = []
            for key in keys:
                value = _resolve_pointer(view, key.field_path)
                if value is _MISSING:
                    raise IntegrityViolation("benchmark ordering path is absent")
                if value is None and key.nulls == "forbidden":
                    raise IntegrityViolation("benchmark ordering path resolved to forbidden null")
                if value is not None and not self._order_scalar(value):
                    raise IntegrityViolation("benchmark ordering value is not a finite scalar")
                values.append(value)
            resolved.append(tuple(values))

        def compare(left_index: int, right_index: int) -> int:
            for ordinal, key in enumerate(keys):
                result = self._compare_order_values(
                    resolved[left_index][ordinal], resolved[right_index][ordinal], key
                )
                if result:
                    return result
            left_case = rows[left_index][1]
            right_case = rows[right_index][1]
            return (left_case.case_id > right_case.case_id) - (
                left_case.case_id < right_case.case_id
            )

        indices = sorted(range(len(rows)), key=cmp_to_key(compare))
        return tuple((rows[index][0], rows[index][1]) for index in indices)

    @staticmethod
    def _order_scalar(value: object) -> bool:
        if isinstance(value, (str, bool, int)):
            return True
        return isinstance(value, float) and math.isfinite(value)

    @staticmethod
    def _compare_order_values(left: object, right: object, key: BenchmarkOrderKeyV1) -> int:
        if left is None or right is None:
            if left is right:
                return 0
            null_first = key.nulls == "first"
            return -1 if (left is None) == null_first else 1
        left_numeric = isinstance(left, (int, float)) and not isinstance(left, bool)
        right_numeric = isinstance(right, (int, float)) and not isinstance(right, bool)
        if left_numeric and right_numeric:
            result = (left > right) - (left < right)
        elif type(left) is type(right) and isinstance(left, (str, bool)):
            result = (left > right) - (left < right)
        else:
            raise IntegrityViolation("benchmark ordering values have incompatible types")
        return result if key.direction == "ascending" else -result

    # --------------------------------------------------------------- evaluate
    def evaluate(
        self,
        request: BenchCaseEvaluationRequestV1,
        *,
        agent_invoker: AgentCaseInvoker | None,
    ) -> BenchCaseVerdictV1:
        try:
            case = BenchmarkDatasetCaseV1.model_validate(request.case.payload)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("benchmark case payload is invalid") from exc
        if case.case_id != request.case.case_id or case.execution_mode != request.case.mode:
            raise IntegrityViolation("benchmark case payload changed its exact identity")
        executor = case.executor
        if isinstance(executor, BenchmarkSeededDetectionExecutorV1):
            if agent_invoker is not None:
                raise IntegrityViolation("deterministic benchmark case received a model invoker")
            report = self._run_oracle_pipeline(executor, execution_seed=request.execution_seed)
            ground_truth = GroundTruth(
                defect_class=DefectClass(executor.defect_class),
                injected_entities=list(executor.injected_entities),
                note="production benchmark dataset authority",
            )
            detected = detects(report, ground_truth)
            taxonomy = {item.value for item in DefectClass}
            expected_entities = set(executor.injected_entities)
            findings = (
                report.deterministic_findings
                if executor.expected_finding_bucket == "deterministic"
                else report.simulation_findings
            )
            unexpected = any(
                finding.defect_class in taxonomy
                and not (
                    finding.defect_class == executor.defect_class
                    and bool(set(finding.entities) & expected_entities)
                )
                for finding in findings
            )
            passed = detected and not unexpected
            return BenchCaseVerdictV1(
                status="pass" if passed else "fail",
                metrics={"detected": detected, "unexpected_taxonomy_finding": unexpected},
            )
        if isinstance(executor, BenchmarkCleanOracleFpExecutorV1):
            if agent_invoker is not None:
                raise IntegrityViolation("deterministic benchmark case received a model invoker")
            report = self._run_oracle_pipeline(executor, execution_seed=request.execution_seed)
            by_bucket = {
                "deterministic": report.deterministic_findings,
                "simulation": report.simulation_findings,
                "unproven": report.unproven_findings,
            }
            false_positive = any(by_bucket[bucket] for bucket in executor.failure_buckets)
            return BenchCaseVerdictV1(
                status="fail" if false_positive else "pass",
                metrics={"false_positive": false_positive},
            )
        if not isinstance(executor, BenchmarkAgentResponseExecutorV1):
            raise IntegrityViolation("benchmark case executor is unsupported")
        if agent_invoker is None or request.agent_source_suffix is None:
            raise IntegrityViolation("Agent benchmark case has no fenced model invoker")
        response = ""
        for prompt in executor.prompts:
            response = agent_invoker(prompt, source_suffix=request.agent_source_suffix)
        if executor.response_format == "json":
            try:
                actual: object = json.loads(
                    response,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
                # ``json.loads`` accepts exponent overflow (for example ``1e400``)
                # as infinity.  Typed canonicalization closes that non-JSON value
                # and recursively verifies the entire response tree.
                typed_canonical_json(actual)
            except (TypeError, ValueError, OverflowError, RecursionError):
                return BenchCaseVerdictV1(
                    status="fail",
                    metrics={"response_valid": False, "oracle_passed": False},
                )
        else:
            actual = response
        try:
            passed = _evaluate_predicate(executor.oracle, actual)
        except (TypeError, ValueError, OverflowError, RecursionError):
            # A successfully completed model call whose response cannot satisfy
            # the frozen response contract is a measured case failure. Provider,
            # bridge, and cassette failures occur above and still fail the Run.
            return BenchCaseVerdictV1(
                status="fail",
                metrics={"response_valid": False, "oracle_passed": False},
            )
        return BenchCaseVerdictV1(
            status="pass" if passed else "fail",
            metrics={"response_valid": True, "oracle_passed": passed},
        )

    @staticmethod
    def _run_oracle_pipeline(
        executor: BenchmarkSeededDetectionExecutorV1 | BenchmarkCleanOracleFpExecutorV1,
        *,
        execution_seed: int | None,
    ):
        snapshot = snapshot_from_canonical_view(executor.snapshot_payload)
        if not hmac.compare_digest(snapshot.snapshot_id, executor.snapshot_id):
            raise IntegrityViolation("benchmark snapshot id differs from its exact payload")
        validate_checker_work_budget(
            snapshot=snapshot,
            execution_count=len(executor.constraints),
            max_work_units=executor.max_checker_work_units,
        )
        checkers = compile_all(list(executor.constraints))
        sim_findings = ()
        if executor.simulation is not None:
            simulation_config = executor.simulation
            if simulation_config.seed_policy == "run_subseed":
                if execution_seed is None:
                    raise IntegrityViolation(
                        "run-subseed benchmark simulation lacks its execution seed"
                    )
                simulation_seed = execution_seed
            else:
                assert simulation_config.fixed_seed is not None
                if execution_seed is not None:
                    raise IntegrityViolation("fixed benchmark simulation received a Run child seed")
                simulation_seed = simulation_config.fixed_seed
            model = EconomyModel.from_snapshot(snapshot)
            validate_economy_simulation_work_budget(
                model,
                n_agents=simulation_config.agents,
                n_ticks=simulation_config.ticks,
                replication_count=1,
                max_work_units=simulation_config.max_work_units,
            )
            simulation = EconomySimulator().run(
                model,
                seed=simulation_seed,
                n_agents=simulation_config.agents,
                n_ticks=simulation_config.ticks,
            )
            sim_findings = tuple(to_findings(simulation, snapshot.snapshot_id, model=model))
        nav = None
        if executor.needs_navigation:
            try:
                nav = AureusEnv(snapshot_to_world(snapshot)).nav_provider()
            except (TypeError, ValueError, KeyError) as exc:
                raise IntegrityViolation(
                    "navigation-required benchmark case lacks real Aureus authority"
                ) from exc
        return build_review_report(snapshot, checkers, sim_findings=sim_findings, nav=nav)

    # ------------------------------------------------------------- aggregate
    def load_verified(
        self,
        *,
        expectation: BenchAggregateInputExpectationV1,
        request: BenchCaseEvaluationRequestV1,
    ) -> BenchVerifiedAggregateInputV1:
        artifact = self._artifacts.load_artifact(expectation.artifact_id)
        from hashlib import sha256

        if (
            artifact.artifact_id != expectation.artifact_id
            or artifact.kind != expectation.artifact_kind
            or artifact.meta.get("payload_schema_id") != expectation.payload_schema_id
            or not hmac.compare_digest(artifact.payload_hash, expectation.payload_hash)
            or artifact.object_ref.size_bytes != expectation.payload_size_bytes
        ):
            raise IntegrityViolation("aggregate benchmark Artifact differs from its binding")
        blob = self._artifacts.read_bytes_bounded(
            expectation.artifact_id,
            max_bytes=expectation.payload_size_bytes,
        )
        if not hmac.compare_digest(sha256(blob).hexdigest(), expectation.payload_hash):
            raise IntegrityViolation("aggregate benchmark bytes differ from their binding")
        # Validate through the owning payload contract; never accept arbitrary JSON
        # merely because the spec carries its hash.
        decode_and_validate_artifact_payload(
            payload_schema_id=expectation.payload_schema_id,
            blob=blob,
        )
        return BenchVerifiedAggregateInputV1(
            identity=BenchAggregateInputIdentityV1(
                artifact_id=artifact.artifact_id,
                case_id=request.case.case_id,
                partition_id=request.case.partition_id,
                mode=request.case.mode,
                dataset_artifact_id=request.dataset_artifact_id,
                benchmark_spec_artifact_id=request.benchmark_spec_artifact_id,
                evaluator_profile=request.evaluator_profile,
                run_kind=request.run_kind,
                root_seed=request.root_seed,
                replication_index=request.replication_index,
                execution_seed=request.execution_seed,
                seed_derivation_version=request.seed_derivation_version,
            ),
            blob=blob,
        )

    # --------------------------------------------------------------- compose
    def compose_execute(
        self,
        *,
        dataset_artifact_id: str,
        benchmark_spec_artifact_id: str,
        partition_ids: tuple[str, ...],
        case_results: tuple[BenchCaseResultV1, ...],
        seed: int | None,
        evaluator_profile: ProfileRefV1,
        execution_profile_catalog_version: int,
        execution_profile_catalog_digest: str,
    ) -> bytes:
        authority = self._load_authority(
            dataset_artifact_id=dataset_artifact_id,
            benchmark_spec_artifact_id=benchmark_spec_artifact_id,
            evaluator_profile=evaluator_profile,
            catalog_version=execution_profile_catalog_version,
            catalog_digest=execution_profile_catalog_digest,
        )
        projections = tuple(
            _ResultProjection(
                case_id=item.case_id,
                partition_id=item.partition_id,
                replication_index=item.replication_index,
                status=item.status,
                metrics=item.metrics,
            )
            for item in case_results
        )
        for item in case_results:
            if (
                item.dataset_artifact_id != dataset_artifact_id
                or item.benchmark_spec_artifact_id != benchmark_spec_artifact_id
                or item.evaluator_profile != evaluator_profile
                or item.run_kind != _BENCH_RUN_KIND
                or item.root_seed != seed
            ):
                raise IntegrityViolation("benchmark case result changed its Run authority")
        return self._compose(
            authority,
            dataset_artifact_id=dataset_artifact_id,
            benchmark_spec_artifact_id=benchmark_spec_artifact_id,
            partition_ids=partition_ids,
            results=projections,
            seed=seed,
        )

    def compose_aggregate(
        self,
        *,
        dataset_artifact_id: str,
        benchmark_spec_artifact_id: str,
        partition_ids: tuple[str, ...],
        case_result_blobs: tuple[BenchAggregateCaseResultV1, ...],
        seed: int | None,
        evaluator_profile: ProfileRefV1,
        execution_profile_catalog_version: int,
        execution_profile_catalog_digest: str,
    ) -> bytes:
        authority = self._load_authority(
            dataset_artifact_id=dataset_artifact_id,
            benchmark_spec_artifact_id=benchmark_spec_artifact_id,
            evaluator_profile=evaluator_profile,
            catalog_version=execution_profile_catalog_version,
            catalog_digest=execution_profile_catalog_digest,
        )
        results: list[_ResultProjection] = []
        for item in case_result_blobs:
            identity = item.identity
            if (
                identity.dataset_artifact_id != dataset_artifact_id
                or identity.benchmark_spec_artifact_id != benchmark_spec_artifact_id
                or identity.evaluator_profile != evaluator_profile
                or identity.run_kind != _BENCH_RUN_KIND
                or identity.root_seed != seed
            ):
                raise IntegrityViolation("aggregate benchmark result changed its Run authority")
            case = self._dataset_case(authority.dataset, identity.case_id)
            if case.aggregate_oracle is None:
                raise IntegrityViolation("aggregate benchmark case has no frozen oracle")
            expectation = next(
                (
                    binding
                    for binding in authority.spec.aggregate_inputs
                    if binding.case_id == identity.case_id
                    and binding.replication_index == identity.replication_index
                ),
                None,
            )
            if expectation is None or expectation.artifact_id != identity.artifact_id:
                raise IntegrityViolation("aggregate benchmark input is absent from the spec")
            payload = decode_and_validate_artifact_payload(
                payload_schema_id=expectation.payload_schema_id,
                blob=item.blob,
            )
            passed = _evaluate_predicate(case.aggregate_oracle, payload)
            results.append(
                _ResultProjection(
                    case_id=identity.case_id,
                    partition_id=identity.partition_id,
                    replication_index=identity.replication_index,
                    status="pass" if passed else "fail",
                    metrics={"oracle_passed": passed},
                )
            )
        return self._compose(
            authority,
            dataset_artifact_id=dataset_artifact_id,
            benchmark_spec_artifact_id=benchmark_spec_artifact_id,
            partition_ids=partition_ids,
            results=tuple(results),
            seed=seed,
        )

    def _compose(
        self,
        authority: _BenchAuthority,
        *,
        dataset_artifact_id: str,
        benchmark_spec_artifact_id: str,
        partition_ids: tuple[str, ...],
        results: tuple[_ResultProjection, ...],
        seed: int | None,
    ) -> bytes:
        if not results:
            raise IntegrityViolation("benchmark report cannot be composed without results")
        for result in results:
            if result.status not in {"pass", "fail"}:
                raise IntegrityViolation("benchmark evaluator returned a non-binary status")
            if not isinstance(result.metrics, Mapping):
                raise IntegrityViolation("benchmark evaluator returned invalid metric signals")
        repetitions = max(item.replication_index for item in results) + 1
        ordered_cases = self._ordered_cases(authority, partition_ids=partition_ids, root_seed=seed)
        expected = tuple(
            (spec_case.case_id, partition_id, replication_index)
            for partition_id, spec_case in ordered_cases
            for replication_index in range(repetitions)
        )
        actual = tuple(
            (item.case_id, item.partition_id, item.replication_index) for item in results
        )
        if actual != expected:
            raise IntegrityViolation("benchmark results differ from exact sampled order")

        dataset_cases = {
            case.case_id: case
            for partition in authority.dataset.partitions
            for case in partition.cases
        }
        if repetitions != 1 and any(
            dataset_cases[result.case_id].execution_mode == "deterministic"
            and getattr(dataset_cases[result.case_id].executor, "simulation", None) is None
            for result in results
        ):
            raise IntegrityViolation(
                "pure deterministic benchmark results cannot be counted as replications"
            )
        by_metric: dict[tuple[str, int], list[_ResultProjection]] = {}
        for result in results:
            case = dataset_cases[result.case_id]
            for ref in case.metric_refs:
                by_metric.setdefault((ref.metric_id, ref.metric_version), []).append(result)

        report = authority.report_template
        metric_definitions = {
            (item.metric.metric_id, item.metric.metric_version): item
            for item in authority.dataset.binary_metrics
        }
        for ref, metric_results in sorted(by_metric.items()):
            definition = metric_definitions.get(ref)
            if definition is None:
                raise IntegrityViolation("benchmark result references an unknown metric")
            old = self._locate_metric(report, definition.target)
            k = 0
            for item in metric_results:
                signal = _resolve_pointer(
                    {"status": item.status, "metrics": dict(item.metrics)},
                    definition.result_pointer,
                )
                if signal is _MISSING:
                    raise IntegrityViolation("benchmark result lacks a metric definition signal")
                try:
                    positive = typed_canonical_json(signal) == typed_canonical_json(
                        definition.positive_value
                    )
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "benchmark result metric signal is not canonical JSON"
                    ) from exc
                k += int(positive)
            status = "measured"
            if definition.updates_power:
                if old.defect_class is None:
                    raise IntegrityViolation("power-driving metric lacks a defect class")
                power = next(item for item in report.power if item.defect_class == old.defect_class)
                provisional = BinaryMetric.wilson(
                    name=old.name,
                    defect_class=old.defect_class,
                    bucket=old.bucket,
                    planned_n=len(metric_results),
                    evaluated_n=len(metric_results),
                    k=k,
                    status="measured",
                    protocol_id="m4c-production-bench@1",
                    evidence_ref=None,
                )
                assert provisional.ci_low is not None and provisional.ci_high is not None
                half_width = (provisional.ci_high - provisional.ci_low) / 2.0
                status = "measured" if half_width <= power.target_half_width else "underpowered"
            metric = BinaryMetric.wilson(
                name=old.name,
                defect_class=old.defect_class,
                bucket=old.bucket,
                planned_n=len(metric_results),
                evaluated_n=len(metric_results),
                k=k,
                status=status,
                protocol_id="m4c-production-bench@1",
                evidence_ref=None,
            )
            report = self._replace_metric(report, definition.target, metric)
            if definition.updates_power:
                if metric.defect_class is None:
                    raise IntegrityViolation("power-driving metric lacks a defect class")
                assert metric.ci_low is not None and metric.ci_high is not None
                report = self._replace_power(
                    report,
                    defect_class=metric.defect_class,
                    evaluated_n=metric.evaluated_n,
                    half_width=(metric.ci_high - metric.ci_low) / 2.0,
                )

        report = report.model_copy(
            update={
                "meta": BenchMeta(
                    seed=seed,
                    corpus_size=len({item.case_id for item in results}),
                    report_builder_version="m4c-bench-composer@1",
                    generated_at=None,
                )
            }
        )
        try:
            report = BenchReport.model_validate(report.model_dump(mode="json"))
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("composed benchmark report violates BenchReport@2") from exc
        blob = canonical_report_bytes(report)
        if (
            len(blob) > authority.spec.resource_limits.max_prepared_report_bytes
            or len(blob) > MAX_BENCHMARK_REPORT_BYTES
        ):
            raise IntegrityViolation("composed benchmark report exceeds its byte limit")
        del dataset_artifact_id, benchmark_spec_artifact_id
        return blob

    @staticmethod
    def _metric_rows(report: BenchReport, collection: str) -> tuple[BinaryMetric, ...]:
        if collection == "seeded":
            return report.seeded
        if collection == "false_positives":
            return report.false_positives
        if collection == "agent":
            return report.agent
        if collection == "external.development":
            return report.external.development
        if collection == "external.verification":
            return report.external.verification
        if collection == "narrative.bdr":
            return report.narrative.bdr
        if collection == "hed.dispositions":
            return report.hed.dispositions
        raise IntegrityViolation("benchmark metric target collection is unknown")

    @classmethod
    def _locate_metric(cls, report: BenchReport, target) -> BinaryMetric:
        matches = tuple(
            item
            for item in cls._metric_rows(report, target.collection)
            if item.name == target.name
            and item.bucket == target.bucket
            and (item.defect_class.value if item.defect_class is not None else None)
            == target.defect_class
        )
        if len(matches) != 1:
            raise IntegrityViolation("benchmark metric target is absent or ambiguous")
        return matches[0]

    @classmethod
    def _replace_metric(cls, report: BenchReport, target, replacement: BinaryMetric) -> BenchReport:
        rows = cls._metric_rows(report, target.collection)
        old = cls._locate_metric(report, target)
        updated = tuple(replacement if item is old else item for item in rows)
        if target.collection == "seeded":
            return report.model_copy(update={"seeded": updated})
        if target.collection == "false_positives":
            return report.model_copy(update={"false_positives": updated})
        if target.collection == "agent":
            return report.model_copy(update={"agent": updated})
        if target.collection == "external.development":
            return report.model_copy(
                update={"external": report.external.model_copy(update={"development": updated})}
            )
        if target.collection == "external.verification":
            return report.model_copy(
                update={"external": report.external.model_copy(update={"verification": updated})}
            )
        if target.collection == "narrative.bdr":
            return report.model_copy(
                update={"narrative": report.narrative.model_copy(update={"bdr": updated})}
            )
        if target.collection == "hed.dispositions":
            return report.model_copy(
                update={"hed": report.hed.model_copy(update={"dispositions": updated})}
            )
        raise IntegrityViolation("benchmark metric target collection is unknown")

    @staticmethod
    def _replace_power(
        report: BenchReport,
        *,
        defect_class: DefectClass,
        evaluated_n: int,
        half_width: float,
    ) -> BenchReport:
        rows: list[PowerMetric] = []
        found = False
        for row in report.power:
            if row.defect_class != defect_class:
                rows.append(row)
                continue
            found = True
            rows.append(
                PowerMetric(
                    defect_class=row.defect_class,
                    bucket=row.bucket,
                    evaluated_n=evaluated_n,
                    achieved_half_width=half_width,
                    target_half_width=row.target_half_width,
                    status=("measured" if half_width <= row.target_half_width else "underpowered"),
                    evidence_ref=None,
                )
            )
        if not found:
            raise IntegrityViolation("benchmark report lacks a metric power row")
        return report.model_copy(update={"power": tuple(rows)})


def build_bench_ports(
    *,
    registry: ImmutablePlatformRegistry,
    artifacts: BenchArtifactReader,
) -> WorkerBenchPorts:
    return WorkerBenchPorts(registry=registry, artifacts=artifacts)


__all__ = ["WorkerBenchPorts", "build_bench_ports"]
