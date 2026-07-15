"""Task 11a — ``review_runner@1`` (composite) + ``bench_runner@1`` (two-phase)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    BenchRunPayloadV1,
    GraphSelectionV1,
    PreparedRunResult,
    ReviewRunPayloadV1,
)
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.platform.run_handlers.bench import (
    BENCH_REPORT_SCHEMA_ID,
    BenchCaseResultV1,
    BenchCaseSpecV1,
    BenchRunHandler,
)
from gameforge.platform.run_handlers.review import ReviewRunHandler, ReviewSimConfig
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    FakeModelBridge,
    build_context,
    execution_plan,
    resolved_binding,
    snapshot_bytes,
)

REVIEW_KIND = RunKindRef(kind="review.run", version=1)
BENCH_KIND = RunKindRef(kind="bench.run", version=1)
SNAPSHOT_ID = "artifact:snapshot"
MODEL_REF = "anthropic/claude-opus-4-8/m2a@1"


# ------------------------------------------------------------------- review
def _combined_snapshot() -> bytes:
    # dangling reference (for the graph checker) + runaway faucet (for the sim).
    gold = Entity(id="gold", type=NodeType.CURRENCY, attrs={})
    mob = Entity(
        id="mob",
        type=NodeType.MONSTER,
        attrs={"gold_min": 60, "gold_max": 140, "kills_per_tick": 10},
    )
    drop = Relation(id="drop", type=EdgeType.DROPS_FROM, src_id="mob", dst_id="gold")
    dangling = Relation(id="bad", type=EdgeType.SELLS, src_id="shop:ghost", dst_id="gold")
    return snapshot_bytes([gold, mob], [drop, dangling])


def _checker_resolver(profile, constraints):
    return GraphChecker()


def _sim_config_resolver(profile):
    return ReviewSimConfig(n_agents=12, n_ticks=40)


def _review_payload(*, triage: bool) -> ReviewRunPayloadV1:
    return ReviewRunPayloadV1(
        snapshot_artifact_id=SNAPSHOT_ID,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=ProfileRefV1(profile_id="review", version=1),
        checker_profiles=(ProfileRefV1(profile_id="graph", version=1),),
        simulation_profiles=(ProfileRefV1(profile_id="econ", version=1),),
        llm_triage_policy=ProfileRefV1(profile_id="triage", version=1) if triage else None,
    )


def _review_handler(store: FakeArtifactStore) -> ReviewRunHandler:
    return ReviewRunHandler(
        blobs=store,
        store=store,
        checker_resolver=_checker_resolver,
        sim_config_resolver=_sim_config_resolver,
    )


def _review_profiles():
    return (
        resolved_binding("/params/review_profile", profile_id="review", version=1, kind="review"),
        resolved_binding("/params/checker_profiles", profile_id="graph", version=1, kind="checker"),
        resolved_binding(
            "/params/simulation_profiles", profile_id="econ", version=1, kind="simulation"
        ),
    )


def test_review_without_triage_is_deterministic_only_and_not_applicable() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    bridge = FakeModelBridge()
    context = build_context(
        params=_review_payload(triage=False),
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=_review_profiles(),
        model_bridge=bridge,
    )
    outcome = _review_handler(store)(context)

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "review_completed"
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "review_report"
    assert primary.payload_schema_id == "review@1"
    assert primary.meta["llm_execution_mode"] == "not_applicable"
    assert primary.meta["llm_triage_applied"] is False
    # No triage policy => the bridge is never touched.
    assert bridge.requests == []
    # output checker_run + simulation_run artifacts, each carrying its /profile.
    kinds = [artifact.kind for artifact in outcome.artifacts]
    assert kinds.count("checker_run") == 1
    assert kinds.count("simulation_run") == 1
    checker_artifact = next(a for a in outcome.artifacts if a.kind == "checker_run")
    payload = json.loads(store.read_prepared(checker_artifact.object_ref))
    assert payload["profile"] == {"profile_id": "graph", "version": 1}
    # authoritative deterministic verdict present (checker + simulation only).
    oracle_types = {f.payload.oracle_type for f in outcome.findings}
    assert oracle_types == {"deterministic", "simulation"}
    assert all(f.payload.producer_run_id == "run:1" for f in outcome.findings)


def test_review_triage_adds_llm_suggestions_without_changing_verdict() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    bridge = FakeModelBridge(
        responses=(
            json.dumps(
                {
                    "suggestions": [
                        {
                            "defect_class": "balance_smell",
                            "severity": "minor",
                            "message": "hot faucet",
                        }
                    ]
                }
            ),
        )
    )
    context = build_context(
        params=_review_payload(triage=True),
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=_review_profiles(),
        llm_execution_mode="replay",
        plan=execution_plan({"review-triage": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )
    outcome = _review_handler(store)(context)

    primary = outcome.artifacts[outcome.primary_index]
    assert primary.meta["llm_execution_mode"] == "replay"
    assert primary.meta["llm_triage_applied"] is True
    # exactly one ordered triage call went through the bridge.
    assert len(bridge.requests) == 1

    by_oracle = {}
    for finding in outcome.findings:
        by_oracle.setdefault(finding.payload.oracle_type, []).append(finding)
    assert "deterministic" in by_oracle and "simulation" in by_oracle
    suggestions = by_oracle.get("llm-assisted", [])
    assert suggestions, "triage suggestion must be recorded as llm-assisted"
    for suggestion in suggestions:
        assert suggestion.payload.source == "llm"
        assert suggestion.payload.status == "unproven"  # advisory, never a proven verdict
    # the deterministic findings are unchanged (still confirmed).
    assert all(f.payload.status == "confirmed" for f in by_oracle["deterministic"])


def test_review_without_triage_is_byte_deterministic() -> None:
    store_a, store_b = FakeArtifactStore(), FakeArtifactStore()
    store_a.register(SNAPSHOT_ID, _combined_snapshot())
    store_b.register(SNAPSHOT_ID, _combined_snapshot())
    out_a = _review_handler(store_a)(
        build_context(
            params=_review_payload(triage=False),
            kind=REVIEW_KIND,
            seed=5,
            resolved_profiles=_review_profiles(),
        )
    )
    out_b = _review_handler(store_b)(
        build_context(
            params=_review_payload(triage=False),
            kind=REVIEW_KIND,
            seed=5,
            resolved_profiles=_review_profiles(),
        )
    )
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]


def test_review_two_profiles_same_checker_id_emit_distinct_finding_ids() -> None:
    # M1 carry-forward fix: two checker_profiles resolving to the SAME checker id on
    # the SAME snapshot must not collide on one finding-series head (the finding CAS
    # would otherwise reject the duplicate at publish).
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _combined_snapshot())
    payload = ReviewRunPayloadV1(
        snapshot_artifact_id=SNAPSHOT_ID,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=ProfileRefV1(profile_id="review", version=1),
        checker_profiles=(
            ProfileRefV1(profile_id="graph-a", version=1),
            ProfileRefV1(profile_id="graph-b", version=1),
        ),
        simulation_profiles=(),
        llm_triage_policy=None,
    )
    context = build_context(
        params=payload,
        kind=REVIEW_KIND,
        seed=5,
        resolved_profiles=(
            resolved_binding(
                "/params/review_profile", profile_id="review", version=1, kind="review"
            ),
            resolved_binding(
                "/params/checker_profiles", profile_id="graph-a", version=1, kind="checker"
            ),
        ),
    )
    outcome = _review_handler(store)(context)

    checker_findings = [f for f in outcome.findings if f.payload.oracle_type == "deterministic"]
    assert checker_findings, "the dangling reference must surface a deterministic finding"
    finding_ids = [f.finding_id for f in checker_findings]
    # each deterministic finding is present once per resolving profile, but the ids
    # are profile-scoped so no two collide on one series head.
    assert len(finding_ids) == len(set(finding_ids))
    assert any(fid.startswith("graph-a@1:") for fid in finding_ids)
    assert any(fid.startswith("graph-b@1:") for fid in finding_ids)


# -------------------------------------------------------------------- bench
class _FakeCaseLoader:
    def __init__(self, cases):
        self._cases = cases
        self.seen = None

    def load_cases(self, *, dataset_artifact_id, benchmark_spec_artifact_id, partition_ids):
        self.seen = (dataset_artifact_id, benchmark_spec_artifact_id, partition_ids)
        return self._cases


class _FakeEvaluator:
    def __init__(self):
        self.deterministic_calls = 0
        self.agent_calls = 0

    def evaluate(self, case, *, agent_invoker):
        if case.mode == "agent":
            self.agent_calls += 1
            text = agent_invoker(case.prompt, source_suffix=case.case_id)
            return BenchCaseResultV1(
                case_id=case.case_id,
                partition_id=case.partition_id,
                mode="agent",
                status=text.strip() or "pass",
            )
        self.deterministic_calls += 1
        return BenchCaseResultV1(
            case_id=case.case_id,
            partition_id=case.partition_id,
            mode="deterministic",
            status="pass",
        )


class _FakeComposer:
    def __init__(self):
        self.execute_calls = []
        self.aggregate_calls = []

    def compose_execute(self, *, case_results, seed, partition_ids, **_):
        self.execute_calls.append((case_results, seed, partition_ids))
        return json.dumps(
            {
                "schema_version": BENCH_REPORT_SCHEMA_ID,
                "phase": "execute_cases",
                "seed": seed,
                "cases": [
                    {"case_id": r.case_id, "partition_id": r.partition_id, "status": r.status}
                    for r in case_results
                ],
            },
            sort_keys=True,
        ).encode("utf-8")

    def compose_aggregate(self, *, case_result_blobs, seed, **_):
        self.aggregate_calls.append((case_result_blobs, seed))
        return json.dumps(
            {
                "schema_version": BENCH_REPORT_SCHEMA_ID,
                "phase": "aggregate_results",
                "seed": seed,
                "case_result_ids": [cid for cid, _ in case_result_blobs],
            },
            sort_keys=True,
        ).encode("utf-8")


def _bench_execute_payload() -> BenchRunPayloadV1:
    return BenchRunPayloadV1(
        dataset_artifact_id="artifact:dataset",
        benchmark_spec_artifact_id="artifact:spec",
        partition_ids=("p1",),
        evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )


def _bench_aggregate_payload() -> BenchRunPayloadV1:
    return BenchRunPayloadV1(
        dataset_artifact_id="artifact:dataset",
        benchmark_spec_artifact_id="artifact:spec",
        partition_ids=("p1",),
        evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
        repetition_count=1,
        execution_scope="aggregate_results",
        case_result_artifact_ids=("case:1", "case:2"),
    )


def test_bench_execute_cases_runs_ordered_agent_cassette_and_seals_report() -> None:
    store = FakeArtifactStore()
    cases = (
        BenchCaseSpecV1(case_id="c0", partition_id="p1", mode="deterministic"),
        BenchCaseSpecV1(case_id="c1", partition_id="p1", mode="agent", prompt="score c1"),
        BenchCaseSpecV1(case_id="c2", partition_id="p1", mode="agent", prompt="score c2"),
    )
    loader, evaluator, composer = _FakeCaseLoader(cases), _FakeEvaluator(), _FakeComposer()
    bridge = FakeModelBridge(responses=("pass", "pass"))
    handler = BenchRunHandler(
        blobs=store, store=store, case_loader=loader, evaluator=evaluator, composer=composer
    )
    context = build_context(
        params=_bench_execute_payload(),
        kind=BENCH_KIND,
        seed=3,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
        llm_execution_mode="replay",
        plan=execution_plan({"bench-agent-case": MODEL_REF}),
        cassette_artifact_id="artifact:cassette",
        model_bridge=bridge,
    )
    outcome = handler(context)

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "bench_completed"
    assert outcome.findings == ()  # bench finding policy is null
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "bench_report"
    assert primary.payload_schema_id == BENCH_REPORT_SCHEMA_ID
    assert primary.lineage == ("artifact:dataset", "artifact:spec")

    assert evaluator.deterministic_calls == 1 and evaluator.agent_calls == 2
    # ONE ordered run-scoped cassette: both agent calls share a monotonic ordinal.
    assert len(bridge.requests) == 2
    keys = [request.idempotency_key for request in bridge.requests]
    assert keys == ["run:1:1:model:1", "run:1:1:model:2"]


def test_bench_aggregate_consumes_case_results_and_binds_lineage() -> None:
    store = FakeArtifactStore()
    store.register("case:1", {"case": 1})
    store.register("case:2", {"case": 2})
    loader, evaluator, composer = _FakeCaseLoader(()), _FakeEvaluator(), _FakeComposer()
    handler = BenchRunHandler(
        blobs=store, store=store, case_loader=loader, evaluator=evaluator, composer=composer
    )
    context = build_context(
        params=_bench_aggregate_payload(),
        kind=BENCH_KIND,
        resolved_profiles=(
            resolved_binding(
                "/params/evaluator_profile", profile_id="eval", version=1, kind="bench_evaluator"
            ),
        ),
    )
    outcome = handler(context)

    primary = outcome.artifacts[outcome.primary_index]
    assert primary.kind == "bench_report"
    assert primary.lineage == ("artifact:dataset", "artifact:spec", "case:1", "case:2")
    assert outcome.findings == ()
    assert evaluator.agent_calls == 0  # aggregation never evaluates cases
    assert len(composer.aggregate_calls) == 1
    consumed_ids = [cid for cid, _ in composer.aggregate_calls[0][0]]
    assert consumed_ids == ["case:1", "case:2"]


def test_bench_payload_enforces_execution_scope_input_rules() -> None:
    with pytest.raises(ValidationError):
        BenchRunPayloadV1(
            dataset_artifact_id="d",
            benchmark_spec_artifact_id="s",
            partition_ids=("p1",),
            evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
            repetition_count=1,
            execution_scope="execute_cases",
            case_result_artifact_ids=("case:1",),
        )
    with pytest.raises(ValidationError):
        BenchRunPayloadV1(
            dataset_artifact_id="d",
            benchmark_spec_artifact_id="s",
            partition_ids=("p1",),
            evaluator_profile=ProfileRefV1(profile_id="eval", version=1),
            repetition_count=1,
            execution_scope="aggregate_results",
            case_result_artifact_ids=(),
        )
