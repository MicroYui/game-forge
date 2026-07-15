"""Unique-rule allocation, count/identity bindings, and disposition checks.

Implements the publication contract's allocation invariant (M4 design §7 /
lines 1120-1124): every PreparedArtifact matches **exactly one**
``OutcomeArtifactRuleV1`` across the whole publication plan (no Artifact consumed
at both attempt and run scope; missing / extra / overlapping is fail-closed), and
each rule's cardinality is the exact count/identity set derived from the frozen
Run payload, resolved-policy snapshot, prepared primary payload, or complete
requirement dispositions — never a process default.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    ArtifactIdentityBindingV1,
    JsonCollectionCountBindingV1,
    OutcomeArtifactRuleV1,
    RequirementDispositionV1,
    ResolvedArtifactRequirementV1,
    ResolvedPolicyCountBindingV1,
    ResolvedPolicySnapshotV1,
    ResolvedPolicySubsetCountBindingV1,
)
from gameforge.contracts.lineage import ObjectLocation, ObjectRef, VersionTuple


@dataclass(frozen=True, slots=True)
class PreparedArtifactView:
    """The exact facts one PreparedArtifact contributes, blob already re-read."""

    index: int
    kind: str
    payload_schema_id: str
    version_tuple: VersionTuple
    lineage: tuple[str, ...]
    payload_hash: str
    object_ref: ObjectRef
    location: ObjectLocation
    meta: Mapping[str, object]
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PlanRule:
    """A single OutcomeArtifactRule situated in its (scope, policy) context."""

    scope: str
    policy_id: str
    rule: OutcomeArtifactRuleV1


@dataclass(frozen=True, slots=True)
class RuleAllocation:
    plan_rule: PlanRule
    artifact_indexes: tuple[int, ...]


def resolve_json_pointer(value: object, pointer: str) -> object:
    """RFC 6901 evaluation; fail-closed on any unresolved token."""

    if pointer == "":
        return value
    current = value
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise IntegrityViolation("count-binding pointer does not resolve", pointer=pointer)
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            if not token.isdecimal() or str(int(token)) != token or int(token) >= len(current):
                raise IntegrityViolation("count-binding array index is invalid", pointer=pointer)
            current = current[int(token)]
        else:
            raise IntegrityViolation("count-binding pointer does not resolve", pointer=pointer)
    return current


def _bounded_collection(value: object, *, pointer: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise IntegrityViolation("count-binding pointer must resolve to an array", pointer=pointer)
    return tuple(value)


def allocate_artifacts(
    *,
    plan_rules: Sequence[PlanRule],
    artifacts: Sequence[PreparedArtifactView],
) -> tuple[RuleAllocation, ...]:
    """Match every artifact to exactly one plan rule; reject gap/overlap/dupe."""

    for rule in plan_rules:
        # Wildcards are forbidden: payload_schema_ids must be a non-empty exact allowlist.
        if not rule.rule.payload_schema_ids or any(
            "*" in schema for schema in rule.rule.payload_schema_ids
        ):
            raise IntegrityViolation(
                "outcome rule payload schema allowlist must be exact and non-empty",
                rule_id=rule.rule.rule_id,
            )

    by_rule: dict[int, list[int]] = {index: [] for index in range(len(plan_rules))}
    for view in artifacts:
        hits = [
            position
            for position, plan_rule in enumerate(plan_rules)
            if plan_rule.rule.artifact_kind == view.kind
            and view.payload_schema_id in plan_rule.rule.payload_schema_ids
        ]
        if not hits:
            raise IntegrityViolation(
                "prepared artifact matched no outcome rule in the publication plan",
                artifact_index=view.index,
                kind=view.kind,
                payload_schema_id=view.payload_schema_id,
            )
        if len(hits) > 1:
            raise IntegrityViolation(
                "prepared artifact matched more than one outcome rule",
                artifact_index=view.index,
                rules=sorted(plan_rules[position].rule.rule_id for position in hits),
            )
        by_rule[hits[0]].append(view.index)

    return tuple(
        RuleAllocation(plan_rule=plan_rule, artifact_indexes=tuple(by_rule[position]))
        for position, plan_rule in enumerate(plan_rules)
    )


def validate_rule_cardinality(
    *,
    allocation: RuleAllocation,
    artifacts_by_index: Mapping[int, PreparedArtifactView],
    run_payload: Mapping[str, object],
    primary_payload: Mapping[str, object] | None,
    snapshots_by_id: Mapping[str, ResolvedPolicySnapshotV1],
    dispositions: Sequence[RequirementDispositionV1],
) -> None:
    """Enforce the exact count/identity set for one allocated rule."""

    rule = allocation.plan_rule.rule
    views = tuple(artifacts_by_index[index] for index in allocation.artifact_indexes)
    count = len(views)

    if count < rule.min_count or (rule.max_count is not None and count > rule.max_count):
        raise IntegrityViolation(
            "allocated artifact count is outside the rule range",
            rule_id=rule.rule_id,
            count=count,
            min_count=rule.min_count,
            max_count=rule.max_count,
        )

    binding = rule.count_binding
    if binding is None:
        return

    if isinstance(binding, JsonCollectionCountBindingV1):
        _validate_json_binding(
            binding=binding,
            rule=rule,
            views=views,
            run_payload=run_payload,
            primary_payload=primary_payload,
        )
    elif isinstance(binding, ResolvedPolicyCountBindingV1):
        requirements = _requirements_for(
            binding.resolved_policy_id, binding.outcome_rule_id, snapshots_by_id
        )
        if len(requirements) != count:
            raise IntegrityViolation(
                "resolved-policy count differs from allocated artifacts",
                rule_id=rule.rule_id,
                expected=len(requirements),
                actual=count,
            )
        _validate_requirement_identity(binding.identity_binding, requirements, views, rule)
    elif isinstance(binding, ResolvedPolicySubsetCountBindingV1):
        _validate_subset_binding(
            binding=binding,
            rule=rule,
            views=views,
            snapshots_by_id=snapshots_by_id,
            dispositions=dispositions,
        )
    else:
        raise IntegrityViolation(
            "outcome artifact rule uses a runtime-only count binding",
            rule_id=rule.rule_id,
            binding_source=binding.source,
        )


def _validate_json_binding(
    *,
    binding: JsonCollectionCountBindingV1,
    rule: OutcomeArtifactRuleV1,
    views: Sequence[PreparedArtifactView],
    run_payload: Mapping[str, object],
    primary_payload: Mapping[str, object] | None,
) -> None:
    if binding.source == "run_payload":
        root: object = run_payload
    else:
        if primary_payload is None:
            raise IntegrityViolation(
                "prepared-primary count binding requires the parsed primary payload",
                rule_id=rule.rule_id,
            )
        root = primary_payload
    collection = _bounded_collection(
        resolve_json_pointer(root, binding.collection_pointer),
        pointer=binding.collection_pointer,
    )
    if len(collection) != len(views):
        raise IntegrityViolation(
            "json collection count differs from allocated artifacts",
            rule_id=rule.rule_id,
            expected=len(collection),
            actual=len(views),
        )
    if binding.identity_binding is not None:
        _validate_collection_identity(binding.identity_binding, collection, views, rule)


def _requirements_for(
    resolved_policy_id: str,
    outcome_rule_id: str,
    snapshots_by_id: Mapping[str, ResolvedPolicySnapshotV1],
) -> tuple[ResolvedArtifactRequirementV1, ...]:
    snapshot = snapshots_by_id.get(resolved_policy_id)
    if snapshot is None:
        raise IntegrityViolation(
            "resolved-policy snapshot is not frozen in the Run payload",
            resolved_policy_id=resolved_policy_id,
        )
    return tuple(
        requirement
        for requirement in snapshot.requirements
        if requirement.outcome_rule_id == outcome_rule_id
    )


def _validate_requirement_identity(
    identity: ArtifactIdentityBindingV1,
    requirements: Sequence[ResolvedArtifactRequirementV1],
    views: Sequence[PreparedArtifactView],
    rule: OutcomeArtifactRuleV1,
) -> None:
    # ResolvedPolicy identity is fixed to requirement /requirement_id ↔ payload /requirement_id.
    by_requirement = {requirement.requirement_id: requirement for requirement in requirements}
    seen: set[str] = set()
    for view in views:
        value = _artifact_identity_value(identity, view)
        requirement = by_requirement.get(value)
        if requirement is None or value in seen:
            raise IntegrityViolation(
                "artifact does not map one-to-one onto a frozen requirement",
                rule_id=rule.rule_id,
                artifact_index=view.index,
            )
        seen.add(value)
        if (
            requirement.artifact_kind != view.kind
            or requirement.payload_schema_id != view.payload_schema_id
        ):
            raise IntegrityViolation(
                "artifact kind/schema differs from its resolved requirement",
                rule_id=rule.rule_id,
                requirement_id=requirement.requirement_id,
            )
    if len(seen) != len(requirements):
        raise IntegrityViolation(
            "resolved requirements are not fully covered by artifacts",
            rule_id=rule.rule_id,
        )


def _validate_subset_binding(
    *,
    binding: ResolvedPolicySubsetCountBindingV1,
    rule: OutcomeArtifactRuleV1,
    views: Sequence[PreparedArtifactView],
    snapshots_by_id: Mapping[str, ResolvedPolicySnapshotV1],
    dispositions: Sequence[RequirementDispositionV1],
) -> None:
    requirements = _requirements_for(
        binding.resolved_policy_id, binding.outcome_rule_id, snapshots_by_id
    )
    frozen_ids = {requirement.requirement_id for requirement in requirements}
    rows = [
        disposition
        for disposition in dispositions
        if disposition.resolved_policy_id == binding.resolved_policy_id
        and disposition.outcome_rule_id == binding.outcome_rule_id
    ]
    row_ids = [row.requirement_id for row in rows]
    if len(row_ids) != len(set(row_ids)) or set(row_ids) != frozen_ids:
        raise IntegrityViolation(
            "subset dispositions do not cover every frozen requirement exactly once",
            rule_id=rule.rule_id,
        )
    produced_values = {_artifact_identity_value(binding.identity_binding, view) for view in views}
    produced_requirement_ids: set[str] = set()
    for row in rows:
        if row.status == "produced":
            if row.reason_code is not None:
                raise IntegrityViolation(
                    "produced disposition cannot carry a reason", requirement_id=row.requirement_id
                )
            if row.requirement_id not in produced_values:
                raise IntegrityViolation(
                    "produced requirement has no matching artifact",
                    requirement_id=row.requirement_id,
                )
            produced_requirement_ids.add(row.requirement_id)
        else:  # not_executed
            if row.reason_code not in binding.allowed_not_executed_reason_codes:
                raise IntegrityViolation(
                    "not-executed reason is outside the policy allowlist",
                    requirement_id=row.requirement_id,
                    reason_code=row.reason_code,
                )
            if row.requirement_id in produced_values:
                raise IntegrityViolation(
                    "not-executed requirement unexpectedly produced an artifact",
                    requirement_id=row.requirement_id,
                )
    if produced_requirement_ids != produced_values or len(views) != len(produced_requirement_ids):
        raise IntegrityViolation(
            "produced artifacts do not match the produced dispositions",
            rule_id=rule.rule_id,
        )


def _validate_collection_identity(
    identity: ArtifactIdentityBindingV1,
    collection: Sequence[object],
    views: Sequence[PreparedArtifactView],
    rule: OutcomeArtifactRuleV1,
) -> None:
    item_values: list[object] = []
    for item in collection:
        pointer = identity.collection_item_pointer
        item_values.append(resolve_json_pointer(item, pointer) if pointer is not None else item)
    artifact_values = [_artifact_identity_value(identity, view) for view in views]
    if len(set(map(_hashable, item_values))) != len(item_values):
        raise IntegrityViolation("collection identity values are not unique", rule_id=rule.rule_id)
    if sorted(map(_hashable, item_values)) != sorted(map(_hashable, artifact_values)):
        raise IntegrityViolation(
            "collection items and artifacts are not a stable one-to-one map",
            rule_id=rule.rule_id,
        )


def _artifact_identity_value(
    identity: ArtifactIdentityBindingV1, view: PreparedArtifactView
) -> object:
    if identity.artifact_value_source == "artifact_id":
        # Resolved after minting; the publisher re-verifies against published ids.
        return view.meta.get("__artifact_id__", view.index)
    pointer = identity.artifact_payload_pointer
    if pointer is None:  # pragma: no cover - contract guarantees the pointer
        raise IntegrityViolation("payload identity binding lacks a pointer")
    return resolve_json_pointer(view.payload, pointer)


def _hashable(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, Mapping):
        return tuple(sorted((key, _hashable(item)) for key, item in value.items()))
    return value


__all__ = [
    "PlanRule",
    "PreparedArtifactView",
    "RuleAllocation",
    "allocate_artifacts",
    "resolve_json_pointer",
    "validate_rule_cardinality",
]
