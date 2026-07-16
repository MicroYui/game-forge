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
from typing import Literal, Mapping, Protocol

from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.jobs import BenchRunPayloadV1, PreparedRunOutcome
from gameforge.contracts.lineage import VersionTuple

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    PreparedArtifactStore,
    build_success_result,
    store_prepared_blob,
)
from gameforge.platform.run_handlers.model_routing import (
    ModelBridgeAgentAdapter,
    plan_node_snapshot,
)

BENCH_REPORT_SCHEMA_ID = "bench-report@2"
BENCH_TOOL_VERSION = "bench@1"
BENCH_AGENT_NODE_ID = "bench-agent-case"
BENCH_AGENT_PROMPT_VERSION = "bench-agent@1"

BenchCaseMode = Literal["deterministic", "agent"]


@dataclass(frozen=True, slots=True)
class BenchCaseSpecV1:
    """One resolved bench case selected from a dataset partition."""

    case_id: str
    partition_id: str
    mode: BenchCaseMode
    prompt: str = ""
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BenchCaseResultV1:
    """The per-case verdict produced by an evaluator."""

    case_id: str
    partition_id: str
    mode: BenchCaseMode
    status: str
    metrics: Mapping[str, object] = field(default_factory=dict)


class AgentCaseInvoker(Protocol):
    """Run one ordered agent turn and return the normalized model text."""

    def __call__(self, prompt: str, *, source_suffix: str) -> str: ...


class BenchCaseLoader(Protocol):
    """Resolve the ordered case specs for the selected partitions."""

    def load_cases(
        self,
        *,
        dataset_artifact_id: str,
        benchmark_spec_artifact_id: str,
        partition_ids: tuple[str, ...],
    ) -> tuple[BenchCaseSpecV1, ...]: ...


class BenchCaseEvaluator(Protocol):
    """Evaluate one case (deterministic via spine oracles; agent via the invoker)."""

    def evaluate(
        self, case: BenchCaseSpecV1, *, agent_invoker: AgentCaseInvoker | None
    ) -> BenchCaseResultV1: ...


class BenchReportComposer(Protocol):
    """Compose the canonical ``bench-report@2`` bytes for either phase."""

    def compose_execute(
        self,
        *,
        dataset_artifact_id: str,
        benchmark_spec_artifact_id: str,
        partition_ids: tuple[str, ...],
        case_results: tuple[BenchCaseResultV1, ...],
        seed: int,
        evaluator_profile: ProfileRefV1,
    ) -> bytes: ...

    def compose_aggregate(
        self,
        *,
        dataset_artifact_id: str,
        benchmark_spec_artifact_id: str,
        partition_ids: tuple[str, ...],
        case_result_blobs: tuple[tuple[str, bytes], ...],
        seed: int,
        evaluator_profile: ProfileRefV1,
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

    def __call__(self, prompt: str, *, source_suffix: str) -> str:
        result = self.adapter.call_model(
            agent_node_id=self._node_id,
            user_prompt=prompt,
            prompt_version=self._prompt_version,
            model_snapshot=self._model_snapshot,
            source_artifact_ids=self._source_artifact_ids,
        )
        return result.response.response_normalized


@dataclass(frozen=True, slots=True)
class BenchRunHandler:
    """A ``RunExecutor`` producing the primary ``bench_report`` (no findings)."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    case_loader: BenchCaseLoader
    evaluator: BenchCaseEvaluator
    composer: BenchReportComposer
    agent_node_id: str = BENCH_AGENT_NODE_ID
    agent_prompt_version: str = BENCH_AGENT_PROMPT_VERSION

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, BenchRunPayloadV1):
            raise TypeError("bench_runner@1 requires a bench-run@1 payload")

        root_seed = context.payload.seed
        seed = int(root_seed) if root_seed is not None else 0
        if payload.execution_scope == "execute_cases":
            report_bytes, lineage = self._execute_cases(context, payload, seed)
        else:
            report_bytes, lineage = self._aggregate_results(context, payload, seed)

        primary = store_prepared_blob(
            self.store,
            kind="bench_report",
            payload_schema_id=BENCH_REPORT_SCHEMA_ID,
            version_tuple=VersionTuple(tool_version=BENCH_TOOL_VERSION, seed=root_seed),
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
        self, context: ExecutorContextLike, payload: BenchRunPayloadV1, seed: int
    ) -> tuple[bytes, tuple[str, ...]]:
        if payload.case_result_artifact_ids:
            raise ValueError("execute_cases forbids precomputed case results")
        cases = self.case_loader.load_cases(
            dataset_artifact_id=payload.dataset_artifact_id,
            benchmark_spec_artifact_id=payload.benchmark_spec_artifact_id,
            partition_ids=payload.partition_ids,
        )
        invoker = self._agent_invoker(context)
        results: list[BenchCaseResultV1] = []
        for case in cases:
            if case.mode == "agent" and invoker is None:
                raise ValueError("agent bench case requires a live/record/replay Run")
            results.append(self.evaluator.evaluate(case, agent_invoker=invoker))
        report_bytes = self.composer.compose_execute(
            dataset_artifact_id=payload.dataset_artifact_id,
            benchmark_spec_artifact_id=payload.benchmark_spec_artifact_id,
            partition_ids=payload.partition_ids,
            case_results=tuple(results),
            seed=seed,
            evaluator_profile=payload.evaluator_profile,
        )
        lineage = (payload.dataset_artifact_id, payload.benchmark_spec_artifact_id)
        return report_bytes, lineage

    def _aggregate_results(
        self, context: ExecutorContextLike, payload: BenchRunPayloadV1, seed: int
    ) -> tuple[bytes, tuple[str, ...]]:
        if not payload.case_result_artifact_ids:
            raise ValueError("aggregate_results requires case result artifacts")
        case_result_blobs = tuple(
            (case_id, self.blobs.read_bytes(case_id))
            for case_id in payload.case_result_artifact_ids
        )
        report_bytes = self.composer.compose_aggregate(
            dataset_artifact_id=payload.dataset_artifact_id,
            benchmark_spec_artifact_id=payload.benchmark_spec_artifact_id,
            partition_ids=payload.partition_ids,
            case_result_blobs=case_result_blobs,
            seed=seed,
            evaluator_profile=payload.evaluator_profile,
        )
        lineage = (
            payload.dataset_artifact_id,
            payload.benchmark_spec_artifact_id,
            *payload.case_result_artifact_ids,
        )
        return report_bytes, lineage

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
            source_artifact_ids=(context.payload.params.dataset_artifact_id,),
            node_id=self.agent_node_id,
            prompt_version=self.agent_prompt_version,
        )


__all__ = [
    "BENCH_REPORT_SCHEMA_ID",
    "AgentCaseInvoker",
    "BenchCaseEvaluator",
    "BenchCaseLoader",
    "BenchCaseResultV1",
    "BenchCaseSpecV1",
    "BenchReportComposer",
    "BenchRunHandler",
]
