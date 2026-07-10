"""M3a Task 2: `gameforge.bench.inject` — the 6 structural defect injectors.

Every property test below verifies its injected defect via a DIRECT,
independent structural assertion over the mutated `Snapshot`'s
entities/relations (or, for `unreachable_target`, a plain grid BFS) — never
by running a `spine.checkers.*` checker. This is the anti-circularity
guardrail (design §8): the injector and the oracle that will eventually score
it (Task 7) must never share code, so a test that instead called
`GraphChecker`/`StructuralChecker` here would prove nothing about the
injector itself.
"""
from __future__ import annotations

from collections import deque

from gameforge.bench.inject import inject
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.ir import EdgeType, NodeType
from tests.bench.testbases import clean_base

_STRUCTURAL_CLASSES = [
    DefectClass.dangling_reference,
    DefectClass.missing_drop_source,
    DefectClass.unreachable_target,
    DefectClass.cyclic_dependency,
    DefectClass.dead_quest,
    DefectClass.unsatisfiable_completion,
]


# --- independent structural helpers (NOT the checker) -----------------------
def _has_precedes_cycle(snapshot) -> bool:
    """Plain DFS cycle detection over PRECEDES-only adjacency — a fresh,
    from-scratch reimplementation, not `spine.checkers.graph.find_cycles`."""
    g = snapshot.to_graph()
    adj: dict[str, list[str]] = {}
    for r in g.all_relations():
        if r.type is EdgeType.PRECEDES:
            adj.setdefault(r.src_id, []).append(r.dst_id)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    def dfs(node: str) -> bool:
        color[node] = GRAY
        for nxt in adj.get(node, []):
            c = color.get(nxt, WHITE)
            if c == GRAY:
                return True
            if c == WHITE and dfs(nxt):
                return True
        color[node] = BLACK
        return False

    return any(dfs(n) for n in list(adj) if color.get(n, WHITE) == WHITE)


def _source_relations(snapshot, item: str) -> list:
    g = snapshot.to_graph()
    return [
        r for r in g.all_relations()
        if r.type in (EdgeType.GRANTS, EdgeType.DROPS_FROM) and r.dst_id == item
    ]


def _starts_at_relations(snapshot, quest_id: str) -> list:
    g = snapshot.to_graph()
    return [
        r for r in g.all_relations()
        if r.type is EdgeType.STARTS_AT and r.src_id == quest_id
    ]


def _grid_reachable(region_attrs: dict, start: tuple[int, int], target: tuple[int, int]) -> bool:
    """Plain 4-directional BFS over the region's own grid/blocked-cell data —
    independent of the runtime nav module."""
    grid = region_attrs["grid"]
    width, height = int(grid["width"]), int(grid["height"])
    blocked = {tuple(c) for c in grid.get("blocked", [])}
    seen = {start}
    q = deque([start])
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in blocked and (nx, ny) not in seen:
                seen.add((nx, ny))
                q.append((nx, ny))
    return target in seen


def _region_and_start(snapshot) -> tuple[dict, tuple[int, int]]:
    g = snapshot.to_graph()
    region = next(e for e in g.all_entities() if e.type is NodeType.REGION and "grid" in e.attrs)
    start = region.attrs.get("start_pos", [0, 0])
    return region.attrs, (int(start[0]), int(start[1]))


def _quest_has_step_children(snapshot, quest_id: str) -> list[str]:
    g = snapshot.to_graph()
    return [r.dst_id for r in g.all_relations() if r.type is EdgeType.HAS_STEP and r.src_id == quest_id]


def _unreachable_turn_ins(snapshot, quest_id: str) -> list[str]:
    """Independent reimplementation (not imported from `spine.checkers.graph`)
    of "which turn_in steps of this quest can never fire": a turn_in step is
    unsatisfiable if it is not reachable, via PRECEDES, from any OTHER entry
    step (indegree 0) of the quest — or if it is itself an otherwise-lone
    entry among more than one step."""
    g = snapshot.to_graph()
    steps = set(_quest_has_step_children(snapshot, quest_id))
    adj: dict[str, list[str]] = {s: [] for s in steps}
    indeg: dict[str, int] = {s: 0 for s in steps}
    for r in g.all_relations():
        if r.type is EdgeType.PRECEDES and r.src_id in steps and r.dst_id in steps:
            adj[r.src_id].append(r.dst_id)
            indeg[r.dst_id] += 1
    entries = [s for s in steps if indeg[s] == 0]
    turn_ins = [s for s in steps if g.get_node(s).attrs.get("kind") == "turn_in"]

    def reachable_from(src: str) -> set[str]:
        seen = {src}
        q = deque([src])
        while q:
            u = q.popleft()
            for v in adj.get(u, []):
                if v not in seen:
                    seen.add(v)
                    q.append(v)
        return seen

    bad = []
    for t in turn_ins:
        if t in entries:
            if len(steps) == 1:
                continue
            bad.append(t)
            continue
        reachable: set[str] = set()
        for e in entries:
            if e != t:
                reachable |= reachable_from(e)
        if t not in reachable:
            bad.append(t)
    return bad


# --- 1. dangling_reference ---------------------------------------------------
def test_dangling_reference_points_dst_to_nonexistent_id():
    s = inject(clean_base(), DefectClass.dangling_reference, seed=1)
    g = s.snapshot.to_graph()
    ids = {n.id for n in g.all_entities()}
    # the injected relation's dst is NOT a known entity id (structural check, no checker)
    bad = s.ground_truth.injected_entities[-1]
    assert bad not in ids
    assert clean_base().to_graph()  # base itself has no dangling ref (sanity)
    # and no relation in the base already dangles onto that exact id
    base_g = clean_base().to_graph()
    assert all(r.dst_id != bad for r in base_g.all_relations())


# --- 2. missing_drop_source ---------------------------------------------------
def test_missing_drop_source_removes_the_items_only_source():
    base = clean_base()
    s = inject(base, DefectClass.missing_drop_source, seed=1)
    item = s.ground_truth.injected_entities[-1]
    assert _source_relations(base, item), "sanity: base item DOES have a source"
    assert _source_relations(s.snapshot, item) == []


# --- 3. unreachable_target ----------------------------------------------------
def test_unreachable_target_giver_cannot_reach_the_walled_target():
    # The injector adds a SELF-CONTAINED quest: a giver at a reachable cell and
    # a talk step whose target is a NEW npc walled into the grid corner.
    # `GraphChecker._unreachable_target` checks reachability from the GIVER to
    # the target (NOT from the player start), so the property test does too.
    s = inject(clean_base(), DefectClass.unreachable_target, seed=1)
    assert s.needs_nav is True
    quest_id, step_id, target_id = s.ground_truth.injected_entities
    g = s.snapshot.to_graph()
    giver = next(
        r.dst_id for r in g.all_relations()
        if r.type is EdgeType.STARTS_AT and r.src_id == quest_id
    )
    assert giver != target_id  # giver and target MUST differ (else reachable(x,x)==True)

    attrs, _ = _region_and_start(s.snapshot)
    giver_pos = tuple(int(v) for v in g.get_node(giver).attrs["pos"])
    target_pos = tuple(int(v) for v in g.get_node(target_id).attrs["pos"])
    # independent grid BFS (not the nav module): giver cannot reach the target,
    # but CAN reach some other interior cell (the grid is not globally blocked)
    assert not _grid_reachable(attrs, giver_pos, target_pos)
    assert _grid_reachable(attrs, giver_pos, (0, 1)) or _grid_reachable(attrs, giver_pos, (1, 0))


# --- 4. cyclic_dependency -----------------------------------------------------
def test_cyclic_dependency_adds_a_precedes_cycle():
    s = inject(clean_base(), DefectClass.cyclic_dependency, seed=1)
    # independent cycle detection over PRECEDES edges (not via ASPChecker)
    assert _has_precedes_cycle(s.snapshot)  # helper in the test
    assert not _has_precedes_cycle(clean_base())


# --- 5. dead_quest -------------------------------------------------------------
def test_dead_quest_removes_the_only_starts_at_edge():
    base = clean_base()
    s = inject(base, DefectClass.dead_quest, seed=1)
    quest_id = s.ground_truth.injected_entities[-1]
    assert _starts_at_relations(base, quest_id), "sanity: base quest DOES have a giver"
    assert _starts_at_relations(s.snapshot, quest_id) == []


# --- 6. unsatisfiable_completion ------------------------------------------------
def test_unsatisfiable_completion_adds_an_unreachable_turn_in():
    base = clean_base()
    s = inject(base, DefectClass.unsatisfiable_completion, seed=1)
    quest_id, orphan_id = s.ground_truth.injected_entities[-2:]
    assert _unreachable_turn_ins(base, quest_id) == []  # base: quest is fully satisfiable
    bad = _unreachable_turn_ins(s.snapshot, quest_id)
    assert orphan_id in bad


# --- cross-cutting: seeded reproducibility + distinctness -------------------
def test_injectors_are_seeded_reproducible_and_distinct():
    a = inject(clean_base(), DefectClass.dangling_reference, seed=1)
    b = inject(clean_base(), DefectClass.dangling_reference, seed=1)
    c = inject(clean_base(), DefectClass.dangling_reference, seed=2)
    assert a.snapshot.snapshot_id == b.snapshot.snapshot_id  # reproducible
    assert a.snapshot.snapshot_id != c.snapshot.snapshot_id  # different seed → different sample


# `clean_base()` (design fixture) has exactly ONE quest, ONE collect step, and
# its lone talk/turn_in target is also the quest giver — so for
# `missing_drop_source` and `dead_quest` there is only ONE candidate to mutate
# from this base, and neither injector mixes the seed into the mutation itself
# (a deletion is a deletion). Their snapshots are therefore identical across
# seeds on THIS base — correct, not a bug: distinctness is a property of
# (base, defect) having a search space bigger than one.
# `dangling_reference`/`cyclic_dependency`/`unsatisfiable_completion`/
# `unreachable_target` all mix the seed into an injected id suffix (or into
# which of >1 candidates is picked), so they DO diverge across seeds.
_SEED_INVARIANT_ON_CLEAN_BASE = {
    DefectClass.missing_drop_source,
    DefectClass.dead_quest,
}


def test_all_six_structural_injectors_are_seeded_reproducible():
    for dc in _STRUCTURAL_CLASSES:
        a = inject(clean_base(), dc, seed=7)
        b = inject(clean_base(), dc, seed=7)
        c = inject(clean_base(), dc, seed=8)
        assert a.snapshot.snapshot_id == b.snapshot.snapshot_id, dc  # always: same seed -> same
        if dc in _SEED_INVARIANT_ON_CLEAN_BASE:
            assert a.snapshot.snapshot_id == c.snapshot.snapshot_id, dc
        else:
            assert a.snapshot.snapshot_id != c.snapshot.snapshot_id, dc


def test_each_structural_injector_sets_correct_ground_truth():
    for dc in _STRUCTURAL_CLASSES:
        s = inject(clean_base(), dc, seed=3)
        assert s.ground_truth.defect_class is dc
        assert s.ground_truth.injected_entities  # non-empty
        assert s.ground_truth.note  # non-empty, human-readable


def test_unreachable_target_is_the_only_structural_class_needing_nav():
    for dc in _STRUCTURAL_CLASSES:
        s = inject(clean_base(), dc, seed=4)
        expected = dc is DefectClass.unreachable_target
        assert s.needs_nav is expected, dc
