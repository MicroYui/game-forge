"""PlaytestAgent main-loop integration test (M2b-1 Task 6).

Drives the REAL `AureusEnv` through the `caravan` quest (talk → collect →
turn_in, no fight) to `done`, in both the planner/executor layered mode and the
flat (no-planner) ablation.

Determinism/hermeticity: a scripted stub transport (no network) supplies the
model outputs. The stub decides the executor's next atomic action purely from
the deterministic abstracted STATE it is handed (navigate toward the current
interaction target until it appears in `available_interactions`, then interact)
— it mirrors the M0a/M0b `ScriptedDriver`'s talk/collect/turn_in macro sequence,
whose atomic trajectory was recorded by running `ScriptedDriver` against this
same env. CRUCIALLY the stub never decides completion: `report.completed` is read
back from `env.done` (`AureusEnv._all_quests_completed()`), never from the stub.

Caravan is used (not outpost) as the primary completion scenario per the task
brief's allowance: it is a clean 3-step talk/collect/turn_in quest with no fight
step, so the scripted sequence stays short and robust. It loads via the YAML
`load_scenario` path (same as `apps.cli.run_slice.run_slice`).
"""
from __future__ import annotations

import json

from gameforge.agents.playtest.agent import PlaytestAgent
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.agent_io import PlaytestInput
from gameforge.contracts.model_router import ModelResponse
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.ir.loader import load_scenario

# Ordered interaction targets to complete `caravan` (talk lincheng → collect from
# emblem_pile → turn_in lincheng). Sourced by running the existing ScriptedDriver
# against this env (see run_slice('scenarios/caravan.yaml')).
_CARAVAN_TARGETS = ["npc:lincheng", "interact:emblem_pile", "npc:lincheng"]


def _resp(text: str) -> ModelResponse:
    return ModelResponse(response_normalized=text)


class _ScriptedTransport:
    """Deterministic, network-free transport that scripts a playthrough.

    Executor requests are answered by reading the abstracted state the agent
    passes in: navigate toward the current target until it shows up in
    `available_interactions` (i.e. the player is standing on it), then interact
    and advance to the next target. Planner/reflect requests get a valid,
    constant JSON payload — the executor decision does not depend on them, so
    the layered and flat modes issue the identical atomic action stream.
    """

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
        # playtest.executor: decide the next atomic action from the state text.
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


def _caravan_env() -> AureusEnv:
    snapshot = load_scenario("scenarios/caravan.yaml")
    world = snapshot_to_world(snapshot)
    env = AureusEnv(world)
    env.reset(world.scenario.scenario_id, 0)
    return env


def _router(transport: _ScriptedTransport, tmp_path) -> ModelRouter:
    # PASSTHROUGH → the scripted transport answers every call; no cassette, no
    # network. The store is required by the ctor but never read in this mode.
    return ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)


def test_playtest_agent_completes_caravan_with_planner(tmp_path):
    env = _caravan_env()
    transport = _ScriptedTransport(_CARAVAN_TARGETS)
    router = _router(transport, tmp_path)

    report = PlaytestAgent().run(
        PlaytestInput(scenario="caravan", seed=0), env, router, use_planner=True
    )

    # Completion is the ENV's verdict, not the stub's.
    assert report.completed is True
    assert env._all_quests_completed() is True
    assert report.defect_findings == []
    assert report.action_trace  # the loop actually stepped the env
    # Layered mode really used the planner node.
    assert "playtest.planner" in transport.node_calls
    assert "playtest.executor" in transport.node_calls


def test_playtest_agent_completes_caravan_flat_ablation(tmp_path):
    env = _caravan_env()
    transport = _ScriptedTransport(_CARAVAN_TARGETS)
    router = _router(transport, tmp_path)

    report = PlaytestAgent().run(
        PlaytestInput(scenario="caravan", seed=0), env, router, use_planner=False
    )

    assert report.completed is True
    assert env._all_quests_completed() is True
    # Flat ablation drives the executor directly — the planner node is NEVER hit.
    assert "playtest.planner" not in transport.node_calls
    assert "playtest.executor" in transport.node_calls
