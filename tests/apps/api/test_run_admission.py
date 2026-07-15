"""App-level Run admission transport tests (M4c Task 8).

Drives the real admission engine through the FastAPI ``TestClient``:

* the Task-7 ``admission=None`` seam is closed — a ``:validate`` endpoint now
  returns a real ``202 RunAccepted`` (a queued validation Run), not
  ``DependencyUnavailable``;
* ``POST /runs`` admits a generic Run and returns 202.

Real SQLite + object store + builtin registry/catalog; no network.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies, require_actor
from gameforge.apps.api.workflow_command_port import WorkflowCommandAdapter
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    Principal,
)
from gameforge.contracts.lineage import VersionTuple, build_artifact_v2
from gameforge.contracts.storage import RefValue
from gameforge.platform.provenance import (
    AuthenticatedGoalSourceWriter,
    GoalProvenancePolicy,
    build_source_kind_registry,
)
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.runs.admission import (
    AdmissionReadPort,
    RunAdmissionEngine,
    _SourceWriteCapabilities,
    build_admission_capability_binder,
)
from gameforge.platform.runs.commands import RunCommandService
from gameforge.platform.workflow.service import WorkflowCommandService
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from tests.platform.m4 import validation_testkit

NOW_DT = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-15T12:00:00Z"
CURSOR_KEY = b"m4c-app-admission-cursor-key"
OBJECT_CURSOR_KEY = b"m4c-app-admission-object-cursor-key"
AUDIT_CHAIN_ID = "platform-authority"
VALIDATION_PROFILE = ProfileRefV1(profile_id="builtin.validation", version=1)
CHECKER_PROFILE = ProfileRefV1(profile_id="builtin.checker", version=1)


def _actor(kind: str = "human") -> ActorContext:
    principal = Principal(
        id=f"{kind}:maker",
        kind=kind,  # type: ignore[arg-type]
        display_name=kind,
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=(),
    )
    mechanism = {"human": "session", "service": "api_key", "system": "trusted_internal"}[kind]
    return ActorContext(
        principal=principal,
        authentication=AuthenticationContext(
            mechanism=mechanism,  # type: ignore[arg-type]
            credential_id=None if kind == "system" else f"credential:{kind}",
        ),
        session_id=f"session:{kind}" if kind == "human" else None,
        request_id=f"request:{kind}",
    )


class _FixedApprovals:
    def __init__(self, item: Any) -> None:
        self._item = item

    def get(self, approval_id: str) -> Any:
        return self._item if approval_id == self._item.approval_id else None


class _Stub:
    """Placeholder for the non-validate workflow collaborators (never invoked)."""


class AppHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = FrozenUtcClock(NOW_DT)
        self.engine = get_engine(f"sqlite:///{tmp_path / 'app-admission.db'}")
        Base.metadata.create_all(self.engine)
        self.objects = LocalObjectStore(
            tmp_path / "objects",
            store_id="local",
            clock=self.clock,
            cursor_signing_key=OBJECT_CURSOR_KEY,
        )
        self.registry = build_builtin_registry()
        self.catalog = self.registry.list_execution_profile_catalogs()[0]
        with Session(self.engine) as session, session.begin():
            SqlPolicySnapshotRepository(session, clock=self.clock).put_execution_profile_catalog(
                self.catalog
            )
        self.uow = SqliteUnitOfWork(self.engine, self._capability_factory)
        self.approvals: _FixedApprovals | None = None
        run_commands = RunCommandService(
            unit_of_work=self.uow,
            bind_capabilities=build_admission_capability_binder(
                registry=self.registry, clock=self.clock, audit_chain_id=AUDIT_CHAIN_ID
            ),
            clock=self.clock,
        )
        self.admission = RunAdmissionEngine(
            run_commands=run_commands,
            unit_of_work=self.uow,
            read_scope=self._read_scope,
            registry=self.registry,
            execution_profile_catalog=self.catalog,
            goal_writer=AuthenticatedGoalSourceWriter(
                policy=GoalProvenancePolicy(registry=build_source_kind_registry())
            ),
            object_store=self.objects,
            clock=self.clock,
            source_uow_capabilities=lambda tx: _SourceWriteCapabilities(
                artifacts=tx.artifacts, object_bindings=tx.object_bindings
            ),
        )
        service = WorkflowCommandService(
            clock=self.clock,
            object_store=self.objects,
            read_scope=self._unused_read_scope,
            approval_commands=_Stub(),  # type: ignore[arg-type]
            apply_service=_Stub(),  # type: ignore[arg-type]
            rebase_service=_Stub(),  # type: ignore[arg-type]
            spec_service=_Stub(),  # type: ignore[arg-type]
            governance=None,
            scope_resolver=None,
            admission=self.admission,
            execution_profile_catalog=self.catalog,
        )
        self._actor_holder: dict[str, ActorContext] = {"actor": _actor()}
        self.app = create_app(
            ApiDependencies(
                workflow_commands=WorkflowCommandAdapter(service),
                run_admission=self.admission,
                request_id_factory=lambda: "request:test",
            )
        )
        self.app.dependency_overrides[require_actor] = lambda: self._actor_holder["actor"]

    def _capability_factory(self, session: Any) -> TransactionCapabilities:
        cursor_signer = CursorSigner(signing_key=CURSOR_KEY, clock=self.clock)
        bindings = SqlObjectBindingRepository(session, self.objects, "local")
        return TransactionCapabilities(
            refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=self.clock),
            audit=SqlAuditSink(session),
            approvals=None,
            lineage=None,
            object_bindings=bindings,
            runs=SqlRunRepository(session),
            cost=SqlCostLedger(session, clock=self.clock),
            policies=SqlPolicySnapshotRepository(session, clock=self.clock),
            idempotency=SqlIdempotencyRepository(session, clock=self.clock),
            artifacts=SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=cursor_signer,
                clock=self.clock,
            ),
        )

    @contextmanager
    def _read_scope(self) -> Iterator[AdmissionReadPort]:
        with Session(self.engine) as session:
            cursor_signer = CursorSigner(signing_key=CURSOR_KEY, clock=self.clock)
            bindings = SqlObjectBindingRepository(session, self.objects, "local")
            yield AdmissionReadPort(
                policies=SqlPolicySnapshotRepository(session, clock=self.clock),
                approvals=self.approvals,
                artifacts=SqlArtifactRepository(
                    session,
                    binding_repository=bindings,
                    cursor_signer=cursor_signer,
                    clock=self.clock,
                ),
                refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=self.clock),
            )

    @contextmanager
    def _unused_read_scope(self) -> Iterator[Any]:
        raise AssertionError("validate admission must not touch the workflow read scope")
        yield  # pragma: no cover

    def seed_artifact(self, *, kind: str, tag: str) -> Any:
        payload = f"{kind}:{tag}".encode("utf-8")
        stored = self.objects.put_verified(payload)
        artifact = build_artifact_v2(
            kind=kind,  # type: ignore[arg-type]
            version_tuple=VersionTuple(ir_snapshot_id=stored.ref.sha256, tool_version=f"{tag}@1"),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            created_at=NOW,
        )
        with Session(self.engine) as session, session.begin():
            bindings = SqlObjectBindingRepository(session, self.objects, "local")
            bindings.bind_verified(stored.ref, stored.location, None)
            SqlArtifactRepository(
                session,
                binding_repository=bindings,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=self.clock),
                clock=self.clock,
            ).put(artifact)
        return artifact

    def run_record(self, run_id: str) -> Any:
        with Session(self.engine) as session:
            return SqlRunRepository(session).get(run_id)


def _validate_headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key, "If-Match": '"etag:1"'}


# ── Task-7 seam closed: HTTP :validate → real 202 RunAccepted ────────────────
def test_patch_validate_endpoint_admits_real_run(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    subject = harness.seed_artifact(kind="patch", tag="patch-subject")
    base = harness.seed_artifact(kind="ir_snapshot", tag="base")
    preview = harness.seed_artifact(kind="ir_snapshot", tag="preview")
    item = validation_testkit.approval_item(
        subject=subject, target=base, kind="patch", approval_id="approval:patch:1"
    )
    harness.approvals = _FixedApprovals(item)
    body = {
        "request_schema_version": "patch-validation-admission-request@1",
        "approval_id": item.approval_id,
        "expected_subject_head_revision": item.subject_revision,
        "expected_workflow_revision": item.workflow_revision,
        "subject_digest": item.subject_digest,
        "base_snapshot_artifact_id": base.artifact_id,
        "preview_snapshot_artifact_id": preview.artifact_id,
        "candidate_config_export_artifact_ids": [],
        "target": {
            "ref_name": "content/head",
            "expected_ref": RefValue(artifact_id=base.artifact_id, revision=1).model_dump(
                mode="json"
            ),
        },
        "validation_policy": VALIDATION_PROFILE.model_dump(mode="json"),
        "checker_profiles": [],
        "simulation_profiles": [],
        "findings": [],
        "review_artifact_ids": [],
        "playtest_trace_artifact_ids": [],
        "regression_suite_artifact_ids": [],
    }
    with TestClient(harness.app) as client:
        response = client.post(
            f"/api/v1/patches/{subject.artifact_id}:validate",
            json=body,
            headers=_validate_headers("patch-validate:1"),
        )
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["accepted_schema_version"] == "run-accepted@1"
    run = harness.run_record(accepted["run_id"])
    assert run is not None
    assert run.status == "queued"
    assert run.kind.kind == "patch.validate"
    assert run.run_budget_hold_group_id  # real budget hold reserved in the same UoW


# ── POST /runs admits a generic checker Run ──────────────────────────────────
def test_post_runs_admits_generic_checker(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tag="snap")
    body = {
        "request_schema_version": "run-submission-request@1",
        "params": {
            "schema_version": "checker-run@1",
            "snapshot_artifact_id": snapshot.artifact_id,
            "constraint_snapshot_artifact_id": None,
            "selection": {"mode": "full", "entity_ids": [], "relation_ids": []},
            "checker_profile": CHECKER_PROFILE.model_dump(mode="json"),
            "checker_ids": [],
            "defect_classes": [],
        },
        "llm_execution_mode": "not_applicable",
        "seed": None,
        "execution_version_plan": None,
        "cassette_artifact_id": None,
    }
    with TestClient(harness.app) as client:
        response = client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": "checker:1"})
    assert response.status_code == 202, response.text
    run = harness.run_record(response.json()["run_id"])
    assert run is not None and run.kind.kind == "checker.run"
    assert run.status == "queued"


# ── POST /runs rejects an internal-only kind ─────────────────────────────────
def test_post_runs_rejects_internal_kind(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    body = {
        "request_schema_version": "run-submission-request@1",
        "params": {
            "schema_version": "artifact-migration@1",
            "source_artifact_id": "artifact:x",
            "target_payload_schema_id": "schema@1",
            "target_meta_schema_version": "meta@1",
            "target_dsl_grammar_version": None,
            "migrator": {"profile_id": "builtin.artifact_migrator", "version": 1},
            "publish_mode": "report_only",
        },
        "llm_execution_mode": "not_applicable",
    }
    with TestClient(harness.app) as client:
        response = client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": "migrate:1"})
    # internal-only kind is not admissible through the generic surface
    assert response.status_code >= 400
