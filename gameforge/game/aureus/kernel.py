"""Aureus deterministic kernel — quest state machine + Env implementation (M0a).

Tick-based, seed-ized, headless authoritative logic. Implements the atomic
Env actions needed for talk/collect/turn_in; combat/economy atomics are declared
in the contract and answered `unsupported_in_m0a` (impl@M0b — declared, not cut).

state_hash covers authoritative state only (contract §4.4): tick, player state,
quest states, inventory, world-object states, monster states, event flags, rng —
NOT logs / render / wall-clock / debug.
"""

from __future__ import annotations

import random

from gameforge.contracts.canonical import compute_snapshot_id
from gameforge.contracts.env_types import (
    Action, Attack, Buy, CastSkill, Choose, Equip, Interact, NavigateTo,
    Observation, Observe, Pickup, Sell, StepResult, Use, Wait,
)
from gameforge.contracts.world import WorldConfig
from gameforge.env.base import Environment
from gameforge.game.aureus.grid import AureusNav
from gameforge.game.aureus.world import AureusWorld

Pos = tuple[int, int]
_UNSUPPORTED = (Attack, CastSkill, Use, Equip, Buy, Sell)


class AureusEnv(Environment):
    def __init__(self, world_config: WorldConfig) -> None:
        self.world = AureusWorld(world_config)
        self.seed = 0
        self._reset_state()

    # --- lifecycle ---
    def _reset_state(self) -> None:
        self.tick = 0
        self.player_pos: Pos = self.world.start_pos
        self.player_stats: dict = {"hp": 100, "gold": 0}
        self.inventory: dict[str, int] = {}
        self.gathered: set[str] = set()
        self.quest_states: dict[str, dict] = {}
        for qid in self.world.quests:
            self.quest_states[qid] = {"status": "known", "current_step": 0}
        self.rng = random.Random(self.seed)
        self.rng_draws = 0
        self.logs: list[str] = []
        self._last_result = "reset"

    def reset(self, scenario: str, seed: int) -> Observation:
        self.seed = int(seed)
        self._reset_state()
        return self.observe()

    # --- step dispatch ---
    def step(self, action: Action) -> StepResult:
        if isinstance(action, Observe):
            self._last_result = "observed"
        elif isinstance(action, Wait):
            self.tick += max(0, int(action.ticks))
            self._last_result = "waited"
        elif isinstance(action, NavigateTo):
            self._navigate_to(action.target)
        elif isinstance(action, Interact):
            self._interact(action.target)
        elif isinstance(action, Pickup):
            self._pickup(action.item_id)
        elif isinstance(action, Choose):
            self._last_result = "no_dialogue_options"
        elif isinstance(action, _UNSUPPORTED):
            self._last_result = "unsupported_in_m0a"
        else:  # pragma: no cover - exhaustive union
            self._last_result = "unknown_action"

        obs = self.observe()
        done = self._all_quests_completed()
        return StepResult(observation=obs, reward=0.0, done=done, info={})

    # --- movement ---
    def _navigate_to(self, target: str) -> None:
        dst = self.world.pos_of(target)
        if dst is None:
            self._last_result = "unknown_target"
            return
        if self.player_pos == dst:
            self._last_result = "arrived"
            return
        path = self.world.grid.shortest_path(self.player_pos, dst)
        if path is None or len(path) < 2:
            self._last_result = "unreachable"
            return
        self.player_pos = path[1]  # one cell per tick
        self.tick += 1
        self._last_result = "arrived" if self.player_pos == dst else "moving"

    # --- interaction ---
    def _interact(self, target: str) -> None:
        pos = self.world.pos_of(target)
        if pos is None:
            self._last_result = "unknown_target"
            return
        if self.player_pos != pos:
            self._last_result = "not_in_range"
            return
        self.tick += 1
        if target in self.world.interactables:
            self._gather(target)
            return
        if target in self.world.npc_ids:
            self._talk_to_npc(target)
            return
        self._last_result = "nothing_here"

    def _talk_to_npc(self, npc_id: str) -> None:
        result = "nothing_here"
        for qid, quest in self.world.quests.items():
            state = self.quest_states[qid]
            if state["status"] == "known" and quest.giver == npc_id:
                state["status"] = "active"
                self.logs.append(f"quest {qid} accepted")
                result = "quest_accepted"
            if state["status"] != "active":
                continue
            cur = self._current_step(quest, state)
            if cur is None:
                continue
            if cur.kind == "talk" and cur.target == npc_id:
                self._advance(qid, quest, state)
                self.logs.append(f"quest step {cur.step_id} completed")
                result = "talk_done" if result != "quest_accepted" else "quest_accepted"
            elif cur.kind == "turn_in" and cur.target == npc_id:
                self._advance(qid, quest, state)
                self.logs.append(f"quest step {cur.step_id} completed")
                result = "quest_completed"
        self._last_result = result

    def _gather(self, interactable_id: str) -> None:
        if interactable_id in self.gathered:  # depletable: one-time gather node
            self._last_result = "already_gathered"
            return
        item, count = self.world.grants_item(interactable_id)
        if item is None:
            self._last_result = "nothing_here"
            return
        self.inventory[item] = self.inventory.get(item, 0) + count
        self.gathered.add(interactable_id)
        self.logs.append(f"item {item} acquired x{count}")
        self._last_result = "gathered"
        self._maybe_advance_collect(item)

    def _pickup(self, item_id: str) -> None:
        for iid, it in self.world.interactables.items():
            if it["pos"] == self.player_pos and it["yields_item"] == item_id:
                self.tick += 1
                self._gather(iid)
                return
        self.tick += 1
        self._last_result = "nothing_to_pick"

    # --- quest state machine ---
    def _current_step(self, quest, state):
        idx = state["current_step"]
        if 0 <= idx < len(quest.steps):
            return quest.steps[idx]
        return None

    def _advance(self, qid, quest, state) -> None:
        state["current_step"] += 1
        if state["current_step"] >= len(quest.steps):
            state["status"] = "completed"
            gold = int(quest.reward.get("gold", 0))
            self.player_stats["gold"] += gold
            reward_item = quest.reward.get("item")
            if reward_item:
                self.inventory[reward_item] = self.inventory.get(reward_item, 0) + 1
            self.logs.append(
                f"quest {qid} completed (reward gold={gold}"
                + (f", item={reward_item}" if reward_item else "") + ")"
            )

    def _maybe_advance_collect(self, item: str) -> None:
        for qid, quest in self.world.quests.items():
            state = self.quest_states[qid]
            if state["status"] != "active":
                continue
            cur = self._current_step(quest, state)
            if cur is None or cur.kind != "collect" or cur.item != item:
                continue
            if self.inventory.get(item, 0) >= cur.count:
                self._advance(qid, quest, state)
                self.logs.append(f"quest step {cur.step_id} completed (collected {item})")

    def _all_quests_completed(self) -> bool:
        return bool(self.quest_states) and all(
            s["status"] == "completed" for s in self.quest_states.values()
        )

    # --- observation ---
    def observe(self) -> Observation:
        quest_state = {}
        active, completed, known = [], [], []
        for qid, quest in self.world.quests.items():
            state = self.quest_states[qid]
            known.append(qid)
            if state["status"] == "active":
                active.append(qid)
            elif state["status"] == "completed":
                completed.append(qid)
            cur = self._current_step(quest, state)
            quest_state[qid] = {
                "status": state["status"],
                "current_step": state["current_step"],
                "step_id": cur.step_id if cur else None,
                "step_kind": cur.kind if cur else None,
            }
        reachable = sorted(
            eid for eid, p in self.world.positions.items()
            if self.world.grid.shortest_path(self.player_pos, p) is not None
        )
        available = sorted(
            eid for eid in self.world.entities_at(self.player_pos)
            if eid in self.world.npc_ids or eid in self.world.interactables
        )
        nearby = sorted(
            eid for eid, p in self.world.positions.items()
            if abs(p[0] - self.player_pos[0]) + abs(p[1] - self.player_pos[1]) <= 1
        )
        return Observation(
            tick=self.tick,
            player_pos=self.player_pos,
            player_stats=dict(self.player_stats),
            active_quests=sorted(active),
            completed_quests=sorted(completed),
            known_quests=sorted(known),
            quest_state=quest_state,
            inventory=dict(self.inventory),
            hp=int(self.player_stats["hp"]),
            nearby_entities=nearby,
            reachable_targets=reachable,
            available_interactions=available,
            visible_map={
                "width": self.world.grid.width,
                "height": self.world.grid.height,
                "blocked": sorted(list(self.world.grid.blocked)),
            },
            dialogue_options=[],
            last_action_result=self._last_result,
            logs=list(self.logs),
        )

    # --- determinism ---
    def state_hash(self) -> str:
        payload = {
            "tick": self.tick,
            "player_pos": list(self.player_pos),
            "player_stats": self.player_stats,
            "inventory": self.inventory,
            "quest_states": self.quest_states,
            "world_object_states": {"gathered": sorted(self.gathered)},
            "monster_states": {},
            "event_flags": {},
            "rng": {"seed": self.seed, "draws": self.rng_draws},
        }
        return compute_snapshot_id(payload)

    def nav_provider(self) -> AureusNav:
        positions = dict(self.world.positions)
        positions["__player_start__"] = self.world.start_pos
        return AureusNav(self.world.grid, positions)
