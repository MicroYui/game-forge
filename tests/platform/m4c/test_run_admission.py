"""Real-SQLite Run admission tests (M4c Task 8).

Exercises the admission engine over the real ``RunCommandService.create_run`` UoW,
the real fenced ``SqlRunRepository`` queue authority, the real ``SqlCostLedger``
budget hold, and the real builtin registry/execution-profile catalog. No network.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cost import CostAmountV1
from gameforge.contracts.errors import IntegrityViolation, QuotaExceeded
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainScope,
    Principal,
)
from gameforge.contracts.jobs import (
    ArtifactMigrationPayloadV1,
    CheckerRunPayloadV1,
    GraphSelectionV1,
    PlannedAgentNodeVersionV1,
    ExecutionVersionPlanV1,
    RefReadBindingV1,
    SimulationRunPayloadV1,
    execution_version_plan_digest,
)
from gameforge.contracts.lineage import VersionTuple, build_artifact_v2
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.cost_policy.run_accounting import SqlRunCostAccounting
from gameforge.platform.provenance import (
    AuthenticatedGoalSourceWriter,
    GoalProvenancePolicy,
    build_source_kind_registry,
)
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.runs.admission import (
    AdmissionReadPort,
    AdmissionRequestContext,
    AdmissionRunPublicationGateway,
    ConservativeAttemptUsageProvider,
    DefaultRunBudgetPlanProvider,
    RunAdmissionEngine,
    _SourceWriteCapabilities,
    build_admission_capability_binder,
)
from gameforge.platform.runs.commands import RunCommandCapabilities, RunCommandService
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

NOW_DT = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-15T12:00:00Z"
CURSOR_KEY = b"m4c-run-admission-cursor-key"
OBJECT_CURSOR_KEY = b"m4c-run-admission-object-cursor-key"
AUDIT_CHAIN_ID = "platform-authority"

CHECKER_PROFILE = ProfileRefV1(profile_id="builtin.checker", version=1)
SIMULATION_PROFILE = ProfileRefV1(profile_id="builtin.simulation", version=1)
WORKLOAD_PROFILE = ProfileRefV1(profile_id="builtin.workload", version=1)
GENERATION_PROFILE = ProfileRefV1(profile_id="builtin.generation", version=1)


def _principal(kind: str) -> Principal:
    return Principal(
        id=f"{kind}:actor",
        kind=kind,  # type: ignore[arg-type]
        display_name=kind,
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=(),
    )


def _actor(kind: str = "human") -> ActorContext:
    mechanism = {"human": "session", "service": "api_key", "system": "trusted_internal"}[kind]
    return ActorContext(
        principal=_principal(kind),
        authentication=AuthenticationContext(
            mechanism=mechanism,  # type: ignore[arg-type]
            credential_id=None if kind == "system" else f"credential:{kind}",
        ),
        session_id=f"session:{kind}" if kind == "human" else None,
        request_id=f"request:{kind}",
    )


def _server(key: str) -> AdmissionRequestContext:
    return AdmissionRequestContext(
        idempotency_key=key,
        request_hash=canonical_sha256({"key": key}),
        trace_id=None,
    )


class _NullApprovals:
    def get(self, approval_id: str) -> Any:
        return None


class Harness:
    def __init__(self, tmp_path: Path, *, budget_limits: tuple[CostAmountV1, ...] | None = None):
        self.clock = FrozenUtcClock(NOW_DT)
        self.engine = get_engine(f"sqlite:///{tmp_path / 'admission.db'}")
        Base.metadata.create_all(self.engine)
        self.objects = LocalObjectStore(
            tmp_path / "objects",
            store_id="local",
            clock=self.clock,
            cursor_signing_key=OBJECT_CURSOR_KEY,
        )
        self.registry = build_builtin_registry()
        catalogs = self.registry.list_execution_profile_catalogs()
        assert len(catalogs) == 1
        self.catalog = catalogs[0]
        from sqlalchemy.orm import Session

        with Session(self.engine) as session, session.begin():
            SqlPolicySnapshotRepository(session, clock=self.clock).put_execution_profile_catalog(
                self.catalog
            )
        self.uow = SqliteUnitOfWork(self.engine, self._capability_factory)
        if budget_limits is None:
            binder = build_admission_capability_binder(
                registry=self.registry, clock=self.clock, audit_chain_id=AUDIT_CHAIN_ID
            )
        else:
            binder = self._failing_binder(budget_limits)
        run_commands = RunCommandService(
            unit_of_work=self.uow, bind_capabilities=binder, clock=self.clock
        )
        goal_writer = AuthenticatedGoalSourceWriter(
            policy=GoalProvenancePolicy(registry=build_source_kind_registry())
        )
        self.engine_admission = RunAdmissionEngine(
            run_commands=run_commands,
            unit_of_work=self.uow,
            read_scope=self._read_scope,
            registry=self.registry,
            execution_profile_catalog=self.catalog,
            goal_writer=goal_writer,
            object_store=self.objects,
            clock=self.clock,
            source_uow_capabilities=lambda tx: _SourceWriteCapabilities(
                artifacts=tx.artifacts, object_bindings=tx.object_bindings
            ),
        )

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

    def _failing_binder(self, limits: tuple[CostAmountV1, ...]):
        def bind(transaction: Any) -> RunCommandCapabilities:
            provider = DefaultRunBudgetPlanProvider(
                ledger=transaction.cost,
                clock=self.clock,
                limits=limits,
                reservation=(CostAmountV1(dimension="request", value=1, unit="request"),),
            )
            accounting = SqlRunCostAccounting(
                ledger=transaction.cost,
                plan_provider=provider,
                settlement_provider=ConservativeAttemptUsageProvider(),
                clock=self.clock,
            )
            publication = AdmissionRunPublicationGateway(
                audit=AuditGate(sink=transaction.audit, clock=self.clock),
                chain_id=AUDIT_CHAIN_ID,
            )
            return RunCommandCapabilities(
                runs=transaction.runs,
                registry=self.registry,
                admission=accounting,
                publication=publication,
                accounting=None,
            )

        return bind

    @contextmanager
    def _read_scope(self) -> Iterator[AdmissionReadPort]:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            cursor_signer = CursorSigner(signing_key=CURSOR_KEY, clock=self.clock)
            bindings = SqlObjectBindingRepository(session, self.objects, "local")
            yield AdmissionReadPort(
                policies=SqlPolicySnapshotRepository(session, clock=self.clock),
                approvals=_NullApprovals(),
                artifacts=SqlArtifactRepository(
                    session,
                    binding_repository=bindings,
                    cursor_signer=cursor_signer,
                    clock=self.clock,
                ),
                refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=self.clock),
            )

    def seed_artifact(self, *, kind: str, tool_version: str, extra: str = "") -> str:
        from sqlalchemy.orm import Session

        payload = f"{kind}:{tool_version}:{extra}".encode("utf-8")
        stored = self.objects.put_verified(payload)
        artifact = build_artifact_v2(
            kind=kind,  # type: ignore[arg-type]
            version_tuple=VersionTuple(ir_snapshot_id=stored.ref.sha256, tool_version=tool_version),
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
        return artifact.artifact_id

    def run_record(self, run_id: str) -> Any:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            return SqlRunRepository(session).get(run_id)

    def reservation_group(self, run_id: str) -> Any:
        from sqlalchemy.orm import Session

        with Session(self.engine) as session:
            return SqlCostLedger(session, clock=self.clock).get_reservation_group(f"hold:{run_id}")


def _plan() -> ExecutionVersionPlanV1:
    plan = {
        "agent_graph_version": "graph@1",
        "nodes": (
            PlannedAgentNodeVersionV1(
                agent_node_id="node-a",
                prompt_version="prompt@1",
                tool_version="tool@1",
                allowed_model_snapshots=("model@1",),
            ),
        ),
        "model_catalog_version": 1,
        "model_catalog_digest": "a" * 64,
        "routing_policy_version": 1,
        "routing_policy_digest": "b" * 64,
    }
    return ExecutionVersionPlanV1(**plan, plan_digest=execution_version_plan_digest(plan))


# ── generic POST /runs happy path (one UoW: record + event + hold + audit) ───
def test_generic_checker_run_creates_queued_run_with_budget_hold(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=CHECKER_PROFILE,
        checker_ids=(),
        defect_classes=(),
    )
    accepted = harness.engine_admission.admit_generic_run(
        params=params, actor=_actor(), server=_server("checker:1")
    )
    assert accepted.accepted_schema_version == "run-accepted@1"
    run = harness.run_record(accepted.run_id)
    assert run is not None
    assert run.status == "queued"
    assert run.kind.kind == "checker.run"
    assert run.payload.llm_execution_mode == "not_applicable"
    assert run.payload.seed is None
    # budget hold retained by the same UoW
    hold = harness.reservation_group(accepted.run_id)
    assert hold is not None
    assert hold.status == "reserved"
    assert run.run_budget_hold_group_id == hold.reservation_group_id
    # initial run.queued event retained (event seq 1)
    from sqlalchemy.orm import Session

    with Session(harness.engine) as session:
        event = SqlRunRepository(session).get_event(accepted.run_id, 1)
    assert event is not None and event.event_type == "run.queued"


def test_generic_simulation_run_requires_and_carries_seed(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = SimulationRunPayloadV1(
        snapshot_artifact_id=snapshot,
        simulation_profile=SIMULATION_PROFILE,
        workload_profile=WORKLOAD_PROFILE,
        replication_count=4,
        horizon_steps=1000,
    )
    accepted = harness.engine_admission.admit_generic_run(
        params=params, actor=_actor(), server=_server("sim:1"), seed=12345
    )
    run = harness.run_record(accepted.run_id)
    assert run is not None and run.payload.seed == 12345
    # two resolved profile bindings (simulation + workload), one per field-path
    assert {b.field_path for b in run.payload.resolved_profiles} == {
        "/params/simulation_profile",
        "/params/workload_profile",
    }


# ── generation:propose mints source_raw BEFORE Run creation ──────────────────
def test_generation_mints_source_raw_and_hides_naked_text(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    goal = "Reduce the boss gold reward so net gold inflow is non-positive."
    accepted = harness.engine_admission.admit_generation(
        base_snapshot_artifact_id=base,
        constraint_snapshot_artifact_id=None,
        findings=(),
        objective_goal_text=goal,
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        generation_policy=GENERATION_PROFILE,
        candidate_export_profiles=(),
        actor=_actor(),
        server=_server("generation:1"),
        llm_execution_mode="record",
        execution_version_plan=_plan(),
    )
    run = harness.run_record(accepted.run_id)
    assert run is not None and run.status == "queued"
    # the payload references only the source_raw artifact id/hash, never the text
    goal_binding = run.payload.params.objective_goal
    source_id = goal_binding.source_artifact_id
    assert source_id in run.payload.input_artifact_ids
    payload_json = run.payload.model_dump_json()
    assert "Reduce the boss gold reward" not in payload_json
    # source_raw artifact was persisted with kind source_raw and matching hash
    from sqlalchemy.orm import Session

    with Session(harness.engine) as session:
        artifact = SqlArtifactRepository(
            session,
            binding_repository=SqlObjectBindingRepository(session, harness.objects, "local"),
            cursor_signer=CursorSigner(signing_key=CURSOR_KEY, clock=harness.clock),
            clock=harness.clock,
        ).get(source_id)
    assert artifact is not None and artifact.kind == "source_raw"
    assert artifact.payload_hash == goal_binding.expected_payload_hash


# ── one-UoW atomicity: mid-admission failure leaves no Run and no hold ────────
def test_budget_exceeded_leaves_no_run_and_no_hold(tmp_path: Path) -> None:
    tiny = (
        CostAmountV1(dimension="request", value=0, unit="request"),
        CostAmountV1(dimension="concurrent_run", value=1, unit="count"),
    )
    harness = Harness(tmp_path, budget_limits=tiny)
    snapshot = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=CHECKER_PROFILE,
        checker_ids=(),
        defect_classes=(),
    )
    with pytest.raises(QuotaExceeded):
        harness.engine_admission.admit_generic_run(
            params=params, actor=_actor(), server=_server("checker:fail")
        )
    run_id = harness.engine_admission._derive_run_id(
        scope="principal:human:actor",
        key="checker:fail",
        request_hash=canonical_sha256({"key": "checker:fail"}),
    )
    assert harness.run_record(run_id) is None
    assert harness.reservation_group(run_id) is None


# ── POST /runs accepts only generic kinds; internal-only rejected everywhere ──
def test_generic_endpoint_rejects_internal_only_kind(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    params = ArtifactMigrationPayloadV1(
        source_artifact_id="artifact:x",
        target_payload_schema_id="schema@1",
        target_meta_schema_version="meta@1",
        migrator=ProfileRefV1(profile_id="builtin.artifact_migrator", version=1),
        publish_mode="report_only",
    )
    with pytest.raises(IntegrityViolation):
        harness.engine_admission.admit_generic_run(
            params=params, actor=_actor(), server=_server("migrate:generic")
        )


def test_internal_run_requires_trusted_system_actor(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    params = ArtifactMigrationPayloadV1(
        source_artifact_id="artifact:x",
        target_payload_schema_id="schema@1",
        target_meta_schema_version="meta@1",
        migrator=ProfileRefV1(profile_id="builtin.artifact_migrator", version=1),
        publish_mode="report_only",
    )
    with pytest.raises(IntegrityViolation):
        harness.engine_admission.admit_internal_run(
            params=params, actor=_actor("human"), server=_server("migrate:human")
        )


def test_generic_endpoint_rejects_resource_only_kind(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base = harness.seed_artifact(kind="ir_snapshot", tool_version="snap@1")
    # generation.propose is resource_endpoint_only; a params payload cannot be
    # submitted through the generic POST /runs surface.
    from gameforge.contracts.identity import DomainScope
    from gameforge.contracts.jobs import GenerationProposePayloadV1, PromptGoalBindingV1

    params = GenerationProposePayloadV1(
        base_snapshot_artifact_id=base,
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal", expected_payload_hash="c" * 64
        ),
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        generation_policy=GENERATION_PROFILE,
        candidate_export_profiles=(),
    )
    with pytest.raises(IntegrityViolation):
        harness.engine_admission.admit_generic_run(
            params=params, actor=_actor(), server=_server("gen:generic")
        )
