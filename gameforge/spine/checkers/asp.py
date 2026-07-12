"""ASPChecker (M1 Task 5): Clingo-encoded structural defect checker.

Encodes the IR graph as ASP facts (`ir_to_asp_facts`, an independently
unit-testable pure function) and evaluates a built-in `.lp` rule set that
derives `violation(Class, EntityA, EntityB)` atoms for the structural defect
classes shared with `GraphChecker` (contract §3 / §12A.1 differential anchor):

  - cyclic_dependency   — mutual reachability over repeatable
                           HAS_STEP/PRECEDES/REQUIRES edges (an SCC of size >
                           1, or a self-loop), computed in Clingo via
                           `dep_reach/2` transitive closure.
  - missing_drop_source — a `collect` QUEST_STEP whose `item` attr has no
                           GRANTS/DROPS_FROM edge landing on it.

`ASPChecker` must independently derive its verdict from this ASP encoding —
it never calls `GraphChecker` (that would make the differential test in
`tests/spine/checkers/test_asp_vs_graph_differential.py` a tautology).

Grounding budget (M1-D7): before grounding, a cheap atom-count estimate is
checked against `grounding_budget_atoms` (default 200_000); if exceeded, no
Clingo call is made at all and every shared defect class is reported as a
Finding with `status="unproven"` (NEVER a silent pass). After grounding, the
solve is wall-clock bounded (`wall_clock_budget_s`, default 10s) via Clingo's
async solve handle; a timeout degrades the same way.
"""

from __future__ import annotations

import clingo

from gameforge.contracts.findings import Finding
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph, NavProvider

_SHARED_DEFECT_CLASSES = ("cyclic_dependency", "missing_drop_source")

_BUILTIN_RULES = """
#defined attr/3.
#defined edge_attr/3.

% --- cyclic_dependency: repeatable dependency edges, mutual reachability ---
dependency_type("HAS_STEP").
dependency_type("PRECEDES").
dependency_type("REQUIRES").
bounded(R) :- edge_attr(R, "repeatability", "once").
dep_edge(X,Y) :- edge(R,T,X,Y), dependency_type(T), not bounded(R).
dep_reach(X,Y) :- dep_edge(X,Y).
dep_reach(X,Z) :- dep_reach(X,Y), dep_edge(Y,Z).
violation("cyclic_dependency", X, Y) :- dep_reach(X,Y), dep_reach(Y,X).

% --- missing_drop_source: collect step's item has no GRANTS/DROPS_FROM source ---
collect_step(S) :- node(S, "QUEST_STEP"), attr(S, "kind", "collect").
collect_item(S, I) :- collect_step(S), attr(S, "item", I).
drop_source(Src, I) :- edge(_, "GRANTS", Src, I).
drop_source(Src, I) :- edge(_, "DROPS_FROM", Src, I).
has_source(S) :- collect_item(S, I), drop_source(_, I).
violation("missing_drop_source", S, I) :- collect_item(S, I), not has_source(S).

#show violation/3.
"""


def _asp_string(value: str) -> str:
    """Quote/escape an id or string attr value as a safe ASP string term.

    IR ids commonly contain `:` and other non-atom-safe characters, so every
    id/string is emitted as a quoted ASP string term (never a bare atom) —
    this is always safe regardless of content, and keeps encoding
    deterministic and injection-proof (backslash/quote escaped).
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _attr_term(value: object) -> str | None:
    """Render a scalar attr value as an ASP term, or None to skip emitting it.

    Only scalars are represented (str/bool/int as terms; float and
    containers are skipped — they play no role in the structural rules
    above and floats are not native ASP terms).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return _asp_string("true" if value else "false")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _asp_string(value)
    return None  # float / list / dict / other: not needed by structural rules


def ir_to_asp_facts(graph: IRGraph) -> str:
    """Encode an IRGraph as node, edge, entity-attr, and edge-attr facts.

    Pure and deterministic: entities/relations/attrs are emitted in sorted
    order so the same graph always yields byte-identical fact text.
    """
    lines: list[str] = []
    for e in sorted(graph.all_entities(), key=lambda e: e.id):
        lines.append(f"node({_asp_string(e.id)}, {_asp_string(e.type.value)}).")
        for key, value in sorted(e.attrs.items()):
            term = _attr_term(value)
            if term is not None:
                lines.append(f"attr({_asp_string(e.id)}, {_asp_string(key)}, {term}).")
    for r in sorted(graph.all_relations(), key=lambda r: r.id):
        lines.append(
            f"edge({_asp_string(r.id)}, {_asp_string(r.type.value)}, "
            f"{_asp_string(r.src_id)}, {_asp_string(r.dst_id)})."
        )
        for key, value in sorted((r.attrs or {}).items()):
            term = _attr_term(value)
            if term is not None:
                lines.append(
                    f"edge_attr({_asp_string(r.id)}, {_asp_string(key)}, {term})."
                )
    return "\n".join(lines) + ("\n" if lines else "")


def _connected_components(pairs: list[tuple[str, str]]) -> list[list[str]]:
    """Union-find grouping of a symmetric-pair relation into equivalence
    classes. This is generic connectivity plumbing over ASP-derived facts,
    NOT a reimplementation of GraphChecker's Tarjan SCC algorithm — the
    actual mutual-reachability *decision* was already made by Clingo.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    nodes: set[str] = set()
    for a, b in pairs:
        nodes.add(a)
        nodes.add(b)
        union(a, b)

    groups: dict[str, set[str]] = {}
    for x in nodes:
        groups.setdefault(find(x), set()).add(x)
    return [sorted(members) for members in groups.values()]


class ASPChecker:
    id = "asp"

    def __init__(
        self,
        grounding_budget_atoms: int = 200_000,
        wall_clock_budget_s: float = 10.0,
    ) -> None:
        self.grounding_budget_atoms = grounding_budget_atoms
        self.wall_clock_budget_s = wall_clock_budget_s

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        g = snapshot.to_graph()
        run_id = f"asp@{snapshot.snapshot_id[:23]}"
        findings: list[Finding] = []
        counter = [0]

        def emit(defect_class, entities, evidence, message, status="confirmed"):
            f = Finding(
                id=f"{run_id}#{counter[0]}", source="checker", producer_id=self.id,
                producer_run_id=run_id, oracle_type="deterministic",
                defect_class=defect_class, severity="critical",
                snapshot_id=snapshot.snapshot_id, entities=list(entities),
                evidence=evidence, status=status, message=message,
            )
            counter[0] += 1
            findings.append(f)

        def emit_unproven_all(reason: str, evidence: dict) -> None:
            for dc in _SHARED_DEFECT_CLASSES:
                emit(
                    dc, [], {**evidence, "reason": reason},
                    f"ASPChecker could not decide {dc}: {reason}",
                    status="unproven",
                )

        n_nodes = sum(1 for _ in g.all_entities())
        n_edges = sum(1 for _ in g.all_relations())
        n_scalar_attrs = sum(
            _attr_term(value) is not None
            for entity in g.all_entities()
            for value in entity.attrs.values()
        ) + sum(
            _attr_term(value) is not None
            for relation in g.all_relations()
            for value in (relation.attrs or {}).values()
        )
        # Coarse worst-case estimate of the transitive-closure grounding size
        # (dep_reach/2 can ground up to n^2 pairs); cheap and conservative,
        # checked BEFORE ever invoking Clingo.
        estimated_atoms = n_nodes + n_edges + n_scalar_attrs + n_nodes * n_nodes
        if estimated_atoms > self.grounding_budget_atoms:
            emit_unproven_all(
                "grounding_budget_exceeded",
                {"estimated_atoms": estimated_atoms, "budget": self.grounding_budget_atoms},
            )
            return findings

        facts = ir_to_asp_facts(g)
        ctl = clingo.Control()
        ctl.add("base", [], facts + _BUILTIN_RULES)
        ctl.ground([("base", [])])

        cyclic_pairs: list[tuple[str, str]] = []
        missing_pairs: list[tuple[str, str]] = []

        def on_model(model: clingo.Model) -> None:
            cyclic_pairs.clear()
            missing_pairs.clear()
            for sym in model.symbols(shown=True):
                if sym.name != "violation":
                    continue
                cls, a, b = sym.arguments
                cls_s, a_s, b_s = cls.string, a.string, b.string
                if cls_s == "cyclic_dependency":
                    cyclic_pairs.append((a_s, b_s))
                elif cls_s == "missing_drop_source":
                    missing_pairs.append((a_s, b_s))

        with ctl.solve(on_model=on_model, async_=True) as handle:
            done = handle.wait(self.wall_clock_budget_s)
            if not done:
                handle.cancel()
                emit_unproven_all(
                    "wall_clock_budget_exceeded",
                    {"budget_s": self.wall_clock_budget_s},
                )
                return findings

        for group in _connected_components(cyclic_pairs):
            emit(
                "cyclic_dependency", group,
                {"cycle_path": group, "violation_pairs": sorted(set(cyclic_pairs))},
                f"Quest step dependency cycle among: {', '.join(group)}",
            )

        for step_id, item_id in sorted(set(missing_pairs)):
            emit(
                "missing_drop_source", [step_id, item_id],
                {"step": step_id, "item": item_id},
                f"collect step {step_id!r} needs item {item_id!r} but has no "
                f"GRANTS/DROPS_FROM source",
            )

        return findings
