"""Decide whether the entities a project selected actually carry the attributes
it declared they must.

Two implementations live here, and they are different on purpose.

``AttributePresenceChecker`` — the production route — walks each required path
segment by segment and stops at the first segment that is not there.
``EagerPathIndexPresenceReference`` does the opposite: it materialises every
addressable dotted path an entity has into one index, without short-circuiting,
and then decides each requirement by lookup.

Same verdict, opposite shape. A rule may only be published once two distinct
exact engines have positively decided it, and two copies of one algorithm cannot
supply that — they would agree even about a bug they share. These two disagree
exactly when path resolution itself has drifted, which is the thing worth
learning. (``presence_asp`` supplies a third, in ASP, for the second engine.)

Everything except resolution is deliberately shared, in ``_PresenceBackend``: the
selection guards, the Finding shape, the ordering, and what counts as a "kind".
Otherwise the differential would compare presentation rather than decisions.

None of them reads a model, and none reads the constraint's text: they read a
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
    PresenceSpec,
    parse_presence_spec,
    presence_conflicts,
)
from gameforge.spine.ir.snapshot import Snapshot

MISSING_REQUIRED_ATTRIBUTE = "missing_required_attribute"


class PresenceUndecidable(Exception):
    """A backend ran out of budget before deciding — never a silent pass."""


def observed_kind(value: Any) -> PresenceKind | None:
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
    """Shared Finding shape for every implementation.

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
            # A rule whose verdict is the same for every entity it selects — always
            # violated, or never — is not authority, it just looks like it. Say so
            # instead of deciding.
            emit(
                [],
                {"reason": "; ".join(conflicts)},
                f"{spec.constraint_id!r} decides nothing: {conflicts[0]}",
                status="unproven",
            )
            return findings

        try:
            selected, observed = self.observe_all(snapshot, spec)
        except (DslError, PresenceUndecidable) as exc:
            emit(
                [],
                {"reason": str(exc)},
                f"could not decide {spec.constraint_id!r}: {exc}",
                status="unproven",
            )
            return findings

        for entity_id in selected:
            for atom in spec.atoms:
                kind = observed.get((entity_id, atom.path))
                if kind is not None and _satisfies(atom.kind, kind):
                    continue
                emit(
                    [entity_id],
                    {
                        "entity": entity_id,
                        "path": atom.path,
                        "required": atom.kind,
                        "observed": kind or "absent",
                    },
                    _violation_message(entity_id, atom, kind),
                )
        return findings

    def observe_all(
        self, snapshot: Snapshot, spec: PresenceSpec
    ) -> tuple[tuple[str, ...], dict[tuple[str, str], PresenceKind]]:
        """Which entities the rule governs, and what each required path holds.

        The whole differential lives in this one method: everything around it —
        selection guards, Finding shape, ordering — is deliberately shared so the
        cross-check compares decisions rather than presentation. A missing key
        means the path resolved to nothing.
        """

        raise NotImplementedError


class _EntityWisePresenceBackend(_PresenceBackend):
    """Select in Python, then ask each entity about each atom independently."""

    def observe_all(
        self, snapshot: Snapshot, spec: PresenceSpec
    ) -> tuple[tuple[str, ...], dict[tuple[str, str], PresenceKind]]:
        entities = sorted(select(snapshot.to_graph(), spec.selector), key=lambda item: item.id)
        observed: dict[tuple[str, str], PresenceKind] = {}
        for entity in entities:
            for atom in spec.atoms:
                kind = self._observe(entity, atom)
                if kind is not None:
                    observed[(entity.id, atom.path)] = kind
        return tuple(entity.id for entity in entities), observed

    def _observe(self, entity: Entity, atom: PresenceAtom) -> PresenceKind | None:
        raise NotImplementedError


class AttributePresenceChecker(_EntityWisePresenceBackend):
    """Resolve each path segment by segment, stopping at the first gap."""

    def _observe(self, entity: Entity, atom: PresenceAtom) -> PresenceKind | None:
        current: Any = entity.attrs
        for segment in atom.path.split("."):
            if not isinstance(current, dict) or segment not in current:
                return None
            current = current[segment]
        return observed_kind(current)


class EagerPathIndexPresenceReference(_EntityWisePresenceBackend):
    """Materialise every path the entity has, then decide by lookup.

    An independent peer for the differential quorum: it never short-circuits and
    never walks a requirement, so it cannot inherit a traversal bug from the
    production checker.
    """

    id = "graph"

    def _observe(self, entity: Entity, atom: PresenceAtom) -> PresenceKind | None:
        return _path_index(entity.attrs).get(atom.path)


def _path_index(attrs: dict[str, Any]) -> dict[str, PresenceKind]:
    """Every *addressable* dotted path this entity carries, mapped to its kind.

    A key that itself contains a dot is skipped rather than indexed. Requirements
    are split on dots before they ever reach a backend, so ``{"profile.home": …}``
    is one key that no requirement can name — joining it into the index anyway
    would let it forge the path that ``{"profile": {"home": …}}`` legitimately owns.
    """

    index: dict[str, PresenceKind] = {}

    def walk(prefix: str, value: Any) -> None:
        if prefix:
            index[prefix] = observed_kind(value)
        if isinstance(value, dict):
            for key in sorted(value, key=str):
                if "." in str(key):
                    continue
                child = f"{prefix}.{key}" if prefix else str(key)
                walk(child, value[key])

    walk("", attrs)
    return index


__all__ = [
    "MISSING_REQUIRED_ATTRIBUTE",
    "AttributePresenceChecker",
    "EagerPathIndexPresenceReference",
    "PresenceUndecidable",
    "observed_kind",
]
