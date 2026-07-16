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

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies, require_actor
from gameforge.apps.api.workflow_command_port import WorkflowCommandAdapter
from gameforge.contracts.api import PatchValidationAdmissionRequestV1, compute_resource_etag
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cost import BudgetV1, CostAmountV1
from gameforge.contracts.errors import Conflict, IntegrityViolation
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
    AdmissionRequestContext,
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
from gameforge.runtime.persistence.models import Base, RunRow
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
SIMULATION_PROFILE = ProfileRefV1(profile_id="builtin.simulation", version=1)

ROLE_POLICY_VERSION = "app-admission-roles@1"
DOMAIN_REGISTRY_VERSION = "app-admission-domains@1"
DOMAIN_IDS = ("builtin", "economy", "narrative")
_TOOLING_GRANTS: tuple[tuple[str, str], ...] = (
    ("run", "checker"),
    ("run", "simulation"),
    ("run", "review"),
    ("run", "bench"),
    ("run", "playtest"),
    ("propose", "patch"),
    ("propose", "constraint_proposal"),
    ("derive", "task_suite"),
    ("validate", "patch"),
    ("validate", "constraint_proposal"),
    ("validate", "rollback_request"),
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


def _tooling_assignment(principal_id: str) -> RoleAssignmentV1:
    return RoleAssignmentV1(
        assignment_id="assign:tooling",
        principal_id=principal_id,
        role="tooling",
        scope="all",
        status="active",
        revision=1,
        granted_at=NOW,
        granted_by=AuditActor(principal_id="human:admin", principal_kind="human"),
    )


def _actor(kind: str = "human", *, authorized: bool = True) -> ActorContext:
    principal_id = f"{kind}:maker"
    roles = (_tooling_assignment(principal_id),) if authorized else ()
    principal = Principal(
        id=principal_id,
        kind=kind,  # type: ignore[arg-type]
        display_name=kind,
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=roles,
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


def _shared_budget(*, budget_id: str, scope_kind: str, scope_id: str) -> BudgetV1:
    return BudgetV1(
        budget_id=budget_id,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        policy_version="app-admission-shared-budget@1",
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


class _FixedApprovals:
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
    def _unused_read_scope(self) -> Iterator[Any]:
        raise AssertionError("validate admission must not touch the workflow read scope")
        yield  # pragma: no cover

    def seed_artifact(self, *, kind: str, tag: str) -> Any:
        payload = (
            canonical_json(
                {"snapshot_id": f"snapshot:{tag}", "entities": [], "relations": []}
            ).encode("utf-8")
            if kind == "ir_snapshot"
            else f"{kind}:{tag}".encode("utf-8")
        )
        stored = self.objects.put_verified(payload)
        artifact = build_artifact_v2(
            kind=kind,  # type: ignore[arg-type]
            version_tuple=VersionTuple(
                ir_snapshot_id=f"snapshot:{tag}" if kind == "ir_snapshot" else stored.ref.sha256,
                tool_version=f"{tag}@1",
            ),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                **({"payload_schema_id": "ir-core@1"} if kind == "ir_snapshot" else {}),
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

    def run_record(self, run_id: str) -> Any:
        with Session(self.engine) as session:
            return SqlRunRepository(session).get(run_id)

    def seed_ref(self, name: str, artifact_id: str) -> RefValue:
        with Session(self.engine) as session, session.begin():
            return SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=self.clock),
                clock=self.clock,
            ).compare_and_set(name, None, artifact_id)

    def run_count(self) -> int:
        with Session(self.engine) as session:
            return int(session.scalar(select(func.count()).select_from(RunRow)) or 0)


def _validate_headers(key: str, *, resource_id: str, revision: int) -> dict[str, str]:
    return {
        "Idempotency-Key": key,
        "If-Match": compute_resource_etag(
            resource_kind="patch",
            resource_id=resource_id,
            revision=revision,
        ),
    }


def test_proposal_resource_routes_use_only_the_frozen_paths(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    headers = {"Idempotency-Key": "proposal-route-contract:1"}
    with TestClient(harness.app) as client:
        for path in (
            "/api/v1/generation:propose",
            "/api/v1/constraint-proposals:propose",
        ):
            assert client.post(path, json={}, headers=headers).status_code == 422
        for drifted_alias in (
            "/api/v1/generations:propose",
            "/api/v1/constraints:propose",
        ):
            assert client.post(drifted_alias, json={}, headers=headers).status_code == 404


def test_generation_candidate_export_without_constraint_is_transport_422(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    body = {
        "request_schema_version": "generation-propose-request@1",
        "base_snapshot_artifact_id": "artifact:base",
        "constraint_snapshot_artifact_id": None,
        "findings": [],
        "objective_goal_text": "Generate a bounded candidate.",
        "domain_scope": {"domain_ids": ["economy"]},
        "target": {"ref_name": "content/head", "expected_ref": None},
        "generation_policy": {"profile_id": "generation", "version": 1},
        "candidate_export_profiles": [{"profile_id": "config-export", "version": 1}],
        "llm_execution_mode": "record",
        "execution_version_plan": None,
        "cassette_artifact_id": None,
    }

    with TestClient(harness.app) as client:
        response = client.post(
            "/api/v1/generation:propose",
            json=body,
            headers={"Idempotency-Key": "generation:missing-constraint"},
        )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "request_schema_invalid"
    assert harness.run_count() == 0


def _patch_validation_item(
    harness: AppHarness,
    *,
    subject: Any,
    base: Any,
    preview: Any,
    approval_id: str,
) -> ApprovalItem:
    initial = validation_testkit.approval_item(
        subject=subject,
        target=preview,
        kind="patch",
        approval_id=approval_id,
    )
    binding = PatchTargetBindingV1(
        target_artifact_id=preview.artifact_id,
        target_snapshot_id=preview.version_tuple.ir_snapshot_id,
        target_digest=preview.payload_hash,
        ref_name="content/head",
        expected_ref=RefValue(artifact_id=base.artifact_id, revision=1),
    )
    item = ApprovalItem.model_validate(
        {
            **initial.model_dump(mode="json"),
            "target_binding": binding.model_dump(mode="json"),
        }
    )
    registry_ref = DomainRegistryRefV1(
        registry_version=harness.domain_registry.registry_version,
        registry_digest=harness.domain_registry.registry_digest,
    )
    scope = DomainScope(domain_ids=("builtin",))
    requirements = tuple(
        requirement.model_copy(
            update={
                "domain_scope": scope,
                "required_permission": requirement.required_permission.model_copy(
                    update={"domain_scope": scope}
                ),
            }
        )
        for requirement in item.requirements
    )
    return item.model_copy(
        update={
            "status": "draft",
            "active_validation_run_id": None,
            "domain_scope": scope,
            "domain_registry_ref": registry_ref,
            "route_policy": item.route_policy.model_copy(
                update={"domain_registry_ref": registry_ref}
            ),
            "role_policy_version": harness.role_policy.policy_version,
            "role_policy_digest": harness.role_policy.policy_digest,
            "requirements": requirements,
        }
    )


def _seed_patch_validation_artifacts(
    harness: AppHarness,
    *,
    tag: str,
) -> tuple[Any, Any, Any]:
    base = harness.seed_artifact(kind="ir_snapshot", tag=f"base-{tag}")
    target_snapshot_id = f"snapshot:preview-{tag}"
    patch = PatchV2(
        revision=1,
        base_snapshot_id=base.version_tuple.ir_snapshot_id,
        target_snapshot_id=target_snapshot_id,
        expected_to_fix=[],
        preconditions=[],
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        producer_run_id=None,
        rationale="app admission fixture",
    )
    subject = harness.seed_payload_artifact(
        kind="patch",
        payload=patch.model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=base.version_tuple.ir_snapshot_id,
            tool_version="patch@2",
        ),
        lineage=(base.artifact_id,),
        payload_schema_id="patch@2",
    )
    preview = harness.seed_payload_artifact(
        kind="ir_snapshot",
        payload={"snapshot_id": target_snapshot_id, "entities": [], "relations": []},
        version_tuple=VersionTuple(ir_snapshot_id=target_snapshot_id, tool_version="patch@2"),
        lineage=(base.artifact_id, subject.artifact_id),
        payload_schema_id="ir-core@1",
    )
    return subject, base, preview


# ── Task-7 seam closed: HTTP :validate → real 202 RunAccepted ────────────────
def test_patch_validate_endpoint_admits_real_run(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    subject, base, preview = _seed_patch_validation_artifacts(harness, tag="subject")
    current_ref = harness.seed_ref("content/head", base.artifact_id)
    item = _patch_validation_item(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        approval_id="approval:patch:1",
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
            "expected_ref": current_ref.model_dump(mode="json"),
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
            headers=_validate_headers(
                "patch-validate:1",
                resource_id=subject.artifact_id,
                revision=item.workflow_revision,
            ),
        )
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["accepted_schema_version"] == "run-accepted@1"
    run = harness.run_record(accepted["run_id"])
    assert run is not None
    assert run.status == "queued"
    assert run.kind.kind == "patch.validate"
    assert run.run_budget_hold_group_id  # real budget hold reserved in the same UoW


def test_patch_validate_stochastic_profile_requires_and_freezes_root_seed(
    tmp_path: Path,
) -> None:
    harness = AppHarness(tmp_path)
    subject, base, preview = _seed_patch_validation_artifacts(harness, tag="seeded")
    current_ref = harness.seed_ref("content/head", base.artifact_id)
    item = _patch_validation_item(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        approval_id="approval:patch:seeded",
    )
    harness.approvals = _FixedApprovals(item)
    body = _patch_validate_body(
        item=item,
        base=base,
        preview=preview,
        target_ref_id=current_ref.artifact_id,
    )
    body["simulation_profiles"] = [SIMULATION_PROFILE.model_dump(mode="json")]

    with pytest.raises(Conflict, match="profile-dependent seed"):
        harness.admission.admit(
            operation="patch.validate",
            resource_id=subject.artifact_id,
            request=PatchValidationAdmissionRequestV1.model_validate(body),
            actor=_actor(),
            server=AdmissionRequestContext(
                idempotency_key="patch-validate:missing-seed",
                request_hash="c" * 64,
                trace_id=None,
            ),
        )
    assert harness.run_count() == 0

    body["seed"] = 42

    with TestClient(harness.app) as client:
        response = client.post(
            f"/api/v1/patches/{subject.artifact_id}:validate",
            json=body,
            headers=_validate_headers(
                "patch-validate:seeded",
                resource_id=subject.artifact_id,
                revision=item.workflow_revision,
            ),
        )

    assert response.status_code == 202, response.text
    run = harness.run_record(response.json()["run_id"])
    assert run is not None
    assert run.payload.seed == 42
    assert run.payload.version_tuple.seed == 42


def test_patch_validate_deterministic_profiles_reject_fabricated_root_seed(
    tmp_path: Path,
) -> None:
    harness = AppHarness(tmp_path)
    subject, base, preview = _seed_patch_validation_artifacts(harness, tag="deterministic")
    current_ref = harness.seed_ref("content/head", base.artifact_id)
    item = _patch_validation_item(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        approval_id="approval:patch:deterministic",
    )
    harness.approvals = _FixedApprovals(item)
    body = _patch_validate_body(
        item=item,
        base=base,
        preview=preview,
        target_ref_id=current_ref.artifact_id,
    )
    body["seed"] = 0

    with pytest.raises(Conflict, match="profile-dependent seed"):
        harness.admission.admit(
            operation="patch.validate",
            resource_id=subject.artifact_id,
            request=PatchValidationAdmissionRequestV1.model_validate(body),
            actor=_actor(),
            server=AdmissionRequestContext(
                idempotency_key="patch-validate:deterministic-seed",
                request_hash="d" * 64,
                trace_id=None,
            ),
        )

    assert harness.run_count() == 0


def test_patch_validate_path_must_bind_subject_without_creating_run(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    subject, base, preview = _seed_patch_validation_artifacts(harness, tag="path-binding")
    item = _patch_validation_item(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        approval_id="approval:patch:path-binding",
    )
    harness.approvals = _FixedApprovals(item)
    body = _patch_validate_body(
        item=item,
        base=base,
        preview=preview,
        target_ref_id=base.artifact_id,
    )
    with TestClient(harness.app) as client:
        response = client.post(
            "/api/v1/patches/artifact:wrong-path:validate",
            json=body,
            headers=_validate_headers(
                "patch-validate:wrong-path",
                resource_id="artifact:wrong-path",
                revision=item.workflow_revision,
            ),
        )
    assert response.status_code == 409, response.text
    assert harness.run_count() == 0


@pytest.mark.parametrize("etag_drift", ["stale_revision", "wrong_resource"])
def test_patch_validate_requires_exact_subject_etag_without_creating_run(
    tmp_path: Path,
    etag_drift: str,
) -> None:
    harness = AppHarness(tmp_path)
    subject, base, preview = _seed_patch_validation_artifacts(harness, tag=etag_drift)
    item = _patch_validation_item(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        approval_id=f"approval:patch:{etag_drift}",
    )
    harness.approvals = _FixedApprovals(item)
    body = _patch_validate_body(
        item=item,
        base=base,
        preview=preview,
        target_ref_id=base.artifact_id,
    )
    etag_resource_id = (
        subject.artifact_id if etag_drift == "stale_revision" else "artifact:another-patch"
    )
    etag_revision = item.workflow_revision + (etag_drift == "stale_revision")

    with TestClient(harness.app) as client:
        response = client.post(
            f"/api/v1/patches/{subject.artifact_id}:validate",
            json=body,
            headers=_validate_headers(
                f"patch-validate:{etag_drift}",
                resource_id=etag_resource_id,
                revision=etag_revision,
            ),
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "revision_conflict"
    assert harness.run_count() == 0


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


def test_post_runs_returns_typed_conflict_for_illegal_run_kind_mode(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tag="illegal-mode")
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
        "llm_execution_mode": "live",
        "seed": None,
        "execution_version_plan": None,
        "cassette_artifact_id": None,
    }

    with TestClient(harness.app) as client:
        response = client.post(
            "/api/v1/runs",
            json=body,
            headers={"Idempotency-Key": "checker:illegal-mode"},
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "revision_conflict"
    assert harness.run_count() == 0


def test_post_runs_same_idempotency_key_changed_payload_is_idempotency_conflict(
    tmp_path: Path,
) -> None:
    harness = AppHarness(tmp_path)
    first_snapshot = harness.seed_artifact(kind="ir_snapshot", tag="idempotency-first")
    second_snapshot = harness.seed_artifact(kind="ir_snapshot", tag="idempotency-second")
    body = {
        "request_schema_version": "run-submission-request@1",
        "params": {
            "schema_version": "checker-run@1",
            "snapshot_artifact_id": first_snapshot.artifact_id,
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
    changed = {
        **body,
        "params": {
            **body["params"],
            "snapshot_artifact_id": second_snapshot.artifact_id,
        },
    }
    headers = {"Idempotency-Key": "checker:idempotency-conflict"}

    with TestClient(harness.app) as client:
        first = client.post("/api/v1/runs", json=body, headers=headers)
        conflict = client.post("/api/v1/runs", json=changed, headers=headers)

    assert first.status_code == 202, first.text
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "idempotency_conflict"
    assert harness.run_count() == 1


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
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "request_schema_invalid"
    assert harness.run_count() == 0


def _patch_validate_body(*, item: Any, base: Any, preview: Any, target_ref_id: str) -> dict:
    return {
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
            "expected_ref": RefValue(artifact_id=target_ref_id, revision=1).model_dump(mode="json"),
        },
        "validation_policy": VALIDATION_PROFILE.model_dump(mode="json"),
        "checker_profiles": [],
        "simulation_profiles": [],
        "findings": [],
        "review_artifact_ids": [],
        "playtest_trace_artifact_ids": [],
        "regression_suite_artifact_ids": [],
    }


# ── C1: a roleless actor is forbidden on every admission path (403) ──────────
def test_validate_endpoint_forbids_roleless_actor(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    subject, base, preview = _seed_patch_validation_artifacts(harness, tag="roleless")
    item = _patch_validation_item(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        approval_id="approval:patch:2",
    )
    harness.approvals = _FixedApprovals(item)
    harness._actor_holder["actor"] = _actor(authorized=False)
    body = _patch_validate_body(
        item=item, base=base, preview=preview, target_ref_id=base.artifact_id
    )
    with TestClient(harness.app) as client:
        response = client.post(
            f"/api/v1/patches/{subject.artifact_id}:validate",
            json=body,
            headers=_validate_headers(
                "patch-validate:roleless",
                resource_id=subject.artifact_id,
                revision=item.workflow_revision,
            ),
        )
    assert response.status_code == 403, response.text


def test_post_runs_forbids_roleless_actor(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tag="snap")
    harness._actor_holder["actor"] = _actor(authorized=False)
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
    }
    with TestClient(harness.app) as client:
        response = client.post(
            "/api/v1/runs", json=body, headers={"Idempotency-Key": "checker:roleless"}
        )
    assert response.status_code == 403, response.text


# ── I2: a wrong-kind validation target ref is rejected before admission ──────
def test_validate_endpoint_kind_checks_target_ref(tmp_path: Path) -> None:
    harness = AppHarness(tmp_path)
    subject, base, preview = _seed_patch_validation_artifacts(harness, tag="target-kind")
    item = _patch_validation_item(
        harness,
        subject=subject,
        base=base,
        preview=preview,
        approval_id="approval:patch:3",
    )
    harness.approvals = _FixedApprovals(item)
    # The validation target ref must resolve to an ir_snapshot; pointing it at the
    # patch subject is a client-controlled revision conflict.
    body = _patch_validate_body(
        item=item, base=base, preview=preview, target_ref_id=subject.artifact_id
    )
    with TestClient(harness.app) as client:
        response = client.post(
            f"/api/v1/patches/{subject.artifact_id}:validate",
            json=body,
            headers=_validate_headers(
                "patch-validate:targetkind",
                resource_id=subject.artifact_id,
                revision=item.workflow_revision,
            ),
        )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "revision_conflict"
