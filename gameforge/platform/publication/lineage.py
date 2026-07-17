"""Typed lineage-role projection (foundations v0.3 §5.1) for publication.

Turns a domain Artifact's bare ``lineage[]`` id list into typed parent roles by
reverse-matching each parent against an ``ArtifactLineagePolicyV1``'s
``parent_rules`` inside the terminal transaction view.  Every parent must match
exactly one role (existence + kind + schema + direct-parent), and every role's
matched cardinality must satisfy ``[min_count, max_count]``.  Nothing self-reports
its role; unmatched, ambiguous, duplicate or dangling parents are fail-closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import ArtifactLineagePolicyV1, ArtifactParentRuleV1
from gameforge.contracts.lineage import VersionTuple
from gameforge.platform.publication.validator import resolve_json_pointer


@dataclass(frozen=True, slots=True)
class ParentInfo:
    """The exact transaction-view facts a candidate parent contributes."""

    artifact_id: str
    kind: str
    payload_schema_id: str
    version_tuple: VersionTuple
    payload_hash: str | None = None


@dataclass(frozen=True, slots=True)
class LineageParentSources:
    """Candidate parents grouped by their trusted provenance for one child."""

    run_inputs: Mapping[str, ParentInfo]
    run_intermediates: Mapping[str, ParentInfo]
    prepared_siblings: Mapping[str, Mapping[str, ParentInfo]]
    child_payload_references: Mapping[str, ParentInfo] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TypedLineage:
    """The typed-role projection result for one child Artifact."""

    parents_by_role: Mapping[str, tuple[ParentInfo, ...]]


class _ResolvedChildPayloadReferences(dict[str, ParentInfo]):
    """Flat compatibility map plus the exact payload-reference role per id."""

    def __init__(
        self,
        values: Mapping[str, ParentInfo],
        roles_by_id: Mapping[str, str],
    ) -> None:
        super().__init__(values)
        self.roles_by_id = dict(roles_by_id)


def resolve_child_payload_references(
    *,
    policy: ArtifactLineagePolicyV1,
    child_payload: Mapping[str, object],
    available_parents: Mapping[str, ParentInfo],
) -> tuple[Mapping[str, ParentInfo], tuple[str, ...]]:
    """Resolve policy-declared child references through already-authorized parents.

    A child payload reference is a selector, not a new source of authority.  Its
    target must already be one of the Run inputs, committed intermediates, or
    earlier same-publication siblings supplied by the caller.  Scalar IDs and
    bounded arrays of IDs are supported; null is allowed only for an optional
    role.  Missing pointers, duplicates, dangling IDs and wrong kind/schema fail
    before Artifact publication.
    """

    resolved: dict[str, ParentInfo] = {}
    roles_by_id: dict[str, str] = {}
    referenced_ids: list[str] = []
    for rule in policy.parent_rules:
        if rule.source != "child_payload_reference":
            continue
        pointer = rule.child_payload_pointer
        if pointer is None:  # pragma: no cover - contract validator guarantees it
            raise IntegrityViolation("child payload reference lacks its pointer")
        value = resolve_json_pointer(child_payload, pointer)
        if value is None:
            if rule.min_count != 0:
                raise IntegrityViolation(
                    "required child payload reference is null",
                    parent_role=rule.parent_role,
                )
            ids: tuple[str, ...] = ()
        elif isinstance(value, str):
            ids = (value,)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if any(not isinstance(item, str) or not item for item in value):
                raise IntegrityViolation(
                    "child payload reference array contains a non-ID value",
                    parent_role=rule.parent_role,
                )
            ids = tuple(value)
        else:
            raise IntegrityViolation(
                "child payload reference must be an Artifact ID or ID array",
                parent_role=rule.parent_role,
            )
        if len(ids) != len(set(ids)):
            raise IntegrityViolation(
                "child payload reference contains duplicate Artifact IDs",
                parent_role=rule.parent_role,
            )
        if len(ids) < rule.min_count or (rule.max_count is not None and len(ids) > rule.max_count):
            raise IntegrityViolation(
                "child payload reference count is outside its role cardinality",
                parent_role=rule.parent_role,
                actual=len(ids),
            )
        for artifact_id in ids:
            info = available_parents.get(artifact_id)
            if info is None:
                raise IntegrityViolation(
                    "child payload reference is not an authorized Run parent",
                    parent_role=rule.parent_role,
                    artifact_id=artifact_id,
                )
            if info.kind not in rule.artifact_kinds or (
                info.payload_schema_id not in rule.payload_schema_ids
            ):
                raise IntegrityViolation(
                    "child payload reference kind/schema differs from its role policy",
                    parent_role=rule.parent_role,
                    artifact_id=artifact_id,
                )
            previous = resolved.get(artifact_id)
            if previous is not None and previous != info:
                raise IntegrityViolation(
                    "child payload reference resolves inconsistently",
                    artifact_id=artifact_id,
                )
            resolved[artifact_id] = info
            roles_by_id[artifact_id] = rule.parent_role
            referenced_ids.append(artifact_id)
    if len(referenced_ids) != len(set(referenced_ids)):
        raise IntegrityViolation(
            "one child Artifact ID is claimed by multiple payload-reference roles"
        )
    return _ResolvedChildPayloadReferences(resolved, roles_by_id), tuple(referenced_ids)


def _candidate_for_rule(
    parent_id: str,
    *,
    rule: ArtifactParentRuleV1,
    sources: LineageParentSources,
) -> ParentInfo | None:
    if rule.source == "run_input":
        info = sources.run_inputs.get(parent_id)
    elif rule.source == "run_intermediate":
        info = sources.run_intermediates.get(parent_id)
    elif rule.source == "prepared_rule":
        pool = sources.prepared_siblings.get(rule.source_rule_id or "", {})
        info = pool.get(parent_id)
    elif rule.source == "child_payload_reference":
        roles_by_id = getattr(sources.child_payload_references, "roles_by_id", None)
        if roles_by_id is not None and roles_by_id.get(parent_id) != rule.parent_role:
            return None
        info = sources.child_payload_references.get(parent_id)
    else:  # pragma: no cover - exhaustive over ArtifactParentRuleV1.source
        return None
    if info is None:
        return None
    if info.kind not in rule.artifact_kinds:
        return None
    if info.payload_schema_id not in rule.payload_schema_ids:
        return None
    return info


def project_typed_lineage(
    *,
    policy: ArtifactLineagePolicyV1,
    child_kind: str,
    child_payload_schema_id: str,
    child_lineage: tuple[str, ...],
    sources: LineageParentSources,
) -> TypedLineage:
    """Reverse-match a child's bare lineage into typed parent roles."""

    if policy.child_kind != child_kind:
        raise IntegrityViolation(
            "lineage policy child kind differs from the Artifact kind",
            expected=policy.child_kind,
            actual=child_kind,
        )
    if child_payload_schema_id not in policy.child_payload_schema_ids:
        raise IntegrityViolation(
            "lineage policy does not allow the child payload schema",
            schema=child_payload_schema_id,
        )

    matched: dict[str, list[ParentInfo]] = {rule.parent_role: [] for rule in policy.parent_rules}
    for parent_id in child_lineage:
        hits: list[tuple[ArtifactParentRuleV1, ParentInfo]] = []
        for rule in policy.parent_rules:
            info = _candidate_for_rule(parent_id, rule=rule, sources=sources)
            if info is not None:
                hits.append((rule, info))
        if not hits:
            raise IntegrityViolation(
                "child lineage parent matched no typed role",
                child_kind=child_kind,
                parent_id=parent_id,
            )
        if len(hits) > 1:
            raise IntegrityViolation(
                "child lineage parent matched more than one typed role",
                child_kind=child_kind,
                parent_id=parent_id,
                roles=sorted(rule.parent_role for rule, _ in hits),
            )
        rule, info = hits[0]
        matched[rule.parent_role].append(info)

    for rule in policy.parent_rules:
        found = matched[rule.parent_role]
        ids = [info.artifact_id for info in found]
        if len(ids) != len(set(ids)):
            raise IntegrityViolation(
                "typed lineage role bound a duplicate parent",
                parent_role=rule.parent_role,
            )
        if len(found) < rule.min_count:
            raise IntegrityViolation(
                "typed lineage role is below its minimum cardinality",
                parent_role=rule.parent_role,
                min_count=rule.min_count,
                actual=len(found),
            )
        if rule.max_count is not None and len(found) > rule.max_count:
            raise IntegrityViolation(
                "typed lineage role exceeds its maximum cardinality",
                parent_role=rule.parent_role,
                max_count=rule.max_count,
                actual=len(found),
            )

    return TypedLineage(parents_by_role={role: tuple(items) for role, items in matched.items()})


__all__ = [
    "LineageParentSources",
    "ParentInfo",
    "TypedLineage",
    "project_typed_lineage",
    "resolve_child_payload_references",
]
