"""Resumable Server-Sent-Events transport tests (M4c Task 15a).

Drives the real ``GET /api/v1/runs/{id}/events`` endpoint through the FastAPI
``TestClient`` over a DB-backed harness (real SQLite + object store + builtin
registry/catalog; no network), plus deterministic core-generator tests that
prove the boundary-race reread, heartbeat framing, and client dedup invariants
without concurrency.

Every persisted RunEvent is encoded with the FROZEN ``encode_sse_event``; the
event ``id`` is always the persisted ``seq`` (never invented). Heartbeats are
SSE comments that do not advance the resume cursor. Retention/410 derives the
earliest cursor from the retained event store (MIN seq), never a speculative
column. Authorization is re-checked on every connection/reconnect.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Iterator

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import (
    ApiDependencies,
    RunEventStreamConfig,
    require_actor,
)
from gameforge.apps.api.streaming import (
    HEARTBEAT_COMMENT,
    RunEventNotifier,
    RunEventReadScope,
    RunEventStreamService,
    render_run_event_stream,
)
from gameforge.apps.api.workflow_command_port import WorkflowCommandAdapter
from gameforge.contracts.api import encode_sse_event
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    Permission,
    Principal,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.jobs import (
    CommandAcceptedDataV1,
    Problem,
    RunEvent,
    RunTerminatedDataV1,
)
from gameforge.contracts.lineage import AuditActor
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
from gameforge.runtime.persistence.models import Base, RunEventRow
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from tests.platform.m4 import validation_testkit

NOW_DT = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-15T12:00:00Z"
CURSOR_KEY = b"m4c-sse-cursor-key"
OBJECT_CURSOR_KEY = b"m4c-sse-object-cursor-key"
AUDIT_CHAIN_ID = "platform-authority"
CHECKER_PROFILE = ProfileRefV1(profile_id="builtin.checker", version=1)
VALIDATION_PROFILE = ProfileRefV1(profile_id="builtin.validation", version=1)

ROLE_POLICY_VERSION = "sse-roles@1"
DOMAIN_REGISTRY_VERSION = "sse-domains@1"
DOMAIN_IDS = ("builtin", "economy", "narrative")
_TOOLING_GRANTS: tuple[tuple[str, str], ...] = (
    ("run", "checker"),
    ("run", "simulation"),
    ("run", "review"),
    ("run", "bench"),
    ("run", "playtest"),
    ("validate", "patch"),
    ("read", "run"),
)


def _domain_registry() -> DomainRegistryV1:
    definitions = tuple(
        DomainDefinitionV1(domain_id=domain_id, display_name=domain_id.title(), status="active")
        for domain_id in DOMAIN_IDS
    )
    return DomainRegistryV1(
        registry_version=DOMAIN_REGISTRY_VERSION,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(DOMAIN_REGISTRY_VERSION, definitions),
    )


def _role_policy(registry: DomainRegistryV1) -> RolePolicy:
    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants = {
        "tooling": tuple(
            Permission(action=action, resource_kind=resource_kind, domain_scope="all")
            for action, resource_kind in _TOOLING_GRANTS
        )
    }
    effective_from = "2026-07-15T00:00:00Z"
    return RolePolicy(
        policy_version=ROLE_POLICY_VERSION,
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            ROLE_POLICY_VERSION, registry_ref, grants, effective_from
        ),
    )


def _tooling_assignment(principal_id: str, *, scope: object = "all") -> RoleAssignmentV1:
    return RoleAssignmentV1(
        assignment_id="assign:tooling",
        principal_id=principal_id,
        role="tooling",
        scope=scope,  # type: ignore[arg-type]
        status="active",
        revision=1,
        granted_at=NOW,
        granted_by=AuditActor(principal_id="human:admin", principal_kind="human"),
    )


def _actor(*, authorized: bool = True, scope: object = "all") -> ActorContext:
    principal_id = "human:maker"
    roles = (_tooling_assignment(principal_id, scope=scope),) if authorized else ()
    principal = Principal(
        id=principal_id,
        kind="human",
        display_name="maker",
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=roles,
    )
    return ActorContext(
        principal=principal,
        authentication=AuthenticationContext(mechanism="session", credential_id="credential:human"),
        session_id="session:human",
        request_id="request:human",
    )


class _FixedApprovals:
    """Return one ApprovalItem by id (admission read port + stream domain reader)."""

    def __init__(self, item: Any) -> None:
        self._item = item

    def get(self, approval_id: str) -> Any:
        return self._item if approval_id == self._item.approval_id else None


class _Stub:
    """Placeholder for the non-validate workflow collaborators (never invoked)."""


class AppHarness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        page_limit: int = 256,
        heartbeat_seconds: float = 0.2,
    ) -> None:
        self.clock = FrozenUtcClock(NOW_DT)
        self.engine = get_engine(f"sqlite:///{tmp_path / 'sse.db'}")
        Base.metadata.create_all(self.engine)
        self.objects = LocalObjectStore(
            tmp_path / "objects",
            store_id="local",
            clock=self.clock,
            cursor_signing_key=OBJECT_CURSOR_KEY,
        )
        self.registry = build_builtin_registry()
        self.catalog = self.registry.list_execution_profile_catalogs()[0]
        self.domain_registry = _domain_registry()
        self.role_policy = _role_policy(self.domain_registry)
        with Session(self.engine) as session, session.begin():
            policies = SqlPolicySnapshotRepository(session, clock=self.clock)
            policies.put_execution_profile_catalog(self.catalog)
            policies.put_domain_registry(self.domain_registry)
            policies.put_role_policy(self.role_policy)
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
            role_policy_version=ROLE_POLICY_VERSION,
            role_policy_digest=self.role_policy.policy_digest,
        )
        self.notifier = RunEventNotifier()
        self.stream = RunEventStreamService(
            read_scope=self._event_read_scope,
            role_policy_version=ROLE_POLICY_VERSION,
            role_policy_digest=self.role_policy.policy_digest,
        )
        workflow_service = WorkflowCommandService(
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
                run_admission=self.admission,
                workflow_commands=WorkflowCommandAdapter(workflow_service),
                run_event_stream=self.stream,
                run_event_notifier=self.notifier,
                run_event_stream_config=RunEventStreamConfig(
                    page_limit=page_limit,
                    heartbeat_seconds=heartbeat_seconds,
                ),
                request_id_factory=lambda: "request:test",
            )
        )
        self.app.dependency_overrides[require_actor] = lambda: self._actor_holder["actor"]

    @contextmanager
    def _unused_read_scope(self) -> Iterator[Any]:
        raise AssertionError("validate admission must not touch the workflow read scope")
        yield  # pragma: no cover

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
    def _event_read_scope(self) -> Iterator[RunEventReadScope]:
        with Session(self.engine) as session:
            yield RunEventReadScope(
                runs=SqlRunRepository(session),
                policies=SqlPolicySnapshotRepository(session, clock=self.clock),
                approvals=self.approvals,
            )

    def set_actor(self, actor: ActorContext) -> None:
        self._actor_holder["actor"] = actor

    # ── run + event seeding ──────────────────────────────────────────────
    def admit_checker_run(self, tag: str = "snap") -> str:
        stored = self.objects.put_verified(f"ir_snapshot:{tag}".encode("utf-8"))
        from gameforge.contracts.lineage import VersionTuple, build_artifact_v2

        artifact = build_artifact_v2(
            kind="ir_snapshot",
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
        body = {
            "request_schema_version": "run-submission-request@1",
            "params": {
                "schema_version": "checker-run@1",
                "snapshot_artifact_id": artifact.artifact_id,
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
        with TestClient(self.app) as client:
            response = client.post(
                "/api/v1/runs", json=body, headers={"Idempotency-Key": f"checker:{tag}"}
            )
        assert response.status_code == 202, response.text
        return response.json()["run_id"]

    def seed_artifact(self, *, kind: str, tag: str) -> Any:
        from gameforge.contracts.lineage import VersionTuple, build_artifact_v2

        stored = self.objects.put_verified(f"{kind}:{tag}".encode("utf-8"))
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

    def admit_patch_validate_run(self, tag: str = "pv") -> str:
        """Admit a real patch.validate Run whose subject domain is ``narrative``.

        The subject ApprovalItem (domain_scope=narrative) is served to BOTH the
        admission read port and the stream's domain reader via ``self.approvals``.
        """

        subject = self.seed_artifact(kind="patch", tag=f"{tag}-subject")
        base = self.seed_artifact(kind="ir_snapshot", tag=f"{tag}-base")
        preview = self.seed_artifact(kind="ir_snapshot", tag=f"{tag}-preview")
        item = validation_testkit.approval_item(
            subject=subject, target=base, kind="patch", approval_id=f"approval:{tag}"
        )
        self.approvals = _FixedApprovals(item)
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
        with TestClient(self.app) as client:
            response = client.post(
                f"/api/v1/patches/{subject.artifact_id}:validate",
                json=body,
                headers={"Idempotency-Key": f"patch-validate:{tag}", "If-Match": '"etag:1"'},
            )
        assert response.status_code == 202, response.text
        return response.json()["run_id"]

    def _next_seq(self, run_id: str) -> int:
        with Session(self.engine) as session:
            highest = session.execute(
                select(func.max(RunEventRow.seq)).where(RunEventRow.run_id == run_id)
            ).scalar_one_or_none()
        return (int(highest) + 1) if highest is not None else 1

    def seed_event(self, run_id: str, *, terminal: bool = False, seq: int | None = None) -> int:
        selected_seq = seq if seq is not None else self._next_seq(run_id)
        if terminal:
            event = RunEvent(
                run_id=run_id,
                seq=selected_seq,
                event_type="run.cancelled",
                occurred_at=NOW,
                data_schema_version="run-terminated@1",
                data=RunTerminatedDataV1(
                    attempt_no=None,
                    failure_artifact_id="artifact:terminal",
                    cause_code="operator_cancel",
                ),
            )
        else:
            event = RunEvent(
                run_id=run_id,
                seq=selected_seq,
                event_type="run.command_accepted",
                occurred_at=NOW,
                data_schema_version="command-accepted@1",
                data=CommandAcceptedDataV1(
                    command_id=f"cmd:{selected_seq}",
                    command_type="cancel",
                    command_revision=1,
                ),
            )
        with Session(self.engine) as session, session.begin():
            session.add(RunEventRow(**event.model_dump(mode="json")))
        return selected_seq

    def seed_events(self, run_id: str, count: int, *, terminal_last: bool = False) -> list[int]:
        seqs: list[int] = []
        for index in range(count):
            is_terminal = terminal_last and index == count - 1
            seqs.append(self.seed_event(run_id, terminal=is_terminal))
        return seqs

    def prune_events_through(self, run_id: str, seq: int) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                delete(RunEventRow).where(
                    RunEventRow.run_id == run_id,
                    RunEventRow.seq <= seq,
                )
            )

    def persisted_event(self, run_id: str, seq: int) -> RunEvent:
        with Session(self.engine) as session:
            event = SqlRunRepository(session).get_event(run_id, seq)
        assert event is not None
        return event


# ── SSE frame parsing ────────────────────────────────────────────────────
def _parse_frames(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block:
            continue
        frame: dict[str, Any] = {"comment": False, "id": None, "event": None, "data": None}
        for line in block.split("\n"):
            if line.startswith(":"):
                frame["comment"] = True
                frame["comment_body"] = line
            elif line.startswith("id:"):
                frame["id"] = line[len("id:") :]
            elif line.startswith("event:"):
                frame["event"] = line[len("event:") :]
            elif line.startswith("data:"):
                frame["data"] = line[len("data:") :]
        frames.append(frame)
    return frames


def _event_ids(frames: list[dict[str, Any]]) -> list[int]:
    return [int(frame["id"]) for frame in frames if frame["id"] is not None]


# ═══════════════════════ HTTP integration tests ═══════════════════════════
def test_stream_delivers_backlog_and_closes_on_terminal(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    run_id = harness.admit_checker_run()  # seq 1 = run.queued
    harness.seed_events(run_id, 3)  # seq 2,3,4
    harness.seed_event(run_id, terminal=True)  # seq 5 = run.cancelled (terminal)
    with TestClient(harness.app) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["Cache-Control"] == "no-store"
    frames = _parse_frames(response.text)
    assert _event_ids(frames) == [1, 2, 3, 4, 5]  # gapless, persisted seqs as ids
    assert frames[-1]["event"] == "run.cancelled"  # closes on terminal
    # Exact FROZEN framing: the queued event is byte-identical to encode_sse_event.
    queued = harness.persisted_event(run_id, 1)
    assert encode_sse_event(queued) in response.text


def test_last_event_id_resumes_after_seq(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    harness.seed_events(run_id, 4)  # seq 2..5
    harness.seed_event(run_id, terminal=True)  # seq 6
    with TestClient(harness.app) as client:
        response = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "3"},
        )
    assert response.status_code == 200, response.text
    frames = _parse_frames(response.text)
    assert _event_ids(frames) == [4, 5, 6]  # resumes strictly after seq 3


def test_paged_delivery_is_gapless_across_pages(tmp_path: Path) -> None:
    # A tiny page limit forces multiple bounded DB rereads (backpressure): the
    # stream must never buffer unbounded and must deliver every seq in order.
    harness = AppHarness(tmp_path, page_limit=2)
    run_id = harness.admit_checker_run()
    harness.seed_events(run_id, 5)  # seq 2..6
    harness.seed_event(run_id, terminal=True)  # seq 7
    with TestClient(harness.app) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")
    assert response.status_code == 200, response.text
    assert _event_ids(_parse_frames(response.text)) == [1, 2, 3, 4, 5, 6, 7]


def test_missing_run_returns_404(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    with TestClient(harness.app) as client:
        response = client.get("/api/v1/runs/run:does-not-exist/events")
    assert response.status_code == 404, response.text


def test_unauthorized_actor_is_forbidden(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    harness.seed_event(run_id, terminal=True)
    harness.set_actor(_actor(authorized=False))
    with TestClient(harness.app) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")
    assert response.status_code == 403, response.text


def test_revoked_permission_cannot_reconnect(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    harness.seed_event(run_id, terminal=True)
    with TestClient(harness.app) as client:
        first = client.get(f"/api/v1/runs/{run_id}/events")
        assert first.status_code == 200, first.text
        # Permission revoked between connections; the reconnect must reauthorize.
        harness.set_actor(_actor(authorized=False))
        reconnect = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )
    assert reconnect.status_code == 403, reconnect.text


def test_retention_expiry_returns_410_with_earliest_cursor(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    harness.seed_events(run_id, 5)  # seq 2..6
    # Simulate retention pruning of the earliest events; MIN(seq) becomes 4.
    harness.prune_events_through(run_id, 3)
    with TestClient(harness.app) as client:
        response = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "2"},  # next expected 3 < earliest retained 4
        )
    assert response.status_code == 410, response.text
    problem = Problem.model_validate(response.json())
    assert problem.code == "cursor_expired"
    assert problem.earliest_cursor == "4"


def test_fresh_connect_after_pruning_streams_from_earliest(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    harness.seed_events(run_id, 4)  # seq 2..5
    harness.seed_event(run_id, terminal=True)  # seq 6
    harness.prune_events_through(run_id, 3)  # MIN(seq) becomes 4
    with TestClient(harness.app) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")  # no Last-Event-ID
    assert response.status_code == 200, response.text
    assert _event_ids(_parse_frames(response.text)) == [4, 5, 6]


def test_boundary_race_reread_delivers_late_events_without_gap(tmp_path: Path) -> None:
    # Read backlog -> wait -> a concurrent writer commits later events + notify ->
    # the DB reread (authority) catches them with no gap. A blocking client.get
    # only returns once the terminal event is streamed, so completion is
    # deterministic; the heartbeat timeout guarantees a reread even if notify is
    # lost.
    harness = AppHarness(tmp_path, heartbeat_seconds=0.1)
    run_id = harness.admit_checker_run()
    harness.seed_events(run_id, 2)  # backlog seq 2,3

    def _late_writer() -> None:
        harness.seed_event(run_id)  # seq 4
        harness.seed_event(run_id)  # seq 5
        harness.seed_event(run_id, terminal=True)  # seq 6
        harness.notifier.notify(run_id)

    writer = threading.Thread(target=_late_writer)
    writer.start()
    try:
        with TestClient(harness.app) as client:
            response = client.get(f"/api/v1/runs/{run_id}/events")
    finally:
        writer.join(timeout=10)
    assert response.status_code == 200, response.text
    assert _event_ids(_parse_frames(response.text)) == [1, 2, 3, 4, 5, 6]


# ── Fix wave 1: SSE read-domain gate must mirror admission's domain derivation ──
def test_validation_run_events_readable_by_subject_domain_scoped_principal(
    tmp_path: Path,
) -> None:
    # A patch.validate run's read domain is its loaded subject's ApprovalItem domain
    # (here "narrative"), NOT all-active. A principal scoped only to "narrative" must
    # be able to read its OWN validation run's events (200, not a wrong 403).
    harness = AppHarness(tmp_path)
    run_id = harness.admit_patch_validate_run()
    harness.seed_event(run_id, terminal=True)
    harness.set_actor(_actor(scope=DomainScope(domain_ids=("narrative",))))
    with TestClient(harness.app) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")
    assert response.status_code == 200, response.text
    assert _event_ids(_parse_frames(response.text))[0] == 1


def test_all_active_run_forbidden_for_subject_domain_scoped_principal(tmp_path: Path) -> None:
    # Contrast: a checker run resolves fail-closed to authority over ALL active
    # domains, so a "narrative"-only principal is correctly forbidden — proving the
    # validation 200 above comes from the narrower, admission-aligned domain.
    harness = AppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    harness.seed_event(run_id, terminal=True)
    harness.set_actor(_actor(scope=DomainScope(domain_ids=("narrative",))))
    with TestClient(harness.app) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")
    assert response.status_code == 403, response.text


def test_resolve_run_read_domain_dr_drill_is_domainless(tmp_path: Path) -> None:
    # dr.drill is the sole domainless kind (admission: base.domain_scope is None);
    # its read must be non-domain (None), not all-active. dr.drill is internal-only
    # (not HTTP-admittable), so this is asserted directly against the resolver.
    from types import SimpleNamespace

    from gameforge.apps.api.streaming import _resolve_run_read_domain
    from gameforge.contracts.jobs import DrDrillPayloadV1

    harness = AppHarness(tmp_path)
    params = DrDrillPayloadV1(
        dr_plan=ProfileRefV1(profile_id="builtin.dr_plan", version=1),
        recovery_catalog_entry_id="catalog:1",
        expected_checkpoint_id="checkpoint:1",
        restore_target_profile=ProfileRefV1(profile_id="builtin.dr_restore", version=1),
        verification_profile=ProfileRefV1(profile_id="builtin.dr_verify", version=1),
        destroy_restored_target_after_verification=True,
    )
    fake_run = SimpleNamespace(payload=SimpleNamespace(params=params))
    resolved = _resolve_run_read_domain(fake_run, harness.domain_registry, None)
    assert resolved is None


def test_resolve_run_read_domain_falls_back_to_all_active_for_resource_kinds(
    tmp_path: Path,
) -> None:
    # The 7 resource kinds carry no per-run domain binding yet -> fail-closed to
    # every active registry domain, exactly as admission does.
    from gameforge.apps.api.streaming import _resolve_run_read_domain

    harness = AppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with Session(harness.engine) as session:
        run = SqlRunRepository(session).get_run_projection(run_id)
    resolved = _resolve_run_read_domain(run, harness.domain_registry, None)
    assert isinstance(resolved, DomainScope)
    assert set(resolved.domain_ids) == set(DOMAIN_IDS)


# ═══════════════════════ deterministic core-generator tests ═══════════════
def _core_event(seq: int, *, terminal: bool = False) -> RunEvent:
    if terminal:
        return RunEvent(
            run_id="run:core",
            seq=seq,
            event_type="run.failed",
            occurred_at=NOW,
            data_schema_version="run-terminated@1",
            data=RunTerminatedDataV1(
                attempt_no=None, failure_artifact_id="artifact:x", cause_code="boom"
            ),
        )
    return RunEvent(
        run_id="run:core",
        seq=seq,
        event_type="run.command_accepted",
        occurred_at=NOW,
        data_schema_version="command-accepted@1",
        data=CommandAcceptedDataV1(
            command_id=f"cmd:{seq}", command_type="cancel", command_revision=1
        ),
    )


class _ScriptedReader:
    def __init__(self, pages: list[tuple[RunEvent, ...]]) -> None:
        self._pages = list(pages)
        self.after_seqs: list[int] = []

    def __call__(self, run_id: str, after_seq: int, limit: int) -> tuple[RunEvent, ...]:
        self.after_seqs.append(after_seq)
        if self._pages:
            return self._pages.pop(0)
        return ()


class _ScriptedSubscription:
    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)

    async def wait(self, timeout: float) -> bool:
        if self._results:
            return self._results.pop(0)
        return False


class _DisconnectAfter:
    def __init__(self, calls_allowed: int) -> None:
        self._calls_allowed = calls_allowed
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self._calls_allowed


def _drain(agen: Any) -> list[str]:
    async def _run() -> list[str]:
        chunks: list[str] = []
        async for chunk in agen:
            chunks.append(chunk)
        return chunks

    return asyncio.run(_run())


def test_core_boundary_race_reread_has_no_gap() -> None:
    # backlog [1,2,3] -> wait(notified) -> empty reread (lost/early notify) ->
    # wait(notified) -> [4,5(terminal)]: no committed-event gap, terminal closes.
    reader = _ScriptedReader(
        [
            (_core_event(1), _core_event(2), _core_event(3)),
            (),
            (_core_event(4), _core_event(5, terminal=True)),
        ]
    )
    subscription = _ScriptedSubscription([True, True])
    config = RunEventStreamConfig(page_limit=256, heartbeat_seconds=1.0)
    chunks = _drain(
        render_run_event_stream(
            run_id="run:core",
            after_seq=0,
            read_events=reader,
            subscription=subscription,
            config=config,
        )
    )
    frames = _parse_frames("".join(chunks))
    assert _event_ids(frames) == [1, 2, 3, 4, 5]  # gapless
    assert all(frame["comment"] is False for frame in frames)  # no heartbeat
    # The reread always uses the last delivered seq as the cursor (never invents).
    assert reader.after_seqs == [0, 3, 3]


def test_core_duplicate_transport_delivery_is_client_deduplicable() -> None:
    # A reconnect-style replay can redeliver an already-seen seq; every id is the
    # persisted seq so the client dedupes by (run_id, seq).
    reader = _ScriptedReader(
        [
            (_core_event(1), _core_event(2), _core_event(3)),
            (_core_event(3), _core_event(4), _core_event(5, terminal=True)),
        ]
    )
    subscription = _ScriptedSubscription([True])
    config = RunEventStreamConfig(page_limit=256, heartbeat_seconds=1.0)
    chunks = _drain(
        render_run_event_stream(
            run_id="run:core",
            after_seq=0,
            read_events=reader,
            subscription=subscription,
            config=config,
        )
    )
    frames = _parse_frames("".join(chunks))
    ids = _event_ids(frames)
    assert ids == [1, 2, 3, 3, 4, 5]  # duplicate seq 3 present
    # The duplicated frames carry the identical persisted (run_id, seq) id.
    duplicated = [frame for frame in frames if frame["id"] == "3"]
    assert len(duplicated) == 2
    assert all(frame["event"] == "run.command_accepted" for frame in duplicated)


def test_core_heartbeat_is_comment_and_does_not_advance_cursor() -> None:
    reader = _ScriptedReader([(_core_event(1), _core_event(2), _core_event(3))])
    subscription = _ScriptedSubscription([False])  # timeout -> heartbeat
    disconnect = _DisconnectAfter(calls_allowed=1)
    config = RunEventStreamConfig(page_limit=256, heartbeat_seconds=0.01)
    chunks = _drain(
        render_run_event_stream(
            run_id="run:core",
            after_seq=0,
            read_events=reader,
            subscription=subscription,
            config=config,
            is_disconnected=disconnect,
        )
    )
    joined = "".join(chunks)
    frames = _parse_frames(joined)
    assert _event_ids(frames) == [1, 2, 3]
    heartbeats = [frame for frame in frames if frame["comment"]]
    assert len(heartbeats) == 1
    assert heartbeats[0]["id"] is None  # comment carries no id -> cursor unchanged
    assert HEARTBEAT_COMMENT in joined
    assert HEARTBEAT_COMMENT.startswith(":")  # SSE comment framing
