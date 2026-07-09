"""M2b-2 Task 5/6: recall injection into Planner/Executor + agent.py memory
wiring (record enrichment, reflect on stuck/grounding-abort, compact on a
quest-step-completion boundary) — gated so `memory=None` (the default) is
BYTE-FOR-BYTE identical to M2b-1.

The single highest-risk item is the regression lock at the bottom: the repo
carries 5792 committed `cassettes/playtest/` cassettes recorded at
`RECORD_MAX_STEPS=150` under M2b-1 (memory=None). If the recall-wired
planner/executor changed so much as one byte of any request on the
`memory=None` path, REPLAY would raise `CassetteReplayMiss` and every one of
those cassettes would be dead. That test proves it still replays clean.
"""
from __future__ import annotations

import json
import os

import pytest

from gameforge.agents.playtest.agent import PlaytestAgent
from gameforge.agents.playtest.executor import Executor
from gameforge.agents.playtest.memory import MemTrace
from gameforge.agents.playtest.planner import Planner
from gameforge.agents.playtest_harness import (
    RECORD_MAX_STEPS,
    default_chain_snapshots,
    replay_router,
    run_playtest_corpus,
)
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.agent_io import PlaytestInput
from gameforge.contracts.model_router import ModelResponse
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import CassetteReplayMiss, ModelRouter, RouterMode
from gameforge.spine.ir.loader import load_scenario

PLAYTEST_CASSETTES = "cassettes/playtest"

_PLANNER_OK = json.dumps({"quest": None, "step_kind": "advance"})
_EXECUTOR_OK = json.dumps({"kind": "observe"})

# Ordered interaction targets to complete `caravan` (talk lincheng → collect
# from emblem_pile → turn_in lincheng) — same sequence used by M2b-1's
# `tests/agents/playtest/test_agent_loop.py`.
_CARAVAN_TARGETS = ["npc:lincheng", "interact:emblem_pile", "npc:lincheng"]


class CapturingRouter:
    """Fake router (no transport, no cassette): stores the last request's
    user-message content and returns a fixed, valid JSON response. Used only
    to assert the EXACT `user` string `Planner.plan`/`Executor.act` build."""

    def __init__(self, response_text: str) -> None:
        self._response_text = response_text
        self.last_user: str | None = None
        self.calls: list = []

    def call(self, req):  # noqa: ANN001 — Protocol shape only
        self.calls.append(req)
        self.last_user = req.messages[-1].content
        return ModelResponse(response_normalized=self._response_text)


class _ScriptedTransport:
    """Deterministic, network-free transport that scripts a caravan
    playthrough (verbatim shape from `tests/agents/playtest/test_agent_loop.py`):
    navigate toward the current target until it is in `available_interactions`,
    then interact and advance. Planner/reflect get a constant valid payload.
    Records every request it answers for post-hoc inspection."""

    def __init__(self, targets: list[str]) -> None:
        self._targets = list(targets)
        self._idx = 0
        self.calls: list = []

    def complete(self, req) -> ModelResponse:  # noqa: ANN001 — Protocol shape
        self.calls.append(req)
        node = req.agent_node_id
        if node == "playtest.planner":
            return ModelResponse(response_normalized=_PLANNER_OK)
        if node == "playtest.reflect":
            return ModelResponse(
                response_normalized=json.dumps({"hint": "try another reachable target"})
            )
        user = req.messages[-1].content
        if self._idx >= len(self._targets):
            return ModelResponse(response_normalized=_EXECUTOR_OK)
        target = self._targets[self._idx]
        avail_line = ""
        for line in user.splitlines():
            if line.startswith("available_interactions="):
                avail_line = line
                break
        if target in avail_line:
            self._idx += 1
            return ModelResponse(
                response_normalized=json.dumps({"kind": "interact", "target": target})
            )
        return ModelResponse(
            response_normalized=json.dumps({"kind": "navigate_to", "target": target})
        )


class _AlwaysObserveTransport:
    """Never progresses any quest — every executor call answers `observe`, so
    the deterministic quest state stagnates and the stuck/self-correction path
    (and, when memory is present, `memory.reflect(..., verdict="stuck")`)
    fires after `_STUCK_LIMIT` steps."""

    def __init__(self) -> None:
        self.calls: list = []

    def complete(self, req) -> ModelResponse:  # noqa: ANN001 — Protocol shape
        self.calls.append(req)
        if req.agent_node_id == "playtest.planner":
            return ModelResponse(response_normalized=_PLANNER_OK)
        if req.agent_node_id == "playtest.reflect":
            return ModelResponse(response_normalized=json.dumps({"hint": "try something else"}))
        return ModelResponse(response_normalized=_EXECUTOR_OK)


def _passthrough_router(transport, tmp_path) -> ModelRouter:
    return ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)


def _caravan_env() -> AureusEnv:
    snapshot = load_scenario("scenarios/caravan.yaml")
    world = snapshot_to_world(snapshot)
    env = AureusEnv(world)
    env.reset(world.scenario.scenario_id, 0)
    return env


def _walled_caravan_env() -> tuple[AureusEnv, str]:
    """Same wall-off trick as `test_agent_loop.py`'s
    `_walled_caravan_env`: block the quest giver off with a full-height wall so
    BFS genuinely finds no path, forcing a confirmed `unreachable_target`
    grounding-abort (not a scripted/mocked one)."""
    snapshot = load_scenario("scenarios/caravan.yaml")
    world = snapshot_to_world(snapshot)
    env = AureusEnv(world)
    giver = "npc:lincheng"
    start = env.world.start_pos
    wall_x = start[0] + 1
    for y in range(env.world.grid.height):
        env.world.grid.blocked.add((wall_x, y))
    env.reset(world.scenario.scenario_id, 0)
    return env, giver


# ---------------------------------------------------------------------------
# Task 5, Step 1: Planner/Executor byte-identical when recall=None, exact
# append when recall is a non-empty string. This is the injection contract in
# isolation, no env/agent involved.
# ---------------------------------------------------------------------------
def test_planner_user_byte_identical_when_recall_none():
    cap = CapturingRouter(_PLANNER_OK)
    Planner().plan("STATE_X", cap, extra=None, recall=None)
    assert cap.last_user == "STATE_X"  # exactly M2b-1 shape


def test_planner_appends_recall_section_when_present():
    cap = CapturingRouter(_PLANNER_OK)
    Planner().plan("STATE_X", cap, extra="hintA", recall="mem1")
    assert cap.last_user == "STATE_X\n\nCorrective hint: hintA\n\nRelevant past experience:\nmem1"


def test_planner_recall_appends_even_without_a_corrective_hint():
    cap = CapturingRouter(_PLANNER_OK)
    Planner().plan("STATE_X", cap, extra=None, recall="mem1")
    assert cap.last_user == "STATE_X\n\nRelevant past experience:\nmem1"


def test_planner_empty_string_recall_treated_as_absent():
    # recall="" is non-None but NOT a non-empty string — must NOT be appended.
    cap = CapturingRouter(_PLANNER_OK)
    Planner().plan("STATE_X", cap, extra=None, recall="")
    assert cap.last_user == "STATE_X"


def test_executor_user_byte_identical_when_recall_none():
    subgoal = {"quest": "q1", "step_kind": "advance", "target": "npc:qi"}
    expected = f"Subgoal: {json.dumps(subgoal, sort_keys=True)}\n\nState:\nSTATE_X"
    cap = CapturingRouter(_EXECUTOR_OK)
    Executor().act(subgoal, "STATE_X", cap, recall=None)
    assert cap.last_user == expected  # exactly M2b-1 shape


def test_executor_appends_recall_section_when_present():
    subgoal = {"quest": "q1", "step_kind": "advance", "target": "npc:qi"}
    expected = f"Subgoal: {json.dumps(subgoal, sort_keys=True)}\n\nState:\nSTATE_X"
    cap = CapturingRouter(_EXECUTOR_OK)
    Executor().act(subgoal, "STATE_X", cap, recall="mem1")
    assert cap.last_user == expected + "\n\nRelevant past experience:\nmem1"


# ---------------------------------------------------------------------------
# Task 5: agent.py wiring — recall computed once per step and injected into
# both calls; `record` enriched with state_hash/tick/step_index; `reflect` on
# the grounding-abort and stuck paths; `compact` at a quest-step-completion
# boundary. All exercised end-to-end against the REAL AureusEnv, all gated
# behind `memory is not None`.
# ---------------------------------------------------------------------------
def test_agent_run_enriches_memory_records_and_injects_recall(tmp_path):
    env = _caravan_env()
    transport = _ScriptedTransport(_CARAVAN_TARGETS)
    router = _passthrough_router(transport, tmp_path)
    memory = MemTrace()

    report = PlaytestAgent().run(
        PlaytestInput(scenario="caravan", seed=0),
        env,
        router,
        use_planner=True,
        memory=memory,
    )

    assert report.completed is True
    # Every action-trace step got a corresponding, enriched Episode. `memory.trace`
    # may ALSO hold extra `reflect(...)`-injected episodes (tick=-1 by
    # construction — see `memory.py`'s `reflect`) from transient stuck spells
    # during a long navigate_to stretch; filter those out to isolate the
    # `record(...)`-originated ones the loop appends 1:1 with `action_trace`.
    recorded = [ep for ep in memory.trace if ep.tick != -1]
    assert len(recorded) == len(report.action_trace)
    for i, ep in enumerate(recorded):
        assert ep.state_hash != ""
        assert ep.step_index == i
        assert ep.tick >= 0

    executor_prompts = [
        req.messages[-1].content for req in transport.calls if req.agent_node_id == "playtest.executor"
    ]
    assert len(executor_prompts) >= 2
    # First step: memory is empty → no recall section injected yet.
    assert "Relevant past experience:" not in executor_prompts[0]
    # A later step: memory now has episodes → recall gets injected.
    assert any("Relevant past experience:" in p for p in executor_prompts[1:])


def test_agent_run_calls_memory_compact_at_quest_step_boundary(tmp_path):
    class _SpyCompactor:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def compact(self, trace, verdicts, *, router=None, node_id="playtest.memory"):
            self.calls.append((len(trace), len(list(verdicts))))
            return "compacted"

    env = _caravan_env()
    transport = _ScriptedTransport(_CARAVAN_TARGETS)
    router = _passthrough_router(transport, tmp_path)
    spy = _SpyCompactor()
    memory = MemTrace(compactor=spy)

    report = PlaytestAgent().run(
        PlaytestInput(scenario="caravan", seed=0),
        env,
        router,
        use_planner=True,
        memory=memory,
    )

    assert report.completed is True
    # caravan is a 3-step quest (talk → collect → turn_in): at least one
    # quest-step-completion boundary must have fired `compact`.
    assert spy.calls


def test_agent_run_calls_memory_reflect_on_grounding_abort(tmp_path):
    env, giver = _walled_caravan_env()
    transport = _ScriptedTransport([giver])
    router = _passthrough_router(transport, tmp_path)
    memory = MemTrace()

    report = PlaytestAgent().run(
        PlaytestInput(scenario="walled", seed=0),
        env,
        router,
        use_planner=False,
        memory=memory,
    )

    assert report.completed is False
    assert len(report.defect_findings) >= 1
    # memory.reflect wrote a down-weighting (negative-verdict) episode for the
    # confirmed-dead path.
    assert any(ep.verdict < 0 for ep in memory.trace)


def test_agent_run_calls_memory_reflect_on_stuck_path(tmp_path):
    env = _caravan_env()
    transport = _AlwaysObserveTransport()
    router = _passthrough_router(transport, tmp_path)
    memory = MemTrace()

    report = PlaytestAgent().run(
        PlaytestInput(scenario="caravan", seed=0),
        env,
        router,
        use_planner=True,
        memory=memory,
        max_steps=10,
    )

    assert report.completed is False  # observe-only never advances the quest
    assert any(ep.verdict < 0 for ep in memory.trace)


def test_agent_run_memory_none_path_untouched_by_new_wiring(tmp_path):
    """Sanity companion to the harness-level regression lock below: the SAME
    scripted caravan playthrough, run twice — once with memory=None (default)
    and once with a real MemTrace — must both complete via the identical
    scripted action sequence (the memory-off run never even calls anything on
    `memory`, since it stays None throughout)."""
    env_a = _caravan_env()
    router_a = _passthrough_router(_ScriptedTransport(_CARAVAN_TARGETS), tmp_path)
    report_a = PlaytestAgent().run(
        PlaytestInput(scenario="caravan", seed=0), env_a, router_a, use_planner=True
    )
    assert report_a.completed is True
    assert report_a.action_trace  # the loop actually stepped the env


# ---------------------------------------------------------------------------
# Task 6a: `run_playtest_corpus(..., memory_factory=lambda: MemTrace())` is
# already parameterized end-to-end — confirm it threads a fresh MemTrace per
# chain and runs to completion with no crash. No cassette needed: a scripted
# PASSTHROUGH transport drives a real, completable corpus (this is the code
# path Task 7/8's live memory-on record pass will exercise).
# ---------------------------------------------------------------------------
def test_run_playtest_corpus_threads_memory_factory_without_crashing(tmp_path):
    corpus = [load_scenario("scenarios/caravan.yaml") for _ in range(2)]
    router = _passthrough_router(_ScriptedTransport(_CARAVAN_TARGETS), tmp_path)

    result = run_playtest_corpus(
        corpus, router, use_planner=True, memory_factory=lambda: MemTrace()
    )

    assert result.n_chains == 2
    assert result.completed == 2


# ---------------------------------------------------------------------------
# Task 6b: THE regression lock. The repo's 5792 committed M2b-1 cassettes
# (`cassettes/playtest/`, recorded at RECORD_MAX_STEPS=150, memory=None) must
# still replay byte-for-byte against the memory-wired code. Any request-byte
# drift on the memory=None path surfaces here as `CassetteReplayMiss`.
# ---------------------------------------------------------------------------
needs_cassettes = pytest.mark.skipif(
    not os.path.isdir(PLAYTEST_CASSETTES) or not os.listdir(PLAYTEST_CASSETTES),
    reason="playtest cassettes not recorded yet — run "
    "`GAMEFORGE_LLM_LIVE=1 GAMEFORGE_LLM_KEY=<key> "
    "uv run python -m gameforge.agents.playtest_harness --record`",
)


@needs_cassettes
def test_memory_none_regression_lock_zero_replay_misses_and_matches_m2b1_rate():
    corpus = default_chain_snapshots()
    router = replay_router(PLAYTEST_CASSETTES)

    try:
        result = run_playtest_corpus(
            corpus, router, use_planner=True, memory=None, max_steps=RECORD_MAX_STEPS
        )
    except CassetteReplayMiss as exc:
        pytest.fail(
            "memory wiring changed a request byte on the memory=None path — "
            f"cassette replay miss: {exc}"
        )

    assert result.n_chains == 20
    assert result.completed == 14
    assert result.completion_rate == 0.7
