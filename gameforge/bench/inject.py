"""GameForge-Bench defect injectors (M3a Task 2 / design §3).

`inject(base, defect, seed) -> InjectedSample` mutates a clean IR `Snapshot`
to introduce EXACTLY one instance of a `DefectClass`, paired with the
`GroundTruth` describing what was injected. Each injector:

  1. clones the base snapshot's entity/relation lists (`_clone_lists` — the
     base `Snapshot`/`Entity`/`Relation` objects are mutable pydantic models,
     so injectors must never mutate them in place);
  2. mutates the copies to introduce the defect;
  3. rebuilds a fresh `Snapshot` via `Snapshot.from_entities_relations`.

Anti-circularity (design §8 / plan Task 2 self-review): this module never
imports `gameforge.spine.checkers.*` — injectors are structurally independent
of the oracle that later scores them (Task 7's `bench/metrics.py`). Their
correctness is locked here by property tests that assert the defect exists
via direct graph/relation inspection, never via a checker run.

Determinism: every injector derives its randomness from a STABLE hash of
`(base.snapshot_id, defect.value, seed)`. This deliberately uses `hashlib`,
NOT Python's builtin `hash()` — the builtin salts `str`/bytes hashing per
process (`PYTHONHASHSEED`), so `hash(("a", "b"))` is only stable within one
interpreter run. "Seeded reproducible" (design §3 invariant (c), plan Task 2)
is a cross-run promise (re-running the same `(base, defect, seed)` on a fresh
`uv run pytest` process must still produce the same `snapshot_id`), so the
seed derivation must be too.

All 15 taxonomy classes are implemented: 6 structural (Task 2), 5
numeric/economy (Task 3), and 4 narrative (Task 4). The narrative injectors
leave the IR graph clean and carry their defect in a seeded
`DialogueNarrativeInput` (the M2 Consistency quorum's input), since narrative
inconsistency lives in dialogue/lore text, not the content graph.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Callable

from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.agent_io import DialogueNarrativeInput
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.snapshot import Snapshot

_SOURCE_EDGES = (EdgeType.GRANTS, EdgeType.DROPS_FROM)
_NARRATIVE_CONSTRAINT = "C-narrative-quest-lore-consistency"


@dataclass
class GroundTruth:
    """What an injector actually did — consumed by `bench/metrics.py` (Task 7)
    to decide whether a checker Finding "detects" this sample: a Finding of
    the right `defect_class` must also touch one of `injected_entities`."""

    defect_class: DefectClass
    injected_entities: list[str]
    note: str


@dataclass
class InjectedSample:
    snapshot: Snapshot
    ground_truth: GroundTruth
    needs_nav: bool = False
    dialogue: object | None = None  # narrative injectors (Task 4) fill this in


def _clone_lists(base: Snapshot) -> tuple[list[Entity], list[Relation]]:
    """Deep-copy `base`'s entities/relations so an injector mutates copies,
    never the shared base snapshot (contract §2.4: `Snapshot` is immutable BY
    CONVENTION — its content-addressed `snapshot_id` is only trustworthy if
    nothing mutates `entities`/`relations` after construction)."""
    entities = [e.model_copy(deep=True) for e in base.entities.values()]
    relations = [r.model_copy(deep=True) for r in base.relations.values()]
    return entities, relations


def _seeded_rng(base: Snapshot, defect: DefectClass, seed: int) -> random.Random:
    """A `random.Random` derived from a STABLE (sha256) hash of
    `(base.snapshot_id, defect.value, seed)` — same inputs always produce the
    same seed, in-process or across a fresh interpreter (see module docstring:
    the builtin `hash()` would NOT give this guarantee for strings)."""
    key = f"{base.snapshot_id}|{defect.value}|{seed}".encode()
    digest = hashlib.sha256(key).digest()
    seed_int = int.from_bytes(digest[:8], "big")
    return random.Random(seed_int)


def _suffix(rng: random.Random) -> str:
    """A 9-digit deterministic-but-unique-enough suffix for injected ids,
    drawn from the sample's seeded RNG (never wall-clock/uuid4)."""
    return f"{rng.randrange(10**9):09d}"


# ---------------------------------------------------------------------------
# 1. dangling_reference (design §3 strategy 1)
# ---------------------------------------------------------------------------
def _inject_dangling_reference(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    relations.sort(key=lambda r: r.id)  # deterministic candidate order
    if not relations:
        raise ValueError("base snapshot has no relation to make dangling")
    rel = relations[rng.randrange(len(relations))]
    bad_dst = f"entity:injected-dangling-{_suffix(rng)}"
    rel.dst_id = bad_dst

    snapshot = Snapshot.from_entities_relations(entities, relations)
    gt = GroundTruth(
        defect_class=DefectClass.dangling_reference,
        injected_entities=[rel.id, bad_dst],
        note=f"relation {rel.id!r} ({rel.type.value}) now points dst at "
        f"nonexistent entity {bad_dst!r}",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 2. missing_drop_source (design §3 strategy 2)
# ---------------------------------------------------------------------------
def _inject_missing_drop_source(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    collect_steps = sorted(
        (
            e for e in entities
            if e.type is NodeType.QUEST_STEP
            and e.attrs.get("kind") == "collect"
            and e.attrs.get("item")
        ),
        key=lambda e: e.id,
    )
    if not collect_steps:
        raise ValueError("base snapshot has no collect step to target for missing_drop_source")
    step = collect_steps[rng.randrange(len(collect_steps))]
    item = step.attrs["item"]

    kept: list[Relation] = []
    removed: list[Relation] = []
    for r in relations:
        if r.type in _SOURCE_EDGES and r.dst_id == item:
            removed.append(r)
        else:
            kept.append(r)
    if not removed:
        raise ValueError(f"item {item!r} (collect step {step.id!r}) has no source edge to remove")

    snapshot = Snapshot.from_entities_relations(entities, kept)
    gt = GroundTruth(
        defect_class=DefectClass.missing_drop_source,
        injected_entities=[item],
        note=f"removed {len(removed)} GRANTS/DROPS_FROM source edge(s) feeding "
        f"collect step {step.id!r}'s item {item!r}",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 3. unreachable_target (design §3 strategy 3) — needs_nav=True
# ---------------------------------------------------------------------------
def _inject_unreachable_target(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    steps = sorted(
        (
            e for e in entities
            if e.type is NodeType.QUEST_STEP
            and e.attrs.get("kind") in ("talk", "turn_in")
            and e.attrs.get("target")
        ),
        key=lambda e: e.id,
    )
    if not steps:
        raise ValueError("base snapshot has no talk/turn_in step with a target")
    step = steps[rng.randrange(len(steps))]
    target_id = step.attrs["target"]
    target = next((e for e in entities if e.id == target_id), None)
    if target is None:
        raise ValueError(f"target entity {target_id!r} referenced by step {step.id!r} not found")

    region = next((e for e in entities if e.type is NodeType.REGION and "grid" in e.attrs), None)
    if region is None:
        raise ValueError("base snapshot has no region carrying grid metadata")
    grid = region.attrs["grid"]
    width, height = int(grid["width"]), int(grid["height"])
    if width < 2 or height < 2:
        raise ValueError("region grid too small to carve an isolated unreachable cell")

    # Move the target into the bottom-right corner and wall off its only two
    # in-bounds (4-directional) neighbors — a corner cell has no others — so
    # it is provably unreachable from anywhere else on the grid, including
    # the region's `start_pos`.
    corner = (width - 1, height - 1)
    blocked = {tuple(c) for c in grid.get("blocked", [])}
    blocked.add((width - 2, height - 1))
    blocked.add((width - 1, height - 2))
    region.attrs = {**region.attrs, "grid": {**grid, "blocked": [list(c) for c in sorted(blocked)]}}
    target.attrs = {**target.attrs, "pos": list(corner)}

    snapshot = Snapshot.from_entities_relations(entities, relations)
    gt = GroundTruth(
        defect_class=DefectClass.unreachable_target,
        injected_entities=[target_id],
        note=f"moved target {target_id!r} (step {step.id!r}) to corner {corner}, "
        f"walled off from the region's start_pos",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt, needs_nav=True)


# ---------------------------------------------------------------------------
# 4. cyclic_dependency (design §3 strategy 4)
# ---------------------------------------------------------------------------
def _inject_cyclic_dependency(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    suffix = _suffix(rng)
    step_a_id = f"step:injected-cycle-a-{suffix}"
    step_b_id = f"step:injected-cycle-b-{suffix}"
    # A brand-new, SELF-CONTAINED pair of steps (no HAS_STEP edge tying them to
    # any real quest) so the only defect introduced is the PRECEDES cycle
    # between them — no side effect on dead_quest/unsatisfiable_completion for
    # any existing quest (design §3 invariant (b): don't cross-contaminate).
    entities.append(Entity(id=step_a_id, type=NodeType.QUEST_STEP, attrs={"kind": "cycle_probe"}))
    entities.append(Entity(id=step_b_id, type=NodeType.QUEST_STEP, attrs={"kind": "cycle_probe"}))
    relations.append(Relation(
        id=f"rel:injected-precedes-{suffix}-ab",
        type=EdgeType.PRECEDES, src_id=step_a_id, dst_id=step_b_id,
    ))
    relations.append(Relation(
        id=f"rel:injected-precedes-{suffix}-ba",
        type=EdgeType.PRECEDES, src_id=step_b_id, dst_id=step_a_id,
    ))

    snapshot = Snapshot.from_entities_relations(entities, relations)
    gt = GroundTruth(
        defect_class=DefectClass.cyclic_dependency,
        injected_entities=[step_a_id, step_b_id],
        note=f"new self-contained sub-task steps {step_a_id!r}<->{step_b_id!r} "
        f"form a PRECEDES cycle",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 5. dead_quest (design §3 strategy 5)
# ---------------------------------------------------------------------------
def _inject_dead_quest(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    quests = sorted((e for e in entities if e.type is NodeType.QUEST), key=lambda e: e.id)
    if not quests:
        raise ValueError("base snapshot has no quest to target for dead_quest")
    quest = quests[rng.randrange(len(quests))]

    kept: list[Relation] = []
    removed: list[Relation] = []
    for r in relations:
        if r.type is EdgeType.STARTS_AT and r.src_id == quest.id:
            removed.append(r)
        else:
            kept.append(r)
    if not removed:
        raise ValueError(f"quest {quest.id!r} has no STARTS_AT relation to remove")

    snapshot = Snapshot.from_entities_relations(entities, kept)
    gt = GroundTruth(
        defect_class=DefectClass.dead_quest,
        injected_entities=[quest.id],
        note=f"removed the STARTS_AT giver edge from quest {quest.id!r}: it can "
        f"never be started",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 6. unsatisfiable_completion (design §3 strategy 6)
# ---------------------------------------------------------------------------
def _inject_unsatisfiable_completion(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    quests = sorted((e for e in entities if e.type is NodeType.QUEST), key=lambda e: e.id)
    if not quests:
        raise ValueError("base snapshot has no quest to target for unsatisfiable_completion")
    quest = quests[rng.randrange(len(quests))]
    suffix = _suffix(rng)
    orphan_id = f"step:injected-orphan-turn-in-{suffix}"

    # A new turn_in step HAS_STEP-attached to the quest but with NO incoming
    # PRECEDES edge from the quest's real chain: its completion condition can
    # never be reached by playing through the quest's other step(s).
    entities.append(Entity(id=orphan_id, type=NodeType.QUEST_STEP, attrs={"kind": "turn_in"}))
    relations.append(Relation(
        id=f"rel:injected-has-step-{suffix}",
        type=EdgeType.HAS_STEP, src_id=quest.id, dst_id=orphan_id,
    ))

    snapshot = Snapshot.from_entities_relations(entities, relations)
    gt = GroundTruth(
        defect_class=DefectClass.unsatisfiable_completion,
        injected_entities=[quest.id, orphan_id],
        note=f"quest {quest.id!r} gained an orphan turn_in step {orphan_id!r} with "
        f"no PRECEDES path from the quest's other step(s): completion can never fire",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 7. reward_out_of_range (design §3 strategy 7) — constraint `reward.gold <= 150`
# ---------------------------------------------------------------------------
def _inject_reward_out_of_range(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    quests = sorted(
        (
            e for e in entities
            if e.type is NodeType.QUEST
            and isinstance(e.attrs.get("reward"), dict)
            and "gold" in e.attrs["reward"]
        ),
        key=lambda e: e.id,
    )
    if not quests:
        raise ValueError("base snapshot has no quest with a gold reward to inflate")
    quest = quests[rng.randrange(len(quests))]
    new_gold = 151 + rng.randrange(1, 1000)  # over the 150 cap, varied by seed
    quest.attrs = {**quest.attrs, "reward": {**quest.attrs["reward"], "gold": new_gold}}

    snapshot = Snapshot.from_entities_relations(entities, relations)
    gt = GroundTruth(
        defect_class=DefectClass.reward_out_of_range,
        injected_entities=[quest.id],
        note=f"quest {quest.id!r} reward.gold set to {new_gold} (> 150 balance cap)",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 8. prob_sum_ne_1 (design §3 strategy 8) — drop-table entry probs must sum to 1
# ---------------------------------------------------------------------------
def _inject_prob_sum_ne_1(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    tables = sorted(
        (e for e in entities if e.type is NodeType.DROP_TABLE and e.attrs.get("entries")),
        key=lambda e: e.id,
    )
    if not tables:
        raise ValueError("base snapshot has no drop table to perturb")
    tbl = tables[rng.randrange(len(tables))]
    entries = [dict(en) for en in tbl.attrs["entries"]]
    # bump one entry's probability by a seeded NON-ZERO delta so the exact sum ≠ 1
    delta = [0.05, 0.1, 0.15, 0.2, 0.25, 0.3][rng.randrange(6)]
    idx = rng.randrange(len(entries))
    entries[idx] = {**entries[idx], "probability": round(entries[idx]["probability"] + delta, 4)}
    tbl.attrs = {**tbl.attrs, "entries": entries}

    snapshot = Snapshot.from_entities_relations(entities, relations)
    gt = GroundTruth(
        defect_class=DefectClass.prob_sum_ne_1,
        injected_entities=[tbl.id],
        note=f"drop table {tbl.id!r} entry #{idx} probability +{delta}: entries no "
        f"longer sum to exactly 1",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 9. non_monotonic_curve (design §3 strategy 9) — `kind=curve` FORMULA curve
# ---------------------------------------------------------------------------
def _inject_non_monotonic_curve(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    curves = sorted(
        (
            e for e in entities
            if e.type is NodeType.FORMULA
            and e.attrs.get("kind") == "curve"
            and isinstance(e.attrs.get("curve"), list)
            and len(e.attrs["curve"]) >= 2
        ),
        key=lambda e: e.id,
    )
    if not curves:
        raise ValueError("base snapshot has no kind=curve FORMULA with a >=2-point curve")
    f = curves[rng.randrange(len(curves))]
    curve = list(f.attrs["curve"])
    i = rng.randrange(len(curve) - 1)
    curve[i + 1] = curve[i] - (1 + rng.randrange(5))  # strictly below its predecessor
    f.attrs = {**f.attrs, "curve": curve}

    snapshot = Snapshot.from_entities_relations(entities, relations)
    gt = GroundTruth(
        defect_class=DefectClass.non_monotonic_curve,
        injected_entities=[f.id],
        note=f"formula {f.id!r} curve made non-monotonic at index {i + 1} ({curve})",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 10. gacha_expectation_violation (design §3 strategy 10)
# ---------------------------------------------------------------------------
def _inject_gacha_expectation_violation(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    pools = sorted(
        (
            e for e in entities
            if e.type is NodeType.GACHA_POOL
            and "base_rate" in e.attrs and "pity_threshold" in e.attrs
        ),
        key=lambda e: e.id,
    )
    if not pools:
        raise ValueError("base snapshot has no gacha pool with base_rate/pity_threshold")
    g = pools[rng.randrange(len(pools))]
    # much lower base_rate AND much higher pity → expected pulls exceed the
    # pool's declared max_expected_pulls budget (constraint violated).
    new_rate = round(g.attrs["base_rate"] / (10 + rng.randrange(10)), 6)
    new_pity = int(g.attrs["pity_threshold"]) + (40 + rng.randrange(40))
    g.attrs = {**g.attrs, "base_rate": new_rate, "pity_threshold": new_pity}

    snapshot = Snapshot.from_entities_relations(entities, relations)
    gt = GroundTruth(
        defect_class=DefectClass.gacha_expectation_violation,
        injected_entities=[g.id],
        note=f"gacha pool {g.id!r} base_rate→{new_rate}, pity→{new_pity}: expected "
        f"pulls now exceed max_expected_pulls budget",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 11. economy_collapse (design §3 strategy 11) — sim-detected (bucket=simulation)
# ---------------------------------------------------------------------------
def _inject_economy_collapse(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, relations = _clone_lists(base)
    monsters = sorted((e for e in entities if e.type is NodeType.MONSTER), key=lambda e: e.id)
    if not monsters:
        raise ValueError("base snapshot has no monster to turn into a runaway faucet")
    m = monsters[rng.randrange(len(monsters))]
    gmin = 500 + rng.randrange(500)
    gmax = gmin + 500 + rng.randrange(500)  # >>> any sink price → source ≫ sink
    m.attrs = {**m.attrs, "gold_min": gmin, "gold_max": gmax, "currency": "gold"}

    snapshot = Snapshot.from_entities_relations(entities, relations)
    gt = GroundTruth(
        defect_class=DefectClass.economy_collapse,
        injected_entities=[m.id],
        note=f"monster {m.id!r} gold_min/max→{gmin}/{gmax} with no offsetting sink: "
        f"gold supply diverges (economy collapse)",
    )
    return InjectedSample(snapshot=snapshot, ground_truth=gt)


# ---------------------------------------------------------------------------
# 12–15. narrative injectors (design §3 strategies 12–15) — bucket=llm_assisted.
# The snapshot graph stays CLEAN; the defect lives in a seeded
# `DialogueNarrativeInput` (the M2 Consistency quorum's input). Each embeds
# class-specific UPPERCASE markers so a property test can confirm the
# contradiction was injected WITHOUT running the consistency checker.
# ---------------------------------------------------------------------------
def _narrative_actor(entities: list[Entity], rng: random.Random, node_type: NodeType, fallback: str) -> str:
    cands = sorted((e.id for e in entities if e.type is node_type))
    return cands[rng.randrange(len(cands))] if cands else fallback


def _narrative_sample(
    base: Snapshot, defect: DefectClass, entity: str, text: str
) -> InjectedSample:
    # snapshot unchanged (clean graph); the DialogueNarrativeInput carries the defect
    snapshot = Snapshot.from_entities_relations(
        list(base.entities.values()), list(base.relations.values())
    )
    dlg = DialogueNarrativeInput(dialogue=text, narrative_constraint_ids=[_NARRATIVE_CONSTRAINT])
    gt = GroundTruth(defect_class=defect, injected_entities=[entity], note=text[:120])
    return InjectedSample(snapshot=snapshot, ground_truth=gt, dialogue=dlg)


def _inject_character_violation(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, _ = _clone_lists(base)
    who = _narrative_actor(entities, rng, NodeType.NPC, "npc:unknown")
    trait = ["honest", "loyal", "peaceful", "generous"][rng.randrange(4)]
    action = ["lies to rob the merchant", "betrays the outpost", "starts a brawl",
              "hoards the reward"][rng.randrange(4)]
    v = _suffix(rng)
    text = (f"[v{v}] {who} is established as TRAIT:{trait}. Later {who} says: "
            f"CONTRADICTION: '{action}' — irreconcilable with being {trait}.")
    return _narrative_sample(base, DefectClass.character_violation, who, text)


def _inject_spoiler(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, _ = _clone_lists(base)
    who = _narrative_actor(entities, rng, NodeType.NPC, "npc:unknown")
    twist = ["the king is the traitor", "the relic is cursed", "the guide is a spy",
             "the outpost falls"][rng.randrange(4)]
    v = _suffix(rng)
    text = (f"[v{v}] REVEAL: '{twist}' is gated behind the finale quest. "
            f"{who} blurts it in the intro — SPOILER: revealed far too early.")
    return _narrative_sample(base, DefectClass.spoiler, who, text)


def _inject_faction_violation(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, _ = _clone_lists(base)
    who = _narrative_actor(entities, rng, NodeType.MONSTER, "mon:wolves")
    v = _suffix(rng)
    text = (f"[v{v}] ENEMIES: the Wolves ({who}) and the Outpost Guard are sworn foes. "
            f"Yet {who} says: ALLIANCE: 'We Guards happily fight alongside you.'")
    return _narrative_sample(base, DefectClass.faction_violation, who, text)


def _inject_uniqueness_violation(base: Snapshot, rng: random.Random) -> InjectedSample:
    entities, _ = _clone_lists(base)
    npcs = sorted(e.id for e in entities if e.type is NodeType.NPC)
    a = npcs[0] if npcs else "npc:a"
    b = npcs[1] if len(npcs) > 1 else "npc:b"
    v = _suffix(rng)
    text = (f"[v{v}] Lore: there is exactly one Chosen. {a} claims UNIQUE-ROLE:Chosen. "
            f"{b} also claims UNIQUE-ROLE:Chosen — two holders of a one-holder role.")
    return _narrative_sample(base, DefectClass.uniqueness_violation, a, text)


_INJECTORS: dict[DefectClass, Callable[[Snapshot, random.Random], InjectedSample]] = {
    DefectClass.dangling_reference: _inject_dangling_reference,
    DefectClass.missing_drop_source: _inject_missing_drop_source,
    DefectClass.unreachable_target: _inject_unreachable_target,
    DefectClass.cyclic_dependency: _inject_cyclic_dependency,
    DefectClass.dead_quest: _inject_dead_quest,
    DefectClass.unsatisfiable_completion: _inject_unsatisfiable_completion,
    DefectClass.reward_out_of_range: _inject_reward_out_of_range,
    DefectClass.prob_sum_ne_1: _inject_prob_sum_ne_1,
    DefectClass.non_monotonic_curve: _inject_non_monotonic_curve,
    DefectClass.gacha_expectation_violation: _inject_gacha_expectation_violation,
    DefectClass.economy_collapse: _inject_economy_collapse,
    DefectClass.character_violation: _inject_character_violation,
    DefectClass.spoiler: _inject_spoiler,
    DefectClass.faction_violation: _inject_faction_violation,
    DefectClass.uniqueness_violation: _inject_uniqueness_violation,
}


def inject(base: Snapshot, defect: DefectClass, seed: int) -> InjectedSample:
    """Mutate `base` to introduce one instance of `defect`, deterministically
    keyed on `(base.snapshot_id, defect.value, seed)` (module docstring).

    Dispatches to a per-class injector — all 15 taxonomy classes are
    registered (6 structural / 5 numeric-economy / 4 narrative).
    """
    handler = _INJECTORS.get(defect)
    if handler is None:
        raise NotImplementedError(f"no injector registered yet for {defect.value!r}")
    rng = _seeded_rng(base, defect, seed)
    return handler(base, rng)
