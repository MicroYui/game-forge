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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import Request
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import (
    ApiDependencies,
    RunEventPage,
    RunEventStreamConfig,
    require_actor,
)
from gameforge.apps.api.streaming import (
    HEARTBEAT_COMMENT,
    RunEventNotifier,
    RunEventReadScope,
    RunEventStreamService,
    _parse_last_event_id,
    render_run_event_stream,
)
from gameforge.apps.api.workflow_command_port import WorkflowCommandAdapter
from gameforge.contracts.api import compute_resource_etag, encode_sse_event
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cost import BudgetV1, CostAmountV1
from gameforge.contracts.errors import CursorInvalid, Forbidden, IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import PatchV2
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
    RetryDecisionV1,
    RunEvent,
    RunTerminatedDataV1,
)
from gameforge.contracts.lineage import AuditActor, VersionTuple, build_artifact_v2
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import ApprovalItem, PatchTargetBindingV1, SubjectHead
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
from gameforge.platform.runs.commands import RunClaimRequest, RunCommandService
from gameforge.platform.runs.lifecycle import AttemptWriteFence
from gameforge.platform.workflow.service import WorkflowCommandService
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine, sqlite_read_snapshot_session
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from tests.apps.api.run_command_testkit import (
    ORIGIN,
    SESSION_COOKIE,
    SESSION_TOKEN,
    CommandAppHarness,
    human_actor,
)
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


def _shared_budget(*, budget_id: str, scope_kind: str, scope_id: str) -> BudgetV1:
    return BudgetV1(
        budget_id=budget_id,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        policy_version="sse-shared-budget@1",
        limits=(
            CostAmountV1(dimension="request", value=10_000_000, unit="request"),
            CostAmountV1(dimension="concurrent_run", value=16, unit="count"),
        ),
        reserved=(),
        consumed=(),
        status="active",
        revision=1,
        created_at=NOW_DT,
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

    def get_subject_head(self, subject_series_id: str) -> SubjectHead | None:
        if subject_series_id != self._item.subject_series_id:
            return None
        return SubjectHead(
            subject_series_id=self._item.subject_series_id,
            current_subject_artifact_id=self._item.subject_artifact_id,
            current_approval_id=self._item.approval_id,
            revision=self._item.subject_revision,
        )

    def compare_and_set(
        self,
        approval_id: str,
        expected_revision: int,
        replacement: ApprovalItem,
    ) -> ApprovalItem:
        if (
            approval_id != self._item.approval_id
            or expected_revision != self._item.workflow_revision
        ):
            raise IntegrityViolation("test approval CAS is stale")
        self._item = replacement
        return replacement


class _TestValidationStartWriter:
    def start(
        self,
        transaction: Any,
        *,
        item: ApprovalItem,
        run_id: str,
        actor: ActorContext,
        request_id: str | None,
        trace_id: str | None,
    ) -> None:
        del actor, request_id, trace_id
        current = transaction.approvals.get(item.approval_id)
        if (
            current is not None
            and current.status == "validating"
            and current.active_validation_run_id == run_id
        ):
            return
        if (
            current is None
            or current.status != "draft"
            or current.workflow_revision != item.workflow_revision
        ):
            raise IntegrityViolation("test validation start requires the exact draft")
        replacement = current.model_copy(
            update={
                "status": "validating",
                "workflow_revision": current.workflow_revision + 1,
                "active_validation_run_id": run_id,
            }
        )
        transaction.approvals.compare_and_set(
            current.approval_id,
            current.workflow_revision,
            replacement,
        )


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
            costs = SqlCostLedger(session, clock=self.clock)
            costs.put_budget(
                _shared_budget(
                    budget_id="budget:principal:human:maker",
                    scope_kind="principal",
                    scope_id="human:maker",
                )
            )
            costs.put_budget(
                _shared_budget(
                    budget_id="budget:system:global",
                    scope_kind="system",
                    scope_id="global",
                )
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
            current_principal_resolver=lambda _tx, actor: actor.principal,
            role_policy_version=ROLE_POLICY_VERSION,
            role_policy_digest=self.role_policy.policy_digest,
            validation_start_writer=_TestValidationStartWriter(),
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
            approvals=self.approvals,
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
                object_bindings=bindings,
                runs=SqlRunRepository(session),
                routing=SqlCostLedger(session, clock=self.clock),
            )

    @contextmanager
    def _event_read_scope(self) -> Iterator[RunEventReadScope]:
        with sqlite_read_snapshot_session(self.engine) as session:
            yield RunEventReadScope(
                runs=SqlRunRepository(session),
                policies=SqlPolicySnapshotRepository(session, clock=self.clock),
                approvals=self.approvals,
            )

    def set_actor(self, actor: ActorContext) -> None:
        self._actor_holder["actor"] = actor

    # ── run + event seeding ──────────────────────────────────────────────
    def admit_checker_run(self, tag: str = "snap") -> str:
        artifact = self.seed_payload_artifact(
            kind="ir_snapshot",
            payload={"snapshot_id": f"snapshot:{tag}", "entities": [], "relations": []},
            version_tuple=VersionTuple(
                ir_snapshot_id=f"snapshot:{tag}",
                tool_version=f"{tag}@1",
            ),
            lineage=(),
            payload_schema_id="ir-core@1",
        )
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

    def seed_payload_artifact(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        version_tuple: VersionTuple,
        lineage: tuple[str, ...],
        payload_schema_id: str,
    ) -> Any:
        blob = canonical_json(payload).encode("utf-8")
        stored = self.objects.put_verified(blob)
        artifact = build_artifact_v2(
            kind=kind,  # type: ignore[arg-type]
            version_tuple=version_tuple,
            lineage=lineage,
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": payload_schema_id,
                "domain_scope": DomainScope(domain_ids=("builtin",)).model_dump(mode="json"),
            },
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
        """Admit a real patch.validate Run whose subject domain is ``builtin``.

        The subject ApprovalItem (domain_scope=builtin) is served to BOTH the
        admission read port and the stream's domain reader via ``self.approvals``.
        """

        base = self.seed_payload_artifact(
            kind="ir_snapshot",
            payload={
                "snapshot_id": f"snapshot:{tag}:base",
                "entities": [],
                "relations": [],
            },
            version_tuple=VersionTuple(
                ir_snapshot_id=f"snapshot:{tag}:base",
                tool_version="ir@1",
            ),
            lineage=(),
            payload_schema_id="ir-core@1",
        )
        patch = PatchV2(
            revision=1,
            base_snapshot_id=base.version_tuple.ir_snapshot_id,
            target_snapshot_id=f"snapshot:{tag}:preview",
            expected_to_fix=[],
            preconditions=[],
            side_effect_risk="low",
            ops=[],
            produced_by="human",
            producer_run_id=None,
            rationale="SSE validation fixture",
        )
        subject = self.seed_payload_artifact(
            kind="patch",
            payload=patch.model_dump(mode="json"),
            version_tuple=VersionTuple(
                ir_snapshot_id=base.version_tuple.ir_snapshot_id,
                tool_version="patch@2",
            ),
            lineage=(base.artifact_id,),
            payload_schema_id="patch@2",
        )
        preview = self.seed_payload_artifact(
            kind="ir_snapshot",
            payload={
                "snapshot_id": patch.target_snapshot_id,
                "entities": [],
                "relations": [],
            },
            version_tuple=VersionTuple(
                ir_snapshot_id=patch.target_snapshot_id,
                tool_version="patch@2",
            ),
            lineage=(base.artifact_id, subject.artifact_id),
            payload_schema_id="ir-core@1",
        )
        initial = validation_testkit.approval_item(
            subject=subject,
            target=preview,
            kind="patch",
            approval_id=f"approval:{tag}",
        )
        expected_ref = RefValue(artifact_id=base.artifact_id, revision=1)
        binding = PatchTargetBindingV1(
            target_artifact_id=preview.artifact_id,
            target_snapshot_id=preview.version_tuple.ir_snapshot_id,
            target_digest=preview.payload_hash,
            ref_name="content/head",
            expected_ref=expected_ref,
        )
        item = ApprovalItem.model_validate(
            {
                **initial.model_dump(mode="json"),
                "target_binding": binding.model_dump(mode="json"),
            }
        )
        registry_ref = DomainRegistryRefV1(
            registry_version=self.domain_registry.registry_version,
            registry_digest=self.domain_registry.registry_digest,
        )
        scope = DomainScope(domain_ids=("builtin",))
        item = item.model_copy(
            update={
                "status": "draft",
                "active_validation_run_id": None,
                "domain_scope": scope,
                "domain_registry_ref": registry_ref,
                "route_policy": item.route_policy.model_copy(
                    update={"domain_registry_ref": registry_ref}
                ),
                "role_policy_version": self.role_policy.policy_version,
                "role_policy_digest": self.role_policy.policy_digest,
                "requirements": tuple(
                    requirement.model_copy(
                        update={
                            "domain_scope": scope,
                            "required_permission": requirement.required_permission.model_copy(
                                update={"domain_scope": scope}
                            ),
                        }
                    )
                    for requirement in item.requirements
                ),
            }
        )
        self.approvals = _FixedApprovals(item)
        with Session(self.engine) as session, session.begin():
            SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=self.clock),
                clock=self.clock,
            ).compare_and_set("content/head", None, base.artifact_id)
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
                "expected_ref": expected_ref.model_dump(mode="json"),
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
                headers={
                    "Idempotency-Key": f"patch-validate:{tag}",
                    "If-Match": compute_resource_etag(
                        resource_kind="patch",
                        resource_id=subject.artifact_id,
                        revision=item.workflow_revision,
                    ),
                },
            )
        assert response.status_code == 202, response.text
        return response.json()["run_id"]


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


# ── real Run aggregate helpers ────────────────────────────────────────────
def _session_client(harness: CommandAppHarness, *, app: Any | None = None) -> TestClient:
    client = TestClient(harness.app if app is None else app, base_url=ORIGIN)
    client.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
    return client


def _cancel_existing_run(harness: CommandAppHarness, run_id: str, *, tag: str) -> None:
    body = harness.cancel_body(
        command_id=f"cmd:{tag}",
        idempotency_key=f"cancel:{tag}",
        expected_run_revision=1,
    )
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.post(f"/api/v1/runs/{run_id}:cancel", json=body)
    assert response.status_code == 200, response.text
    run = harness.run_record(run_id)
    assert run.status == "cancelled"
    assert run.next_event_seq == 4


def _cancel_queued_run(harness: CommandAppHarness, *, tag: str) -> str:
    """Create and cancel through the real command UoW; no synthetic event rows."""

    run_id = harness.admit_checker_run(tag)
    _cancel_existing_run(harness, run_id, tag=tag)
    return run_id


def _terminal_attempt_run_without_command(harness: CommandAppHarness, *, tag: str) -> str:
    """Build a three-event terminal aggregate without a command/event FK."""

    run_id = harness.admit_checker_run(tag)
    worker = AuditActor(
        principal_id=f"service:sse-retention:{tag}",
        principal_kind="service",
    )
    claim = harness.command_service.claim_next(
        RunClaimRequest(
            worker=worker,
            lease_id=f"lease:sse-retention:{tag}",
            lease_duration_ns=30_000_000_000,
        )
    )
    assert claim is not None and claim.run.run_id == run_id
    decision = RetryDecisionV1(
        cause_code="cancelled",
        failure_class="cancelled",
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="not_retry_eligible",
        classifier=claim.run.failure_classifier,
        retry_policy=claim.run.retry_policy,
        evaluated_at_utc=NOW,
    )
    terminal_event = RunEvent(
        run_id=run_id,
        seq=claim.run.next_event_seq,
        event_type="run.cancelled",
        attempt_no=claim.attempt.attempt_no,
        occurred_at=NOW,
        data_schema_version="run-terminated@1",
        data=RunTerminatedDataV1(
            attempt_no=claim.attempt.attempt_no,
            failure_artifact_id=harness._failure_artifact_id,
            cause_code="cancelled",
        ),
    )
    with harness.uow.begin() as transaction:
        terminal = transaction.runs.close_attempt_terminal(
            fence=AttemptWriteFence(
                run_id=run_id,
                attempt_no=claim.attempt.attempt_no,
                expected_run_revision=claim.run.revision,
                lease_id=claim.lease.lease_id,
                fencing_token=claim.attempt.fencing_token,
            ),
            ended_at=NOW,
            attempt_status="cancelled",
            lease_status="closed",
            run_status="cancelled",
            failure_class="cancelled",
            attempt_failure_artifact_id=harness._failure_artifact_id,
            run_failure_artifact_id=harness._failure_artifact_id,
            attempt_cassette_artifact_id=None,
            terminal_cassette_artifact_id=None,
            retry_decision=decision,
            leading_events=(),
            terminal_event=terminal_event,
        )
    assert terminal.run.next_event_seq == 4
    return run_id


def _persisted_event(harness: CommandAppHarness, run_id: str, seq: int) -> RunEvent:
    with Session(harness.engine) as session:
        event = SqlRunRepository(session).get_event(run_id, seq)
    assert event is not None
    return event


def _prune_events_through(harness: CommandAppHarness, run_id: str, seq: int) -> None:
    with Session(harness.engine) as session, session.begin():
        removed = SqlRunRepository(session).prune_terminal_event_prefix(
            run_id,
            before_seq=seq + 1,
        )
    assert removed == seq


def _restarted_stream_app(harness: CommandAppHarness) -> Any:
    restarted_stream = RunEventStreamService(
        read_scope=harness._event_read_scope,
        role_policy_version=harness.role_policy.policy_version,
        role_policy_digest=harness.role_policy.policy_digest,
    )
    dependencies = replace(
        harness.app.state.dependencies,
        run_event_stream=restarted_stream,
        run_event_notifier=RunEventNotifier(),
    )
    return create_app(dependencies)


# ── cursor + frozen-authorization regressions ─────────────────────────────
def _cursor_request(value: str | None) -> Request:
    headers = [] if value is None else [(b"last-event-id", value.encode("utf-8"))]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/runs/run:x/events",
            "headers": headers,
        }
    )


@pytest.mark.parametrize(
    "value",
    ("", " ", "00", "01", "not-a-sequence", "-1", "+1", "1.0", "１２", str(1 << 63)),
)
def test_malformed_or_overflow_last_event_id_is_rejected(value: str) -> None:
    with pytest.raises(CursorInvalid):
        _parse_last_event_id(_cursor_request(value))


def test_absent_last_event_id_is_the_only_fresh_cursor() -> None:
    assert _parse_last_event_id(_cursor_request(None)) is None
    assert _parse_last_event_id(_cursor_request("0")) == 0


def test_future_last_event_id_returns_invalid_cursor_problem(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _cancel_queued_run(harness, tag="future-cursor")

    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "4"},
        )

    assert response.status_code == 400, response.text
    assert Problem.model_validate(response.json()).code == "invalid_cursor"


def test_stream_authorization_uses_frozen_admission_domain_scope(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    builtin_scope = DomainScope(domain_ids=("builtin",))
    harness.set_actor(_actor(scope=builtin_scope))
    run_id = harness.admit_checker_run("frozen-domain")
    with Session(harness.engine) as session:
        run = SqlRunRepository(session).get(run_id)
    assert run is not None and run.resource_domain_scope == builtin_scope

    grant = harness.stream.authorize_stream(
        run_id=run_id,
        actor=_actor(scope=builtin_scope),
        after_seq=0,
    )
    assert grant.earliest_retained_seq == grant.latest_event_seq == 1
    assert grant.terminal is False


def test_validation_stream_uses_frozen_scope_after_approval_reader_disappears(
    tmp_path: Path,
) -> None:
    harness = AppHarness(tmp_path)
    run_id = harness.admit_patch_validate_run("frozen-validation")
    harness.approvals = None

    grant = harness.stream.authorize_stream(
        run_id=run_id,
        actor=_actor(scope=DomainScope(domain_ids=("builtin",))),
        after_seq=0,
    )

    assert grant.latest_event_seq == 1


def test_legacy_resource_run_without_frozen_scope_falls_back_conservatively(
    tmp_path: Path,
) -> None:
    from gameforge.apps.api.streaming import _resolve_run_read_domain

    harness = AppHarness(tmp_path)
    run_id = harness.admit_checker_run("legacy-domain")
    with Session(harness.engine) as session:
        run = SqlRunRepository(session).get_run_projection(run_id)
    assert run is not None
    legacy = run.model_copy(update={"resource_domain_scope": None})

    resolved = _resolve_run_read_domain(legacy, harness.domain_registry, None)

    assert isinstance(resolved, DomainScope)
    assert set(resolved.domain_ids) == set(DOMAIN_IDS)


def test_legacy_validation_run_rejects_mismatched_approval_subject_scope(
    tmp_path: Path,
) -> None:
    from gameforge.apps.api.streaming import _resolve_run_read_domain

    harness = AppHarness(tmp_path)
    run_id = harness.admit_patch_validate_run("legacy-validation-mismatch")
    with Session(harness.engine) as session:
        run = SqlRunRepository(session).get_run_projection(run_id)
    assert run is not None
    assert harness.approvals is not None
    legacy = run.model_copy(update={"resource_domain_scope": None})
    mismatched = harness.approvals._item.model_copy(  # noqa: SLF001
        update={"subject_artifact_id": "artifact:unrelated"}
    )

    resolved = _resolve_run_read_domain(
        legacy,
        harness.domain_registry,
        _FixedApprovals(mismatched),
    )

    assert isinstance(resolved, DomainScope)
    assert set(resolved.domain_ids) == set(DOMAIN_IDS)


def test_resolve_run_read_domain_dr_drill_is_domainless(tmp_path: Path) -> None:
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
    assert _resolve_run_read_domain(fake_run, harness.domain_registry, None) is None


# ═══════════════════════ real HTTP/SQLite integration ═════════════════════
def test_stream_delivers_real_cancel_events_and_closes_on_terminal(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _cancel_queued_run(harness, tag="terminal-backlog")

    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["Cache-Control"] == "no-store"
    frames = _parse_frames(response.text)
    assert _event_ids(frames) == [1, 2, 3]
    assert [frame["event"] for frame in frames] == [
        "run.queued",
        "run.cancel_requested",
        "run.cancelled",
    ]
    assert encode_sse_event(_persisted_event(harness, run_id, 1)) in response.text


def test_last_event_id_replays_only_real_committed_suffix(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _cancel_queued_run(harness, tag="resume")

    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )

    assert response.status_code == 200, response.text
    assert _event_ids(_parse_frames(response.text)) == [2, 3]


def test_cross_connection_redelivery_is_deduplicable_by_run_and_seq(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _cancel_queued_run(harness, tag="cross-connection-dedup")

    with TestClient(harness.app, base_url=ORIGIN) as client:
        first = client.get(f"/api/v1/runs/{run_id}/events")
        # Simulate a client that processed seq 2 but only durably stored cursor 1.
        replay = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )

    first_ids = _event_ids(_parse_frames(first.text))
    replay_ids = _event_ids(_parse_frames(replay.text))
    assert first_ids == [1, 2, 3]
    assert replay_ids == [2, 3]
    combined = [(run_id, seq) for seq in (*first_ids, *replay_ids)]
    assert len(combined) == 5
    assert sorted(set(combined)) == [(run_id, 1), (run_id, 2), (run_id, 3)]


def test_terminal_exact_head_reconnect_closes_without_heartbeat(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _cancel_queued_run(harness, tag="exact-terminal-head")

    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "3"},
        )

    assert response.status_code == 200, response.text
    assert response.text == ""
    assert HEARTBEAT_COMMENT not in response.text


def test_missing_run_returns_404(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.get("/api/v1/runs/run:does-not-exist/events")
    assert response.status_code == 404, response.text


def test_unauthorized_actor_is_forbidden(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run("forbidden")
    harness.set_http_actor(human_actor(authorized=False))
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")
    assert response.status_code == 403, response.text


def test_revoked_permission_cannot_reconnect(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _cancel_queued_run(harness, tag="reconnect-revoked")
    harness.set_http_actor(human_actor(authorized=False))

    with TestClient(harness.app, base_url=ORIGIN) as client:
        reconnect = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )

    assert reconnect.status_code == 403, reconnect.text


def test_retention_expiry_returns_410_with_actual_earliest_cursor(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _terminal_attempt_run_without_command(harness, tag="retention-expired")
    _prune_events_through(harness, run_id, 2)

    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )

    assert response.status_code == 410, response.text
    problem = Problem.model_validate(response.json())
    assert problem.code == "cursor_expired"
    assert problem.earliest_cursor == "3"


def test_explicit_zero_resume_after_retention_returns_410(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _terminal_attempt_run_without_command(harness, tag="retention-explicit-zero")
    _prune_events_through(harness, run_id, 2)

    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "0"},
        )

    assert response.status_code == 410, response.text
    problem = Problem.model_validate(response.json())
    assert problem.code == "cursor_expired"
    assert problem.earliest_cursor == "3"


def test_fresh_connect_after_pruning_streams_from_actual_minimum(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _cancel_queued_run(harness, tag="retention-fresh")
    _prune_events_through(harness, run_id, 1)

    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")

    assert response.status_code == 200, response.text
    assert response.headers["X-Earliest-Event-Cursor"] == "2"
    assert _event_ids(_parse_frames(response.text)) == [2, 3]


def test_event_read_scope_holds_one_physical_snapshot_across_retention(
    tmp_path: Path,
) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _terminal_attempt_run_without_command(harness, tag="retention-read-snapshot")

    with harness._event_read_scope() as scope:
        raw_connection = scope.runs._session.connection().connection.driver_connection
        assert raw_connection.in_transaction is True
        run = scope.runs.get_run_projection(run_id)
        assert run is not None
        assert scope.runs.earliest_event_seq(run_id) == 1

        # A WAL writer may prune concurrently, but the page read remains on the
        # snapshot established above instead of mixing the old MIN with a new suffix.
        _prune_events_through(harness, run_id, 2)
        page = scope.runs.stream_events(run_id, after_seq=0, limit=16)

    assert tuple(event.seq for event in page) == (1, 2, 3)
    with Session(harness.engine) as session:
        assert SqlRunRepository(session).earliest_event_seq(run_id) == 3


def test_restart_replays_persisted_suffix_with_new_stream_and_notifier(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _cancel_queued_run(harness, tag="restart")
    restarted_app = _restarted_stream_app(harness)

    with TestClient(restarted_app, base_url=ORIGIN) as client:
        client.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
        response = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )

    assert response.status_code == 200, response.text
    assert _event_ids(_parse_frames(response.text)) == [2, 3]


class _PruneAfterFirstPage:
    def __init__(self, harness: CommandAppHarness) -> None:
        self._harness = harness
        self._delegate = harness.stream
        self._pruned = False

    def authorize_stream(self, **kwargs: Any) -> Any:
        return self._delegate.authorize_stream(**kwargs)

    def read_authorized_page(self, **kwargs: Any) -> RunEventPage:
        page = self._delegate.read_authorized_page(**kwargs)
        if not self._pruned:
            self._pruned = True
            _prune_events_through(self._harness, kwargs["run_id"], 2)
        return page


def test_midstream_retention_gap_closes_then_reconnect_returns_410(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _terminal_attempt_run_without_command(harness, tag="midstream-prune")
    pruning_port = _PruneAfterFirstPage(harness)
    app = create_app(
        replace(
            harness.app.state.dependencies,
            run_event_stream=pruning_port,
            run_event_stream_config=RunEventStreamConfig(
                page_limit=1,
                heartbeat_seconds=0.01,
            ),
        )
    )

    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
        first = client.get(f"/api/v1/runs/{run_id}/events")
        reconnect = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )

    assert first.status_code == 200, first.text
    assert _event_ids(_parse_frames(first.text)) == [1]
    assert reconnect.status_code == 410, reconnect.text
    problem = Problem.model_validate(reconnect.json())
    assert problem.code == "cursor_expired"
    assert problem.earliest_cursor == "3"


class _RevokeSessionOnWaitSubscription:
    def __init__(self, harness: CommandAppHarness) -> None:
        self._harness = harness
        self.wait_calls = 0
        self.closed = False

    async def wait(self, timeout: float) -> bool:
        del timeout
        self.wait_calls += 1
        self._harness.revoke_session()
        return False

    def close(self) -> None:
        self.closed = True


class _RevokeSessionOnWaitNotifier:
    def __init__(self, harness: CommandAppHarness, run_id: str) -> None:
        self._run_id = run_id
        self.subscription = _RevokeSessionOnWaitSubscription(harness)

    def subscribe(self, run_id: str) -> _RevokeSessionOnWaitSubscription:
        assert run_id == self._run_id
        return self.subscription

    def notify(self, run_id: str) -> None:
        assert run_id == self._run_id


class _RevokeRoleOnWaitSubscription:
    def __init__(self, harness: CommandAppHarness) -> None:
        self._harness = harness
        self.wait_calls = 0
        self.closed = False

    async def wait(self, timeout: float) -> bool:
        del timeout
        self.wait_calls += 1
        self._harness.set_session_actor(human_actor(authorized=False))
        return False

    def close(self) -> None:
        self.closed = True


class _RevokeRoleOnWaitNotifier:
    def __init__(self, harness: CommandAppHarness, run_id: str) -> None:
        self._run_id = run_id
        self.subscription = _RevokeRoleOnWaitSubscription(harness)

    def subscribe(self, run_id: str) -> _RevokeRoleOnWaitSubscription:
        assert run_id == self._run_id
        return self.subscription

    def notify(self, run_id: str) -> None:
        assert run_id == self._run_id


def test_real_session_revoke_while_waiting_closes_before_heartbeat(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run("open-session-revoke")
    notifier = _RevokeSessionOnWaitNotifier(harness, run_id)
    app = create_app(
        replace(
            harness.app.state.dependencies,
            run_event_notifier=notifier,
            run_event_stream_config=RunEventStreamConfig(
                page_limit=256,
                heartbeat_seconds=0.01,
            ),
        )
    )

    with _session_client(harness, app=app) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")
        reconnect = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )

    assert response.status_code == 200, response.text
    assert _event_ids(_parse_frames(response.text)) == [1]
    assert HEARTBEAT_COMMENT not in response.text
    assert notifier.subscription.wait_calls == 1
    assert notifier.subscription.closed is True
    assert reconnect.status_code == 401, reconnect.text
    assert Problem.model_validate(reconnect.json()).code == "auth_failed"


def test_current_role_revoke_while_waiting_stops_before_next_page(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run("open-role-revoke")
    notifier = _RevokeRoleOnWaitNotifier(harness, run_id)
    app = create_app(
        replace(
            harness.app.state.dependencies,
            run_event_notifier=notifier,
            run_event_stream_config=RunEventStreamConfig(
                page_limit=256,
                heartbeat_seconds=0.01,
            ),
        )
    )

    with _session_client(harness, app=app) as client:
        response = client.get(f"/api/v1/runs/{run_id}/events")
        reconnect = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "1"},
        )

    assert response.status_code == 200, response.text
    assert _event_ids(_parse_frames(response.text)) == [1]
    assert HEARTBEAT_COMMENT not in response.text
    assert notifier.subscription.wait_calls == 1
    assert notifier.subscription.closed is True
    assert reconnect.status_code == 403, reconnect.text
    assert Problem.model_validate(reconnect.json()).code == "forbidden"


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
                attempt_no=None,
                failure_artifact_id="artifact:x",
                cause_code="boom",
            ),
        )
    return RunEvent(
        run_id="run:core",
        seq=seq,
        event_type="run.command_accepted",
        occurred_at=NOW,
        data_schema_version="command-accepted@1",
        data=CommandAcceptedDataV1(
            command_id=f"cmd:{seq}",
            command_type="cancel",
            command_revision=1,
        ),
    )


def _page(
    events: tuple[RunEvent, ...],
    *,
    latest: int,
    terminal: bool = False,
    earliest: int = 1,
) -> RunEventPage:
    return RunEventPage(
        events=events,
        earliest_retained_seq=earliest,
        latest_event_seq=latest,
        terminal=terminal,
    )


class _ScriptedReader:
    def __init__(self, pages: list[RunEventPage]) -> None:
        self._pages = list(pages)
        self.after_seqs: list[int] = []

    async def __call__(
        self,
        run_id: str,
        actor: ActorContext,
        after_seq: int,
        limit: int,
    ) -> RunEventPage:
        del run_id, actor, limit
        self.after_seqs.append(after_seq)
        if not self._pages:
            raise AssertionError("scripted reader exhausted")
        return self._pages.pop(0)


class _ScriptedSubscription:
    def __init__(self, results: list[bool]) -> None:
        self._results = list(results)
        self.calls = 0

    async def wait(self, timeout: float) -> bool:
        del timeout
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        raise AssertionError("scripted subscription exhausted")


class _NeverWait:
    async def wait(self, timeout: float) -> bool:
        del timeout
        raise AssertionError("full persisted pages must apply backpressure without waiting")


class _DisconnectAfter:
    def __init__(self, calls_allowed: int) -> None:
        self._calls_allowed = calls_allowed
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self._calls_allowed


async def _refresh_core_actor() -> ActorContext:
    return _actor()


def _drain(agen: Any) -> list[str]:
    async def _run() -> list[str]:
        chunks: list[str] = []
        async for chunk in agen:
            chunks.append(chunk)
        return chunks

    return asyncio.run(_run())


def test_real_db_notifier_boundary_race_rereads_committed_terminal_suffix(
    tmp_path: Path,
) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run("boundary-race")
    read_after: list[int] = []

    async def scenario() -> list[str]:
        wait_started = asyncio.Event()
        delegate = harness.notifier.subscribe(run_id)

        class BarrierSubscription:
            async def wait(self, timeout: float) -> bool:
                wait_started.set()
                return await delegate.wait(timeout)

        async def read_page(
            selected_run_id: str,
            actor: ActorContext,
            after_seq: int,
            limit: int,
        ) -> RunEventPage:
            read_after.append(after_seq)
            return await asyncio.to_thread(
                harness.stream.read_authorized_page,
                run_id=selected_run_id,
                actor=actor,
                after_seq=after_seq,
                limit=limit,
            )

        async def writer() -> None:
            await wait_started.wait()
            await asyncio.to_thread(
                _cancel_existing_run,
                harness,
                run_id,
                tag="boundary-race",
            )
            harness.notifier.notify(run_id)

        async def consume() -> list[str]:
            chunks: list[str] = []
            async for chunk in render_run_event_stream(
                run_id=run_id,
                after_seq=0,
                read_page=read_page,
                refresh_actor=_refresh_core_actor,
                subscription=BarrierSubscription(),
                config=RunEventStreamConfig(page_limit=256, heartbeat_seconds=2.0),
            ):
                chunks.append(chunk)
            return chunks

        try:
            chunks, _ = await asyncio.gather(consume(), writer())
            return chunks
        finally:
            delegate.close()

    chunks = asyncio.run(scenario())
    assert _event_ids(_parse_frames("".join(chunks))) == [1, 2, 3]
    assert read_after == [0, 1]


def test_core_boundary_race_reread_has_no_gap() -> None:
    reader = _ScriptedReader(
        [
            _page((_core_event(1), _core_event(2), _core_event(3)), latest=3),
            _page((), latest=3),
            _page(
                (_core_event(4), _core_event(5, terminal=True)),
                latest=5,
                terminal=True,
            ),
        ]
    )
    subscription = _ScriptedSubscription([True, True])
    chunks = _drain(
        render_run_event_stream(
            run_id="run:core",
            after_seq=0,
            read_page=reader,
            refresh_actor=_refresh_core_actor,
            subscription=subscription,
            config=RunEventStreamConfig(page_limit=256, heartbeat_seconds=1.0),
        )
    )

    frames = _parse_frames("".join(chunks))
    assert _event_ids(frames) == [1, 2, 3, 4, 5]
    assert all(frame["comment"] is False for frame in frames)
    assert reader.after_seqs == [0, 3, 3]


def test_core_heartbeat_is_comment_and_does_not_advance_cursor() -> None:
    reader = _ScriptedReader(
        [
            _page((_core_event(1), _core_event(2), _core_event(3)), latest=3),
            _page((), latest=3),
        ]
    )
    subscription = _ScriptedSubscription([False])
    disconnect = _DisconnectAfter(calls_allowed=1)
    chunks = _drain(
        render_run_event_stream(
            run_id="run:core",
            after_seq=0,
            read_page=reader,
            refresh_actor=_refresh_core_actor,
            subscription=subscription,
            config=RunEventStreamConfig(page_limit=256, heartbeat_seconds=0.01),
            is_disconnected=disconnect,
        )
    )

    joined = "".join(chunks)
    frames = _parse_frames(joined)
    assert _event_ids(frames) == [1, 2, 3]
    heartbeats = [frame for frame in frames if frame["comment"]]
    assert len(heartbeats) == 1
    assert heartbeats[0]["id"] is None
    assert HEARTBEAT_COMMENT in joined


def test_wait_revocation_stops_before_heartbeat_or_later_event() -> None:
    reader = _ScriptedReader([_page((_core_event(1),), latest=1)])
    subscription = _ScriptedSubscription([False])
    refresh_calls = 0

    async def refresh_actor() -> ActorContext:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls > 1:
            raise Forbidden("permission revoked while stream was waiting")
        return _actor()

    async def scenario() -> str:
        stream = render_run_event_stream(
            run_id="run:core",
            after_seq=0,
            read_page=reader,
            refresh_actor=refresh_actor,
            subscription=subscription,
            config=RunEventStreamConfig(page_limit=256, heartbeat_seconds=0.01),
        )
        first = await anext(stream)
        with pytest.raises(Forbidden):
            await anext(stream)
        return first

    first = asyncio.run(scenario())
    assert _event_ids(_parse_frames(first)) == [1]
    assert HEARTBEAT_COMMENT not in first
    assert subscription.calls == 1


def test_real_db_pages_are_pull_backpressured_by_slow_consumer(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = _cancel_queued_run(harness, tag="slow-consumer")
    read_after: list[int] = []

    async def read_page(
        selected_run_id: str,
        actor: ActorContext,
        after_seq: int,
        limit: int,
    ) -> RunEventPage:
        read_after.append(after_seq)
        return await asyncio.to_thread(
            harness.stream.read_authorized_page,
            run_id=selected_run_id,
            actor=actor,
            after_seq=after_seq,
            limit=limit,
        )

    async def refresh_actor() -> ActorContext:
        return human_actor()

    async def scenario() -> tuple[str, str, str]:
        stream = render_run_event_stream(
            run_id=run_id,
            after_seq=0,
            read_page=read_page,
            refresh_actor=refresh_actor,
            subscription=_NeverWait(),
            config=RunEventStreamConfig(page_limit=1, heartbeat_seconds=1.0),
        )
        first = await anext(stream)
        assert read_after == [0]
        await asyncio.sleep(0)
        assert read_after == [0]
        second = await anext(stream)
        assert read_after == [0, 1]
        third = await anext(stream)
        assert read_after == [0, 1, 2]
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return first, second, third

    chunks = asyncio.run(scenario())
    assert _event_ids(_parse_frames("".join(chunks))) == [1, 2, 3]
