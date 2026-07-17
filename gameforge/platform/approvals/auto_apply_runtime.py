"""Exact production composition around the pure Patch auto-apply guard.

The pure guard deliberately accepts already-resolved immutable authority.  This
module is the reusable bridge used by worker terminal publication and synchronous
approval/apply gateways: it resolves the exact Run-frozen profile/policies,
retained Artifact bytes, current Ref, complete EvidenceSet closure, and a
schema-versioned canonical diff assessment before invoking that one guard.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from gameforge.contracts.auto_apply_ownership import (
    AutoApplyIrOwnershipV1,
    auto_apply_ir_classifier_binding,
)
from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    CheckerProfileConfigV1,
    ExecutionProfileDefinitionV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    SimulationProfileConfigV1,
    ValidationProfileDetailsV1,
)
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import DomainRegistryRefV1, DomainRegistryV1, DomainScope
from gameforge.contracts.jobs import PatchValidationPayloadV1, RunRecord, RunResultV1
from gameforge.contracts.lineage import ArtifactV2
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    AutoApplyEvidenceContextV1,
    AutoApplyOracleAttestationV1,
    AutoApplyOutcomeAttestationV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyPolicyRegistryV1,
    AutoApplyProofV1,
    DeterministicOracleRegistryRefV1,
    DeterministicOracleRegistryV1,
    EvidenceSet,
)
from gameforge.platform.approvals.auto_apply import (
    AUTO_APPLY_OUTCOME_CODE,
    AutoApplyChangeAssessment,
    OracleEvidenceClaims,
    QualifiedOutcomeEvidenceClaims,
    ResolvedArtifactPayload,
    validate_auto_apply,
)
from gameforge.platform.diff.engine import iter_snapshot_diff_entries
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.publication.payload_schema import validate_artifact_payload
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.run_handlers.validation_common import (
    PATCH_SIMULATION_EXECUTION_MODE_V1,
    derive_validation_subseed,
    validation_child_seed_evidence,
)
from gameforge.spine.patch import PatchRejected, apply_patch


class ExactAutoApplyAuthority(Protocol):
    """Transaction/snapshot-bound retained authority required by the guard."""

    def load_artifact(self, artifact_id: str) -> ResolvedArtifactPayload: ...

    def get_domain_registry(self, ref: DomainRegistryRefV1) -> DomainRegistryV1 | None: ...

    def get_auto_apply_policy_registry(
        self, ref: AutoApplyPolicyRegistryRefV1
    ) -> AutoApplyPolicyRegistryV1 | None: ...

    def get_deterministic_oracle_registry(
        self, ref: DeterministicOracleRegistryRefV1
    ) -> DeterministicOracleRegistryV1 | None: ...

    def resolve_execution_profile(
        self, binding: ResolvedExecutionProfileBindingV1
    ) -> ExecutionProfileDefinitionV1 | None: ...

    def get_ref(self, ref_name: str) -> RefValue | None: ...


class AutoApplyChangeAssessor(Protocol):
    def assess(
        self,
        *,
        base: ResolvedArtifactPayload,
        subject: ResolvedArtifactPayload,
        target: ResolvedArtifactPayload,
        domain_registry: DomainRegistryV1,
    ) -> AutoApplyChangeAssessment: ...


@dataclass(frozen=True, slots=True)
class ExactAutoApplyEligibilityRequest:
    """Minimal IDs plus immutable Run/item authority for one guard execution."""

    run: RunRecord
    item: ApprovalItem
    outcome_code: str
    proof_artifact_id: str
    evidence_set_artifact_id: str


@dataclass(frozen=True, slots=True)
class CanonicalIrAutoApplyChangeAssessor:
    """Frozen classifier over exact canonical ``ir-core@1`` base/target bytes.

    IR resource ownership comes only from the exact policy-bound Domain Registry's
    versioned ``auto-apply:*@1`` tags. Artifact metadata can assert a superset of
    the recomputed scope, but never decides it. Numeric/text classification walks
    only frozen IR schema paths and fails closed on every unknown path/value shape.
    """

    def assess(
        self,
        *,
        base: ResolvedArtifactPayload,
        subject: ResolvedArtifactPayload,
        target: ResolvedArtifactPayload,
        domain_registry: DomainRegistryV1,
    ) -> AutoApplyChangeAssessment:
        if (
            base.artifact.kind != "ir_snapshot"
            or target.artifact.kind != "ir_snapshot"
            or base.payload_schema_id != "ir-core@1"
            or target.payload_schema_id != "ir-core@1"
        ):
            raise IntegrityViolation("auto-apply diff requires exact ir-core@1 snapshots")
        if subject.artifact.kind != "patch" or subject.payload_schema_id != "patch@2":
            raise IntegrityViolation("auto-apply diff requires an exact patch@2 subject")

        base_payload = _strict_payload(base)
        target_payload = _strict_payload(target)
        patch_payload = _strict_payload(subject)
        try:
            patch = PatchV2.model_validate(patch_payload)
            base_snapshot = snapshot_from_canonical_view(base_payload)
            target_snapshot = snapshot_from_canonical_view(target_payload)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("auto-apply canonical diff input is invalid") from exc
        base_canonical_bytes = canonical_json(base_snapshot.content_payload).encode("utf-8")
        target_canonical_bytes = canonical_json(target_snapshot.content_payload).encode("utf-8")
        if (
            base.payload_bytes != base_canonical_bytes
            or target.payload_bytes != target_canonical_bytes
        ):
            raise IntegrityViolation("auto-apply snapshot bytes are not canonical ir-core@1")
        if subject.payload_bytes != canonical_json(patch.model_dump(mode="json")).encode("utf-8"):
            raise IntegrityViolation("auto-apply Patch bytes are not canonical patch@2")
        if (
            base.artifact.version_tuple.ir_snapshot_id != patch.base_snapshot_id
            or base_snapshot.snapshot_id != patch.base_snapshot_id
            or target.artifact.version_tuple.ir_snapshot_id != patch.target_snapshot_id
            or target_snapshot.snapshot_id != patch.target_snapshot_id
        ):
            raise IntegrityViolation("auto-apply Patch/base/target snapshot identity differs")
        try:
            replayed_target = apply_patch(base_snapshot, patch)
        except PatchRejected as exc:
            raise IntegrityViolation(
                "auto-apply Patch does not replay against its exact base"
            ) from exc
        replayed_bytes = canonical_json(replayed_target.content_payload).encode("utf-8")
        if (
            replayed_target.snapshot_id != target_snapshot.snapshot_id
            or replayed_bytes != target.payload_bytes
        ):
            raise IntegrityViolation(
                "auto-apply Patch replay differs from its exact target snapshot"
            )

        entries = tuple(iter_snapshot_diff_entries(base_payload, target_payload))
        if not entries:
            raise IntegrityViolation("auto-apply canonical diff is empty")
        try:
            classifier = auto_apply_ir_classifier_binding(domain_registry)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "auto-apply Domain Registry ownership tags are invalid"
            ) from exc
        active_scope = _active_domain_scope(domain_registry)
        affected_ids: set[str] = set()
        numeric_changed = False
        narrative_changed = False
        complete = classifier.ownership.complete
        for entry in entries:
            classification = _classify_diff_entry(entry.path, entry.before, entry.after)
            owner_complete, owners = _diff_resource_owners(
                path=entry.path,
                base_payload=base_payload,
                target_payload=target_payload,
                ownership=classifier.ownership,
            )
            complete = complete and classification[0] and owner_complete
            affected_ids.update(owners)
            numeric_changed = numeric_changed or classification[1]
            narrative_changed = narrative_changed or classification[2]
        if complete and not affected_ids:
            raise IntegrityViolation("auto-apply canonical diff resolved no owning domain")
        scope = DomainScope(domain_ids=tuple(affected_ids)) if complete else active_scope
        _assert_artifact_scope(
            derived=scope,
            require_containment=complete,
            registry=domain_registry,
            records=(base, subject, target),
        )

        return AutoApplyChangeAssessment(
            base_artifact_id=base.artifact.artifact_id,
            base_snapshot_id=patch.base_snapshot_id,
            subject_artifact_id=subject.artifact.artifact_id,
            subject_digest=subject.artifact.payload_hash,
            target_artifact_id=target.artifact.artifact_id,
            target_snapshot_id=patch.target_snapshot_id,
            target_digest=target.artifact.payload_hash,
            target_payload_schema_id=target.payload_schema_id,
            schema_id=classifier.classifier_schema_id,
            schema_digest=classifier.classifier_schema_digest,
            affected_domain_scope=scope,
            field_classification_complete=complete,
            numeric_value_changed=numeric_changed,
            narrative_text_changed=narrative_changed,
        )


def _definition_authorizes_patch_validation(
    definition: ExecutionProfileDefinitionV1,
    *,
    expected_kind: str,
) -> bool:
    contracts = {
        "validation": (
            "builtin_validation_profile@1",
            "validation-profile-config@1",
            {"constraint-validation@1", "patch-validation@1"},
            {"auto-apply-proof@1", "evidence-set@1"},
            {
                RunKindRef(kind="constraint_proposal.validate", version=1),
                RunKindRef(kind="patch.validate", version=1),
            },
            False,
        ),
        "checker": (
            "builtin_checker_profile@1",
            "checker-profile-config@1",
            {"checker-run@1", "patch-repair@1", "patch-validation@1", "review-run@1"},
            {"checker-report@1", "regression-evidence@1"},
            {
                RunKindRef(kind="checker.run", version=1),
                RunKindRef(kind="patch.repair", version=1),
                RunKindRef(kind="patch.validate", version=1),
                RunKindRef(kind="review.run", version=1),
            },
            False,
        ),
        "simulation": (
            "builtin_simulation_profile@1",
            "simulation-profile-config@1",
            {"patch-repair@1", "patch-validation@1", "review-run@1", "simulation-run@1"},
            {"regression-evidence@1", "simulation-result@1"},
            {
                RunKindRef(kind="patch.repair", version=1),
                RunKindRef(kind="patch.validate", version=1),
                RunKindRef(kind="review.run", version=1),
                RunKindRef(kind="simulation.run", version=1),
            },
            True,
        ),
    }
    contract = contracts.get(expected_kind)
    if contract is None:
        return False
    handler_key, config_schema_id, inputs, outputs, compatible_kinds, stochastic = contract
    if (
        definition.profile_kind != expected_kind
        or definition.handler_key != handler_key
        or definition.config_schema_id != config_schema_id
        or set(definition.input_schema_ids) != inputs
        or set(definition.output_schema_ids) != outputs
        or set(definition.compatible_run_kinds) != compatible_kinds
        or definition.stochastic is not stochastic
        or definition.required_capabilities
    ):
        return False
    try:
        if expected_kind == "validation":
            return (
                definition.config == {}
                and isinstance(definition.details, ValidationProfileDetailsV1)
                and definition.details.subject_kinds
                == ("patch", "constraint_proposal", "rollback_request")
            )
        if expected_kind == "checker":
            CheckerProfileConfigV1.model_validate(definition.config)
        else:
            SimulationProfileConfigV1.model_validate(definition.config)
    except (TypeError, ValueError):
        return False
    return True


def _resolve_patch_validation_profiles(
    *,
    authority: ExactAutoApplyAuthority,
    run: RunRecord,
    params: PatchValidationPayloadV1,
) -> dict[str, ExecutionProfileDefinitionV1]:
    expected: dict[str, tuple[object, str]] = {
        "/params/validation_policy": (params.validation_policy, "validation"),
        **{
            f"/params/checker_profiles/{index}": (profile, "checker")
            for index, profile in enumerate(params.checker_profiles)
        },
        **{
            f"/params/simulation_profiles/{index}": (profile, "simulation")
            for index, profile in enumerate(params.simulation_profiles)
        },
    }
    bindings = {binding.field_path: binding for binding in run.payload.resolved_profiles}
    if len(bindings) != len(run.payload.resolved_profiles) or set(bindings) != set(expected):
        raise IntegrityViolation(
            "auto-apply Run lacks its complete exact execution profile closure"
        )
    catalogs = {(binding.catalog_version, binding.catalog_digest) for binding in bindings.values()}
    if len(catalogs) != 1:
        raise IntegrityViolation("auto-apply profiles come from different exact catalogs")
    resolved: dict[str, ExecutionProfileDefinitionV1] = {}
    for field_path, (profile, expected_kind) in expected.items():
        binding = bindings[field_path]
        if binding.profile != profile or binding.expected_profile_kind != expected_kind:
            raise IntegrityViolation(
                "auto-apply Run execution profile binding differs from its params",
                field_path=field_path,
            )
        definition = authority.resolve_execution_profile(binding)
        if (
            definition is None
            or definition.profile != binding.profile
            or run.kind not in definition.compatible_run_kinds
            or not _definition_authorizes_patch_validation(
                definition,
                expected_kind=expected_kind,
            )
        ):
            raise IntegrityViolation(
                "auto-apply execution profile history is unavailable",
                field_path=field_path,
            )
        resolved[field_path] = definition
    return resolved


@dataclass(frozen=True, slots=True)
class ExactAutoApplyEligibilityService:
    """Resolve complete exact authority and invoke the canonical pure guard."""

    authority: ExactAutoApplyAuthority
    change_assessor: AutoApplyChangeAssessor = CanonicalIrAutoApplyChangeAssessor()

    def validate_eligibility(self, request: ExactAutoApplyEligibilityRequest) -> None:
        params = request.run.payload.params
        if not isinstance(params, PatchValidationPayloadV1):
            raise IntegrityViolation("auto-apply eligibility requires patch.validate@1")
        if request.outcome_code != AUTO_APPLY_OUTCOME_CODE:
            raise IntegrityViolation("auto-apply eligibility received another outcome")

        proof_record = self.authority.load_artifact(request.proof_artifact_id)
        evidence_record = self.authority.load_artifact(request.evidence_set_artifact_id)
        proof = _model_payload(proof_record, AutoApplyProofV1, "auto-apply proof")
        evidence = _model_payload(evidence_record, EvidenceSet, "EvidenceSet")
        profile_binding = proof.validation_profile_binding
        resolved_profiles = _resolve_patch_validation_profiles(
            authority=self.authority,
            run=request.run,
            params=params,
        )
        validation_run_binding = next(
            binding
            for binding in request.run.payload.resolved_profiles
            if binding.field_path == "/params/validation_policy"
        )
        if (
            request.item.auto_apply_proof is None
            or request.item.auto_apply_proof.proof_artifact_id != request.proof_artifact_id
            or request.item.evidence_set_artifact_id != request.evidence_set_artifact_id
            or proof.validation_evidence_artifact_id != request.evidence_set_artifact_id
            or evidence.validation_run_id != request.run.run_id
            or params.subject.approval_id != request.item.approval_id
            or params.subject.subject_artifact_id != request.item.subject_artifact_id
            or params.subject.subject_digest != request.item.subject_digest
            or params.subject.active_validation_run_id != request.run.run_id
            or params.preview_snapshot_artifact_id != proof.target_binding.target_artifact_id
            or params.target.ref_name != proof.target_binding.ref_name
            or params.target.expected_ref != proof.target_binding.expected_ref
            or request.item.target_binding != proof.target_binding
            or request.item.domain_scope != proof.affected_domain_scope
            or request.run.resource_domain_scope != proof.affected_domain_scope
            or validation_run_binding.profile != profile_binding.validation_profile
            or validation_run_binding.profile_payload_hash
            != profile_binding.validation_profile_payload_hash
        ):
            raise IntegrityViolation("auto-apply runtime request differs from proof/item/Run")

        subject = self.authority.load_artifact(request.item.subject_artifact_id)
        if request.item.target_binding is None:
            raise IntegrityViolation("auto-apply ApprovalItem has no exact target binding")
        target = self.authority.load_artifact(request.item.target_binding.target_artifact_id)
        base = self.authority.load_artifact(params.base_snapshot_artifact_id)

        policy_registry = self.authority.get_auto_apply_policy_registry(proof.policy.registry)
        if policy_registry is None:
            raise IntegrityViolation("auto-apply policy registry history is unavailable")
        policies = tuple(
            policy
            for policy in policy_registry.policies
            if (policy.policy_id, policy.policy_version)
            == (proof.policy.policy_id, proof.policy.policy_version)
        )
        if len(policies) != 1:
            raise IntegrityViolation("auto-apply policy does not resolve exactly once")
        policy = policies[0]
        oracle_registry = self.authority.get_deterministic_oracle_registry(
            policy.deterministic_oracle_registry
        )
        if oracle_registry is None:
            raise IntegrityViolation("deterministic oracle registry history is unavailable")
        domain_registry = self.authority.get_domain_registry(policy.domain_registry)
        if domain_registry is None:
            raise IntegrityViolation("domain registry history is unavailable")
        validation_profile = resolved_profiles["/params/validation_policy"]

        closure_ids = {
            *evidence.supporting_artifact_ids,
            *(binding.evidence_artifact_id for binding in evidence.finding_bindings),
            *(
                requirement.evidence_artifact_id
                for requirement in evidence.requirements
                if requirement.evidence_artifact_id is not None
            ),
        }
        closure_ids.discard(subject.artifact.artifact_id)
        closure_ids.discard(target.artifact.artifact_id)
        evidence_artifacts = tuple(
            self.authority.load_artifact(artifact_id) for artifact_id in sorted(closure_ids)
        )
        assessment = self.change_assessor.assess(
            base=base,
            subject=subject,
            target=target,
            domain_registry=domain_registry,
        )
        decoders = _EvidenceClaimDecoders(
            proof=proof,
            evidence=evidence,
            oracle_registry=oracle_registry,
            run=request.run,
            executor_profiles={
                f"checker:{profile.profile_id}@{profile.version}": resolved_profiles[
                    f"/params/checker_profiles/{index}"
                ]
                for index, profile in enumerate(params.checker_profiles)
            }
            | {
                f"simulation:{profile.profile_id}@{profile.version}": resolved_profiles[
                    f"/params/simulation_profiles/{index}"
                ]
                for index, profile in enumerate(params.simulation_profiles)
            },
        )
        validate_auto_apply(
            outcome_code=request.outcome_code,
            item=request.item,
            subject=subject,
            target=target,
            proof=proof_record,
            evidence_set=evidence_record,
            evidence_artifacts=evidence_artifacts,
            domain_registry=domain_registry,
            policy_registry=policy_registry,
            oracle_registry=oracle_registry,
            validation_profile=validation_profile,
            resolved_outcome_policies=request.run.payload.resolved_policy_snapshots,
            change_assessment=assessment,
            current_ref=self.authority.get_ref(request.item.target_binding.ref_name),
            oracle_evidence_decoder=decoders.decode_oracle,
            outcome_evidence_decoder=decoders.decode_outcome,
        )

    def resolve_terminal_outcome(self, run: RunRecord, *, item: ApprovalItem) -> str:
        """Read the immutable RunResult; never infer a post-commit outcome."""

        if run.status != "succeeded" or run.result_artifact_id is None:
            raise IntegrityViolation("auto-apply requires a succeeded RunResult authority")
        record = self.authority.load_artifact(run.result_artifact_id)
        if record.artifact.kind != "run_result" or record.payload_schema_id != "run-result@1":
            raise IntegrityViolation("auto-apply Run result Artifact has another contract")
        if item.evidence_set_artifact_id is None or item.auto_apply_proof is None:
            raise IntegrityViolation("auto-apply ApprovalItem lacks terminal proof closure")
        raw_result = _strict_payload(record)
        try:
            result = RunResultV1.model_validate(raw_result)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("RunResult payload violates its exact contract") from exc
        if record.payload_bytes != canonical_json(result.model_dump(mode="json")).encode("utf-8"):
            raise IntegrityViolation("auto-apply RunResult payload is noncanonical")
        expected_outputs = {
            item.evidence_set_artifact_id,
            item.auto_apply_proof.proof_artifact_id,
            *item.regression_evidence_artifact_ids,
        }
        expected_output_roles = {
            item.evidence_set_artifact_id: "output",
            item.auto_apply_proof.proof_artifact_id: "evidence",
            **{artifact_id: "output" for artifact_id in item.regression_evidence_artifact_ids},
        }
        expected_inputs = set(run.payload.input_artifact_ids)
        projection = result.version_projection
        parent_ids = tuple(parent.artifact_id for parent in projection.parents)
        projected_produced_ids = tuple(
            sorted(
                parent.artifact_id
                for parent in projection.parents
                if parent.publication == "run_published" and parent.role != "input"
            )
        )
        exact_parent_bindings = all(
            (
                parent.artifact_id in expected_inputs
                and parent.role == "input"
                and parent.publication == "existing"
                and parent.attempt_no is None
                and parent.ordinal is None
                and parent.cassette_scope is None
            )
            or (
                parent.artifact_id in expected_output_roles
                and parent.role == expected_output_roles[parent.artifact_id]
                and parent.publication == "run_published"
                and parent.attempt_no is None
                and parent.ordinal is None
                and parent.cassette_scope is None
            )
            for parent in projection.parents
        )
        if (
            result.run_id != run.run_id
            or result.attempt_no != run.current_attempt_no
            or result.run_kind != run.kind
            or projection.run_kind != run.kind
            or projection.run_payload_hash != run.payload_hash
            or projection.frozen_input_version_tuple != run.payload.version_tuple
            or projection.terminal_version_tuple != record.artifact.version_tuple
            or result.outcome_code != AUTO_APPLY_OUTCOME_CODE
            or result.primary_artifact_id != item.evidence_set_artifact_id
            or result.produced_artifact_ids != projected_produced_ids
            or set(result.produced_artifact_ids) != expected_outputs
            or run.payload.llm_execution_mode != "not_applicable"
            or expected_inputs & expected_outputs
            or len(expected_output_roles) != 2 + len(item.regression_evidence_artifact_ids)
            or set(parent_ids) != expected_inputs | expected_outputs
            or not exact_parent_bindings
            or record.artifact.lineage != tuple(sorted(parent_ids))
        ):
            raise IntegrityViolation("auto-apply RunResult does not prove its terminal outcome")
        return result.outcome_code


class AutoApplyRunAuthority(Protocol):
    def get(self, run_id: str) -> RunRecord | None: ...


@dataclass(frozen=True, slots=True)
class ExactAutoApplyApprovalGateway:
    """Synchronous submit/apply adapter over the same exact guard service."""

    eligibility: ExactAutoApplyEligibilityService
    runs: AutoApplyRunAuthority

    def validate_eligibility(self, *, item: ApprovalItem) -> None:
        if item.auto_apply_proof is None or item.evidence_set_artifact_id is None:
            raise IntegrityViolation("auto-apply ApprovalItem lacks exact proof evidence")
        evidence_record = self.eligibility.authority.load_artifact(item.evidence_set_artifact_id)
        evidence = _model_payload(evidence_record, EvidenceSet, "EvidenceSet")
        run = self.runs.get(evidence.validation_run_id)
        if (
            not isinstance(run, RunRecord)
            or run.status != "succeeded"
            or run.kind.kind != "patch.validate"
            or run.kind.version != 1
        ):
            raise IntegrityViolation("auto-apply validation Run authority is unavailable")
        self.eligibility.validate_eligibility(
            ExactAutoApplyEligibilityRequest(
                run=run,
                item=item,
                outcome_code=self.eligibility.resolve_terminal_outcome(run, item=item),
                proof_artifact_id=item.auto_apply_proof.proof_artifact_id,
                evidence_set_artifact_id=item.evidence_set_artifact_id,
            )
        )


@dataclass(frozen=True, slots=True)
class TransactionBoundAutoApplyAuthority:
    """One write-UoW view shared by worker completion and synchronous APIs."""

    transaction: Any
    object_store: Any
    profiles: ImmutablePlatformRegistry

    def load_artifact(self, artifact_id: str) -> ResolvedArtifactPayload:
        artifact = self.transaction.artifacts.get(artifact_id)
        if not isinstance(artifact, ArtifactV2):
            raise IntegrityViolation(
                "auto-apply retained Artifact is unavailable", artifact_id=artifact_id
            )
        binding = self.transaction.object_bindings.resolve(artifact.object_ref)
        stat = self.object_store.stat(binding.location)
        if stat.ref != artifact.object_ref or stat.location != binding.location:
            raise IntegrityViolation(
                "auto-apply ObjectBinding differs from retained Artifact",
                artifact_id=artifact_id,
            )
        with self.object_store.open(binding.location) as stream:
            payload = stream.read()
        schema = artifact.meta.get("payload_schema_id")
        if not isinstance(schema, str) or not schema:
            raise IntegrityViolation(
                "auto-apply Artifact payload schema is unavailable",
                artifact_id=artifact_id,
            )
        return ResolvedArtifactPayload(
            artifact=artifact,
            payload_schema_id=schema,
            payload_bytes=payload,
        )

    def get_domain_registry(self, ref: DomainRegistryRefV1) -> DomainRegistryV1 | None:
        return self.transaction.policies.get_domain_registry(ref)

    def get_auto_apply_policy_registry(
        self, ref: AutoApplyPolicyRegistryRefV1
    ) -> AutoApplyPolicyRegistryV1 | None:
        return self.transaction.policies.get_auto_apply_policy_registry(ref)

    def get_deterministic_oracle_registry(
        self, ref: DeterministicOracleRegistryRefV1
    ) -> DeterministicOracleRegistryV1 | None:
        return self.transaction.policies.get_deterministic_oracle_registry(ref)

    def resolve_execution_profile(
        self, binding: ResolvedExecutionProfileBindingV1
    ) -> ExecutionProfileDefinitionV1 | None:
        try:
            definition, lifecycle = self.profiles.resolve_execution_profile_binding(binding)
        except IntegrityViolation:
            return None
        if lifecycle.state != "active":
            return None
        return definition

    def get_ref(self, ref_name: str) -> RefValue | None:
        return self.transaction.refs.get(ref_name)


@dataclass(frozen=True, slots=True)
class _EvidenceClaimDecoders:
    proof: AutoApplyProofV1
    evidence: EvidenceSet
    oracle_registry: DeterministicOracleRegistryV1
    run: RunRecord
    executor_profiles: Mapping[str, ExecutionProfileDefinitionV1]

    def _projection(
        self, payload: Mapping[str, Any]
    ) -> tuple[str, str, AutoApplyEvidenceContextV1]:
        requirement_id = payload.get("requirement_id")
        status = payload.get("status")
        if not isinstance(requirement_id, str) or not requirement_id:
            raise ValueError("qualified evidence has no requirement_id")
        if status not in {"passed", "failed", "unproven"}:
            raise ValueError("qualified evidence has no deterministic verdict")
        context = AutoApplyEvidenceContextV1.model_validate(payload.get("auto_apply_context"))
        return requirement_id, status, context

    def _validated_payload(self, payload_schema_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload_schema_id != "regression-evidence@1":
            raise ValueError("production auto-apply decoder requires regression-evidence@1")
        try:
            validated = validate_artifact_payload(
                payload_schema_id=payload_schema_id,
                payload=payload,
            )
        except IntegrityViolation as exc:
            raise ValueError("qualified evidence violates regression-evidence@1") from exc
        return dict(validated)

    def _artifact_id_for_requirement(self, requirement_id: str) -> str:
        matches = tuple(
            requirement.evidence_artifact_id
            for requirement in self.evidence.requirements
            if requirement.requirement_id == requirement_id
            and requirement.evidence_artifact_id is not None
        )
        if len(matches) != 1:
            raise ValueError("qualified evidence requirement is not unique")
        return matches[0]

    def decode_oracle(
        self, payload_schema_id: str, payload: dict[str, Any]
    ) -> OracleEvidenceClaims:
        payload = self._validated_payload(payload_schema_id, payload)
        requirement_id, status, context = self._projection(payload)
        artifact_id = self._artifact_id_for_requirement(requirement_id)
        bindings = tuple(
            binding
            for binding in self.proof.deterministic_oracle_evidence
            if binding.evidence_artifact_id == artifact_id
        )
        if len(bindings) != 1:
            raise ValueError("one evidence Artifact must qualify exactly one oracle")
        binding = bindings[0]
        definitions = tuple(
            definition
            for definition in self.oracle_registry.definitions
            if (definition.oracle_id, definition.oracle_version)
            == (binding.oracle.oracle_id, binding.oracle.oracle_version)
        )
        if len(definitions) != 1:
            raise ValueError("bound oracle does not resolve exactly once")
        definition = definitions[0]
        attestations = _oracle_attestations(payload)
        matches = tuple(item for item in attestations if item.oracle == binding.oracle)
        if len(matches) != 1:
            raise ValueError("bound oracle lacks one exact payload attestation")
        attestation = matches[0]
        if (
            attestation.engine_kind != definition.engine_kind
            or attestation.engine_id != _native_engine_id(definition.engine_kind)
            or attestation.tool_version != definition.tool_version
            or attestation.predicate_schema_id != definition.predicate_schema_id
            or attestation.evaluated_domain_scope != context.evaluated_domain_scope
            or attestation.verdict != status
            or attestation.verdict_authority != context.verdict_authority
            or attestation.direct_parent_artifact_ids != context.direct_parent_artifact_ids
            or attestation.predicate
            != {
                "kind": "dimension_status",
                "requirement_id": requirement_id,
                "engine_id": attestation.engine_id,
                "engine_version": attestation.engine_version,
                "status": status,
            }
            or not _attested_executor_is_frozen(
                run=self.run,
                requirement_id=requirement_id,
                engine_kind=attestation.engine_kind,
                engine_id=attestation.engine_id,
                engine_version=attestation.engine_version,
                definition=self.executor_profiles.get(requirement_id),
                payload=payload,
            )
        ):
            raise ValueError("oracle attestation differs from exact executor authority")
        return OracleEvidenceClaims(
            oracle=attestation.oracle,
            subject_artifact_id=context.subject_artifact_id,
            subject_digest=context.subject_digest,
            target_binding=context.target_binding,
            evaluated_domain_scope=context.evaluated_domain_scope,
            predicate_schema_id=attestation.predicate_schema_id,
            predicate=attestation.predicate,
            verdict=attestation.verdict,
            verdict_authority=attestation.verdict_authority,
            direct_parent_artifact_ids=attestation.direct_parent_artifact_ids,
        )

    def decode_outcome(
        self, payload_schema_id: str, payload: dict[str, Any]
    ) -> QualifiedOutcomeEvidenceClaims:
        payload = self._validated_payload(payload_schema_id, payload)
        requirement_id, status, context = self._projection(payload)
        artifact_id = self._artifact_id_for_requirement(requirement_id)
        bindings = tuple(
            binding
            for binding in self.proof.required_outcome_evidence
            if binding.requirement_id == requirement_id
            and binding.evidence_artifact_id == artifact_id
        )
        if len(bindings) != 1:
            raise ValueError("qualified outcome binding is not unique")
        binding = bindings[0]
        attestations = _outcome_attestations(payload)
        matches = tuple(
            item
            for item in attestations
            if item.rule == binding.rule and item.requirement_id == requirement_id
        )
        if len(matches) != 1:
            raise ValueError("bound outcome lacks one exact payload attestation")
        attestation = matches[0]
        if (
            attestation.evaluated_domain_scope != context.evaluated_domain_scope
            or attestation.verdict != status
            or attestation.verdict_authority != context.verdict_authority
            or attestation.direct_parent_artifact_ids != context.direct_parent_artifact_ids
        ):
            raise ValueError("outcome attestation differs from exact executor authority")
        return QualifiedOutcomeEvidenceClaims(
            rule=attestation.rule,
            requirement_id=attestation.requirement_id,
            subject_artifact_id=context.subject_artifact_id,
            subject_digest=context.subject_digest,
            target_binding=context.target_binding,
            evaluated_domain_scope=attestation.evaluated_domain_scope,
            verdict=attestation.verdict,
            verdict_authority=attestation.verdict_authority,
            direct_parent_artifact_ids=attestation.direct_parent_artifact_ids,
        )


def _strict_payload(record: ResolvedArtifactPayload) -> dict[str, Any]:
    if (
        len(record.payload_bytes) != record.artifact.object_ref.size_bytes
        or sha256_lowerhex(record.payload_bytes) != record.artifact.payload_hash
        or record.artifact.payload_hash != record.artifact.object_ref.sha256
    ):
        raise IntegrityViolation(
            "auto-apply Artifact bytes differ from immutable identity",
            artifact_id=record.artifact.artifact_id,
        )

    def reject_constant(value: str) -> object:
        raise ValueError(value)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key {key}")
            value[key] = item
        return value

    try:
        payload = json.loads(
            record.payload_bytes,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise IntegrityViolation("auto-apply Artifact payload is not strict JSON") from exc
    if not isinstance(payload, dict):
        raise IntegrityViolation("auto-apply Artifact payload is not an object")
    return payload


def _model_payload[T](record: ResolvedArtifactPayload, model: type[T], label: str) -> T:
    try:
        return model.model_validate(_strict_payload(record))  # type: ignore[attr-defined,no-any-return]
    except (TypeError, ValueError) as exc:
        raise IntegrityViolation(f"{label} payload violates its exact contract") from exc


def _active_domain_scope(domain_registry: DomainRegistryV1) -> DomainScope:
    active = tuple(
        definition.domain_id
        for definition in domain_registry.definitions
        if definition.status == "active"
    )
    if not active:
        raise IntegrityViolation("auto-apply domain registry has no active domain")
    return DomainScope(domain_ids=active)


def _assert_artifact_scope(
    *,
    derived: DomainScope,
    require_containment: bool,
    registry: DomainRegistryV1,
    records: tuple[ResolvedArtifactPayload, ...],
) -> None:
    known = {definition.domain_id for definition in registry.definitions}
    for record in records:
        raw = record.artifact.meta.get("domain_scope")
        try:
            scope = DomainScope.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "auto-apply Artifact lacks exact domain scope authority",
                artifact_id=record.artifact.artifact_id,
            ) from exc
        if raw != scope.model_dump(mode="json"):
            raise IntegrityViolation("auto-apply Artifact domain scope is noncanonical")
        unknown = set(scope.domain_ids) - known
        if unknown:
            raise IntegrityViolation(
                "auto-apply Artifact domain scope references an unknown domain",
                artifact_id=record.artifact.artifact_id,
                domain_ids=tuple(sorted(unknown)),
            )
        if require_containment and not set(derived.domain_ids) <= set(scope.domain_ids):
            raise IntegrityViolation(
                "auto-apply Artifact metadata narrows the recomputed affected scope",
                artifact_id=record.artifact.artifact_id,
            )


def _decode_pointer_token(value: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "~":
            decoded.append(character)
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            raise ValueError("invalid RFC 6901 escape")
        decoded.append("~" if value[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _resource_type(
    payload: Mapping[str, Any],
    *,
    collection: str,
    resource_id: str,
) -> str | None:
    values = payload.get(collection)
    if not isinstance(values, Mapping):
        return None
    resource = values.get(resource_id)
    if resource is None:
        return None
    if not isinstance(resource, Mapping):
        return None
    resource_type = resource.get("type")
    return resource_type if isinstance(resource_type, str) and resource_type else None


def _diff_resource_owners(
    *,
    path: str,
    base_payload: Mapping[str, Any],
    target_payload: Mapping[str, Any],
    ownership: AutoApplyIrOwnershipV1,
) -> tuple[bool, tuple[str, ...]]:
    if not path.startswith("/"):
        return False, ()
    encoded = path[1:].split("/")
    if len(encoded) < 2 or encoded[0] not in {"entities", "relations"}:
        return False, ()
    try:
        resource_id = _decode_pointer_token(encoded[1])
    except ValueError:
        return False, ()
    collection = encoded[0]
    resource_kind = "entity" if collection == "entities" else "relation"
    resource_types = {
        value
        for value in (
            _resource_type(
                base_payload,
                collection=collection,
                resource_id=resource_id,
            ),
            _resource_type(
                target_payload,
                collection=collection,
                resource_id=resource_id,
            ),
        )
        if value is not None
    }
    if not resource_types:
        return False, ()
    owners: set[str] = set()
    complete = True
    for resource_type in resource_types:
        resolved = ownership.owners_for(resource_kind, resource_type)
        if not resolved:
            complete = False
        owners.update(resolved)
    return complete, tuple(sorted(owners))


def _native_engine_id(engine_kind: str) -> str:
    native = {
        "graph": "graph",
        "asp": "asp",
        "smt": "smt",
        "simulation": "economy_sim",
        "playtest_completion": "playtest_completion",
    }.get(engine_kind)
    if native is None:
        raise ValueError("unsupported deterministic oracle engine kind")
    return native


def _attested_executor_is_frozen(
    *,
    run: RunRecord,
    requirement_id: str,
    engine_kind: str,
    engine_id: str,
    engine_version: str,
    definition: ExecutionProfileDefinitionV1 | None,
    payload: Mapping[str, Any],
) -> bool:
    params = run.payload.params
    if not isinstance(params, PatchValidationPayloadV1):
        return False
    if engine_kind in {"graph", "asp", "smt"}:
        if definition is None or not _definition_authorizes_patch_validation(
            definition,
            expected_kind="checker",
        ):
            return False
        try:
            config = CheckerProfileConfigV1.model_validate(definition.config)
        except (TypeError, ValueError):
            return False
        expected_requirement = (
            f"checker:{definition.profile.profile_id}@{definition.profile.version}"
        )
        raw_profile = payload.get("checker_profile")
        raw_bindings = payload.get("checker_execution_bindings")
        if (
            requirement_id != expected_requirement
            or definition.profile not in params.checker_profiles
            or engine_version != str(definition.profile.version)
            or engine_id not in config.allowed_checker_ids
            or raw_profile != definition.profile.model_dump(mode="json")
            or not isinstance(raw_bindings, list)
        ):
            return False
        return any(
            isinstance(binding, Mapping)
            and binding.get("native_id") == engine_id
            and binding.get("constraint_id") is None
            for binding in raw_bindings
        )
    if engine_kind == "simulation":
        if definition is None or not _definition_authorizes_patch_validation(
            definition,
            expected_kind="simulation",
        ):
            return False
        try:
            config = SimulationProfileConfigV1.model_validate(definition.config)
        except (TypeError, ValueError):
            return False
        execution_binding = payload.get("simulation_execution_binding")
        root_seed = run.payload.seed
        if not isinstance(execution_binding, Mapping) or root_seed is None:
            return False
        case_id = f"simulation:{definition.profile.profile_id}@{definition.profile.version}"
        execution_seed = derive_validation_subseed(
            root_seed=root_seed,
            run_kind=run.kind,
            profile=definition.profile,
            case_id=case_id,
            replication_index=0,
        )
        return (
            requirement_id == case_id
            and definition.profile in params.simulation_profiles
            and engine_id == "economy_sim"
            and engine_version == str(definition.profile.version)
            and payload.get("profile_id") == definition.profile.profile_id
            and payload.get("profile_version") == definition.profile.version
            and execution_binding
            == {
                "binding_schema_version": "simulation-expected-finding-binding@1",
                "producer_id": "economy_sim",
                "simulation_profile": definition.profile.model_dump(mode="json"),
                "execution_mode": PATCH_SIMULATION_EXECUTION_MODE_V1,
                "seed_binding": validation_child_seed_evidence(
                    root_seed=root_seed,
                    execution_seed=execution_seed,
                    run_kind=run.kind,
                    profile=definition.profile,
                    case_id=case_id,
                ),
                "constraint_snapshot_binding_status": "not_applicable",
                "constraint_ids": [],
                "constraint_application": {"status": "not_applicable"},
                "n_agents": config.default_population,
                "n_ticks": config.default_horizon_steps,
            }
        )
    if engine_kind == "playtest_completion":
        return (
            engine_version == "1"
            and requirement_id.startswith("playtest:")
            and requirement_id.removeprefix("playtest:") in params.playtest_trace_artifact_ids
        )
    return False


def _oracle_attestations(
    payload: Mapping[str, Any],
) -> tuple[AutoApplyOracleAttestationV1, ...]:
    raw = payload.get("oracle_attestations")
    if not isinstance(raw, list):
        raise ValueError("qualified oracle evidence has no exact attestation array")
    attestations = tuple(AutoApplyOracleAttestationV1.model_validate(item) for item in raw)
    keys = tuple((item.oracle.oracle_id, item.oracle.oracle_version) for item in attestations)
    if keys != tuple(sorted(set(keys))):
        raise ValueError("oracle evidence attestations are noncanonical")
    return attestations


def _outcome_attestations(
    payload: Mapping[str, Any],
) -> tuple[AutoApplyOutcomeAttestationV1, ...]:
    raw = payload.get("outcome_attestations")
    if not isinstance(raw, list):
        raise ValueError("qualified outcome evidence has no exact attestation array")
    attestations = tuple(AutoApplyOutcomeAttestationV1.model_validate(item) for item in raw)
    keys = tuple(
        (
            item.rule.resolved_policy_id,
            item.rule.outcome_rule_id,
            item.requirement_id,
        )
        for item in attestations
    )
    if keys != tuple(sorted(set(keys))):
        raise ValueError("outcome evidence attestations are noncanonical")
    return attestations


_MISSING_JSON_VALUE = object()


def _state_value(state: object) -> object:
    if getattr(state, "presence", None) == "missing":
        return _MISSING_JSON_VALUE
    return getattr(state, "value")


def _semantic_value_classes(value: object) -> tuple[bool, bool, bool]:
    if value is _MISSING_JSON_VALUE:
        return True, False, False
    # IR attrs/tags are open JSON in ir-core@1.  Without a versioned, exact-path
    # schema proving that a boolean/null field is structural, changing it could
    # toggle gameplay or narrative authority.  Treat it as unknown so auto-apply
    # fails closed; a future structural boolean must be added via a versioned
    # classifier/path allowlist rather than inferred from its scalar type.
    if value is None or type(value) is bool:
        return False, False, False
    if type(value) in {int, float}:
        return True, True, False
    if isinstance(value, str):
        return True, False, True
    if isinstance(value, list):
        values = tuple(_semantic_value_classes(item) for item in value)
    elif isinstance(value, dict) and all(isinstance(key, str) for key in value):
        values = tuple(_semantic_value_classes(item) for item in value.values())
    else:
        return False, False, False
    return (
        all(item[0] for item in values),
        any(item[1] for item in values),
        any(item[2] for item in values),
    )


def _classify_diff_entry(path: str, before: object, after: object) -> tuple[bool, bool, bool]:
    tokens = path.removeprefix("/").split("/") if path else []
    if len(tokens) < 2 or tokens[0] not in {"entities", "relations"}:
        return False, False, False
    kind = tokens[0]
    if len(tokens) == 2:
        # Whole entity/relation additions contain structural identity fields plus
        # optional semantic attrs/tags.  Inspect only those schema-declared fields.
        numeric = False
        narrative = False
        complete = True
        for state in (before, after):
            value = _state_value(state)
            if value is _MISSING_JSON_VALUE:
                continue
            if not isinstance(value, dict):
                return False, False, False
            allowed = (
                {"type", "attrs", "source_ref", "tags", "schema_version"}
                if kind == "entities"
                else {"type", "src_id", "dst_id", "attrs", "source_ref", "schema_version"}
            )
            if not set(value).issubset(allowed):
                complete = False
            for field in ("attrs", "tags"):
                if field not in value:
                    continue
                classified = _semantic_value_classes(value[field])
                complete = complete and classified[0]
                numeric = numeric or classified[1]
                narrative = narrative or classified[2]
        return complete, numeric, narrative

    field = tokens[2]
    structural = (
        {"type", "schema_version", "source_ref"}
        if kind == "entities"
        else {"type", "src_id", "dst_id", "schema_version", "source_ref"}
    )
    if field in structural:
        return True, False, False
    if field not in {"attrs", "tags"} or (kind == "relations" and field == "tags"):
        return False, False, False
    values = tuple(_semantic_value_classes(_state_value(state)) for state in (before, after))
    return (
        all(item[0] for item in values),
        any(item[1] for item in values),
        any(item[2] for item in values),
    )


__all__ = [
    "AutoApplyChangeAssessor",
    "CanonicalIrAutoApplyChangeAssessor",
    "ExactAutoApplyAuthority",
    "ExactAutoApplyApprovalGateway",
    "ExactAutoApplyEligibilityRequest",
    "ExactAutoApplyEligibilityService",
    "TransactionBoundAutoApplyAuthority",
]
