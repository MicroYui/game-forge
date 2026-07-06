"""Scripted deterministic driver — the M0a/M0b planner stand-in.

Compiles the high-level macros (talk / collect / turn_in / fight, plus optional
post-quest economy/gacha macros) into atomic Env actions, using the current
Observation to know the active step and to detect arrival/progress. The real
LLM Playtest Agent (planner/executor + memory + self-correction) is M2; this
plain driver exists only to close the M0a/M0b vertical slices.
"""

from __future__ import annotations

from gameforge.contracts.env_types import parse_action
from gameforge.contracts.world import (
    BattleEncounterSpec, GachaPoolSpec, QuestSpec, ShopSpec, WorldConfig,
)
from gameforge.game.aureus.kernel import AureusEnv

_NAV_BUDGET = 500
_STEP_BUDGET = 200
_COMBAT_BUDGET = 200


class ScriptedDriver:
    def __init__(self, world_config: WorldConfig) -> None:
        self._quests: dict[str, QuestSpec] = {q.quest_id: q for q in world_config.quests}
        self._source_by_item: dict[str, str] = {}
        for p in world_config.placements:
            item = p.attrs.get("yields_item")
            if item:
                self._source_by_item.setdefault(item, p.entity_id)
        self._encounters: dict[str, BattleEncounterSpec] = {
            e.encounter_id: e for e in world_config.encounters
        }
        self._shops: dict[str, ShopSpec] = {s.shop_id: s for s in world_config.shops}
        self._gacha_pools: dict[str, GachaPoolSpec] = {
            g.gacha_pool_id: g for g in world_config.gacha_pools
        }
        self._systems: set[str] = set()

    def _step_spec(self, quest: QuestSpec, step_id: str):
        return next((s for s in quest.steps if s.step_id == step_id), None)

    # --- navigation ---
    def _navigate(self, env: AureusEnv, target: str, trajectory: list[str]) -> str:
        last = ""
        for _ in range(_NAV_BUDGET):
            r = env.step(parse_action({"kind": "navigate_to", "target": target}))
            trajectory.append(f"navigate_to {target}")
            last = r.observation.last_action_result
            if last in ("arrived", "unreachable", "unknown_target"):
                break
        return last

    def _go_and_interact(self, env: AureusEnv, target: str, trajectory: list[str]):
        last = self._navigate(env, target, trajectory)
        if last in ("unreachable", "unknown_target"):
            return None
        r = env.step(parse_action({"kind": "interact", "target": target}))
        trajectory.append(f"interact {target}")
        return r

    # --- combat macro ---
    def _do_fight(self, env: AureusEnv, encounter_id: str | None, trajectory: list[str]) -> None:
        """Navigate to the encounter's placement, then repeat `attack` on the
        alive monster (advancing to the next one in the encounter's monster
        list on a kill) until the encounter is won (or the combat budget runs
        out, as a defensive guard against a stuck/broken encounter)."""
        if encounter_id is None:
            return
        last = self._navigate(env, encounter_id, trajectory)
        if last in ("unreachable", "unknown_target"):
            return
        encounter = self._encounters.get(encounter_id)
        monsters = list(encounter.monsters) if encounter is not None else []
        if not monsters:
            return
        idx = 0
        for _ in range(_COMBAT_BUDGET):
            if idx >= len(monsters):
                break
            target_id = monsters[idx]
            r = env.step(parse_action({"kind": "attack", "target_id": target_id}))
            trajectory.append(f"attack {target_id}")
            res = r.observation.last_action_result
            if res == "victory":
                self._systems.add("combat")
                break
            if res == "kill":
                self._systems.add("combat")
                idx += 1
                continue
            if res in ("hit", "miss"):
                continue
            break  # not_in_combat / no_target / unknown result: bail out

    # --- economy/gacha macros (exercised once, after quests are driven) ---
    def _maybe_buy_shop(self, env: AureusEnv, trajectory: list[str]) -> None:
        gold = env.observe().player_stats.get("gold", 0)
        for shop_id, shop in self._shops.items():
            entry = next((e for e in shop.entries if e.price <= gold), None)
            if entry is None:
                continue
            r = env.step(parse_action(
                {"kind": "buy", "shop_id": shop_id, "item_id": entry.item, "count": 1}
            ))
            trajectory.append(f"buy {shop_id}:{entry.item}")
            if r.observation.last_action_result == "bought":
                self._systems.add("economy")
            return

    def _maybe_buy_gacha(self, env: AureusEnv, trajectory: list[str]) -> None:
        gold = env.observe().player_stats.get("gold", 0)
        for pool_id, pool in self._gacha_pools.items():
            if pool.cost > gold:
                continue
            item_id = pool.entries[0].item if pool.entries else ""
            r = env.step(parse_action(
                {"kind": "buy", "shop_id": pool_id, "item_id": item_id, "count": 1}
            ))
            trajectory.append(f"buy {pool_id}:gacha")
            if r.observation.last_action_result == "pulled":
                self._systems.add("gacha")
            return

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
            self._systems.add("quest")
            before = qs["current_step"]
            if step.kind == "fight":
                self._do_fight(env, step.encounter, trajectory)
            else:
                if step.kind == "collect":
                    target = self._source_by_item.get(step.item)
                else:  # talk / turn_in
                    target = step.target or quest.giver
                if target is None:
                    break
                self._go_and_interact(env, target, trajectory)
            # guard against no-progress loops
            if env.observe().quest_state[qid]["current_step"] == before and \
                    env.observe().quest_state[qid]["status"] != "completed":
                break

        self._maybe_buy_shop(env, trajectory)
        self._maybe_buy_gacha(env, trajectory)

        final = env.observe()
        completed = bool(final.quest_state) and all(
            qs["status"] == "completed" for qs in final.quest_state.values()
        )
        return {
            "completed": completed,
            "trajectory": trajectory,
            "final_hash": env.state_hash(),
            "ticks": final.tick,
            "systems_exercised": sorted(self._systems),
        }
