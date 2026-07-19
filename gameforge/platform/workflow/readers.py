"""Transaction-bound typed readers and the draft-lineage verifier for M4c.

These are the production counterparts of the seams the M4a integration harness
inlined. They resolve exact object bytes through the authoritative ObjectBinding
(or, for pre-commit draft bytes, the content-addressed generation in the store)
and never substitute in-memory payloads. Every read reverifies the ObjectRef.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from gameforge.contracts.canonical import canonical_json, compute_snapshot_id
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.jobs import RunResultV1
from gameforge.contracts.lineage import ArtifactV2, ExecutionIdentityV1
from gameforge.contracts.storage import ObjectStore
from gameforge.contracts.workflow import (
    ConstraintProposalV1,
    EvidenceSet,
    RollbackRequestV1,
    regression_companion_evidence_ids,
)
from gameforge.platform.approvals.apply import VerifiedTargetPayload
from gameforge.platform.approvals.commands import (
    ArtifactRepository,
    DraftSubjectFacts,
    EvidenceStateProjection,
    PreparedDraft,
)
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.lineage.validation import (
    PRODUCER_RULES,
    ProducerValidationContext,
    validate_artifact_producer,
)
from gameforge.spine.ir.snapshot import Snapshot


def _producer_context(artifact: ArtifactV2) -> ProducerValidationContext:
    """Reconstruct the matrix facts already sealed into one draft Artifact.

    The terminal publisher validated parent-derived projections before creating the
    Artifact.  The workflow boundary must still validate the complete LLM/cassette
    projection instead of silently treating an Agent artifact as
    ``not_applicable`` merely because it is reading the immutable result later.
    """

    rule = PRODUCER_RULES[artifact.kind]
    expected = {
        field: value
        for field in rule.projected_fields
        if (value := getattr(artifact.version_tuple, field)) is not None
    }
    identity = artifact.meta.get("execution_identity")
    has_llm_invocations = isinstance(identity, ExecutionIdentityV1) and bool(identity.bindings)
    replayability = artifact.meta.get("replayability")
    if replayability == "online_only":
        mode = "live"
    elif replayability == "cassette_replay":
        mode = (
            "replay"
            if has_llm_invocations
            and all(binding.execution_source == "cassette_replay" for binding in identity.bindings)
            else "record"
        )
    else:
        mode = "not_applicable"
    return ProducerValidationContext(
        expected_versions=expected,
        llm_execution_mode=mode,
        has_llm_invocations=has_llm_invocations,
        produced_by_agent=has_llm_invocations,
        operational_observation=(replayability == "operational_observation"),
    )


class ObjectBindingReader:
    """Narrow view of the object-binding repository the readers require."""

    def resolve(self, ref: object) -> Any:  # pragma: no cover - Protocol shape
        raise NotImplementedError


def workflow_target_snapshot_id(artifact: ArtifactV2) -> str | None:
    """Project the snapshot identity carried by a workflow target Artifact."""

    if artifact.kind == "ir_snapshot":
        return artifact.version_tuple.ir_snapshot_id
    if artifact.kind == "constraint_snapshot":
        return artifact.version_tuple.constraint_snapshot_id
    return None


class WorkflowTypedReaders:
    """One transaction's exact typed reads for subjects, evidence, and targets.

    Implements ``SubjectPayloadGateway``, ``ApplyEvidenceGateway``,
    ``ApplyTargetGateway`` and ``RebasePayloadGateway`` for every M4 subject kind
    (patch, constraint proposal, rollback request).
    """

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        bindings: Any,
        objects: ObjectStore,
    ) -> None:
        self._artifacts = artifacts
        self._bindings = bindings
        self._objects = objects

    # ── exact byte resolution ────────────────────────────────────────────────
    def _read(self, artifact: ArtifactV2) -> bytes:
        retained = self._artifacts.get(artifact.artifact_id)
        # ``artifact_id`` encodes kind/version/lineage/payload_hash/meta, so a retained
        # Artifact sharing that id can differ only in its server-owned ``created_at``
        # timestamp (e.g. a draft re-assembled on an idempotent replay under a real
        # clock). That timestamp is not part of identity, so compare modulo it and rely
        # on the exact ObjectRef verification below for content integrity.
        if retained is not None and (
            retained.model_copy(update={"created_at": artifact.created_at}) != artifact
        ):
            raise IntegrityViolation("typed reader Artifact differs from persistence")
        try:
            location = self._bindings.resolve(artifact.object_ref).location
        except FileNotFoundError:
            if retained is not None:
                raise IntegrityViolation(
                    "persisted Artifact has no authoritative ObjectBinding"
                ) from None
            location = self._locate_preverified(artifact)
        with self._objects.open(location) as source:
            payload = source.read()
        if (
            len(payload) != artifact.object_ref.size_bytes
            or hashlib.sha256(payload).hexdigest() != artifact.object_ref.sha256
        ):
            raise IntegrityViolation("typed reader ObjectRef verification failed")
        return payload

    def _locate_preverified(self, artifact: ArtifactV2) -> Any:
        cursor = None
        while True:
            page = self._objects.list_versions(cursor)
            for stat in page.items:
                if stat.ref == artifact.object_ref:
                    return stat.location
            cursor = page.next_cursor
            if cursor is None:
                raise IntegrityViolation("preverified draft object generation is unavailable")

    def _json(self, artifact: ArtifactV2) -> Any:
        try:
            return json.loads(self._read(artifact))
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("typed Artifact payload is not JSON") from exc

    # ── SubjectPayloadGateway ────────────────────────────────────────────────
    def inspect_draft_subject(self, artifact: ArtifactV2) -> DraftSubjectFacts:
        if artifact.kind == "patch":
            patch = PatchV2.model_validate(self._json(artifact))
            return DraftSubjectFacts(
                subject_kind="patch",
                subject_revision=patch.revision,
                produced_by=patch.produced_by,
                producer_run_id=patch.producer_run_id,
                supersedes_artifact_id=patch.supersedes_artifact_id,
                target_artifact_id=None,
                target_snapshot_id=patch.target_snapshot_id,
            )
        if artifact.kind == "constraint_proposal":
            proposal = ConstraintProposalV1.model_validate(self._json(artifact))
            return DraftSubjectFacts(
                subject_kind="constraint_proposal",
                subject_revision=proposal.revision,
                produced_by=proposal.produced_by,
                producer_run_id=proposal.producer_run_id,
                supersedes_artifact_id=proposal.supersedes_artifact_id,
                target_artifact_id=None,
                target_snapshot_id=None,
            )
        if artifact.kind == "rollback_request":
            request = RollbackRequestV1.model_validate(self._json(artifact))
            target = self._artifacts.get(request.target_artifact_id)
            if not isinstance(target, ArtifactV2):
                raise IntegrityViolation("rollback typed reader cannot resolve target")
            return DraftSubjectFacts(
                subject_kind="rollback_request",
                subject_revision=None,
                produced_by="human",
                producer_run_id=None,
                supersedes_artifact_id=None,
                target_artifact_id=target.artifact_id,
                target_snapshot_id=workflow_target_snapshot_id(target),
                rollback_request=request,
            )
        raise IntegrityViolation("unsupported workflow subject kind")

    def load_patch(self, artifact: ArtifactV2) -> PatchV2:
        return PatchV2.model_validate(self._json(artifact))

    def load_constraint_proposal(self, artifact: ArtifactV2) -> ConstraintProposalV1:
        return ConstraintProposalV1.model_validate(self._json(artifact))

    def load_rollback_request(self, artifact: ArtifactV2) -> RollbackRequestV1:
        return RollbackRequestV1.model_validate(self._json(artifact))

    def load_run_result(self, artifact: ArtifactV2) -> RunResultV1:
        if (
            artifact.kind != "run_result"
            or artifact.meta.get("payload_schema_id") != "run-result@1"
        ):
            raise IntegrityViolation("run result reader received another Artifact kind/schema")
        return RunResultV1.model_validate(self._json(artifact))

    def load_snapshot(self, artifact: ArtifactV2) -> Snapshot:
        payload = self._json(artifact)
        snapshot = snapshot_from_canonical_view(payload)
        if artifact.kind == "ir_snapshot" and (
            snapshot.snapshot_id != artifact.version_tuple.ir_snapshot_id
        ):
            raise IntegrityViolation("ir_snapshot payload differs from VersionTuple")
        return snapshot

    def load_constraints(self, artifact: ArtifactV2) -> list[Constraint]:
        if artifact.kind != "constraint_snapshot":
            raise IntegrityViolation("constraint reader received another Artifact kind")
        payload = self._json(artifact)
        if not isinstance(payload, dict) or set(payload) != {
            "dsl_grammar_version",
            "constraints",
        }:
            raise IntegrityViolation("constraint snapshot payload has the wrong shape")
        raw = payload["constraints"]
        if not isinstance(raw, list):
            raise IntegrityViolation("constraint snapshot constraints must be a list")
        constraints = [Constraint.model_validate(item) for item in raw]
        if any(item.dsl_grammar_version != payload["dsl_grammar_version"] for item in constraints):
            raise IntegrityViolation(
                "constraint snapshot grammar differs from a contained Constraint"
            )
        return constraints

    # ── ApplyEvidenceGateway / ApprovalEvidenceGateway ───────────────────────
    def load_evidence_set(self, artifact: ArtifactV2) -> EvidenceSet:
        if artifact.kind != "validation_evidence":
            raise IntegrityViolation("evidence reader received another Artifact kind")
        return EvidenceSet.model_validate(self._json(artifact))

    def validate_submission(
        self,
        *,
        item: Any,
        subject_artifact: ArtifactV2,
        target_artifact: ArtifactV2,
        evidence_artifact: ArtifactV2,
        regression_artifacts: tuple[ArtifactV2, ...],
    ) -> EvidenceStateProjection:
        self.inspect_draft_subject(subject_artifact)
        self.read_verified(target_artifact)
        evidence = self.load_evidence_set(evidence_artifact)
        if (
            evidence.subject_artifact_id != item.subject_artifact_id
            or evidence.subject_digest != item.subject_digest
            or evidence.target_binding != item.target_binding
        ):
            raise IntegrityViolation("typed EvidenceSet differs from ApprovalItem")
        regression_status = self._verify_regression_evidence(evidence, regression_artifacts)
        return EvidenceStateProjection(
            validation_status=evidence.overall_status,
            regression_status=regression_status,
        )

    @staticmethod
    def _verify_regression_evidence(
        evidence: EvidenceSet,
        regression_artifacts: tuple[ArtifactV2, ...],
    ) -> Literal["passed", "failed", "unproven", "not_applicable"]:
        """Cross-check the ApprovalItem's frozen regression evidence against the EvidenceSet.

        A real invariant/economy validation seals one ``regression`` dimension per
        checker/simulation; the validation-completion effect binds those published
        ``regression-evidence@1`` Artifacts onto ``item.regression_evidence_artifact_ids``,
        which apply / auto-apply re-verify. Submission must therefore ACCEPT them and
        confirm they are EXACTLY the EvidenceSet's regression-requirement bindings (kind /
        schema / content-addressed id), then derive the regression status from those
        requirements — never reject them wholesale (that under-accepted every checker- or
        economy-validated Patch).
        """

        requirements_by_artifact_id = {
            requirement.evidence_artifact_id: requirement
            for requirement in evidence.requirements
            if requirement.evidence_artifact_id is not None
        }
        expected_ids = regression_companion_evidence_ids(evidence)
        presented_ids = tuple(sorted(artifact.artifact_id for artifact in regression_artifacts))
        if presented_ids != expected_ids:
            raise IntegrityViolation(
                "frozen regression evidence differs from the EvidenceSet requirements"
            )
        for artifact in regression_artifacts:
            if artifact.kind != "regression_evidence":
                raise IntegrityViolation("regression evidence has the wrong Artifact kind")
            schema = (getattr(artifact, "meta", {}) or {}).get("payload_schema_id")
            if schema != "regression-evidence@1":
                raise IntegrityViolation("regression evidence declares the wrong payload schema")
            if artifact.artifact_id not in requirements_by_artifact_id:
                raise IntegrityViolation("regression evidence has no EvidenceSet requirement")
        statuses = [
            requirement.status
            for requirement in evidence.requirements
            if requirement.kind == "regression" and requirement.applicability == "required"
        ]
        if not statuses:
            return "not_applicable"
        if all(status == "passed" for status in statuses):
            return "passed"
        if any(status == "failed" for status in statuses):
            return "failed"
        return "unproven"

    def project_state(self, *, item: Any) -> EvidenceStateProjection:
        if item.evidence_set_artifact_id is None:
            return EvidenceStateProjection(
                validation_status=(
                    "running" if item.active_validation_run_id is not None else "not_started"
                ),
                regression_status="not_started",
            )
        evidence = self._artifacts.get(item.evidence_set_artifact_id)
        if not isinstance(evidence, ArtifactV2):
            raise IntegrityViolation("ApprovalItem evidence Artifact is unavailable")
        return EvidenceStateProjection(
            validation_status=self.load_evidence_set(evidence).overall_status,
            regression_status="not_applicable",
        )

    # ── ApplyTargetGateway ───────────────────────────────────────────────────
    def read_verified(self, artifact: ArtifactV2) -> VerifiedTargetPayload:
        payload = self._read(artifact)
        snapshot_id = None
        schema_id = "json@1"
        if artifact.kind == "ir_snapshot":
            snapshot_id = compute_snapshot_id(json.loads(payload))
            schema_id = "ir-snapshot@1"
            if snapshot_id != artifact.version_tuple.ir_snapshot_id:
                raise IntegrityViolation("snapshot payload differs from VersionTuple")
        elif artifact.kind == "constraint_snapshot":
            schema_id = "constraint-snapshot@1"
            snapshot_id = artifact.version_tuple.constraint_snapshot_id
        return VerifiedTargetPayload(
            artifact=artifact,
            payload_bytes=payload,
            payload_schema_id=schema_id,
            snapshot_id=snapshot_id,
        )


class WorkflowDraftLineageVerifier:
    """Prove draft publications against the exact producer matrix and DAG rules."""

    @staticmethod
    def _validate(artifact: ArtifactV2) -> None:
        if validate_artifact_producer(artifact, _producer_context(artifact)).status != "valid":
            raise IntegrityViolation("Artifact producer validation did not pass")

    def validate_draft_publication(
        self,
        *,
        prepared: PreparedDraft,
        retained_parent_ids: tuple[str, ...],
    ) -> None:
        subject = prepared.subject_artifact
        for artifact in prepared.artifacts:
            self._validate(artifact)
        if subject.kind == "patch":
            previews = tuple(
                artifact
                for artifact in prepared.companion_artifacts
                if artifact.kind == "ir_snapshot"
            )
            configs = tuple(
                artifact
                for artifact in prepared.companion_artifacts
                if artifact.kind == "config_export"
            )
            if len(previews) != 1:
                raise IntegrityViolation("Patch draft requires exactly one preview companion")
            preview = previews[0]
            preview_parents = set(preview.lineage)
            preview_base = preview_parents - {subject.artifact_id}
            if subject.artifact_id not in preview_parents or len(preview_base) != 1:
                raise IntegrityViolation("preview direct parents must be base plus Patch")
            base_parent_id = next(iter(preview_base))
            constraint_parent_id: str | None = None
            if prepared.expected_subject_head is None:
                # An initial human Patch directly binds its exact base plus the optional
                # authoritative constraint Artifact.  The constraint is not inferred
                # from config-export companions: a caller may deliberately bind one
                # even when no executable export was requested.
                other_parents = set(subject.lineage) - {base_parent_id}
                if subject.version_tuple.constraint_snapshot_id is None:
                    if other_parents or set(subject.lineage) != {base_parent_id}:
                        raise IntegrityViolation(
                            "unconstrained Patch draft must directly bind only its base"
                        )
                else:
                    if len(other_parents) != 1 or base_parent_id not in subject.lineage:
                        raise IntegrityViolation(
                            "constrained Patch draft must directly bind base plus constraint"
                        )
                    constraint_parent_id = next(iter(other_parents))
            else:
                # A rebase remains the explicit merge projection {source Patch,current};
                # its constraint semantic may be inherited transitively from the source
                # Patch and therefore does not add a third direct parent.
                if len(subject.lineage) != 2 or base_parent_id not in subject.lineage:
                    raise IntegrityViolation(
                        "rebased Patch must bind source Patch plus its exact current base"
                    )
            if subject.version_tuple.doc_version != preview.version_tuple.doc_version:
                raise IntegrityViolation(
                    "Patch and preview document versions must inherit the same exact base"
                )
            profiles: set[str] = set()
            for config in configs:
                config_parents = set(config.lineage)
                config_constraints = config_parents - {preview.artifact_id}
                if (
                    preview.artifact_id not in config_parents
                    or len(config_parents) != 2
                    or len(config_constraints) != 1
                ):
                    raise IntegrityViolation(
                        "config export must descend from the exact Patch preview and constraint"
                    )
                config_constraint_id = next(iter(config_constraints))
                if constraint_parent_id is None:
                    constraint_parent_id = config_constraint_id
                elif config_constraint_id != constraint_parent_id:
                    raise IntegrityViolation(
                        "Patch draft config exports bind different constraint Artifacts"
                    )
                if (
                    config.version_tuple.doc_version != preview.version_tuple.doc_version
                    or config.version_tuple.ir_snapshot_id != preview.version_tuple.ir_snapshot_id
                    or config.version_tuple.constraint_snapshot_id
                    != subject.version_tuple.constraint_snapshot_id
                    or config.meta.get("payload_schema_id") != "config-export-package@1"
                ):
                    raise IntegrityViolation(
                        "config export VersionTuple/schema differs from its Patch candidate"
                    )
                try:
                    parsed_profile = ProfileRefV1.model_validate(config.meta.get("export_profile"))
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "config export has an invalid profile binding"
                    ) from exc
                profile = canonical_json(parsed_profile.model_dump(mode="json"))
                if profile in profiles:
                    raise IntegrityViolation(
                        "Patch draft contains duplicate config-export profiles"
                    )
                profiles.add(profile)
        elif subject.kind == "constraint_proposal":
            if prepared.companion_artifacts:
                raise IntegrityViolation("constraint proposal draft carries no companions")
        elif subject.kind == "rollback_request":
            if len(subject.lineage) != 2 or prepared.companion_artifacts:
                raise IntegrityViolation("RollbackRequest must bind current and target Artifacts")
        else:  # pragma: no cover - PreparedDraft forbids other kinds
            raise IntegrityViolation("unsupported draft subject kind")
        prepared_ids = {artifact.artifact_id for artifact in prepared.artifacts}
        expected_retained = tuple(
            sorted(
                {
                    parent_id
                    for artifact in prepared.artifacts
                    for parent_id in artifact.lineage
                    if parent_id not in prepared_ids
                }
            )
        )
        if retained_parent_ids != expected_retained:
            raise IntegrityViolation("draft retained parents differ from exact lineage")


__all__ = [
    "WorkflowDraftLineageVerifier",
    "WorkflowTypedReaders",
    "workflow_target_snapshot_id",
]
