from __future__ import annotations

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from gameforge.apps.worker.auto_apply import (
    RegistryResolvedAutoApplyEvaluator,
    build_worker_auto_apply_evaluator,
    ensure_worker_auto_apply_catalog_supported,
)
from gameforge.contracts.auto_apply_ownership import (
    AUTO_APPLY_IR_ALL_TAG_V1,
    auto_apply_ir_classifier_binding,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    execution_profile_payload_hash,
)
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    compute_domain_registry_digest,
)
from gameforge.contracts.jobs import (
    PatchValidationPayloadV1,
    RefReadBindingV1,
    ResolvedArtifactRequirementV1,
    ResolvedPolicySnapshotV1,
    ValidationSubjectBindingV1,
    resolved_policy_snapshot_digest,
)
from gameforge.contracts.findings import PatchV2, TypedOp
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.lineage import VersionTuple, build_artifact_v2, object_ref_for_bytes
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyPolicyRegistryV1,
    AutoApplyPolicyV1,
    AutoApplyOracleAttestationV1,
    AutoApplyOutcomeAttestationV1,
    DeterministicOracleRefV1,
    DeterministicOracleDefinitionV1,
    DeterministicOracleRegistryRefV1,
    DeterministicOracleRegistryV1,
    EvidenceRequirement,
    PatchTargetBindingV1,
    QualifiedOutcomeRuleRefV1,
    compute_auto_apply_policy_digest,
    compute_auto_apply_policy_registry_digest,
    compute_deterministic_oracle_digest,
    compute_deterministic_oracle_registry_digest,
)
from gameforge.platform.approvals.auto_apply import AutoApplyChangeAssessment
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.run_handlers.patch_validation import (
    AutoApplyEvaluationRequest,
    AutoApplyEvidenceCandidate,
)
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.runtime.clock import SystemUtcClock
from tests.platform.m4c.handler_support import build_context

_HEX = "a" * 64


class _UnexpectedPolicyRead:
    def resolve(self, ref):
        raise AssertionError(f"null auto-apply profile read policy registry {ref!r}")


@dataclass
class _PolicyResolver:
    registry: AutoApplyPolicyRegistryV1 | None
    seen: list[AutoApplyPolicyRegistryRefV1]

    def resolve(self, ref: AutoApplyPolicyRegistryRefV1) -> AutoApplyPolicyRegistryV1 | None:
        self.seen.append(ref)
        return self.registry


@dataclass
class _Resolver:
    value: object | None

    def resolve(self, ref):
        return self.value


class _Artifacts:
    def __init__(self) -> None:
        self.artifacts = {}
        self.blobs = {}
        base = Snapshot(
            entities={
                "npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={}),
                "item:1": Entity(id="item:1", type=NodeType.ITEM, attrs={}),
            },
            relations={},
        )
        relation = Relation(
            id="relation:1",
            type=EdgeType.DROPS_FROM,
            src_id="npc:1",
            dst_id="item:1",
        )
        preview = Snapshot(
            entities=base.entities,
            relations={relation.id: relation},
        )
        base_blob = canonical_json(base.content_payload).encode()
        base_ref = object_ref_for_bytes(base_blob)
        base_artifact = build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=VersionTuple(
                ir_snapshot_id=base.snapshot_id,
                tool_version="test@1",
            ),
            lineage=(),
            payload_hash=base_ref.sha256,
            object_ref=base_ref,
            meta={
                "payload_schema_id": "ir-core@1",
                "domain_scope": {"domain_ids": ["economy"]},
            },
            created_at="2026-07-17T00:00:00Z",
        )
        patch = PatchV2(
            revision=1,
            base_snapshot_id=base.snapshot_id,
            target_snapshot_id=preview.snapshot_id,
            expected_to_fix=[],
            preconditions=[],
            side_effect_risk="low",
            ops=[
                TypedOp(
                    op_id="op:1",
                    op="add_relation",
                    target=relation.id,
                    new_value=relation.model_dump(mode="json"),
                )
            ],
            produced_by="agent",
            producer_run_id="run:repair",
            rationale="test",
        )
        patch_blob = canonical_json(patch.model_dump(mode="json")).encode()
        patch_ref = object_ref_for_bytes(patch_blob)
        patch_artifact = build_artifact_v2(
            kind="patch",
            version_tuple=VersionTuple(
                ir_snapshot_id=base.snapshot_id,
                tool_version="test@1",
            ),
            lineage=(base_artifact.artifact_id,),
            payload_hash=patch_ref.sha256,
            object_ref=patch_ref,
            meta={
                "payload_schema_id": "patch@2",
                "domain_scope": {"domain_ids": ["economy"]},
            },
            created_at="2026-07-17T00:00:00Z",
        )
        preview_blob = canonical_json(preview.content_payload).encode()
        preview_ref = object_ref_for_bytes(preview_blob)
        preview_artifact = build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=VersionTuple(
                ir_snapshot_id=preview.snapshot_id,
                tool_version="test@1",
            ),
            lineage=tuple(sorted((base_artifact.artifact_id, patch_artifact.artifact_id))),
            payload_hash=preview_ref.sha256,
            object_ref=preview_ref,
            meta={
                "payload_schema_id": "ir-core@1",
                "domain_scope": {"domain_ids": ["economy"]},
            },
            created_at="2026-07-17T00:00:00Z",
        )
        for artifact, blob in (
            (base_artifact, base_blob),
            (patch_artifact, patch_blob),
            (preview_artifact, preview_blob),
        ):
            artifact_id = artifact.artifact_id
            object_ref = object_ref_for_bytes(blob)
            assert object_ref == artifact.object_ref
            self.artifacts[artifact_id] = artifact
            self.blobs[artifact_id] = blob
        self.base_id = base_artifact.artifact_id
        self.patch_id = patch_artifact.artifact_id
        self.preview_id = preview_artifact.artifact_id

    def load_artifact(self, artifact_id):
        return self.artifacts[artifact_id]

    def read_bytes(self, artifact_id):
        return self.blobs[artifact_id]

    def get_ref(self, ref_name):
        assert ref_name == "content/head"
        return RefValue(artifact_id=self.base_id, revision=1)


class _Assessment:
    def assess(self, *, base, subject, target, domain_registry):
        classifier = auto_apply_ir_classifier_binding(domain_registry)
        return AutoApplyChangeAssessment(
            base_artifact_id=base.artifact.artifact_id,
            base_snapshot_id=base.artifact.version_tuple.ir_snapshot_id,
            subject_artifact_id=subject.artifact.artifact_id,
            subject_digest=subject.artifact.payload_hash,
            target_artifact_id=target.artifact.artifact_id,
            target_snapshot_id=target.artifact.version_tuple.ir_snapshot_id,
            target_digest=target.artifact.payload_hash,
            target_payload_schema_id="ir-core@1",
            schema_id=classifier.classifier_schema_id,
            schema_digest=classifier.classifier_schema_digest,
            affected_domain_scope=DomainScope(domain_ids=("economy",)),
            field_classification_complete=True,
            numeric_value_changed=False,
            narrative_text_changed=False,
        )


def _builtin_validation_definition() -> ExecutionProfileDefinitionV1:
    definitions: list[ExecutionProfileDefinitionV1] = []
    for definition in (
        definition
        for catalog in build_builtin_registry().list_execution_profile_catalogs()
        for definition in catalog.definitions
        if definition.profile_kind == "validation"
    ):
        if definition not in definitions:
            definitions.append(definition)
    assert len(definitions) == 1
    return definitions[0]


def _domain_registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(domain_id="economy", display_name="Economy", status="active"),
    )
    version = "domains@1"
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _multi_domain_registry(*, complete: bool) -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(
            domain_id="content",
            display_name="Content",
            status="active",
        ),
        DomainDefinitionV1(
            domain_id="economy",
            display_name="Economy",
            tags=((AUTO_APPLY_IR_ALL_TAG_V1,) if complete else ("auto-apply:entity-type:NPC@1",)),
            status="active",
        ),
    )
    version = "domains@multi-1"
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _oracle_registry(domains: DomainRegistryV1) -> DeterministicOracleRegistryV1:
    domain_ref = DomainRegistryRefV1(
        registry_version=domains.registry_version,
        registry_digest=domains.registry_digest,
    )
    fields = {
        "oracle_id": "graph.structural",
        "oracle_version": "1",
        "engine_kind": "graph",
        "tool_version": "checker@1",
        "domain_registry": domain_ref,
        "supported_domain_scope": DomainScope(domain_ids=("economy",)),
        "evidence_artifact_kinds": ("regression_evidence",),
        "evidence_payload_schema_ids": ("regression-evidence@1",),
        "predicate_schema_id": "gameforge-dimension-status@1",
    }
    definition = DeterministicOracleDefinitionV1(
        **fields,
        oracle_digest=compute_deterministic_oracle_digest(**fields),
    )
    version = "oracles@1"
    return DeterministicOracleRegistryV1(
        registry_version=version,
        definitions=(definition,),
        registry_digest=compute_deterministic_oracle_registry_digest(version, (definition,)),
    )


def _policy_registry(
    domains: DomainRegistryV1 | None = None,
    oracles: DeterministicOracleRegistryV1 | None = None,
) -> AutoApplyPolicyRegistryV1:
    domains = domains or _domain_registry()
    oracles = oracles or _oracle_registry(domains)
    oracle_definition = oracles.definitions[0]
    oracle = DeterministicOracleRefV1(
        oracle_id=oracle_definition.oracle_id,
        oracle_version=oracle_definition.oracle_version,
        oracle_digest=oracle_definition.oracle_digest,
    )
    policy = AutoApplyPolicyV1(
        policy_id="structural-safe",
        policy_version="1",
        allowed_operation_kinds=("add_relation",),
        maximum_operation_count=1,
        domain_registry=DomainRegistryRefV1(
            registry_version=domains.registry_version,
            registry_digest=domains.registry_digest,
        ),
        deterministic_oracle_registry=DeterministicOracleRegistryRefV1(
            registry_version=oracles.registry_version,
            registry_digest=oracles.registry_digest,
        ),
        required_deterministic_oracles=(oracle,),
        required_outcome_rules=(
            QualifiedOutcomeRuleRefV1(
                resolved_policy_id="patch-validation",
                outcome_rule_id="regression",
            ),
        ),
        allowed_domain_scopes=(DomainScope(domain_ids=("economy",)),),
        forbidden_domain_scopes=(),
        require_no_numeric_value_change=True,
        require_no_narrative_text_change=True,
        allowed_ref_names=("content/head",),
    )
    version = "auto@1"
    return AutoApplyPolicyRegistryV1(
        registry_version=version,
        policies=(policy,),
        registry_digest=compute_auto_apply_policy_registry_digest(version, (policy,)),
    )


def _policy_ref(registry: AutoApplyPolicyRegistryV1) -> AutoApplyPolicyRefV1:
    policy = registry.policies[0]
    return AutoApplyPolicyRefV1(
        registry=AutoApplyPolicyRegistryRefV1(
            registry_version=registry.registry_version,
            registry_digest=registry.registry_digest,
        ),
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_digest=compute_auto_apply_policy_digest(policy),
    )


def _configured_definition(
    policy_ref: AutoApplyPolicyRefV1,
) -> ExecutionProfileDefinitionV1:
    payload = _builtin_validation_definition().model_dump(mode="json")
    payload["details"]["auto_apply_policy"] = policy_ref.model_dump(mode="json")
    payload["domain_scope"] = {"domain_ids": ["economy"]}
    return ExecutionProfileDefinitionV1.model_validate(payload)


def _profile_history(
    definition: ExecutionProfileDefinitionV1,
    *,
    lifecycle_state: str = "active",
    duplicate_catalog: bool = False,
):
    builtin = build_builtin_registry()

    def resolve(binding: ResolvedExecutionProfileBindingV1):
        if (
            binding.catalog_version != 1
            or binding.catalog_digest != _HEX
            or binding.profile != definition.profile
            or binding.expected_profile_kind != definition.profile_kind
            or binding.profile_payload_hash != execution_profile_payload_hash(definition)
        ):
            raise IntegrityViolation("test execution profile binding is unavailable")
        return definition, SimpleNamespace(state=lifecycle_state)

    catalogs = (SimpleNamespace(definitions=(definition,)),)
    if duplicate_catalog:
        catalogs = (*catalogs, SimpleNamespace(definitions=(definition,)))
    return SimpleNamespace(
        list_execution_profile_catalogs=lambda: catalogs,
        resolve_execution_profile_binding=resolve,
        get_run_kind=builtin.get_run_kind,
    )


def _request(
    definition: ExecutionProfileDefinitionV1,
    *,
    payload_hash: str | None = None,
    artifacts: _Artifacts | None = None,
) -> AutoApplyEvaluationRequest:
    artifacts = artifacts or _Artifacts()
    frozen_hash = payload_hash or execution_profile_payload_hash(definition)
    subject_digest = artifacts.artifacts[artifacts.patch_id].payload_hash
    target_digest = artifacts.artifacts[artifacts.preview_id].payload_hash
    validation_policy = definition.profile
    params = PatchValidationPayloadV1(
        subject=ValidationSubjectBindingV1(
            approval_id="approval:1",
            expected_workflow_revision=2,
            subject_head_revision=1,
            subject_artifact_id=artifacts.patch_id,
            subject_digest=subject_digest,
            active_validation_run_id="run:1",
        ),
        base_snapshot_artifact_id=artifacts.base_id,
        preview_snapshot_artifact_id=artifacts.preview_id,
        candidate_config_export_artifact_ids=(),
        target=RefReadBindingV1(
            ref_name="content/head",
            expected_ref=RefValue(artifact_id=artifacts.base_id, revision=1),
        ),
        validation_policy=validation_policy,
        checker_profiles=(ProfileRefV1(profile_id="graph", version=1),),
        simulation_profiles=(),
        findings=(),
        review_artifact_ids=(),
        playtest_trace_artifact_ids=(),
        regression_suite_artifact_ids=(),
    )
    requirement = ResolvedArtifactRequirementV1(
        requirement_id="checker:graph@1",
        outcome_rule_id="regression",
        artifact_kind="regression_evidence",
        payload_schema_id="regression-evidence@1",
        ordinal=1,
    )
    snapshot_body = {
        "resolved_policy_id": "patch-validation",
        "source_profile_field_path": "/params/validation_policy",
        "source_profile_payload_hash": frozen_hash,
        "requirements": (requirement,),
    }
    snapshot = ResolvedPolicySnapshotV1(
        **snapshot_body,
        digest=resolved_policy_snapshot_digest(snapshot_body),
    )
    builtin_catalog = next(
        (
            catalog
            for catalog in build_builtin_registry().list_execution_profile_catalogs()
            if definition in catalog.definitions
        ),
        None,
    )
    profile_binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/validation_policy",
        profile=validation_policy,
        expected_profile_kind="validation",
        profile_payload_hash=frozen_hash,
        catalog_version=(1 if builtin_catalog is None else builtin_catalog.catalog_version),
        catalog_digest=(_HEX if builtin_catalog is None else builtin_catalog.catalog_digest),
    )
    run = build_context(
        params=params,
        kind=RunKindRef(kind="patch.validate", version=1),
        resolved_profiles=(profile_binding,),
        resolved_policy_snapshots=(snapshot,),
        resource_domain_scope=DomainScope(domain_ids=("economy",)),
    ).run
    evidence_requirement = EvidenceRequirement(
        requirement_id=requirement.requirement_id,
        kind="regression",
        applicability="required",
        status="passed",
        evidence_artifact_id="artifact:regression",
        tool_version="checker@1",
    )
    candidate = AutoApplyEvidenceCandidate(
        requirement=evidence_requirement,
        artifact_id="artifact:regression",
        payload_hash="d" * 64,
        direct_parent_artifact_ids=tuple(sorted((artifacts.patch_id, artifacts.preview_id))),
        oracle_coverage=("graph", "source:checker"),
        oracle_attestations=(
            AutoApplyOracleAttestationV1(
                oracle=DeterministicOracleRefV1(
                    oracle_id="graph.structural",
                    oracle_version="1",
                    oracle_digest=_oracle_registry(_domain_registry()).definitions[0].oracle_digest,
                ),
                engine_kind="graph",
                engine_id="graph",
                engine_version="1",
                tool_version="checker@1",
                predicate_schema_id="gameforge-dimension-status@1",
                predicate={
                    "kind": "dimension_status",
                    "requirement_id": "checker:graph@1",
                    "engine_id": "graph",
                    "engine_version": "1",
                    "status": "passed",
                },
                evaluated_domain_scope=DomainScope(domain_ids=("economy",)),
                verdict="passed",
                direct_parent_artifact_ids=tuple(
                    sorted((artifacts.patch_id, artifacts.preview_id))
                ),
            ),
        ),
        outcome_attestations=(
            AutoApplyOutcomeAttestationV1(
                rule=QualifiedOutcomeRuleRefV1(
                    resolved_policy_id="patch-validation",
                    outcome_rule_id="regression",
                ),
                requirement_id="checker:graph@1",
                evaluated_domain_scope=DomainScope(domain_ids=("economy",)),
                verdict="passed",
                direct_parent_artifact_ids=tuple(
                    sorted((artifacts.patch_id, artifacts.preview_id))
                ),
            ),
        ),
    )
    return AutoApplyEvaluationRequest(
        run=run,
        validation_profile=definition.profile,
        validation_profile_payload_hash=frozen_hash,
        subject_artifact_id=artifacts.patch_id,
        subject_digest=subject_digest,
        target_binding=PatchTargetBindingV1(
            target_artifact_id=artifacts.preview_id,
            target_snapshot_id=artifacts.artifacts[
                artifacts.preview_id
            ].version_tuple.ir_snapshot_id,
            target_digest=target_digest,
            ref_name="content/head",
            expected_ref=RefValue(artifact_id=artifacts.base_id, revision=1),
        ),
        validation_evidence_artifact_id="artifact:evidence-set",
        regression_evidence_artifact_ids=("artifact:regression",),
        requirements=(evidence_requirement,),
        evidence_candidates=(candidate,),
    )


def test_null_policy_resolves_exact_profile_without_reading_policy_history() -> None:
    registry = build_builtin_registry()
    definition = _builtin_validation_definition()
    evaluator = RegistryResolvedAutoApplyEvaluator(
        profiles=registry,
        policy_registries=_UnexpectedPolicyRead(),
    )

    assert evaluator.evaluate(_request(definition)) is None


def test_null_policy_rejects_a_different_frozen_profile_hash() -> None:
    definition = _builtin_validation_definition()
    evaluator = RegistryResolvedAutoApplyEvaluator(
        profiles=build_builtin_registry(),
        policy_registries=_UnexpectedPolicyRead(),
    )

    with pytest.raises(IntegrityViolation, match="payload hash differs"):
        evaluator.evaluate(_request(definition, payload_hash="b" * 64))


def _detached_validation_definition() -> ExecutionProfileDefinitionV1:
    builtin = _builtin_validation_definition()
    return ExecutionProfileDefinitionV1.model_validate(
        builtin.model_copy(update={"display_name": "Detached validation fixture"}).model_dump(
            mode="python"
        )
    )


def test_exact_binding_is_not_stranded_by_duplicate_retained_catalogs() -> None:
    definition = _detached_validation_definition()
    evaluator = RegistryResolvedAutoApplyEvaluator(
        profiles=_profile_history(definition, duplicate_catalog=True),  # type: ignore[arg-type]
        policy_registries=_UnexpectedPolicyRead(),
    )

    assert evaluator.evaluate(_request(definition)) is None


def test_inactive_exact_validation_profile_cannot_construct_auto_apply_proof() -> None:
    definition = _detached_validation_definition()
    evaluator = RegistryResolvedAutoApplyEvaluator(
        profiles=_profile_history(definition, lifecycle_state="disabled"),  # type: ignore[arg-type]
        policy_registries=_UnexpectedPolicyRead(),
    )

    with pytest.raises(IntegrityViolation, match="does not authorize"):
        evaluator.evaluate(_request(definition))


def test_unknown_validation_profile_cannot_degrade_to_no_policy() -> None:
    definition = _builtin_validation_definition()
    evaluator = RegistryResolvedAutoApplyEvaluator(
        profiles=build_builtin_registry(),
        policy_registries=_UnexpectedPolicyRead(),
    )
    request = _request(definition)
    request = replace(
        request,
        validation_profile=ProfileRefV1(profile_id="missing.validation", version=1),
    )

    with pytest.raises(IntegrityViolation, match="exact validation profile binding"):
        evaluator.evaluate(request)


def test_configured_policy_builds_exact_oracle_and_all_outcome_bindings() -> None:
    domains = _domain_registry()
    oracles = _oracle_registry(domains)
    policy_registry = _policy_registry(domains, oracles)
    definition = _configured_definition(_policy_ref(policy_registry))
    resolver = _PolicyResolver(registry=policy_registry, seen=[])
    artifacts = _Artifacts()
    evaluator = RegistryResolvedAutoApplyEvaluator(
        profiles=_profile_history(definition),  # type: ignore[arg-type]
        policy_registries=resolver,
        domain_registries=_Resolver(domains),
        oracle_registries=_Resolver(oracles),
        artifacts=artifacts,
        change_assessor=_Assessment(),
    )

    proof = evaluator.evaluate(_request(definition, artifacts=artifacts))

    assert proof is not None
    assert resolver.seen == [_policy_ref(policy_registry).registry]
    assert (
        tuple(binding.oracle for binding in proof.deterministic_oracle_evidence)
        == policy_registry.policies[0].required_deterministic_oracles
    )
    assert [
        (binding.rule, binding.requirement_id) for binding in proof.required_outcome_evidence
    ] == [
        (
            policy_registry.policies[0].required_outcome_rules[0],
            "checker:graph@1",
        )
    ]
    assert proof.regression_evidence_artifact_ids == ("artifact:regression",)


def test_configured_policy_missing_exact_history_is_not_treated_as_disabled() -> None:
    policy_registry = _policy_registry()
    definition = _configured_definition(_policy_ref(policy_registry))
    evaluator = RegistryResolvedAutoApplyEvaluator(
        profiles=_profile_history(definition),  # type: ignore[arg-type]
        policy_registries=_PolicyResolver(registry=None, seen=[]),
    )

    with pytest.raises(IntegrityViolation, match="registry history is unavailable"):
        evaluator.evaluate(_request(definition))


def test_composition_accepts_configured_catalog_only_with_exact_registry_history() -> None:
    domains = _domain_registry()
    oracles = _oracle_registry(domains)
    policy_registry = _policy_registry(domains, oracles)
    definition = _configured_definition(_policy_ref(policy_registry))

    ensure_worker_auto_apply_catalog_supported(  # type: ignore[arg-type]
        _profile_history(definition),
        policy_registries=_PolicyResolver(registry=policy_registry, seen=[]),
        domain_registries=_Resolver(domains),
        oracle_registries=_Resolver(oracles),
    )

    with pytest.raises(IntegrityViolation, match="domain registry"):
        ensure_worker_auto_apply_catalog_supported(  # type: ignore[arg-type]
            _profile_history(definition),
            policy_registries=_PolicyResolver(registry=policy_registry, seen=[]),
            domain_registries=_Resolver(None),
            oracle_registries=_Resolver(oracles),
        )


@pytest.mark.parametrize("missing", ("policy", "domain", "oracle"))
def test_readiness_rejects_each_missing_configured_auto_apply_resolver(
    missing: str,
) -> None:
    domains = _domain_registry()
    oracles = _oracle_registry(domains)
    policies = _policy_registry(domains, oracles)
    definition = _configured_definition(_policy_ref(policies))

    with pytest.raises(IntegrityViolation, match="unavailable"):
        ensure_worker_auto_apply_catalog_supported(  # type: ignore[arg-type]
            _profile_history(definition),
            policy_registries=(
                None if missing == "policy" else _PolicyResolver(registry=policies, seen=[])
            ),
            domain_registries=(None if missing == "domain" else _Resolver(domains)),
            oracle_registries=(None if missing == "oracle" else _Resolver(oracles)),
        )


def test_readiness_rejects_missing_auto_eligible_outcome_policy() -> None:
    domains = _domain_registry()
    oracles = _oracle_registry(domains)
    policies = _policy_registry(domains, oracles)
    definition = _configured_definition(_policy_ref(policies))
    history = _profile_history(definition)
    retained = build_builtin_registry().get_run_kind(RunKindRef(kind="patch.validate", version=1))
    assert retained is not None
    missing = retained.model_copy(
        update={
            "outcome_policies": tuple(
                policy
                for policy in retained.outcome_policies
                if policy.outcome_code != "patch_validation_auto_eligible"
            )
        }
    )
    target_kind = RunKindRef(kind="patch.validate", version=1)
    history.get_run_kind = lambda run_kind: (
        missing if run_kind == target_kind else build_builtin_registry().get_run_kind(run_kind)
    )

    with pytest.raises(IntegrityViolation, match="publication policy"):
        ensure_worker_auto_apply_catalog_supported(  # type: ignore[arg-type]
            history,
            policy_registries=_PolicyResolver(registry=policies, seen=[]),
            domain_registries=_Resolver(domains),
            oracle_registries=_Resolver(oracles),
        )


def test_readiness_rejects_required_outcome_rule_without_count_binding() -> None:
    domains = _domain_registry()
    oracles = _oracle_registry(domains)
    original = _policy_registry(domains, oracles)
    policy = original.policies[0].model_copy(
        update={
            "required_outcome_rules": (
                QualifiedOutcomeRuleRefV1(
                    resolved_policy_id="patch-validation",
                    outcome_rule_id="not-published",
                ),
            )
        }
    )
    policies = AutoApplyPolicyRegistryV1(
        registry_version=original.registry_version,
        policies=(policy,),
        registry_digest=compute_auto_apply_policy_registry_digest(
            original.registry_version,
            (policy,),
        ),
    )
    definition = _configured_definition(_policy_ref(policies))

    with pytest.raises(IntegrityViolation, match="publication coverage"):
        ensure_worker_auto_apply_catalog_supported(  # type: ignore[arg-type]
            _profile_history(definition),
            policy_registries=_PolicyResolver(registry=policies, seen=[]),
            domain_registries=_Resolver(domains),
            oracle_registries=_Resolver(oracles),
        )


def test_readiness_accepts_complete_multi_domain_ownership_and_rejects_partial() -> None:
    complete_domains = _multi_domain_registry(complete=True)
    complete_oracles = _oracle_registry(complete_domains)
    complete_policies = _policy_registry(complete_domains, complete_oracles)
    complete_definition = _configured_definition(_policy_ref(complete_policies))

    ensure_worker_auto_apply_catalog_supported(  # type: ignore[arg-type]
        _profile_history(complete_definition),
        policy_registries=_PolicyResolver(registry=complete_policies, seen=[]),
        domain_registries=_Resolver(complete_domains),
        oracle_registries=_Resolver(complete_oracles),
    )

    partial_domains = _multi_domain_registry(complete=False)
    partial_oracles = _oracle_registry(partial_domains)
    partial_policies = _policy_registry(partial_domains, partial_oracles)
    partial_definition = _configured_definition(_policy_ref(partial_policies))
    with pytest.raises(IntegrityViolation, match="ownership is incomplete"):
        ensure_worker_auto_apply_catalog_supported(  # type: ignore[arg-type]
            _profile_history(partial_definition),
            policy_registries=_PolicyResolver(registry=partial_policies, seen=[]),
            domain_registries=_Resolver(partial_domains),
            oracle_registries=_Resolver(partial_oracles),
        )


def test_builtin_composition_builds_registry_resolved_evaluator() -> None:
    engine = create_engine("sqlite://")
    try:
        evaluator = build_worker_auto_apply_evaluator(
            registry=build_builtin_registry(),
            engine=engine,
            clock=SystemUtcClock(),
        )
    finally:
        engine.dispose()

    assert isinstance(evaluator, RegistryResolvedAutoApplyEvaluator)
