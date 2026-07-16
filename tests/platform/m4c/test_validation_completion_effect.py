"""Task 17b — focused coverage of the validation-completion effects (decision (a)).

Drives the registered ``publication/effects.py`` effects directly against a REAL
``SqlApprovalRepository`` over real SQLite (the same CAS the ``TerminalPublisher``
runs inside its terminal UoW), covering branches the real-admission E2E does not
reach directly: the ``validated`` / ``validation_failed`` CAS, the run-failure
``restore_current_draft@1`` revert, and the superseded-subject NO-OP (never revive).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainRegistryRefV1, DomainScope, Permission
from gameforge.contracts.jobs import (
    OutcomeArtifactPolicyV1,
    PatchValidationPayloadV1,
    RefReadBindingV1,
    ValidationSubjectBindingV1,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.lineage import AuditActor, VersionTuple, build_artifact_v2
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalRequirement,
    DomainRoutePolicyRefV1,
    EvidenceSet,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.publication.effects import WorkflowEffectContext, apply_workflow_effect
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository

import pytest

NOW = "2026-07-16T00:00:00Z"
RUN_ID = "run:validation:1"
APPROVAL_ID = "approval:1"
SERIES_ID = "series:1"
REF_NAME = "content/head"
_HEX = "a" * 64
_DOMAIN = "narrative"
_WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")


class _Fixture:
    def __init__(self, tmp_path: Path) -> None:
        self.database_url = f"sqlite:///{tmp_path / 'effect.db'}"
        self.object_root = tmp_path / "objects"
        migrations_api.upgrade(self.database_url)
        from gameforge.runtime.clock import SystemUtcClock

        self.clock = SystemUtcClock()
        self.patch = self._seed_artifact(
            kind="patch",
            schema="patch@2",
            blob=b'{"payload_schema_version":"patch@2","ops":[]}',
            version_tuple=VersionTuple(tool_version="patch@2"),
        )
        self.preview = self._seed_artifact(
            kind="ir_snapshot",
            schema="ir-core@1",
            blob=b'{"schema_version":"ir-core@1"}',
            version_tuple=VersionTuple(ir_snapshot_id="snap:preview", tool_version="ir-core@1"),
        )
        self.base = self._seed_artifact(
            kind="ir_snapshot",
            schema="ir-core@1",
            blob=b'{"schema_version":"ir-core@1","base":true}',
            version_tuple=VersionTuple(ir_snapshot_id="snap:base", tool_version="ir-core@1"),
        )
        self.evidence_artifact = self._seed_artifact(
            kind="validation_evidence",
            schema="evidence-set@1",
            blob=b'{"payload_schema_version":"evidence-set@1"}',
            version_tuple=VersionTuple(
                ir_snapshot_id="snap:preview", tool_version="patch-validation@1"
            ),
        )
        self.failure_artifact = self._seed_artifact(
            kind="run_failure",
            schema="run-failure@1",
            blob=b'{"payload_schema_version":"run-failure@1"}',
            version_tuple=VersionTuple(tool_version="run@1"),
        )

    def _seed_artifact(self, *, kind, schema, blob, version_tuple):
        objects = LocalObjectStore(
            self.object_root, store_id="local:test", clock=self.clock, cursor_signing_key=b"o" * 32
        )
        stored = objects.put_verified(blob)
        artifact = build_artifact_v2(
            kind=kind,
            version_tuple=version_tuple,
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={"payload_schema_id": schema},
            created_at=NOW,
        )
        engine = get_engine(self.database_url)
        with Session(engine) as session, session.begin():
            bindings = SqlObjectBindingRepository(session, objects, "local:test")
            bindings.bind_verified(stored.ref, stored.location, None)
            SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=self.clock),
                clock=self.clock,
            ).put(artifact)
        engine.dispose()
        return artifact

    def session(self) -> Session:
        return Session(get_engine(self.database_url))


def _domain_ref() -> DomainRegistryRefV1:
    return DomainRegistryRefV1(registry_version="domains@1", registry_digest="4" * 64)


def _target_binding(fix: _Fixture) -> PatchTargetBindingV1:
    return PatchTargetBindingV1(
        target_artifact_id=fix.preview.artifact_id,
        target_snapshot_id="snap:preview",
        target_digest=fix.preview.payload_hash,
        ref_name=REF_NAME,
        expected_ref=RefValue(artifact_id=fix.base.artifact_id, revision=1),
    )


def _item(fix: _Fixture, *, approval_id=APPROVAL_ID, series_id=SERIES_ID, status, revision, run_id):
    scope = DomainScope(domain_ids=(_DOMAIN,))
    ref = _domain_ref()
    return ApprovalItem(
        approval_id=approval_id,
        subject_series_id=series_id,
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id=fix.patch.artifact_id,
        subject_digest=fix.patch.payload_hash,
        status=status,
        workflow_revision=revision,
        proposer=AuditActor(principal_id="human:proposer", principal_kind="human"),
        domain_scope=scope,
        domain_registry_ref=ref,
        route_policy=DomainRoutePolicyRefV1(
            route_version="routes@1", route_digest="5" * 64, domain_registry_ref=ref
        ),
        role_policy_version="roles@1",
        role_policy_digest="6" * 64,
        approval_policy=ApprovalPolicyRefV1(policy_version="approval@1", policy_digest="7" * 64),
        requirements=(
            ApprovalRequirement(
                requirement_id="requirement:narrative",
                domain_scope=scope,
                required_permission=Permission(
                    action="approval.decide", resource_kind="approval", domain_scope=scope
                ),
                route_role="content_designer",
                min_approvals=1,
                assignee_principal_ids=("human:approver",),
                distinct_from_requirement_ids=(),
            ),
        ),
        decisions=(),
        regression_evidence_artifact_ids=(),
        target_binding=_target_binding(fix),
        active_validation_run_id=run_id,
        created_at=NOW,
    )


def _payload(fix: _Fixture) -> PatchValidationPayloadV1:
    return PatchValidationPayloadV1(
        subject=ValidationSubjectBindingV1(
            approval_id=APPROVAL_ID,
            expected_workflow_revision=2,
            subject_head_revision=1,
            subject_artifact_id=fix.patch.artifact_id,
            subject_digest=fix.patch.payload_hash,
            active_validation_run_id=RUN_ID,
        ),
        base_snapshot_artifact_id=fix.base.artifact_id,
        preview_snapshot_artifact_id=fix.preview.artifact_id,
        candidate_config_export_artifact_ids=(),
        target=RefReadBindingV1(
            ref_name=REF_NAME, expected_ref=RefValue(artifact_id=fix.base.artifact_id, revision=1)
        ),
        validation_policy=ProfileRefV1(profile_id="builtin.validation", version=1),
        checker_profiles=(),
        simulation_profiles=(),
        findings=(),
        review_artifact_ids=(),
        playtest_trace_artifact_ids=(),
        regression_suite_artifact_ids=(),
    )


def _evidence(fix: _Fixture, *, overall: str) -> EvidenceSet:
    return EvidenceSet(
        subject_artifact_id=fix.patch.artifact_id,
        subject_digest=fix.patch.payload_hash,
        policy_version="builtin.validation@1",
        validation_run_id=RUN_ID,
        target_binding=_target_binding(fix),
        supporting_artifact_ids=(),
        finding_bindings=(),
        requirements=(),
        overall_status=overall,
    )


def _run_record(fix: _Fixture):
    from tests.platform.m4c.handler_support import build_envelope, build_run_record

    envelope = build_envelope(params=_payload(fix))
    return build_run_record(envelope, RunKindRef(kind="patch.validate", version=1), run_id=RUN_ID)


def _policy(outcome_code: str, effect_key: str) -> OutcomeArtifactPolicyV1:
    return OutcomeArtifactPolicyV1(
        policy_schema_version="outcome-artifact-policy@1",
        policy_id=f"{outcome_code}-policy",
        policy_version=1,
        outcome_code=outcome_code,
        prepared_outcome="failure",
        publication_scope="run",
        run_status_after_publication="failed",
        failure_class="execution",
        retry_disposition="terminal",
        artifact_rules=(),
        workflow_effect_key=effect_key,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition", policy_version=1, digest=_HEX
        ),
    )


def _context(fix, *, approvals, evidence, primary_id, policy):
    return WorkflowEffectContext(
        run=_run_record(fix),
        policy=policy,
        scope="run",
        published_primary_artifact_id=primary_id,
        published_output_artifact_ids=(),
        approvals=approvals,
        actor=_WORKER,
        occurred_at=NOW,
        published_primary_payload=None if evidence is None else evidence.model_dump(mode="json"),
    )


def _seed_validating(fix: _Fixture, repo: SqlApprovalRepository) -> None:
    draft = _item(fix, status="draft", revision=1, run_id=None)
    repo.insert_draft(draft)
    repo.compare_and_set_subject_head(
        SERIES_ID,
        None,
        SubjectHead(
            subject_series_id=SERIES_ID,
            current_subject_artifact_id=fix.patch.artifact_id,
            current_approval_id=APPROVAL_ID,
            revision=1,
        ),
    )
    validating = _item(fix, status="validating", revision=2, run_id=RUN_ID)
    repo.compare_and_set(APPROVAL_ID, 1, validating)


def _run_effect(fix, *, effect_key, target_status, evidence_overall, primary_id):
    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        _seed_validating(fix, repo)
        evidence = None if evidence_overall is None else _evidence(fix, overall=evidence_overall)
        outcome_code = (
            "patch_validation_passed" if target_status == "validated" else "execution_failed"
        )
        apply_workflow_effect(
            effect_key,
            _context(
                fix,
                approvals=repo,
                evidence=evidence,
                primary_id=primary_id,
                policy=_policy(outcome_code, effect_key),
            ),
        )
    with fix.session() as session:
        return SqlApprovalRepository(session).get(APPROVAL_ID)


def test_set_patch_validated_effect_cases_item_to_validated(tmp_path: Path) -> None:
    fix = _Fixture(tmp_path)
    item = _run_effect(
        fix,
        effect_key="set_patch_validated@1",
        target_status="validated",
        evidence_overall="passed",
        primary_id=fix.evidence_artifact.artifact_id,
    )
    assert item is not None and item.status == "validated"
    assert item.workflow_revision == 3
    assert item.active_validation_run_id is None
    assert item.evidence_set_artifact_id == fix.evidence_artifact.artifact_id


def test_restore_current_draft_effect_reverts_validating_to_draft(tmp_path: Path) -> None:
    fix = _Fixture(tmp_path)
    item = _run_effect(
        fix,
        effect_key="restore_current_draft@1",
        target_status="draft",
        evidence_overall=None,
        primary_id=fix.failure_artifact.artifact_id,
    )
    assert item is not None and item.status == "draft"
    assert item.active_validation_run_id is None
    assert item.last_validation_failure_artifact_id == fix.failure_artifact.artifact_id


def test_set_patch_validation_failed_fails_closed_on_passed_evidence(tmp_path: Path) -> None:
    # ``set_patch_validation_failed@1`` demands overall failed/unproven; a passed
    # EvidenceSet is an integrity mismatch (the publisher's outcome selection would
    # never pair them) → fail-closed, no mutation.
    fix = _Fixture(tmp_path)
    with pytest.raises(IntegrityViolation):
        _run_effect(
            fix,
            effect_key="set_patch_validation_failed@1",
            target_status="validation_failed",
            evidence_overall="passed",
            primary_id=fix.evidence_artifact.artifact_id,
        )


def test_missing_approvals_capability_fails_closed(tmp_path: Path) -> None:
    fix = _Fixture(tmp_path)
    context = WorkflowEffectContext(
        run=_run_record(fix),
        policy=_policy("patch_validation_passed", "set_patch_validated@1"),
        scope="run",
        published_primary_artifact_id=fix.evidence_artifact.artifact_id,
        published_output_artifact_ids=(),
        approvals=None,
        actor=_WORKER,
        occurred_at=NOW,
        published_primary_payload=_evidence(fix, overall="passed").model_dump(mode="json"),
    )
    with pytest.raises(IntegrityViolation):
        apply_workflow_effect("set_patch_validated@1", context)


def test_set_patch_validated_never_revives_superseded_subject(tmp_path: Path) -> None:
    fix = _Fixture(tmp_path)
    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        _seed_validating(fix, repo)
        # Supersede the validating item: it goes validating→superseded and a new
        # revision becomes the current SubjectHead. The effect must observe the
        # non-current head and NOT mutate/revive the superseded item.
        repo.compare_and_set(
            APPROVAL_ID, 2, _item(fix, status="superseded", revision=3, run_id=None)
        )
        successor = _item(
            fix, approval_id="approval:2", status="draft", revision=1, run_id=None
        ).model_copy(update={"subject_revision": 2, "supersedes_approval_id": APPROVAL_ID})
        repo.insert_draft(successor)
        repo.compare_and_set_subject_head(
            SERIES_ID,
            SubjectHead(
                subject_series_id=SERIES_ID,
                current_subject_artifact_id=fix.patch.artifact_id,
                current_approval_id=APPROVAL_ID,
                revision=1,
            ),
            SubjectHead(
                subject_series_id=SERIES_ID,
                current_subject_artifact_id=fix.patch.artifact_id,
                current_approval_id="approval:2",
                revision=2,
            ),
        )
        before = repo.get(APPROVAL_ID)
        apply_workflow_effect(
            "set_patch_validated@1",
            _context(
                fix,
                approvals=repo,
                evidence=_evidence(fix, overall="passed"),
                primary_id=fix.evidence_artifact.artifact_id,
                policy=_policy("patch_validation_passed", "set_patch_validated@1"),
            ),
        )
        after = repo.get(APPROVAL_ID)
    assert before is not None and after is not None
    assert after.status == "superseded"
    assert after.evidence_set_artifact_id is None
    assert after == before
