"""Kernel semantic fix: a `collect` step must be satisfied whenever the
required item is ALREADY in inventory once the quest's current step IS that
collect step — regardless of WHEN the item was gathered. Before the fix,
`_gather` only auto-advances a collect step at the moment of gathering, and
nothing re-checks inventory when a quest's current step later *becomes* a
collect step, so a player who gathers early permanently stalls the quest.

Two anchors:
  - `test_prefetched_item_auto_advances_collect_step_on_later_step_advance`:
    gather the item while the quest is still on an EARLIER step (talk to a
    second NPC), then advance the quest onto the collect step via an
    unrelated interact — the collect step must auto-complete right there
    (fix (b): re-check on every step-advance, inside `_advance`).
  - `test_already_gathered_reinteract_triggers_collect_recheck`: a quest
    whose FIRST step is `collect` (no preceding `talk` step to hook the
    re-check into `_advance`), gathered+depleted BEFORE the quest is even
    accepted; only a re-interact of the now-depleted (`already_gathered`)
    source triggers the recheck (fix (a): the `already_gathered` short-circuit
    in `_gather` must still call `_maybe_advance_collect`).

Guard: `test_insufficient_inventory_does_not_advance_collect` — a collect
step with inventory < count must NOT advance, before or after the fix.
"""

from gameforge.contracts.env_types import parse_action
from gameforge.contracts.ir import NodeType
from gameforge.contracts.world import (
    GridSpec, Placement, QuestSpec, QuestStepSpec, ScenarioConfig, WorldConfig,
)
from gameforge.game.aureus.kernel import AureusEnv


def _navigate_until_arrived(env, target, limit=50):
    for _ in range(limit):
        r = env.step(parse_action({"kind": "navigate_to", "target": target}))
        if r.observation.last_action_result == "arrived":
            return r
    raise AssertionError(f"did not arrive at {target}")


def _interact(env, target):
    return env.step(parse_action({"kind": "interact", "target": target}))


# --- fix (b): pre-gather before the quest reaches the collect step ---------

def _wc_talk_talk_collect_turn_in():
    """talk(accept, npc:a) -> talk(npc:b) -> collect(item:x, count=2) -> turn_in(npc:a).

    The gather source is reachable independent of quest status, so the item
    can be picked up while the quest is still `known` (not yet accepted),
    i.e. strictly before the quest's current step is ever the collect step.
    """
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=8, height=8, blocked=[]),
        placements=[
            Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 0), attrs={}),
            Placement(entity_id="npc:b", type=NodeType.NPC, pos=(2, 0), attrs={}),
            Placement(
                entity_id="interact:pile", type=NodeType.INTERACTABLE, pos=(4, 4),
                attrs={"kind": "gather", "yields_item": "item:x", "yields_count": 2},
            ),
        ],
        quests=[QuestSpec(quest_id="q", giver="npc:a", reward={"gold": 10}, steps=[
            QuestStepSpec(step_id="t1", kind="talk", target="npc:a"),
            QuestStepSpec(step_id="t2", kind="talk", target="npc:b"),
            QuestStepSpec(step_id="c", kind="collect", item="item:x", count=2),
            QuestStepSpec(step_id="d", kind="turn_in", target="npc:a"),
        ])],
    )


def test_prefetched_item_auto_advances_collect_step_on_later_step_advance():
    e = AureusEnv(_wc_talk_talk_collect_turn_in())
    e.reset("s", seed=0)

    # Gather the item FIRST, while the quest is still "known" (not accepted) —
    # strictly before the quest is anywhere near its collect step.
    _navigate_until_arrived(e, "interact:pile")
    _interact(e, "interact:pile")
    assert e.observe().inventory.get("item:x", 0) == 2
    assert e.observe().quest_state["q"]["status"] == "known"

    # Accept the quest (talk to npc:a) -> advances past step0 onto step1
    # (talk to npc:b) — still not the collect step.
    _navigate_until_arrived(e, "npc:a")
    _interact(e, "npc:a")
    st = e.observe().quest_state["q"]
    assert st["status"] == "active"
    assert st["step_kind"] == "talk"

    # Advance onto the collect step via the SECOND talk step (unrelated to
    # gathering). Because inventory ALREADY satisfies the collect requirement,
    # the collect step must auto-complete right here, landing on turn_in —
    # without any fresh gather action.
    _navigate_until_arrived(e, "npc:b")
    _interact(e, "npc:b")
    st = e.observe().quest_state["q"]
    assert st["step_kind"] == "turn_in", (
        f"collect step did not auto-advance from pre-fetched inventory: {st}"
    )

    # Quest still completes normally afterwards.
    _navigate_until_arrived(e, "npc:a")
    r = _interact(e, "npc:a")
    assert "q" in r.observation.completed_quests
    assert r.done is True


# --- fix (a): already_gathered short-circuit must still re-check ----------

def _wc_collect_first_step():
    """collect(item:x, count=1) -> turn_in(npc:a): the quest's FIRST step is
    `collect` (accepting via npc:a does not itself advance a non-talk step0),
    so gathering the item before acceptance leaves the quest stuck on an
    already-satisfied collect step with no `_advance` call ever fired. Only a
    re-interact of the (now depleted) gather source can trigger the recheck.
    """
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s", start_pos=(0, 0)),
        grid=GridSpec(width=8, height=8, blocked=[]),
        placements=[
            Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 0), attrs={}),
            Placement(
                entity_id="interact:pile", type=NodeType.INTERACTABLE, pos=(4, 4),
                attrs={"kind": "gather", "yields_item": "item:x", "yields_count": 1},
            ),
        ],
        quests=[QuestSpec(quest_id="q", giver="npc:a", reward={"gold": 10}, steps=[
            QuestStepSpec(step_id="c", kind="collect", item="item:x", count=1),
            QuestStepSpec(step_id="d", kind="turn_in", target="npc:a"),
        ])],
    )


def test_already_gathered_reinteract_triggers_collect_recheck():
    e = AureusEnv(_wc_collect_first_step())
    e.reset("s", seed=0)

    # Gather + deplete the (one-shot) source before the quest is even accepted.
    _navigate_until_arrived(e, "interact:pile")
    r = _interact(e, "interact:pile")
    assert r.observation.last_action_result == "gathered"
    assert e.observe().inventory.get("item:x", 0) == 1

    # Accept the quest. Step0 is `collect`, not `talk`, so acceptance alone
    # does not advance it — the quest is now active, already satisfied, but
    # stuck (this is the documented scope boundary: only `_advance`/`_gather`
    # re-check, not acceptance itself).
    _navigate_until_arrived(e, "npc:a")
    _interact(e, "npc:a")
    st = e.observe().quest_state["q"]
    assert st["status"] == "active"
    assert st["step_kind"] == "collect"

    # Re-interact the now-depleted source. `_gather` takes the
    # `already_gathered` short-circuit, but must STILL trigger the collect
    # recheck (fix (a)), advancing the quest onto turn_in.
    _navigate_until_arrived(e, "interact:pile")
    r = _interact(e, "interact:pile")
    assert r.observation.last_action_result == "already_gathered"
    st = e.observe().quest_state["q"]
    assert st["step_kind"] == "turn_in", (
        f"already_gathered re-interact did not trigger collect recheck: {st}"
    )

    _navigate_until_arrived(e, "npc:a")
    r = _interact(e, "npc:a")
    assert "q" in r.observation.completed_quests
    assert r.done is True


# --- guard: insufficient inventory must never auto-advance -----------------

def test_insufficient_inventory_does_not_advance_collect():
    e = AureusEnv(_wc_talk_talk_collect_turn_in())
    e.reset("s", seed=0)

    # Gather is depletable and yields 2, but pretend only a partial amount is
    # ever available: gather it, then manually drain inventory below the
    # required count to simulate "not enough was ever collected" without
    # touching quest/step machinery directly.
    _navigate_until_arrived(e, "interact:pile")
    _interact(e, "interact:pile")
    e.inventory["item:x"] = 1  # below the collect step's required count=2

    _navigate_until_arrived(e, "npc:a")
    _interact(e, "npc:a")
    st = e.observe().quest_state["q"]
    assert st["status"] == "active"
    assert st["step_kind"] == "talk"  # step1 (talk to npc:b), not yet collect

    _navigate_until_arrived(e, "npc:b")
    _interact(e, "npc:b")
    st = e.observe().quest_state["q"]
    assert st["step_kind"] == "collect", (
        f"collect step advanced despite insufficient inventory: {st}"
    )
    assert e.observe().inventory.get("item:x", 0) == 1
