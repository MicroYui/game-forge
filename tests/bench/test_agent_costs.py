"""Agent-layer composition for source-neutral cost and latency evidence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gameforge.bench.agent_costs import (
    RequestTrackingRouter,
    build_agent_cost_evidence,
    hed_traces,
    narrative_verification_traces,
    playtest_replay_traces,
    repair_replay_traces,
)
from gameforge.bench.hed.contracts import load_evidence as load_hed_evidence
from gameforge.bench.narrative.harness import load_evidence as load_narrative_evidence
from gameforge.contracts.model_router import (
    Message,
    ModelRequest,
    ModelResponse,
    ModelSnapshot,
)

GPT_56 = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)
OPUS_M2 = ModelSnapshot(
    provider="anthropic",
    model="claude-opus-4-8",
    snapshot_tag="m2a@1",
)


class _FakeReplayRouter:
    def __init__(self, snapshot: ModelSnapshot) -> None:
        self.default_model_snapshot = snapshot

    def call(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(response_normalized="{}", latency_ms=1)


def _request(label: str, snapshot: ModelSnapshot) -> ModelRequest:
    return ModelRequest(
        model_snapshot=snapshot,
        messages=[Message(role="user", content=label)],
        agent_node_id="test.trace",
        prompt_version="test@1",
    )


def test_narrative_trace_uses_all_1905_frozen_verification_cases():
    evidence = load_narrative_evidence(
        "scenarios/narrative_bench/verification-evidence.json"
    )
    traces = narrative_verification_traces(evidence)

    assert len(traces) == 1905
    assert sum(len(row.request_hashes) for row in traces) == 5715
    assert tuple(row.sample_id for row in traces) == tuple(
        outcome.case_id for outcome in evidence.outcomes
    )


def test_hed_trace_keeps_14_logical_requests_but_only_10_recorded_requests():
    evidence = load_hed_evidence(
        "scenarios/external_cases/endless_sky/hed-evidence.json"
    )
    traces = hed_traces(evidence)

    assert len(traces) == 8
    assert sum(len(row.request_hashes) for row in traces) == 14
    assert sum(len(dict.fromkeys(row.request_hashes)) for row in traces) == 10


def test_request_tracking_router_preserves_logical_call_order():
    delegate = _FakeReplayRouter(GPT_56)
    tracker = RequestTrackingRouter(delegate)
    first = _request("first", GPT_56)
    second = _request("second", GPT_56)

    tracker.call(first)
    tracker.call(first)
    tracker.call(second)

    assert tracker.default_model_snapshot == GPT_56
    assert len(tracker.request_hashes) == 3
    assert tracker.request_hashes[0] == tracker.request_hashes[1]
    assert tracker.request_hashes[1] != tracker.request_hashes[2]


def test_repair_trace_runs_each_scenario_separately_with_one_shared_replay_router():
    delegate = _FakeReplayRouter(GPT_56)
    calls: list[tuple[tuple[str, ...], str, int]] = []

    def fake_run(scenarios, constraints, router, *, max_steps):
        calls.append((tuple(scenarios), constraints, max_steps))
        request = _request(scenarios[0], GPT_56)
        router.call(request)
        router.call(request)
        return SimpleNamespace(attempted=1)

    traces = repair_replay_traces(
        scenario_dirs=("fixtures/zeta", "fixtures/alpha"),
        constraints_path="fixtures/constraints",
        router=delegate,
        run_corpus=fake_run,
    )

    assert tuple(row.sample_id for row in traces) == ("alpha", "zeta")
    assert all(len(row.request_hashes) == 2 for row in traces)
    assert calls == [
        (("fixtures/alpha",), "fixtures/constraints", 4),
        (("fixtures/zeta",), "fixtures/constraints", 4),
    ]

    with pytest.raises(ValueError, match="nonempty"):
        repair_replay_traces(
            scenario_dirs=(),
            router=delegate,
            run_corpus=fake_run,
        )


@pytest.mark.parametrize(
    ("variant", "expected_planner", "expected_memory"),
    (("layered", True, False), ("flat", False, False), ("memory_on", True, True)),
)
def test_playtest_trace_maps_each_variant_to_the_frozen_harness_shape(
    variant,
    expected_planner,
    expected_memory,
):
    delegate = _FakeReplayRouter(OPUS_M2)
    kwargs_seen: list[dict] = []

    def fake_run(chains, router, **kwargs):
        kwargs_seen.append(kwargs)
        router.call(_request(str(chains[0]), OPUS_M2))
        return SimpleNamespace(n_chains=1)

    traces = playtest_replay_traces(
        variant,
        chain_snapshots=("chain-b", "chain-a"),
        router=delegate,
        run_corpus=fake_run,
    )

    assert tuple(row.sample_id for row in traces) == ("chain-000", "chain-001")
    assert all(len(row.request_hashes) == 1 for row in traces)
    assert all(item["use_planner"] is expected_planner for item in kwargs_seen)
    assert all(item["max_steps"] == 150 for item in kwargs_seen)
    assert all((item.get("memory_factory") is not None) is expected_memory for item in kwargs_seen)


def test_measured_agent_cost_evidence_uses_exact_denominators_and_snapshots():
    evidence = build_agent_cost_evidence()
    workloads = {item.workload_id: item for item in evidence.workloads}

    assert set(workloads) == {
        "external-hed",
        "narrative-verification",
        "playtest-flat",
        "playtest-layered",
        "playtest-memory-on",
        "repair-search",
    }
    assert workloads["narrative-verification"].evaluated_n == 1905
    assert workloads["narrative-verification"].logical_requests == 5715
    assert workloads["external-hed"].evaluated_n == 8
    assert workloads["external-hed"].logical_requests == 14
    assert workloads["external-hed"].recorded_requests == 10
    assert workloads["repair-search"].evaluated_n == 10
    for workload_id in (
        "playtest-layered",
        "playtest-flat",
        "playtest-memory-on",
    ):
        assert workloads[workload_id].evaluated_n == 20
        assert workloads[workload_id].model_snapshot == OPUS_M2
    assert workloads["narrative-verification"].model_snapshot == GPT_56
    assert workloads["external-hed"].model_snapshot == GPT_56
    assert workloads["repair-search"].model_snapshot == GPT_56
