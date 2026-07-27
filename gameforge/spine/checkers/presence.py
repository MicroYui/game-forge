"""Decide whether the entities a project selected actually carry the attributes
it declared they must.

Two implementations live here, and they are different on purpose.

``AttributePresenceChecker`` walks each required path segment by segment and stops
at the first segment that is not there. ``EagerPathIndexPresenceReference`` does
the opposite: it materialises every resolvable dotted path an entity has into one
index, without short-circuiting, and then decides each requirement by lookup.

Same verdict, opposite shape. A rule may only be published once two distinct
exact engines have positively decided it, and two copies of one algorithm cannot
supply that — they would agree even about a bug they share. These two disagree
exactly when path resolution itself has drifted, which is the thing worth
learning.

Neither reads a model, and neither reads the constraint's text: they read a
``PresenceSpec`` the DSL layer already reduced it to.
"""

from __future__ import annotations

from typing import Any

from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import Entity
from gameforge.spine.ir.store import NavProvider
from gameforge.spine.dsl.ast import DslError, select
from gameforge.spine.dsl.presence import (
    PresenceAtom,
    PresenceKind,
    parse_presence_spec,
    presence_conflicts,
)
from gameforge.spine.ir.snapshot import Snapshot

MISSING_REQUIRED_ATTRIBUTE = "missing_required_attribute"


def _observed_kind(value: Any) -> PresenceKind | None:
    """What kind the value actually is, or ``None`` if the path resolved to nothing.

    ``bool`` is checked before ``str``/``dict`` deliberately: it is neither, and a
    silent coercion here would let ``faction: true`` satisfy ``is_text(faction)``.
    """

    if isinstance(value, bool):
        return "present"
    if isinstance(value, str):
        return "text"
    if isinstance(value, dict):
        return "object"
    return "present"


def _satisfies(required: PresenceKind, observed: PresenceKind) -> bool:
    return observed == required if required != "present" else True


def _violation_message(entity_id: str, atom: PresenceAtom, observed: PresenceKind | None) -> str:
    if observed is None:
        return f"{entity_id} has no {atom.path}"
    expected = {"text": "text", "object": "an object"}[atom.kind]
    return f"{entity_id} has {atom.path}, but it is not {expected}"


class _PresenceBackend:
    """Shared Finding shape for both implementations.

    Only the resolution strategy differs; how a verdict is reported must not, or
    the differential would compare presentation instead of decisions.
    """

    id = "graph"

    def __init__(self, constraint: Constraint) -> None:
        self._constraint = constraint
        self._spec = parse_presence_spec(constraint)

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        del nav
        findings: list[Finding] = []
        run_id = f"{self.id}@{snapshot.snapshot_id[:23]}"
        counter = 0

        def emit(
            entity_ids: list[str],
            evidence: dict[str, Any],
            message: str,
            *,
            status: str = "confirmed",
        ) -> None:
            nonlocal counter
            findings.append(
                Finding(
                    id=f"{run_id}#{counter}",
                    source="checker",
                    producer_id=self.id,
                    producer_run_id=run_id,
                    oracle_type="deterministic",
                    defect_class=MISSING_REQUIRED_ATTRIBUTE,
                    severity=self._constraint.severity,
                    snapshot_id=snapshot.snapshot_id,
                    entities=entity_ids,
                    constraint_id=self._constraint.id,
                    evidence=evidence,
                    status=status,
                    message=message,
                )
            )
            counter += 1

        spec = self._spec
        if spec is None:
            emit(
                [],
                {"reason": "constraint is not an attribute-presence predicate"},
                f"{self._constraint.id!r} is not an attribute-presence constraint",
                status="unproven",
            )
            return findings

        conflicts = presence_conflicts(spec)
        if conflicts:
            # A rule nothing could satisfy would reject every candidate forever
            # while looking like ordinary authority. Say so instead of deciding.
            emit(
                [],
                {"reason": "; ".join(conflicts)},
                f"{spec.constraint_id!r} cannot be satisfied: {conflicts[0]}",
                status="unproven",
            )
            return findings

        try:
            entities = select(snapshot.to_graph(), spec.selector)
        except DslError as exc:
            emit(
                [],
                {"reason": str(exc)},
                f"could not select entities for {spec.constraint_id!r}: {exc}",
                status="unproven",
            )
            return findings

        for entity in sorted(entities, key=lambda item: item.id):
            for atom in spec.atoms:
                observed = self._observe(entity, atom)
                if observed is not None and _satisfies(atom.kind, observed):
                    continue
                emit(
                    [entity.id],
                    {
                        "entity": entity.id,
                        "path": atom.path,
                        "required": atom.kind,
                        "observed": observed or "absent",
                    },
                    _violation_message(entity.id, atom, observed),
                )
        return findings

    def _observe(self, entity: Entity, atom: PresenceAtom) -> PresenceKind | None:
        raise NotImplementedError


class AttributePresenceChecker(_PresenceBackend):
    """Resolve each path segment by segment, stopping at the first gap."""

    def _observe(self, entity: Entity, atom: PresenceAtom) -> PresenceKind | None:
        current: Any = entity.attrs
        for segment in atom.path.split("."):
            if not isinstance(current, dict) or segment not in current:
                return None
            current = current[segment]
        return _observed_kind(current)


class EagerPathIndexPresenceReference(_PresenceBackend):
    """Materialise every path the entity has, then decide by lookup.

    The independent peer for the differential quorum: it never short-circuits and
    never walks a requirement, so it cannot inherit a traversal bug from the
    production checker.
    """

    id = "graph"

    def _observe(self, entity: Entity, atom: PresenceAtom) -> PresenceKind | None:
        return _path_index(entity.attrs).get(atom.path)


def _path_index(attrs: dict[str, Any]) -> dict[str, PresenceKind]:
    """Every dotted path this entity carries, mapped to what kind it holds."""

    index: dict[str, PresenceKind] = {}

    def walk(prefix: str, value: Any) -> None:
        if prefix:
            index[prefix] = _observed_kind(value)
        if isinstance(value, dict):
            for key in sorted(value):
                child = f"{prefix}.{key}" if prefix else str(key)
                walk(child, value[key])

    walk("", attrs)
    return index


__all__ = [
    "MISSING_REQUIRED_ATTRIBUTE",
    "AttributePresenceChecker",
    "EagerPathIndexPresenceReference",
]
