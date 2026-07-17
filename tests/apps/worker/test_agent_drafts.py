from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType, SimpleNamespace

import pytest
from sqlalchemy.orm import Session

import gameforge.apps.worker.app as worker_app
from gameforge.apps.worker.agent_drafts import (
    WorkerAgentDraftGovernanceRefs,
    WorkerAgentDraftPreparedAssembler,
    build_agent_draft_workflow_port,
)
from gameforge.apps.worker.app import LocalWorkerConfig, WorkerConfigurationError
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    GenerationProposePayloadV1,
    PatchRepairPayloadV1,
    PromptGoalBindingV1,
    RefReadBindingV1,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    ObjectBinding,
    ObjectLocation,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalPolicyRegistryV1,
    FindingEvidenceBindingV1,
    SubjectHead,
    compute_approval_policy_registry_digest,
)
from gameforge.platform.approvals.commands import DraftPublicationResult
from gameforge.platform.publication.effects import AgentDraftWorkflowRequest
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.workflow.service import WorkflowGovernance
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from tests.platform.m4 import apply_testkit
from tests.platform.m4c.handler_support import HUMAN, NOW, WORKER, build_envelope, build_run_record


_SCOPE = DomainScope(domain_ids=("economy",))
_CLOCK = FrozenUtcClock(apply_testkit.NOW_DT)


class _Artifacts:
    def __init__(self, artifacts: tuple[ArtifactV2, ...]) -> None:
        self.values = {artifact.artifact_id: artifact for artifact in artifacts}

    def get(self, artifact_id: str) -> ArtifactV2 | None:
        return self.values.get(artifact_id)


class _Bindings:
    def __init__(self, bindings: tuple[ObjectBinding, ...]) -> None:
        self.values = {binding.object_ref.key: binding for binding in bindings}

    def resolve(self, object_ref: object) -> ObjectBinding:
        return self.values[object_ref.key]  # type: ignore[attr-defined]


class _Governance:
    def __init__(self, value: WorkflowGovernance) -> None:
        self.value = value

    def current(self) -> WorkflowGovernance:
        return self.value


class _Policies:
    def __init__(self, governance: WorkflowGovernance) -> None:
        self.governance = governance

    def get_role_policy(self, version: str, digest: str):
        roles = self.governance.roles
        return roles if (version, digest) == (roles.policy_version, roles.policy_digest) else None

    def get_domain_registry(self, ref):
        registry = self.governance.registry
        return registry if ref == self.governance.roles.domain_registry_ref else None

    def get_domain_route_policy(self, ref):
        return self.governance.route if ref == self.governance.route_ref() else None

    def get_approval_policy(self, ref):
        return self.governance.approval if ref == self.governance.approval_ref() else None


@dataclass
class _FakeCommands:
    prepared: object | None = None
    capabilities: object | None = None

    def publish_draft_in_transaction(self, *, prepared, context, capabilities):
        del context
        self.prepared = prepared
        self.capabilities = capabilities
        item = prepared.approval_item
        expected = prepared.expected_subject_head
        return DraftPublicationResult(
            approval_item=item,
            subject_head=SubjectHead(
                subject_series_id=item.subject_series_id,
                current_subject_artifact_id=item.subject_artifact_id,
                current_approval_id=item.approval_id,
                revision=1 if expected is None else expected.revision + 1,
            ),
        )


def _governance() -> WorkflowGovernance:
    registry = apply_testkit._registry()
    return WorkflowGovernance(
        registry=registry,
        route=apply_testkit._route(registry),
        roles=apply_testkit._roles(registry),
        approval=apply_testkit._approval_policy(),
    )


def _governance_refs(governance: WorkflowGovernance) -> WorkerAgentDraftGovernanceRefs:
    return WorkerAgentDraftGovernanceRefs(
        role_policy_version=governance.roles.policy_version,
        role_policy_digest=governance.roles.policy_digest,
        route_policy_version=governance.route.route_version,
        route_policy_digest=governance.route.route_digest,
        approval_policy_version=governance.approval.policy_version,
        approval_policy_digest=governance.approval.policy_digest,
    )


def _artifact(
    *,
    kind: str,
    payload: object,
    version_tuple: VersionTuple,
    lineage: tuple[str, ...],
    schema_id: str,
) -> tuple[ArtifactV2, ObjectBinding]:
    blob = canonical_json(payload).encode("utf-8")
    object_ref = object_ref_for_bytes(blob)
    artifact = build_artifact_v2(
        kind=kind,  # type: ignore[arg-type]
        version_tuple=version_tuple,
        lineage=lineage,
        payload_hash=object_ref.sha256,
        object_ref=object_ref,
        meta={"payload_schema_id": schema_id},
        created_at=NOW,
    )
    binding = ObjectBinding(
        object_ref=object_ref,
        location=ObjectLocation(
            store_id="local:test",
            key=object_ref.key,
            backend_generation=f"generation:{object_ref.sha256}",
        ),
        status="active",
        revision=3,
        verified_at=NOW,
    )
    return artifact, binding


def _policy(run_kind: str, policy_id: str):
    definition = build_builtin_registry().get_run_kind(RunKindRef(kind=run_kind, version=1))
    assert definition is not None
    return next(policy for policy in definition.outcome_policies if policy.policy_id == policy_id)


def _request(
    *,
    effect_key: str,
    run,
    policy,
    subject: ArtifactV2,
    subject_payload: dict[str, object],
    preview: ArtifactV2 | None,
    current_item=None,
    current_head=None,
) -> AgentDraftWorkflowRequest:
    empty_artifacts = {rule.rule_id: () for rule in policy.artifact_rules}
    artifacts_by_rule = {
        **empty_artifacts,
        "primary": (subject,),
        **({} if preview is None else {"preview": (preview,)}),
    }
    empty_payloads = {rule.rule_id: () for rule in policy.artifact_rules}
    payloads_by_rule = {
        **empty_payloads,
        "primary": (subject_payload,),
        **({} if preview is None else {"preview": ({"snapshot": "preview"},)}),
    }
    return AgentDraftWorkflowRequest(
        effect_key=effect_key,  # type: ignore[arg-type]
        run=run,
        policy=policy,
        initiated_by=run.initiated_by,
        executed_by=WORKER,
        subject_artifact_id=subject.artifact_id,
        artifacts_by_rule=MappingProxyType(artifacts_by_rule),
        artifact_ids_by_rule=MappingProxyType(
            {
                rule_id: tuple(artifact.artifact_id for artifact in artifacts)
                for rule_id, artifacts in artifacts_by_rule.items()
            }
        ),
        payloads_by_rule=MappingProxyType(payloads_by_rule),
        expected_subject_head_revision=None if current_head is None else current_head.revision,
        expected_workflow_revision=(
            None if current_item is None else current_item.workflow_revision
        ),
        expected_current_approval=current_item,
        expected_current_subject_head=current_head,
        occurred_at=NOW,
    )


def _generation_material():
    params = GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:base",
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal",
            expected_payload_hash="a" * 64,
        ),
        domain_scope=_SCOPE,
        target=RefReadBindingV1(
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=7),
        ),
        generation_policy=ProfileRefV1(profile_id="generation.default", version=1),
        candidate_export_profiles=(),
    )
    run = build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="generation.propose", version=1),
        run_id="run:generation:1",
    ).model_copy(update={"resource_domain_scope": _SCOPE})
    patch = PatchV2(
        revision=1,
        base_snapshot_id="snapshot:base",
        target_snapshot_id="snapshot:preview",
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[],
        produced_by="agent",
        producer_run_id=run.run_id,
        rationale="gated generation",
    )
    subject, subject_binding = _artifact(
        kind="patch",
        payload=patch.model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=patch.base_snapshot_id,
            tool_version="generation@1",
        ),
        lineage=("artifact:base", "artifact:goal"),
        schema_id="patch@2",
    )
    preview, preview_binding = _artifact(
        kind="ir_snapshot",
        payload={"snapshot": "preview"},
        version_tuple=VersionTuple(
            ir_snapshot_id=patch.target_snapshot_id,
            tool_version="generation@1",
        ),
        lineage=("artifact:base", subject.artifact_id),
        schema_id="ir-core@1",
    )
    policy = _policy("generation.propose", "generation-gate-pass")
    request = _request(
        effect_key="create_patch_subject_head_and_draft@1",
        run=run,
        policy=policy,
        subject=subject,
        subject_payload=patch.model_dump(mode="json"),
        preview=preview,
    )
    return request, (subject, preview), (subject_binding, preview_binding)


def test_port_assembles_exact_published_generation_artifacts_and_run_scope() -> None:
    request, artifacts, bindings = _generation_material()
    governance = _governance()
    repository = _Artifacts(artifacts)
    binding_repository = _Bindings(bindings)
    commands = _FakeCommands()
    transaction = SimpleNamespace(
        artifacts=repository,
        object_bindings=binding_repository,
        policies=_Policies(governance),
        approvals=object(),
        idempotency=object(),
        audit=object(),
        runs=object(),
        refs=object(),
    )
    port = build_agent_draft_workflow_port(
        transaction=transaction,
        object_store=object(),
        clock=_CLOCK,
        commands=commands,  # type: ignore[arg-type]
        governance_refs=_governance_refs(governance),
    )

    result = port.publish_agent_draft(request)

    prepared = commands.prepared
    assert prepared is not None
    assert prepared.subject_artifact == artifacts[0]  # type: ignore[attr-defined]
    assert prepared.companion_artifacts == (artifacts[1],)  # type: ignore[attr-defined]
    assert prepared.approval_item.domain_scope == request.run.resource_domain_scope  # type: ignore[attr-defined]
    assert prepared.approval_item.proposer == HUMAN  # type: ignore[attr-defined]
    assert prepared.approval_item.target_binding.target_artifact_id == artifacts[1].artifact_id  # type: ignore[attr-defined,union-attr]
    assert {binding.expected_revision for binding in prepared.object_bindings} == {3}  # type: ignore[attr-defined]
    assert result.approval_item == prepared.approval_item  # type: ignore[attr-defined]


def test_assembler_rejects_payload_or_run_domain_substitution() -> None:
    request, artifacts, bindings = _generation_material()
    assembler = WorkerAgentDraftPreparedAssembler(
        artifacts=_Artifacts(artifacts),
        object_bindings=_Bindings(bindings),
        governance=_Governance(_governance()),
    )
    tampered_payloads = dict(request.payloads_by_rule)
    tampered_payloads["primary"] = ({"patch_schema_version": "patch@2"},)
    with pytest.raises(IntegrityViolation, match="payload differs"):
        assembler.prepare(replace(request, payloads_by_rule=MappingProxyType(tampered_payloads)))

    mismatched_run = request.run.model_copy(
        update={"resource_domain_scope": DomainScope(domain_ids=("combat",))}
    )
    with pytest.raises(IntegrityViolation, match="payload domain differs"):
        assembler.prepare(replace(request, run=mismatched_run))


def test_assembler_rejects_document_version_drift_across_agent_patch_outputs() -> None:
    request, artifacts, _ = _generation_material()
    subject, preview = artifacts
    patch = PatchV2.model_validate(request.payloads_by_rule["primary"][0])
    drifted_preview = preview.model_copy(
        update={
            "version_tuple": preview.version_tuple.model_copy(
                update={"doc_version": "another-doc@1"}
            )
        }
    )

    with pytest.raises(IntegrityViolation, match="exact Run authority"):
        WorkerAgentDraftPreparedAssembler._validate_patch(
            request,
            subject,
            patch,
            (drifted_preview,),
        )

    drifted_config, _ = _artifact(
        kind="config_export",
        payload={"package_schema_version": "config-export-package@1"},
        version_tuple=VersionTuple(
            doc_version="another-doc@1",
            ir_snapshot_id=preview.version_tuple.ir_snapshot_id,
            constraint_snapshot_id="constraint@1",
            tool_version="config-export@1",
        ),
        lineage=(preview.artifact_id, "artifact:constraint"),
        schema_id="config-export-package@1",
    )
    with pytest.raises(IntegrityViolation, match="exact Run authority"):
        WorkerAgentDraftPreparedAssembler._validate_patch(
            request,
            subject,
            patch,
            (preview, drifted_config),
        )


def test_assembler_preserves_repair_series_and_both_cas_revisions() -> None:
    initial_request, initial_artifacts, initial_bindings = _generation_material()
    initial = WorkerAgentDraftPreparedAssembler(
        artifacts=_Artifacts(initial_artifacts),
        object_bindings=_Bindings(initial_bindings),
        governance=_Governance(_governance()),
    ).prepare(initial_request)
    current = initial.approval_item.model_copy(
        update={
            "status": "validation_failed",
            "workflow_revision": 4,
            "evidence_set_artifact_id": "artifact:validation-evidence",
        }
    )
    head = SubjectHead(
        subject_series_id=current.subject_series_id,
        current_subject_artifact_id=current.subject_artifact_id,
        current_approval_id=current.approval_id,
        revision=1,
    )
    params = PatchRepairPayloadV1(
        subject_patch_artifact_id=current.subject_artifact_id,
        expected_subject_head_revision=head.revision,
        expected_workflow_revision=current.workflow_revision,
        base_snapshot_artifact_id="artifact:base",
        preview_snapshot_artifact_id=initial_artifacts[1].artifact_id,
        validation_evidence_artifact_id="artifact:validation-evidence",
        findings=(
            FindingEvidenceBindingV1(
                finding_id="finding:one",
                finding_revision=1,
                evidence_artifact_id="artifact:validation-evidence",
                finding_digest="f" * 64,
            ),
        ),
        target=RefReadBindingV1(
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=7),
        ),
        repair_policy=ProfileRefV1(profile_id="repair.default", version=1),
        checker_profiles=(),
        simulation_profiles=(),
        regression_suite_artifact_ids=(),
        candidate_export_profiles=(),
    )
    run = build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="patch.repair", version=1),
        run_id="run:repair:1",
    ).model_copy(update={"resource_domain_scope": _SCOPE})
    patch = PatchV2(
        revision=2,
        supersedes_artifact_id=current.subject_artifact_id,
        base_snapshot_id="snapshot:base",
        target_snapshot_id="snapshot:repair-preview",
        expected_to_fix=["finding:one"],
        preconditions=[],
        side_effect_risk="low",
        ops=[],
        produced_by="agent",
        producer_run_id=run.run_id,
        rationale="verified repair",
    )
    subject, subject_binding = _artifact(
        kind="patch",
        payload=patch.model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=patch.base_snapshot_id,
            tool_version="repair@1",
        ),
        lineage=("artifact:base", current.subject_artifact_id),
        schema_id="patch@2",
    )
    preview, preview_binding = _artifact(
        kind="ir_snapshot",
        payload={"snapshot": "repair-preview"},
        version_tuple=VersionTuple(
            ir_snapshot_id=patch.target_snapshot_id,
            tool_version="repair@1",
        ),
        lineage=("artifact:base", subject.artifact_id),
        schema_id="ir-core@1",
    )
    request = _request(
        effect_key="supersede_patch_head_create_draft@1",
        run=run,
        policy=_policy("patch.repair", "repair-verified"),
        subject=subject,
        subject_payload=patch.model_dump(mode="json"),
        preview=preview,
        current_item=current,
        current_head=head,
    )

    prepared = WorkerAgentDraftPreparedAssembler(
        artifacts=_Artifacts((subject, preview)),
        object_bindings=_Bindings((subject_binding, preview_binding)),
        governance=_Governance(_governance()),
    ).prepare(request)

    assert prepared.approval_item.subject_series_id == current.subject_series_id
    assert prepared.approval_item.supersedes_approval_id == current.approval_id
    assert prepared.expected_subject_head == head
    assert prepared.expected_previous_workflow_revision == current.workflow_revision


def test_missing_governance_pointers_leave_a_real_fail_closed_port() -> None:
    request, artifacts, bindings = _generation_material()
    transaction = SimpleNamespace(
        artifacts=_Artifacts(artifacts),
        object_bindings=_Bindings(bindings),
        policies=object(),
        approvals=object(),
        idempotency=object(),
        audit=object(),
        runs=object(),
        refs=object(),
    )
    port = build_agent_draft_workflow_port(
        transaction=transaction,
        object_store=object(),
        clock=_CLOCK,
        commands=_FakeCommands(),  # type: ignore[arg-type]
        governance_refs=None,
    )

    with pytest.raises(DependencyUnavailable, match="governance pointers"):
        port.publish_agent_draft(request)


def test_worker_config_accepts_only_a_complete_governance_pointer_set(tmp_path) -> None:
    base = {
        "database_url": f"sqlite:///{tmp_path / 'worker.db'}",
        "object_store_root": tmp_path / "objects",
        "object_store_id": "local:test",
        "telemetry_db_path": tmp_path / "telemetry.db",
        "worker_principal_id": "service:worker:1",
        "reaper_principal_id": "system:reaper",
        "root_secret": b"0" * 32,
    }
    with pytest.raises(WorkerConfigurationError, match="provided together"):
        LocalWorkerConfig(**base, role_policy_version="roles@1")

    governance = _governance()
    config = LocalWorkerConfig(
        **base,
        role_policy_version=governance.roles.policy_version,
        role_policy_digest=governance.roles.policy_digest,
        workflow_route_policy_version=governance.route.route_version,
        workflow_route_policy_digest=governance.route.route_digest,
        workflow_approval_policy_version=governance.approval.policy_version,
        workflow_approval_policy_digest=governance.approval.policy_digest,
    )
    assert config.workflow_route_policy_digest == governance.route.route_digest


def test_worker_governance_readiness_requires_exact_retained_authority(tmp_path) -> None:
    base = {
        "database_url": f"sqlite:///{tmp_path / 'worker-governance.db'}",
        "object_store_root": tmp_path / "objects",
        "object_store_id": "local:test",
        "telemetry_db_path": tmp_path / "telemetry.db",
        "worker_principal_id": "service:worker:1",
        "reaper_principal_id": "system:reaper",
        "root_secret": b"0" * 32,
    }
    engine = get_engine(base["database_url"])
    Base.metadata.create_all(engine)
    try:
        missing = LocalWorkerConfig(**base)
        with pytest.raises(WorkerConfigurationError, match="pointers are required"):
            worker_app._validate_worker_workflow_governance(  # type: ignore[attr-defined]
                SimpleNamespace(config=missing, engine=engine)
            )

        governance = _governance()
        configured = LocalWorkerConfig(
            **base,
            role_policy_version=governance.roles.policy_version,
            role_policy_digest=governance.roles.policy_digest,
            workflow_route_policy_version=governance.route.route_version,
            workflow_route_policy_digest=governance.route.route_digest,
            workflow_approval_policy_version=governance.approval.policy_version,
            workflow_approval_policy_digest=governance.approval.policy_digest,
        )
        runtime = SimpleNamespace(config=configured, engine=engine)
        with pytest.raises(WorkerConfigurationError, match="unavailable or inconsistent"):
            worker_app._validate_worker_workflow_governance(runtime)  # type: ignore[attr-defined]

        approval_registry = ApprovalPolicyRegistryV1(
            policies=(governance.approval,),
            registry_digest=compute_approval_policy_registry_digest((governance.approval,)),
        )
        with Session(engine) as session, session.begin():
            policies = SqlPolicySnapshotRepository(session, clock=_CLOCK)
            policies.put_domain_registry(governance.registry)
            policies.put_role_policy(governance.roles)
            policies.put_domain_route_policy(governance.route)
            policies.put_approval_policy_registry(approval_registry)

        worker_app._validate_worker_workflow_governance(runtime)  # type: ignore[attr-defined]
    finally:
        engine.dispose()
