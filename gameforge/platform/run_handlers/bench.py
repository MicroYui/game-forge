"""``bench_runner@1`` — the two-phase benchmark handler.

Unlike the other 11a handlers this is a genuine integration, not a thin wrap:
``gameforge/bench/run_bench.py::build_bench_report`` is a whole-corpus, repo-path
CLI that reads a fixed constellation of files and takes no artifact ids, so it
cannot be called from a fenced executor. The frozen ``bench.run`` payload instead
encodes a two-phase contract (``execution_scope``):

* ``execute_cases`` — run the selected partitions' cases and produce the primary
  ``bench_report[bench-report@2]``. Deterministic cases run the spine oracles;
  Agent cases go through a SINGLE ordered, run-scoped model-bridge adapter (one
  ordered cassette across every agent case). ``case_result_artifact_ids`` is
  forbidden here.
* ``aggregate_results`` — consume the ``case_result_artifact_ids`` produced by
  prior per-case evaluator runs and compose the aggregate ``bench_report``.
  ``case_result_artifact_ids`` is required here.

The ``bench.run`` finding-output policy is null, so this handler emits NO
``PreparedFinding``. ``outcome_code=bench_completed``, primary
``bench_report[bench-report@2]``.

Dependency direction forbids ``platform → gameforge.bench``, and ``bench-report@2``
is a whole-corpus aggregate root defined there. Case loading, case evaluation, and
report composition are therefore injected ports; the composition root binds the
composer to the real ``gameforge.bench`` composer (which may import it), while this
module stays lint-clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Literal, Mapping, Protocol

from pydantic import JsonValue, TypeAdapter, ValidationError

from gameforge.contracts.benchmark import (
    MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL,
    MAX_BENCHMARK_AGENT_TURNS,
    MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT,
    MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL,
    MAX_BENCHMARK_CASE_EXECUTIONS,
    MAX_BENCHMARK_REPORT_BYTES,
    MAX_BENCHMARK_RESULT_METRIC_FIELDS,
    MAX_BENCHMARK_RESULT_METRICS_BYTES,
    MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL,
    BenchmarkAggregateInputBindingV1,
    BenchmarkAggregateProducerSeedBindingV1,
    BenchmarkDatasetCaseV1,
    validate_benchmark_aggregate_producer_seed_authority,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.jobs import (
    MAX_BENCHMARK_AGGREGATE_RESULT_ARTIFACTS,
    BenchRunPayloadV1,
    PreparedRunOutcome,
)

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    ExecutorContextLike,
    PreparedArtifactStore,
    build_success_result,
    prepared_version_tuple,
    require_exact_profile_bindings,
    store_prepared_blob,
    trust_typed_profile_binding,
)
from gameforge.platform.run_handlers.model_routing import (
    ModelBridgeAgentAdapter,
    plan_node_snapshot,
    prompt_source_artifact_ids,
)
from gameforge.platform.run_handlers.validation_common import (
    VALIDATION_SEED_DERIVATION_VERSION,
    derive_validation_subseed,
)

BENCH_REPORT_SCHEMA_ID = "bench-report@2"
BENCH_TOOL_VERSION = "bench@1"
BENCH_AGENT_NODE_ID = "bench-agent-case"
BENCH_AGENT_PROMPT_VERSION = "bench-agent@1"
BENCH_SEED_DERIVATION_VERSION = VALIDATION_SEED_DERIVATION_VERSION

BenchCaseMode = Literal["deterministic", "agent"]


@dataclass(frozen=True, slots=True)
class BenchAggregateInputExpectationV1:
    """Spec-authoritative Artifact identity for one case replication."""

    binding: BenchmarkAggregateInputBindingV1
    benchmark_spec_artifact_id: str
    dataset_case: BenchmarkDatasetCaseV1

    @property
    def artifact_id(self) -> str:
        return self.binding.artifact_id

    @property
    def payload_hash(self) -> str:
        return self.binding.payload_hash

    @property
    def payload_size_bytes(self) -> int:
        return self.binding.payload_size_bytes

    @property
    def artifact_kind(self) -> str:
        return self.binding.artifact_kind

    @property
    def payload_schema_id(self) -> str:
        return self.binding.payload_schema_id


@dataclass(frozen=True, slots=True)
class BenchAggregateInputIdentityV1:
    """Identity recovered from immutable spec plus producer Run/RunResult authority."""

    artifact_id: str
    case_id: str
    partition_id: str
    mode: BenchCaseMode
    dataset_artifact_id: str
    benchmark_spec_artifact_id: str
    evaluator_profile: ProfileRefV1
    run_kind: RunKindRef
    root_seed: int | None
    replication_index: int
    execution_seed: int | None
    seed_derivation_version: str
    producer_run_id: str
    producer_run_kind: RunKindRef
    producer_run_payload_hash: str
    producer_attempt_no: int
    producer_result_artifact_id: str
    producer_result_payload_hash: str
    producer_seed_binding: BenchmarkAggregateProducerSeedBindingV1
    producer_root_seed: int | None
    producer_seed_derivation_version: str | None
    producer_resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...]


@dataclass(frozen=True, slots=True)
class BenchCaseSpecV1:
    """One resolved bench case selected from a dataset partition."""

    case_id: str
    partition_id: str
    mode: BenchCaseMode
    prompt: str = ""
    payload: Mapping[str, object] = field(default_factory=dict)
    aggregate_inputs: tuple[BenchAggregateInputExpectationV1, ...] = ()
    result_metrics_bytes_total_limit: int = MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL
    agent_prompt_count: int = 1
    agent_model_calls_total_limit: int = MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL


@dataclass(frozen=True, slots=True)
class BenchCaseEvaluationRequestV1:
    """Exact immutable input closure for one case replication.

    ``replication_index`` and both ordinals are zero-based.  ``execution_seed`` is
    the frozen ``subseed@1`` child seed when the Run has a stochastic root seed;
    deterministic evaluator profiles retain ``None`` rather than inventing seed 0.
    """

    case: BenchCaseSpecV1
    dataset_artifact_id: str
    benchmark_spec_artifact_id: str
    evaluator_profile: ProfileRefV1
    run_kind: RunKindRef
    root_seed: int | None
    replication_index: int
    execution_seed: int | None
    seed_derivation_version: str
    case_ordinal: int
    result_ordinal: int
    agent_source_suffix: str | None


@dataclass(frozen=True, slots=True)
class BenchCaseVerdictV1:
    """Evaluator-owned verdict only; execution identity is handler-owned."""

    status: str
    metrics: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BenchCaseResultV1:
    """One ordered result with its complete handler-authenticated identity."""

    case_id: str
    partition_id: str
    mode: BenchCaseMode
    dataset_artifact_id: str
    benchmark_spec_artifact_id: str
    evaluator_profile: ProfileRefV1
    run_kind: RunKindRef
    root_seed: int | None
    replication_index: int
    execution_seed: int | None
    seed_derivation_version: str
    case_ordinal: int
    result_ordinal: int
    agent_source_suffix: str | None
    model_call_ordinals: tuple[int, ...]
    status: str
    metrics: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BenchAggregateCaseResultV1:
    """One verified aggregate input placed in exact case/replication order."""

    identity: BenchAggregateInputIdentityV1
    case_ordinal: int
    result_ordinal: int
    blob: bytes


@dataclass(frozen=True, slots=True)
class BenchVerifiedAggregateInputV1:
    """A bounded blob authenticated by the production aggregate reader."""

    identity: BenchAggregateInputIdentityV1
    blob: bytes


class AgentCaseInvoker(Protocol):
    """Run one ordered agent turn and return the normalized model text."""

    def __call__(self, prompt: str, *, source_suffix: str) -> str: ...


class BenchCaseLoader(Protocol):
    """Resolve the exact spec-sampled and spec-ordered selected cases."""

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
        execution_scope: Literal["execute_cases", "aggregate_results"],
    ) -> tuple[BenchCaseSpecV1, ...]: ...


class BenchCaseEvaluator(Protocol):
    """Evaluate one case (deterministic via spine oracles; agent via the invoker)."""

    def evaluate(
        self,
        request: BenchCaseEvaluationRequestV1,
        *,
        agent_invoker: AgentCaseInvoker | None,
    ) -> BenchCaseVerdictV1: ...


class BenchAggregateInputVerifier(Protocol):
    """Authenticate and project one heterogeneous aggregate-result Artifact."""

    def load_verified(
        self,
        *,
        expectation: BenchAggregateInputExpectationV1,
    ) -> BenchVerifiedAggregateInputV1: ...


class BenchReportComposer(Protocol):
    """Compose the canonical ``bench-report@2`` bytes for either phase."""

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
    ) -> bytes: ...

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
    ) -> bytes: ...


class _BoundAgentInvoker:
    """Bind the run-scoped model adapter + planned model snapshot into an invoker.

    A single instance is shared across every agent case in a run, so all agent
    turns land on ONE ordered cassette (the adapter's monotonic call index).
    """

    def __init__(
        self,
        *,
        adapter: ModelBridgeAgentAdapter,
        model_snapshot: object,
        source_artifact_ids: tuple[str, ...],
        node_id: str,
        prompt_version: str,
    ) -> None:
        self.adapter = adapter
        self._model_snapshot = model_snapshot
        self._source_artifact_ids = source_artifact_ids
        self._node_id = node_id
        self._prompt_version = prompt_version
        self._last_call_ordinal: int | None = None
        self._call_ordinals: list[int] = []

    @property
    def call_count(self) -> int:
        return self.adapter.call_count

    @property
    def call_ordinals(self) -> tuple[int, ...]:
        return tuple(self._call_ordinals)

    def __call__(
        self,
        prompt: str,
        *,
        source_suffix: str,
        include_previous_consumption: bool,
    ) -> str:
        previous_call_ordinal = self._last_call_ordinal
        # The binding is deliberately opaque: it prevents dataset case labels from
        # becoming model-visible specialisation hints while making case/replication
        # identity part of the exact ModelRequest bytes and therefore its hash.
        bound_prompt = (
            f"<gameforge-bench-binding>{source_suffix}</gameforge-bench-binding>\n{prompt}"
        )
        result = self.adapter.call_model(
            agent_node_id=self._node_id,
            user_prompt=bound_prompt,
            prompt_version=self._prompt_version,
            model_snapshot=self._model_snapshot,
            source_artifact_ids=self._source_artifact_ids,
            context_kind="bench_agent_case",
            include_previous_consumption=include_previous_consumption,
        )
        call_ordinal = result.call_ordinal
        expected_call_ordinal = self.adapter.call_count
        if (
            isinstance(call_ordinal, bool)
            or not isinstance(call_ordinal, int)
            or call_ordinal < 1
            or call_ordinal != expected_call_ordinal
            or (previous_call_ordinal is not None and call_ordinal != previous_call_ordinal + 1)
        ):
            raise ValueError("Agent bench calls are not one ordered cassette sequence")
        self._last_call_ordinal = call_ordinal
        self._call_ordinals.append(call_ordinal)
        return result.response.response_normalized


class _ScopedAgentInvoker:
    """Fence every call from one replication to its exact case identity.

    Agent evaluators may be multi-step.  The frozen contract requires all calls to
    share one ordered run-scoped cassette; it does not reduce each case to a single
    model turn.
    """

    def __init__(
        self,
        *,
        delegate: _BoundAgentInvoker,
        expected_source_suffix: str,
    ) -> None:
        self._delegate = delegate
        self._expected_source_suffix = expected_source_suffix
        self.attempt_count = 0

    def __call__(self, prompt: str, *, source_suffix: str) -> str:
        self.attempt_count += 1
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("Agent bench evaluator emitted an empty model prompt")
        if source_suffix != self._expected_source_suffix:
            raise ValueError("Agent bench evaluator changed the exact case/replication identity")
        return self._delegate(
            prompt,
            source_suffix=source_suffix,
            include_previous_consumption=self.attempt_count > 1,
        )


@dataclass(frozen=True, slots=True)
class BenchRunHandler:
    """A ``RunExecutor`` producing the primary ``bench_report`` (no findings)."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    case_loader: BenchCaseLoader
    evaluator: BenchCaseEvaluator
    composer: BenchReportComposer
    aggregate_input_verifier: BenchAggregateInputVerifier | None = None
    agent_node_id: str = BENCH_AGENT_NODE_ID
    agent_prompt_version: str = BENCH_AGENT_PROMPT_VERSION
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, BenchRunPayloadV1):
            raise TypeError("bench_runner@1 requires a bench-run@1 payload")
        if context.run.kind != RunKindRef(kind="bench.run", version=1):
            raise TypeError("bench_runner@1 requires Run kind bench.run@1")
        require_exact_profile_bindings(
            context,
            expected={
                "/params/evaluator_profile": (payload.evaluator_profile, "bench_evaluator"),
            },
            validator=self.profile_binding_validator,
        )

        root_seed = context.payload.seed
        if payload.execution_scope == "execute_cases":
            report_bytes, lineage = self._execute_cases(context, payload, root_seed)
        else:
            if context.payload.llm_execution_mode != "not_applicable":
                raise ValueError("aggregate_results requires not_applicable model execution")
            report_bytes, lineage = self._aggregate_results(context, payload, root_seed)
        if not isinstance(report_bytes, bytes):
            raise TypeError("bench report composer must return exact bytes")
        if not report_bytes or len(report_bytes) > MAX_BENCHMARK_REPORT_BYTES:
            raise ValueError("bench report exceeds the frozen prepared-report byte limit")

        primary = store_prepared_blob(
            self.store,
            kind="bench_report",
            payload_schema_id=BENCH_REPORT_SCHEMA_ID,
            version_tuple=prepared_version_tuple(
                context,
                tool_version=BENCH_TOOL_VERSION,
                projected_fields=(
                    "doc_version",
                    "ir_snapshot_id",
                    "constraint_snapshot_id",
                    "env_contract_version",
                ),
                overrides={"seed": root_seed},
            ),
            lineage=lineage,
            blob=report_bytes,
            extra_meta={"execution_scope": payload.execution_scope},
        )
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code="bench_completed",
            primary_index=0,
            artifacts=(primary,),
            findings=(),
        )

    def _execute_cases(
        self,
        context: ExecutorContextLike,
        payload: BenchRunPayloadV1,
        root_seed: int | None,
    ) -> tuple[bytes, tuple[str, ...]]:
        if payload.case_result_artifact_ids:
            raise ValueError("execute_cases forbids precomputed case results")
        cases = self._load_cases(context, payload, root_seed=root_seed)
        self._enforce_case_execution_limit(cases, payload.repetition_count)
        invoker = self._agent_invoker(context)
        contains_agent_cases = any(case.mode == "agent" for case in cases)
        if contains_agent_cases != (invoker is not None):
            raise ValueError("bench case modes differ from the exact Run LLM execution mode")

        results: list[BenchCaseResultV1] = []
        total_metrics_bytes = 0
        total_metrics_bytes_limit = cases[0].result_metrics_bytes_total_limit
        agent_model_calls_total_limit = cases[0].agent_model_calls_total_limit
        planned_agent_model_calls = (
            sum(case.agent_prompt_count for case in cases if case.mode == "agent")
            * payload.repetition_count
        )
        if planned_agent_model_calls > agent_model_calls_total_limit:
            raise ValueError("bench Agent calls exceed the frozen Run-total limit")
        for request in self._evaluation_requests(
            context=context,
            payload=payload,
            cases=cases,
            root_seed=root_seed,
        ):
            result = self._evaluate_one(request=request, invoker=invoker)
            total_metrics_bytes += len(canonical_json(result.metrics).encode("utf-8"))
            if total_metrics_bytes > total_metrics_bytes_limit:
                raise ValueError("bench result metrics exceed the frozen Run-total bound")
            results.append(result)
        report_bytes = self.composer.compose_execute(
            dataset_artifact_id=payload.dataset_artifact_id,
            benchmark_spec_artifact_id=payload.benchmark_spec_artifact_id,
            partition_ids=payload.partition_ids,
            case_results=tuple(results),
            seed=root_seed,
            evaluator_profile=payload.evaluator_profile,
            execution_profile_catalog_version=(context.payload.execution_profile_catalog_version),
            execution_profile_catalog_digest=context.payload.execution_profile_catalog_digest,
        )
        lineage = (payload.dataset_artifact_id, payload.benchmark_spec_artifact_id)
        return report_bytes, lineage

    def _aggregate_results(
        self,
        context: ExecutorContextLike,
        payload: BenchRunPayloadV1,
        root_seed: int | None,
    ) -> tuple[bytes, tuple[str, ...]]:
        if not payload.case_result_artifact_ids:
            raise ValueError("aggregate_results requires case result artifacts")
        if len(payload.case_result_artifact_ids) > MAX_BENCHMARK_AGGREGATE_RESULT_ARTIFACTS:
            raise ValueError("aggregate results exceed the complete Run input closure")
        if self.aggregate_input_verifier is None:
            raise ValueError("bench aggregate input identity verifier is unavailable")

        cases = self._load_cases(context, payload, root_seed=root_seed)
        self._enforce_case_execution_limit(cases, payload.repetition_count)
        requests = self._evaluation_requests(
            context=context,
            payload=payload,
            cases=cases,
            root_seed=root_seed,
        )
        if len(payload.case_result_artifact_ids) != len(requests):
            raise ValueError("aggregate inputs differ from the exact case/replication set")

        expectations = tuple(self._aggregate_expectation(request) for request in requests)
        expected_artifact_ids = tuple(item.artifact_id for item in expectations)
        if len(set(expected_artifact_ids)) != len(expected_artifact_ids) or set(
            payload.case_result_artifact_ids
        ) != set(expected_artifact_ids):
            raise ValueError("aggregate inputs differ from the exact spec bindings")
        if any(
            item.payload_size_bytes > MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_PER_ARTIFACT
            for item in expectations
        ) or sum(item.payload_size_bytes for item in expectations) > (
            MAX_BENCHMARK_AGGREGATE_INPUT_BYTES_TOTAL
        ):
            raise ValueError("aggregate inputs exceed the frozen byte limits")

        case_result_blobs: list[BenchAggregateCaseResultV1] = []
        for request, expectation in zip(requests, expectations, strict=True):
            if self._expectation_request_identity_key(expectation) != self._request_identity_key(
                request
            ):
                raise ValueError("aggregate input provenance differs from the exact Bench Run")
            try:
                dataset_case = BenchmarkDatasetCaseV1.model_validate(request.case.payload)
                validate_benchmark_aggregate_producer_seed_authority(
                    expectation.binding,
                    dataset_case,
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise ValueError(
                    "aggregate producer seed relation differs from exact dataset authority"
                ) from exc
            if dataset_case != expectation.dataset_case:
                raise ValueError("aggregate dataset case differs from exact loader authority")
            verified = self.aggregate_input_verifier.load_verified(
                expectation=expectation,
            )
            if not isinstance(verified, BenchVerifiedAggregateInputV1):
                raise TypeError("bench aggregate verifier returned the wrong type")
            identity = verified.identity
            blob = verified.blob
            if not isinstance(identity, BenchAggregateInputIdentityV1):
                raise TypeError("bench aggregate identity verifier returned the wrong type")
            if not isinstance(blob, bytes) or len(blob) != expectation.payload_size_bytes:
                raise ValueError("bench aggregate verifier changed the exact payload size")
            if identity.artifact_id != expectation.artifact_id:
                raise ValueError("bench aggregate verifier changed the exact Artifact identity")
            self._validate_aggregate_identity(identity)
            if self._aggregate_identity_key(identity) != self._expectation_identity_key(
                expectation
            ):
                raise ValueError("aggregate input identity differs from its exact spec binding")
            case_result_blobs.append(
                BenchAggregateCaseResultV1(
                    identity=identity,
                    case_ordinal=request.case_ordinal,
                    result_ordinal=request.result_ordinal,
                    blob=blob,
                )
            )
        report_bytes = self.composer.compose_aggregate(
            dataset_artifact_id=payload.dataset_artifact_id,
            benchmark_spec_artifact_id=payload.benchmark_spec_artifact_id,
            partition_ids=payload.partition_ids,
            case_result_blobs=tuple(case_result_blobs),
            seed=root_seed,
            evaluator_profile=payload.evaluator_profile,
            execution_profile_catalog_version=(context.payload.execution_profile_catalog_version),
            execution_profile_catalog_digest=context.payload.execution_profile_catalog_digest,
        )
        lineage = (
            payload.dataset_artifact_id,
            payload.benchmark_spec_artifact_id,
            *payload.case_result_artifact_ids,
        )
        return report_bytes, lineage

    def _load_cases(
        self,
        context: ExecutorContextLike,
        payload: BenchRunPayloadV1,
        *,
        root_seed: int | None,
    ) -> tuple[BenchCaseSpecV1, ...]:
        cases = self.case_loader.load_cases(
            dataset_artifact_id=payload.dataset_artifact_id,
            benchmark_spec_artifact_id=payload.benchmark_spec_artifact_id,
            partition_ids=payload.partition_ids,
            root_seed=root_seed,
            seed_derivation_version=BENCH_SEED_DERIVATION_VERSION,
            evaluator_profile=payload.evaluator_profile,
            execution_profile_catalog_version=(context.payload.execution_profile_catalog_version),
            execution_profile_catalog_digest=context.payload.execution_profile_catalog_digest,
            repetition_count=payload.repetition_count,
            execution_scope=payload.execution_scope,
        )
        if not isinstance(cases, tuple):
            raise TypeError("bench case loader must return an immutable tuple")
        if not cases:
            raise ValueError("bench selection resolved to no cases")
        selected_partitions = set(payload.partition_ids)
        case_ids: set[str] = set()
        for case in cases:
            if not isinstance(case, BenchCaseSpecV1):
                raise TypeError("bench case loader returned the wrong type")
            if (
                not isinstance(case.case_id, str)
                or not isinstance(case.partition_id, str)
                or not case.case_id
                or not case.partition_id
            ):
                raise ValueError("bench case identity must be non-empty")
            if case.case_id in case_ids:
                raise ValueError("bench case loader returned duplicate case identities")
            case_ids.add(case.case_id)
            if selected_partitions and case.partition_id not in selected_partitions:
                raise ValueError("bench case loader returned an unselected partition")
            if case.mode not in {"deterministic", "agent"}:
                raise ValueError("bench case loader returned an unknown execution mode")
            if not isinstance(case.prompt, str) or not isinstance(case.payload, Mapping):
                raise TypeError("bench case loader returned an invalid case payload")
            if not isinstance(case.aggregate_inputs, tuple) or any(
                not isinstance(item, BenchAggregateInputExpectationV1)
                for item in case.aggregate_inputs
            ):
                raise TypeError("bench case loader returned invalid aggregate bindings")
            if (
                isinstance(case.result_metrics_bytes_total_limit, bool)
                or not isinstance(case.result_metrics_bytes_total_limit, int)
                or not 1
                <= case.result_metrics_bytes_total_limit
                <= MAX_BENCHMARK_RESULT_METRICS_BYTES_TOTAL
            ):
                raise ValueError("bench case loader returned an invalid metrics-total limit")
            if (
                isinstance(case.agent_model_calls_total_limit, bool)
                or not isinstance(case.agent_model_calls_total_limit, int)
                or not 1
                <= case.agent_model_calls_total_limit
                <= MAX_BENCHMARK_AGENT_MODEL_CALLS_TOTAL
            ):
                raise ValueError("bench case loader returned an invalid Agent-call limit")
            if (
                isinstance(case.agent_prompt_count, bool)
                or not isinstance(case.agent_prompt_count, int)
                or case.agent_prompt_count < 0
                or (case.mode == "agent" and case.agent_prompt_count < 1)
                or case.agent_prompt_count > MAX_BENCHMARK_AGENT_TURNS
            ):
                raise ValueError("bench case loader returned an invalid Agent prompt count")
            if case.mode == "agent" and not case.prompt:
                raise ValueError("Agent bench case requires a non-empty exact prompt")
        if len({case.result_metrics_bytes_total_limit for case in cases}) != 1:
            raise ValueError("bench cases disagree on the frozen metrics-total limit")
        if len({case.agent_model_calls_total_limit for case in cases}) != 1:
            raise ValueError("bench cases disagree on the frozen Agent-call limit")
        return cases

    @staticmethod
    def _enforce_case_execution_limit(
        cases: tuple[BenchCaseSpecV1, ...], repetition_count: int
    ) -> None:
        if len(cases) * repetition_count > MAX_BENCHMARK_CASE_EXECUTIONS:
            raise ValueError("bench case replications exceed the frozen execution limit")

    @staticmethod
    def _aggregate_expectation(
        request: BenchCaseEvaluationRequestV1,
    ) -> BenchAggregateInputExpectationV1:
        bindings = request.case.aggregate_inputs
        if len(bindings) <= request.replication_index:
            raise ValueError("aggregate case lacks its exact replication Artifact binding")
        return bindings[request.replication_index]

    @staticmethod
    def _validate_aggregate_identity(identity: BenchAggregateInputIdentityV1) -> None:
        """Reject coercive/untyped projections before Python equality can alias them.

        The aggregate verifier is a trusted adapter, but its projection still crosses
        a heterogeneous payload boundary.  In particular, Python considers ``True``
        equal to integer ``1``; accepting it here would let a malformed replication or
        seed identity satisfy an exact expected key.
        """

        for name, value in (
            ("artifact_id", identity.artifact_id),
            ("case_id", identity.case_id),
            ("partition_id", identity.partition_id),
            ("dataset_artifact_id", identity.dataset_artifact_id),
            ("benchmark_spec_artifact_id", identity.benchmark_spec_artifact_id),
            ("seed_derivation_version", identity.seed_derivation_version),
            ("producer_run_id", identity.producer_run_id),
            ("producer_run_payload_hash", identity.producer_run_payload_hash),
            ("producer_result_artifact_id", identity.producer_result_artifact_id),
            ("producer_result_payload_hash", identity.producer_result_payload_hash),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"bench aggregate {name} must be a non-empty string")
        if identity.mode not in {"deterministic", "agent"}:
            raise ValueError("bench aggregate identity has an unknown execution mode")
        if not isinstance(identity.evaluator_profile, ProfileRefV1):
            raise TypeError("bench aggregate evaluator profile has the wrong type")
        if not isinstance(identity.run_kind, RunKindRef):
            raise TypeError("bench aggregate Run kind has the wrong type")
        if not isinstance(identity.producer_run_kind, RunKindRef):
            raise TypeError("bench aggregate producer Run kind has the wrong type")
        if not isinstance(
            identity.producer_seed_binding,
            BenchmarkAggregateProducerSeedBindingV1,
        ):
            raise TypeError("bench aggregate producer seed binding has the wrong type")
        if (
            isinstance(identity.replication_index, bool)
            or not isinstance(identity.replication_index, int)
            or identity.replication_index < 0
        ):
            raise ValueError("bench aggregate replication index must be a non-negative integer")
        if (
            isinstance(identity.producer_attempt_no, bool)
            or not isinstance(identity.producer_attempt_no, int)
            or identity.producer_attempt_no < 1
        ):
            raise ValueError("bench aggregate producer attempt must be a positive integer")
        for name, value in (
            ("root_seed", identity.root_seed),
            ("execution_seed", identity.execution_seed),
            ("producer_root_seed", identity.producer_root_seed),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"bench aggregate {name} must be an exact integer or null")
        if identity.producer_seed_derivation_version is not None and (
            not isinstance(identity.producer_seed_derivation_version, str)
            or not identity.producer_seed_derivation_version
        ):
            raise ValueError(
                "bench aggregate producer seed derivation must be a non-empty string or null"
            )
        if not isinstance(identity.producer_resolved_profiles, tuple):
            raise TypeError("bench aggregate producer profiles have the wrong type")

    @staticmethod
    def _agent_source_suffix(
        *,
        case: BenchCaseSpecV1,
        dataset_artifact_id: str,
        benchmark_spec_artifact_id: str,
        evaluator_profile: ProfileRefV1,
        run_kind: RunKindRef,
        root_seed: int | None,
        replication_index: int,
        execution_seed: int | None,
        seed_derivation_version: str,
        case_ordinal: int,
        result_ordinal: int,
    ) -> str:
        digest = sha256(
            canonical_json(
                {
                    "binding_schema_version": "bench-agent-source-binding@1",
                    "case_id": case.case_id,
                    "partition_id": case.partition_id,
                    "execution_mode": case.mode,
                    "dataset_artifact_id": dataset_artifact_id,
                    "benchmark_spec_artifact_id": benchmark_spec_artifact_id,
                    "evaluator_profile": evaluator_profile.model_dump(mode="json"),
                    "run_kind": run_kind.model_dump(mode="json"),
                    "root_seed": root_seed,
                    "replication_index": replication_index,
                    "execution_seed": execution_seed,
                    "seed_derivation_version": seed_derivation_version,
                    "case_ordinal": case_ordinal,
                    "result_ordinal": result_ordinal,
                }
            ).encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}"

    def _evaluation_requests(
        self,
        *,
        context: ExecutorContextLike,
        payload: BenchRunPayloadV1,
        cases: tuple[BenchCaseSpecV1, ...],
        root_seed: int | None,
    ) -> tuple[BenchCaseEvaluationRequestV1, ...]:
        requests: list[BenchCaseEvaluationRequestV1] = []
        for case_ordinal, case in enumerate(cases):
            for replication_index in range(payload.repetition_count):
                execution_seed = (
                    None
                    if root_seed is None
                    else derive_validation_subseed(
                        root_seed=root_seed,
                        run_kind=context.run.kind,
                        profile=payload.evaluator_profile,
                        case_id=case.case_id,
                        replication_index=replication_index,
                    )
                )
                requests.append(
                    BenchCaseEvaluationRequestV1(
                        case=case,
                        dataset_artifact_id=payload.dataset_artifact_id,
                        benchmark_spec_artifact_id=payload.benchmark_spec_artifact_id,
                        evaluator_profile=payload.evaluator_profile,
                        run_kind=context.run.kind,
                        root_seed=root_seed,
                        replication_index=replication_index,
                        execution_seed=execution_seed,
                        seed_derivation_version=BENCH_SEED_DERIVATION_VERSION,
                        case_ordinal=case_ordinal,
                        result_ordinal=len(requests),
                        agent_source_suffix=(
                            self._agent_source_suffix(
                                case=case,
                                dataset_artifact_id=payload.dataset_artifact_id,
                                benchmark_spec_artifact_id=(payload.benchmark_spec_artifact_id),
                                evaluator_profile=payload.evaluator_profile,
                                run_kind=context.run.kind,
                                root_seed=root_seed,
                                replication_index=replication_index,
                                execution_seed=execution_seed,
                                seed_derivation_version=BENCH_SEED_DERIVATION_VERSION,
                                case_ordinal=case_ordinal,
                                result_ordinal=len(requests),
                            )
                            if case.mode == "agent"
                            else None
                        ),
                    )
                )
        return tuple(requests)

    def _evaluate_one(
        self,
        *,
        request: BenchCaseEvaluationRequestV1,
        invoker: _BoundAgentInvoker | None,
    ) -> BenchCaseResultV1:
        model_call_ordinals: tuple[int, ...] = ()
        scoped_invoker: _ScopedAgentInvoker | None = None
        if request.case.mode == "agent":
            if invoker is None or request.agent_source_suffix is None:
                raise ValueError("Agent bench case requires a live/record/replay Run")
            before = invoker.call_count
            scoped_invoker = _ScopedAgentInvoker(
                delegate=invoker,
                expected_source_suffix=request.agent_source_suffix,
            )
            verdict = self.evaluator.evaluate(request, agent_invoker=scoped_invoker)
            if (
                scoped_invoker.attempt_count != request.case.agent_prompt_count
                or invoker.call_count != before + scoped_invoker.attempt_count
            ):
                raise ValueError(
                    "an Agent bench case replication changed its exact model-call count"
                )
            model_call_ordinals = invoker.call_ordinals[before:]
            if len(model_call_ordinals) != scoped_invoker.attempt_count:
                raise ValueError("Agent bench result lacks its ordered cassette call identity")
        else:
            verdict = self.evaluator.evaluate(request, agent_invoker=None)

        if not isinstance(verdict, BenchCaseVerdictV1):
            raise TypeError("bench evaluator returned the wrong verdict type")
        if verdict.status not in {"pass", "fail"}:
            raise ValueError("bench evaluator returned a non-binary status")
        if not isinstance(verdict.metrics, Mapping):
            raise TypeError("bench evaluator returned an invalid metric mapping")
        try:
            metrics = TypeAdapter(dict[str, JsonValue]).validate_python(
                dict(verdict.metrics), strict=True
            )
            metrics_bytes = canonical_json(metrics).encode("utf-8")
        except (TypeError, ValueError, ValidationError) as exc:
            raise ValueError("bench evaluator metrics are not canonical JSON") from exc
        if (
            len(metrics) > MAX_BENCHMARK_RESULT_METRIC_FIELDS
            or len(metrics_bytes) > MAX_BENCHMARK_RESULT_METRICS_BYTES
            or any(not key or len(key) > 512 for key in metrics)
        ):
            raise ValueError("bench evaluator metrics exceed their frozen bounds")
        return BenchCaseResultV1(
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
            case_ordinal=request.case_ordinal,
            result_ordinal=request.result_ordinal,
            agent_source_suffix=request.agent_source_suffix,
            model_call_ordinals=model_call_ordinals,
            status=verdict.status,
            metrics=MappingProxyType(metrics),
        )

    @staticmethod
    def _request_identity_key(request: BenchCaseEvaluationRequestV1) -> tuple[object, ...]:
        return (
            request.case.case_id,
            request.case.partition_id,
            request.case.mode,
            request.dataset_artifact_id,
            request.benchmark_spec_artifact_id,
            request.evaluator_profile.profile_id,
            request.evaluator_profile.version,
            request.run_kind.kind,
            request.run_kind.version,
            request.root_seed,
            request.replication_index,
            request.execution_seed,
            request.seed_derivation_version,
        )

    @staticmethod
    def _aggregate_identity_key(identity: BenchAggregateInputIdentityV1) -> tuple[object, ...]:
        return (
            identity.artifact_id,
            identity.case_id,
            identity.partition_id,
            identity.mode,
            identity.dataset_artifact_id,
            identity.benchmark_spec_artifact_id,
            identity.evaluator_profile.profile_id,
            identity.evaluator_profile.version,
            identity.run_kind.kind,
            identity.run_kind.version,
            identity.root_seed,
            identity.replication_index,
            identity.execution_seed,
            identity.seed_derivation_version,
            identity.producer_run_id,
            identity.producer_run_kind.kind,
            identity.producer_run_kind.version,
            identity.producer_run_payload_hash,
            identity.producer_attempt_no,
            identity.producer_result_artifact_id,
            identity.producer_result_payload_hash,
            identity.producer_seed_binding,
            identity.producer_root_seed,
            identity.producer_seed_derivation_version,
            identity.producer_resolved_profiles,
        )

    @staticmethod
    def _expectation_request_identity_key(
        expectation: BenchAggregateInputExpectationV1,
    ) -> tuple[object, ...]:
        binding = expectation.binding
        return (
            binding.case_id,
            binding.partition_id,
            binding.execution_mode,
            binding.dataset_artifact_id,
            expectation.benchmark_spec_artifact_id,
            binding.evaluator_profile.profile_id,
            binding.evaluator_profile.version,
            binding.run_kind.kind,
            binding.run_kind.version,
            binding.root_seed,
            binding.replication_index,
            binding.execution_seed,
            binding.seed_derivation_version,
        )

    @staticmethod
    def _expectation_identity_key(
        expectation: BenchAggregateInputExpectationV1,
    ) -> tuple[object, ...]:
        binding = expectation.binding
        return (
            binding.artifact_id,
            *BenchRunHandler._expectation_request_identity_key(expectation),
            binding.producer_run_id,
            binding.producer_run_kind.kind,
            binding.producer_run_kind.version,
            binding.producer_run_payload_hash,
            binding.producer_attempt_no,
            binding.producer_result_artifact_id,
            binding.producer_result_payload_hash,
            binding.producer_seed_binding,
            binding.producer_root_seed,
            binding.producer_seed_derivation_version,
            binding.producer_resolved_profiles,
        )

    def _agent_invoker(self, context: ExecutorContextLike) -> _BoundAgentInvoker | None:
        if context.payload.llm_execution_mode == "not_applicable":
            return None
        adapter = ModelBridgeAgentAdapter(
            model_bridge=context.model_bridge,
            idempotency_scope=(f"run:{context.run.run_id}:attempt:{context.attempt.attempt_no}"),
            deadline_utc=context.deadline_utc,
        )
        model_snapshot = plan_node_snapshot(
            context.payload.execution_version_plan,
            self.agent_node_id,
            context.model_bridge,
        )
        return _BoundAgentInvoker(
            adapter=adapter,
            model_snapshot=model_snapshot,
            source_artifact_ids=prompt_source_artifact_ids(context),
            node_id=self.agent_node_id,
            prompt_version=self.agent_prompt_version,
        )


__all__ = [
    "BENCH_REPORT_SCHEMA_ID",
    "BENCH_SEED_DERIVATION_VERSION",
    "AgentCaseInvoker",
    "BenchAggregateCaseResultV1",
    "BenchAggregateInputExpectationV1",
    "BenchAggregateInputIdentityV1",
    "BenchAggregateInputVerifier",
    "BenchVerifiedAggregateInputV1",
    "BenchCaseEvaluationRequestV1",
    "BenchCaseEvaluator",
    "BenchCaseLoader",
    "BenchCaseResultV1",
    "BenchCaseSpecV1",
    "BenchCaseVerdictV1",
    "BenchReportComposer",
    "BenchRunHandler",
]
