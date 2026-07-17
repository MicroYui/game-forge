"""Task 17b — validation-completion workflow effect, proven end-to-end.

Drives REAL ``patch.validate@1`` Runs through the composed M4c stack (real
admission engine → worker ``dispatch_once`` → real ``TerminalPublisher`` →
validation-completion effect → real ``SqlApprovalRepository`` CAS) over one shared
SQLite authority + ObjectStore, and asserts the ApprovalItem transitions the three
Journey-B validation terminals:

* passed  → ``set_patch_validated@1``          → ``validating → validated``
* failed  → ``set_patch_validation_failed@1``  → ``validating → validation_failed``
* execution failure → ``restore_current_draft@1`` → ``validating → draft``

No faked publisher, no faked approvals repo. The composed ``patch.validate``
admission does NOT itself move the ApprovalItem to ``validating`` (that draft→
validating CAS is a separate command the M4c endpoint does not wire); this test
performs the transition through the real ``SqlApprovalRepository`` (fixture
bootstrap, mirroring the admin bootstrap) so the worker's terminal effect observes
exactly the state Journey B's ``patch:validate`` leaves behind.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy.orm import Session

from gameforge.apps.api.local import build_local_api_resources
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.contracts.api import PatchValidationAdmissionRequestV1
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import DomainRegistryRefV1, DomainScope, Permission
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import RefReadBindingV1
from gameforge.contracts.lineage import AuditActor, VersionTuple, build_artifact_v2
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    ApprovalPolicyRefV1,
    ApprovalRequirement,
    DomainRoutePolicyRefV1,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.runs.admission import AdmissionRequestContext
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.spine.ir.snapshot import Snapshot

from tests.e2e.m4c.test_composition import NOW, OBJECT_STORE_ID, _Harness, _tooling_actor

DOMAIN = "builtin"
REF_NAME = "content/head"
_GRAPH_CHECKER = ProfileRefV1(profile_id="builtin.checker", version=1)


def _canonical(payload: object) -> bytes:
    from gameforge.contracts.canonical import canonical_json

    return canonical_json(payload).encode("utf-8")


def _clean_snapshot() -> tuple[bytes, str]:
    # REGION is intentionally outside GraphChecker's key-node isolation rule, so
    # this fixture is genuinely checker-clean rather than merely well-formed IR.
    snapshot = Snapshot({"region:1": Entity(id="region:1", type=NodeType.REGION, attrs={})}, {})
    return _canonical(snapshot.content_payload), snapshot.snapshot_id


def _dangling_snapshot() -> tuple[bytes, str]:
    npc = Entity(id="npc:1", type=NodeType.NPC, attrs={})
    dangling = Relation(id="r1", type=EdgeType.DROPS_FROM, src_id="monster:ghost", dst_id="npc:1")
    snapshot = Snapshot({npc.id: npc}, {dangling.id: dangling})
    return _canonical(snapshot.content_payload), snapshot.snapshot_id


def _store_artifact(harness, *, kind, schema, blob, version_tuple, lineage=()):
    objects = LocalObjectStore(
        harness.object_root,
        store_id=OBJECT_STORE_ID,
        clock=harness.clock,
        cursor_signing_key=b"o" * 32,
    )
    stored = objects.put_verified(blob)
    artifact = build_artifact_v2(
        kind=kind,
        version_tuple=version_tuple,
        lineage=lineage,
        payload_hash=stored.ref.sha256,
        object_ref=stored.ref,
        meta={
            "payload_schema_id": schema,
            "domain_scope": {"domain_ids": [DOMAIN]},
        },
        created_at=NOW,
    )
    engine = get_engine(harness.database_url)
    with Session(engine) as session, session.begin():
        bindings = SqlObjectBindingRepository(session, objects, OBJECT_STORE_ID)
        bindings.bind_verified(stored.ref, stored.location, None)
        SqlArtifactRepository(
            session,
            binding_repository=bindings,
            cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=harness.clock),
            clock=harness.clock,
        ).put(artifact)
    engine.dispose()
    return artifact


def _persist(harness, run) -> None:
    engine = get_engine(harness.database_url)
    with Session(engine) as session, session.begin():
        run(SqlApprovalRepository(session))
    engine.dispose()


def _read_item(harness, approval_id):
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session:
            return SqlApprovalRepository(session).get(approval_id)
    finally:
        engine.dispose()


async def _drive(dispatcher, harness, run_id, *, max_iterations=80):
    for _ in range(max_iterations):
        run = harness.run_record(run_id)
        if run is not None and run.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            return run
        await dispatcher.dispatch_once()
    return harness.run_record(run_id)


def _draft_item(harness, *, approval_id, series_id, patch, preview, base, preview_snapshot_id):
    domain_ref = DomainRegistryRefV1(
        registry_version=harness.domain_registry.registry_version,
        registry_digest=harness.domain_registry.registry_digest,
    )
    scope = DomainScope(domain_ids=(DOMAIN,))
    return ApprovalItem(
        approval_id=approval_id,
        subject_series_id=series_id,
        subject_revision=1,
        subject_kind="patch",
        subject_artifact_id=patch.artifact_id,
        subject_digest=patch.payload_hash,
        status="draft",
        workflow_revision=1,
        proposer=AuditActor(principal_id="human:proposer", principal_kind="human"),
        domain_scope=scope,
        domain_registry_ref=domain_ref,
        route_policy=DomainRoutePolicyRefV1(
            route_version="routes@1", route_digest="5" * 64, domain_registry_ref=domain_ref
        ),
        role_policy_version=harness.role_policy.policy_version,
        role_policy_digest=harness.role_policy.policy_digest,
        approval_policy=ApprovalPolicyRefV1(
            policy_version="approval-policy@1", policy_digest="7" * 64
        ),
        requirements=(
            ApprovalRequirement(
                requirement_id="requirement:content",
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
        target_binding=PatchTargetBindingV1(
            target_artifact_id=preview.artifact_id,
            target_snapshot_id=preview_snapshot_id,
            target_digest=preview.payload_hash,
            ref_name=REF_NAME,
            expected_ref=RefValue(artifact_id=base.artifact_id, revision=1),
        ),
        created_at=NOW,
    )


def _run_validation(
    tmp_path: Path,
    *,
    approval_id: str,
    series_id: str,
    preview_blob: bytes,
    preview_snapshot_id: str,
    checker_profiles: tuple[ProfileRefV1, ...],
    idem: str,
):
    """Seed a validating Patch subject, admit patch.validate, drive it, return
    (terminal RunRecord, completed ApprovalItem)."""

    harness = _Harness(tmp_path)
    base_blob, base_snap = _clean_snapshot()
    base = _store_artifact(
        harness,
        kind="ir_snapshot",
        schema="ir-core@1",
        blob=base_blob,
        version_tuple=VersionTuple(ir_snapshot_id=base_snap, tool_version="ir-core@1"),
    )
    patch_payload = PatchV2(
        revision=1,
        base_snapshot_id=base_snap,
        target_snapshot_id=preview_snapshot_id,
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        rationale="E2E validation fixture.",
    )
    patch = _store_artifact(
        harness,
        kind="patch",
        schema="patch@2",
        blob=_canonical(patch_payload.model_dump(mode="json")),
        version_tuple=VersionTuple(ir_snapshot_id=base_snap, tool_version="patch@2"),
        lineage=(base.artifact_id,),
    )
    preview = _store_artifact(
        harness,
        kind="ir_snapshot",
        schema="ir-core@1",
        blob=preview_blob,
        version_tuple=VersionTuple(ir_snapshot_id=preview_snapshot_id, tool_version="ir-core@1"),
        lineage=(base.artifact_id, patch.artifact_id),
    )
    engine = get_engine(harness.database_url)
    with Session(engine) as session, session.begin():
        ref = SqlRefStore(
            session,
            cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=harness.clock),
            clock=harness.clock,
        ).compare_and_set(REF_NAME, None, base.artifact_id)
        assert ref == RefValue(artifact_id=base.artifact_id, revision=1)
    engine.dispose()
    item = _draft_item(
        harness,
        approval_id=approval_id,
        series_id=series_id,
        patch=patch,
        preview=preview,
        base=base,
        preview_snapshot_id=preview_snapshot_id,
    )
    head = SubjectHead(
        subject_series_id=series_id,
        current_subject_artifact_id=patch.artifact_id,
        current_approval_id=approval_id,
        revision=1,
    )
    _persist(
        harness,
        lambda r: (r.insert_draft(item), r.compare_and_set_subject_head(series_id, None, head)),
    )

    resources = build_local_api_resources(harness.api_config())
    process = build_worker_process(harness.worker_config())
    try:
        request = PatchValidationAdmissionRequestV1(
            approval_id=approval_id,
            expected_subject_head_revision=1,
            expected_workflow_revision=1,
            subject_digest=patch.payload_hash,
            base_snapshot_artifact_id=base.artifact_id,
            preview_snapshot_artifact_id=preview.artifact_id,
            candidate_config_export_artifact_ids=(),
            target=RefReadBindingV1(
                ref_name=REF_NAME, expected_ref=RefValue(artifact_id=base.artifact_id, revision=1)
            ),
            validation_policy=ProfileRefV1(profile_id="builtin.validation", version=1),
            checker_profiles=checker_profiles,
            simulation_profiles=(),
            findings=(),
            review_artifact_ids=(),
            playtest_trace_artifact_ids=(),
            regression_suite_artifact_ids=(),
        )
        accepted = resources.dependencies.run_admission.admit(
            operation="patch.validate",
            resource_id=patch.artifact_id,
            request=request,
            actor=_tooling_actor(harness),
            server=AdmissionRequestContext(
                idempotency_key=idem, request_hash="d" * 64, trace_id=None
            ),
        )
        run_id = accepted.run_id
        # Task 17c: the composed ``patch.validate`` admission CASes the ApprovalItem
        # ``draft→validating`` (bound to this Run) atomically in the same UoW, so the
        # subject is already ``validating`` at revision 2 here — no out-of-band CAS.
        started = _read_item(harness, approval_id)
        assert started is not None and started.status == "validating"
        assert started.workflow_revision == 2
        assert started.active_validation_run_id == run_id
        terminal = asyncio.run(_drive(process.dispatcher, harness, run_id))
    finally:
        process.close()
        resources.close()
    return harness, terminal, _read_item(harness, approval_id)


def _read_artifact(harness, artifact_id):
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session:
            objects = LocalObjectStore(
                harness.object_root,
                store_id=OBJECT_STORE_ID,
                clock=harness.clock,
                cursor_signing_key=b"o" * 32,
            )
            bindings = SqlObjectBindingRepository(session, objects, OBJECT_STORE_ID)
            return SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=harness.clock),
                clock=harness.clock,
            ).get(artifact_id)
    finally:
        engine.dispose()


def test_patch_validate_passed_cases_item_to_validated(tmp_path: Path) -> None:
    clean_blob, clean_snap = _clean_snapshot()
    _harness, terminal, item = _run_validation(
        tmp_path,
        approval_id="approval:passed",
        series_id="series:passed",
        preview_blob=clean_blob,
        preview_snapshot_id=clean_snap,
        checker_profiles=(_GRAPH_CHECKER,),
        idem="passed:1",
    )
    assert terminal is not None and terminal.status == "succeeded", (
        f"run terminated as {None if terminal is None else terminal.status!r}"
    )
    assert item is not None and item.status == "validated"
    assert item.workflow_revision == 3
    assert item.active_validation_run_id is None
    assert item.evidence_set_artifact_id is not None


def test_patch_validate_failed_cases_item_to_validation_failed(tmp_path: Path) -> None:
    # A dangling-relation preview makes the composed GraphChecker confirm a finding →
    # overall failed → set_patch_validation_failed@1. This also exercises the regression
    # sibling publish (#3 tool_version) + publisher sibling-lineage injection.
    dangling_blob, dangling_snap = _dangling_snapshot()
    harness, terminal, item = _run_validation(
        tmp_path,
        approval_id="approval:failed",
        series_id="series:failed",
        preview_blob=dangling_blob,
        preview_snapshot_id=dangling_snap,
        checker_profiles=(_GRAPH_CHECKER,),
        idem="failed:1",
    )
    assert terminal is not None and terminal.status == "succeeded", (
        f"run terminated as {None if terminal is None else terminal.status!r}"
    )
    assert item is not None and item.status == "validation_failed"
    assert item.workflow_revision == 3
    assert item.active_validation_run_id is None
    assert item.evidence_set_artifact_id is not None
    # The failed EvidenceSet recorded its published regression sibling.
    assert len(item.regression_evidence_artifact_ids) == 1
    regression_id = item.regression_evidence_artifact_ids[0]
    # Deliverable 4: the publisher INJECTED the content-addressed regression sibling id
    # into the EvidenceSet's published lineage (the handler could not compute it).
    evidence = _read_artifact(harness, item.evidence_set_artifact_id)
    assert evidence is not None and evidence.kind == "validation_evidence"
    assert regression_id in evidence.lineage, (
        "regression sibling was not injected into the published EvidenceSet lineage"
    )
    regression = _read_artifact(harness, regression_id)
    assert regression is not None and regression.kind == "regression_evidence"


def test_patch_validate_execution_failure_restores_draft(tmp_path: Path) -> None:
    # An unreadable preview (valid JSON, not a canonical IR object) makes the
    # deterministic handler fail → run-final execution_failed → restore_current_draft@1.
    _harness, terminal, item = _run_validation(
        tmp_path,
        approval_id="approval:restore",
        series_id="series:restore",
        preview_blob=b"[]",
        preview_snapshot_id="snapshot:restore",
        checker_profiles=(),
        idem="restore:1",
    )
    assert terminal is not None and terminal.status in {"failed", "timed_out", "cancelled"}, (
        f"run terminated as {None if terminal is None else terminal.status!r}"
    )
    assert item is not None and item.status == "draft"
    assert item.active_validation_run_id is None
    assert item.evidence_set_artifact_id is None
