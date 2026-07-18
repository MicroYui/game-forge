"""Agent-Env contract §4.3 (Observation actionability) regression: a
partial-observability agent — one that reads ONLY `observe()` and never the
`WorldConfig`/`env.world` ground truth — must be able to START and WIN a fight.

Before the kernel fix, a `fight` step's encounter tile and monster ids were
invisible in the `Observation` until combat was already active
(`observe()` only unioned monsters into `reachable_targets`/`nearby_entities`
when `combat["active_encounter"] is not None`). So an observation-only agent
could never reach or attack the encounter to trigger combat — only the
`ScriptedDriver` completed fights, and only because it cheats with full ground
truth. These tests are the proof the gap is closed:

  * `test_fight_step_monster_exposed_before_combat` — the focused regression
    lock: the moment the current step becomes `fight`, BEFORE any combat, the
    encounter's monster id is in `observe().reachable_targets` (and in
    `nearby_entities` once adjacent/on the tile). Absent before the fix.
  * `test_navigate_to_pending_fight_monster_routes_to_encounter` — proves
    `navigate_to(<monster id>)` walks to the encounter tile pre-combat.
  * `test_partial_obs_agent_completes_fight_chain` — drives a real fight-bearing
    GENERATED chain to full completion using ONLY the observation.
"""

from __future__ import annotations

from gameforge.agents.scenario_gen import generate_chains
from gameforge.apps.cli.driver import ScriptedDriver
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.env_types import Attack, Interact, NavigateTo
from gameforge.contracts.ir import NodeType
from gameforge.contracts.world import (
    BattleEncounterSpec,
    GridSpec,
    MonsterSpec,
    Placement,
    QuestSpec,
    QuestStepSpec,
    ScenarioConfig,
    WorldConfig,
)
from gameforge.game.aureus.kernel import AureusEnv


# --------------------------------------------------------------------------- #
# a tiny, self-contained fight-bearing world (talk -> fight -> turn_in)
# --------------------------------------------------------------------------- #
def _fight_world() -> WorldConfig:
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="fs", start_pos=(0, 0)),
        grid=GridSpec(width=8, height=8, blocked=[]),
        placements=[
            Placement(entity_id="npc:g", type=NodeType.NPC, pos=(1, 0), attrs={}),
            Placement(
                entity_id="enc:1",
                type=NodeType.BATTLE_ENCOUNTER,
                pos=(3, 3),
                attrs={"monsters": ["mon:1"], "reward": {"gold": 10}, "pos": [3, 3]},
            ),
        ],
        quests=[
            QuestSpec(
                quest_id="q",
                giver="npc:g",
                reward={"gold": 50},
                steps=[
                    QuestStepSpec(step_id="t", kind="talk", target="npc:g"),
                    QuestStepSpec(step_id="f", kind="fight", encounter="enc:1"),
                    QuestStepSpec(step_id="d", kind="turn_in", target="npc:g"),
                ],
            )
        ],
        monsters=[MonsterSpec(monster_id="mon:1", stats={"hp": 20, "atk": 3, "def": 0})],
        encounters=[
            BattleEncounterSpec(
                encounter_id="enc:1",
                monsters=["mon:1"],
                reward={"gold": 10},
                pos=(3, 3),
            )
        ],
    )


def _navigate_until_arrived(env: AureusEnv, target: str, limit: int = 100) -> str:
    last = ""
    for _ in range(limit):
        last = env.step(NavigateTo(target=target)).observation.last_action_result
        if last in ("arrived", "unreachable", "unknown_target"):
            return last
    raise AssertionError(f"did not settle navigating to {target}")


def _advance_to_fight_step(env: AureusEnv) -> None:
    """Accept + complete the talk step so the ACTIVE quest's current step is the
    fight step — WITHOUT starting combat."""
    _navigate_until_arrived(env, "npc:g")
    env.step(Interact(target="npc:g"))


def test_fight_step_monster_exposed_before_combat() -> None:
    env = AureusEnv(_fight_world())
    env.reset("fs", seed=0)

    # At reset the current step is `talk`, not `fight`: the monster must NOT be
    # surfaced yet (we only surface the CURRENT fight step's monsters).
    assert "mon:1" not in env.observe().reachable_targets

    _advance_to_fight_step(env)
    obs = env.observe()
    assert obs.quest_state["q"]["step_kind"] == "fight"
    assert env.combat["active_encounter"] is None  # combat has NOT started

    # REGRESSION LOCK: the pending fight's monster is reachable BEFORE combat.
    assert "mon:1" in obs.reachable_targets

    # ...and becomes `nearby` once the player is on/adjacent to the tile.
    assert "mon:1" not in obs.nearby_entities  # player still at start
    _navigate_until_arrived(env, "mon:1")
    assert "mon:1" in env.observe().nearby_entities


def test_observe_uses_one_bounded_reachability_search(monkeypatch) -> None:
    env = AureusEnv(_fight_world())
    env.reset("fs", seed=0)
    grid = env.world.grid
    original = grid.reachable_positions
    calls = 0

    def reachable_positions(src, positions):
        nonlocal calls
        calls += 1
        return original(src, positions)

    def reject_per_target_path(*_args, **_kwargs):
        raise AssertionError("observe must not run one BFS per target")

    monkeypatch.setattr(grid, "reachable_positions", reachable_positions)
    monkeypatch.setattr(grid, "shortest_path", reject_per_target_path)

    observation = env.observe()

    assert calls == 1
    assert "npc:g" in observation.reachable_targets


def test_navigate_to_pending_fight_monster_routes_to_encounter() -> None:
    env = AureusEnv(_fight_world())
    env.reset("fs", seed=0)
    _advance_to_fight_step(env)
    assert env.combat["active_encounter"] is None

    # navigate_to(<monster id>) resolves to the encounter tile (3,3) pre-combat.
    result = _navigate_until_arrived(env, "mon:1")
    assert result == "arrived"
    assert env.player_pos == (3, 3)

    # ...and attacking there now triggers combat (the whole point).
    env.step(Attack(target_id="mon:1"))
    assert env.combat["active_encounter"] == "enc:1"


def test_navigate_to_unknown_non_fight_target_unchanged() -> None:
    """Guard: navigation semantics for genuinely unknown ids are untouched."""
    env = AureusEnv(_fight_world())
    env.reset("fs", seed=0)
    env.step(NavigateTo(target="does:not:exist"))
    assert env.observe().last_action_result == "unknown_target"
    # a monster id whose quest is NOT at its fight step is still unknown to nav
    env.step(NavigateTo(target="mon:1"))  # quest still on the talk step
    assert env.observe().last_action_result == "unknown_target"


# --------------------------------------------------------------------------- #
# observation-only agent (NO access to env.world / WorldConfig ground truth)
#
# The agent knows ONLY what `observe()` returns: quest step kinds, reachable
# target ids, interactions available at the current tile, and action results.
# It never reads `env.world`, quest step targets, encounter ids, or positions.
#
# One env property forces discipline: `collect` advancement is EDGE-triggered
# on the gather event (kernel `_gather` -> `_maybe_advance_collect`), so a node
# gathered before its quest reaches the collect step is depleted and the step
# can never advance. A blind "interact everything" prober trips this. So the
# agent EXPLORES first (learning which ids are gather nodes purely from the
# "gathered" action result), then RESETS the deterministic episode and EXECUTES
# cleanly — a legitimate explore-then-execute strategy that stays 100%
# observation-only (ids are learned from outcomes, never from ground truth).
# --------------------------------------------------------------------------- #
def _all_done(obs) -> bool:
    return bool(obs.quest_state) and all(
        qs["status"] == "completed" for qs in obs.quest_state.values()
    )


def _progress_sig(obs):
    return tuple(
        sorted((qid, qs["status"], qs["current_step"]) for qid, qs in obs.quest_state.items())
    )


def _first_pending_step_kind(obs):
    for qid in sorted(obs.quest_state):
        qs = obs.quest_state[qid]
        if qs["status"] != "completed" and qs["step_kind"] is not None:
            return qs["step_kind"]
    return None


def _nav(env: AureusEnv, target: str, budget: int = 1000) -> str:
    last = ""
    for _ in range(budget):
        last = env.step(NavigateTo(target=target)).observation.last_action_result
        if last in ("arrived", "unreachable", "unknown_target"):
            break
    return last


def _discover_gather_ids(env: AureusEnv, scenario: str, seed: int) -> set[str]:
    """Learn which reachable ids are gather nodes, from the "gathered" action
    result alone (no ground truth). Leaves the episode dirty — the caller
    resets afterwards."""
    obs = env.reset(scenario, seed)
    gather_ids: set[str] = set()
    for target in list(obs.reachable_targets):
        if _nav(env, target) in ("unreachable", "unknown_target"):
            continue
        res = env.step(Interact(target=target)).observation.last_action_result
        if res in ("gathered", "already_gathered"):
            gather_ids.add(target)
    return gather_ids


def _run_active_combat(env: AureusEnv, budget: int = 500) -> None:
    """Combat is active: attack alive monsters (reachable targets not
    interactable at the current tile) until victory. Observation-driven."""
    for _ in range(budget):
        obs = env.observe()
        candidates = [t for t in obs.reachable_targets if t not in obs.available_interactions]
        hit = False
        for m in candidates:
            res = env.step(Attack(target_id=m)).observation.last_action_result
            if res == "victory":
                return
            if res in ("hit", "miss", "kill"):
                hit = True
                break
        if not hit:
            return


def _clean_run(env: AureusEnv, gather_ids: set[str], outer_budget: int = 4000) -> bool:
    """Drive to completion using ONLY the observation + learned `gather_ids`.
    Per outer step, act on the first pending quest's CURRENT step kind:
      * fight    -> attack-probe reachable targets; a pending-fight monster on
                    its tile engages combat (safe no-op on everything else),
                    then finish the fight.
      * collect  -> gather (interact) a learned gather node.
      * talk/turn_in -> interact reachable NON-gather targets (NPCs progress;
                    other placements are harmless no-ops).
    Re-observe as soon as any quest's progress signature changes."""
    for _ in range(outer_budget):
        obs = env.observe()
        if _all_done(obs):
            return True
        kind = _first_pending_step_kind(obs)
        before = _progress_sig(obs)
        if kind == "fight":
            targets = list(obs.reachable_targets)
        elif kind == "collect":
            targets = [t for t in obs.reachable_targets if t in gather_ids]
        else:  # talk / turn_in
            targets = [t for t in obs.reachable_targets if t not in gather_ids]
        acted = False
        for t in targets:
            if _nav(env, t) in ("unreachable", "unknown_target"):
                continue
            if kind == "fight":
                res = env.step(Attack(target_id=t)).observation.last_action_result
                if res in ("hit", "miss", "kill", "victory"):
                    if res != "victory":
                        _run_active_combat(env)
            else:
                env.step(Interact(target=t))
            if _progress_sig(env.observe()) != before:
                acted = True
                break
        if not acted:
            break
    return _all_done(env.observe())


def _partial_obs_run(env: AureusEnv, scenario: str, seed: int) -> bool:
    gather_ids = _discover_gather_ids(env, scenario, seed)
    env.reset(scenario, seed)  # fresh deterministic episode
    return _clean_run(env, gather_ids)


def _first_fight_bearing_world() -> WorldConfig:
    """A GENERATED chain (not hand-authored) that contains a fight step whose
    encounter is placed on the grid; asserted fight-bearing + ScriptedDriver-
    completable so the partial-obs run has a legitimate target."""
    for snap in generate_chains(0, 20):
        world = snapshot_to_world(snap)
        has_fight = any(s.kind == "fight" for q in world.quests for s in q.steps)
        if has_fight:
            return world
    raise AssertionError("no fight-bearing generated chain found")


def test_scripted_driver_completes_the_fight_chain() -> None:
    """Baseline: the ground-truth-cheating ScriptedDriver still completes it
    and exercises combat (guards against regressing the reference driver)."""
    world = _first_fight_bearing_world()
    env = AureusEnv(world)
    env.reset(world.scenario.scenario_id, seed=0)
    result = ScriptedDriver(world).run(env)
    assert result["completed"] is True
    assert "combat" in result["systems_exercised"]


def test_partial_obs_agent_completes_fight_chain() -> None:
    world = _first_fight_bearing_world()
    # sanity: the chosen chain really does carry a fight step
    assert any(s.kind == "fight" for q in world.quests for s in q.steps)

    env = AureusEnv(world)
    assert _partial_obs_run(env, world.scenario.scenario_id, seed=0) is True
    assert env._all_quests_completed() is True
