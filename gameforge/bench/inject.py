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

Only the 6 STRUCTURAL injectors are implemented in this milestone slice
(Task 2): dangling_reference, missing_drop_source, unreachable_target,
cyclic_dependency, dead_quest, unsatisfiable_completion. The 5 numeric/economy
injectors (Task 3) and 4 narrative injectors (Task 4) register into the same
`inject()` dispatch table later — declared in `taxonomy.DefectClass` now,
implementation deferred (不简化只延后), not stubbed here.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Callable

from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.snapshot import Snapshot

_SOURCE_EDGES = (EdgeType.GRANTS, EdgeType.DROPS_FROM)


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


_INJECTORS: dict[DefectClass, Callable[[Snapshot, random.Random], InjectedSample]] = {
    DefectClass.dangling_reference: _inject_dangling_reference,
    DefectClass.missing_drop_source: _inject_missing_drop_source,
    DefectClass.unreachable_target: _inject_unreachable_target,
    DefectClass.cyclic_dependency: _inject_cyclic_dependency,
    DefectClass.dead_quest: _inject_dead_quest,
    DefectClass.unsatisfiable_completion: _inject_unsatisfiable_completion,
}


def inject(base: Snapshot, defect: DefectClass, seed: int) -> InjectedSample:
    """Mutate `base` to introduce one instance of `defect`, deterministically
    keyed on `(base.snapshot_id, defect.value, seed)` (module docstring).

    Dispatches to a per-class injector. Only the 6 structural classes (Task 2
    of this milestone) are registered so far; the remaining 9 (numeric/
    economy Task 3, narrative Task 4) raise `NotImplementedError` until their
    tasks land — declared in `taxonomy.DefectClass` now, not stubbed here
    (不简化只延后).
    """
    handler = _INJECTORS.get(defect)
    if handler is None:
        raise NotImplementedError(f"no injector registered yet for {defect.value!r}")
    rng = _seeded_rng(base, defect, seed)
    return handler(base, rng)
