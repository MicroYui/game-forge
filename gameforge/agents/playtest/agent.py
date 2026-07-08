"""PlaytestAgent (M2b-1): the bounded main loop that drives the REAL AureusEnv.

Layering (PRD §7.8): a Planner PROPOSES a high-level subgoal, an Executor
PROPOSES the next atomic action, and the deterministic game engine (AureusEnv)
is the SOLE authority on outcomes — every `done`/`completed` verdict is read back
from the env, never from any model output. A flat (no-planner) ablation runs the
Executor straight off the abstracted state, so the two modes can be compared.

Verifier-grounding: when the engine reports a target `unreachable`, that engine
oracle is cross-checked against the static BFS reachability oracle
(`ground_target`); only when BOTH agree the target is dead does the loop record a
confirmed `unreachable_target` Finding and abort that quest — an LLM's mere
suspicion of unreachability can never abort a quest on its own.

Self-correction: if the deterministic quest state stagnates for several steps the
loop asks the Reflector for one advisory hint, injected into the next Planner
call. A `memory` slot is wired (guarded by a None check) for M2b-2's MemTrace;
this milestone runs it with `memory=None`.
"""
from __future__ import annotations

from typing import Any

from gameforge.agents.playtest.executor import Executor
from gameforge.agents.playtest.grounding import ground_target, make_unreachable_finding
from gameforge.agents.playtest.planner import Planner
from gameforge.agents.playtest.reflect import reflect
from gameforge.agents.playtest.state import abstract_state
from gameforge.contracts.agent_io import PlaytestInput, PlaytestReport
from gameforge.contracts.env_types import Action
from gameforge.contracts.findings import Finding
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.runtime.model_router.router import ModelRouter

# Flat (no-planner) ablation subgoal — the executor works straight off state.
_FLAT_SUBGOAL: dict[str, Any] = {"quest": None, "step_kind": "advance"}

# Consecutive steps of unchanged quest state before asking the Reflector.
_STUCK_LIMIT = 6


def _action_target(action: Action) -> str | None:
    """The entity/location an action targets, if any (for grounding)."""
    target = getattr(action, "target", None)
    if target is None:
        target = getattr(action, "target_id", None)
    return target


class PlaytestAgent:
    def __init__(self) -> None:
        self._planner = Planner()
        self._executor = Executor()

    def run(
        self,
        input: PlaytestInput,
        env: AureusEnv,
        router: ModelRouter,
        *,
        use_planner: bool = True,
        memory: Any = None,
        max_steps: int = 200,
    ) -> PlaytestReport:
        obs = env.observe()
        nav = env.nav_provider()
        action_trace: list[dict] = []
        defect_findings: list[Finding] = []
        aborted_quests: set[str] = set()

        prev_quest_state: dict | None = None
        stuck = 0
        reflect_hint: str | None = None
        result = None

        for _ in range(max_steps):
            state = abstract_state(obs)

            # 1) subgoal (planner PROPOSES; flat ablation skips the planner call).
            if use_planner:
                subgoal, _ = self._planner.plan(state, router, extra=reflect_hint)
            else:
                subgoal = dict(_FLAT_SUBGOAL)

            # 2) atomic action (executor PROPOSES).
            action, _ = self._executor.act(subgoal, state, router)

            # 3) the deterministic engine decides the outcome.
            result = env.step(action)
            obs = result.observation
            action_trace.append(
                {
                    "action": action.model_dump(),
                    "last_action_result": obs.last_action_result,
                    "tick": obs.tick,
                }
            )

            # 4) memory slot (M2b-2 MemTrace; no-op while memory is None).
            if memory is not None:
                memory.record(
                    {
                        "state": state,
                        "action": action.model_dump(),
                        "result": obs.last_action_result,
                    }
                )

            # 5) verifier-grounding: the engine's transient `unreachable` is
            #    cross-checked against the static reachability oracle. Only a
            #    confirmed-dead target aborts its quest.
            target = _action_target(action)
            if target is not None and obs.last_action_result == "unreachable":
                verdict = ground_target(target, obs, nav)
                if verdict.action == "abort_quest":
                    defect_findings.append(
                        make_unreachable_finding(target, env.state_hash())
                    )
                    quest = self._current_quest(subgoal, obs)
                    if quest is not None:
                        aborted_quests.add(quest)
                    if self._all_remaining_aborted(obs, aborted_quests):
                        break  # nothing left the run can make progress on

            # 6) completion is the ENV's verdict.
            if result.done:
                break

            # 7) stuck detection → self-correction hint for the next planner call.
            if prev_quest_state is not None and obs.quest_state == prev_quest_state:
                stuck += 1
            else:
                stuck = 0
                reflect_hint = None
            if stuck >= _STUCK_LIMIT and use_planner:
                reflect_hint, _ = reflect(action_trace[-_STUCK_LIMIT:], router)
                stuck = 0
            prev_quest_state = obs.quest_state

        completed = result.done if result is not None else env._all_quests_completed()
        return PlaytestReport(
            action_trace=action_trace,
            defect_findings=defect_findings,
            completed=completed,
        )

    # --- helpers -----------------------------------------------------------
    def _current_quest(self, subgoal: dict, obs) -> str | None:
        """The quest the current action was working toward: the subgoal's quest
        if named, else the first active quest, else the first non-completed
        known quest.
        # TODO(M2b-2): multi-quest — attribute the abort to the quest whose
        # current step references `target`, not active_quests[0].
        """
        quest = subgoal.get("quest")
        if isinstance(quest, str) and quest:
            return quest
        if obs.active_quests:
            return obs.active_quests[0]
        for qid in obs.known_quests:
            st = obs.quest_state.get(qid, {})
            if st.get("status") != "completed":
                return qid
        return None

    def _all_remaining_aborted(self, obs, aborted_quests: set[str]) -> bool:
        """True once every not-yet-completed quest has been aborted (so the run
        can honestly stop — it can no longer reach `done`)."""
        remaining = [
            qid
            for qid in obs.known_quests
            if obs.quest_state.get(qid, {}).get("status") != "completed"
        ]
        return bool(remaining) and all(qid in aborted_quests for qid in remaining)
