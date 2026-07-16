"""DB-backed harness for the durable Run-command transport (M4c Task 15b).

Composes the REAL M4a ``RunCommandService.submit`` path over a real SQLite RunStore
plus the 15a admission engine and resumable SSE stream, and wires the 15b transport
ports (``run_command_service`` + ``run_command_authorizer``) and WebSocket-scope auth
stubs. Command submission runs end-to-end through the authoritative UoW — a queued
checker Run cancel persists ``run.cancel_requested`` + ``run.cancelled`` + audit and
closes its budget hold BEFORE the ACK — so tests observe durable persist-before-ACK,
idempotency/OCC conflicts, terminal rejection, and browser lease-token safety without
stubbing the platform.

No live network; the LLM gateway is never touched (checker runs are ``not_applicable``).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from gameforge.apps.api.app import create_app
from gameforge.apps.api.commands import (
    RunCommandAuthorizationScope,
    RunCommandAuthorizationService,
)
from gameforge.apps.api.dependencies import (
    ApiDependencies,
    RunCommandWebSocketConfig,
    RunEventStreamConfig,
    require_actor,
)
from gameforge.apps.api.streaming import RunEventNotifier, RunEventReadScope, RunEventStreamService
from gameforge.apps.api.workflow_command_port import WorkflowCommandAdapter
from gameforge.contracts.auth import ApiKeyAuthRequestV1, PasswordAuthRequestV1, SessionToken
from gameforge.contracts.cost import BudgetV1, CostAmountV1
from gameforge.contracts.errors import AuthFailed, IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    Permission,
    Principal,
    RoleAssignmentV1,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.jobs import CancelRunPayloadV1, RunCommandV1
from gameforge.contracts.lineage import AuditActor, VersionTuple, build_artifact_v2
from gameforge.platform.audit.gate import AuditGate
from gameforge.contracts.lineage import AuditCorrelation, AuditSubject
from gameforge.platform.provenance import (
    AuthenticatedGoalSourceWriter,
    GoalProvenancePolicy,
    build_source_kind_registry,
)
from gameforge.platform.registry import build_builtin_registry
from gameforge.platform.runs.admission import (
    AdmissionReadPort,
    ConservativeAttemptUsageProvider,
    DefaultRunBudgetPlanProvider,
    RunAdmissionEngine,
    _SourceWriteCapabilities,
    build_admission_capability_binder,
)
from gameforge.platform.cost_policy.run_accounting import SqlRunCostAccounting
from gameforge.platform.runs.commands import RunCommandCapabilities, RunCommandService
from gameforge.platform.runs.lifecycle import RunFailurePublication
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

NOW_DT = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-15T12:00:00Z"
CURSOR_KEY = b"m4c-runcmd-cursor-key"
OBJECT_CURSOR_KEY = b"m4c-runcmd-object-cursor-key"
AUDIT_CHAIN_ID = "platform-authority"
CHECKER_PROFILE = ProfileRefV1(profile_id="builtin.checker", version=1)
ORIGIN = "https://gameforge.test"

ROLE_POLICY_VERSION = "runcmd-roles@1"
DOMAIN_REGISTRY_VERSION = "runcmd-domains@1"
DOMAIN_IDS = ("builtin", "economy", "narrative")
_TOOLING_GRANTS: tuple[tuple[str, str], ...] = (
    ("run", "checker"),
    ("run", "simulation"),
    ("run", "playtest"),
    ("read", "run"),
)

SESSION_COOKIE = "gameforge_session"
SESSION_TOKEN = "runcmd-session-token"
API_KEY = "gfk_service.runcmd"


def _shared_budget(*, budget_id: str, scope_kind: str, scope_id: str) -> BudgetV1:
    return BudgetV1(
        budget_id=budget_id,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        policy_version="runcmd-shared-budget@1",
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


def build_cancel_command(
    *,
    command_id: str,
    idempotency_key: str,
    expected_run_revision: int,
    client_id: str = "browser:a",
    client_seq: int = 1,
    reason_code: str = "user_requested",
) -> RunCommandV1:
    """Build the full ``RunCommandV1(type="cancel", …)`` a WS client frame carries."""

    return RunCommandV1(
        command_id=command_id,
        client_id=client_id,
        client_seq=client_seq,
        idempotency_key=idempotency_key,
        expected_run_revision=expected_run_revision,
        type="cancel",
        payload_schema_id="run-cancel@1",
        payload=CancelRunPayloadV1(reason_code=reason_code),
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


def human_actor(*, authorized: bool = True, scope: object = "all") -> ActorContext:
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


def service_actor(*, authorized: bool = True) -> ActorContext:
    principal_id = "service:automation"
    roles = (_tooling_assignment(principal_id),) if authorized else ()
    principal = Principal(
        id=principal_id,
        kind="service",
        display_name="automation",
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=roles,
    )
    return ActorContext(
        principal=principal,
        authentication=AuthenticationContext(mechanism="api_key", credential_id="credential:key"),
        request_id="request:service",
    )


class _SessionAuthStub:
    """Re-resolvable session port: returns the harness's CURRENT actor for a live token."""

    def __init__(self, holder: dict[str, ActorContext | None], *, valid: set[str]) -> None:
        self._holder = holder
        self._valid = valid

    def login(self, request: PasswordAuthRequestV1, *, request_id: str) -> Any:
        raise NotImplementedError

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: Any,
        request_method: str,
        request_id: str,
    ) -> ActorContext:
        del csrf_token, request_method, request_id
        if token.get_secret_value() not in self._valid:
            raise AuthFailed("session is not live")
        actor = self._holder["session"]
        if actor is None:
            raise AuthFailed("session principal is unavailable")
        return actor


class _ApiKeyAuthStub:
    def __init__(self, holder: dict[str, ActorContext | None], *, valid: set[str]) -> None:
        self._holder = holder
        self._valid = valid

    def authenticate(self, request: ApiKeyAuthRequestV1, *, request_id: str) -> ActorContext:
        del request_id
        if request.api_key.get_secret_value() not in self._valid:
            raise AuthFailed("api key is not live")
        actor = self._holder["api_key"]
        if actor is None:
            raise AuthFailed("api key principal is unavailable")
        return actor


class _CommandPublicationGateway:
    """Command-scope publication: real audit hooks + a not_applicable failure publication.

    Every ``record_*`` hook appends to the platform audit chain (same UoW as the Run
    mutation), so a cancel truly persists command + Run + Event + audit before the ACK.
    ``publish_run_failure`` returns the run cancellation failure artifact id for a
    non-LLM (``not_applicable``) Run, whose terminal cassette is None.
    """

    def __init__(self, *, audit: AuditGate, chain_id: str, failure_artifact_id: str) -> None:
        self._audit = audit
        self._chain_id = chain_id
        self._failure_artifact_id = failure_artifact_id

    def _record(self, *, action: str, run: Any, actor: AuditActor) -> None:
        self._audit.append(
            chain_id=self._chain_id,
            actor=actor,
            initiated_by=None,
            action=action,
            subject=AuditSubject(resource_kind="run", resource_id=run.run_id),
            correlation=AuditCorrelation(request_id=None, run_id=run.run_id, trace_id=None),
        )

    def record_run_created(
        self,
        *,
        run: Any,
        event: Any,
        request_id: str | None = None,
    ) -> None:
        del request_id
        self._record(action="run.queued", run=run, actor=run.initiated_by)

    def record_run_claimed(self, *, previous, run, attempt, lease, event, actor) -> None:  # type: ignore[no-untyped-def]
        self._record(action="run.claimed", run=run, actor=actor)

    def record_command_submitted(self, *, run, record, events, actor) -> None:  # type: ignore[no-untyped-def]
        self._record(action="run.command_submitted", run=run, actor=actor)

    def record_command_completed(self, *, run, record, event, actor) -> None:  # type: ignore[no-untyped-def]
        self._record(action="run.command_completed", run=run, actor=actor)

    def record_run_terminal(self, *, run, attempt, event, actor) -> None:  # type: ignore[no-untyped-def]
        self._record(action="run.terminal", run=run, actor=actor)

    def publish_run_failure(
        self,
        *,
        run,
        attempt,
        prepared,
        retry_decision,
        policy,
        attempt_failure_artifact_id,
        occurred_at,
        actor,
    ) -> RunFailurePublication:  # type: ignore[no-untyped-def]
        return RunFailurePublication(
            failure_artifact_id=self._failure_artifact_id,
            terminal_cassette_artifact_id=None,
        )

    def get_prompt_replay(self, **_: Any) -> Any:
        raise IntegrityViolation("prompt replay is not a command-scope operation")

    def publish_prompt_rendered(self, **_: Any) -> Any:
        raise IntegrityViolation("prompt publication is not a command-scope operation")


class _Stub:
    """Placeholder for the non-validate workflow collaborators (never invoked)."""


class CommandAppHarness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        ws_config: RunCommandWebSocketConfig | None = None,
    ) -> None:
        self.clock = FrozenUtcClock(NOW_DT)
        self.engine = get_engine(f"sqlite:///{tmp_path / 'runcmd.db'}")
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
        self.approvals = None
        self._failure_artifact_id = self._seed_failure_artifact()
        admission_run_commands = RunCommandService(
            unit_of_work=self.uow,
            bind_capabilities=build_admission_capability_binder(
                registry=self.registry, clock=self.clock, audit_chain_id=AUDIT_CHAIN_ID
            ),
            clock=self.clock,
        )
        self.admission = RunAdmissionEngine(
            run_commands=admission_run_commands,
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
        )
        # The 15b durable command service: full command-scope binder (accounting + audit
        # publication) over the SAME RunStore, so cancels reach terminal end to end.
        self.command_service = RunCommandService(
            unit_of_work=self.uow,
            bind_capabilities=self._command_binder,
            clock=self.clock,
        )
        self.command_authorizer = RunCommandAuthorizationService(
            read_scope=self._command_auth_scope,
            registry=self.registry,
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
        # Actor holders let the WS auth stubs re-resolve the CURRENT principal per message.
        self._session_actor: dict[str, ActorContext | None] = {"session": human_actor()}
        self._api_key_actor: dict[str, ActorContext | None] = {"api_key": service_actor()}
        self._valid_sessions = {SESSION_TOKEN}
        self._valid_api_keys = {API_KEY}
        self._http_actor: dict[str, ActorContext] = {"actor": human_actor()}
        self.app = create_app(
            ApiDependencies(
                run_admission=self.admission,
                workflow_commands=WorkflowCommandAdapter(workflow_service),
                run_event_stream=self.stream,
                run_event_notifier=self.notifier,
                run_event_stream_config=RunEventStreamConfig(page_limit=256, heartbeat_seconds=0.2),
                run_command_service=self.command_service,
                run_command_authorizer=self.command_authorizer,
                run_command_ws_config=ws_config or RunCommandWebSocketConfig(max_frame_bytes=4096),
                session_authentication=_SessionAuthStub(
                    self._session_actor, valid=self._valid_sessions
                ),
                api_key_authentication=_ApiKeyAuthStub(
                    self._api_key_actor, valid=self._valid_api_keys
                ),
                allowed_websocket_origins=frozenset({ORIGIN}),
                request_id_factory=lambda: "request:test",
            )
        )
        self.app.dependency_overrides[require_actor] = lambda: self._http_actor["actor"]

    # ── capability factories ─────────────────────────────────────────────
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

    def _command_binder(self, transaction: TransactionCapabilities) -> RunCommandCapabilities:
        accounting = SqlRunCostAccounting(
            ledger=transaction.cost,
            plan_provider=DefaultRunBudgetPlanProvider(ledger=transaction.cost, clock=self.clock),
            settlement_provider=ConservativeAttemptUsageProvider(),
            clock=self.clock,
        )
        publication = _CommandPublicationGateway(
            audit=AuditGate(sink=transaction.audit, clock=self.clock),
            chain_id=AUDIT_CHAIN_ID,
            failure_artifact_id=self._failure_artifact_id,
        )
        return RunCommandCapabilities(
            runs=transaction.runs,
            registry=self.registry,
            admission=accounting,
            publication=publication,
            accounting=accounting,
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
    def _command_auth_scope(self) -> Iterator[RunCommandAuthorizationScope]:
        with Session(self.engine) as session:
            yield RunCommandAuthorizationScope(
                runs=SqlRunRepository(session),
                policies=SqlPolicySnapshotRepository(session, clock=self.clock),
                approvals=self.approvals,
            )

    @contextmanager
    def _event_read_scope(self) -> Iterator[RunEventReadScope]:
        with Session(self.engine) as session:
            yield RunEventReadScope(
                runs=SqlRunRepository(session),
                policies=SqlPolicySnapshotRepository(session, clock=self.clock),
                approvals=self.approvals,
            )

    @contextmanager
    def _unused_read_scope(self) -> Iterator[Any]:
        raise AssertionError("validate admission must not touch the workflow read scope")
        yield  # pragma: no cover

    # ── actor / auth mutation ────────────────────────────────────────────
    def set_http_actor(self, actor: ActorContext) -> None:
        self._http_actor["actor"] = actor

    def set_session_actor(self, actor: ActorContext | None) -> None:
        self._session_actor["session"] = actor

    def revoke_session(self) -> None:
        self._valid_sessions.discard(SESSION_TOKEN)

    def ws_cookies(self) -> dict[str, str]:
        return {SESSION_COOKIE: SESSION_TOKEN}

    # ── run + command seeding ────────────────────────────────────────────
    def _seed_failure_artifact(self) -> str:
        """Persist one real ``run_failure`` artifact the cancel terminal path can point at.

        ``runs.failure_artifact_id`` has a FK to the artifacts table, so the queued-cancel
        terminal publication must reference a persisted artifact (its kind is irrelevant to
        the transport; only referential existence matters).
        """

        stored = self.objects.put_verified(b"run-failure:cancelled")
        artifact = build_artifact_v2(
            kind="run_failure",
            version_tuple=VersionTuple(ir_snapshot_id=stored.ref.sha256, tool_version="cancel@1"),
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

    def admit_checker_run(self, tag: str = "snap") -> str:
        stored = self.objects.put_verified(f"ir_snapshot:{tag}".encode("utf-8"))
        artifact = build_artifact_v2(
            kind="ir_snapshot",
            version_tuple=VersionTuple(ir_snapshot_id=stored.ref.sha256, tool_version=f"{tag}@1"),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={
                "payload_schema_id": "ir-core@1",
                "domain_scope": {"domain_ids": ["builtin"]},
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
        with TestClient(self.app, base_url=ORIGIN) as client:
            response = client.post(
                "/api/v1/runs", json=body, headers={"Idempotency-Key": f"checker:{tag}"}
            )
        assert response.status_code == 202, response.text
        return response.json()["run_id"]

    def run_record(self, run_id: str) -> Any:
        with Session(self.engine) as session:
            return SqlRunRepository(session).get_run_projection(run_id)

    def audit_actions(self, run_id: str) -> list[str]:
        from sqlalchemy import select

        from gameforge.runtime.persistence.models import AuditRow

        with Session(self.engine) as session:
            rows = session.execute(select(AuditRow).order_by(AuditRow.seq)).scalars().all()
        actions: list[str] = []
        for row in rows:
            correlation = row.correlation or {}
            if isinstance(correlation, dict) and correlation.get("run_id") == run_id:
                actions.append(row.action)
        return actions

    def cancel_body(
        self,
        *,
        command_id: str,
        client_id: str = "browser:a",
        client_seq: int = 1,
        expected_run_revision: int,
        reason_code: str = "user_requested",
    ) -> dict[str, Any]:
        return {
            "request_schema_version": "run-cancel-request@1",
            "command_id": command_id,
            "client_id": client_id,
            "client_seq": client_seq,
            "expected_run_revision": expected_run_revision,
            "payload": {"schema_version": "run-cancel@1", "reason_code": reason_code},
        }

    def cancel_command(
        self,
        *,
        command_id: str,
        client_id: str = "browser:a",
        client_seq: int = 1,
        idempotency_key: str,
        expected_run_revision: int,
        reason_code: str = "user_requested",
    ) -> RunCommandV1:
        return build_cancel_command(
            command_id=command_id,
            client_id=client_id,
            client_seq=client_seq,
            idempotency_key=idempotency_key,
            expected_run_revision=expected_run_revision,
            reason_code=reason_code,
        )
