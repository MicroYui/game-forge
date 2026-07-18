"""Transaction-bound Agent draft workflow composition for the persistent worker.

Terminal publication has already published and verified the final domain Artifacts
when its workflow effect runs.  This module turns only those retained Artifacts, the
admission-resolved Run domain scope, immutable governance snapshots, and the current
repair CAS into a complete :class:`PreparedDraft`.  The resulting port delegates the
actual ApprovalItem/SubjectHead mutation to the canonical Task-7 command service in
the caller-owned terminal UoW; it never opens another write transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import (
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRoutePolicyRefV1,
    DomainScope,
    RolePolicy,
)
from gameforge.contracts.jobs import (
    ConstraintProposalProposePayloadV1,
    GenerationProposePayloadV1,
    PatchRepairPayloadV1,
    RunRecord,
)
from gameforge.contracts.lineage import ArtifactV2, AuditActor
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalPolicyV1,
    ConstraintProposalV1,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.approvals import build_approval_requirements
from gameforge.platform.approvals.commands import (
    ApprovalCommandCapabilities,
    ApprovalCommandService,
    DraftSubjectFacts,
    PreparedDraft,
    PreparedObjectBinding,
    PreparedTerminalDraft,
    PreparedValidationStart,
)
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.publication.effects import (
    AgentDraftWorkflowRequest,
    ApprovalCommandAgentDraftWorkflowPort,
)
from gameforge.platform.workflow.readers import (
    WorkflowDraftLineageVerifier,
    WorkflowTypedReaders,
)
from gameforge.platform.workflow.service import WorkflowGovernance, WorkflowGovernanceProvider


_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class WorkerAgentDraftGovernanceRefs:
    """Configured exact governance pointers shared with the API command surface."""

    role_policy_version: str
    role_policy_digest: str
    route_policy_version: str
    route_policy_digest: str
    approval_policy_version: str
    approval_policy_digest: str

    def __post_init__(self) -> None:
        for field_name in (
            "role_policy_version",
            "route_policy_version",
            "approval_policy_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError(f"{field_name} must be a non-empty bounded string")
        for field_name in (
            "role_policy_digest",
            "route_policy_digest",
            "approval_policy_digest",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _LOWER_SHA256.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class TransactionWorkflowGovernanceProvider:
    """Resolve all four governance documents from the active terminal transaction."""

    policies: object
    refs: WorkerAgentDraftGovernanceRefs

    def current(self) -> WorkflowGovernance:
        roles = self.policies.get_role_policy(  # type: ignore[attr-defined]
            self.refs.role_policy_version,
            self.refs.role_policy_digest,
        )
        if not isinstance(roles, RolePolicy):
            raise DependencyUnavailable(
                "worker Agent draft role policy is unavailable",
                component="workflow_governance",
            )
        registry = self.policies.get_domain_registry(  # type: ignore[attr-defined]
            roles.domain_registry_ref
        )
        if not isinstance(registry, DomainRegistryV1):
            raise DependencyUnavailable(
                "worker Agent draft domain registry is unavailable",
                component="workflow_governance",
            )
        route = self.policies.get_domain_route_policy(  # type: ignore[attr-defined]
            DomainRoutePolicyRefV1(
                route_version=self.refs.route_policy_version,
                route_digest=self.refs.route_policy_digest,
                domain_registry_ref=roles.domain_registry_ref,
            )
        )
        if not isinstance(route, DomainRoutePolicy):
            raise DependencyUnavailable(
                "worker Agent draft route policy is unavailable",
                component="workflow_governance",
            )
        approval = self.policies.get_approval_policy(  # type: ignore[attr-defined]
            ApprovalPolicyRefV1(
                policy_version=self.refs.approval_policy_version,
                policy_digest=self.refs.approval_policy_digest,
            )
        )
        if not isinstance(approval, ApprovalPolicyV1):
            raise DependencyUnavailable(
                "worker Agent draft approval policy is unavailable",
                component="workflow_governance",
            )
        return WorkflowGovernance(
            registry=registry,
            route=route,
            roles=roles,
            approval=approval,
        )


class _UnavailableWorkflowGovernanceProvider:
    def current(self) -> WorkflowGovernance:
        raise DependencyUnavailable(
            "worker Agent draft governance pointers are unavailable",
            component="workflow_governance",
        )


@dataclass(frozen=True, slots=True)
class WorkerAgentDraftPreparedAssembler:
    """Assemble one exact initial/superseding Agent draft without writing state."""

    artifacts: object
    object_bindings: object
    governance: WorkflowGovernanceProvider

    def prepare(self, request: AgentDraftWorkflowRequest) -> PreparedDraft:
        subject, companions, _facts, item, expected_head, expected_workflow_revision = (
            self._prepare_projection(request, require_published=True)
        )
        return PreparedDraft(
            subject_artifact=subject,
            companion_artifacts=companions,
            object_bindings=self._retained_bindings((subject, *companions)),
            approval_item=item,
            expected_subject_head=expected_head,
            expected_previous_workflow_revision=expected_workflow_revision,
        )

    def prepare_terminal(self, request: AgentDraftWorkflowRequest) -> PreparedTerminalDraft:
        """Prepare new terminal Artifacts before they exist in ArtifactRepository.

        The generic terminal planner has already validated every planned Artifact
        and payload.  This method validates the workflow-specific subject/preview
        closure plus immutable retained lineage using the read snapshot; it never
        requires an active binding for the newly staged outputs.
        """

        subject, companions, facts, item, expected_head, expected_workflow_revision = (
            self._prepare_projection(request, require_published=False)
        )
        retained_parent_ids = self._retained_parent_ids((subject, *companions))
        WorkerAgentDraftLineageVerifier().validate_terminal_draft_publication(
            subject_artifact=subject,
            companion_artifacts=companions,
            retained_parent_ids=retained_parent_ids,
        )
        return PreparedTerminalDraft.seal(
            subject_artifact=subject,
            companion_artifacts=companions,
            subject_facts=facts,
            retained_parent_ids=retained_parent_ids,
            approval_item=item,
            expected_subject_head=expected_head,
            expected_previous_workflow_revision=expected_workflow_revision,
        )

    def _prepare_projection(
        self,
        request: AgentDraftWorkflowRequest,
        *,
        require_published: bool,
    ) -> tuple[
        ArtifactV2,
        tuple[ArtifactV2, ...],
        DraftSubjectFacts,
        ApprovalItem,
        SubjectHead | None,
        int | None,
    ]:
        subject, payload = self._subject(request, require_published=require_published)
        companions = self._companions(request, require_published=require_published)
        scope = self._domain_scope(request)
        governance = self.governance.current()

        if request.effect_key == "create_constraint_subject_head_and_draft@1":
            proposal = ConstraintProposalV1.model_validate(payload)
            self._validate_constraint(request, subject, proposal, companions, scope)
            subject_kind = "constraint_proposal"
            target_binding = None
            subject_revision = proposal.revision
            series_id = f"series:constraint_proposal:{subject.artifact_id}"
            supersedes_approval_id = None
            expected_head = None
            expected_workflow_revision = None
            facts = DraftSubjectFacts(
                subject_kind="constraint_proposal",
                subject_revision=proposal.revision,
                produced_by=proposal.produced_by,
                producer_run_id=proposal.producer_run_id,
                supersedes_artifact_id=proposal.supersedes_artifact_id,
                target_artifact_id=None,
                target_snapshot_id=None,
            )
        else:
            patch = PatchV2.model_validate(payload)
            preview = self._validate_patch(
                request,
                subject,
                patch,
                companions,
            )
            params = request.run.payload.params
            if not isinstance(params, (GenerationProposePayloadV1, PatchRepairPayloadV1)):
                raise IntegrityViolation("Agent Patch draft has another Run payload")
            subject_kind = "patch"
            target_binding = PatchTargetBindingV1(
                target_artifact_id=preview.artifact_id,
                target_snapshot_id=patch.target_snapshot_id,
                target_digest=preview.payload_hash,
                ref_name=params.target.ref_name,
                expected_ref=params.target.expected_ref,
            )
            subject_revision = patch.revision
            if isinstance(params, PatchRepairPayloadV1):
                current_item, current_head = self._repair_cas(request)
                series_id = current_item.subject_series_id
                supersedes_approval_id = current_item.approval_id
                expected_head = current_head
                expected_workflow_revision = current_item.workflow_revision
            else:
                self._require_initial_cas(request)
                series_id = f"series:patch:{subject.artifact_id}"
                supersedes_approval_id = None
                expected_head = None
                expected_workflow_revision = None
            facts = DraftSubjectFacts(
                subject_kind="patch",
                subject_revision=patch.revision,
                produced_by=patch.produced_by,
                producer_run_id=patch.producer_run_id,
                supersedes_artifact_id=patch.supersedes_artifact_id,
                target_artifact_id=None,
                target_snapshot_id=patch.target_snapshot_id,
            )

        requirements = build_approval_requirements(
            registry=governance.registry,
            policy=governance.route,
            subject_kind=subject_kind,
            domain_scope=scope,
        )
        item = ApprovalItem(
            approval_id=f"approval:{subject_kind}:{subject.artifact_id}",
            subject_series_id=series_id,
            subject_revision=subject_revision,
            subject_kind=subject_kind,
            subject_artifact_id=subject.artifact_id,
            subject_digest=subject.payload_hash,
            status="draft",
            workflow_revision=1,
            supersedes_approval_id=supersedes_approval_id,
            proposer=request.initiated_by,
            domain_scope=scope,
            domain_registry_ref=governance.domain_registry_ref(),
            route_policy=governance.route_ref(),
            role_policy_version=governance.roles.policy_version,
            role_policy_digest=governance.roles.policy_digest,
            approval_policy=governance.approval_ref(),
            requirements=requirements,
            decisions=(),
            regression_evidence_artifact_ids=(),
            target_binding=target_binding,
            created_at=request.occurred_at,
        )
        return (
            subject,
            companions,
            facts,
            item,
            expected_head,
            expected_workflow_revision,
        )

    def _subject(
        self,
        request: AgentDraftWorkflowRequest,
        *,
        require_published: bool,
    ) -> tuple[ArtifactV2, dict[str, object]]:
        artifacts = request.artifacts_by_rule.get("primary", ())
        payloads = request.payloads_by_rule.get("primary", ())
        if (
            len(artifacts) != 1
            or len(payloads) != 1
            or artifacts[0].artifact_id != request.subject_artifact_id
        ):
            raise IntegrityViolation("Agent draft lacks one exact primary Artifact")
        artifact = artifacts[0]
        payload = dict(payloads[0])
        if sha256_lowerhex(canonical_json(payload).encode("utf-8")) != artifact.payload_hash:
            raise IntegrityViolation("Agent draft primary payload differs from its Artifact")
        if require_published:
            self._require_retained_artifact(artifact)
        return artifact, payload

    def _companions(
        self,
        request: AgentDraftWorkflowRequest,
        *,
        require_published: bool,
    ) -> tuple[ArtifactV2, ...]:
        companions = tuple(
            sorted(
                (
                    *request.artifacts_by_rule.get("preview", ()),
                    *request.artifacts_by_rule.get("config-export", ()),
                ),
                key=lambda artifact: artifact.artifact_id,
            )
        )
        if require_published:
            for artifact in companions:
                self._require_retained_artifact(artifact)
        return companions

    def _require_retained_artifact(self, artifact: ArtifactV2) -> None:
        retained = self.artifacts.get(artifact.artifact_id)  # type: ignore[attr-defined]
        if retained != artifact:
            raise IntegrityViolation(
                "Agent draft Artifact differs from terminal persistence",
                artifact_id=artifact.artifact_id,
            )

    @staticmethod
    def _domain_scope(request: AgentDraftWorkflowRequest) -> DomainScope:
        scope = request.run.resource_domain_scope
        if not isinstance(scope, DomainScope):
            raise IntegrityViolation("Agent draft Run has no resolved resource domain scope")
        params = request.run.payload.params
        if isinstance(params, (GenerationProposePayloadV1, ConstraintProposalProposePayloadV1)):
            if params.domain_scope != scope:
                raise IntegrityViolation(
                    "Agent draft payload domain differs from Run admission authority"
                )
        elif isinstance(params, PatchRepairPayloadV1):
            current = request.expected_current_approval
            if current is None or current.domain_scope != scope:
                raise IntegrityViolation(
                    "repair draft domain differs from current ApprovalItem/Run authority"
                )
        else:
            raise IntegrityViolation("Agent draft Run payload is unsupported")
        return scope

    @staticmethod
    def _validate_constraint(
        request: AgentDraftWorkflowRequest,
        subject: ArtifactV2,
        proposal: ConstraintProposalV1,
        companions: tuple[ArtifactV2, ...],
        scope: DomainScope,
    ) -> None:
        if (
            subject.kind != "constraint_proposal"
            or companions
            or proposal.revision != 1
            or proposal.supersedes_artifact_id is not None
            or proposal.produced_by != "agent"
            or proposal.producer_run_id != request.run.run_id
            or proposal.domain_scope != scope
        ):
            raise IntegrityViolation("Agent constraint draft differs from exact Run authority")
        WorkerAgentDraftPreparedAssembler._require_initial_cas(request)

    @staticmethod
    def _validate_patch(
        request: AgentDraftWorkflowRequest,
        subject: ArtifactV2,
        patch: PatchV2,
        companions: tuple[ArtifactV2, ...],
    ) -> ArtifactV2:
        previews = tuple(artifact for artifact in companions if artifact.kind == "ir_snapshot")
        configs = tuple(artifact for artifact in companions if artifact.kind == "config_export")
        if (
            subject.kind != "patch"
            or len(previews) != 1
            or len(previews) + len(configs) != len(companions)
            or patch.produced_by != "agent"
            or patch.producer_run_id != request.run.run_id
            or subject.version_tuple.ir_snapshot_id != patch.base_snapshot_id
            or previews[0].version_tuple.ir_snapshot_id != patch.target_snapshot_id
            or subject.version_tuple.doc_version != previews[0].version_tuple.doc_version
            or any(
                config.version_tuple.doc_version != previews[0].version_tuple.doc_version
                for config in configs
            )
        ):
            raise IntegrityViolation("Agent Patch draft differs from exact Run authority")
        params = request.run.payload.params
        if isinstance(params, GenerationProposePayloadV1):
            if patch.revision != 1 or patch.supersedes_artifact_id is not None:
                raise IntegrityViolation("initial Agent Patch is not revision one")
        elif isinstance(params, PatchRepairPayloadV1):
            current = request.expected_current_approval
            if (
                current is None
                or patch.revision != current.subject_revision + 1
                or patch.supersedes_artifact_id != current.subject_artifact_id
            ):
                raise IntegrityViolation("repair Patch differs from current subject revision")
        else:
            raise IntegrityViolation("Agent Patch Run payload is unsupported")
        return previews[0]

    @staticmethod
    def _require_initial_cas(request: AgentDraftWorkflowRequest) -> None:
        if (
            request.expected_current_approval is not None
            or request.expected_current_subject_head is not None
            or request.expected_subject_head_revision is not None
            or request.expected_workflow_revision is not None
        ):
            raise IntegrityViolation("initial Agent draft carries repair CAS state")

    @staticmethod
    def _repair_cas(
        request: AgentDraftWorkflowRequest,
    ) -> tuple[ApprovalItem, SubjectHead]:
        item = request.expected_current_approval
        head = request.expected_current_subject_head
        if (
            item is None
            or head is None
            or request.expected_subject_head_revision != head.revision
            or request.expected_workflow_revision != item.workflow_revision
            or head.current_approval_id != item.approval_id
            or head.current_subject_artifact_id != item.subject_artifact_id
            or head.subject_series_id != item.subject_series_id
        ):
            raise IntegrityViolation("repair draft lacks the exact current workflow CAS")
        return item, head

    def _retained_parent_ids(self, artifacts: tuple[ArtifactV2, ...]) -> tuple[str, ...]:
        prepared_ids = {artifact.artifact_id for artifact in artifacts}
        retained_ids = tuple(
            sorted(
                {
                    parent_id
                    for artifact in artifacts
                    for parent_id in artifact.lineage
                    if parent_id not in prepared_ids
                }
            )
        )
        retained: dict[str, ArtifactV2] = {}
        for artifact_id in retained_ids:
            artifact = self.artifacts.get(artifact_id)  # type: ignore[attr-defined]
            if not isinstance(artifact, ArtifactV2):
                raise IntegrityViolation(
                    "Agent draft retained lineage parent is unavailable",
                    parent_artifact_id=artifact_id,
                )
            retained[artifact_id] = artifact
        for config in (artifact for artifact in artifacts if artifact.kind == "config_export"):
            sibling_parent_ids = set(config.lineage).intersection(prepared_ids)
            constraint_parent_ids = set(config.lineage) - sibling_parent_ids
            if len(sibling_parent_ids) != 1 or len(constraint_parent_ids) != 1:
                raise IntegrityViolation(
                    "Agent config export must bind one preview and one constraint parent"
                )
            constraint = retained.get(next(iter(constraint_parent_ids)))
            if (
                constraint is None
                or constraint.kind != "constraint_snapshot"
                or constraint.version_tuple.constraint_snapshot_id is None
                or config.version_tuple.constraint_snapshot_id
                != constraint.version_tuple.constraint_snapshot_id
            ):
                raise IntegrityViolation(
                    "Agent config export constraint lineage/VersionTuple differs"
                )
        return retained_ids

    def _retained_bindings(
        self, artifacts: tuple[ArtifactV2, ...]
    ) -> tuple[PreparedObjectBinding, ...]:
        prepared: list[PreparedObjectBinding] = []
        for artifact in artifacts:
            binding = self.object_bindings.resolve(  # type: ignore[attr-defined]
                artifact.object_ref
            )
            if (
                binding.object_ref != artifact.object_ref
                or binding.status != "active"
                or binding.location.store_id == ""
            ):
                raise IntegrityViolation(
                    "Agent draft Artifact has no exact active ObjectBinding",
                    artifact_id=artifact.artifact_id,
                )
            prepared.append(
                PreparedObjectBinding(
                    object_ref=artifact.object_ref,
                    location=binding.location,
                    expected_revision=binding.revision,
                )
            )
        return tuple(prepared)


class WorkerAgentDraftLineageVerifier:
    """Extend the shared draft verifier for terminal Agent Patch parent closure."""

    def __init__(self) -> None:
        self._shared = WorkflowDraftLineageVerifier()

    def validate_draft_publication(
        self,
        *,
        prepared: PreparedDraft,
        retained_parent_ids: tuple[str, ...],
    ) -> None:
        self.validate_terminal_draft_publication(
            subject_artifact=prepared.subject_artifact,
            companion_artifacts=prepared.companion_artifacts,
            retained_parent_ids=retained_parent_ids,
        )

    def validate_terminal_draft_publication(
        self,
        *,
        subject_artifact: ArtifactV2,
        companion_artifacts: tuple[ArtifactV2, ...],
        retained_parent_ids: tuple[str, ...],
    ) -> None:
        subject = subject_artifact
        artifacts = (subject, *companion_artifacts)
        if subject.kind != "patch":
            for artifact in artifacts:
                self._shared._validate(artifact)
            if subject.kind == "constraint_proposal":
                if companion_artifacts:
                    raise IntegrityViolation("Agent constraint proposal draft carries companions")
            elif subject.kind == "rollback_request":
                if len(subject.lineage) != 2 or companion_artifacts:
                    raise IntegrityViolation(
                        "Agent RollbackRequest must bind current and target Artifacts"
                    )
            else:
                raise IntegrityViolation("unsupported Agent draft subject kind")
            self._require_retained_projection(
                artifacts=artifacts,
                retained_parent_ids=retained_parent_ids,
            )
            return

        for artifact in artifacts:
            self._shared._validate(artifact)
        previews = tuple(
            artifact for artifact in companion_artifacts if artifact.kind == "ir_snapshot"
        )
        configs = tuple(
            artifact for artifact in companion_artifacts if artifact.kind == "config_export"
        )
        if len(previews) != 1:
            raise IntegrityViolation("Agent Patch draft requires one exact preview")
        preview = previews[0]
        preview_parents = set(preview.lineage)
        base_parents = preview_parents - {subject.artifact_id}
        if (
            subject.artifact_id not in preview_parents
            or len(base_parents) != 1
            or not base_parents <= set(subject.lineage)
            or subject.version_tuple.doc_version != preview.version_tuple.doc_version
        ):
            raise IntegrityViolation(
                "Agent Patch preview must descend from its Patch and exact base"
            )
        profiles: set[str] = set()
        for config in configs:
            if preview.artifact_id not in config.lineage or len(config.lineage) != 2:
                raise IntegrityViolation(
                    "config export must descend from the exact Agent Patch preview and constraint"
                )
            if (
                config.version_tuple.doc_version != preview.version_tuple.doc_version
                or config.version_tuple.ir_snapshot_id != preview.version_tuple.ir_snapshot_id
                or config.version_tuple.constraint_snapshot_id is None
                or config.meta.get("payload_schema_id") != "config-export-package@1"
            ):
                raise IntegrityViolation(
                    "config export VersionTuple/schema differs from its Agent Patch"
                )
            try:
                profile = canonical_json(
                    ProfileRefV1.model_validate(config.meta.get("export_profile")).model_dump(
                        mode="json"
                    )
                )
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("config export has an invalid profile binding") from exc
            if profile in profiles:
                raise IntegrityViolation("Agent Patch has duplicate config-export profiles")
            profiles.add(profile)

        self._require_retained_projection(
            artifacts=artifacts,
            retained_parent_ids=retained_parent_ids,
        )

    @staticmethod
    def _require_retained_projection(
        *,
        artifacts: tuple[ArtifactV2, ...],
        retained_parent_ids: tuple[str, ...],
    ) -> None:
        prepared_ids = {artifact.artifact_id for artifact in artifacts}
        expected_retained = tuple(
            sorted(
                {
                    parent_id
                    for artifact in artifacts
                    for parent_id in artifact.lineage
                    if parent_id not in prepared_ids
                }
            )
        )
        if retained_parent_ids != expected_retained:
            raise IntegrityViolation("Agent draft retained parents differ from exact lineage")


@dataclass(frozen=True, slots=True)
class WorkerAgentDraftRunGateway:
    """Verify the subject is an exact output of the still-running producer Run."""

    runs: object
    artifacts: object
    readers: WorkflowTypedReaders

    def verify_producer_membership(
        self,
        *,
        run_id: str,
        artifact_id: str,
        initiated_by: AuditActor,
    ) -> None:
        run = self.runs.get(run_id)  # type: ignore[attr-defined]
        artifact = self.artifacts.get(artifact_id)  # type: ignore[attr-defined]
        if (
            not isinstance(run, RunRecord)
            or run.status != "running"
            or run.initiated_by != initiated_by
            or not isinstance(artifact, ArtifactV2)
        ):
            raise IntegrityViolation("Agent draft producer Run membership is unavailable")
        facts = self.readers.inspect_draft_subject(artifact)
        expected_subject_kind = {
            "generation.propose": "patch",
            "patch.repair": "patch",
            "constraint_proposal.propose": "constraint_proposal",
        }.get(run.kind.kind)
        if (
            run.kind.version != 1
            or expected_subject_kind is None
            or facts.subject_kind != expected_subject_kind
            or facts.produced_by != "agent"
            or facts.producer_run_id != run.run_id
        ):
            raise IntegrityViolation("Agent draft subject is not a producer Run output")

    def verify_prepared_terminal_producer_authority(
        self,
        *,
        run_id: str,
        initiated_by: AuditActor,
    ) -> None:
        """Fresh DB-only producer check after planning validated subject bytes."""

        run = self.runs.get(run_id)  # type: ignore[attr-defined]
        if (
            not isinstance(run, RunRecord)
            or run.status != "running"
            or run.initiated_by != initiated_by
            or run.kind.version != 1
            or run.kind.kind
            not in {
                "generation.propose",
                "patch.repair",
                "constraint_proposal.propose",
            }
        ):
            raise IntegrityViolation("prepared Agent draft producer Run authority changed")

    def start_validation(
        self,
        *,
        prepared: PreparedValidationStart,
        item: ApprovalItem,
        initiated_by: AuditActor,
    ) -> str:
        del prepared, item, initiated_by
        raise IntegrityViolation("Agent draft publication cannot start validation")

    def request_validation_cancel(
        self,
        *,
        run_id: str,
        reason: str,
        requested_by: AuditActor,
    ) -> None:
        del run_id, reason, requested_by
        raise IntegrityViolation("repair draft reached publication with an active validation Run")


def build_agent_draft_capabilities(
    *,
    transaction: object,
    object_store: object,
    clock: object,
) -> ApprovalCommandCapabilities:
    """Bind canonical command capabilities to one active worker transaction."""

    readers = WorkflowTypedReaders(
        artifacts=transaction.artifacts,  # type: ignore[attr-defined]
        bindings=transaction.object_bindings,  # type: ignore[attr-defined]
        objects=object_store,  # type: ignore[arg-type]
    )
    return ApprovalCommandCapabilities(
        approvals=transaction.approvals,  # type: ignore[attr-defined]
        policies=transaction.policies,  # type: ignore[attr-defined]
        artifacts=transaction.artifacts,  # type: ignore[attr-defined]
        object_bindings=transaction.object_bindings,  # type: ignore[attr-defined]
        idempotency=transaction.idempotency,  # type: ignore[attr-defined]
        audit=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[arg-type,attr-defined]
        runs=WorkerAgentDraftRunGateway(
            runs=transaction.runs,  # type: ignore[attr-defined]
            artifacts=transaction.artifacts,  # type: ignore[attr-defined]
            readers=readers,
        ),
        subjects=readers,
        lineage=WorkerAgentDraftLineageVerifier(),
        evidence=readers,
        refs=transaction.refs,  # type: ignore[attr-defined]
    )


def build_agent_draft_workflow_port(
    *,
    transaction: object,
    object_store: object,
    clock: object,
    commands: ApprovalCommandService,
    governance_refs: WorkerAgentDraftGovernanceRefs | None,
) -> ApprovalCommandAgentDraftWorkflowPort:
    """Build the exact workflow port from the caller-owned terminal UoW."""

    governance: WorkflowGovernanceProvider
    if governance_refs is None:
        governance = _UnavailableWorkflowGovernanceProvider()
    else:
        governance = TransactionWorkflowGovernanceProvider(
            policies=transaction.policies,  # type: ignore[attr-defined]
            refs=governance_refs,
        )
    return ApprovalCommandAgentDraftWorkflowPort(
        commands=commands,
        capabilities=build_agent_draft_capabilities(
            transaction=transaction,
            object_store=object_store,
            clock=clock,
        ),
        assembler=WorkerAgentDraftPreparedAssembler(
            artifacts=transaction.artifacts,  # type: ignore[attr-defined]
            object_bindings=transaction.object_bindings,  # type: ignore[attr-defined]
            governance=governance,
        ),
    )


__all__ = [
    "TransactionWorkflowGovernanceProvider",
    "WorkerAgentDraftGovernanceRefs",
    "WorkerAgentDraftLineageVerifier",
    "WorkerAgentDraftPreparedAssembler",
    "WorkerAgentDraftRunGateway",
    "build_agent_draft_capabilities",
    "build_agent_draft_workflow_port",
]
