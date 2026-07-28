"""Decide the same attribute-presence question through real Clingo.

A rule may only be published once two distinct exact engines have positively
decided it. Two Python implementations cannot supply that on their own — they
run the same interpreter over the same dicts, so a shared misconception about
what a path *is* would pass both. This backend never sees a dict: the attrs tree
is flattened into ``child(Parent, Key, Child)`` edges that carry no dotted paths
at all, the requirement is emitted as a sequence of segments, and Clingo derives
the resolution itself by fixpoint. Selection is likewise re-derived from
``node``/``sel_type``/``sel_where`` facts rather than reusing ``select``.

What it deliberately does NOT re-derive: what a "kind" is (``observed_kind`` is
the shell's definition, not an algorithm) and how a violation is reported. Those
are shared on purpose, so the cross-check compares resolutions rather than
presentation.
"""

from __future__ import annotations

from typing import Any

import clingo

from gameforge.contracts.dsl import Constraint

from gameforge.spine.checkers.presence import (
    PresenceUndecidable,
    _PresenceBackend,
    observed_kind,
)
from gameforge.spine.dsl.presence import PresenceKind, PresenceSpec
from gameforge.spine.ir.snapshot import Snapshot

_RULES = """
% An empty graph or an attribute-free entity leaves these with no facts at all;
% declare them so Clingo stays silent instead of logging about every one.
#defined node/2.
#defined root/2.
#defined kind/2.
#defined child/3.
#defined scalar/2.
#defined sel_where/2.

% --- selection, re-derived: right type, and every where-pair met on a top-level scalar
top_scalar(E, K, V) :- root(E, R), child(R, K, N), scalar(N, V).
where_unmet(E) :- node(E, _), sel_where(K, V), not top_scalar(E, K, V).
selected(E) :- node(E, T), sel_type(T), not where_unmet(E).

% --- resolution, re-derived: walk one segment per step over the flattened tree
at(E, A, 0, R) :- selected(E), atom(A), root(E, R).
at(E, A, I+1, C) :- at(E, A, I, N), seg(A, I, K), child(N, K, C).
observed(E, A, K) :- at(E, A, L, N), atom_len(A, L), kind(N, K).

#show selected/1.
#show observed/3.
"""


class ClingoPresenceReference(_PresenceBackend):
    """The independent peer that decides presence in ASP instead of Python."""

    id = "graph"

    def __init__(
        self,
        constraint: Constraint,
        grounding_budget_atoms: int = 200_000,
        wall_clock_budget_s: float = 10.0,
    ) -> None:
        super().__init__(constraint)
        self.grounding_budget_atoms = grounding_budget_atoms
        self.wall_clock_budget_s = wall_clock_budget_s

    def observe_all(
        self, snapshot: Snapshot, spec: PresenceSpec
    ) -> tuple[tuple[str, ...], dict[tuple[str, str], PresenceKind]]:
        facts, node_count = _encode(snapshot, spec)
        # ``at/4`` grounds one atom per (entity, requirement, depth, tree node);
        # bound it before Clingo is ever invoked, and degrade to unproven — never
        # to a silent pass — when the estimate exceeds the budget.
        depth = max((atom.path.count(".") + 1 for atom in spec.atoms), default=0)
        estimated = node_count * (1 + len(spec.atoms) * (depth + 1))
        if estimated > self.grounding_budget_atoms:
            raise PresenceUndecidable(
                f"grounding_budget_exceeded: {estimated} > {self.grounding_budget_atoms}"
            )

        control = clingo.Control()
        control.add("base", [], facts + _RULES)
        control.ground([("base", [])])

        selected: list[str] = []
        observed: dict[tuple[str, str], PresenceKind] = {}
        atom_paths = {f"a{index}": atom.path for index, atom in enumerate(spec.atoms)}

        def on_model(model: clingo.Model) -> None:
            selected.clear()
            observed.clear()
            for symbol in model.symbols(shown=True):
                if symbol.name == "selected":
                    selected.append(symbol.arguments[0].string)
                elif symbol.name == "observed":
                    entity, atom, kind = symbol.arguments
                    observed[(entity.string, atom_paths[atom.string])] = kind.string

        with control.solve(on_model=on_model, async_=True) as handle:
            if not handle.wait(self.wall_clock_budget_s):
                handle.cancel()
                raise PresenceUndecidable(
                    f"wall_clock_budget_exceeded: {self.wall_clock_budget_s}s"
                )
        return tuple(sorted(selected)), observed


def _term(value: str) -> str:
    """Quote any id or key as an ASP string term — always safe, never a bare atom."""

    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _value_term(value: Any) -> str | None:
    """Render a scalar so distinct Python values stay distinct ASP terms.

    ``True`` and ``"true"`` must not collapse onto one term: ``select`` compares
    them with ``==`` and would keep them apart, so collapsing here would make the
    two engines disagree about selection for a reason that has nothing to do with
    presence. ``None`` means the value has no faithful term at all.
    """

    if isinstance(value, bool):
        return _term(f"b:{'true' if value else 'false'}")
    if isinstance(value, int):
        return _term(f"i:{value}")
    if isinstance(value, str):
        return _term(f"s:{value}")
    return None


def _encode(snapshot: Snapshot, spec: PresenceSpec) -> tuple[str, int]:
    """Flatten the graph and the requirement into facts. Deterministic and sorted."""

    lines: list[str] = []
    node_count = 0
    for entity in sorted(snapshot.to_graph().all_entities(), key=lambda item: item.id):
        lines.append(f"node({_term(entity.id)}, {_term(entity.type.value)}).")
        root = f"{entity.id}#"
        lines.append(f"root({_term(entity.id)}, {_term(root)}).")
        lines.append(f'kind({_term(root)}, "object").')
        node_count += 1 + _emit_subtree(lines, root, entity.attrs)

    lines.append(f"sel_type({_term(spec.selector.node_type)}).")
    for key, value in sorted(spec.selector.where.items()):
        term = _value_term(value)
        if term is None:
            # A where-value ASP cannot represent would silently widen selection.
            raise PresenceUndecidable(f"selector_value_unsupported: {key}")
        lines.append(f"sel_where({_term(key)}, {term}).")

    for index, atom in enumerate(spec.atoms):
        name = _term(f"a{index}")
        segments = atom.path.split(".")
        lines.append(f"atom({name}).")
        lines.append(f"atom_len({name}, {len(segments)}).")
        for position, segment in enumerate(segments):
            lines.append(f"seg({name}, {position}, {_term(segment)}).")
    return "\n".join(lines) + "\n", node_count


def _emit_subtree(lines: list[str], parent: str, value: Any) -> int:
    """Emit one ``child``/``kind`` fact per attrs-tree edge. Returns the node count."""

    if not isinstance(value, dict):
        return 0
    count = 0
    for key in sorted(value, key=str):
        child_value = value[key]
        # Length-prefixed so a key containing the separator cannot forge another
        # node's id (``{"a/b": …}`` must not collide with ``{"a": {"b": …}}``).
        child = f"{parent}{len(str(key))}:{key}/"
        lines.append(f"child({_term(parent)}, {_term(str(key))}, {_term(child)}).")
        lines.append(f"kind({_term(child)}, {_term(observed_kind(child_value))}).")
        term = _value_term(child_value)
        if term is not None:
            lines.append(f"scalar({_term(child)}, {term}).")
        count += 1 + _emit_subtree(lines, child, child_value)
    return count


__all__ = ["ClingoPresenceReference"]
