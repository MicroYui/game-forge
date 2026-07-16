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

from gameforge.contracts.canonical import compute_snapshot_id
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.lineage import ArtifactV2
from gameforge.contracts.storage import ObjectStore
from gameforge.contracts.workflow import (
    ConstraintProposalV1,
    EvidenceSet,
    RollbackRequestV1,
)
from gameforge.platform.approvals.apply import VerifiedTargetPayload
from gameforge.platform.approvals.commands import (
    ArtifactRepository,
    DraftSubjectFacts,
    EvidenceStateProjection,
    PreparedDraft,
)
from gameforge.platform.lineage.validation import (
    ProducerValidationContext,
    validate_artifact_producer,
)
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph


def _producer_context(artifact: ArtifactV2) -> ProducerValidationContext:
    expected: dict[str, object] = {}
    if artifact.version_tuple.doc_version is not None:
        expected["doc_version"] = artifact.version_tuple.doc_version
    if artifact.version_tuple.ir_snapshot_id is not None:
        expected["ir_snapshot_id"] = artifact.version_tuple.ir_snapshot_id
    if artifact.version_tuple.constraint_snapshot_id is not None:
        expected["constraint_snapshot_id"] = artifact.version_tuple.constraint_snapshot_id
    return ProducerValidationContext(expected_versions=expected)


class ObjectBindingReader:
    """Narrow view of the object-binding repository the readers require."""

    def resolve(self, ref: object) -> Any:  # pragma: no cover - Protocol shape
        raise NotImplementedError


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
                target_snapshot_id=target.version_tuple.ir_snapshot_id,
                rollback_request=request,
            )
        raise IntegrityViolation("unsupported workflow subject kind")

    def load_patch(self, artifact: ArtifactV2) -> PatchV2:
        return PatchV2.model_validate(self._json(artifact))

    def load_constraint_proposal(self, artifact: ArtifactV2) -> ConstraintProposalV1:
        return ConstraintProposalV1.model_validate(self._json(artifact))

    def load_rollback_request(self, artifact: ArtifactV2) -> RollbackRequestV1:
        return RollbackRequestV1.model_validate(self._json(artifact))

    def load_snapshot(self, artifact: ArtifactV2) -> Snapshot:
        payload = self._json(artifact)
        snapshot = _snapshot_from_canonical_view(payload)
        if artifact.kind == "ir_snapshot" and (
            snapshot.snapshot_id != artifact.version_tuple.ir_snapshot_id
        ):
            raise IntegrityViolation("ir_snapshot payload differs from VersionTuple")
        return snapshot

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

        requirements = {
            requirement.evidence_artifact_id: requirement
            for requirement in evidence.requirements
            if requirement.kind == "regression" and requirement.evidence_artifact_id is not None
        }
        expected_ids = tuple(sorted(requirements))
        presented_ids = tuple(sorted(artifact.artifact_id for artifact in regression_artifacts))
        if presented_ids != expected_ids:
            raise IntegrityViolation(
                "frozen regression evidence differs from the EvidenceSet requirements"
            )
        statuses: list[str] = []
        for artifact in regression_artifacts:
            if artifact.kind != "regression_evidence":
                raise IntegrityViolation("regression evidence has the wrong Artifact kind")
            schema = (getattr(artifact, "meta", {}) or {}).get("payload_schema_id")
            if schema != "regression-evidence@1":
                raise IntegrityViolation("regression evidence declares the wrong payload schema")
            statuses.append(requirements[artifact.artifact_id].status)
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


def _snapshot_from_canonical_view(view: Any) -> Snapshot:
    from gameforge.contracts.ir import Entity, Relation

    if not isinstance(view, dict) or set(view) != {
        "meta_schema_version",
        "entities",
        "relations",
    }:
        raise IntegrityViolation("snapshot payload is not the canonical IR shape")
    graph = IRGraph()
    for entity_id, payload in view["entities"].items():
        graph.add_entity(Entity.model_validate({"id": entity_id, **payload}))
    for relation_id, payload in view["relations"].items():
        graph.add_relation(Relation.model_validate({"id": relation_id, **payload}))
    return Snapshot.from_graph(graph)


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
            if len(prepared.companion_artifacts) != 1:
                raise IntegrityViolation("Patch draft requires exactly one preview companion")
            preview = prepared.companion_artifacts[0]
            if len(subject.lineage) == 1:
                # Initial draft: Patch descends from its base; preview = {base, Patch}.
                if set(preview.lineage) != {subject.lineage[0], subject.artifact_id}:
                    raise IntegrityViolation("preview direct parents must be base plus Patch")
            elif len(subject.lineage) == 2:
                # Rebased draft: Patch descends from {source Patch, current}; the preview
                # descends from {current, Patch}. The rebased base (current) must be one of
                # the Patch's exact lineage parents.
                preview_parents = set(preview.lineage)
                if subject.artifact_id not in preview_parents:
                    raise IntegrityViolation("rebased preview must descend from its Patch")
                rebased_base = preview_parents - {subject.artifact_id}
                if len(rebased_base) != 1 or not rebased_base <= set(subject.lineage):
                    raise IntegrityViolation(
                        "rebased preview base must be an exact Patch lineage parent"
                    )
            else:
                raise IntegrityViolation("Patch draft has an unexpected lineage shape")
            expected_retained = subject.lineage
        elif subject.kind == "constraint_proposal":
            if prepared.companion_artifacts:
                raise IntegrityViolation("constraint proposal draft carries no companions")
            expected_retained = subject.lineage
        elif subject.kind == "rollback_request":
            if len(subject.lineage) != 2 or prepared.companion_artifacts:
                raise IntegrityViolation("RollbackRequest must bind current and target Artifacts")
            expected_retained = subject.lineage
        else:  # pragma: no cover - PreparedDraft forbids other kinds
            raise IntegrityViolation("unsupported draft subject kind")
        if retained_parent_ids != tuple(sorted(expected_retained)):
            raise IntegrityViolation("draft retained parents differ from exact lineage")


__all__ = [
    "WorkflowDraftLineageVerifier",
    "WorkflowTypedReaders",
]
