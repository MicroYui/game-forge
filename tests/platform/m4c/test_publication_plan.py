"""Unit tests for publication-plan selection, allocation and projection (Task 9).

Covers the pure engine surface: mutually-exclusive/gap-free policy selection,
the unique-rule-allocation invariant, count/identity/subset bindings, typed
lineage-role projection and VersionTuple projection.
"""

from __future__ import annotations

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    ArtifactIdentityBindingV1,
    ExecutionModeCountBindingV1,
    ExecutionModeCountsV1,
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
    resolved_policy_snapshot_digest,
)
from gameforge.contracts.execution_profiles import ArtifactLineagePolicyRefV1
from gameforge.contracts.lineage import ObjectLocation, VersionTuple, object_ref_for_bytes
from gameforge.platform.publication.effects import resolve_workflow_effect
from gameforge.platform.publication.lineage import (
    LineageParentSources,
    ParentInfo,
    project_typed_lineage,
)
from gameforge.platform.publication.validator import (
    PlanRule,
    PreparedArtifactView,
    ProjectedRuntimeParent,
    allocate_artifacts,
    validate_rule_cardinality,
    validate_runtime_parents,
)
from gameforge.platform.publication.version import (
    project_domain_version_tuple,
    project_manifest_version_tuple,
)
from gameforge.platform.publication.planner import build_publication_plan, resolve_definition
from gameforge.platform.registry.defaults import (
    _OutcomeBuilder,
    _runtime_parent_rules,
    _simple_primary_policy,
    _transition_policy,
)
from gameforge.platform.runs.lifecycle import select_outcome_policy
from tests.platform.m4c.test_terminal_publisher import (
    _registry_and_definition,
    _run_record,
)


def _builder() -> _OutcomeBuilder:
    return _OutcomeBuilder(
        attempt_transition=_transition_policy(scope="attempt"),
        run_transition=_transition_policy(scope="run"),
    )


def _checker_lineage(builder: _OutcomeBuilder):
    _simple_primary_policy(
        builder,
        policy_id="checker-completed",
        outcome_code="checker_completed",
        artifact_kind="checker_run",
        payload_schema_id="checker-report@1",
    )
    return builder.lineage_policies[("checker-completed/primary-lineage", 1)]


def _view(index: int, *, kind="checker_run", schema="checker-report@1", payload=None, lineage=()):
    ref = object_ref_for_bytes(f"blob-{index}".encode())
    return PreparedArtifactView(
        index=index,
        kind=kind,
        payload_schema_id=schema,
        version_tuple=VersionTuple(),
        lineage=tuple(lineage),
        payload_hash=ref.sha256,
        object_ref=ref,
        location=ObjectLocation(store_id="s3", key=ref.key, backend_generation="g1"),
        meta={},
        payload=payload or {},
    )


def _rule(
    *,
    rule_id="primary",
    role="primary",
    kind="checker_run",
    schemas=("checker-report@1",),
    min_count=1,
    max_count=1,
    binding=None,
):
    return OutcomeArtifactRuleV1(
        rule_id=rule_id,
        role=role,
        artifact_kind=kind,
        payload_schema_ids=schemas,
        min_count=min_count,
        max_count=max_count,
        count_binding=binding,
        lineage_policy_ref=ArtifactLineagePolicyRefV1(
            policy_id=f"p/{rule_id}-lineage", policy_version=1, digest="a" * 64
        ),
    )


def _plan_rule(rule, scope="run", policy_id="p"):
    return PlanRule(scope=scope, policy_id=policy_id, rule=rule)


# ----------------------------------------------------------------- allocation
def test_allocation_matches_each_artifact_to_exactly_one_rule():
    plan = [_plan_rule(_rule())]
    views = [_view(0)]
    allocations = allocate_artifacts(plan_rules=plan, artifacts=views)
    assert allocations[0].artifact_indexes == (0,)


def test_allocation_rejects_unmatched_extra_artifact():
    plan = [_plan_rule(_rule())]
    views = [_view(0), _view(1, kind="patch", schema="patch@2")]
    with pytest.raises(IntegrityViolation):
        allocate_artifacts(plan_rules=plan, artifacts=views)


def test_allocation_rejects_overlapping_rules():
    plan = [_plan_rule(_rule(rule_id="a")), _plan_rule(_rule(rule_id="b"))]
    with pytest.raises(IntegrityViolation):
        allocate_artifacts(plan_rules=plan, artifacts=[_view(0)])


def test_allocation_rejects_wildcard_schema():
    plan = [_plan_rule(_rule(schemas=("checker-*",)))]
    with pytest.raises(IntegrityViolation):
        allocate_artifacts(plan_rules=plan, artifacts=[_view(0)])


def test_missing_required_artifact_fails_cardinality():
    rule = _rule()
    plan = [_plan_rule(rule)]
    allocations = allocate_artifacts(plan_rules=plan, artifacts=[])
    with pytest.raises(IntegrityViolation):
        validate_rule_cardinality(
            allocation=allocations[0],
            artifacts_by_index={},
            run_payload={},
            primary_payload=None,
            snapshots_by_id={},
            dispositions=(),
        )


# ------------------------------------------------------------- count bindings
def test_json_collection_binding_matches_payload_array():
    binding = JsonCollectionCountBindingV1(
        source="run_payload", collection_pointer="/params/profiles"
    )
    rule = _rule(rule_id="checker", role="output", min_count=0, max_count=None, binding=binding)
    plan = [_plan_rule(rule)]
    views = [_view(0), _view(1)]
    allocations = allocate_artifacts(plan_rules=plan, artifacts=views)
    run_payload = {"params": {"profiles": ["a", "b"]}}
    validate_rule_cardinality(
        allocation=allocations[0],
        artifacts_by_index={0: views[0], 1: views[1]},
        run_payload=run_payload,
        primary_payload=None,
        snapshots_by_id={},
        dispositions=(),
    )
    # A third artifact violates the frozen collection count.
    views3 = [*views, _view(2)]
    allocations3 = allocate_artifacts(plan_rules=plan, artifacts=views3)
    with pytest.raises(IntegrityViolation):
        validate_rule_cardinality(
            allocation=allocations3[0],
            artifacts_by_index={view.index: view for view in views3},
            run_payload=run_payload,
            primary_payload=None,
            snapshots_by_id={},
            dispositions=(),
        )


def _snapshot():
    requirements = (
        ResolvedArtifactRequirementV1(
            requirement_id="r1",
            outcome_rule_id="checker",
            artifact_kind="checker_run",
            payload_schema_id="checker-report@1",
            ordinal=1,
        ),
    )
    base = {
        "snapshot_schema_version": "resolved-policy@1",
        "resolved_policy_id": "gen",
        "source_profile_field_path": "/resolved_profiles/0",
        "source_profile_payload_hash": "a" * 64,
        "requirements": [r.model_dump(mode="json") for r in requirements],
    }
    return ResolvedPolicySnapshotV1(**base, digest=resolved_policy_snapshot_digest(base))


def test_resolved_policy_binding_maps_requirement_identity():
    binding = ResolvedPolicyCountBindingV1(
        resolved_policy_id="gen",
        outcome_rule_id="checker",
        identity_binding=ArtifactIdentityBindingV1(
            collection_item_pointer="/requirement_id",
            artifact_value_source="payload",
            artifact_payload_pointer="/requirement_id",
        ),
    )
    rule = _rule(rule_id="checker", role="output", min_count=0, max_count=None, binding=binding)
    view = _view(0, payload={"requirement_id": "r1"})
    allocations = allocate_artifacts(plan_rules=[_plan_rule(rule)], artifacts=[view])
    validate_rule_cardinality(
        allocation=allocations[0],
        artifacts_by_index={0: view},
        run_payload={},
        primary_payload=None,
        snapshots_by_id={"gen": _snapshot()},
        dispositions=(),
    )
    # Wrong requirement id breaks the one-to-one map.
    bad = _view(0, payload={"requirement_id": "other"})
    allocations_bad = allocate_artifacts(plan_rules=[_plan_rule(rule)], artifacts=[bad])
    with pytest.raises(IntegrityViolation):
        validate_rule_cardinality(
            allocation=allocations_bad[0],
            artifacts_by_index={0: bad},
            run_payload={},
            primary_payload=None,
            snapshots_by_id={"gen": _snapshot()},
            dispositions=(),
        )


def test_subset_binding_requires_complete_dispositions():
    binding = ResolvedPolicySubsetCountBindingV1(
        resolved_policy_id="gen",
        outcome_rule_id="checker",
        allowed_not_executed_reason_codes=("search_exhausted",),
        identity_binding=ArtifactIdentityBindingV1(
            collection_item_pointer="/requirement_id",
            artifact_value_source="payload",
            artifact_payload_pointer="/requirement_id",
        ),
    )
    rule = _rule(rule_id="checker", role="output", min_count=0, max_count=None, binding=binding)
    # Frozen requirement r1 is not executed: no artifact, valid reason.
    allocations = allocate_artifacts(plan_rules=[_plan_rule(rule)], artifacts=[])
    validate_rule_cardinality(
        allocation=allocations[0],
        artifacts_by_index={},
        run_payload={},
        primary_payload=None,
        snapshots_by_id={"gen": _snapshot()},
        dispositions=(
            RequirementDispositionV1(
                resolved_policy_id="gen",
                outcome_rule_id="checker",
                requirement_id="r1",
                status="not_executed",
                reason_code="search_exhausted",
            ),
        ),
    )
    # A disallowed reason code is fail-closed.
    with pytest.raises(IntegrityViolation):
        validate_rule_cardinality(
            allocation=allocations[0],
            artifacts_by_index={},
            run_payload={},
            primary_payload=None,
            snapshots_by_id={"gen": _snapshot()},
            dispositions=(
                RequirementDispositionV1(
                    resolved_policy_id="gen",
                    outcome_rule_id="checker",
                    requirement_id="r1",
                    status="not_executed",
                    reason_code="made_up",
                ),
            ),
        )


# -------------------------------------------------------- typed lineage roles
def _sources(*, include_input=True):
    inputs = {}
    if include_input:
        inputs = {
            "artifact:input": ParentInfo(
                artifact_id="artifact:input",
                kind="ir_snapshot",
                payload_schema_id="ir-core@1",
                version_tuple=VersionTuple(ir_snapshot_id="snapshot:input"),
            )
        }
    return LineageParentSources(run_inputs=inputs, run_intermediates={}, prepared_siblings={})


def test_typed_lineage_projects_snapshot_role():
    policy = _checker_lineage(_builder())
    typed = project_typed_lineage(
        policy=policy,
        child_kind="checker_run",
        child_payload_schema_id="checker-report@1",
        child_lineage=("artifact:input",),
        sources=_sources(),
    )
    assert [info.artifact_id for info in typed.parents_by_role["snapshot"]] == ["artifact:input"]
    assert typed.parents_by_role["constraint"] == ()


def test_typed_lineage_rejects_dangling_parent():
    policy = _checker_lineage(_builder())
    with pytest.raises(IntegrityViolation):
        project_typed_lineage(
            policy=policy,
            child_kind="checker_run",
            child_payload_schema_id="checker-report@1",
            child_lineage=("artifact:input",),
            sources=_sources(include_input=False),
        )


def test_domain_version_tuple_inherits_and_uses_producer_value():
    policy = _checker_lineage(_builder())
    tuple_out = project_domain_version_tuple(
        policy=policy,
        parent_tuples={
            "snapshot": (VersionTuple(ir_snapshot_id="snapshot:input"),),
            "constraint": (),
        },
        producer_tuple=VersionTuple(tool_version="checker@1"),
    )
    assert tuple_out.ir_snapshot_id == "snapshot:input"
    assert tuple_out.tool_version == "checker@1"
    assert tuple_out.constraint_snapshot_id is None
    assert tuple_out.prompt_version is None


# --------------------------------------------------------- manifest transition
def test_manifest_transition_nulls_invocation_fields_for_not_applicable():
    run_transition = _transition_policy(scope="run")
    frozen = VersionTuple(
        ir_snapshot_id="snapshot:input",
        tool_version="checker@1",
        prompt_version="p1",
        model_snapshot="m1",
    )
    projected = project_manifest_version_tuple(
        policy=run_transition,
        manifest_scope="run",
        llm_execution_mode="not_applicable",
        frozen_tuple=frozen,
        execution_identity=None,
        cassette_ids_by_scope={},
    )
    assert projected.ir_snapshot_id == "snapshot:input"
    assert projected.tool_version == "checker@1"
    assert projected.prompt_version is None
    assert projected.model_snapshot is None
    assert projected.cassette_id is None


def test_manifest_transition_rejects_wrong_scope():
    attempt_transition = _transition_policy(scope="attempt")
    with pytest.raises(IntegrityViolation):
        project_manifest_version_tuple(
            policy=attempt_transition,
            manifest_scope="run",
            llm_execution_mode="not_applicable",
            frozen_tuple=VersionTuple(),
            execution_identity=None,
            cassette_ids_by_scope={},
        )


# ------------------------------------------------------------- plan selection
def test_build_publication_plan_resolves_exact_policies():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    resolved = resolve_definition(registry=registry, run=run)
    policy = select_outcome_policy(
        definition=resolved,
        outcome_code="checker_completed",
        prepared_outcome="success",
        publication_scope="run",
        run_status="succeeded",
        attempt_status=None,
        failure_class=None,
        retry_disposition=None,
    )
    plan = build_publication_plan(
        registry=registry, definition=resolved, policy=policy, scope="run"
    )
    assert plan.transition_policy.manifest_scope == "run"
    assert plan.finding_policy is not None
    assert "primary" in plan.lineage_by_rule_id
    assert [pr.rule.rule_id for pr in plan.plan_rules] == ["primary"]


def test_select_policy_gap_is_fail_closed():
    _, definition = _registry_and_definition()
    with pytest.raises(IntegrityViolation):
        select_outcome_policy(
            definition=definition,
            outcome_code="does_not_exist",
            prepared_outcome="success",
            publication_scope="run",
            run_status="succeeded",
            attempt_status=None,
            failure_class=None,
            retry_disposition=None,
        )


def test_attempt_scope_plan_rejects_business_artifact_rules():
    registry, definition = _registry_and_definition()
    run = _run_record(definition)
    resolved = resolve_definition(registry=registry, run=run)
    # The success policy carries a business primary rule; used at attempt scope it
    # violates the attempt-close invariant (artifact_rules must be empty).
    success = select_outcome_policy(
        definition=resolved,
        outcome_code="checker_completed",
        prepared_outcome="success",
        publication_scope="run",
        run_status="succeeded",
        attempt_status=None,
        failure_class=None,
        retry_disposition=None,
    )
    with pytest.raises(IntegrityViolation):
        build_publication_plan(
            registry=registry, definition=resolved, policy=success, scope="attempt"
        )


def test_duplicate_selectors_fail_registry_load():
    from gameforge.contracts.jobs import RunKindDefinition

    _, definition = _registry_and_definition()
    success = next(
        policy for policy in definition.outcome_policies if policy.policy_id == "checker-completed"
    )
    twin = success.model_copy(update={"policy_id": "checker-completed-twin"})
    with pytest.raises(ValueError):
        RunKindDefinition.model_validate(
            {
                **definition.model_dump(mode="python"),
                "outcome_policies": (*definition.outcome_policies, twin),
            }
        )


# -------------------------------------------------------- workflow effects (#1)
def test_validation_completion_effects_are_registered():
    # Task 17b registers the validation-completion + revert effects; a validation
    # terminal now runs the ApprovalItem CAS inside the publisher's UoW instead of
    # fail-closing on an unregistered key.
    for key in (
        "set_patch_validated@1",
        "set_patch_validated_with_auto_proof@1",
        "set_patch_validation_failed@1",
        "set_rollback_validated@1",
        "set_rollback_validation_failed@1",
        "set_exact_binding_and_validated@1",
        "set_exact_binding_and_validation_failed@1",
        "leave_binding_null_and_validation_failed@1",
        "restore_current_draft@1",
    ):
        assert resolve_workflow_effect(key) is not None


def test_unregistered_workflow_effect_still_fails_closed():
    with pytest.raises(IntegrityViolation):
        resolve_workflow_effect("create_patch_subject_head_and_draft@1")


def test_no_op_effects_still_resolve():
    for key in (
        "no_workflow_change@1",
        "no_workflow_subject@1",
        "terminal_only@1",
        "close_attempt_for_terminal@1",
        "close_attempt_for_retry@1",
        "leave_patch_head_unchanged@1",
    ):
        assert resolve_workflow_effect(key) is not None


# ----------------------------------------------- artifact_id identity guard (#5)
def test_artifact_id_identity_binding_is_fail_closed():
    binding = JsonCollectionCountBindingV1(
        source="prepared_primary_payload",
        collection_pointer="/episodes",
        identity_binding=ArtifactIdentityBindingV1(
            collection_item_pointer="/scenario_spec_artifact_id",
            artifact_value_source="artifact_id",
        ),
    )
    rule = _rule(
        rule_id="scenario",
        role="output",
        kind="scenario_spec",
        schemas=("scenario-spec@1",),
        min_count=1,
        max_count=None,
        binding=binding,
    )
    view = _view(0, kind="scenario_spec", schema="scenario-spec@1")
    allocations = allocate_artifacts(plan_rules=[_plan_rule(rule)], artifacts=[view])
    with pytest.raises(IntegrityViolation):
        validate_rule_cardinality(
            allocation=allocations[0],
            artifacts_by_index={0: view},
            run_payload={},
            primary_payload={"episodes": [{"scenario_spec_artifact_id": "x"}]},
            snapshots_by_id={},
            dispositions=(),
        )


# ----------------------------------------------------- runtime-parents@1 (#2)
def _prompt_rule_set(*, scope="attempt", link_scope="current_attempt", enabled=None):
    return RuntimeParentRuleSetV1(
        rule_set_id="rp",
        version=1,
        rules=(
            RuntimeParentRuleV1(
                rule_id="prompts",
                manifest_scope=scope,
                source="published_intermediate",
                parent_role="intermediate",
                artifact_kind="source_rendered",
                payload_schema_ids=("source-rendered@1",),
                attempt_selector="current",
                enabled_execution_modes=enabled or ("not_applicable", "live", "record", "replay"),
                min_count=0,
                max_count=None,
                count_binding=IntermediateCountBindingV1(
                    link_role="prompt_rendered", scope=link_scope
                ),
            ),
        ),
    )


def _prompt_parent():
    return ProjectedRuntimeParent(
        artifact_id="rendered:1",
        source="published_intermediate",
        kind="source_rendered",
        payload_schema_id="source-rendered@1",
    )


def test_runtime_parents_intermediate_binding_satisfied():
    validate_runtime_parents(
        rule_set=_prompt_rule_set(),
        manifest_scope="attempt",
        llm_execution_mode="not_applicable",
        parents=[_prompt_parent()],
        committed_link_counts={"current_attempt": 1, "all_attempts": 1},
    )


def test_runtime_parents_count_mismatch_fails_closed():
    with pytest.raises(IntegrityViolation):
        validate_runtime_parents(
            rule_set=_prompt_rule_set(),
            manifest_scope="attempt",
            llm_execution_mode="not_applicable",
            parents=[_prompt_parent()],
            committed_link_counts={"current_attempt": 2, "all_attempts": 2},
        )


def test_runtime_parents_execution_mode_binding_satisfied_and_enforced():
    rule_set = RuntimeParentRuleSetV1(
        rule_set_id="rx",
        version=1,
        rules=(
            RuntimeParentRuleV1(
                rule_id="run-bundle",
                manifest_scope="run",
                source="run_bundle",
                parent_role="intermediate",
                artifact_kind="cassette_bundle",
                payload_schema_ids=("cassette-bundle@1",),
                attempt_selector="all_closed",
                enabled_execution_modes=("record",),
                min_count=0,
                max_count=1,
                count_binding=ExecutionModeCountBindingV1(
                    exact_count_by_mode=ExecutionModeCountsV1(
                        not_applicable=0, live=0, record=1, replay=0
                    )
                ),
            ),
        ),
    )
    bundle = ProjectedRuntimeParent(
        artifact_id="bundle:1",
        source="run_bundle",
        kind="cassette_bundle",
        payload_schema_id="cassette-bundle@1",
    )
    validate_runtime_parents(
        rule_set=rule_set,
        manifest_scope="run",
        llm_execution_mode="record",
        parents=[bundle],
        committed_link_counts={"current_attempt": 0, "all_attempts": 0},
    )
    with pytest.raises(IntegrityViolation):
        validate_runtime_parents(
            rule_set=rule_set,
            manifest_scope="run",
            llm_execution_mode="record",
            parents=[],
            committed_link_counts={"current_attempt": 0, "all_attempts": 0},
        )


def test_runtime_parent_in_disabled_mode_fails_closed():
    rule_set = _prompt_rule_set(enabled=("record",))
    with pytest.raises(IntegrityViolation):
        validate_runtime_parents(
            rule_set=rule_set,
            manifest_scope="attempt",
            llm_execution_mode="not_applicable",
            parents=[_prompt_parent()],
            committed_link_counts={"current_attempt": 0, "all_attempts": 0},
        )


def test_not_applicable_enforces_zero_runtime_parents():
    rule_set = _runtime_parent_rules()
    validate_runtime_parents(
        rule_set=rule_set,
        manifest_scope="attempt",
        llm_execution_mode="not_applicable",
        parents=[],
        committed_link_counts={"current_attempt": 0, "all_attempts": 0},
    )
    with pytest.raises(IntegrityViolation):
        validate_runtime_parents(
            rule_set=rule_set,
            manifest_scope="attempt",
            llm_execution_mode="not_applicable",
            parents=[_prompt_parent()],
            committed_link_counts={"current_attempt": 0, "all_attempts": 0},
        )
