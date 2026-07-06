"""Minimal deterministic structural checker (M0a).

Three rules, each emitting a deterministic Finding with evidence + minimal_repro:
  1. reference integrity  — every relation endpoint resolves; talk/turn_in targets exist.
  2. collect-needs-source — every collect step's item has a source (GRANTS/DROPS_FROM);
     reachable in-region when a NavProvider is supplied.
  3. quest-DAG-acyclic    — HAS_STEP + PRECEDES subgraph has no cycle.

The full Graph/ASP/SMT suite + DSL→checker compilation is M1; this is the minimal
oracle that gates the M0a vertical slice.
"""

from __future__ import annotations

from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph, NavProvider

_SOURCE_EDGES = (EdgeType.GRANTS, EdgeType.DROPS_FROM)


class StructuralChecker:
    id = "structural"

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        g = snapshot.to_graph()
        run_id = f"structural@{snapshot.snapshot_id[:23]}"
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

        self._reference_integrity(g, emit)
        self._collect_needs_source(g, nav, emit)
        self._quest_dag_acyclic(g, emit)
        return findings

    # --- rule 1 ---
    def _reference_integrity(self, g: IRGraph, emit) -> None:
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

    # --- rule 2 ---
    def _collect_needs_source(self, g: IRGraph, nav: NavProvider | None, emit) -> None:
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

    # --- rule 3 ---
    def _quest_dag_acyclic(self, g: IRGraph, emit) -> None:
        adj: dict[str, list[str]] = {}
        for r in g.all_relations():
            if r.type in (EdgeType.HAS_STEP, EdgeType.PRECEDES):
                adj.setdefault(r.src_id, []).append(r.dst_id)
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {}

        def dfs(node: str, stack: list[str]) -> list[str] | None:
            color[node] = GRAY
            stack.append(node)
            for nxt in adj.get(node, []):
                c = color.get(nxt, WHITE)
                if c == GRAY:  # back edge → cycle
                    return stack[stack.index(nxt):] + [nxt]
                if c == WHITE:
                    found = dfs(nxt, stack)
                    if found:
                        return found
            stack.pop()
            color[node] = BLACK
            return None

        for node in list(adj.keys()):
            if color.get(node, WHITE) == WHITE:
                cycle = dfs(node, [])
                if cycle:
                    emit(
                        "cyclic_dependency", "critical", cycle,
                        {"cycle_path": cycle},
                        {"entity": cycle[0], "cycle": cycle},
                        f"Quest step dependency cycle: {' -> '.join(cycle)}",
                    )
                    return


def _nav_start_reachable(nav: NavProvider, dst_pos) -> bool:
    start = nav.pos_of("__player_start__")
    if start is None:
        return True  # no start anchor → existence is sufficient for M0a
    return nav.reachable(start, dst_pos)
