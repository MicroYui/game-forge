"""Generation material inputs remain typed parents through terminal publication."""

from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    GenerationProposePayloadV2,
    PromptGoalBindingV1,
    RefReadBindingV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.platform.publication.lineage import (
    LineageParentSources,
    ParentInfo,
    project_typed_lineage,
)
from gameforge.platform.publication.payload_binding import expected_typed_run_parent_ids
from gameforge.platform.registry.defaults import build_builtin_registry
from tests.platform.m4c.handler_support import build_envelope, build_run_record


def _parent(artifact_id: str, kind: str, schema: str) -> ParentInfo:
    return ParentInfo(
        artifact_id=artifact_id,
        kind=kind,
        payload_schema_id=schema,
        version_tuple=VersionTuple(),
    )


def _run():
    params = GenerationProposePayloadV2(
        base_snapshot_artifact_id="artifact:base",
        constraint_snapshot_artifact_id=None,
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal",
            expected_payload_hash="a" * 64,
        ),
        source_artifact_ids=("artifact:material-raw", "artifact:material-rendered"),
        domain_scope=DomainScope(domain_ids=("project:one",)),
        target=RefReadBindingV1(ref_name="ref:project:one:content"),
        generation_policy=ProfileRefV1(profile_id="generation", version=1),
        candidate_export_profiles=(),
    )
    return build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="generation.propose", version=2),
    )


def _binding(rule_id: str):
    registry = build_builtin_registry()
    definition = registry.get_run_kind(RunKindRef(kind="generation.propose", version=2))
    assert definition is not None
    policy = next(
        item for item in definition.outcome_policies if item.policy_id == "generation-gate-pass"
    )
    rule = next(item for item in policy.artifact_rules if item.rule_id == rule_id)
    lineage = registry.get_lineage_policy(rule.lineage_policy_ref)
    assert lineage is not None
    return policy, rule, lineage


def test_generation_patch_and_preview_bind_exact_planning_material_parents() -> None:
    run = _run()
    params = run.payload.params
    assert isinstance(params, GenerationProposePayloadV2)
    inputs = {
        "artifact:base": _parent("artifact:base", "ir_snapshot", "ir-core@1"),
        "artifact:goal": _parent("artifact:goal", "source_raw", "source-raw@1"),
        "artifact:material-raw": _parent(
            "artifact:material-raw", "source_raw", "project-material-original@1"
        ),
        "artifact:material-rendered": _parent(
            "artifact:material-rendered", "source_rendered", "project-material-rendered@1"
        ),
    }

    policy, primary, patch_lineage = _binding("primary")
    patch = project_typed_lineage(
        policy=patch_lineage,
        child_kind="patch",
        child_payload_schema_id="patch@2",
        child_lineage=(
            "artifact:base",
            "artifact:material-raw",
            "artifact:material-rendered",
            "artifact:goal",
        ),
        sources=LineageParentSources(
            run_inputs=inputs,
            run_intermediates={},
            prepared_siblings={},
            child_payload_references={},
        ),
        expected_parent_ids_by_role=expected_typed_run_parent_ids(
            run=run,
            policy=policy,
            rule=primary,
        ),
    )
    assert (
        tuple(parent.artifact_id for parent in patch.parents_by_role["planning_material"])
        == params.source_artifact_ids
    )

    policy, preview, preview_lineage = _binding("preview")
    prepared_patch = _parent("artifact:prepared-patch", "patch", "patch@2")
    projected = project_typed_lineage(
        policy=preview_lineage,
        child_kind="ir_snapshot",
        child_payload_schema_id="ir-core@1",
        child_lineage=(
            "artifact:base",
            "artifact:material-raw",
            "artifact:material-rendered",
            "artifact:prepared-patch",
        ),
        sources=LineageParentSources(
            run_inputs=inputs,
            run_intermediates={},
            prepared_siblings={"primary": {prepared_patch.artifact_id: prepared_patch}},
            child_payload_references={},
        ),
        expected_parent_ids_by_role=expected_typed_run_parent_ids(
            run=run,
            policy=policy,
            rule=preview,
        ),
    )
    assert (
        tuple(parent.artifact_id for parent in projected.parents_by_role["planning_material"])
        == params.source_artifact_ids
    )
