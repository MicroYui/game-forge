"""Typed lineage-role projection (foundations v0.3 §5.1) for publication.

Turns a domain Artifact's bare ``lineage[]`` id list into typed parent roles by
reverse-matching each parent against an ``ArtifactLineagePolicyV1``'s
``parent_rules`` inside the terminal transaction view.  Every parent must match
exactly one role (existence + kind + schema + direct-parent), and every role's
matched cardinality must satisfy ``[min_count, max_count]``.  Nothing self-reports
its role; unmatched, ambiguous, duplicate or dangling parents are fail-closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import ArtifactLineagePolicyV1, ArtifactParentRuleV1
from gameforge.contracts.lineage import VersionTuple


@dataclass(frozen=True, slots=True)
class ParentInfo:
    """The exact transaction-view facts a candidate parent contributes."""

    artifact_id: str
    kind: str
    payload_schema_id: str
    version_tuple: VersionTuple


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
]
