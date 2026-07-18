"""Task 17b — focused coverage of the validation-completion effects (decision (a)).

Drives the registered ``publication/effects.py`` effects directly against a REAL
``SqlApprovalRepository`` over real SQLite (the same CAS the ``TerminalPublisher``
runs inside its terminal UoW), covering branches the real-admission E2E does not
reach directly: the ``validated`` / ``validation_failed`` CAS, the run-failure
``restore_current_draft@1`` revert, the pre-publication supersede guard, and the
fail-closed invariant if a validation-success effect somehow observes supersede.
"""

from __future__ import annotations

from copy import copy, deepcopy
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainRegistryRefV1, DomainScope, Permission
from gameforge.contracts.jobs import (
    OutcomeArtifactPolicyV1,
    PatchValidationPayloadV1,
    PreparedArtifact,
    PreparedRunFailure,
    PreparedRunResult,
    PreparedRunResultSummaryV1,
    RefReadBindingV1,
    ValidationSubjectBindingV1,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.lineage import (
    AuditActor,
    ObjectLocation,
    VersionTuple,
    build_artifact_v2,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalRequirement,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyProofV1,
    AutoApplyValidationProfileBindingV1,
    DomainRoutePolicyRefV1,
    EvidenceSet,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.publication.effects import (
    AutoApplyValidationRequest,
    WorkflowEffectContext,
    apply_workflow_effect,
    apply_preflighted_workflow_effect,
    commit_prepared_workflow_effect,
    preflight_prepared_workflow_effect,
    prepare_workflow_effect,
)
import gameforge.platform.publication.effects as workflow_effects
from gameforge.platform.publication.publisher import TerminalPublisher
from gameforge.platform.registry.defaults import build_builtin_registry
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


def _attempt():
    from tests.platform.m4c.handler_support import build_attempt

    return build_attempt(run_id=RUN_ID)


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


def _supersede_validating(fix: _Fixture, repo: SqlApprovalRepository) -> None:
    repo.compare_and_set(APPROVAL_ID, 2, _item(fix, status="superseded", revision=3, run_id=None))
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


class _UnusedPublicationPort:
    pass


def _terminal_publisher(approvals: SqlApprovalRepository | None) -> TerminalPublisher:
    unused = _UnusedPublicationPort()
    return TerminalPublisher(
        registry=build_builtin_registry(),
        artifacts=unused,
        blobs=unused,
        findings=unused,
        ledger=unused,
        audit=unused,
        approvals=approvals,
    )


def _prepared_validation_success(fix: _Fixture) -> PreparedRunResult:
    evidence = fix.evidence_artifact
    prepared = PreparedArtifact(
        kind="validation_evidence",
        payload_schema_id="evidence-set@1",
        version_tuple=evidence.version_tuple,
        lineage=tuple(evidence.lineage),
        payload_hash=evidence.payload_hash,
        meta={"payload_schema_id": "evidence-set@1"},
        object_ref=evidence.object_ref,
        location=ObjectLocation(
            store_id="local:test",
            key=evidence.object_ref.key,
            backend_generation="generation:test",
        ),
    )
    return PreparedRunResult(
        run_id=RUN_ID,
        attempt_no=1,
        run_kind=RunKindRef(kind="patch.validate", version=1),
        primary_index=0,
        artifacts=(prepared,),
        findings=(),
        requirement_dispositions=(),
        summary=PreparedRunResultSummaryV1(
            outcome_code="patch_validation_passed",
            primary_artifact_kind="validation_evidence",
            prepared_domain_artifact_count=1,
            prepared_finding_count=0,
        ),
    )


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


def test_validation_effect_prepares_outside_writer_and_commits_db_only(
    tmp_path: Path,
) -> None:
    fix = _Fixture(tmp_path)
    policy = _policy("patch_validation_passed", "set_patch_validated@1")
    evidence = _evidence(fix, overall="passed")
    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        _seed_validating(fix, repo)
    with fix.session() as session:
        prepared = prepare_workflow_effect(
            "set_patch_validated@1",
            _context(
                fix,
                approvals=SqlApprovalRepository(session),
                evidence=evidence,
                primary_id=fix.evidence_artifact.artifact_id,
                policy=policy,
            ),
        )
        projection = prepared.canonical_projection()
        assert projection["effect_key"] == "set_patch_validated@1"
        assert projection["payload"]["effect_kind"] == "validation_completion"  # type: ignore[index]

    with fix.session() as session, session.begin():
        statements: list[str] = []

        def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(session.get_bind(), "before_cursor_execute", _capture)
        try:
            write_context = _context(
                fix,
                approvals=SqlApprovalRepository(session),
                evidence=evidence,
                primary_id=fix.evidence_artifact.artifact_id,
                policy=policy,
            )
            preflighted = preflight_prepared_workflow_effect(prepared, write_context)
            assert not {"INSERT", "UPDATE", "DELETE"}.intersection(statements)
            apply_start = len(statements)
            apply_preflighted_workflow_effect(preflighted, write_context)
            assert "SELECT" not in statements[apply_start:]
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", _capture)
    with fix.session() as session:
        item = SqlApprovalRepository(session).get(APPROVAL_ID)
    assert item is not None and item.status == "validated"


def test_workflow_preflight_seal_is_transaction_bound_and_one_shot_before_dml(
    tmp_path: Path,
) -> None:
    fix = _Fixture(tmp_path)
    policy = _policy("patch_validation_passed", "set_patch_validated@1")
    evidence = _evidence(fix, overall="passed")
    with fix.session() as session, session.begin():
        _seed_validating(fix, SqlApprovalRepository(session))
    with fix.session() as session:
        prepared = prepare_workflow_effect(
            "set_patch_validated@1",
            _context(
                fix,
                approvals=SqlApprovalRepository(session),
                evidence=evidence,
                primary_id=fix.evidence_artifact.artifact_id,
                policy=policy,
            ),
        )

    dml: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        operation = statement.lstrip().upper().split(maxsplit=1)[0]
        if operation in {"INSERT", "UPDATE", "DELETE"}:
            dml.append(statement)

    engine = get_engine(fix.database_url)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with fix.session() as owner_session, owner_session.begin():
            owner_context = _context(
                fix,
                approvals=SqlApprovalRepository(owner_session),
                evidence=evidence,
                primary_id=fix.evidence_artifact.artifact_id,
                policy=policy,
            )
            seal = preflight_prepared_workflow_effect(prepared, owner_context)
            with pytest.raises((AttributeError, TypeError)):
                object.__setattr__(seal, "_payload", object())
            with pytest.raises(IntegrityViolation, match="invalid"):
                apply_preflighted_workflow_effect(copy(seal), owner_context)
            assert dml == []
            with fix.session() as other_session, other_session.begin():
                other_context = _context(
                    fix,
                    approvals=SqlApprovalRepository(other_session),
                    evidence=evidence,
                    primary_id=fix.evidence_artifact.artifact_id,
                    policy=policy,
                )
                with pytest.raises(IntegrityViolation, match="another transaction"):
                    apply_preflighted_workflow_effect(seal, other_context)
                assert dml == []

            apply_preflighted_workflow_effect(seal, owner_context)
            dml.clear()
            with pytest.raises(IntegrityViolation, match="reused"):
                apply_preflighted_workflow_effect(seal, owner_context)
            assert dml == []
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_workflow_preflight_rejects_one_repository_reused_in_a_later_transaction(
    tmp_path: Path,
) -> None:
    fix = _Fixture(tmp_path)
    policy = _policy("patch_validation_passed", "set_patch_validated@1")
    evidence = _evidence(fix, overall="passed")
    with fix.session() as session, session.begin():
        _seed_validating(fix, SqlApprovalRepository(session))
    with fix.session() as session:
        repository = SqlApprovalRepository(session)
        with session.begin():
            context = _context(
                fix,
                approvals=repository,
                evidence=evidence,
                primary_id=fix.evidence_artifact.artifact_id,
                policy=policy,
            )
            prepared = prepare_workflow_effect("set_patch_validated@1", context)
            seal = preflight_prepared_workflow_effect(prepared, context)
        with session.begin():
            with pytest.raises(IntegrityViolation, match="another transaction"):
                apply_preflighted_workflow_effect(seal, context)

    with fix.session() as session:
        item = SqlApprovalRepository(session).get(APPROVAL_ID)
    assert item is not None and item.status == "validating"


def test_prepared_workflow_projection_is_immutable_and_not_rehashed_in_write_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = _Fixture(tmp_path)
    policy = _policy("execution_failed", "no_workflow_change@1")
    context = _context(
        fix,
        approvals=None,
        evidence=None,
        primary_id=fix.failure_artifact.artifact_id,
        policy=policy,
    )
    prepared = prepare_workflow_effect("no_workflow_change@1", context)
    projection = prepared.canonical_projection()  # complete seal check stays lock-out

    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(prepared, "_payload", object())
    with pytest.raises(IntegrityViolation, match="trusted planner seal"):
        copy(prepared).canonical_projection()
    with pytest.raises(IntegrityViolation, match="trusted planner seal"):
        deepcopy(prepared).canonical_projection()
    with pytest.raises(TypeError, match="immutable"):
        prepared.context_selector["scope"] = "attempt"  # type: ignore[index]
    with pytest.raises(TypeError, match="immutable"):
        prepared.context_selector = {}  # type: ignore[misc]
    with pytest.raises(TypeError, match="immutable"):
        projection["payload"]["effect_kind"] = "changed"  # type: ignore[index]
    dict.__setitem__(projection, "effect_key", "changed-through-base-dict")
    assert prepared.canonical_projection()["effect_key"] == "no_workflow_change@1"

    def unexpected_full_projection_hash(_value: object) -> str:
        raise AssertionError("write-phase workflow preflight rehashed the full projection")

    monkeypatch.setattr(workflow_effects, "canonical_sha256", unexpected_full_projection_hash)
    seal = preflight_prepared_workflow_effect(prepared, context)
    apply_preflighted_workflow_effect(seal, context)


def test_prepared_validation_effect_rejects_fresh_workflow_drift(
    tmp_path: Path,
) -> None:
    fix = _Fixture(tmp_path)
    policy = _policy("patch_validation_passed", "set_patch_validated@1")
    evidence = _evidence(fix, overall="passed")
    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        _seed_validating(fix, repo)
    with fix.session() as session:
        prepared = prepare_workflow_effect(
            "set_patch_validated@1",
            _context(
                fix,
                approvals=SqlApprovalRepository(session),
                evidence=evidence,
                primary_id=fix.evidence_artifact.artifact_id,
                policy=policy,
            ),
        )
    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        current = repo.get(APPROVAL_ID)
        assert current is not None
        drifted = current.model_copy(update={"workflow_revision": current.workflow_revision + 1})
        repo.compare_and_set(APPROVAL_ID, current.workflow_revision, drifted)

    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        with pytest.raises(IntegrityViolation, match="changed after preparation"):
            commit_prepared_workflow_effect(
                prepared,
                _context(
                    fix,
                    approvals=repo,
                    evidence=evidence,
                    primary_id=fix.evidence_artifact.artifact_id,
                    policy=policy,
                ),
            )
        retained = repo.get(APPROVAL_ID)
        assert retained is not None and retained.workflow_revision == 3


class _AutoApplyGuard:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[AutoApplyValidationRequest] = []

    def validate_completion(self, request: AutoApplyValidationRequest) -> None:
        self.calls.append(request)
        assert request.projected_item.status == "validated"
        assert request.projected_item.auto_apply_proof is not None
        if self.fail:
            raise IntegrityViolation("auto-apply authority rejected proof")


def _auto_effect_fixture(fix: _Fixture):
    evidence = _evidence(fix, overall="passed")
    policy_ref = AutoApplyPolicyRefV1(
        registry=AutoApplyPolicyRegistryRefV1(
            registry_version="auto-policies@1", registry_digest="8" * 64
        ),
        policy_id="safe-structural",
        policy_version="1",
        policy_digest="9" * 64,
    )
    proof = AutoApplyProofV1(
        subject_artifact_id=fix.patch.artifact_id,
        subject_digest=fix.patch.payload_hash,
        target_binding=_target_binding(fix),
        affected_domain_scope=DomainScope(domain_ids=(_DOMAIN,)),
        validation_evidence_artifact_id=fix.evidence_artifact.artifact_id,
        regression_evidence_artifact_ids=(),
        validation_profile_binding=AutoApplyValidationProfileBindingV1(
            validation_profile=ProfileRefV1(profile_id="builtin.validation", version=1),
            validation_profile_payload_hash=_HEX,
            policy=policy_ref,
        ),
        deterministic_oracle_evidence=(),
        required_outcome_evidence=(),
        policy=policy_ref,
    )
    proof_artifact = fix._seed_artifact(
        kind="validation_evidence",
        schema="auto-apply-proof@1",
        blob=canonical_json(proof.model_dump(mode="json")).encode("utf-8"),
        version_tuple=VersionTuple(
            ir_snapshot_id="snap:preview", tool_version="patch-validation@1"
        ),
    )
    registry = build_builtin_registry()
    definition = registry.get_run_kind(RunKindRef(kind="patch.validate", version=1))
    assert definition is not None
    policy = next(
        item
        for item in definition.outcome_policies
        if item.policy_id == "patch-validation-auto-eligible"
    )
    return evidence, proof, proof_artifact, policy


@pytest.mark.parametrize("guard_mode", ["present", "missing", "reject"])
def test_auto_apply_effect_attaches_exact_proof_or_fails_closed(
    tmp_path: Path, guard_mode: str
) -> None:
    fix = _Fixture(tmp_path)
    evidence, proof, proof_artifact, policy = _auto_effect_fixture(fix)
    guard = None if guard_mode == "missing" else _AutoApplyGuard(fail=guard_mode == "reject")
    with fix.session() as session, session.begin():
        _seed_validating(fix, SqlApprovalRepository(session))
    with fix.session() as session:
        with pytest.raises(IntegrityViolation) if guard_mode != "present" else _does_not_raise():
            with session.begin():
                repo = SqlApprovalRepository(session)
                apply_workflow_effect(
                    "set_patch_validated_with_auto_proof@1",
                    WorkflowEffectContext(
                        run=_run_record(fix),
                        policy=policy,
                        scope="run",
                        published_primary_artifact_id=fix.evidence_artifact.artifact_id,
                        published_output_artifact_ids=(
                            fix.evidence_artifact.artifact_id,
                            proof_artifact.artifact_id,
                        ),
                        approvals=repo,
                        actor=_WORKER,
                        occurred_at=NOW,
                        published_primary_payload=evidence.model_dump(mode="json"),
                        published_artifact_ids_by_rule={
                            "primary": (fix.evidence_artifact.artifact_id,),
                            "regression": (),
                            "auto-apply-proof": (proof_artifact.artifact_id,),
                        },
                        published_payloads_by_rule={
                            "primary": (evidence.model_dump(mode="json"),),
                            "regression": (),
                            "auto-apply-proof": (proof.model_dump(mode="json"),),
                        },
                        published_artifacts_by_rule={
                            "primary": (fix.evidence_artifact,),
                            "regression": (),
                            "auto-apply-proof": (proof_artifact,),
                        },
                        auto_apply=guard,
                    ),
                )
        item = SqlApprovalRepository(session).get(APPROVAL_ID)

    assert item is not None
    if guard_mode == "present":
        assert item.status == "validated"
        assert item.auto_apply_proof is not None
        assert item.auto_apply_proof.proof_artifact_id == proof_artifact.artifact_id
        assert guard is not None and len(guard.calls) == 1
        request = guard.calls[0]
        assert request.evidence_artifact == fix.evidence_artifact
        assert request.proof_artifact == proof_artifact
        assert request.regression_artifacts == ()
    else:
        assert item.status == "validating"
        assert item.auto_apply_proof is None


class _does_not_raise:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> bool:
        return False


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


def test_validation_outcome_preflight_preserves_exact_current_binding(tmp_path: Path) -> None:
    fix = _Fixture(tmp_path)
    prepared = _prepared_validation_success(fix)
    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        _seed_validating(fix, repo)

        result = _terminal_publisher(repo).preflight_outcome(
            run=_run_record(fix),
            attempt=_attempt(),
            prepared=prepared,
        )

    assert result is prepared


def test_validation_outcome_preflight_discards_superseded_evidence(tmp_path: Path) -> None:
    fix = _Fixture(tmp_path)
    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        _seed_validating(fix, repo)
        _supersede_validating(fix, repo)

        result = _terminal_publisher(repo).preflight_outcome(
            run=_run_record(fix),
            attempt=_attempt(),
            prepared=_prepared_validation_success(fix),
        )

    assert isinstance(result, PreparedRunFailure)
    assert result.cause_code == "subject_superseded"
    assert result.failure_class == "subject_superseded"
    assert result.intrinsic_retry_eligible is False
    assert result.classifier == _run_record(fix).failure_classifier
    assert result.artifacts == ()
    assert result.requirement_dispositions == ()


def test_validation_outcome_preflight_rejects_unproven_noncurrent_state(
    tmp_path: Path,
) -> None:
    fix = _Fixture(tmp_path)
    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        _seed_validating(fix, repo)
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

        with pytest.raises(IntegrityViolation, match="strict superseded"):
            _terminal_publisher(repo).preflight_outcome(
                run=_run_record(fix),
                attempt=_attempt(),
                prepared=_prepared_validation_success(fix),
            )


def test_validation_outcome_preflight_requires_transaction_bound_approvals(
    tmp_path: Path,
) -> None:
    fix = _Fixture(tmp_path)
    with pytest.raises(IntegrityViolation, match="transaction-bound approvals"):
        _terminal_publisher(None).preflight_outcome(
            run=_run_record(fix),
            attempt=_attempt(),
            prepared=_prepared_validation_success(fix),
        )


def test_set_patch_validated_fails_closed_if_supersede_reaches_late_effect(
    tmp_path: Path,
) -> None:
    fix = _Fixture(tmp_path)
    with fix.session() as session, session.begin():
        repo = SqlApprovalRepository(session)
        _seed_validating(fix, repo)
        _supersede_validating(fix, repo)
        before = repo.get(APPROVAL_ID)
        with pytest.raises(IntegrityViolation, match="after subject supersede"):
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
    assert after == before
