"""M2b-1 Task 8: end-to-end smoke test tying together the whole Playtest slice
built in Tasks 1-7 — proof-of-life for the acceptance milestone, not a unit test
for any one piece (those live in the other `tests/agents/playtest/*` modules and
`tests/agents/test_playtest_harness.py`).

Three claims, each load-bearing for the M2b-1 acceptance:

1. The scripted Playtest agent (`run_playtest_corpus`, `use_planner=True`) closes
   the loop on a real scenario (`caravan`) end-to-end through `AureusEnv`:
   `completion_rate == 1.0`.
2. The agent is non-trivial: a no-LLM `random_baseline` on the SAME scenario
   cannot close the loop within the step budget (`completion_rate < 1.0`) — the
   scripted agent's 1.0 is not a floor artifact of a trivial scenario.
3. The planner/executor ablation mechanism runs end-to-end in BOTH positions
   (`use_planner=True` and `use_planner=False`), each producing a
   `PlaytestCorpusResult` — proving the ablation switch is wired through the
   harness, not just the lower-level `PlaytestAgent.run` (already covered by
   `tests/agents/playtest/test_agent_loop.py`).

Deterministic & hermetic throughout: REPLAY-mode `ModelRouter` fed a scripted,
network-free `_ScriptedTransport` (verbatim shape from `test_agent_loop.py` /
`test_playtest_harness.py`, dispatching on `req.agent_node_id`); the random
baseline touches no router at all. No live LLM call anywhere in this module.
"""
from __future__ import annotations

import json

from gameforge.agents.playtest_harness import (
    PlaytestCorpusResult,
    random_baseline,
    run_playtest_corpus,
)
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
    # PASSTHROUGH → the scripted transport answers every call, no cassette/network.
    return ModelRouter(
        _ScriptedTransport(_CARAVAN_TARGETS),
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
    )


def _caravan_snapshot():
    return load_scenario("scenarios/caravan.yaml")


def test_scripted_agent_completes_caravan_end_to_end(tmp_path):
    snap = _caravan_snapshot()
    router = _scripted_router(tmp_path)

    result = run_playtest_corpus([snap], router, use_planner=True)

    assert isinstance(result, PlaytestCorpusResult)
    assert result.completion_rate == 1.0


def test_scripted_agent_beats_random_floor_on_same_scenario():
    snap = _caravan_snapshot()

    baseline = random_baseline([snap], seed=0)

    # The no-LLM random agent cannot close the loop within the step budget on
    # this scenario — proves the scripted agent's 1.0 above is not a trivial
    # floor artifact but genuine non-random task closure.
    assert baseline.completion_rate < 1.0


def test_ablation_mechanism_runs_both_positions_through_harness(tmp_path):
    snap = _caravan_snapshot()

    layered = run_playtest_corpus([snap], _scripted_router(tmp_path), use_planner=True)
    flat = run_playtest_corpus([snap], _scripted_router(tmp_path), use_planner=False)

    assert isinstance(layered, PlaytestCorpusResult)
    assert isinstance(flat, PlaytestCorpusResult)
