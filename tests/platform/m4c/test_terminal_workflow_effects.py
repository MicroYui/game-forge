"""Task 9 terminal workflow-effect authority boundaries."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import DomainRegistryRefV1, DomainScope
from gameforge.contracts.jobs import (
    ConstraintProposalProposePayloadV1,
    GenerationProposePayloadV1,
    PatchRepairPayloadV1,
    PromptGoalBindingV1,
    RefReadBindingV1,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    ConstraintProposalV1,
    ConstraintSourceBinding,
    DomainRoutePolicyRefV1,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.publication.effects import (
    AgentDraftWorkflowRequest,
    WorkflowEffectContext,
    apply_workflow_effect,
    resolve_workflow_effect,
)
from gameforge.platform.registry.defaults import build_builtin_registry
from tests.platform.m4c.handler_support import (
    HUMAN,
    NOW,
    WORKER,
    build_envelope,
    build_run_record,
)


_HEX = "a" * 64
_DOMAIN = DomainScope(domain_ids=("economy",))
_DOMAIN_REF = DomainRegistryRefV1(registry_version="domains@1", registry_digest="1" * 64)


def _artifact(
    *, kind: str, schema: str, payload: dict[str, object], version_tuple: VersionTuple
) -> ArtifactV2:
    blob = canonical_json(payload).encode("utf-8")
    ref = object_ref_for_bytes(blob)
    return build_artifact_v2(
        kind=kind,
        version_tuple=version_tuple,
        lineage=(),
        payload_hash=ref.sha256,
        object_ref=ref,
        meta={"payload_schema_id": schema, "domain_scope": _DOMAIN.model_dump(mode="json")},
        created_at=NOW,
    )


def _policy(kind: RunKindRef, policy_id: str):
    definition = build_builtin_registry().get_run_kind(kind)
    assert definition is not None
    return next(item for item in definition.outcome_policies if item.policy_id == policy_id)


def _patch(*, run_id: str, revision: int = 1, supersedes: str | None = None) -> PatchV2:
    return PatchV2(
        revision=revision,
        supersedes_artifact_id=supersedes,
        base_snapshot_id="snapshot:base",
        target_snapshot_id=f"snapshot:preview:{revision}",
        expected_to_fix=[],
        side_effect_risk="low",
        ops=[],
        produced_by="agent",
        producer_run_id=run_id,
        rationale="bounded agent draft",
    )


def _approval_item(
    *,
    subject_kind: str,
    subject: ArtifactV2,
    revision: int,
    series_id: str,
    proposer: AuditActor,
    domain_scope: DomainScope,
    target: PatchTargetBindingV1 | None,
    supersedes_approval_id: str | None = None,
    workflow_revision: int = 1,
    status: str = "draft",
    evidence_set_artifact_id: str | None = None,
) -> ApprovalItem:
    return ApprovalItem(
        approval_id=f"approval:{subject_kind}:{subject.artifact_id}",
        subject_series_id=series_id,
        subject_revision=revision,
        subject_kind=subject_kind,
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        status=status,
        workflow_revision=workflow_revision,
        supersedes_approval_id=supersedes_approval_id,
        proposer=proposer,
        domain_scope=domain_scope,
        domain_registry_ref=_DOMAIN_REF,
        route_policy=DomainRoutePolicyRefV1(
            route_version="routes@1",
            route_digest="2" * 64,
            domain_registry_ref=_DOMAIN_REF,
        ),
        role_policy_version="roles@1",
        role_policy_digest="3" * 64,
        approval_policy=ApprovalPolicyRefV1(policy_version="approval@1", policy_digest="4" * 64),
        requirements=(),
        decisions=(),
        regression_evidence_artifact_ids=(),
        evidence_set_artifact_id=evidence_set_artifact_id,
        target_binding=target,
        created_at=NOW,
    )


class _Approvals:
    def __init__(self, item: ApprovalItem | None = None, head: SubjectHead | None = None) -> None:
        self.item = item
        self.head = head

    def get(self, approval_id: str) -> ApprovalItem | None:
        return self.item if self.item is not None and self.item.approval_id == approval_id else None

    def get_subject_head(self, subject_series_id: str) -> SubjectHead | None:
        return (
            self.head
            if self.head is not None and self.head.subject_series_id == subject_series_id
            else None
        )


@dataclass(frozen=True, slots=True)
class _PreparedDraft:
    approval_item: ApprovalItem
    expected_subject_head: SubjectHead | None
    result: object

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "approval_item": self.approval_item.model_dump(mode="json"),
            "expected_subject_head": (
                None
                if self.expected_subject_head is None
                else self.expected_subject_head.model_dump(mode="json")
            ),
        }


@dataclass
class _DraftPort:
    calls: list[AgentDraftWorkflowRequest]

    def publish_agent_draft(self, request: AgentDraftWorkflowRequest) -> object:
        self.calls.append(request)
        return self._result(request)

    def prepare_agent_draft(self, request: AgentDraftWorkflowRequest) -> _PreparedDraft:
        self.calls.append(request)
        result = self._result(request)
        return _PreparedDraft(
            approval_item=result.approval_item,
            expected_subject_head=request.expected_current_subject_head,
            result=result,
        )

    def preflight_prepared_agent_draft(
        self,
        *,
        prepared: _PreparedDraft,
        request: AgentDraftWorkflowRequest,
        merge_audit_into_terminal_batch: bool = False,
    ) -> _PreparedDraft:
        assert request == self.calls[-1]
        assert merge_audit_into_terminal_batch is False
        return prepared

    def apply_preflighted_agent_draft(
        self,
        *,
        preflighted: _PreparedDraft,
        request: AgentDraftWorkflowRequest,
    ) -> object:
        assert request == self.calls[-1]
        return preflighted.result

    @staticmethod
    def _result(request: AgentDraftWorkflowRequest) -> object:
        params = request.run.payload.params
        primary = request.artifacts_by_rule["primary"][0]
        payload = request.payloads_by_rule["primary"][0]
        if isinstance(params, ConstraintProposalProposePayloadV1):
            subject = ConstraintProposalV1.model_validate(payload)
            item = _approval_item(
                subject_kind="constraint_proposal",
                subject=primary,
                revision=subject.revision,
                series_id=f"series:constraint_proposal:{primary.artifact_id}",
                proposer=request.initiated_by,
                domain_scope=params.domain_scope,
                target=None,
            )
            head_revision = 1
        else:
            subject = PatchV2.model_validate(payload)
            preview = request.artifacts_by_rule["preview"][0]
            target = PatchTargetBindingV1(
                target_artifact_id=preview.artifact_id,
                target_snapshot_id=subject.target_snapshot_id,
                target_digest=preview.payload_hash,
                ref_name=params.target.ref_name,
                expected_ref=params.target.expected_ref,
            )
            previous = request.expected_current_approval
            item = _approval_item(
                subject_kind="patch",
                subject=primary,
                revision=subject.revision,
                series_id=(
                    previous.subject_series_id
                    if previous is not None
                    else f"series:patch:{primary.artifact_id}"
                ),
                proposer=request.initiated_by,
                domain_scope=(
                    previous.domain_scope if previous is not None else params.domain_scope
                ),
                target=target,
                supersedes_approval_id=(previous.approval_id if previous is not None else None),
            )
            head_revision = (
                request.expected_current_subject_head.revision + 1
                if request.expected_current_subject_head is not None
                else 1
            )
        from gameforge.platform.approvals.commands import DraftPublicationResult

        return DraftPublicationResult(
            approval_item=item,
            subject_head=SubjectHead(
                subject_series_id=item.subject_series_id,
                current_subject_artifact_id=item.subject_artifact_id,
                current_approval_id=item.approval_id,
                revision=head_revision,
            ),
        )


def _context(
    *, run, policy, artifacts_by_rule, payloads_by_rule, port=None, approvals=None
) -> WorkflowEffectContext:
    ids = {
        key: tuple(artifact.artifact_id for artifact in values)
        for key, values in artifacts_by_rule.items()
    }
    return WorkflowEffectContext(
        run=run,
        policy=policy,
        scope="run",
        published_primary_artifact_id=ids["primary"][0],
        published_output_artifact_ids=tuple(
            artifact_id for values in ids.values() for artifact_id in values
        ),
        approvals=approvals,
        actor=WORKER,
        occurred_at=NOW,
        published_primary_payload=payloads_by_rule["primary"][0],
        published_artifact_ids_by_rule=ids,
        published_payloads_by_rule=payloads_by_rule,
        published_artifacts_by_rule=artifacts_by_rule,
        agent_drafts=port,
    )


def _generation_case():
    run_id = "run:generation"
    params = GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:base",
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal", expected_payload_hash=_HEX
        ),
        domain_scope=_DOMAIN,
        target=RefReadBindingV1(
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=1),
        ),
        generation_policy=ProfileRefV1(profile_id="generation.default", version=1),
        candidate_export_profiles=(),
    )
    run = build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="generation.propose", version=1),
        run_id=run_id,
    )
    policy = _policy(run.kind, "generation-gate-pass")
    patch = _patch(run_id=run_id)
    primary = _artifact(
        kind="patch",
        schema="patch@2",
        payload=patch.model_dump(mode="json"),
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:base", tool_version="generation@1"),
    )
    preview = _artifact(
        kind="ir_snapshot",
        schema="ir-core@1",
        payload={"schema_version": "ir-core@1", "snapshot_id": patch.target_snapshot_id},
        version_tuple=VersionTuple(
            ir_snapshot_id=patch.target_snapshot_id, tool_version="generation@1"
        ),
    )
    artifacts = {rule.rule_id: () for rule in policy.artifact_rules}
    payloads = {rule.rule_id: () for rule in policy.artifact_rules}
    artifacts.update({"primary": (primary,), "preview": (preview,)})
    payloads.update(
        {
            "primary": (patch.model_dump(mode="json"),),
            "preview": ({"snapshot_id": patch.target_snapshot_id},),
        }
    )
    return run, policy, artifacts, payloads, _Approvals()


def _constraint_case():
    run_id = "run:constraint"
    params = ConstraintProposalProposePayloadV1(
        source_artifact_ids=("artifact:source",),
        domain_scope=_DOMAIN,
        authoring_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal", expected_payload_hash=_HEX
        ),
        dsl_grammar_version="constraint-dsl@1",
        extraction_policy=ProfileRefV1(profile_id="constraint.default", version=1),
    )
    run = build_run_record(
        build_envelope(params=params),
        RunKindRef(kind="constraint_proposal.propose", version=1),
        run_id=run_id,
    )
    policy = _policy(run.kind, "constraint-proposal-drafted")
    proposal = ConstraintProposalV1(
        revision=1,
        dsl_grammar_version=params.dsl_grammar_version,
        domain_scope=_DOMAIN,
        constraints=(),
        source_bindings=(
            ConstraintSourceBinding(source_artifact_id="artifact:source", provenance_hash=_HEX),
        ),
        produced_by="agent",
        producer_run_id=run_id,
        rationale="bounded extraction proposal",
    )
    primary = _artifact(
        kind="constraint_proposal",
        schema="constraint-proposal@1",
        payload=proposal.model_dump(mode="json"),
        version_tuple=VersionTuple(tool_version="constraint-proposal@1"),
    )
    return (
        run,
        policy,
        {"primary": (primary,)},
        {"primary": (proposal.model_dump(mode="json"),)},
        _Approvals(),
    )


def _repair_case():
    run_id = "run:repair"
    old_artifact = _artifact(
        kind="patch",
        schema="patch@2",
        payload=_patch(run_id="run:old", revision=3, supersedes="artifact:previous").model_dump(
            mode="json"
        ),
        version_tuple=VersionTuple(tool_version="patch@2"),
    )
    old_preview = _artifact(
        kind="ir_snapshot",
        schema="ir-core@1",
        payload={"schema_version": "ir-core@1"},
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:old", tool_version="patch@2"),
    )
    old = _approval_item(
        subject_kind="patch",
        subject=old_artifact,
        revision=3,
        series_id="series:patch:stable",
        proposer=HUMAN,
        domain_scope=DomainScope(domain_ids=("narrative",)),
        target=PatchTargetBindingV1(
            target_artifact_id=old_preview.artifact_id,
            target_snapshot_id="snapshot:old",
            target_digest=old_preview.payload_hash,
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=1),
        ),
        workflow_revision=7,
        status="validation_failed",
        evidence_set_artifact_id="artifact:evidence",
    )
    head = SubjectHead(
        subject_series_id=old.subject_series_id,
        current_subject_artifact_id=old.subject_artifact_id,
        current_approval_id=old.approval_id,
        revision=3,
    )
    params = PatchRepairPayloadV1(
        subject_patch_artifact_id=old.subject_artifact_id,
        expected_subject_head_revision=3,
        expected_workflow_revision=7,
        base_snapshot_artifact_id="artifact:base",
        preview_snapshot_artifact_id=old_preview.artifact_id,
        validation_evidence_artifact_id="artifact:evidence",
        findings=(),
        target=RefReadBindingV1(
            ref_name="content/head",
            expected_ref=RefValue(artifact_id="artifact:base", revision=1),
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
        run_id=run_id,
    )
    policy = _policy(run.kind, "repair-verified")
    patch = _patch(run_id=run_id, revision=4, supersedes=old.subject_artifact_id)
    primary = _artifact(
        kind="patch",
        schema="patch@2",
        payload=patch.model_dump(mode="json"),
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:base", tool_version="repair@1"),
    )
    preview = _artifact(
        kind="ir_snapshot",
        schema="ir-core@1",
        payload={"schema_version": "ir-core@1", "snapshot_id": patch.target_snapshot_id},
        version_tuple=VersionTuple(
            ir_snapshot_id=patch.target_snapshot_id, tool_version="repair@1"
        ),
    )
    artifacts = {rule.rule_id: () for rule in policy.artifact_rules}
    payloads = {rule.rule_id: () for rule in policy.artifact_rules}
    artifacts.update({"primary": (primary,), "preview": (preview,)})
    payloads.update(
        {
            "primary": (patch.model_dump(mode="json"),),
            "preview": ({"snapshot_id": patch.target_snapshot_id},),
        }
    )
    return run, policy, artifacts, payloads, _Approvals(old, head)


@pytest.mark.parametrize(
    ("effect_key", "factory"),
    (
        ("create_patch_subject_head_and_draft@1", _generation_case),
        ("supersede_patch_head_create_draft@1", _repair_case),
        ("create_constraint_subject_head_and_draft@1", _constraint_case),
    ),
)
def test_agent_draft_effects_delegate_exact_final_authority(effect_key, factory) -> None:
    run, policy, artifacts, payloads, approvals = factory()
    port = _DraftPort([])
    apply_workflow_effect(
        effect_key,
        _context(
            run=run,
            policy=policy,
            artifacts_by_rule=artifacts,
            payloads_by_rule=payloads,
            port=port,
            approvals=approvals,
        ),
    )

    assert len(port.calls) == 1
    request = port.calls[0]
    assert request.initiated_by == run.initiated_by
    assert request.executed_by == WORKER
    assert request.subject_artifact_id == artifacts["primary"][0].artifact_id
    assert request.artifacts_by_rule == artifacts
    if isinstance(run.payload.params, PatchRepairPayloadV1):
        assert request.expected_current_approval is approvals.item
        assert request.expected_current_subject_head is approvals.head
        assert request.expected_subject_head_revision == 3
        assert request.expected_workflow_revision == 7


@pytest.mark.parametrize(
    ("effect_key", "factory"),
    (
        ("create_patch_subject_head_and_draft@1", _generation_case),
        ("supersede_patch_head_create_draft@1", _repair_case),
        ("create_constraint_subject_head_and_draft@1", _constraint_case),
    ),
)
def test_agent_draft_effects_fail_closed_without_transaction_port(effect_key, factory) -> None:
    run, policy, artifacts, payloads, approvals = factory()
    with pytest.raises(IntegrityViolation, match="authority port"):
        apply_workflow_effect(
            effect_key,
            _context(
                run=run,
                policy=policy,
                artifacts_by_rule=artifacts,
                payloads_by_rule=payloads,
                approvals=approvals,
            ),
        )


@pytest.mark.parametrize(
    "drift",
    (
        "workflow_revision",
        "status",
        "evidence",
        "last_failure",
        "head_subject_revision",
        "preview",
        "ref_name",
        "expected_ref",
    ),
)
def test_repair_effect_rejects_admission_authority_drift_before_port(drift: str) -> None:
    run, policy, artifacts, payloads, approvals = _repair_case()
    assert approvals.item is not None
    item_updates: dict[str, object] = {}
    target = approvals.item.target_binding
    assert isinstance(target, PatchTargetBindingV1)
    if drift == "workflow_revision":
        item_updates["workflow_revision"] = run.payload.params.expected_workflow_revision + 1
    elif drift == "status":
        item_updates["status"] = "draft"
    elif drift == "evidence":
        item_updates["evidence_set_artifact_id"] = "artifact:other-evidence"
    elif drift == "last_failure":
        item_updates["last_validation_failure_artifact_id"] = "artifact:failure"
    elif drift == "head_subject_revision":
        item_updates["subject_revision"] = approvals.item.subject_revision - 1
    else:
        target_update: dict[str, object]
        if drift == "preview":
            target_update = {"target_artifact_id": "artifact:other-preview"}
        elif drift == "ref_name":
            target_update = {"ref_name": "other/head"}
        else:
            target_update = {
                "expected_ref": RefValue(artifact_id="artifact:other-base", revision=1)
            }
        item_updates["target_binding"] = target.model_copy(update=target_update)
    approvals.item = approvals.item.model_copy(update=item_updates)
    port = _DraftPort([])
    with pytest.raises(IntegrityViolation, match="current ApprovalItem/SubjectHead CAS"):
        apply_workflow_effect(
            "supersede_patch_head_create_draft@1",
            _context(
                run=run,
                policy=policy,
                artifacts_by_rule=artifacts,
                payloads_by_rule=payloads,
                port=port,
                approvals=approvals,
            ),
        )
    assert port.calls == []


def test_repair_effect_rejects_authority_result_that_does_not_inherit_domain() -> None:
    run, policy, artifacts, payloads, approvals = _repair_case()

    class _ForgedDomainPort(_DraftPort):
        def apply_preflighted_agent_draft(
            self,
            *,
            preflighted: _PreparedDraft,
            request: AgentDraftWorkflowRequest,
        ) -> object:
            result = super().apply_preflighted_agent_draft(
                preflighted=preflighted,
                request=request,
            )
            return result.model_copy(
                update={
                    "approval_item": result.approval_item.model_copy(
                        update={"domain_scope": DomainScope(domain_ids=("economy",))}
                    )
                }
            )

    port = _ForgedDomainPort([])
    with pytest.raises(IntegrityViolation, match="committed another projection"):
        apply_workflow_effect(
            "supersede_patch_head_create_draft@1",
            _context(
                run=run,
                policy=policy,
                artifacts_by_rule=artifacts,
                payloads_by_rule=payloads,
                port=port,
                approvals=approvals,
            ),
        )


def test_active_registry_agent_effect_keys_resolve_to_callables() -> None:
    for key in (
        "create_patch_subject_head_and_draft@1",
        "supersede_patch_head_create_draft@1",
        "create_constraint_subject_head_and_draft@1",
    ):
        assert callable(resolve_workflow_effect(key))
