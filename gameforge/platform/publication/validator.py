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
from gameforge.contracts.execution_profiles import ResolvedExecutionProfileBindingV1
from gameforge.contracts.jobs import (
    ArtifactIdentityBindingV1,
    ExecutionIdentityCountBindingV1,
    ExecutionModeCountBindingV1,
    IntermediateCountBindingV1,
    JsonCollectionCountBindingV1,
    OutcomeArtifactRuleV1,
    RequirementDispositionV1,
    ResolvedArtifactRequirementV1,
    ResolvedPolicyCountBindingV1,
    ResolvedPolicySnapshotV1,
    ResolvedPolicySubsetCountBindingV1,
    RuntimeParentRuleSetV1,
    RuntimeParentRuleV1,
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
    blob: bytes = b""


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
    published_artifact_ids_by_index: Mapping[int, str] | None = None,
    defer_artifact_id_identity: bool = False,
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
            published_artifact_ids_by_index=published_artifact_ids_by_index,
            defer_artifact_id_identity=defer_artifact_id_identity,
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
        if not (
            defer_artifact_id_identity
            and _requires_published_artifact_id(binding.identity_binding)
            and published_artifact_ids_by_index is None
        ):
            _validate_requirement_identity(
                binding.identity_binding,
                requirements,
                views,
                rule,
                published_artifact_ids_by_index=published_artifact_ids_by_index,
            )
    elif isinstance(binding, ResolvedPolicySubsetCountBindingV1):
        _validate_subset_binding(
            binding=binding,
            rule=rule,
            views=views,
            snapshots_by_id=snapshots_by_id,
            dispositions=dispositions,
            published_artifact_ids_by_index=published_artifact_ids_by_index,
            defer_artifact_id_identity=defer_artifact_id_identity,
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
    published_artifact_ids_by_index: Mapping[int, str] | None,
    defer_artifact_id_identity: bool,
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
        if (
            defer_artifact_id_identity
            and _requires_published_artifact_id(binding.identity_binding)
            and published_artifact_ids_by_index is None
        ):
            return
        _validate_collection_identity(
            binding.identity_binding,
            collection,
            views,
            rule,
            published_artifact_ids_by_index=published_artifact_ids_by_index,
        )


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
    *,
    published_artifact_ids_by_index: Mapping[int, str] | None,
) -> None:
    # ResolvedPolicy identity is fixed to requirement /requirement_id ↔ payload /requirement_id.
    by_requirement = {requirement.requirement_id: requirement for requirement in requirements}
    seen: set[str] = set()
    for view in views:
        value = _artifact_identity_value(
            identity,
            view,
            published_artifact_ids_by_index=published_artifact_ids_by_index,
        )
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
    published_artifact_ids_by_index: Mapping[int, str] | None,
    defer_artifact_id_identity: bool,
) -> None:
    requirements = _requirements_for(
        binding.resolved_policy_id, binding.outcome_rule_id, snapshots_by_id
    )
    by_requirement = {requirement.requirement_id: requirement for requirement in requirements}
    if len(by_requirement) != len(requirements):
        raise IntegrityViolation(
            "subset requirements cannot reuse a requirement id",
            rule_id=rule.rule_id,
        )
    frozen_ids = set(by_requirement)
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
    deferred_identity = (
        defer_artifact_id_identity
        and _requires_published_artifact_id(binding.identity_binding)
        and published_artifact_ids_by_index is None
    )
    produced_values: set[object] = set()
    if not deferred_identity:
        for view in views:
            value = _artifact_identity_value(
                binding.identity_binding,
                view,
                published_artifact_ids_by_index=published_artifact_ids_by_index,
            )
            requirement = by_requirement.get(value)
            if requirement is None or value in produced_values:
                raise IntegrityViolation(
                    "subset artifact does not map one-to-one onto a frozen requirement",
                    rule_id=rule.rule_id,
                    artifact_index=view.index,
                )
            if (
                requirement.artifact_kind != view.kind
                or requirement.payload_schema_id != view.payload_schema_id
            ):
                raise IntegrityViolation(
                    "subset artifact kind/schema differs from its frozen requirement",
                    rule_id=rule.rule_id,
                    requirement_id=requirement.requirement_id,
                )
            produced_values.add(value)
    produced_requirement_ids: set[str] = set()
    for row in rows:
        if row.status == "produced":
            if row.reason_code is not None:
                raise IntegrityViolation(
                    "produced disposition cannot carry a reason", requirement_id=row.requirement_id
                )
            if not deferred_identity and row.requirement_id not in produced_values:
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
            if not deferred_identity and row.requirement_id in produced_values:
                raise IntegrityViolation(
                    "not-executed requirement unexpectedly produced an artifact",
                    requirement_id=row.requirement_id,
                )
    if deferred_identity:
        if len(views) != len(produced_requirement_ids):
            raise IntegrityViolation(
                "produced artifact count differs from produced dispositions",
                rule_id=rule.rule_id,
            )
    elif produced_requirement_ids != produced_values or len(views) != len(produced_requirement_ids):
        raise IntegrityViolation(
            "produced artifacts do not match the produced dispositions",
            rule_id=rule.rule_id,
        )


def validate_plan_dispositions(
    *,
    plan_rules: Sequence[PlanRule],
    snapshots_by_id: Mapping[str, ResolvedPolicySnapshotV1],
    dispositions: Sequence[RequirementDispositionV1],
) -> None:
    """Require dispositions to be the exact union of all subset bindings.

    A disposition is authoritative terminal metadata, not an optional worker note.
    Therefore every row must be claimed by exactly one
    ``ResolvedPolicySubsetCountBindingV1`` in the selected publication plan, and
    every frozen requirement of every such binding must have exactly one row.  A
    plan with no subset binding accepts no dispositions at all.
    """

    selectors: set[tuple[str, str]] = set()
    expected: set[tuple[str, str, str]] = set()
    for plan_rule in plan_rules:
        binding = plan_rule.rule.count_binding
        if not isinstance(binding, ResolvedPolicySubsetCountBindingV1):
            continue
        selector = (binding.resolved_policy_id, binding.outcome_rule_id)
        if selector in selectors:
            raise IntegrityViolation(
                "publication plan repeats a subset-disposition selector",
                resolved_policy_id=binding.resolved_policy_id,
                outcome_rule_id=binding.outcome_rule_id,
            )
        selectors.add(selector)
        requirements = _requirements_for(*selector, snapshots_by_id)
        requirement_ids = [item.requirement_id for item in requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise IntegrityViolation(
                "subset requirements cannot reuse a requirement id",
                resolved_policy_id=binding.resolved_policy_id,
                outcome_rule_id=binding.outcome_rule_id,
            )
        expected.update(
            (binding.resolved_policy_id, binding.outcome_rule_id, requirement_id)
            for requirement_id in requirement_ids
        )

    actual = [
        (row.resolved_policy_id, row.outcome_rule_id, row.requirement_id) for row in dispositions
    ]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise IntegrityViolation(
            "requirement dispositions do not exactly cover the publication plan subsets"
        )


def validate_requirement_profile_bindings(
    *,
    snapshots: Sequence[ResolvedPolicySnapshotV1],
    resolved_profiles: Sequence[ResolvedExecutionProfileBindingV1],
) -> None:
    """Re-close every resolved requirement onto the Run's frozen profiles."""

    profiles = {binding.field_path: binding for binding in resolved_profiles}
    if len(profiles) != len(resolved_profiles):
        raise IntegrityViolation("resolved profile field paths are not unique")
    for snapshot in snapshots:
        source = profiles.get(snapshot.source_profile_field_path)
        if source is None or source.profile_payload_hash != snapshot.source_profile_payload_hash:
            raise IntegrityViolation(
                "resolved-policy snapshot source profile is not frozen in the Run",
                resolved_policy_id=snapshot.resolved_policy_id,
            )
        for requirement in snapshot.requirements:
            path = requirement.producer_profile_field_path
            if path is not None and path not in profiles:
                raise IntegrityViolation(
                    "resolved requirement producer profile is not frozen in the Run",
                    resolved_policy_id=snapshot.resolved_policy_id,
                    requirement_id=requirement.requirement_id,
                    field_path=path,
                )


def validate_published_artifact_ids(
    *,
    artifacts: Sequence[PreparedArtifactView],
    published_artifact_ids_by_index: Mapping[int, str],
) -> None:
    """Prove every Prepared entry minted one distinct content-addressed Artifact."""

    indexes = [view.index for view in artifacts]
    if len(indexes) != len(set(indexes)):
        raise IntegrityViolation("prepared artifact indexes are not unique")
    if set(published_artifact_ids_by_index) != set(indexes):
        raise IntegrityViolation(
            "published artifact ids do not cover every prepared artifact exactly once"
        )
    published_ids = tuple(published_artifact_ids_by_index.values())
    if len(published_ids) != len(set(published_ids)):
        raise IntegrityViolation("multiple prepared artifacts collapsed to one published Artifact")


def _validate_collection_identity(
    identity: ArtifactIdentityBindingV1,
    collection: Sequence[object],
    views: Sequence[PreparedArtifactView],
    rule: OutcomeArtifactRuleV1,
    *,
    published_artifact_ids_by_index: Mapping[int, str] | None,
) -> None:
    item_values: list[object] = []
    for item in collection:
        pointer = identity.collection_item_pointer
        item_values.append(resolve_json_pointer(item, pointer) if pointer is not None else item)
    artifact_values = [
        _artifact_identity_value(
            identity,
            view,
            published_artifact_ids_by_index=published_artifact_ids_by_index,
        )
        for view in views
    ]
    if len(set(map(_hashable, item_values))) != len(item_values):
        raise IntegrityViolation("collection identity values are not unique", rule_id=rule.rule_id)
    if sorted(map(_hashable, item_values)) != sorted(map(_hashable, artifact_values)):
        raise IntegrityViolation(
            "collection items and artifacts are not a stable one-to-one map",
            rule_id=rule.rule_id,
        )


def _artifact_identity_value(
    identity: ArtifactIdentityBindingV1,
    view: PreparedArtifactView,
    *,
    published_artifact_ids_by_index: Mapping[int, str] | None,
) -> object:
    if identity.artifact_value_source == "artifact_id":
        if (
            published_artifact_ids_by_index is None
            or view.index not in published_artifact_ids_by_index
        ):
            raise IntegrityViolation(
                "artifact-id identity binding requires the exact published artifact id",
                artifact_index=view.index,
            )
        return published_artifact_ids_by_index[view.index]
    pointer = identity.artifact_payload_pointer
    if pointer is None:  # pragma: no cover - contract guarantees the pointer
        raise IntegrityViolation("payload identity binding lacks a pointer")
    return resolve_json_pointer(view.payload, pointer)


def _requires_published_artifact_id(identity: ArtifactIdentityBindingV1) -> bool:
    return identity.artifact_value_source == "artifact_id"


def _hashable(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, Mapping):
        return tuple(sorted((key, _hashable(item)) for key, item in value.items()))
    return value


@dataclass(frozen=True, slots=True)
class ProjectedRuntimeParent:
    """One runtime intermediate/input parent as classified by its trusted source."""

    artifact_id: str
    source: str
    kind: str
    payload_schema_id: str


def validate_runtime_parents(
    *,
    rule_set: RuntimeParentRuleSetV1,
    manifest_scope: str,
    llm_execution_mode: str,
    parents: Sequence[ProjectedRuntimeParent],
    committed_link_counts: Mapping[str, int],
    execution_identity_counts: Mapping[str, int] | None = None,
) -> None:
    """Cross-check the projected runtime parents against ``runtime-parents@1``.

    Every projected parent must be claimed by exactly one in-scope rule; a parent
    whose rule is disabled in the current execution mode is fail-closed.  Each
    rule's observed count must equal the exact count its ``count_binding`` derives
    (``IntermediateCountBinding`` from committed prompt-link counts;
    ``ExecutionModeCountBinding`` from the four per-mode counts) and stay within
    ``[min_count, max_count]``.  For a ``not_applicable`` run every LLM/cassette rule
    is disabled, so this asserts zero such parents explicitly rather than by
    accident.
    """

    scoped = tuple(
        rule for rule in rule_set.rules if rule.manifest_scope in (manifest_scope, "both")
    )
    _validate_runtime_rule_shapes(scoped, manifest_scope=manifest_scope)
    counts: dict[str, int] = {rule.rule_id: 0 for rule in scoped}
    for parent in parents:
        matches = [
            rule
            for rule in scoped
            if rule.source == parent.source
            and parent.kind == rule.artifact_kind
            and parent.payload_schema_id in rule.payload_schema_ids
        ]
        if len(matches) != 1:
            raise IntegrityViolation(
                "runtime parent matched no unique rule-set rule",
                artifact_id=parent.artifact_id,
                source=parent.source,
                matched=[rule.rule_id for rule in matches],
            )
        rule = matches[0]
        if llm_execution_mode not in rule.enabled_execution_modes:
            raise IntegrityViolation(
                "runtime parent present in a disabled execution mode",
                artifact_id=parent.artifact_id,
                rule_id=rule.rule_id,
                mode=llm_execution_mode,
            )
        counts[rule.rule_id] += 1

    for rule in scoped:
        observed = counts[rule.rule_id]
        if llm_execution_mode not in rule.enabled_execution_modes:
            # A disabled rule contributes an exact count of zero; its [min,max]
            # range (e.g. replay-input min_count=1) does not apply in this mode.
            if observed != 0:  # pragma: no cover - already caught in the match loop
                raise IntegrityViolation(
                    "runtime parent present for a disabled rule",
                    rule_id=rule.rule_id,
                    mode=llm_execution_mode,
                )
            continue
        expected = _expected_runtime_count(
            rule=rule,
            llm_execution_mode=llm_execution_mode,
            committed_link_counts=committed_link_counts,
            execution_identity_counts=execution_identity_counts or {},
        )
        if expected is not None and observed != expected:
            raise IntegrityViolation(
                "runtime parent count differs from its rule-set binding",
                rule_id=rule.rule_id,
                expected=expected,
                actual=observed,
            )
        if observed < rule.min_count or (rule.max_count is not None and observed > rule.max_count):
            raise IntegrityViolation(
                "runtime parent count is outside its rule range",
                rule_id=rule.rule_id,
                min_count=rule.min_count,
                max_count=rule.max_count,
                actual=observed,
            )


def _validate_runtime_rule_shapes(
    rules: Sequence[RuntimeParentRuleV1],
    *,
    manifest_scope: str,
) -> None:
    """Reject ambiguous or internally contradictory runtime-parent policies."""

    if manifest_scope not in {"attempt", "run"}:
        raise IntegrityViolation("runtime-parent validation received an unknown manifest scope")

    expected_role = {
        "run_input": "input",
        "published_intermediate": "intermediate",
        "record_shard": "intermediate",
        "attempt_bundle": "intermediate",
        "run_bundle": "intermediate",
        "closed_attempt_failure": "intermediate",
    }
    for rule in rules:
        if not rule.payload_schema_ids or any("*" in item for item in rule.payload_schema_ids):
            raise IntegrityViolation(
                "runtime-parent payload schema allowlist must be exact and non-empty",
                rule_id=rule.rule_id,
            )
        if rule.parent_role != expected_role[rule.source]:
            raise IntegrityViolation(
                "runtime-parent source uses an incompatible manifest role",
                rule_id=rule.rule_id,
            )

        expected_selector = {
            "run_input": "none",
            "published_intermediate": ("current" if manifest_scope == "attempt" else "all_closed"),
            "record_shard": "current" if manifest_scope == "attempt" else "all_closed",
            "attempt_bundle": "current",
            "run_bundle": "all_closed",
            "closed_attempt_failure": "all_closed",
        }[rule.source]
        if rule.attempt_selector != expected_selector:
            raise IntegrityViolation(
                "runtime-parent attempt selector differs from its source/scope",
                rule_id=rule.rule_id,
            )
        if rule.source == "attempt_bundle" and manifest_scope != "attempt":
            raise IntegrityViolation(
                "attempt cassette bundle cannot be projected into a run-scoped rule",
                rule_id=rule.rule_id,
            )
        if rule.source in {"run_bundle", "closed_attempt_failure"} and manifest_scope != "run":
            raise IntegrityViolation(
                "run aggregate parent cannot be projected into an attempt-scoped rule",
                rule_id=rule.rule_id,
            )

        binding = rule.count_binding
        if binding is not None and not isinstance(
            binding,
            (
                IntermediateCountBindingV1,
                ExecutionIdentityCountBindingV1,
                ExecutionModeCountBindingV1,
            ),
        ):
            raise IntegrityViolation(
                "runtime-parent rule uses a non-runtime count binding",
                rule_id=rule.rule_id,
                binding_source=binding.source,
            )
        if isinstance(binding, IntermediateCountBindingV1):
            if rule.source != "published_intermediate":
                raise IntegrityViolation(
                    "intermediate-link count binding uses an incompatible parent source",
                    rule_id=rule.rule_id,
                )
            expected_scope = "current_attempt" if manifest_scope == "attempt" else "all_attempts"
            if binding.scope != expected_scope:
                raise IntegrityViolation(
                    "intermediate-link count scope differs from manifest scope",
                    rule_id=rule.rule_id,
                )
        elif isinstance(binding, ExecutionIdentityCountBindingV1):
            if rule.source != "record_shard":
                raise IntegrityViolation(
                    "execution-identity count binding uses an incompatible parent source",
                    rule_id=rule.rule_id,
                )
            expected_scope = "current_attempt" if manifest_scope == "attempt" else "all_attempts"
            if binding.scope != expected_scope:
                raise IntegrityViolation(
                    "execution-identity count scope differs from manifest scope",
                    rule_id=rule.rule_id,
                )
        elif isinstance(binding, ExecutionModeCountBindingV1):
            for mode in ("not_applicable", "live", "record", "replay"):
                exact_count = getattr(binding.exact_count_by_mode, mode)
                if mode not in rule.enabled_execution_modes and exact_count != 0:
                    raise IntegrityViolation(
                        "disabled execution mode has a non-zero runtime-parent count",
                        rule_id=rule.rule_id,
                        mode=mode,
                    )
                if mode in rule.enabled_execution_modes and (
                    exact_count < rule.min_count
                    or (rule.max_count is not None and exact_count > rule.max_count)
                ):
                    raise IntegrityViolation(
                        "execution-mode count is outside the runtime-parent rule range",
                        rule_id=rule.rule_id,
                        mode=mode,
                    )

    for position, left in enumerate(rules):
        for right in rules[position + 1 :]:
            if (
                left.source == right.source
                and left.artifact_kind == right.artifact_kind
                and set(left.payload_schema_ids).intersection(right.payload_schema_ids)
            ):
                raise IntegrityViolation(
                    "runtime-parent rules have overlapping selectors",
                    rules=sorted((left.rule_id, right.rule_id)),
                )


def _expected_runtime_count(
    *,
    rule: RuntimeParentRuleV1,
    llm_execution_mode: str,
    committed_link_counts: Mapping[str, int],
    execution_identity_counts: Mapping[str, int],
) -> int | None:
    binding = rule.count_binding
    if binding is None:
        return None
    if isinstance(binding, IntermediateCountBindingV1):
        if binding.scope not in committed_link_counts:
            raise IntegrityViolation(
                "committed intermediate-link count is unavailable for the binding scope",
                rule_id=rule.rule_id,
                scope=binding.scope,
            )
        return committed_link_counts[binding.scope]
    if isinstance(binding, ExecutionIdentityCountBindingV1):
        if binding.scope not in execution_identity_counts:
            raise IntegrityViolation(
                "execution identity count is unavailable for the binding scope",
                rule_id=rule.rule_id,
                scope=binding.scope,
            )
        return execution_identity_counts[binding.scope]
    if isinstance(binding, ExecutionModeCountBindingV1):
        return getattr(binding.exact_count_by_mode, llm_execution_mode)
    raise IntegrityViolation(
        "runtime-parent rule uses a non-runtime count binding",
        rule_id=rule.rule_id,
        binding_source=binding.source,
    )


__all__ = [
    "PlanRule",
    "PreparedArtifactView",
    "ProjectedRuntimeParent",
    "RuleAllocation",
    "allocate_artifacts",
    "resolve_json_pointer",
    "validate_plan_dispositions",
    "validate_published_artifact_ids",
    "validate_requirement_profile_bindings",
    "validate_rule_cardinality",
    "validate_runtime_parents",
]
