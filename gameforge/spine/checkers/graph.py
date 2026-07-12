"""GraphChecker (M1 Task 4): hand-rolled graph algorithms, 7 structural defect classes.

No `networkx` dependency (M1-D3): BFS reachability, Tarjan SCC (cycle detection),
and Kahn topological sort are hand-written here and doubly serve as the "naive
reference implementation" anchor for differential testing (contract §3 / §12A.1)
— `ASPChecker` (Task 5) must agree with these pure functions on shared
structural constraints.

The 7 defect classes (each Finding: oracle_type="deterministic", status="confirmed",
source="checker", producer_id="graph"):
  1. dangling_reference       — a relation endpoint does not exist as an entity.
  2. missing_drop_source      — a `collect` step's item has no GRANTS/DROPS_FROM
                                 source (reachable-from-start when nav given).
  3. unreachable_target       — a quest step's talk/turn_in target is unreachable
                                 from the quest giver, or its destination has an
                                 access gate the quest neither requires nor unlocks.
  4. cyclic_dependency        — the repeatable HAS_STEP/PRECEDES/REQUIRES
                                 subgraph contains a cycle.
  5. dead_quest                — a quest has no giver (STARTS_AT) or no steps
                                 (HAS_STEP) at all: it can never be started.
  6. unsatisfiable_completion — a quest's turn_in step is not reachable (via
                                 PRECEDES) from the quest's other entry step(s):
                                 the completion condition can never fire.
  7. isolated_node            — a key entity (QUEST/NPC/ITEM/MONSTER) has zero
                                 incoming and zero outgoing relations.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable

from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph, NavProvider

_SOURCE_EDGES = (EdgeType.GRANTS, EdgeType.DROPS_FROM)
_DEPENDENCY_EDGES = (EdgeType.HAS_STEP, EdgeType.PRECEDES, EdgeType.REQUIRES)
_ACCESS_PROOF_EDGES = (EdgeType.REQUIRES, EdgeType.UNLOCKS)
_KEY_NODE_TYPES = (NodeType.QUEST, NodeType.NPC, NodeType.ITEM, NodeType.MONSTER)

EmitFn = Callable[..., None]


# --------------------------------------------------------------------------
# Pure, independently-testable graph algorithms (contract §3 differential
# anchor + §12A.1 property-test line). `adj` is a plain dict[node, list[node]].
# --------------------------------------------------------------------------

def reachable_set(adj: dict, src: Any) -> set:
    """BFS forward-reachable set from `src` (src included)."""
    seen = {src}
    q: deque = deque([src])
    while q:
        u = q.popleft()
        for v in adj.get(u, []):
            if v not in seen:
                seen.add(v)
                q.append(v)
    return seen


def find_cycles(adj: dict) -> list[list[Any]]:
    """Tarjan SCC: return every cycle-bearing component (SCC size > 1, or a
    single node with a self-loop). Non-recursive to tolerate arbitrary graph
    sizes without hitting Python's recursion limit. Components and their nodes
    use canonical order so Finding evidence is stable across hash seeds.
    """
    index_counter = 0
    index: dict[Any, int] = {}
    lowlink: dict[Any, int] = {}
    on_stack: dict[Any, bool] = {}
    stack: list[Any] = []
    result: list[list[Any]] = []

    nodes: set[Any] = set(adj.keys())
    for children in adj.values():
        nodes.update(children)

    for start in nodes:
        if start in index:
            continue
        # explicit work-stack DFS: frames of (node, iterator-index-into-children)
        work: list[list[Any]] = [[start, 0]]
        index[start] = lowlink[start] = index_counter
        index_counter += 1
        stack.append(start)
        on_stack[start] = True

        while work:
            frame = work[-1]
            v, i = frame[0], frame[1]
            children = adj.get(v, [])
            if i < len(children):
                w = children[i]
                frame[1] += 1
                if w not in index:
                    index[w] = lowlink[w] = index_counter
                    index_counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append([w, 0])
                elif on_stack.get(w, False):
                    lowlink[v] = min(lowlink[v], index[w])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[v])
                if lowlink[v] == index[v]:
                    comp: list[Any] = []
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        comp.append(w)
                        if w == v:
                            break
                    if len(comp) > 1 or v in adj.get(v, []):
                        result.append(comp)
    components = [sorted(component, key=repr) for component in result]
    return sorted(components, key=lambda component: tuple(map(repr, component)))


def topo_order(adj: dict) -> list[Any] | None:
    """Kahn's algorithm. Returns None if the graph is cyclic (no total order)."""
    nodes: set[Any] = set(adj.keys())
    for children in adj.values():
        nodes.update(children)
    indeg: dict[Any, int] = {n: 0 for n in nodes}
    for u in nodes:
        for v in adj.get(u, []):
            indeg[v] += 1
    q: deque = deque(sorted((n for n in nodes if indeg[n] == 0), key=repr))
    order: list[Any] = []
    indeg_work = dict(indeg)
    while q:
        u = q.popleft()
        order.append(u)
        for v in adj.get(u, []):
            indeg_work[v] -= 1
            if indeg_work[v] == 0:
                q.append(v)
    return order if len(order) == len(nodes) else None


def _dependency_adj(g: IRGraph) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {}
    for r in g.all_relations():
        if r.type in _DEPENDENCY_EDGES and (r.attrs or {}).get("repeatability") != "once":
            adj.setdefault(r.src_id, []).append(r.dst_id)
    return adj


class GraphChecker:
    id = "graph"

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        g = snapshot.to_graph()
        run_id = f"graph@{snapshot.snapshot_id[:23]}"
        findings: list[Finding] = []
        n = 0

        def emit(defect_class, severity, entities, evidence, repro, message):
            nonlocal n
            f = Finding(
                id=f"{run_id}#{n}", source="checker", producer_id=self.id,
                producer_run_id=run_id, oracle_type="deterministic",
                defect_class=defect_class, severity=severity,
                snapshot_id=snapshot.snapshot_id, entities=entities,
                evidence=evidence, minimal_repro=repro, status="confirmed",
                message=message,
            )
            n += 1
            findings.append(f)

        self._dangling_reference(g, emit)
        self._missing_drop_source(g, nav, emit)
        self._unreachable_target(g, nav, emit)
        self._gated_destination(g, emit)
        self._cyclic_dependency(g, emit)
        self._dead_quest(g, emit)
        self._unsatisfiable_completion(g, emit)
        self._isolated_node(g, emit)
        return findings

    # --- 1. dangling_reference ---
    def _dangling_reference(self, g: IRGraph, emit: EmitFn) -> None:
        for r in list(g.all_relations()):
            missing = [ep for ep in (r.src_id, r.dst_id) if g.get_node(ep) is None]
            if missing:
                emit(
                    "dangling_reference", "critical", [r.src_id, r.dst_id],
                    {"relation": r.id, "edge_type": r.type.value, "missing": missing},
                    {"relation": r.id,
                     "source_ref": r.source_ref.model_dump() if r.source_ref else None},
                    f"Relation {r.id} ({r.type.value}) points at missing entities {missing}",
                )

    # --- 2. missing_drop_source ---
    def _missing_drop_source(self, g: IRGraph, nav: NavProvider | None, emit: EmitFn) -> None:
        for step in g.nodes_of_type(NodeType.QUEST_STEP):
            if step.attrs.get("kind") != "collect":
                continue
            item = step.attrs.get("item")
            if item is None:
                continue
            sources = [
                r.src_id for r in g.all_relations()
                if r.type in _SOURCE_EDGES and r.dst_id == item
            ]
            reachable_sources = sources
            if nav is not None and sources:
                reachable_sources = [
                    sid for sid in sources
                    if (sp := nav.pos_of(sid)) is not None and _nav_start_reachable(nav, sp)
                ]
            if not reachable_sources:
                emit(
                    "missing_drop_source", "critical", [step.id, item],
                    {"item": item, "known_sources": sources,
                     "reachable": bool(reachable_sources)},
                    {"entity": step.id,
                     "source_ref": step.source_ref.model_dump() if step.source_ref else None},
                    f"collect step {step.id!r} needs item {item!r} but has no "
                    f"{'reachable ' if sources else ''}source",
                )

    # --- 3. unreachable_target ---
    def _unreachable_target(self, g: IRGraph, nav: NavProvider | None, emit: EmitFn) -> None:
        if nav is None:
            return  # cannot prove unreachability without spatial ground truth
        for quest in g.nodes_of_type(NodeType.QUEST):
            giver_rels = g.neighbors(quest.id, EdgeType.STARTS_AT, direction="out")
            if not giver_rels:
                continue
            giver = giver_rels[0].dst_id
            start_pos = nav.pos_of(giver)
            if start_pos is None:
                continue
            for step_rel in g.neighbors(quest.id, EdgeType.HAS_STEP, direction="out"):
                step = g.get_node(step_rel.dst_id)
                if step is None or step.attrs.get("kind") not in ("talk", "turn_in"):
                    continue
                target = step.attrs.get("target")
                if not target:
                    continue
                target_pos = nav.pos_of(target)
                if target_pos is None:
                    continue
                if not nav.reachable(start_pos, target_pos):
                    emit(
                        "unreachable_target", "critical", [quest.id, step.id, target],
                        {"quest": quest.id, "step": step.id, "giver": giver,
                         "target": target, "unreachable_pair": [giver, target]},
                        {"entity": step.id,
                         "source_ref": step.source_ref.model_dump() if step.source_ref else None},
                        f"Quest {quest.id} step {step.id} target {target!r} "
                        f"unreachable from giver {giver!r}",
                    )

    def _gated_destination(self, g: IRGraph, emit: EmitFn) -> None:
        for quest in g.nodes_of_type(NodeType.QUEST):
            proof_relations = [
                relation
                for relation in g.neighbors(quest.id, direction="out")
                if relation.type in _ACCESS_PROOF_EDGES
            ]
            proofs_by_gate: dict[str, list[str]] = {}
            for relation in proof_relations:
                proofs_by_gate.setdefault(relation.dst_id, []).append(relation.id)

            seen: set[tuple[str, str, str]] = set()
            for step_relation in g.neighbors(quest.id, EdgeType.HAS_STEP, direction="out"):
                step = g.get_node(step_relation.dst_id)
                if step is None:
                    continue
                for location in g.neighbors(step.id, EdgeType.LOCATED_IN, direction="out"):
                    region = g.get_node(location.dst_id)
                    if region is None:
                        continue
                    for gate_relation in g.neighbors(
                        region.id,
                        EdgeType.GATED_BY,
                        direction="out",
                    ):
                        gate = gate_relation.dst_id
                        access_proofs = sorted(proofs_by_gate.get(gate, []))
                        if access_proofs:
                            continue
                        key = (step.id, region.id, gate)
                        if key in seen:
                            continue
                        seen.add(key)
                        emit(
                            "unreachable_target",
                            "critical",
                            [quest.id, step.id, region.id, gate],
                            {
                                "quest": quest.id,
                                "step": step.id,
                                "region": region.id,
                                "gate": gate,
                                "access_proofs": access_proofs,
                            },
                            {
                                "entity": step.id,
                                "source_ref": (
                                    step.source_ref.model_dump() if step.source_ref else None
                                ),
                            },
                            f"Quest {quest.id} step {step.id} enters gated region "
                            f"{region.id} without requiring or unlocking {gate}",
                        )

    # --- 4. cyclic_dependency ---
    def _cyclic_dependency(self, g: IRGraph, emit: EmitFn) -> None:
        adj = _dependency_adj(g)
        for cycle in find_cycles(adj):
            emit(
                "cyclic_dependency", "critical", cycle,
                {"cycle_path": cycle},
                {"entity": cycle[0], "cycle": cycle},
                f"Quest step dependency cycle among: {', '.join(cycle)}",
            )

    # --- 5. dead_quest ---
    def _dead_quest(self, g: IRGraph, emit: EmitFn) -> None:
        for quest in g.nodes_of_type(NodeType.QUEST):
            giver_rels = g.neighbors(quest.id, EdgeType.STARTS_AT, direction="out")
            step_rels = g.neighbors(quest.id, EdgeType.HAS_STEP, direction="out")
            if not giver_rels or not step_rels:
                emit(
                    "dead_quest", "critical", [quest.id],
                    {"quest": quest.id, "has_giver": bool(giver_rels),
                     "has_steps": bool(step_rels)},
                    {"entity": quest.id,
                     "source_ref": quest.source_ref.model_dump() if quest.source_ref else None},
                    f"Quest {quest.id} can never be started "
                    f"(has_giver={bool(giver_rels)}, has_steps={bool(step_rels)})",
                )

    # --- 6. unsatisfiable_completion ---
    def _unsatisfiable_completion(self, g: IRGraph, emit: EmitFn) -> None:
        # Assumes PRECEDES encodes prerequisite ordering: a turn_in that is
        # itself a PRECEDES-entry among multiple steps is treated as unsatisfiable.
        for quest in g.nodes_of_type(NodeType.QUEST):
            step_ids = [
                r.dst_id for r in g.neighbors(quest.id, EdgeType.HAS_STEP, direction="out")
                if g.get_node(r.dst_id) is not None
            ]
            steps = set(step_ids)
            if not steps:
                continue
            turn_ins = [
                sid for sid in steps
                if (s := g.get_node(sid)) is not None and s.attrs.get("kind") == "turn_in"
            ]
            if not turn_ins:
                continue

            adj: dict[str, list[str]] = {sid: [] for sid in steps}
            indeg: dict[str, int] = {sid: 0 for sid in steps}
            for r in g.all_relations():
                if r.type is EdgeType.PRECEDES and r.src_id in steps and r.dst_id in steps:
                    adj[r.src_id].append(r.dst_id)
                    indeg[r.dst_id] += 1
            entries = [sid for sid in steps if indeg[sid] == 0]

            for turn_in in turn_ins:
                if turn_in in entries:
                    if len(steps) == 1:
                        continue  # single-step (turn_in only) quest: trivially completable
                    reachable: set[str] = set()
                else:
                    others = [e for e in entries if e != turn_in]
                    reachable = set()
                    for e in others:
                        reachable |= reachable_set(adj, e)
                    if turn_in in reachable:
                        continue
                turn_in_step = g.get_node(turn_in)
                emit(
                    "unsatisfiable_completion", "critical", [quest.id, turn_in],
                    {"quest": quest.id, "turn_in_step": turn_in,
                     "entries": sorted(e for e in entries if e != turn_in),
                     "reachable_from_entries": sorted(reachable)},
                    {"entity": turn_in,
                     "source_ref": turn_in_step.source_ref.model_dump()
                     if turn_in_step and turn_in_step.source_ref else None},
                    f"Quest {quest.id} completion step {turn_in} is unreachable "
                    f"from its other step(s): completion condition can never fire",
                )

    # --- 7. isolated_node ---
    def _isolated_node(self, g: IRGraph, emit: EmitFn) -> None:
        for e in g.all_entities():
            if e.type not in _KEY_NODE_TYPES:
                continue
            out_rels = g.neighbors(e.id, direction="out")
            in_rels = g.neighbors(e.id, direction="in")
            if not out_rels and not in_rels:
                emit(
                    "isolated_node", "minor", [e.id],
                    {"entity": e.id, "type": e.type.value},
                    {"entity": e.id,
                     "source_ref": e.source_ref.model_dump() if e.source_ref else None},
                    f"{e.type.value} {e.id!r} has no relations (isolated)",
                )


def _nav_start_reachable(nav: NavProvider, dst_pos) -> bool:
    start = nav.pos_of("__player_start__")
    if start is None:
        return True  # no start anchor known -> existence is sufficient
    return nav.reachable(start, dst_pos)
