"""Task 9 semantic payload/meta authority and schema-owned byte decoding."""

from __future__ import annotations

import pytest

from gameforge.contracts.canonical import canonical_json, compute_snapshot_id, sha256_lowerhex
from gameforge.contracts.config_export import (
    ConfigExportFileV1,
    ConfigExportPackageV1,
    canonical_config_export_bytes,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    CheckerRunPayloadV1,
    ConstraintProposalProposePayloadV1,
    ConstraintValidationPayloadV1,
    GenerationProposePayloadV1,
    GraphSelectionV1,
    PatchRepairPayloadV1,
    PatchValidationPayloadV1,
    PromptGoalBindingV1,
    RefReadBindingV1,
    RefValue,
    RequirementDispositionV1,
    ResolvedArtifactRequirementV1,
    ReviewRunPayloadV1,
    RollbackValidationPayloadV1,
    SimulationRunPayloadV1,
    ValidationSubjectBindingV1,
)
from gameforge.contracts.lineage import (
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_key_for_sha256,
)
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.workflow import (
    ConstraintProposalV1,
    ConstraintSourceBinding,
    FindingEvidenceBindingV1,
)
from gameforge.platform.publication.lineage import ParentInfo, TypedLineage
from gameforge.platform.publication.payload_binding import (
    FinalSiblingFact,
    bind_final_payload_references,
    final_sibling_fact_for,
    validate_domain_payload_binding_registry,
    validate_domain_payload_bindings,
)
from gameforge.platform.publication.payload_schema import (
    decode_and_validate_artifact_payload,
    encode_validated_artifact_payload,
    validate_artifact_payload,
)
from gameforge.platform.registry.defaults import build_builtin_registry
from tests.platform.m4c.handler_support import (
    build_envelope,
    build_run_record,
    resolved_policy_snapshot,
)


_HEX = "a" * 64


def _outcome_binding(kind: str, policy_id: str, rule_id: str):
    registry = build_builtin_registry()
    definition = registry.get_run_kind(RunKindRef(kind=kind, version=1))
    assert definition is not None
    policy = next(item for item in definition.outcome_policies if item.policy_id == policy_id)
    rule = next(item for item in policy.artifact_rules if item.rule_id == rule_id)
    return policy, rule


def _parent(
    artifact_id: str,
    kind: str,
    schema: str,
    version_tuple: VersionTuple | None = None,
    payload_hash: str | None = None,
) -> ParentInfo:
    return ParentInfo(
        artifact_id=artifact_id,
        kind=kind,
        payload_schema_id=schema,
        version_tuple=version_tuple or VersionTuple(),
        payload_hash=payload_hash,
    )


def test_constraint_proposal_source_hash_must_match_exact_typed_parent() -> None:
    source_id = "artifact:design-source"
    params = ConstraintProposalProposePayloadV1(
        source_artifact_ids=(source_id,),
        domain_scope=DomainScope(domain_ids=("content",)),
        authoring_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal",
            expected_payload_hash=_HEX,
        ),
        dsl_grammar_version="dsl@1",
        extraction_policy=ProfileRefV1(profile_id="extract", version=1),
    )
    run = build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="constraint_proposal.propose", version=1),
    )
    policy, rule = _outcome_binding(
        "constraint_proposal.propose",
        "constraint-proposal-drafted",
        "primary",
    )
    proposal = ConstraintProposalV1(
        revision=1,
        base_constraint_snapshot_id=None,
        dsl_grammar_version="dsl@1",
        domain_scope=params.domain_scope,
        constraints=(),
        source_bindings=(
            ConstraintSourceBinding(
                source_artifact_id=source_id,
                provenance_hash="b" * 64,
            ),
        ),
        produced_by="agent",
        producer_run_id=run.run_id,
        rationale="fixture",
    )
    typed = TypedLineage(
        parents_by_role={
            "source": (
                _parent(
                    source_id,
                    "design_source",
                    "design-source@1",
                    payload_hash="c" * 64,
                ),
                _parent(
                    params.authoring_goal.source_artifact_id,
                    "source_raw",
                    "source-raw@1",
                    payload_hash=params.authoring_goal.expected_payload_hash,
                ),
            ),
            "base_constraint": (),
        }
    )

    with pytest.raises(IntegrityViolation, match="semantic binding"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="constraint-proposal@1",
            canonical_payload=proposal.model_dump(mode="json"),
            typed_lineage=typed,
            projected_tuple=VersionTuple(),
            prepared_meta={"payload_schema_id": "constraint-proposal@1"},
        )


def _generation_run(*, with_export: bool = False):
    params = GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:base",
        constraint_snapshot_artifact_id=("artifact:constraint" if with_export else None),
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal", expected_payload_hash=_HEX
        ),
        domain_scope=DomainScope(domain_ids=("content",)),
        target=RefReadBindingV1(ref_name="ref:content"),
        generation_policy=ProfileRefV1(profile_id="generation", version=1),
        candidate_export_profiles=(
            (ProfileRefV1(profile_id="csv", version=1),) if with_export else ()
        ),
    )
    return build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="generation.propose", version=1),
    )


def _repair_run():
    params = PatchRepairPayloadV1(
        subject_patch_artifact_id="artifact:subject-patch",
        expected_subject_head_revision=1,
        expected_workflow_revision=1,
        base_snapshot_artifact_id="artifact:base",
        preview_snapshot_artifact_id="artifact:old-preview",
        constraint_snapshot_artifact_id=None,
        validation_evidence_artifact_id="artifact:validation",
        findings=(),
        target=RefReadBindingV1(ref_name="ref:content"),
        repair_policy=ProfileRefV1(profile_id="repair", version=1),
        checker_profiles=(),
        simulation_profiles=(),
        regression_suite_artifact_ids=("artifact:suite",),
        candidate_export_profiles=(),
    )
    return build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="patch.repair", version=1),
    )


def _validation_subject(subject_artifact_id: str) -> ValidationSubjectBindingV1:
    return ValidationSubjectBindingV1(
        approval_id="approval:1",
        expected_workflow_revision=2,
        subject_head_revision=1,
        subject_artifact_id=subject_artifact_id,
        subject_digest=_HEX,
        active_validation_run_id="run:1",
    )


def _patch_validation_run(*, overlapping_finding_evidence: bool = False):
    findings = (
        (
            FindingEvidenceBindingV1(
                finding_id="finding:1",
                finding_revision=1,
                evidence_artifact_id="artifact:review",
                finding_digest=_HEX,
            ),
        )
        if overlapping_finding_evidence
        else ()
    )
    params = PatchValidationPayloadV1(
        subject=_validation_subject("artifact:patch"),
        base_snapshot_artifact_id="artifact:base",
        preview_snapshot_artifact_id="artifact:preview",
        candidate_config_export_artifact_ids=("artifact:config",),
        target=RefReadBindingV1(
            ref_name="ref:content",
            expected_ref=RefValue(artifact_id="artifact:base", revision=1),
        ),
        validation_policy=ProfileRefV1(profile_id="validation", version=1),
        checker_profiles=(ProfileRefV1(profile_id="checker", version=1),),
        simulation_profiles=(),
        findings=findings,
        review_artifact_ids=("artifact:review",),
        playtest_trace_artifact_ids=("artifact:trace",),
        regression_suite_artifact_ids=("artifact:suite",),
    )
    return build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="patch.validate", version=1),
    )


def _constraint_validation_run():
    params = ConstraintValidationPayloadV1(
        subject=_validation_subject("artifact:proposal"),
        base_constraint_snapshot_artifact_id="artifact:base-constraint",
        target=RefReadBindingV1(
            ref_name="ref:constraints",
            expected_ref=RefValue(artifact_id="artifact:base-constraint", revision=1),
        ),
        dsl_grammar_version="dsl@1",
        compiler_profile=ProfileRefV1(profile_id="compiler", version=1),
        differential_engines=(
            {"engine_id": "clingo", "version": 1},
            {"engine_id": "z3", "version": 1},
        ),
        golden_suite_artifact_id="artifact:golden",
        regression_suite_artifact_ids=("artifact:suite",),
        validation_policy=ProfileRefV1(profile_id="validation", version=1),
    )
    return build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="constraint_proposal.validate", version=1),
    )


def _rollback_validation_run():
    params = RollbackValidationPayloadV1(
        subject=_validation_subject("artifact:rollback"),
        ref_name="ref:content",
        expected_current_ref=RefValue(artifact_id="artifact:current", revision=4),
        target_artifact_id="artifact:target",
        target_history_revision=2,
        rollback_profile=ProfileRefV1(profile_id="rollback", version=1),
        schema_compatibility_policy=ProfileRefV1(profile_id="schema", version=1),
        impact_profiles=(ProfileRefV1(profile_id="impact", version=1),),
        regression_suite_artifact_ids=("artifact:suite",),
    )
    return build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="rollback.validate", version=1),
    )


def test_semantic_binding_registry_closes_every_active_schema_valid_selector() -> None:
    assert validate_domain_payload_binding_registry(build_builtin_registry()) == 61


def test_checker_report_cannot_claim_a_different_snapshot_than_typed_lineage() -> None:
    params = CheckerRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        constraint_snapshot_artifact_id=None,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=ProfileRefV1(profile_id="checker", version=1),
        checker_ids=("graph",),
        defect_classes=(),
    )
    run = build_run_record(build_envelope(params=params), RunKindRef(kind="checker.run", version=1))
    policy, rule = _outcome_binding("checker.run", "checker-completed", "primary")
    typed = TypedLineage(
        parents_by_role={
            "snapshot": (
                _parent(
                    "artifact:snapshot",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:real"),
                ),
            ),
            "constraint": (),
        }
    )

    with pytest.raises(IntegrityViolation, match="semantic binding"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="checker-report@1",
            canonical_payload={
                "payload_schema_version": "checker-report@1",
                "snapshot_id": "sha256:forged",
                "checker_ids": ["graph"],
                "defect_classes": [],
                "constraint_application": [],
                "findings": [],
            },
            typed_lineage=typed,
            projected_tuple=VersionTuple(ir_snapshot_id="sha256:real", tool_version="checker@1"),
            prepared_meta={"payload_schema_id": "checker-report@1"},
        )


def test_unknown_prepared_metadata_is_rejected_even_without_authority_named_tokens() -> None:
    params = CheckerRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        constraint_snapshot_artifact_id=None,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=ProfileRefV1(profile_id="checker", version=1),
        checker_ids=("graph",),
        defect_classes=(),
    )
    run = build_run_record(build_envelope(params=params), RunKindRef(kind="checker.run", version=1))
    policy, rule = _outcome_binding("checker.run", "checker-completed", "primary")
    typed = TypedLineage(
        parents_by_role={
            "snapshot": (
                _parent(
                    "artifact:snapshot",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:real"),
                ),
            ),
            "constraint": (),
        }
    )

    with pytest.raises(IntegrityViolation, match="unknown key"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="checker-report@1",
            canonical_payload={
                "payload_schema_version": "checker-report@1",
                "snapshot_id": "sha256:real",
                "checker_ids": ["graph"],
                "defect_classes": [],
                "constraint_application": [],
                "findings": [],
            },
            typed_lineage=typed,
            projected_tuple=VersionTuple(ir_snapshot_id="sha256:real", tool_version="checker@1"),
            prepared_meta={
                "payload_schema_id": "checker-report@1",
                "certified": True,
            },
        )


def test_checker_run_requires_constraint_application_even_without_constraints() -> None:
    params = CheckerRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=ProfileRefV1(profile_id="checker", version=1),
        checker_ids=("graph",),
        defect_classes=(),
    )
    run = build_run_record(build_envelope(params=params), RunKindRef(kind="checker.run", version=1))
    policy, rule = _outcome_binding("checker.run", "checker-completed", "primary")
    typed = TypedLineage(
        parents_by_role={
            "snapshot": (
                _parent(
                    "artifact:snapshot",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:real"),
                ),
            ),
            "constraint": (),
        }
    )

    with pytest.raises(IntegrityViolation, match="authoritative semantic field"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="checker-report@1",
            canonical_payload={
                "payload_schema_version": "checker-report@1",
                "snapshot_id": "sha256:real",
                "checker_ids": ["graph"],
                "defect_classes": [],
                "findings": [],
            },
            typed_lineage=typed,
            projected_tuple=VersionTuple(ir_snapshot_id="sha256:real", tool_version="checker@1"),
            prepared_meta={"payload_schema_id": "checker-report@1"},
        )


def test_checker_constraint_application_ids_are_derived_from_exact_parent_payload() -> None:
    params = CheckerRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        constraint_snapshot_artifact_id="artifact:constraint",
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=ProfileRefV1(profile_id="checker", version=1),
        checker_ids=(),
        defect_classes=(),
    )
    run = build_run_record(build_envelope(params=params), RunKindRef(kind="checker.run", version=1))
    policy, rule = _outcome_binding("checker.run", "checker-completed", "primary")
    typed = TypedLineage(
        parents_by_role={
            "snapshot": (
                _parent(
                    "artifact:snapshot",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:real"),
                ),
            ),
            "constraint": (
                _parent("artifact:constraint", "constraint_snapshot", "constraint-snapshot@1"),
            ),
        }
    )

    with pytest.raises(IntegrityViolation, match="authoritative semantic binding"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="checker-report@1",
            canonical_payload={
                "payload_schema_version": "checker-report@1",
                "snapshot_id": "sha256:real",
                "checker_ids": [],
                "defect_classes": [],
                "constraint_application": [
                    {
                        "constraint_id": "constraint:forged",
                        "checker_id": "graph",
                        "status": "executed",
                    }
                ],
                "findings": [],
            },
            typed_lineage=typed,
            projected_tuple=VersionTuple(ir_snapshot_id="sha256:real", tool_version="checker@1"),
            prepared_meta={"payload_schema_id": "checker-report@1"},
            authoritative_parent_payloads={
                "artifact:constraint": {
                    "dsl_grammar_version": "dsl@1",
                    "constraints": [
                        {
                            "id": "constraint:real",
                            "dsl_grammar_version": "dsl@1",
                            "kind": "structural",
                            "oracle": "deterministic",
                            "predicates": [],
                            "assert": "true",
                            "severity": "major",
                        }
                    ],
                }
            },
        )


def test_standalone_simulation_rejects_budget_only_payload_without_execution_binding() -> None:
    params = SimulationRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        simulation_profile=ProfileRefV1(profile_id="simulation", version=1),
        workload_profile=ProfileRefV1(profile_id="workload", version=1),
        replication_count=2,
        horizon_steps=4,
    )
    run = build_run_record(
        build_envelope(params=params, seed=7),
        RunKindRef(kind="simulation.run", version=1),
    )
    policy, rule = _outcome_binding("simulation.run", "simulation-completed", "primary")
    typed = TypedLineage(
        parents_by_role={
            "snapshot": (
                _parent(
                    "artifact:snapshot",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:real"),
                ),
            ),
            "constraint": (),
            "scenario": (),
        }
    )

    with pytest.raises(IntegrityViolation, match="authoritative semantic field"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="simulation-result@1",
            canonical_payload={
                "payload_schema_version": "simulation-result@1",
                "snapshot_id": "sha256:real",
                "seed": 7,
                "replication_count": 2,
                "horizon_steps": 4,
                "findings": [],
            },
            typed_lineage=typed,
            projected_tuple=VersionTuple(
                ir_snapshot_id="sha256:real",
                seed=7,
                tool_version="economy-sim@1",
            ),
            prepared_meta={"payload_schema_id": "simulation-result@1"},
        )


@pytest.mark.parametrize(
    ("field", "forged_value"),
    (
        ("constraint_snapshot_artifact_id", "artifact:foreign-constraint"),
        ("scenario_artifact_id", "artifact:foreign-scenario"),
    ),
)
def test_standalone_simulation_binds_exact_semantic_input_artifact_ids(
    field: str,
    forged_value: str,
) -> None:
    params = SimulationRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        constraint_snapshot_artifact_id="artifact:constraint",
        scenario_artifact_id="artifact:scenario",
        simulation_profile=ProfileRefV1(profile_id="simulation", version=1),
        workload_profile=ProfileRefV1(profile_id="workload", version=1),
        replication_count=2,
        horizon_steps=4,
    )
    run = build_run_record(
        build_envelope(params=params, seed=7),
        RunKindRef(kind="simulation.run", version=1),
    )
    policy, rule = _outcome_binding("simulation.run", "simulation-completed", "primary")
    typed = TypedLineage(
        parents_by_role={
            "snapshot": (
                _parent(
                    "artifact:snapshot",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:real"),
                ),
            ),
            "constraint": (
                _parent("artifact:constraint", "constraint_snapshot", "constraint-snapshot@1"),
            ),
            "scenario": (_parent("artifact:scenario", "scenario_spec", "scenario-spec@1"),),
        }
    )
    execution_binding = {
        "simulation_profile": {"profile_id": "simulation", "version": 1},
        "workload_profile": {"profile_id": "workload", "version": 1},
        "constraint_snapshot_artifact_id": "artifact:constraint",
        "scenario_artifact_id": "artifact:scenario",
        "constraint_ids": ["constraint:real"],
        "scenario_id": "scenario:real",
        "constraint_application": {
            "status": "unproven",
            "reason_code": "constraint_profile_not_executable",
        },
        "scenario_application": {
            "status": "unproven",
            "reason_code": "scenario_reset_not_executable",
        },
    }
    execution_binding[field] = forged_value

    with pytest.raises(IntegrityViolation, match="authoritative semantic binding"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="simulation-result@1",
            canonical_payload={
                "payload_schema_version": "simulation-result@1",
                "snapshot_id": "sha256:real",
                "seed": 7,
                "replication_count": 2,
                "horizon_steps": 4,
                "invariants": [],
                "sensitivity": {"execution_binding": execution_binding},
                "findings": [],
            },
            typed_lineage=typed,
            projected_tuple=VersionTuple(
                ir_snapshot_id="sha256:real",
                seed=7,
                tool_version="economy-sim@1",
            ),
            prepared_meta={"payload_schema_id": "simulation-result@1"},
        )


@pytest.mark.parametrize(
    ("field", "forged_value"),
    (
        ("constraint_ids", ["constraint:forged"]),
        ("scenario_id", "scenario:forged"),
    ),
)
def test_standalone_simulation_semantic_ids_are_derived_from_exact_parent_payloads(
    field: str,
    forged_value: object,
) -> None:
    params = SimulationRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        constraint_snapshot_artifact_id="artifact:constraint",
        scenario_artifact_id="artifact:scenario",
        simulation_profile=ProfileRefV1(profile_id="simulation", version=1),
        workload_profile=ProfileRefV1(profile_id="workload", version=1),
        replication_count=2,
        horizon_steps=4,
    )
    run = build_run_record(
        build_envelope(params=params, seed=7),
        RunKindRef(kind="simulation.run", version=1),
    )
    policy, rule = _outcome_binding("simulation.run", "simulation-completed", "primary")
    typed = TypedLineage(
        parents_by_role={
            "snapshot": (
                _parent(
                    "artifact:snapshot",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:real"),
                ),
            ),
            "constraint": (
                _parent("artifact:constraint", "constraint_snapshot", "constraint-snapshot@1"),
            ),
            "scenario": (_parent("artifact:scenario", "scenario_spec", "scenario-spec@1"),),
        }
    )
    execution_binding = {
        "simulation_profile": {"profile_id": "simulation", "version": 1},
        "workload_profile": {"profile_id": "workload", "version": 1},
        "constraint_snapshot_artifact_id": "artifact:constraint",
        "scenario_artifact_id": "artifact:scenario",
        "constraint_ids": ["constraint:real"],
        "scenario_id": "scenario:real",
        "constraint_application": {
            "status": "unproven",
            "reason_code": "constraint_profile_not_executable",
        },
        "scenario_application": {
            "status": "unproven",
            "reason_code": "scenario_reset_not_executable",
        },
    }
    execution_binding[field] = forged_value

    with pytest.raises(IntegrityViolation, match="authoritative semantic binding"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="simulation-result@1",
            canonical_payload={
                "payload_schema_version": "simulation-result@1",
                "snapshot_id": "sha256:real",
                "seed": 7,
                "replication_count": 2,
                "horizon_steps": 4,
                "invariants": [],
                "sensitivity": {"execution_binding": execution_binding},
                "findings": [],
            },
            typed_lineage=typed,
            projected_tuple=VersionTuple(
                ir_snapshot_id="sha256:real",
                env_contract_version="env@1",
                seed=7,
                tool_version="economy-sim@1",
            ),
            prepared_meta={"payload_schema_id": "simulation-result@1"},
            authoritative_parent_payloads={
                "artifact:constraint": {
                    "dsl_grammar_version": "dsl@1",
                    "constraints": [{"id": "constraint:real"}],
                },
                "artifact:scenario": {
                    "scenario_id": "scenario:real",
                    "source_preview_artifact_id": "artifact:snapshot",
                    "constraint_snapshot_artifact_id": "artifact:constraint",
                    "env_contract_version": "env@1",
                },
            },
        )


def test_review_simulation_constraint_application_is_bound_to_exact_parent_payload() -> None:
    params = ReviewRunPayloadV1(
        snapshot_artifact_id="artifact:snapshot",
        constraint_snapshot_artifact_id="artifact:constraint",
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        review_profile=ProfileRefV1(profile_id="review", version=1),
        checker_profiles=(ProfileRefV1(profile_id="checker", version=1),),
        simulation_profiles=(ProfileRefV1(profile_id="simulation", version=1),),
    )
    run = build_run_record(
        build_envelope(params=params, seed=7),
        RunKindRef(kind="review.run", version=1),
    )
    policy, rule = _outcome_binding("review.run", "review-completed", "simulation")
    typed = TypedLineage(
        parents_by_role={
            "snapshot": (
                _parent(
                    "artifact:snapshot",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:real"),
                ),
            ),
            "constraint": (
                _parent("artifact:constraint", "constraint_snapshot", "constraint-snapshot@1"),
            ),
        }
    )
    canonical_payload = {
        "payload_schema_version": "simulation-result@1",
        "profile": {"profile_id": "simulation", "version": 1},
        "snapshot_id": "sha256:real",
        "seed": 7,
        "replication_count": 2,
        "horizon_steps": 4,
        "invariants": [],
        "sensitivity": {
            "execution_binding": {
                "simulation_profile": {"profile_id": "simulation", "version": 1},
                "constraint_snapshot_artifact_id": "artifact:constraint",
                "constraint_ids": ["constraint:real"],
                "constraint_application": {
                    "status": "unproven",
                    "reason_code": "constraint_profile_not_executable",
                },
            }
        },
        "findings": [],
    }
    kwargs = {
        "run": run,
        "outcome_policy": policy,
        "outcome_rule": rule,
        "payload_schema_id": "simulation-result@1",
        "typed_lineage": typed,
        "projected_tuple": VersionTuple(
            ir_snapshot_id="sha256:real",
            constraint_snapshot_id="constraint:semantic:1",
            seed=7,
            tool_version="economy-sim@1",
        ),
        "prepared_meta": {"payload_schema_id": "simulation-result@1"},
        "authoritative_parent_payloads": {
            "artifact:constraint": {
                "dsl_grammar_version": "dsl@1",
                "constraints": [{"id": "constraint:real"}],
            },
        },
    }

    validate_domain_payload_bindings(canonical_payload=canonical_payload, **kwargs)

    forged = {
        **canonical_payload,
        "sensitivity": {
            "execution_binding": {
                **canonical_payload["sensitivity"]["execution_binding"],
                "constraint_ids": ["constraint:forged"],
            }
        },
    }
    with pytest.raises(IntegrityViolation, match="authoritative semantic binding"):
        validate_domain_payload_bindings(canonical_payload=forged, **kwargs)


def test_patch_base_is_bound_to_projected_parent_and_target_to_preview_content() -> None:
    run = _generation_run()
    policy, rule = _outcome_binding("generation.propose", "generation-gate-pass", "primary")
    typed = TypedLineage(
        parents_by_role={
            "snapshot": (
                _parent(
                    "artifact:base",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:base"),
                ),
            ),
            "constraint": (),
            "goal": (_parent("artifact:goal", "source_raw", "source-raw@1"),),
            "rendered_prompt": (),
            "supporting_evidence": (),
        }
    )
    preview = {
        "meta_schema_version": "ir@1",
        "entities": {"npc:a": {"type": "NPC"}},
        "relations": {},
    }
    patch = PatchV2(
        revision=1,
        base_snapshot_id="sha256:forged-base",
        target_snapshot_id=compute_snapshot_id(preview),
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[],
        produced_by="agent",
        producer_run_id=run.run_id,
        rationale="test",
    )

    with pytest.raises(IntegrityViolation, match="semantic binding"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="patch@2",
            canonical_payload=patch.model_dump(mode="json"),
            typed_lineage=typed,
            projected_tuple=VersionTuple(ir_snapshot_id="sha256:base", tool_version="generation@1"),
            prepared_meta={"payload_schema_id": "patch@2"},
            related_payloads_by_rule={"preview": (preview,)},
        )


@pytest.mark.parametrize(
    ("policy_id", "preview_artifact_id", "snapshot_id"),
    (
        ("repair-verified", "artifact:new-preview", "sha256:new-preview"),
        ("repair-unverified", "artifact:old-preview", "sha256:old-preview"),
    ),
)
@pytest.mark.parametrize(
    ("rule_id", "payload_schema_id"),
    (
        ("checker", "checker-report@1"),
        ("simulation", "simulation-result@1"),
        ("regression", "regression-evidence@1"),
    ),
)
def test_repair_evidence_preview_authority_follows_the_selected_policy(
    policy_id: str,
    preview_artifact_id: str,
    snapshot_id: str,
    rule_id: str,
    payload_schema_id: str,
) -> None:
    run = _repair_run()
    policy, rule = _outcome_binding("patch.repair", policy_id, rule_id)
    typed = TypedLineage(
        parents_by_role={
            "preview": (
                _parent(
                    preview_artifact_id,
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id=snapshot_id),
                ),
            ),
            "constraint": (),
        }
    )
    canonical_payload = (
        {
            "payload_schema_version": payload_schema_id,
            "suite_artifact_id": "artifact:suite",
            "snapshot_id": snapshot_id,
            "status": "passed",
        }
        if payload_schema_id == "regression-evidence@1"
        else {
            "payload_schema_version": payload_schema_id,
            "snapshot_id": snapshot_id,
            "findings": [],
        }
    )
    canonical_payload = validate_artifact_payload(
        payload_schema_id=payload_schema_id,
        payload=canonical_payload,
    )

    meta = validate_domain_payload_bindings(
        run=run,
        outcome_policy=policy,
        outcome_rule=rule,
        payload_schema_id=payload_schema_id,
        canonical_payload=canonical_payload,
        typed_lineage=typed,
        projected_tuple=VersionTuple(
            ir_snapshot_id=snapshot_id,
            tool_version="repair-verifier@1",
        ),
        prepared_meta={"payload_schema_id": payload_schema_id},
    )
    assert meta == {"payload_schema_id": payload_schema_id}

    with pytest.raises(IntegrityViolation, match="no Run authority"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id=payload_schema_id,
            canonical_payload={
                **canonical_payload,
                "domain_scope": {"domain_ids": ["worker-self-reported"]},
            },
            typed_lineage=typed,
            projected_tuple=VersionTuple(
                ir_snapshot_id=snapshot_id,
                tool_version="repair-verifier@1",
            ),
            prepared_meta={
                "payload_schema_id": payload_schema_id,
                "domain_scope": {"domain_ids": ["worker-self-reported"]},
            },
        )


def test_config_export_meta_profile_cannot_disagree_with_typed_payload() -> None:
    run = _generation_run(with_export=True)
    policy, rule = _outcome_binding("generation.propose", "generation-gate-pass", "config-export")
    typed = TypedLineage(
        parents_by_role={
            "preview": (
                _parent(
                    "artifact:preview",
                    "ir_snapshot",
                    "ir-core@1",
                    VersionTuple(ir_snapshot_id="sha256:preview"),
                ),
            ),
            "constraint": (
                _parent(
                    "artifact:constraint",
                    "constraint_snapshot",
                    "constraint-snapshot@1",
                    VersionTuple(constraint_snapshot_id="constraint:1"),
                ),
            ),
        }
    )
    payload = {
        "package_schema_version": "config-export-package@1",
        "export_profile": {"profile_id": "csv", "version": 1},
        "target_environment_profile": {"profile_id": "environment", "version": 1},
        "env_contract_version": "env@1",
        "source_preview_artifact_id": "artifact:preview",
        "constraint_snapshot_artifact_id": "artifact:constraint",
        "format_schema_id": "csv@1",
        "files": [],
    }

    valid_meta = {
        "payload_schema_id": "config-export-package@1",
        "export_profile": {"profile_id": "csv", "version": 1},
    }
    checked = validate_domain_payload_bindings(
        run=run,
        outcome_policy=policy,
        outcome_rule=rule,
        payload_schema_id="config-export-package@1",
        canonical_payload=payload,
        typed_lineage=typed,
        projected_tuple=VersionTuple(
            ir_snapshot_id="sha256:preview",
            constraint_snapshot_id="constraint:1",
            tool_version="config-export@1",
            env_contract_version="env@1",
        ),
        prepared_meta=valid_meta,
    )
    assert checked["domain_scope"] == {"domain_ids": ["content"]}
    final_artifact = build_artifact_v2(
        kind="config_export",
        version_tuple=VersionTuple(
            ir_snapshot_id="sha256:preview",
            constraint_snapshot_id="constraint:1",
            tool_version="config-export@1",
            env_contract_version="env@1",
        ),
        lineage=("artifact:preview", "artifact:constraint"),
        payload_hash=_HEX,
        object_ref=ObjectRef(key=object_key_for_sha256(_HEX), sha256=_HEX, size_bytes=0),
        meta=checked,
    )
    assert final_artifact.meta["domain_scope"] == {"domain_ids": ["content"]}

    with pytest.raises(IntegrityViolation, match="semantic binding"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="config-export-package@1",
            canonical_payload=payload,
            typed_lineage=typed,
            projected_tuple=VersionTuple(
                ir_snapshot_id="sha256:preview",
                constraint_snapshot_id="constraint:1",
                tool_version="config-export@1",
                env_contract_version="env@1",
            ),
            prepared_meta={
                **valid_meta,
                "domain_scope": {"domain_ids": ["forged"]},
            },
        )

    with pytest.raises(IntegrityViolation, match="semantic binding"):
        validate_domain_payload_bindings(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="config-export-package@1",
            canonical_payload=payload,
            typed_lineage=typed,
            projected_tuple=VersionTuple(
                ir_snapshot_id="sha256:preview",
                constraint_snapshot_id="constraint:1",
                tool_version="config-export@1",
                env_contract_version="env@1",
            ),
            prepared_meta={
                "payload_schema_id": "config-export-package@1",
                "export_profile": {"profile_id": "other", "version": 1},
            },
        )


def test_schema_owned_decoder_accepts_canonical_config_binary_framing() -> None:
    content = b"id,value\nnpc,1\n"
    package = ConfigExportPackageV1(
        export_profile=ProfileRefV1(profile_id="csv", version=1),
        target_environment_profile=ProfileRefV1(profile_id="environment", version=1),
        env_contract_version="env@1",
        source_preview_artifact_id="artifact:preview",
        constraint_snapshot_artifact_id="artifact:constraint",
        format_schema_id="csv-files@1",
        files=(
            ConfigExportFileV1(
                relative_path="data/items.csv",
                media_type="text/csv",
                content_sha256=sha256_lowerhex(content),
                size_bytes=len(content),
                content_bytes=content,
            ),
        ),
    )

    decoded = decode_and_validate_artifact_payload(
        payload_schema_id="config-export-package@1",
        blob=canonical_config_export_bytes(package),
    )
    assert decoded["export_profile"] == {"profile_id": "csv", "version": 1}
    assert decoded["source_preview_artifact_id"] == "artifact:preview"
    assert encode_validated_artifact_payload(
        payload_schema_id="config-export-package@1", payload=decoded
    ) == canonical_config_export_bytes(package)


def test_config_export_reseal_binds_only_verified_logical_preview_to_final_id() -> None:
    run = _generation_run(with_export=True)
    policy, rule = _outcome_binding("generation.propose", "generation-gate-pass", "config-export")
    payload = {
        "package_schema_version": "config-export-package@1",
        "export_profile": {"profile_id": "csv", "version": 1},
        "target_environment_profile": {"profile_id": "environment", "version": 1},
        "env_contract_version": "env@1",
        "source_preview_artifact_id": "sha256:preview",
        "constraint_snapshot_artifact_id": "artifact:constraint",
        "format_schema_id": "csv-files@1",
        "files": [],
    }

    bound = bind_final_payload_references(
        run=run,
        outcome_policy=policy,
        outcome_rule=rule,
        payload_schema_id="config-export-package@1",
        canonical_payload=payload,
        projected_tuple=VersionTuple(ir_snapshot_id="sha256:preview"),
        final_artifact_ids_by_rule={"preview": ("artifact:final-preview",)},
        final_sibling_facts_by_id={},
    )
    assert bound["source_preview_artifact_id"] == "artifact:final-preview"

    with pytest.raises(IntegrityViolation, match="logical snapshot"):
        bind_final_payload_references(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="config-export-package@1",
            canonical_payload={**payload, "source_preview_artifact_id": "artifact:forged"},
            projected_tuple=VersionTuple(ir_snapshot_id="sha256:preview"),
            final_artifact_ids_by_rule={"preview": ("artifact:final-preview",)},
            final_sibling_facts_by_id={},
        )


@pytest.mark.parametrize(
    "blob",
    (
        b'{"payload_schema_version":"checker-report@1","snapshot_id":"s","findings":[],"snapshot_id":"forged"}',
        b'{"payload_schema_version":"checker-report@1","snapshot_id":NaN,"findings":[]}',
    ),
)
def test_schema_owned_json_decoder_rejects_duplicate_keys_and_nan(blob: bytes) -> None:
    with pytest.raises(IntegrityViolation, match="strict UTF-8 JSON"):
        decode_and_validate_artifact_payload(payload_schema_id="checker-report@1", blob=blob)


def test_schema_owned_decoder_enforces_raw_bound_before_decode(monkeypatch) -> None:
    from gameforge.platform.publication import payload_schema

    monkeypatch.setattr(payload_schema, "MAX_PREPARED_ARTIFACT_BYTES", 8)
    blob = canonical_json(
        {
            "payload_schema_version": "checker-report@1",
            "snapshot_id": "snapshot",
            "findings": [],
        }
    ).encode("utf-8")
    with pytest.raises(IntegrityViolation, match="byte bound"):
        decode_and_validate_artifact_payload(payload_schema_id="checker-report@1", blob=blob)


def test_deferred_schema_requires_an_explicit_trusted_decoder() -> None:
    blob = b"canonical-bench-wire"
    with pytest.raises(IntegrityViolation, match="not valid on the terminal"):
        decode_and_validate_artifact_payload(payload_schema_id="bench-report@2", blob=blob)

    def decode_bench(value: bytes) -> dict[str, object]:
        if value != blob:
            raise ValueError("non-canonical bench wire")
        return {"schema_version": "bench-report@2", "meta": {"seed": 7}}

    decoded = decode_and_validate_artifact_payload(
        payload_schema_id="bench-report@2",
        blob=blob,
        external_decoders={"bench-report@2": decode_bench},
    )
    assert decoded == {"schema_version": "bench-report@2", "meta": {"seed": 7}}


def _patch_evidence_payload(
    *,
    evidence_id: str,
    status: str,
    supporting: tuple[str, ...] | None = None,
    finding_bindings: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    return {
        "evidence_schema_version": "evidence-set@1",
        "subject_artifact_id": "artifact:patch",
        "subject_digest": _HEX,
        "policy_version": "validation@1",
        "validation_run_id": "run:1",
        "target_binding": {
            "binding_schema_version": "approval-target-binding@1",
            "subject_kind": "patch",
            "target_artifact_kind": "ir_snapshot",
            "target_artifact_id": "artifact:preview",
            "target_snapshot_id": "snapshot:preview",
            "target_digest": _HEX,
            "ref_name": "ref:content",
            "expected_ref": {"artifact_id": "artifact:base", "revision": 1},
        },
        "supporting_artifact_ids": list(
            supporting
            or (
                evidence_id,
                "artifact:config",
                "artifact:review",
                "artifact:trace",
                "artifact:suite",
            )
        ),
        "finding_bindings": list(finding_bindings),
        "requirements": [
            {
                "requirement_id": "checker:checker@1",
                "kind": "regression",
                "applicability": "required",
                "status": status,
                "evidence_artifact_id": evidence_id,
                "reason_code": None,
                "tool_version": "checker@1",
            }
        ],
        "overall_status": status,
    }


def _patch_regression_fact(
    run: object,
    rule: object,
    *,
    artifact_id: str,
    status: str,
) -> FinalSiblingFact:
    return final_sibling_fact_for(
        run=run,
        artifact_id=artifact_id,
        outcome_rule=rule,
        payload_schema_id="regression-evidence@1",
        canonical_payload={
            "payload_schema_version": "regression-evidence@1",
            "requirement_id": "checker:checker@1",
            "dimension": "checker",
            "snapshot_id": "snapshot:preview",
            "status": status,
            "findings": [],
        },
        payload_hash=_HEX,
        authoritative_meta={"requirement_id": "checker:checker@1"},
    )


def test_evidence_set_cannot_launder_failed_final_evidence_as_passed() -> None:
    run = _patch_validation_run()
    policy, rule = _outcome_binding("patch.validate", "patch-validation-passed", "primary")
    _regression_policy, regression_rule = _outcome_binding(
        "patch.validate", "patch-validation-passed", "regression"
    )
    evidence_id = "artifact:checker-evidence"
    fact = _patch_regression_fact(
        run,
        regression_rule,
        artifact_id=evidence_id,
        status="failed",
    )

    with pytest.raises(IntegrityViolation, match="disposition"):
        bind_final_payload_references(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="evidence-set@1",
            canonical_payload=_patch_evidence_payload(
                evidence_id=evidence_id,
                status="passed",
            ),
            projected_tuple=VersionTuple(ir_snapshot_id="snapshot:preview"),
            final_artifact_ids_by_rule={"regression": (evidence_id,)},
            final_sibling_facts_by_id={evidence_id: fact},
        )


@pytest.mark.parametrize(
    "requirement_update",
    (
        {"tool_version": "forged@1"},
        {"reason_code": "forged_reason"},
        {
            "applicability": "not_applicable",
            "status": "not_applicable",
            "evidence_artifact_id": None,
            "reason_code": "skipped",
        },
    ),
)
def test_evidence_set_requirement_reason_tool_and_applicability_are_final_facts(
    requirement_update: dict[str, object],
) -> None:
    run = _patch_validation_run()
    policy, rule = _outcome_binding("patch.validate", "patch-validation-passed", "primary")
    _regression_policy, regression_rule = _outcome_binding(
        "patch.validate", "patch-validation-passed", "regression"
    )
    evidence_id = "artifact:checker-evidence"
    fact = _patch_regression_fact(
        run,
        regression_rule,
        artifact_id=evidence_id,
        status="passed",
    )
    payload = _patch_evidence_payload(evidence_id=evidence_id, status="passed")
    requirement = {**payload["requirements"][0], **requirement_update}
    payload["requirements"] = [requirement]

    with pytest.raises(IntegrityViolation):
        bind_final_payload_references(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="evidence-set@1",
            canonical_payload=payload,
            projected_tuple=VersionTuple(ir_snapshot_id="snapshot:preview"),
            final_artifact_ids_by_rule={"regression": (evidence_id,)},
            final_sibling_facts_by_id={evidence_id: fact},
        )


def test_regression_suite_unproven_reason_is_bound_from_the_final_wire() -> None:
    run = _patch_validation_run()
    policy, rule = _outcome_binding("patch.validate", "patch-validation-passed", "primary")
    _regression_policy, regression_rule = _outcome_binding(
        "patch.validate", "patch-validation-passed", "regression"
    )
    evidence_id = "artifact:regression-evidence"
    exact_reason = "adapter_environment_unavailable"
    fact = final_sibling_fact_for(
        run=run,
        artifact_id=evidence_id,
        outcome_rule=regression_rule,
        payload_schema_id="regression-evidence@1",
        canonical_payload={
            "payload_schema_version": "regression-evidence@1",
            "suite_artifact_id": "artifact:suite",
            "snapshot_id": "snapshot:preview",
            "status": "unproven",
            "reason_code": exact_reason,
        },
        payload_hash=_HEX,
        authoritative_meta={"requirement_id": "regression:artifact:suite"},
    )
    payload = {
        **_patch_evidence_payload(evidence_id=evidence_id, status="unproven"),
        "requirements": [
            {
                "requirement_id": "regression:artifact:suite",
                "kind": "regression",
                "applicability": "required",
                "status": "unproven",
                "evidence_artifact_id": evidence_id,
                "reason_code": exact_reason,
                "tool_version": "regression@1",
            }
        ],
    }
    bound = bind_final_payload_references(
        run=run,
        outcome_policy=policy,
        outcome_rule=rule,
        payload_schema_id="evidence-set@1",
        canonical_payload=payload,
        projected_tuple=VersionTuple(ir_snapshot_id="snapshot:preview"),
        final_artifact_ids_by_rule={"regression": (evidence_id,)},
        final_sibling_facts_by_id={evidence_id: fact},
    )
    assert bound["requirements"][0]["reason_code"] == exact_reason

    forged = {
        **payload,
        "requirements": [{**payload["requirements"][0], "reason_code": "forged_reason"}],
    }
    with pytest.raises(IntegrityViolation, match="disposition"):
        bind_final_payload_references(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="evidence-set@1",
            canonical_payload=forged,
            projected_tuple=VersionTuple(ir_snapshot_id="snapshot:preview"),
            final_artifact_ids_by_rule={"regression": (evidence_id,)},
            final_sibling_facts_by_id={evidence_id: fact},
        )


@pytest.mark.parametrize(
    "supporting",
    (
        (
            "artifact:checker-evidence",
            "artifact:config",
            "artifact:trace",
            "artifact:suite",
        ),
        (
            "artifact:checker-evidence",
            "artifact:config",
            "artifact:review",
            "artifact:trace",
            "artifact:suite",
            "artifact:patch",
        ),
    ),
)
def test_patch_evidence_set_requires_exact_supporting_closure(
    supporting: tuple[str, ...],
) -> None:
    run = _patch_validation_run()
    policy, rule = _outcome_binding("patch.validate", "patch-validation-passed", "primary")
    _regression_policy, regression_rule = _outcome_binding(
        "patch.validate", "patch-validation-passed", "regression"
    )
    evidence_id = "artifact:checker-evidence"
    fact = _patch_regression_fact(
        run,
        regression_rule,
        artifact_id=evidence_id,
        status="passed",
    )

    with pytest.raises(IntegrityViolation, match="supporting"):
        bind_final_payload_references(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="evidence-set@1",
            canonical_payload=_patch_evidence_payload(
                evidence_id=evidence_id,
                status="passed",
                supporting=supporting,
            ),
            projected_tuple=VersionTuple(ir_snapshot_id="snapshot:preview"),
            final_artifact_ids_by_rule={"regression": (evidence_id,)},
            final_sibling_facts_by_id={evidence_id: fact},
        )


def test_patch_evidence_supporting_closure_allows_cross_field_overlap() -> None:
    run = _patch_validation_run(overlapping_finding_evidence=True)
    policy, rule = _outcome_binding("patch.validate", "patch-validation-passed", "primary")
    _regression_policy, regression_rule = _outcome_binding(
        "patch.validate", "patch-validation-passed", "regression"
    )
    evidence_id = "artifact:checker-evidence"
    fact = _patch_regression_fact(
        run,
        regression_rule,
        artifact_id=evidence_id,
        status="passed",
    )
    params = run.payload.params
    assert isinstance(params, PatchValidationPayloadV1)
    payload = _patch_evidence_payload(
        evidence_id=evidence_id,
        status="passed",
        finding_bindings=tuple(item.model_dump(mode="json") for item in params.findings),
    )

    bound = bind_final_payload_references(
        run=run,
        outcome_policy=policy,
        outcome_rule=rule,
        payload_schema_id="evidence-set@1",
        canonical_payload=payload,
        projected_tuple=VersionTuple(ir_snapshot_id="snapshot:preview"),
        final_artifact_ids_by_rule={"regression": (evidence_id,)},
        final_sibling_facts_by_id={evidence_id: fact},
    )

    assert bound["supporting_artifact_ids"].count("artifact:review") == 1


def test_patch_evidence_set_finding_bindings_are_an_exact_run_payload_copy() -> None:
    run = _patch_validation_run()
    policy, rule = _outcome_binding("patch.validate", "patch-validation-passed", "primary")
    _regression_policy, regression_rule = _outcome_binding(
        "patch.validate", "patch-validation-passed", "regression"
    )
    evidence_id = "artifact:checker-evidence"
    fact = _patch_regression_fact(
        run,
        regression_rule,
        artifact_id=evidence_id,
        status="passed",
    )

    with pytest.raises(IntegrityViolation, match="semantic binding"):
        bind_final_payload_references(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="evidence-set@1",
            canonical_payload=_patch_evidence_payload(
                evidence_id=evidence_id,
                status="passed",
                finding_bindings=(
                    {
                        "finding_id": "finding:forged",
                        "finding_revision": 1,
                        "evidence_artifact_id": "artifact:review",
                        "finding_digest": _HEX,
                    },
                ),
            ),
            projected_tuple=VersionTuple(ir_snapshot_id="snapshot:preview"),
            final_artifact_ids_by_rule={"regression": (evidence_id,)},
            final_sibling_facts_by_id={evidence_id: fact},
        )


def _constraint_evidence_closure() -> tuple[
    object,
    object,
    object,
    dict[str, tuple[str, ...]],
    dict[str, FinalSiblingFact],
    dict[str, object],
]:
    run = _constraint_validation_run()
    policy, rule = _outcome_binding(
        "constraint_proposal.validate",
        "constraint-validated-with-candidate",
        "primary",
    )
    _compile_policy, compile_rule = _outcome_binding(
        "constraint_proposal.validate",
        "constraint-validated-with-candidate",
        "compile-evidence",
    )
    _regression_policy, regression_rule = _outcome_binding(
        "constraint_proposal.validate",
        "constraint-validated-with-candidate",
        "regression",
    )
    candidate_id = "artifact:candidate"
    compile_id = "artifact:compile"
    regression_id = "artifact:regression"
    candidate_fact = FinalSiblingFact(
        artifact_id=candidate_id,
        outcome_rule_id="candidate",
        artifact_kind="constraint_snapshot",
        payload_schema_id="constraint-snapshot@1",
        payload_hash="b" * 64,
        requirement_id=None,
        requirement_kind=None,
    )
    compile_fact = final_sibling_fact_for(
        run=run,
        artifact_id=compile_id,
        outcome_rule=compile_rule,
        payload_schema_id="constraint-compile-evidence@1",
        canonical_payload={
            "compiler_profile": {"profile_id": "compiler", "version": 1},
            "overall_status": "passed",
        },
        payload_hash="c" * 64,
        authoritative_meta={},
    )
    regression_fact = final_sibling_fact_for(
        run=run,
        artifact_id=regression_id,
        outcome_rule=regression_rule,
        payload_schema_id="regression-evidence@1",
        canonical_payload={
            "payload_schema_version": "regression-evidence@1",
            "suite_artifact_id": "artifact:suite",
            "snapshot_id": None,
            "status": "passed",
        },
        payload_hash="d" * 64,
        authoritative_meta={"requirement_id": "regression:artifact:suite"},
    )
    rules = {
        "candidate": (candidate_id,),
        "compile-evidence": (compile_id,),
        "regression": (regression_id,),
    }
    facts = {
        candidate_id: candidate_fact,
        compile_id: compile_fact,
        regression_id: regression_fact,
    }
    payload: dict[str, object] = {
        "evidence_schema_version": "evidence-set@1",
        "subject_artifact_id": "artifact:proposal",
        "subject_digest": _HEX,
        "policy_version": "validation@1",
        "validation_run_id": "run:1",
        "target_binding": {
            "target_artifact_id": candidate_id,
            "target_digest": "b" * 64,
        },
        "supporting_artifact_ids": sorted(
            (
                candidate_id,
                compile_id,
                regression_id,
                "artifact:base-constraint",
                "artifact:suite",
                "artifact:golden",
            )
        ),
        "finding_bindings": [],
        "requirements": [
            {
                "requirement_id": "compile",
                "kind": "compile",
                "applicability": "required",
                "status": "passed",
                "evidence_artifact_id": compile_id,
                "reason_code": None,
                "tool_version": "compiler@1",
            },
            {
                "requirement_id": "regression:artifact:suite",
                "kind": "regression",
                "applicability": "required",
                "status": "passed",
                "evidence_artifact_id": regression_id,
                "reason_code": None,
                "tool_version": "regression@1",
            },
        ],
        "overall_status": "passed",
    }
    return run, policy, rule, rules, facts, payload


def test_constraint_evidence_set_requires_exact_support_and_empty_findings() -> None:
    run, policy, rule, rules, facts, payload = _constraint_evidence_closure()
    valid = bind_final_payload_references(
        run=run,
        outcome_policy=policy,
        outcome_rule=rule,
        payload_schema_id="evidence-set@1",
        canonical_payload=payload,
        projected_tuple=VersionTuple(constraint_snapshot_id="candidate:1"),
        final_artifact_ids_by_rule=rules,
        final_sibling_facts_by_id=facts,
    )
    assert valid["supporting_artifact_ids"] == payload["supporting_artifact_ids"]

    for forged in (
        {**payload, "supporting_artifact_ids": payload["supporting_artifact_ids"][1:]},
        {
            **payload,
            "finding_bindings": [
                {
                    "finding_id": "finding:forged",
                    "finding_revision": 1,
                    "evidence_artifact_id": "artifact:proposal",
                    "finding_digest": _HEX,
                }
            ],
        },
    ):
        with pytest.raises(IntegrityViolation):
            bind_final_payload_references(
                run=run,
                outcome_policy=policy,
                outcome_rule=rule,
                payload_schema_id="evidence-set@1",
                canonical_payload=forged,
                projected_tuple=VersionTuple(constraint_snapshot_id="candidate:1"),
                final_artifact_ids_by_rule=rules,
                final_sibling_facts_by_id=facts,
            )


def test_constraint_not_executed_requirement_binds_exact_prepared_disposition() -> None:
    base_run = _constraint_validation_run()
    requirement_id = "regression:artifact:suite"
    snapshot = resolved_policy_snapshot(
        "constraint-validation",
        "/params/validation_policy",
        (
            ResolvedArtifactRequirementV1(
                requirement_id=requirement_id,
                outcome_rule_id="regression",
                artifact_kind="regression_evidence",
                payload_schema_id="regression-evidence@1",
                ordinal=1,
            ),
        ),
    )
    run = build_run_record(
        build_envelope(
            params=base_run.payload.params,
            resolved_policy_snapshots=(snapshot,),
        ),
        RunKindRef(kind="constraint_proposal.validate", version=1),
    )
    policy, rule = _outcome_binding(
        "constraint_proposal.validate",
        "constraint-validation-failed-without-candidate",
        "primary",
    )
    _compile_policy, compile_rule = _outcome_binding(
        "constraint_proposal.validate",
        "constraint-validation-failed-without-candidate",
        "compile-evidence",
    )
    compile_id = "artifact:compile"
    compile_fact = final_sibling_fact_for(
        run=run,
        artifact_id=compile_id,
        outcome_rule=compile_rule,
        payload_schema_id="constraint-compile-evidence@1",
        canonical_payload={
            "compiler_profile": {"profile_id": "compiler", "version": 1},
            "overall_status": "failed",
        },
        payload_hash="c" * 64,
        authoritative_meta={},
    )
    exact_reason = "candidate_unavailable"
    disposition = RequirementDispositionV1(
        resolved_policy_id="constraint-validation",
        outcome_rule_id="regression",
        requirement_id=requirement_id,
        status="not_executed",
        reason_code=exact_reason,
    )
    regression_requirement = {
        "requirement_id": requirement_id,
        "kind": "regression",
        "applicability": "required",
        "status": "unproven",
        "evidence_artifact_id": None,
        "reason_code": exact_reason,
        "tool_version": "regression@1",
    }
    payload = {
        "evidence_schema_version": "evidence-set@1",
        "subject_artifact_id": "artifact:proposal",
        "subject_digest": _HEX,
        "policy_version": "validation@1",
        "validation_run_id": "run:1",
        "target_binding": None,
        "supporting_artifact_ids": sorted(
            (
                compile_id,
                "artifact:base-constraint",
                "artifact:suite",
                "artifact:golden",
            )
        ),
        "finding_bindings": [],
        "requirements": [
            {
                "requirement_id": "compile",
                "kind": "compile",
                "applicability": "required",
                "status": "failed",
                "evidence_artifact_id": compile_id,
                "reason_code": None,
                "tool_version": "compiler@1",
            },
            regression_requirement,
        ],
        "overall_status": "failed",
    }
    binding = {
        "run": run,
        "outcome_policy": policy,
        "outcome_rule": rule,
        "payload_schema_id": "evidence-set@1",
        "projected_tuple": VersionTuple(),
        "final_artifact_ids_by_rule": {"compile-evidence": (compile_id,)},
        "final_sibling_facts_by_id": {compile_id: compile_fact},
        "requirement_dispositions": (disposition,),
    }

    valid = bind_final_payload_references(canonical_payload=payload, **binding)
    assert valid["requirements"][1]["reason_code"] == exact_reason

    wrong_reason = {
        **payload,
        "requirements": [
            payload["requirements"][0],
            {**regression_requirement, "reason_code": "worker_chosen_reason"},
        ],
    }
    with pytest.raises(IntegrityViolation, match="semantic binding"):
        bind_final_payload_references(canonical_payload=wrong_reason, **binding)

    missing_not_executed = {**payload, "requirements": [payload["requirements"][0]]}
    with pytest.raises(IntegrityViolation, match="omits a prepared subset disposition"):
        bind_final_payload_references(canonical_payload=missing_not_executed, **binding)

    produced_without_sibling = RequirementDispositionV1(
        resolved_policy_id="constraint-validation",
        outcome_rule_id="regression",
        requirement_id=requirement_id,
        status="produced",
    )
    with pytest.raises(IntegrityViolation, match="no exact EvidenceSet sibling"):
        bind_final_payload_references(
            canonical_payload=payload,
            **{**binding, "requirement_dispositions": (produced_without_sibling,)},
        )


def test_rollback_evidence_set_requires_exact_support_and_empty_findings() -> None:
    run = _rollback_validation_run()
    policy, rule = _outcome_binding("rollback.validate", "rollback-validation-passed", "primary")
    _regression_policy, regression_rule = _outcome_binding(
        "rollback.validate", "rollback-validation-passed", "regression"
    )
    evidence_id = "artifact:history-evidence"
    fact = final_sibling_fact_for(
        run=run,
        artifact_id=evidence_id,
        outcome_rule=regression_rule,
        payload_schema_id="regression-evidence@1",
        canonical_payload={
            "payload_schema_version": "regression-evidence@1",
            "requirement_id": "history",
            "dimension": "history",
            "status": "passed",
            "reason_code": None,
            "detail": {},
        },
        payload_hash=_HEX,
        authoritative_meta={"requirement_id": "history"},
    )
    payload: dict[str, object] = {
        "evidence_schema_version": "evidence-set@1",
        "subject_artifact_id": "artifact:rollback",
        "subject_digest": _HEX,
        "policy_version": "rollback@1",
        "validation_run_id": "run:1",
        "target_binding": {"target_artifact_id": "artifact:target"},
        "supporting_artifact_ids": [
            evidence_id,
            "artifact:target",
            "artifact:suite",
        ],
        "finding_bindings": [],
        "requirements": [
            {
                "requirement_id": "history",
                "kind": "history",
                "applicability": "required",
                "status": "passed",
                "evidence_artifact_id": evidence_id,
                "reason_code": None,
                "tool_version": "rollback-validation@1",
            }
        ],
        "overall_status": "passed",
    }
    valid = bind_final_payload_references(
        run=run,
        outcome_policy=policy,
        outcome_rule=rule,
        payload_schema_id="evidence-set@1",
        canonical_payload=payload,
        projected_tuple=VersionTuple(),
        final_artifact_ids_by_rule={"regression": (evidence_id,)},
        final_sibling_facts_by_id={evidence_id: fact},
    )
    assert valid["supporting_artifact_ids"] == sorted(payload["supporting_artifact_ids"])

    with pytest.raises(IntegrityViolation, match="supporting"):
        bind_final_payload_references(
            run=run,
            outcome_policy=policy,
            outcome_rule=rule,
            payload_schema_id="evidence-set@1",
            canonical_payload={
                **payload,
                "supporting_artifact_ids": [evidence_id, "artifact:suite"],
            },
            projected_tuple=VersionTuple(),
            final_artifact_ids_by_rule={"regression": (evidence_id,)},
            final_sibling_facts_by_id={evidence_id: fact},
        )
