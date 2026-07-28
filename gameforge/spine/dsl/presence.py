"""Recognise a constraint that says "this attribute must be here, and be this".

Structural constraints do not evaluate their ``assert_`` text — ``compile._classify_structural``
keyword-matches it to pick a backend, and the text itself is discarded.  That is
deliberate for the built-in defect detectors, but it leaves a planner unable to
state the most ordinary house rule there is: *every NPC must have a faction*.

This module recognises exactly that shape and nothing wider.  A positive
conjunction of ``has`` / ``is_text`` / ``is_object`` over a selector is decidable
by inspection of the typed IR, with a clean witness pair, which is what the
constraint governance requires before a rule may be published.

Everything else returns ``None`` rather than raising, so an unrecognised assert
falls back to the existing structural routing.  A constraint this module declines
is never silently weakened — it simply is not a presence constraint.

Deliberately NOT supported, and this is a design decision rather than a subset:

* ``or`` / ``not`` — the differential quorum needs a *clean* witness, an entity
  the constraint provably accepts.  Constructing one for an arbitrary boolean
  formula is a satisfiability problem; for a conjunction it is "supply every
  atom".  A rule whose compliance cannot be exhibited cannot be validated.
* Enum membership and relation cardinality — separate expressiveness, separate
  decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gameforge.contracts.dsl import Constraint, Selector
from gameforge.contracts.ir import Entity, NodeType
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.dsl.ast import (
    AssertNode,
    BoolOp,
    Call,
    DslError,
    Field,
    parse_assert,
)

PRESENCE_POLICY_VERSION = "attribute-presence@1"

PresenceKind = Literal["present", "text", "object"]

_CALL_KINDS: dict[str, PresenceKind] = {
    "has": "present",
    "is_text": "text",
    "is_object": "object",
}


@dataclass(frozen=True, slots=True)
class PresenceAtom:
    """One required attribute path and how strong the requirement is."""

    path: str
    kind: PresenceKind


@dataclass(frozen=True, slots=True)
class PresenceSpec:
    """A constraint reduced to "these entities must carry these attributes"."""

    constraint_id: str
    selector: Selector
    atoms: tuple[PresenceAtom, ...]


def parse_presence_spec(constraint: Constraint) -> PresenceSpec | None:
    """Return the presence shape of ``constraint``, or ``None`` if it has none.

    Never raises: an assert this cannot read is an assert for some other backend.
    """

    if constraint.kind != "structural" or constraint.has_llm_predicate():
        return None
    selector = constraint.forall or constraint.scope
    if selector is None:
        return None
    try:
        node = parse_assert(constraint.assert_)
    except DslError:
        return None
    atoms = _atoms(node)
    if atoms is None or not atoms:
        return None
    return PresenceSpec(
        constraint_id=constraint.id,
        selector=selector,
        atoms=tuple(sorted(set(atoms), key=lambda atom: (atom.path, atom.kind))),
    )


def _atoms(node: AssertNode) -> list[PresenceAtom] | None:
    """Flatten a positive conjunction into atoms, or ``None`` if it is not one."""

    if isinstance(node, BoolOp):
        if node.op != "and":
            return None
        collected: list[PresenceAtom] = []
        for value in node.values:
            nested = _atoms(value)
            if nested is None:
                return None
            collected.extend(nested)
        return collected
    if isinstance(node, Call):
        kind = _CALL_KINDS.get(node.func)
        if kind is None or len(node.args) != 1:
            return None
        argument = node.args[0]
        # The argument is a bare or dotted attribute path, resolved against the
        # entity's own attrs — the same binding SMT uses, so a planner writes
        # `is_text(faction)`, never `is_text(n.faction)`.
        if not isinstance(argument, Field):
            return None
        return [PresenceAtom(path=argument.path, kind=kind)]
    return None


def presence_conflicts(spec: PresenceSpec) -> tuple[str, ...]:
    """Reasons this spec can never be exhibited as satisfied.

    A constraint no entity could comply with is not a rule, it is a trap: it
    would reject every candidate forever while looking like ordinary authority.
    The validator refuses to publish one rather than letting it through.
    """

    reasons: list[str] = []
    by_path: dict[str, set[PresenceKind]] = {}
    for atom in spec.atoms:
        by_path.setdefault(atom.path, set()).add(atom.kind)
    for path, kinds in sorted(by_path.items()):
        if "text" in kinds and "object" in kinds:
            reasons.append(f"attribute {path!r} is required to be both text and an object")
    for atom in spec.atoms:
        if atom.kind != "text":
            continue
        prefix = atom.path + "."
        for other in spec.atoms:
            if other.path.startswith(prefix):
                reasons.append(
                    f"attribute {atom.path!r} is required to be text but "
                    f"{other.path!r} requires it to nest"
                )
    for atom in spec.atoms:
        root = atom.path.split(".", 1)[0]
        if root in spec.selector.where:
            reasons.append(
                f"attribute {atom.path!r} starts at {root!r}, which the selector already "
                "fixes to one value, so the requirement decides nothing about the "
                "entities it selects"
            )
    return tuple(sorted(set(reasons)))


def presence_witnesses(spec: PresenceSpec) -> tuple[Snapshot, Snapshot] | None:
    """A pair of one-entity snapshots the rule must reject, then accept.

    Publication requires an engine to have GENUINELY decided a candidate, which
    means exhibiting both verdicts rather than asserting one.  The dirty witness
    carries only what the selector pins, so every atom fails; the clean witness
    adds exactly what the atoms ask for.

    ``None`` when no such pair exists — see ``presence_conflicts``.
    """

    if presence_conflicts(spec):
        return None
    try:
        node_type = NodeType[spec.selector.node_type]
    except KeyError:
        return None

    def entity(attrs: dict[str, object]) -> Snapshot:
        merged: dict[str, object] = dict(spec.selector.where)
        merged.update(attrs)
        return Snapshot.from_entities_relations(
            [Entity(id="witness:presence:subject", type=node_type, attrs=merged)], []
        )

    satisfied: dict[str, object] = {}
    for atom in sorted(spec.atoms, key=lambda item: item.path.count(".")):
        _assign(satisfied, atom.path.split("."), atom.kind)
    return entity({}), entity(satisfied)


def _assign(target: dict[str, object], segments: list[str], kind: PresenceKind) -> None:
    """Place a value of ``kind`` at ``segments``, creating the nesting it needs."""

    head, rest = segments[0], segments[1:]
    if rest:
        child = target.get(head)
        if not isinstance(child, dict):
            child = {}
            target[head] = child
        _assign(child, rest, kind)
        return
    if head in target and isinstance(target[head], dict) and kind != "text":
        return
    target[head] = {"present": "witness", "text": "witness", "object": {}}[kind]


__all__ = [
    "PRESENCE_POLICY_VERSION",
    "PresenceAtom",
    "PresenceKind",
    "PresenceSpec",
    "parse_presence_spec",
    "presence_witnesses",
    "presence_conflicts",
]
