from __future__ import annotations

import json
from dataclasses import dataclass, replace

import pytest

from gameforge.apps.worker.auto_apply import RegistryResolvedAutoApplyEvaluator
from gameforge.contracts.auto_apply_ownership import auto_apply_ir_classifier_binding
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    VersionTransitionPolicyRefV1,
    canonical_config_hash,
    execution_profile_payload_hash,
)
from gameforge.contracts.findings import PatchV2, TypedOp
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainRoutePolicyRefV1,
    DomainScope,
    compute_domain_registry_digest,
)
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import (
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunRecord,
    RunResultSummaryV1,
    RunResultV1,
    canonical_payload_hash,
)
from gameforge.contracts.lineage import (
    AuditActor,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    AutoApplyEvidenceContextV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyPolicyRegistryV1,
    AutoApplyProofBindingV1,
    AutoApplyProofV1,
    DeterministicOracleRegistryRefV1,
    DeterministicOracleRegistryV1,
    EvidenceSet,
)
from gameforge.platform.approvals.auto_apply import ResolvedArtifactPayload
from gameforge.platform.approvals.auto_apply_runtime import (
    CanonicalIrAutoApplyChangeAssessor,
    ExactAutoApplyApprovalGateway,
    ExactAutoApplyEligibilityRequest,
    ExactAutoApplyEligibilityService,
    _attested_executor_is_frozen,
)
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.run_handlers.validation_common import (
    VALIDATION_SEED_DERIVATION_VERSION,
    derive_validation_subseed,
)
from gameforge.spine.ir.snapshot import Snapshot
from tests.apps.worker.test_auto_apply import (
    _Artifacts as _WorkerArtifacts,
    _configured_definition,
    _domain_registry as _worker_domain_registry,
    _oracle_registry as _worker_oracle_registry,
    _PolicyResolver,
    _policy_ref,
    _policy_registry as _worker_policy_registry,
    _profile_history,
    _request as _worker_request,
    _Resolver,
)


def _ownership_registry() -> DomainRegistryV1:
    content_tags = (
        *(f"auto-apply:entity-type:{item.value}@1" for item in NodeType if item != NodeType.ITEM),
        *(
            f"auto-apply:relation-type:{item.value}@1"
            for item in EdgeType
            if item != EdgeType.SELLS
        ),
    )
    economy_tags = (
        f"auto-apply:entity-type:{NodeType.ITEM.value}@1",
        f"auto-apply:relation-type:{EdgeType.SELLS.value}@1",
    )
    definitions = (
        DomainDefinitionV1(
            domain_id="content",
            display_name="Content",
            tags=content_tags,
            status="active",
        ),
        DomainDefinitionV1(
            domain_id="economy",
            display_name="Economy",
            tags=economy_tags,
            status="active",
        ),
    )
    version = "domains@ownership-1"
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _partial_registry() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(
            domain_id="content",
            display_name="Content",
            tags=("auto-apply:entity-type:NPC@1",),
            status="active",
        ),
        DomainDefinitionV1(
            domain_id="economy",
            display_name="Economy",
            status="active",
        ),
    )
    version = "domains@partial-1"
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _record(
    *,
    kind: str,
    schema: str,
    payload: object,
    version_tuple: VersionTuple,
    lineage: tuple[str, ...] = (),
    scope: tuple[str, ...] = ("content", "economy"),
) -> ResolvedArtifactPayload:
    value = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    blob = canonical_json(value).encode()
    object_ref = object_ref_for_bytes(blob)
    artifact = build_artifact_v2(
        kind=kind,  # type: ignore[arg-type]
        version_tuple=version_tuple,
        lineage=lineage,
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": schema, "domain_scope": {"domain_ids": list(scope)}},
        created_at="2026-07-17T00:00:00Z",
    )
    return ResolvedArtifactPayload(
        artifact=artifact,
        payload_schema_id=schema,
        payload_bytes=blob,
    )


@dataclass(frozen=True)
class _RuntimeAuthority:
    records: dict[str, ResolvedArtifactPayload]
    domains: DomainRegistryV1
    policies: AutoApplyPolicyRegistryV1
    oracles: DeterministicOracleRegistryV1
    validation_profile: ExecutionProfileDefinitionV1
    executor_profiles: tuple[ExecutionProfileDefinitionV1, ...]
    current_ref: RefValue

    def load_artifact(self, artifact_id: str) -> ResolvedArtifactPayload:
        try:
            return self.records[artifact_id]
        except KeyError as exc:
            raise IntegrityViolation(
                "test auto-apply Artifact authority is unavailable",
                artifact_id=artifact_id,
            ) from exc

    def get_domain_registry(self, ref: DomainRegistryRefV1) -> DomainRegistryV1 | None:
        expected = DomainRegistryRefV1(
            registry_version=self.domains.registry_version,
            registry_digest=self.domains.registry_digest,
        )
        return self.domains if ref == expected else None

    def get_auto_apply_policy_registry(
        self, ref: AutoApplyPolicyRegistryRefV1
    ) -> AutoApplyPolicyRegistryV1 | None:
        expected = AutoApplyPolicyRegistryRefV1(
            registry_version=self.policies.registry_version,
            registry_digest=self.policies.registry_digest,
        )
        return self.policies if ref == expected else None

    def get_deterministic_oracle_registry(
        self, ref: DeterministicOracleRegistryRefV1
    ) -> DeterministicOracleRegistryV1 | None:
        expected = DeterministicOracleRegistryRefV1(
            registry_version=self.oracles.registry_version,
            registry_digest=self.oracles.registry_digest,
        )
        return self.oracles if ref == expected else None

    def resolve_execution_profile(
        self, binding: ResolvedExecutionProfileBindingV1
    ) -> ExecutionProfileDefinitionV1 | None:
        if binding.catalog_version != 1 or binding.catalog_digest != "a" * 64:
            return None
        candidates = tuple(
            definition
            for definition in (self.validation_profile, *self.executor_profiles)
            if definition.profile == binding.profile
            and definition.profile_kind == binding.expected_profile_kind
            and execution_profile_payload_hash(definition) == binding.profile_payload_hash
        )
        return candidates[0] if len(candidates) == 1 else None

    def get_ref(self, ref_name: str) -> RefValue | None:
        return self.current_ref if ref_name == "content/head" else None


@dataclass(frozen=True)
class _RuntimeCase:
    authority: _RuntimeAuthority
    request: ExactAutoApplyEligibilityRequest
    item: ApprovalItem
    regression_artifact_id: str

    @property
    def service(self) -> ExactAutoApplyEligibilityService:
        return ExactAutoApplyEligibilityService(authority=self.authority)


def _add_worker_record(
    artifacts: _WorkerArtifacts,
    record: ResolvedArtifactPayload,
) -> None:
    artifact_id = record.artifact.artifact_id
    artifacts.artifacts[artifact_id] = record.artifact
    artifacts.blobs[artifact_id] = record.payload_bytes


def _runtime_case() -> _RuntimeCase:
    artifacts = _WorkerArtifacts()
    domains = _worker_domain_registry()
    oracles = _worker_oracle_registry(domains)
    policies = _worker_policy_registry(domains, oracles)
    policy_ref = _policy_ref(policies)
    definition = _configured_definition(policy_ref)
    evaluation = _worker_request(definition, artifacts=artifacts)
    checker_ref = evaluation.run.payload.params.checker_profiles[0]
    builtin_checker_definitions = tuple(
        candidate
        for catalog in build_builtin_registry().list_execution_profile_catalogs()
        for candidate in catalog.definitions
        if candidate.profile_kind == "checker" and candidate.profile.version == 1
    )
    builtin_checker = builtin_checker_definitions[0]
    assert builtin_checker_definitions and all(
        candidate == builtin_checker for candidate in builtin_checker_definitions
    )
    checker_definition = ExecutionProfileDefinitionV1.model_validate(
        builtin_checker.model_copy(update={"profile": checker_ref}).model_dump(mode="python")
    )
    checker_binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/checker_profiles/0",
        profile=checker_ref,
        expected_profile_kind="checker",
        profile_payload_hash=execution_profile_payload_hash(checker_definition),
        catalog_version=1,
        catalog_digest="a" * 64,
    )
    run_payload = evaluation.run.payload.model_copy(
        update={
            "resolved_profiles": (
                *evaluation.run.payload.resolved_profiles,
                checker_binding,
            )
        }
    )
    run_payload = type(evaluation.run.payload).model_validate(run_payload.model_dump(mode="python"))
    run_fields = evaluation.run.model_dump(mode="python")
    run_fields.update(
        {
            "payload": run_payload,
            "payload_hash": canonical_payload_hash(run_payload),
        }
    )
    evaluation = replace(
        evaluation,
        run=RunRecord.model_validate(run_fields),
    )
    candidate = evaluation.evidence_candidates[0]
    context = AutoApplyEvidenceContextV1(
        subject_artifact_id=evaluation.subject_artifact_id,
        subject_digest=evaluation.subject_digest,
        target_binding=evaluation.target_binding,
        evaluated_domain_scope=DomainScope(domain_ids=("economy",)),
        direct_parent_artifact_ids=candidate.direct_parent_artifact_ids,
    )
    regression_payload = {
        "payload_schema_version": "regression-evidence@1",
        "requirement_id": candidate.requirement.requirement_id,
        "dimension": "checker",
        "lineage_suite_artifact_ids": [],
        "checker_profile": evaluation.run.payload.params.checker_profiles[0].model_dump(
            mode="json"
        ),
        "checker_execution_bindings": [
            {
                "wrapper_id": "graph",
                "native_id": "graph",
                "constraint_id": None,
            }
        ],
        "constraint_snapshot_binding_status": "not_applicable",
        "snapshot_id": evaluation.target_binding.target_snapshot_id,
        "status": "passed",
        "findings": [],
        "auto_apply_context": context.model_dump(mode="json"),
        "oracle_attestations": [
            item.model_dump(mode="json") for item in candidate.oracle_attestations
        ],
        "outcome_attestations": [
            item.model_dump(mode="json") for item in candidate.outcome_attestations
        ],
    }
    regression = _record(
        kind="regression_evidence",
        schema="regression-evidence@1",
        payload=regression_payload,
        version_tuple=evaluation.run.payload.version_tuple,
        lineage=candidate.direct_parent_artifact_ids,
        scope=("economy",),
    )
    _add_worker_record(artifacts, regression)
    requirement = candidate.requirement.model_copy(
        update={"evidence_artifact_id": regression.artifact.artifact_id}
    )
    evidence = EvidenceSet(
        subject_artifact_id=evaluation.subject_artifact_id,
        subject_digest=evaluation.subject_digest,
        policy_version=(f"{definition.profile.profile_id}@{definition.profile.version}"),
        validation_run_id=evaluation.run.run_id,
        target_binding=evaluation.target_binding,
        supporting_artifact_ids=(),
        finding_bindings=(),
        requirements=(requirement,),
        overall_status="passed",
    )
    evidence_record = _record(
        kind="validation_evidence",
        schema="evidence-set@1",
        payload=evidence,
        version_tuple=evaluation.run.payload.version_tuple,
        lineage=tuple(
            sorted(
                {
                    evaluation.subject_artifact_id,
                    evaluation.target_binding.target_artifact_id,
                    regression.artifact.artifact_id,
                }
            )
        ),
        scope=("economy",),
    )
    _add_worker_record(artifacts, evidence_record)
    candidate = replace(
        candidate,
        requirement=requirement,
        artifact_id=regression.artifact.artifact_id,
        payload_hash=regression.artifact.payload_hash,
    )
    evaluation = replace(
        evaluation,
        validation_evidence_artifact_id=evidence_record.artifact.artifact_id,
        regression_evidence_artifact_ids=(regression.artifact.artifact_id,),
        requirements=(requirement,),
        evidence_candidates=(candidate,),
    )
    evaluator = RegistryResolvedAutoApplyEvaluator(
        profiles=_profile_history(definition),
        policy_registries=_PolicyResolver(registry=policies, seen=[]),
        domain_registries=_Resolver(domains),
        oracle_registries=_Resolver(oracles),
        artifacts=artifacts,
    )
    proof = evaluator.evaluate(evaluation)
    assert isinstance(proof, AutoApplyProofV1)
    proof_record = _record(
        kind="validation_evidence",
        schema="auto-apply-proof@1",
        payload=proof,
        version_tuple=evaluation.run.payload.version_tuple,
        lineage=tuple(
            sorted(
                {
                    evaluation.subject_artifact_id,
                    evaluation.target_binding.target_artifact_id,
                    evidence_record.artifact.artifact_id,
                    regression.artifact.artifact_id,
                }
            )
        ),
        scope=("economy",),
    )
    _add_worker_record(artifacts, proof_record)
    current_ref = evaluation.target_binding.expected_ref
    assert current_ref is not None
    domain_ref = DomainRegistryRefV1(
        registry_version=domains.registry_version,
        registry_digest=domains.registry_digest,
    )
    item = ApprovalItem(
        approval_id="approval:1",
        subject_series_id="patch-series:1",
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id=evaluation.subject_artifact_id,
        subject_digest=evaluation.subject_digest,
        status="validated",
        workflow_revision=3,
        proposer=AuditActor(principal_id="human:alice", principal_kind="human"),
        domain_scope=DomainScope(domain_ids=("economy",)),
        domain_registry_ref=domain_ref,
        route_policy=DomainRoutePolicyRefV1(
            route_version="routes@1",
            route_digest="2" * 64,
            domain_registry_ref=domain_ref,
        ),
        role_policy_version="roles@1",
        role_policy_digest="3" * 64,
        approval_policy=ApprovalPolicyRefV1(
            policy_version="approval@1",
            policy_digest="4" * 64,
        ),
        requirements=(),
        decisions=(),
        evidence_set_artifact_id=evidence_record.artifact.artifact_id,
        regression_evidence_artifact_ids=(regression.artifact.artifact_id,),
        target_binding=evaluation.target_binding,
        auto_apply_proof=AutoApplyProofBindingV1(
            proof_artifact_id=proof_record.artifact.artifact_id,
            policy=policy_ref,
            subject_digest=evaluation.subject_digest,
            target_digest=evaluation.target_binding.target_digest,
            expected_ref=current_ref,
            validation_evidence_artifact_id=evidence_record.artifact.artifact_id,
        ),
        created_at="2026-07-17T00:00:00Z",
    )
    records = {
        artifact_id: ResolvedArtifactPayload(
            artifact=artifact,
            payload_schema_id=artifact.meta["payload_schema_id"],  # type: ignore[arg-type]
            payload_bytes=artifacts.blobs[artifact_id],
        )
        for artifact_id, artifact in artifacts.artifacts.items()
    }
    authority = _RuntimeAuthority(
        records=records,
        domains=domains,
        policies=policies,
        oracles=oracles,
        validation_profile=definition,
        executor_profiles=(checker_definition,),
        current_ref=current_ref,
    )
    request = ExactAutoApplyEligibilityRequest(
        run=evaluation.run,
        item=item,
        outcome_code="patch_validation_auto_eligible",
        proof_artifact_id=proof_record.artifact.artifact_id,
        evidence_set_artifact_id=evidence_record.artifact.artifact_id,
    )
    return _RuntimeCase(
        authority=authority,
        request=request,
        item=item,
        regression_artifact_id=regression.artifact.artifact_id,
    )


def _assessment_case(
    *,
    base: Snapshot,
    target: Snapshot,
    target_scope: tuple[str, ...] = ("content", "economy"),
) -> tuple[ResolvedArtifactPayload, ResolvedArtifactPayload, ResolvedArtifactPayload]:
    base_record = _record(
        kind="ir_snapshot",
        schema="ir-core@1",
        payload=base.content_payload,
        version_tuple=VersionTuple(ir_snapshot_id=base.snapshot_id, tool_version="test@1"),
    )
    ops: list[TypedOp] = []
    for relation_id in sorted(set(base.relations) - set(target.relations)):
        ops.append(
            TypedOp(
                op_id=f"delete-relation:{relation_id}",
                op="delete_relation",
                target=relation_id,
            )
        )
    for entity_id in sorted(set(base.entities) - set(target.entities)):
        ops.append(
            TypedOp(
                op_id=f"delete-entity:{entity_id}",
                op="delete_entity",
                target=entity_id,
            )
        )
    if target.entities or target.relations:
        ops.append(
            TypedOp(
                op_id="replace-target-subgraph",
                op="replace_subgraph",
                target="auto-apply:target",
                new_value={
                    "entities": [
                        target.entities[entity_id].model_dump(mode="json")
                        for entity_id in sorted(target.entities)
                    ],
                    "relations": [
                        target.relations[relation_id].model_dump(mode="json")
                        for relation_id in sorted(target.relations)
                    ],
                },
            )
        )
    patch = PatchV2(
        revision=1,
        base_snapshot_id=base.snapshot_id,
        target_snapshot_id=target.snapshot_id,
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=ops,
        produced_by="agent",
        producer_run_id="run:repair",
        rationale="ownership test",
    )
    patch_record = _record(
        kind="patch",
        schema="patch@2",
        payload=patch,
        version_tuple=VersionTuple(ir_snapshot_id=base.snapshot_id, tool_version="test@1"),
        lineage=(base_record.artifact.artifact_id,),
    )
    target_record = _record(
        kind="ir_snapshot",
        schema="ir-core@1",
        payload=target.content_payload,
        version_tuple=VersionTuple(ir_snapshot_id=target.snapshot_id, tool_version="test@1"),
        lineage=tuple(
            sorted((base_record.artifact.artifact_id, patch_record.artifact.artifact_id))
        ),
        scope=target_scope,
    )
    return base_record, patch_record, target_record


def _assess(
    base: Snapshot,
    target: Snapshot,
    *,
    registry: DomainRegistryV1 | None = None,
    target_scope: tuple[str, ...] = ("content", "economy"),
):
    base_record, patch_record, target_record = _assessment_case(
        base=base,
        target=target,
        target_scope=target_scope,
    )
    return CanonicalIrAutoApplyChangeAssessor().assess(
        base=base_record,
        subject=patch_record,
        target=target_record,
        domain_registry=registry or _ownership_registry(),
    )


def test_multi_domain_diff_derives_single_and_union_resource_owners() -> None:
    empty = Snapshot(entities={}, relations={})
    content = Snapshot(
        entities={"npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={})},
        relations={},
    )
    both = Snapshot(
        entities={
            "npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={}),
            "item:1": Entity(id="item:1", type=NodeType.ITEM, attrs={}),
        },
        relations={},
    )

    assert _assess(empty, content).affected_domain_scope == DomainScope(domain_ids=("content",))
    assert _assess(empty, both).affected_domain_scope == DomainScope(
        domain_ids=("content", "economy")
    )


def test_type_change_unions_old_and_new_owners() -> None:
    base = Snapshot(
        entities={"resource:1": Entity(id="resource:1", type=NodeType.NPC, attrs={})},
        relations={},
    )
    target = Snapshot(
        entities={"resource:1": Entity(id="resource:1", type=NodeType.ITEM, attrs={})},
        relations={},
    )

    assert _assess(base, target).affected_domain_scope == DomainScope(
        domain_ids=("content", "economy")
    )


def test_deep_path_and_escaped_resource_id_use_enclosing_resource_type() -> None:
    resource_id = "npc/a~b"
    base = Snapshot(
        entities={resource_id: Entity(id=resource_id, type=NodeType.NPC, attrs={"level": 1})},
        relations={},
    )
    target = Snapshot(
        entities={resource_id: Entity(id=resource_id, type=NodeType.NPC, attrs={"level": 2})},
        relations={},
    )

    assessment = _assess(base, target)

    assert assessment.affected_domain_scope == DomainScope(domain_ids=("content",))
    assert assessment.numeric_value_changed
    assert assessment.field_classification_complete


@pytest.mark.parametrize("value", [True, None])
def test_semantic_boolean_or_null_change_is_not_provably_structural(value: object) -> None:
    base = Snapshot(
        entities={"npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={"enabled": False})},
        relations={},
    )
    target = Snapshot(
        entities={"npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={"enabled": value})},
        relations={},
    )

    assessment = _assess(base, target)

    assert assessment.affected_domain_scope == DomainScope(domain_ids=("content", "economy"))
    assert not assessment.field_classification_complete
    assert not assessment.numeric_value_changed
    assert not assessment.narrative_text_changed


def test_relation_type_uses_its_explicit_owner() -> None:
    entities = {
        "npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={}),
        "item:1": Entity(id="item:1", type=NodeType.ITEM, attrs={}),
    }
    base = Snapshot(entities=entities, relations={})
    relation = Relation(
        id="sells:1",
        type=EdgeType.SELLS,
        src_id="npc:1",
        dst_id="item:1",
    )
    target = Snapshot(entities=entities, relations={relation.id: relation})

    assert _assess(base, target).affected_domain_scope == DomainScope(domain_ids=("economy",))


def test_deleted_resource_uses_its_base_snapshot_owner() -> None:
    base = Snapshot(
        entities={"item:1": Entity(id="item:1", type=NodeType.ITEM, attrs={})},
        relations={},
    )
    target = Snapshot(entities={}, relations={})

    assert _assess(base, target).affected_domain_scope == DomainScope(domain_ids=("economy",))


def test_incomplete_ownership_fails_closed_with_conservative_scope() -> None:
    empty = Snapshot(entities={}, relations={})
    target = Snapshot(
        entities={"npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={})},
        relations={},
    )

    assessment = _assess(empty, target, registry=_partial_registry())

    assert not assessment.field_classification_complete
    assert assessment.affected_domain_scope == DomainScope(domain_ids=("content", "economy"))
    classifier = auto_apply_ir_classifier_binding(_partial_registry())
    assert assessment.schema_digest == classifier.classifier_schema_digest


def test_artifact_metadata_cannot_narrow_derived_scope() -> None:
    empty = Snapshot(entities={}, relations={})
    target = Snapshot(
        entities={
            "npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={}),
            "item:1": Entity(id="item:1", type=NodeType.ITEM, attrs={}),
        },
        relations={},
    )

    with pytest.raises(IntegrityViolation, match="narrows"):
        _assess(empty, target, target_scope=("content",))


def test_assessor_rejects_patch_ops_detached_from_the_claimed_target() -> None:
    base = Snapshot(entities={}, relations={})
    target = Snapshot(
        entities={"npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={})},
        relations={},
    )
    base_record, _, target_record = _assessment_case(base=base, target=target)
    detached = PatchV2(
        revision=1,
        base_snapshot_id=base.snapshot_id,
        target_snapshot_id=target.snapshot_id,
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[
            TypedOp(
                op_id="detached",
                op="add_entity",
                target="item:1",
                new_value={"type": NodeType.ITEM.value, "attrs": {}},
            )
        ],
        produced_by="agent",
        producer_run_id="run:repair",
        rationale="detached target",
    )
    detached_record = _record(
        kind="patch",
        schema="patch@2",
        payload=detached,
        version_tuple=VersionTuple(ir_snapshot_id=base.snapshot_id, tool_version="test@1"),
        lineage=(base_record.artifact.artifact_id,),
    )

    with pytest.raises(IntegrityViolation, match="replay differs"):
        CanonicalIrAutoApplyChangeAssessor().assess(
            base=base_record,
            subject=detached_record,
            target=target_record,
            domain_registry=_ownership_registry(),
        )


def test_assessor_rejects_ir_payload_under_a_non_snapshot_base_kind() -> None:
    base = Snapshot(entities={}, relations={})
    target = Snapshot(
        entities={"npc:1": Entity(id="npc:1", type=NodeType.NPC, attrs={})},
        relations={},
    )
    _, patch_record, target_record = _assessment_case(base=base, target=target)
    forged_base = _record(
        kind="config_export",
        schema="ir-core@1",
        payload=base.content_payload,
        version_tuple=VersionTuple(ir_snapshot_id=base.snapshot_id, tool_version="test@1"),
    )

    with pytest.raises(IntegrityViolation, match="exact ir-core"):
        CanonicalIrAutoApplyChangeAssessor().assess(
            base=forged_base,
            subject=patch_record,
            target=target_record,
            domain_registry=_ownership_registry(),
        )


def _decoded_payload(record: ResolvedArtifactPayload) -> dict[str, object]:
    payload = json.loads(record.payload_bytes)
    assert isinstance(payload, dict)
    return payload


def _rebind_regression(
    case: _RuntimeCase,
    regression: ResolvedArtifactPayload,
) -> _RuntimeCase:
    old_regression_id = case.regression_artifact_id
    evidence_id = case.item.evidence_set_artifact_id
    proof_binding = case.item.auto_apply_proof
    assert evidence_id is not None
    assert proof_binding is not None
    evidence = EvidenceSet.model_validate(_decoded_payload(case.authority.records[evidence_id]))
    requirements = tuple(
        requirement.model_copy(update={"evidence_artifact_id": regression.artifact.artifact_id})
        if requirement.evidence_artifact_id == old_regression_id
        else requirement
        for requirement in evidence.requirements
    )
    evidence = EvidenceSet.model_validate(
        evidence.model_copy(update={"requirements": requirements}).model_dump(mode="python")
    )
    evidence_record = _record(
        kind="validation_evidence",
        schema="evidence-set@1",
        payload=evidence,
        version_tuple=case.request.run.payload.version_tuple,
        lineage=tuple(
            sorted(
                {
                    case.item.subject_artifact_id,
                    case.item.target_binding.target_artifact_id,  # type: ignore[union-attr]
                    regression.artifact.artifact_id,
                }
            )
        ),
        scope=("economy",),
    )
    proof = AutoApplyProofV1.model_validate(
        _decoded_payload(case.authority.records[proof_binding.proof_artifact_id])
    )
    oracle_bindings = tuple(
        binding.model_copy(
            update={
                "evidence_artifact_id": regression.artifact.artifact_id,
                "evidence_payload_hash": regression.artifact.payload_hash,
            }
        )
        if binding.evidence_artifact_id == old_regression_id
        else binding
        for binding in proof.deterministic_oracle_evidence
    )
    outcome_bindings = tuple(
        binding.model_copy(
            update={
                "evidence_artifact_id": regression.artifact.artifact_id,
                "evidence_payload_hash": regression.artifact.payload_hash,
            }
        )
        if binding.evidence_artifact_id == old_regression_id
        else binding
        for binding in proof.required_outcome_evidence
    )
    proof = AutoApplyProofV1.model_validate(
        proof.model_copy(
            update={
                "validation_evidence_artifact_id": evidence_record.artifact.artifact_id,
                "regression_evidence_artifact_ids": (regression.artifact.artifact_id,),
                "deterministic_oracle_evidence": oracle_bindings,
                "required_outcome_evidence": outcome_bindings,
            }
        ).model_dump(mode="python")
    )
    target_binding = case.item.target_binding
    assert target_binding is not None
    proof_record = _record(
        kind="validation_evidence",
        schema="auto-apply-proof@1",
        payload=proof,
        version_tuple=case.request.run.payload.version_tuple,
        lineage=tuple(
            sorted(
                {
                    case.item.subject_artifact_id,
                    target_binding.target_artifact_id,
                    evidence_record.artifact.artifact_id,
                    regression.artifact.artifact_id,
                }
            )
        ),
        scope=("economy",),
    )
    item_proof_binding = AutoApplyProofBindingV1(
        proof_artifact_id=proof_record.artifact.artifact_id,
        policy=proof_binding.policy,
        subject_digest=case.item.subject_digest,
        target_digest=target_binding.target_digest,
        expected_ref=target_binding.expected_ref,
        validation_evidence_artifact_id=evidence_record.artifact.artifact_id,
    )
    item = ApprovalItem.model_validate(
        case.item.model_copy(
            update={
                "evidence_set_artifact_id": evidence_record.artifact.artifact_id,
                "regression_evidence_artifact_ids": (regression.artifact.artifact_id,),
                "auto_apply_proof": item_proof_binding,
            }
        ).model_dump(mode="python")
    )
    records = dict(case.authority.records)
    records.update(
        {
            regression.artifact.artifact_id: regression,
            evidence_record.artifact.artifact_id: evidence_record,
            proof_record.artifact.artifact_id: proof_record,
        }
    )
    authority = replace(case.authority, records=records)
    request = ExactAutoApplyEligibilityRequest(
        run=case.request.run,
        item=item,
        outcome_code=case.request.outcome_code,
        proof_artifact_id=proof_record.artifact.artifact_id,
        evidence_set_artifact_id=evidence_record.artifact.artifact_id,
    )
    return _RuntimeCase(
        authority=authority,
        request=request,
        item=item,
        regression_artifact_id=regression.artifact.artifact_id,
    )


def _forge_regression(case: _RuntimeCase, variant: str) -> _RuntimeCase:
    original = case.authority.records[case.regression_artifact_id]
    payload = _decoded_payload(original)
    oracle_attestations = payload.get("oracle_attestations")
    outcome_attestations = payload.get("outcome_attestations")
    assert isinstance(oracle_attestations, list) and oracle_attestations
    assert isinstance(oracle_attestations[0], dict)
    assert isinstance(outcome_attestations, list) and outcome_attestations
    if variant == "predicate":
        predicate = oracle_attestations[0]["predicate"]
        assert isinstance(predicate, dict)
        predicate["status"] = "failed"
    elif variant == "predicate_schema":
        oracle_attestations[0]["predicate_schema_id"] = "forged-predicate@1"
    elif variant == "engine_kind":
        oracle_attestations[0]["engine_kind"] = "smt"
    elif variant == "engine_id":
        oracle_attestations[0]["engine_id"] = "forged-engine"
    elif variant == "engine_version":
        oracle_attestations[0]["engine_version"] = "2"
    elif variant == "tool_version":
        oracle_attestations[0]["tool_version"] = "checker@forged"
    elif variant == "scope":
        oracle_attestations[0]["evaluated_domain_scope"] = {"domain_ids": ["content"]}
    elif variant == "parents":
        oracle_attestations[0]["direct_parent_artifact_ids"] = [
            *oracle_attestations[0]["direct_parent_artifact_ids"],
            "artifact:forged-parent",
        ]
    elif variant == "duplicate_attestation":
        oracle_attestations.append(dict(oracle_attestations[0]))
    elif variant == "missing_oracle_attestation":
        payload.pop("oracle_attestations")
    elif variant == "missing_outcome_attestation":
        payload.pop("outcome_attestations")
    elif variant == "invalid_discriminator":
        payload["payload_schema_version"] = "regression-evidence@2"
    elif variant == "invalid_findings":
        payload["findings"] = [{"forged": "finding"}]
    else:  # pragma: no cover - test table controls this helper
        raise AssertionError(variant)
    regression = _record(
        kind="regression_evidence",
        schema="regression-evidence@1",
        payload=payload,
        version_tuple=original.artifact.version_tuple,
        lineage=original.artifact.lineage,
        scope=("economy",),
    )
    return _rebind_regression(case, regression)


def test_exact_runtime_guard_accepts_evaluator_produced_authority_closure() -> None:
    case = _runtime_case()

    case.service.validate_eligibility(case.request)


def _with_run_profile_bindings(
    case: _RuntimeCase,
    bindings: tuple[ResolvedExecutionProfileBindingV1, ...],
) -> _RuntimeCase:
    payload = case.request.run.payload.model_copy(update={"resolved_profiles": bindings})
    payload = type(case.request.run.payload).model_validate(payload.model_dump(mode="python"))
    run_fields = case.request.run.model_dump(mode="python")
    run_fields.update(
        {
            "payload": payload,
            "payload_hash": canonical_payload_hash(payload),
        }
    )
    run = RunRecord.model_validate(run_fields)
    return replace(
        case,
        request=replace(case.request, run=run),
    )


def test_runtime_guard_rejects_missing_exact_checker_profile_binding() -> None:
    case = _runtime_case()
    bindings = tuple(
        binding
        for binding in case.request.run.payload.resolved_profiles
        if binding.field_path != "/params/checker_profiles/0"
    )
    forged = _with_run_profile_bindings(case, bindings)

    with pytest.raises(IntegrityViolation, match="profile closure"):
        forged.service.validate_eligibility(forged.request)


def test_runtime_guard_rejects_forged_profile_catalog_binding() -> None:
    case = _runtime_case()
    bindings = tuple(
        binding.model_copy(update={"catalog_digest": "b" * 64})
        if binding.field_path == "/params/validation_policy"
        else binding
        for binding in case.request.run.payload.resolved_profiles
    )
    forged = _with_run_profile_bindings(case, bindings)

    with pytest.raises(IntegrityViolation, match="exact catalogs"):
        forged.service.validate_eligibility(forged.request)


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    (
        ("handler_key", "forged_validation_profile@1"),
        ("config_schema_id", "forged-validation-profile-config@1"),
        ("input_schema_ids", ("constraint-validation@1",)),
        ("output_schema_ids", ("evidence-set@1",)),
        (
            "compatible_run_kinds",
            (
                RunKindRef(kind="constraint_proposal.validate", version=1),
                RunKindRef(kind="patch.validate", version=1),
                RunKindRef(kind="review.run", version=1),
            ),
        ),
    ),
)
def test_runtime_guard_rejects_forged_builtin_validation_definition_contract(
    field_name: str,
    forged_value: object,
) -> None:
    case = _runtime_case()
    definition = ExecutionProfileDefinitionV1.model_validate(
        case.authority.validation_profile.model_copy(update={field_name: forged_value}).model_dump(
            mode="python"
        )
    )
    authority = replace(case.authority, validation_profile=definition)
    bindings = tuple(
        binding.model_copy(
            update={"profile_payload_hash": execution_profile_payload_hash(definition)}
        )
        if binding.field_path == "/params/validation_policy"
        else binding
        for binding in case.request.run.payload.resolved_profiles
    )
    forged = _with_run_profile_bindings(
        replace(case, authority=authority),
        bindings,
    )

    with pytest.raises(IntegrityViolation, match="profile history"):
        forged.service.validate_eligibility(forged.request)


def test_runtime_guard_rejects_partial_builtin_validation_subject_contract() -> None:
    case = _runtime_case()
    details = case.authority.validation_profile.details.model_copy(
        update={"subject_kinds": ("patch",)}
    )
    definition = ExecutionProfileDefinitionV1.model_validate(
        case.authority.validation_profile.model_copy(update={"details": details}).model_dump(
            mode="python"
        )
    )
    authority = replace(case.authority, validation_profile=definition)
    bindings = tuple(
        binding.model_copy(
            update={"profile_payload_hash": execution_profile_payload_hash(definition)}
        )
        if binding.field_path == "/params/validation_policy"
        else binding
        for binding in case.request.run.payload.resolved_profiles
    )
    forged = _with_run_profile_bindings(
        replace(case, authority=authority),
        bindings,
    )

    with pytest.raises(IntegrityViolation, match="profile history"):
        forged.service.validate_eligibility(forged.request)


def test_runtime_guard_rejects_native_engine_outside_exact_checker_config() -> None:
    case = _runtime_case()
    checker = case.authority.executor_profiles[0]
    config = dict(checker.config)
    config["allowed_checker_ids"] = ["smt"]
    forged_checker = ExecutionProfileDefinitionV1.model_validate(
        checker.model_copy(
            update={
                "config": config,
                "config_hash": canonical_config_hash(config),
            }
        ).model_dump(mode="python")
    )
    authority = replace(case.authority, executor_profiles=(forged_checker,))
    bindings = tuple(
        binding.model_copy(
            update={"profile_payload_hash": execution_profile_payload_hash(forged_checker)}
        )
        if binding.field_path == "/params/checker_profiles/0"
        else binding
        for binding in case.request.run.payload.resolved_profiles
    )
    forged = _with_run_profile_bindings(
        replace(case, authority=authority),
        bindings,
    )

    with pytest.raises(IntegrityViolation):
        forged.service.validate_eligibility(forged.request)


def test_simulation_attestation_rejects_unexecuted_bound_constraint_input() -> None:
    case = _runtime_case()
    definition = next(
        definition
        for catalog in build_builtin_registry().list_execution_profile_catalogs()
        for definition in catalog.definitions
        if definition.profile_kind == "simulation"
        and definition.profile.profile_id == "builtin.simulation"
    )
    params = case.request.run.payload.params.model_copy(
        update={"simulation_profiles": (definition.profile,)}
    )
    root_seed = 7
    envelope = case.request.run.payload.model_copy(
        update={
            "params": params,
            "seed": root_seed,
            "version_tuple": case.request.run.payload.version_tuple.model_copy(
                update={"seed": root_seed}
            ),
        }
    )
    run_fields = case.request.run.model_dump(mode="python")
    run_fields.update(
        {
            "payload": envelope,
            "payload_hash": canonical_payload_hash(envelope),
        }
    )
    run = RunRecord.model_validate(run_fields)
    config = definition.config
    case_id = f"simulation:{definition.profile.profile_id}@{definition.profile.version}"
    execution_seed = derive_validation_subseed(
        root_seed=root_seed,
        run_kind=run.kind,
        profile=definition.profile,
        case_id=case_id,
        replication_index=0,
    )
    base_binding = {
        "binding_schema_version": "simulation-expected-finding-binding@1",
        "producer_id": "economy_sim",
        "simulation_profile": definition.profile.model_dump(mode="json"),
        "execution_mode": "single_population@1",
        "seed_binding": {
            "root_seed": root_seed,
            "run_kind": run.kind.model_dump(mode="json"),
            "profile_id": definition.profile.profile_id,
            "profile_version": definition.profile.version,
            "case_id": case_id,
            "replication_index": 0,
            "seed": execution_seed,
            "seed_derivation_version": VALIDATION_SEED_DERIVATION_VERSION,
        },
        "constraint_snapshot_binding_status": "not_applicable",
        "constraint_ids": [],
        "constraint_application": {"status": "not_applicable"},
        "n_agents": config["default_population"],
        "n_ticks": config["default_horizon_steps"],
    }
    payload = {
        "profile_id": definition.profile.profile_id,
        "profile_version": definition.profile.version,
        "simulation_execution_binding": base_binding,
    }
    kwargs = {
        "run": run,
        "requirement_id": (
            f"simulation:{definition.profile.profile_id}@{definition.profile.version}"
        ),
        "engine_kind": "simulation",
        "engine_id": "economy_sim",
        "engine_version": str(definition.profile.version),
        "definition": definition,
    }

    assert _attested_executor_is_frozen(payload=payload, **kwargs)
    other_root_seed = root_seed + 1
    other_execution_seed = derive_validation_subseed(
        root_seed=other_root_seed,
        run_kind=run.kind,
        profile=definition.profile,
        case_id=case_id,
        replication_index=0,
    )
    other_seed_binding = {
        **base_binding["seed_binding"],
        "root_seed": other_root_seed,
        "seed": other_execution_seed,
    }
    assert not _attested_executor_is_frozen(
        payload={
            **payload,
            "simulation_execution_binding": {
                **base_binding,
                "seed_binding": other_seed_binding,
            },
        },
        **kwargs,
    )
    bound = {
        **base_binding,
        "constraint_snapshot_binding_status": "bound",
        "constraint_snapshot_artifact_id": "artifact:constraints",
        "constraint_ids": ["C_impossible"],
        "constraint_application": {
            "status": "unproven",
            "reason_code": "constraint_profile_not_executable",
        },
    }
    assert not _attested_executor_is_frozen(
        payload={**payload, "simulation_execution_binding": bound},
        **kwargs,
    )


@pytest.mark.parametrize(
    "variant",
    (
        "predicate",
        "predicate_schema",
        "engine_kind",
        "engine_id",
        "engine_version",
        "tool_version",
        "scope",
        "parents",
        "duplicate_attestation",
        "missing_oracle_attestation",
        "missing_outcome_attestation",
        "invalid_discriminator",
        "invalid_findings",
    ),
)
def test_runtime_guard_rejects_resealed_semantic_attestation_forgeries(
    variant: str,
) -> None:
    case = _forge_regression(_runtime_case(), variant)

    with pytest.raises(IntegrityViolation):
        case.service.validate_eligibility(case.request)


def test_runtime_guard_rejects_regression_bytes_under_an_old_hash() -> None:
    case = _runtime_case()
    original = case.authority.records[case.regression_artifact_id]
    records = dict(case.authority.records)
    records[case.regression_artifact_id] = replace(
        original,
        payload_bytes=original.payload_bytes + b" ",
    )
    forged = replace(case, authority=replace(case.authority, records=records))

    with pytest.raises(IntegrityViolation):
        forged.service.validate_eligibility(forged.request)


@dataclass(frozen=True)
class _RunAuthority:
    run: RunRecord

    def get(self, run_id: str) -> RunRecord | None:
        return self.run if run_id == self.run.run_id else None


def _gateway(case: _RuntimeCase, variant: str = "valid") -> ExactAutoApplyApprovalGateway:
    run = case.request.run
    proof_binding = case.item.auto_apply_proof
    evidence_id = case.item.evidence_set_artifact_id
    assert proof_binding is not None
    assert evidence_id is not None
    expected_outputs = (
        evidence_id,
        proof_binding.proof_artifact_id,
        *case.item.regression_evidence_artifact_ids,
    )
    parents = [
        *(
            RunManifestParentBindingV1(
                artifact_id=artifact_id,
                role="input",
                publication="existing",
            )
            for artifact_id in run.payload.input_artifact_ids
        ),
        RunManifestParentBindingV1(
            artifact_id=evidence_id,
            role="output",
            publication="run_published",
        ),
        RunManifestParentBindingV1(
            artifact_id=proof_binding.proof_artifact_id,
            role="evidence",
            publication="run_published",
        ),
        *(
            RunManifestParentBindingV1(
                artifact_id=artifact_id,
                role="output",
                publication="run_published",
            )
            for artifact_id in case.item.regression_evidence_artifact_ids
        ),
    ]
    outputs_override: tuple[str, ...] | None = None
    lineage_override: tuple[str, ...] | None = None
    reverse_parent_order = False
    attempt_no = 1
    result_kind = run.kind
    run_payload_hash = run.payload_hash
    frozen_tuple = run.payload.version_tuple
    terminal_tuple = run.payload.version_tuple
    artifact_tuple = terminal_tuple
    outcome_code = "patch_validation_auto_eligible"
    if variant == "attempt":
        attempt_no = 2
    elif variant == "run_kind":
        result_kind = RunKindRef(kind="patch.repair", version=1)
    elif variant == "run_payload_hash":
        run_payload_hash = "b" * 64
    elif variant == "frozen_tuple":
        frozen_tuple = VersionTuple(tool_version="forged@1")
    elif variant == "terminal_tuple":
        terminal_tuple = VersionTuple(tool_version="forged@1")
    elif variant == "extra_output":
        outputs_override = (*expected_outputs, "artifact:forged-extra")
    elif variant == "missing_output":
        outputs_override = tuple(
            artifact_id
            for artifact_id in expected_outputs
            if artifact_id != proof_binding.proof_artifact_id
        )
    elif variant == "empty_parents":
        parents = []
        outputs_override = expected_outputs
    elif variant == "mismatched_parent":
        parents = [
            parent.model_copy(update={"artifact_id": "artifact:forged-proof-parent"})
            if parent.artifact_id == proof_binding.proof_artifact_id
            else parent
            for parent in parents
        ]
    elif variant == "extra_parent":
        parents.append(
            RunManifestParentBindingV1(
                artifact_id="artifact:forged-extra-parent",
                role="output",
                publication="run_published",
            )
        )
    elif variant == "wrong_parent_role":
        parents = [
            parent.model_copy(update={"role": "input", "publication": "existing"})
            if parent.artifact_id == proof_binding.proof_artifact_id
            else parent
            for parent in parents
        ]
    elif variant == "proof_wrong_output_role":
        parents = [
            parent.model_copy(update={"role": "output"})
            if parent.artifact_id == proof_binding.proof_artifact_id
            else parent
            for parent in parents
        ]
    elif variant == "input_cassette_metadata":
        parents = [
            parent.model_copy(update={"cassette_scope": "replay_input"})
            if parent.artifact_id == run.payload.input_artifact_ids[0]
            else parent
            for parent in parents
        ]
    elif variant == "mismatched_lineage":
        lineage_override = tuple(
            parent.artifact_id
            for parent in parents
            if parent.artifact_id != run.payload.input_artifact_ids[0]
        )
    elif variant == "noncanonical_parent_order":
        reverse_parent_order = True
    elif variant == "ordinary_outcome":
        outcome_code = "patch_validation_passed"
    elif variant != "valid":  # pragma: no cover - test table controls this helper
        raise AssertionError(variant)
    projection = RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=attempt_no,
        run_kind=result_kind,
        run_payload_hash=run_payload_hash,
        frozen_input_version_tuple=frozen_tuple,
        terminal_version_tuple=terminal_tuple,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition",
            policy_version=1,
            digest="c" * 64,
        ),
        parents=tuple(parents),
    )
    projected_outputs = tuple(
        parent.artifact_id
        for parent in projection.parents
        if parent.publication == "run_published" and parent.role != "input"
    )
    outputs = projected_outputs if outputs_override is None else outputs_override
    result = RunResultV1(
        run_id=run.run_id,
        attempt_no=attempt_no,
        run_kind=result_kind,
        primary_artifact_id=evidence_id,
        produced_artifact_ids=outputs,
        finding_count=0,
        outcome_code=outcome_code,
        summary=RunResultSummaryV1(
            outcome_code=outcome_code,
            primary_artifact_kind="validation_evidence",
            produced_artifact_count=len(outputs),
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=projection,
    )
    result_payload: object = result
    if reverse_parent_order:
        raw_result = result.model_dump(mode="json")
        raw_parents = raw_result["version_projection"]["parents"]
        assert isinstance(raw_parents, list)
        raw_result["version_projection"]["parents"] = list(reversed(raw_parents))
        result_payload = raw_result
    result_record = _record(
        kind="run_result",
        schema="run-result@1",
        payload=result_payload,
        version_tuple=artifact_tuple,
        lineage=lineage_override or tuple(parent.artifact_id for parent in projection.parents),
        scope=("economy",),
    )
    terminal_payload = run.model_dump(mode="python")
    terminal_payload.update(
        {
            "status": "succeeded",
            "revision": run.revision + 1,
            "current_attempt_no": 1,
            "next_attempt_no": max(run.next_attempt_no, 2),
            "result_artifact_id": result_record.artifact.artifact_id,
            "updated_at": "2026-07-17T00:01:00Z",
        }
    )
    terminal_run = RunRecord.model_validate(terminal_payload)
    records = dict(case.authority.records)
    records[result_record.artifact.artifact_id] = result_record
    eligibility = ExactAutoApplyEligibilityService(
        authority=replace(case.authority, records=records)
    )
    return ExactAutoApplyApprovalGateway(
        eligibility=eligibility,
        runs=_RunAuthority(terminal_run),
    )


def test_gateway_accepts_exact_terminal_run_result_then_replays_the_pure_guard() -> None:
    case = _runtime_case()

    _gateway(case).validate_eligibility(item=case.item)


@pytest.mark.parametrize(
    "variant",
    (
        "attempt",
        "run_kind",
        "run_payload_hash",
        "frozen_tuple",
        "terminal_tuple",
        "extra_output",
        "missing_output",
        "empty_parents",
        "mismatched_parent",
        "extra_parent",
        "wrong_parent_role",
        "proof_wrong_output_role",
        "input_cassette_metadata",
        "mismatched_lineage",
        "noncanonical_parent_order",
        "ordinary_outcome",
    ),
)
def test_gateway_rejects_forged_terminal_run_result_authority(variant: str) -> None:
    case = _runtime_case()

    with pytest.raises(IntegrityViolation):
        _gateway(case, variant).validate_eligibility(item=case.item)
