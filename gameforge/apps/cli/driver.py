"""Scripted deterministic driver — the M0a planner stand-in.

Compiles the high-level macros (talk / collect / turn_in) into atomic Env actions,
using the current Observation to know the active step and to detect arrival. The
real LLM Playtest Agent (planner/executor + memory + self-correction) is M2; this
plain driver exists only to close the M0a vertical slice.
"""

from __future__ import annotations

from gameforge.contracts.env_types import parse_action
from gameforge.contracts.world import QuestSpec, WorldConfig
from gameforge.game.aureus.kernel import AureusEnv

_NAV_BUDGET = 500
_STEP_BUDGET = 200


class ScriptedDriver:
    def __init__(self, world_config: WorldConfig) -> None:
        self._quests: dict[str, QuestSpec] = {q.quest_id: q for q in world_config.quests}
        self._source_by_item: dict[str, str] = {}
        for p in world_config.placements:
            item = p.attrs.get("yields_item")
            if item:
                self._source_by_item.setdefault(item, p.entity_id)

    def _step_spec(self, quest: QuestSpec, step_id: str):
        return next((s for s in quest.steps if s.step_id == step_id), None)

    def _go_and_interact(self, env: AureusEnv, target: str, trajectory: list[str]):
        for _ in range(_NAV_BUDGET):
            r = env.step(parse_action({"kind": "navigate_to", "target": target}))
            trajectory.append(f"navigate_to {target}")
            res = r.observation.last_action_result
            if res == "arrived":
                break
            if res in ("unreachable", "unknown_target"):
                return r
        r = env.step(parse_action({"kind": "interact", "target": target}))
        trajectory.append(f"interact {target}")
        return r

    def run(self, env: AureusEnv) -> dict:
        trajectory: list[str] = []
        for _ in range(_STEP_BUDGET):
            obs = env.observe()
            pending = sorted(
                qid for qid, qs in obs.quest_state.items() if qs["status"] != "completed"
            )
            if not pending:
                break
            qid = pending[0]
            qs = obs.quest_state[qid]
            if qs["step_kind"] is None:
                break
            quest = self._quests[qid]
            step = self._step_spec(quest, qs["step_id"])
            if step is None:
                break
            if step.kind == "collect":
                target = self._source_by_item.get(step.item)
            else:  # talk / turn_in
                target = step.target or quest.giver
            if target is None:
                break
            before = qs["current_step"]
            self._go_and_interact(env, target, trajectory)
            # guard against no-progress loops
            if env.observe().quest_state[qid]["current_step"] == before and \
                    env.observe().quest_state[qid]["status"] != "completed":
                break

        final = env.observe()
        completed = bool(final.quest_state) and all(
            qs["status"] == "completed" for qs in final.quest_state.values()
        )
        return {
            "completed": completed,
            "trajectory": trajectory,
            "final_hash": env.state_hash(),
            "ticks": final.tick,
        }
