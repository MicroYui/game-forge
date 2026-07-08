"""M2b-1 Task 7 harness tests: Wilson CI numerics + playtest-corpus completion
rate + the no-LLM random baseline floor.

Deterministic & hermetic: the corpus is driven by a scripted, network-free
`_ScriptedTransport` (copied from `tests/agents/playtest/test_agent_loop.py`)
under `RouterMode.PASSTHROUGH`, so completion is the ENV's verdict and no
cassette / gateway is touched. The random baseline uses NO router at all.
"""
from __future__ import annotations

import json

from gameforge.agents.playtest_harness import (
    PlaytestCorpusResult,
    random_baseline,
    run_playtest_corpus,
    wilson_ci,
)
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.ir.loader import load_scenario

# Ordered interaction targets to complete `caravan` (talk lincheng → collect from
# emblem_pile → turn_in lincheng) — same sequence proven by the loop test.
_CARAVAN_TARGETS = ["npc:lincheng", "interact:emblem_pile", "npc:lincheng"]


def _resp(text: str) -> ModelResponse:
    return ModelResponse(response_normalized=text)


class _ScriptedTransport:
    """Deterministic, network-free transport that scripts a caravan playthrough
    (verbatim shape from `tests/agents/playtest/test_agent_loop.py`): navigate
    toward the current target until it is in `available_interactions`, then
    interact and advance. Planner/reflect get a constant valid payload."""

    def __init__(self, targets: list[str]) -> None:
        self._targets = list(targets)
        self._idx = 0
        self.node_calls: list[str] = []

    def complete(self, req) -> ModelResponse:  # noqa: ANN001 — Protocol shape
        node = req.agent_node_id
        self.node_calls.append(node)
        if node == "playtest.planner":
            return _resp(json.dumps({"quest": None, "step_kind": "advance"}))
        if node == "playtest.reflect":
            return _resp(json.dumps({"hint": "try another reachable target"}))
        user = req.messages[-1].content
        if self._idx >= len(self._targets):
            return _resp(json.dumps({"kind": "observe"}))
        target = self._targets[self._idx]
        avail_line = ""
        for line in user.splitlines():
            if line.startswith("available_interactions="):
                avail_line = line
                break
        if target in avail_line:
            self._idx += 1
            return _resp(json.dumps({"kind": "interact", "target": target}))
        return _resp(json.dumps({"kind": "navigate_to", "target": target}))


def _scripted_router(tmp_path) -> ModelRouter:
    # PASSTHROUGH → the scripted transport answers every call. The router's
    # session cache makes a second identical caravan chain a byte replay of the
    # first, so the whole corpus completes deterministically with no network.
    return ModelRouter(
        _ScriptedTransport(_CARAVAN_TARGETS),
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
    )


def _caravan_corpus(n: int = 2):
    return [load_scenario("scenarios/caravan.yaml") for _ in range(n)]


# --- wilson_ci numerics ----------------------------------------------------
def test_wilson_ci_degenerate_zero_sample():
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_wilson_ci_all_success_upper_is_one():
    low, high = wilson_ci(10, 10)
    rate = 10 / 10
    assert 0.0 < low < 1.0
    assert low <= rate <= high
    assert high == 1.0  # Wilson upper bound at p̂=1 is exactly 1.0


def test_wilson_ci_no_success_lower_is_zero():
    low, high = wilson_ci(0, 10)
    rate = 0.0
    assert low == 0.0  # Wilson lower bound at p̂=0 is exactly 0.0
    assert low <= rate <= high
    assert 0.0 < high < 1.0


def test_wilson_ci_half_is_symmetric_and_bracketing():
    low, high = wilson_ci(5, 10)
    rate = 0.5
    assert 0.0 < low < rate < high < 1.0
    # symmetric about 0.5 for a symmetric count
    assert abs((low + high) / 2 - 0.5) < 1e-9


def test_wilson_ci_always_within_unit_and_ordered():
    for n in range(1, 12):
        for k in range(0, n + 1):
            low, high = wilson_ci(k, n)
            assert 0.0 <= low <= high <= 1.0
            assert low <= k / n <= high


# --- run_playtest_corpus ---------------------------------------------------
def test_corpus_scripted_completes_all(tmp_path):
    corpus = _caravan_corpus(2)
    router = _scripted_router(tmp_path)

    result = run_playtest_corpus(corpus, router, use_planner=True)

    assert isinstance(result, PlaytestCorpusResult)
    assert result.n_chains == 2
    assert result.completed == 2
    assert result.completion_rate == 1.0
    assert result.mean_steps > 0
    # every chain recorded with its bucket + step count
    assert len(result.per_chain) == 2
    for row in result.per_chain:
        assert row["completed"] is True
        assert row["steps"] > 0
        assert row["length_bucket"] in {"short", "medium", "long"}
    # by_length populated with a per-bucket Wilson CI covering the rate
    assert result.by_length
    total_bucket_n = 0
    for bucket in result.by_length.values():
        total_bucket_n += bucket["n"]
        assert 0.0 <= bucket["ci_low"] <= bucket["rate"] <= bucket["ci_high"] <= 1.0
        assert bucket["completed"] <= bucket["n"]
    assert total_bucket_n == result.n_chains


def test_corpus_flat_ablation_also_completes(tmp_path):
    corpus = _caravan_corpus(1)
    router = _scripted_router(tmp_path)

    result = run_playtest_corpus(corpus, router, use_planner=False)

    assert result.completion_rate == 1.0
    assert result.n_chains == 1


def test_corpus_empty_is_zero_not_crash(tmp_path):
    result = run_playtest_corpus([], _scripted_router(tmp_path))
    assert result.n_chains == 0
    assert result.completion_rate == 0.0
    assert result.mean_steps == 0.0
    assert result.by_length == {}


# --- random_baseline (no-LLM floor) ----------------------------------------
def test_random_baseline_is_a_floor(tmp_path):
    corpus = _caravan_corpus(2)
    scripted = run_playtest_corpus(corpus, _scripted_router(tmp_path))

    baseline = random_baseline(_caravan_corpus(2), seed=0)

    assert isinstance(baseline, PlaytestCorpusResult)
    assert baseline.n_chains == 2
    # The no-LLM random agent can never beat the scripted (=optimal) agent.
    assert baseline.completion_rate <= scripted.completion_rate


def test_random_baseline_is_deterministic():
    a = random_baseline(_caravan_corpus(2), seed=0)
    b = random_baseline(_caravan_corpus(2), seed=0)
    assert a.completion_rate == b.completion_rate
    assert a.mean_steps == b.mean_steps
    assert [r["steps"] for r in a.per_chain] == [r["steps"] for r in b.per_chain]


def test_random_baseline_needs_no_router():
    # A world it can be pointed at; the baseline builds env itself, no router arg.
    corpus = _caravan_corpus(1)
    result = random_baseline(corpus)
    assert result.n_chains == 1
    # sanity: it actually stepped the env (steps recorded, bucketed with CI)
    assert result.per_chain[0]["steps"] > 0
    assert result.by_length
    # snapshot_to_world is the same converter the harness uses per chain
    assert snapshot_to_world(corpus[0]).scenario.scenario_id
