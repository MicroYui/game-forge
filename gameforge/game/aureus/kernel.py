"""Aureus deterministic kernel — quest state machine + combat/economy/gacha
integration + Env implementation (M0a talk/collect/turn_in, extended M0b).

Tick-based, seed-ized, headless authoritative logic. M0a implemented the
atomic Env actions needed for talk/collect/turn_in; M0b implements the
combat/economy atomics (attack/cast_skill/use/equip/buy/sell) declared by
the contract, routing gacha through `buy` per contract §4.2 (no Env-contract
change) and threading every rng draw through a single `CountingRandom` so
replay-determinism (contract §4.4) extends to combat/gacha.

state_hash covers authoritative state only (contract §4.4): tick, player
state (incl. equipped/active_effects/gacha_pity), quest states, inventory,
world-object states, monster states, combat scope, event flags, rng — NOT
logs / render / wall-clock / debug.

Buff/debuff timing decision (M0b): magnitudes are applied ONCE, when the
status is queued by `CombatSystem.resolve_skill` (mirroring the comment in
combat.py that ticking only tracks remaining duration for buffs/debuffs),
and reverted ONCE, on the tick where `remaining` would drop to 0 — i.e.
"apply-on-queue, revert-on-expiry". For the player this mutates
`player_stats` directly (the same dict object combat.py treats as
`target_state`); for a monster (whose `MonsterSpec.stats` is shared,
immutable content data) it accumulates into a per-instance
`monster_state["stat_mods"]` overlay instead, so the underlying spec is
never mutated.
"""

from __future__ import annotations

from gameforge.contracts.canonical import compute_snapshot_id
from gameforge.contracts.env_types import (
    Action,
    Attack,
    Buy,
    CastSkill,
    Choose,
    Equip,
    Interact,
    NavigateTo,
    Observation,
    Observe,
    Pickup,
    Sell,
    StepResult,
    Use,
    Wait,
)
from gameforge.contracts.world import BattleEncounterSpec, WorldConfig
from gameforge.env.base import Environment
from gameforge.game.aureus.combat import CombatSystem
from gameforge.game.aureus.economy import EconomySystem
from gameforge.game.aureus.gacha import GachaSystem
from gameforge.game.aureus.grid import AureusNav
from gameforge.game.aureus.rng import CountingRandom
from gameforge.game.aureus.world import AureusWorld

Pos = tuple[int, int]
_PLAYER_STAT_KEYS = ("hp", "atk", "def", "spd", "mp", "gold")
_BUFF_DEBUFF_KINDS = ("buff", "debuff")


class AureusEnv(Environment):
    def __init__(self, world_config: WorldConfig) -> None:
        self.world = AureusWorld(world_config)
        self.seed = 0
        self._reset_state()

    # --- lifecycle ---
    def _reset_state(self) -> None:
        self.tick = 0
        self.player_pos: Pos = self.world.start_pos
        self.player_stats: dict = {"hp": 100, "atk": 10, "def": 5, "spd": 10, "mp": 20, "gold": 0}
        self.inventory: dict[str, int] = {}
        self.equipped: dict[str, str] = {}
        self.gathered: set[str] = set()
        self.quest_states: dict[str, dict] = {}
        for qid in self.world.quests:
            self.quest_states[qid] = {"status": "known", "current_step": 0}
        self.monster_states: dict[str, dict] = {}
        self.combat: dict = {"active_encounter": None, "turn": 0}
        self.gacha_pity: dict[str, int] = {}

        self.rng = CountingRandom(self.seed)
        self.combat_system = CombatSystem(rng=self.rng)
        self.economy = EconomySystem()
        self.gacha = GachaSystem()

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
        elif isinstance(action, Attack):
            self._attack(action.target_id)
        elif isinstance(action, CastSkill):
            self._cast_skill(action.skill_id, action.target_id)
        elif isinstance(action, Use):
            self._use(action.item_id)
        elif isinstance(action, Equip):
            self._equip(action.item_id)
        elif isinstance(action, Buy):
            self._buy(action.shop_id, action.item_id, action.count)
        elif isinstance(action, Sell):
            self._sell(action.shop_id, action.item_id, action.count)
        else:  # pragma: no cover - exhaustive union
            self._last_result = "unknown_action"

        obs = self.observe()
        done = self._all_quests_completed()
        return StepResult(observation=obs, reward=0.0, done=done, info={})

    # --- pending-fight targets (contract §4.3 actionability) ---
    def _pending_fight_encounters(self) -> list[BattleEncounterSpec]:
        """Encounters of the CURRENT `fight` step of every ACTIVE quest — the
        fights the agent is expected to start next. Surfaced pre-combat so an
        observation-only agent can SEE and REACH the encounter before combat is
        active (without cheating with `WorldConfig` ground truth). Driven purely
        by the quest state machine, so it never re-triggers the location-based
        combat activation and never touches `state_hash`."""
        result: list[BattleEncounterSpec] = []
        for qid, quest in self.world.quests.items():
            state = self.quest_states[qid]
            if state["status"] != "active":
                continue
            cur = self._current_step(quest, state)
            if cur is None or cur.kind != "fight" or not cur.encounter:
                continue
            enc = self.world.encounters.get(cur.encounter)
            if enc is not None:
                result.append(enc)
        return result

    def _undefeated_monsters(self, encounter: BattleEncounterSpec) -> list[str]:
        """Monster ids of `encounter` not yet defeated (no combat state, or a
        state still flagged alive)."""
        return [
            mid
            for mid in encounter.monsters
            if not (mid in self.monster_states and not self.monster_states[mid].get("alive", False))
        ]

    def _pending_fight_pos(self, target: str) -> Pos | None:
        """Encounter tile for `target` iff it is a monster of a current
        pending-fight encounter that is placed on the grid — so
        `navigate_to(<monster id>)` routes to the encounter before combat is
        active. None for every other id (existing nav semantics untouched)."""
        for enc in self._pending_fight_encounters():
            if enc.pos is not None and target in enc.monsters:
                return (int(enc.pos[0]), int(enc.pos[1]))
        return None

    # --- movement ---
    def _navigate_to(self, target: str) -> None:
        dst = self.world.pos_of(target)
        if dst is None:
            dst = self._pending_fight_pos(target)
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
            # Re-checking here (not just on a fresh gather below) is what lets
            # a quest that reaches a collect step *after* this node was
            # already depleted still get unstuck via a plain re-interact —
            # `_advance`'s own re-check (below) only fires on a step
            # transition, which never happens for a quest whose current step
            # is `collect` from the moment it becomes active.
            item, _ = self.world.grants_item(interactable_id)
            if item is not None:
                self._maybe_advance_collect(item)
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
        """Advance `state` by one step, then keep auto-advancing through any
        immediately-following `collect` step(s) already satisfied by EXISTING
        inventory — a `collect` step must complete whenever the required item
        is already held once the quest's current step IS that collect step,
        regardless of when the item was gathered (kernel semantic fix: a
        pre-fetched item must not permanently stall the quest). Looping
        handles a chain of several pre-satisfied collect steps in a row.
        Only `collect` steps are auto-skipped this way; talk/turn_in/fight
        steps still require their own explicit satisfying action."""
        while True:
            state["current_step"] += 1
            if state["current_step"] >= len(quest.steps):
                state["status"] = "completed"
                gold = int(quest.reward.get("gold", 0))
                self.player_stats["gold"] = self.player_stats.get("gold", 0) + gold
                reward_item = quest.reward.get("item")
                if reward_item:
                    self.inventory[reward_item] = self.inventory.get(reward_item, 0) + 1
                self.logs.append(
                    f"quest {qid} completed (reward gold={gold}"
                    + (f", item={reward_item}" if reward_item else "")
                    + ")"
                )
                return
            cur = self._current_step(quest, state)
            if cur is None or cur.kind != "collect" or self.inventory.get(cur.item, 0) < cur.count:
                return
            self.logs.append(f"quest step {cur.step_id} completed (collected {cur.item})")

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

    def _maybe_advance_fight(self, encounter_id: str) -> None:
        for qid, quest in self.world.quests.items():
            state = self.quest_states[qid]
            if state["status"] != "active":
                continue
            cur = self._current_step(quest, state)
            if cur is None or cur.kind != "fight" or cur.encounter != encounter_id:
                continue
            self._advance(qid, quest, state)
            self.logs.append(f"quest step {cur.step_id} completed (defeated {encounter_id})")

    def _all_quests_completed(self) -> bool:
        return bool(self.quest_states) and all(
            s["status"] == "completed" for s in self.quest_states.values()
        )

    # --- economy/gacha player view ---
    def _player_view(self) -> dict:
        """Live-reference dict shape expected by EconomySystem/GachaSystem.
        Sub-dicts are the SAME objects as the kernel's own attributes, so
        in-place mutation inside those systems (e.g. `player["stats"][k] = ..`)
        is directly reflected on `self.player_stats`/`self.inventory`/etc."""
        return {
            "stats": self.player_stats,
            "inventory": self.inventory,
            "equipped": self.equipped,
            "gacha_pity": self.gacha_pity,
        }

    # --- economy/gacha atomics ---
    def _use(self, item_id: str) -> None:
        result = self.economy.use(self._player_view(), item_id)
        if result == "used":
            self.tick += 1
        self._last_result = result

    def _equip(self, item_id: str) -> None:
        equipment = self.world.equipment.get(item_id)
        if equipment is None:
            self._last_result = "unknown_item"
            return
        prev_id = self.equipped.get(equipment.slot)
        previous = self.world.equipment.get(prev_id) if prev_id else None
        result = self.economy.equip(self._player_view(), equipment, previous=previous)
        if result == "equipped":
            self.tick += 1
        self._last_result = result

    def _buy(self, shop_id: str, item_id: str, count: int) -> None:
        if shop_id in self.world.gacha_pools:
            self._buy_gacha(shop_id, count)
            return
        shop = self.world.shops.get(shop_id)
        if shop is None:
            self._last_result = "unknown_shop"
            return
        result = self.economy.buy(self._player_view(), shop, item_id, count)
        if result == "bought":
            self.tick += 1
        self._last_result = result

    def _buy_gacha(self, pool_id: str, count: int) -> None:
        pool = self.world.gacha_pools[pool_id]
        cost = pool.cost * max(0, int(count))
        if count <= 0 or self.player_stats.get(pool.currency, 0) < cost:
            self._last_result = "insufficient_funds"
            return
        results = self.gacha.pull(self._player_view(), pool, self.rng, int(count))
        for item in results:
            if item:
                self.inventory[item] = self.inventory.get(item, 0) + 1
        self.tick += 1
        self._last_result = "pulled"

    def _sell(self, shop_id: str, item_id: str, count: int) -> None:
        shop = self.world.shops.get(shop_id)
        if shop is None:
            self._last_result = "unknown_shop"
            return
        result = self.economy.sell(self._player_view(), shop, item_id, count)
        if result == "sold":
            self.tick += 1
        self._last_result = result

    # --- combat ---
    def _monster_stat(self, monster_id: str, stat: str) -> int:
        monster = self.world.monsters.get(monster_id)
        base = int(monster.stats.get(stat, 0)) if monster else 0
        overlay = self.monster_states.get(monster_id, {}).get("stat_mods", {})
        return base + int(overlay.get(stat, 0))

    def _maybe_activate_encounter(self, target_id: str) -> None:
        """Location-triggered combat: if no encounter is active, but the
        player stands on an encounter's tile and is attacking one of its
        monsters, spawn `monster_states` for it and mark it active. Does not
        re-trigger an encounter that has already been fully defeated (all its
        monsters have a state with alive=False)."""
        if self.combat["active_encounter"] is not None:
            return
        encounter = self.world.encounter_at(self.player_pos)
        if encounter is None or target_id not in encounter.monsters:
            return
        already_defeated = all(
            mid in self.monster_states and not self.monster_states[mid].get("alive", False)
            for mid in encounter.monsters
        )
        if already_defeated:
            return
        for mid in encounter.monsters:
            if mid not in self.monster_states:
                monster = self.world.monsters.get(mid)
                hp = int(monster.stats.get("hp", 0)) if monster else 0
                self.monster_states[mid] = {"hp": hp, "alive": True, "pos": encounter.pos}
        self.combat["active_encounter"] = encounter.encounter_id
        self.combat["turn"] = 0

    def _attack(self, target_id: str) -> None:
        self._maybe_activate_encounter(target_id)
        if self.combat["active_encounter"] is None:
            self._last_result = "not_in_combat"
            return
        monster_state = self.monster_states.get(target_id)
        if monster_state is None or not monster_state.get("alive", False):
            self._last_result = "no_target"
            return
        attacker_stats = {
            "atk": int(self.player_stats.get("atk", 0)),
            "defense": self._monster_stat(target_id, "def"),
        }
        result = self.combat_system.resolve_attack(attacker_stats, monster_state, formula=None)
        self.tick += 1
        outcome = "hit" if result["hit"] else "miss"
        self._last_result = self._resolve_combat_turn(target_id, monster_state, outcome)

    def _cast_skill(self, skill_id: str, target_id: str) -> None:
        skill = self.world.skills.get(skill_id)
        if skill is None:
            self._last_result = "unknown_skill"
            return
        is_player_target = skill.target in ("self", "ally")
        if is_player_target:
            if self.combat["active_encounter"] is None:
                self._last_result = "not_in_combat"
                return
            target_state = self.player_stats
        else:
            self._maybe_activate_encounter(target_id)
            if self.combat["active_encounter"] is None:
                self._last_result = "not_in_combat"
                return
            target_state = self.monster_states.get(target_id)
            if target_state is None or not target_state.get("alive", False):
                self._last_result = "no_target"
                return
        if int(self.player_stats.get("mp", 0)) < skill.cost:
            self._last_result = "insufficient_mp"
            return
        self.player_stats["mp"] = int(self.player_stats.get("mp", 0)) - skill.cost

        result = self.combat_system.resolve_skill(
            skill,
            self.player_stats,
            target_state,
            formulas=self.world.formulas,
            effects=self.world.effects,
            status_effects=self.world.status_effects,
        )
        self.tick += 1
        if result["status_applied"] is not None:
            self._apply_new_status_stat_mod(target_state, is_player=is_player_target)

        if is_player_target:
            encounter = self.world.encounters.get(self.combat["active_encounter"])
            self._run_monster_turns(encounter)
            self._tick_all_status_effects(encounter)
            self.combat["turn"] += 1
            self._last_result = result["kind"]  # "heal"
        else:
            self._last_result = self._resolve_combat_turn(target_id, target_state, result["kind"])

    def _resolve_combat_turn(self, target_id: str, monster_state: dict, outcome_label: str) -> str:
        """Shared post-player-action bookkeeping for attack/enemy-skill: mark
        the kill if hp dropped to/below 0, let surviving monsters in the
        encounter take their turn, tick status effects, advance the turn
        counter, then check for encounter-wide victory."""
        killed_now = False
        if monster_state.get("hp", 0) <= 0 and monster_state.get("alive", True):
            monster_state["alive"] = False
            monster_state["hp"] = 0
            killed_now = True

        encounter = self.world.encounters.get(self.combat["active_encounter"])
        self._run_monster_turns(encounter)
        self._tick_all_status_effects(encounter)
        self.combat["turn"] += 1

        if encounter is not None and all(
            not self.monster_states.get(mid, {}).get("alive", False) for mid in encounter.monsters
        ):
            self._grant_encounter_victory(encounter)
            return "victory"
        return "kill" if killed_now else outcome_label

    def _run_monster_turns(self, encounter: BattleEncounterSpec | None) -> None:
        if encounter is None:
            return
        for monster_id in encounter.monsters:
            state = self.monster_states.get(monster_id)
            monster = self.world.monsters.get(monster_id)
            if state is None or monster is None or not state.get("alive", False):
                continue
            if self.combat_system.monster_ai_action(state, monster) != "attack":
                continue
            attacker_stats = {
                "atk": self._monster_stat(monster_id, "atk"),
                "defense": int(self.player_stats.get("def", 0)),
            }
            self.combat_system.resolve_attack(attacker_stats, self.player_stats, formula=None)

    def _apply_new_status_stat_mod(self, target_state: dict, is_player: bool) -> None:
        """Apply-on-queue half of the buff/debuff scheme: the entry just
        appended by resolve_skill is always the list's last item."""
        entries = target_state.get("status_effects", [])
        if not entries:
            return
        effect = self.world.effects.get(entries[-1]["effect_id"])
        if effect is None or effect.kind not in _BUFF_DEBUFF_KINDS:
            return
        stat = effect.stat
        if not stat:
            return
        delta = effect.magnitude if effect.kind == "buff" else -effect.magnitude
        if is_player:
            self.player_stats[stat] = self.player_stats.get(stat, 0) + delta
        else:
            mods = target_state.setdefault("stat_mods", {})
            mods[stat] = mods.get(stat, 0) + delta

    def _tick_status_effects_for(self, entity_state: dict, is_player: bool) -> None:
        """Revert-on-expiry half: any entry with remaining==1 dies THIS tick
        (combat.tick_status_effects decrements then drops at <=0), so revert
        its buff/debuff delta now, before delegating dot/heal/decrement to
        combat.py's generic implementation."""
        for status in entity_state.get("status_effects", []):
            if status.get("remaining", 1) != 1:
                continue
            effect = self.world.effects.get(status["effect_id"])
            if effect is None or effect.kind not in _BUFF_DEBUFF_KINDS:
                continue
            stat = effect.stat
            if not stat:
                continue
            delta = effect.magnitude if effect.kind == "buff" else -effect.magnitude
            if is_player:
                self.player_stats[stat] = self.player_stats.get(stat, 0) - delta
            else:
                mods = entity_state.setdefault("stat_mods", {})
                mods[stat] = mods.get(stat, 0) - delta
        self.combat_system.tick_status_effects(entity_state, self.world.effects)

    def _tick_all_status_effects(self, encounter: BattleEncounterSpec | None) -> None:
        self._tick_status_effects_for(self.player_stats, is_player=True)
        if encounter is None:
            return
        for monster_id in encounter.monsters:
            state = self.monster_states.get(monster_id)
            if state is not None:
                self._tick_status_effects_for(state, is_player=False)

    def _grant_encounter_victory(self, encounter: BattleEncounterSpec) -> None:
        gold = int(encounter.reward.get("gold", 0))
        self.player_stats["gold"] = self.player_stats.get("gold", 0) + gold
        reward_item = encounter.reward.get("item")
        if reward_item:
            self.inventory[reward_item] = self.inventory.get(reward_item, 0) + 1
        for monster_id in encounter.monsters:
            monster = self.world.monsters.get(monster_id)
            if monster is None or not monster.drop_table_id:
                continue
            drop_table = self.world.drop_tables.get(monster.drop_table_id)
            for item in self.combat_system.roll_drops(drop_table):
                self.inventory[item] = self.inventory.get(item, 0) + 1
        self.combat["active_encounter"] = None
        self._maybe_advance_fight(encounter.encounter_id)

    # --- observation ---
    def _player_stats_view(self) -> dict:
        return {k: self.player_stats.get(k, 0) for k in _PLAYER_STAT_KEYS}

    def _player_status_effects(self) -> list[dict]:
        return self.player_stats.get("status_effects", [])

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
        pending_encounters = self._pending_fight_encounters()
        target_positions = set(self.world.positions.values())
        target_positions.update(
            (int(enc.pos[0]), int(enc.pos[1])) for enc in pending_encounters if enc.pos is not None
        )
        reachable_positions = self.world.grid.reachable_positions(
            self.player_pos,
            target_positions,
        )
        reachable = {eid for eid, p in self.world.positions.items() if p in reachable_positions}
        available = sorted(
            eid
            for eid in self.world.entities_at(self.player_pos)
            if eid in self.world.npc_ids or eid in self.world.interactables
        )
        nearby = {
            eid
            for eid, p in self.world.positions.items()
            if abs(p[0] - self.player_pos[0]) + abs(p[1] - self.player_pos[1]) <= 1
        }
        # Pre-combat actionability (contract §4.3): surface the monsters of every
        # ACTIVE quest's CURRENT fight step whose encounter tile the player can
        # reach, so an observation-only agent can navigate to + attack them to
        # START combat — before `active_encounter` is set. Only undefeated
        # monsters of a placed, still-reachable encounter are added.
        pending_fight: set[str] = set()
        for enc in pending_encounters:
            if enc.pos is None:
                continue
            undefeated = self._undefeated_monsters(enc)
            if not undefeated:
                continue
            enc_pos = (int(enc.pos[0]), int(enc.pos[1]))
            if enc_pos in reachable_positions:
                reachable.update(undefeated)
                pending_fight.update(undefeated)
            if abs(enc_pos[0] - self.player_pos[0]) + abs(enc_pos[1] - self.player_pos[1]) <= 1:
                nearby.update(undefeated)
        if self.combat["active_encounter"] is not None:
            alive_monsters = sorted(
                mid for mid, st in self.monster_states.items() if st.get("alive")
            )
            nearby.update(alive_monsters)
            reachable.update(alive_monsters)
            pending_fight.update(alive_monsters)
        return Observation(
            tick=self.tick,
            player_pos=self.player_pos,
            player_stats=self._player_stats_view(),
            equipped_items=sorted(self.equipped.values()),
            active_effects=sorted(e["effect_id"] for e in self._player_status_effects()),
            active_quests=sorted(active),
            completed_quests=sorted(completed),
            known_quests=sorted(known),
            quest_state=quest_state,
            inventory=dict(self.inventory),
            hp=int(self.player_stats.get("hp", 0)),
            nearby_entities=sorted(nearby),
            reachable_targets=sorted(reachable),
            available_interactions=available,
            pending_fight_targets=sorted(pending_fight),
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
            "player_stats": self._player_stats_view(),
            "inventory": self.inventory,
            "quest_states": self.quest_states,
            "world_object_states": {"gathered": sorted(self.gathered)},
            "monster_states": self.monster_states,
            "event_flags": {},
            "equipped": self.equipped,
            "active_effects": sorted(
                self._player_status_effects(), key=lambda e: (e["effect_id"], e["remaining"])
            ),
            "combat": {
                "active_encounter": self.combat["active_encounter"],
                "turn": self.combat["turn"],
            },
            "gacha_pity": self.gacha_pity,
            "rng": {"seed": self.seed, "draws": self.rng.draws},
        }
        return compute_snapshot_id(payload)

    def nav_provider(self) -> AureusNav:
        positions = dict(self.world.positions)
        positions["__player_start__"] = self.world.start_pos
        return AureusNav(self.world.grid, positions)
