"""Real local composition for the API process.

This module owns environment reads and concrete adapters. ``app.create_app``
stays side-effect free for tests and later production composition roots.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import os
from pathlib import Path
import secrets

from fastapi import FastAPI
from sqlalchemy import Engine, inspect
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies
from gameforge.apps.api.health import (
    AuditVerificationCache,
    CostLedgerReadinessProbe,
    DatabaseReadinessProbe,
    LocalObjectStoreReadinessProbe,
    MigrationHeadReadinessProbe,
    ReadinessChecks,
    ReadinessService,
    RegistryReadinessProbe,
    SloRetentionReadinessProbe,
)
from gameforge.apps.api.local_reads import build_local_read_services
from gameforge.apps.api.streaming import (
    RunEventNotifier,
    RunEventReadScope,
    RunEventStreamService,
)
from gameforge.apps.api.workflow_command_port import WorkflowCommandAdapter
from gameforge.apps.worker.config_export import build_aureus_config_exporter
from gameforge.apps.cli.identity import (
    AUDIT_CHAIN_ID_ENV,
    PASSWORD_HASH_POLICY_VERSION_ENV,
    ROLE_POLICY_DIGEST_ENV,
    ROLE_POLICY_VERSION_ENV,
)
from gameforge.contracts.errors import Conflict, DependencyUnavailable, IntegrityViolation
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.identity import (
    DomainRegistryV1,
    DomainRoutePolicy,
    DomainRoutePolicyRefV1,
    DomainScope,
    RolePolicy,
)
from gameforge.contracts.lineage import ArtifactV2, AuditActor, AuditCorrelation, AuditSubject
from gameforge.contracts.observability import SpanDataV1
from gameforge.contracts.workflow import ApprovalPolicyRefV1, ApprovalPolicyV1, RollbackRequestV1
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.approvals.apply import (
    ApprovedApplyCapabilities,
    ApprovedApplyService,
    ExactRollbackExecutionVerifier,
)
from gameforge.platform.approvals.commands import (
    ApprovalCommandCapabilities,
    ApprovalCommandService,
)
from gameforge.platform.approvals.state import validate_status_transition
from gameforge.platform.diff.rebase import (
    RebaseWorkflowCapabilities,
    RebaseWorkflowService,
)
from gameforge.platform.read_models.workflows import CurrentApprovalProgressProjector
from gameforge.platform.identity.authentication import (
    ApiKeyAuthenticationCapabilities,
    ApiKeyAuthenticationService,
)
from gameforge.platform.identity.logout import LogoutCapabilities, LogoutCommandService
from gameforge.platform.identity.sessions import (
    SessionAuthenticationCapabilities,
    SessionAuthenticationService,
)
from gameforge.platform.registry import (
    PlatformReadinessValidator,
    TrustedComponentMaps,
    build_builtin_registry,
    build_readiness_component_maps,
)
from gameforge.platform.provenance import (
    AuthenticatedGoalSourceWriter,
    GoalProvenancePolicy,
    build_source_kind_registry,
)
from gameforge.platform.runs.admission import (
    AdmissionReadPort,
    RunAdmissionEngine,
    _SourceWriteCapabilities,
    build_admission_capability_binder,
)
from gameforge.platform.runs.commands import RunCommandService
from gameforge.platform.slo.service import (
    SLODefinitionCapabilities,
    SLODefinitionService,
)
from gameforge.platform.workflow.readers import (
    WorkflowDraftLineageVerifier,
    WorkflowTypedReaders,
)
from gameforge.platform.workflow.service import (
    WorkflowCommandService,
    WorkflowGovernance,
    WorkflowGovernanceProvider,
    WorkflowReadPort,
)
from gameforge.platform.workflow.spec import (
    SpecUploadCapabilities,
    SpecUploadService,
)
from gameforge.runtime.auth.local import (
    LocalApiKeyAuthenticator,
    LocalPasswordAuthenticator,
    LocalSessionRuntime,
)
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime
from gameforge.runtime.auth.tokens import ApiKeyRuntime, SessionTokenRuntime
from gameforge.runtime.cassette.legacy_import import LegacyImportAuthority
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.observability import AlwaysOnSampler, Tracer
from gameforge.runtime.observability.local_store import LocalTelemetryStore
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.approvals import SqlApprovalRepository
from gameforge.runtime.persistence.artifacts import SqlArtifactRepository
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.conflicts import SqlConflictSetRepository
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import DATABASE_URL_ENV, DEFAULT_URL, get_engine
from gameforge.runtime.persistence.findings import SqlFindingRepository
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.object_bindings import SqlObjectBindingRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.persistence.ref_transitions import SqlRefTransitionRepository
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from gameforge.runtime.persistence.slo import SqlSloRepository
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork
from gameforge.runtime.secrets.session_keys import (
    SessionSigningKeyConfigurationError,
    SessionSigningKeyProvider,
)


LOCAL_ROOT_SECRET_ENV = "GAMEFORGE_LOCAL_SECRET_BASE64"
SESSION_POLICY_VERSION_ENV = "GAMEFORGE_IDENTITY_SESSION_POLICY_VERSION"
OBJECT_STORE_ROOT_ENV = "GAMEFORGE_OBJECT_STORE_ROOT"
OBJECT_STORE_ID_ENV = "GAMEFORGE_OBJECT_STORE_ID"
TELEMETRY_DB_PATH_ENV = "GAMEFORGE_TELEMETRY_DB_PATH"
ALLOWED_WEBSOCKET_ORIGINS_ENV = "GAMEFORGE_ALLOWED_WEBSOCKET_ORIGINS"
WORKFLOW_ROUTE_POLICY_VERSION_ENV = "GAMEFORGE_WORKFLOW_ROUTE_POLICY_VERSION"
WORKFLOW_ROUTE_POLICY_DIGEST_ENV = "GAMEFORGE_WORKFLOW_ROUTE_POLICY_DIGEST"
WORKFLOW_APPROVAL_POLICY_VERSION_ENV = "GAMEFORGE_WORKFLOW_APPROVAL_POLICY_VERSION"
WORKFLOW_APPROVAL_POLICY_DIGEST_ENV = "GAMEFORGE_WORKFLOW_APPROVAL_POLICY_DIGEST"


class LocalApiConfigurationError(ValueError):
    """The trusted local API composition is incomplete or unsafe."""


def _required(source: Mapping[str, str], name: str) -> str:
    value = source.get(name)
    if not isinstance(value, str) or not value:
        raise LocalApiConfigurationError(f"{name} is required")
    return value


def _root_secret(source: Mapping[str, str]) -> bytes:
    encoded = _required(source, LOCAL_ROOT_SECRET_ENV)
    try:
        value = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise LocalApiConfigurationError(f"{LOCAL_ROOT_SECRET_ENV} must be valid base64") from None
    if len(value) < 32:
        raise LocalApiConfigurationError(
            f"{LOCAL_ROOT_SECRET_ENV} must decode to at least 32 bytes"
        )
    return value


def _lower_sha256(value: str, *, name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise LocalApiConfigurationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _allowed_websocket_origins(source: Mapping[str, str]) -> frozenset[str]:
    raw = source.get(ALLOWED_WEBSOCKET_ORIGINS_ENV, "")
    if not isinstance(raw, str):
        raise LocalApiConfigurationError(
            f"{ALLOWED_WEBSOCKET_ORIGINS_ENV} must be a comma-separated string"
        )
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class LocalApiConfig:
    database_url: str
    object_store_root: Path
    object_store_id: str
    telemetry_db_path: Path
    current_password_hash_policy_version: str
    session_policy_version: str
    role_policy_version: str
    role_policy_digest: str
    audit_chain_id: str
    root_secret: bytes = field(repr=False)
    session_signing_keys: SessionSigningKeyProvider = field(repr=False)
    allowed_websocket_origins: frozenset[str] = frozenset()
    # Workflow governance pointers. When all four are present the maker-checker
    # composition resolves the exact DomainRoutePolicy and ApprovalPolicy from the
    # authoritative policy snapshot repository (registry + roles come from the role
    # policy ref above), enabling every draft/rebase op. Absent (a deployment that
    # does not provision workflow governance) those ops fail closed with a typed
    # ``workflow_governance`` dependency error rather than fabricating authority.
    workflow_route_policy_version: str | None = None
    workflow_route_policy_digest: str | None = None
    workflow_approval_policy_version: str | None = None
    workflow_approval_policy_digest: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "database_url",
            "object_store_id",
            "current_password_hash_policy_version",
            "session_policy_version",
            "role_policy_version",
            "role_policy_digest",
            "audit_chain_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise LocalApiConfigurationError(f"{name} must be a non-empty bounded string")
        _lower_sha256(self.role_policy_digest, name="role_policy_digest")
        governance = (
            self.workflow_route_policy_version,
            self.workflow_route_policy_digest,
            self.workflow_approval_policy_version,
            self.workflow_approval_policy_digest,
        )
        present = [value for value in governance if value is not None]
        if present and len(present) != len(governance):
            raise LocalApiConfigurationError(
                "workflow governance pointers must be provided together or not at all"
            )
        if present:
            for name in (
                "workflow_route_policy_version",
                "workflow_approval_policy_version",
            ):
                value = getattr(self, name)
                if not isinstance(value, str) or not value or len(value) > 4096:
                    raise LocalApiConfigurationError(f"{name} must be a non-empty bounded string")
            _lower_sha256(self.workflow_route_policy_digest, name="workflow_route_policy_digest")
            _lower_sha256(
                self.workflow_approval_policy_digest, name="workflow_approval_policy_digest"
            )
        if not isinstance(self.root_secret, bytes) or len(self.root_secret) < 32:
            raise LocalApiConfigurationError("root_secret must contain at least 32 bytes")
        if not isinstance(self.session_signing_keys, SessionSigningKeyProvider):
            raise LocalApiConfigurationError(
                "session_signing_keys must be a SessionSigningKeyProvider"
            )
        object.__setattr__(self, "object_store_root", Path(self.object_store_root))
        object.__setattr__(self, "telemetry_db_path", Path(self.telemetry_db_path))

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> LocalApiConfig:
        source = os.environ if environment is None else environment
        try:
            signing_keys = SessionSigningKeyProvider.from_environment(source)
        except SessionSigningKeyConfigurationError as exc:
            raise LocalApiConfigurationError(
                "session signing key configuration is invalid"
            ) from exc
        return cls(
            database_url=source.get(DATABASE_URL_ENV, DEFAULT_URL),
            object_store_root=Path(source.get(OBJECT_STORE_ROOT_ENV, ".gameforge/objects")),
            object_store_id=source.get(OBJECT_STORE_ID_ENV, "local:default"),
            telemetry_db_path=Path(
                source.get(TELEMETRY_DB_PATH_ENV, ".gameforge/telemetry.sqlite3")
            ),
            current_password_hash_policy_version=_required(
                source,
                PASSWORD_HASH_POLICY_VERSION_ENV,
            ),
            session_policy_version=_required(source, SESSION_POLICY_VERSION_ENV),
            role_policy_version=_required(source, ROLE_POLICY_VERSION_ENV),
            role_policy_digest=_lower_sha256(
                _required(source, ROLE_POLICY_DIGEST_ENV),
                name=ROLE_POLICY_DIGEST_ENV,
            ),
            audit_chain_id=source.get(AUDIT_CHAIN_ID_ENV, "identity"),
            root_secret=_root_secret(source),
            session_signing_keys=signing_keys,
            allowed_websocket_origins=_allowed_websocket_origins(source),
            workflow_route_policy_version=source.get(WORKFLOW_ROUTE_POLICY_VERSION_ENV),
            workflow_route_policy_digest=source.get(WORKFLOW_ROUTE_POLICY_DIGEST_ENV),
            workflow_approval_policy_version=source.get(WORKFLOW_APPROVAL_POLICY_VERSION_ENV),
            workflow_approval_policy_digest=source.get(WORKFLOW_APPROVAL_POLICY_DIGEST_ENV),
        )


def _derive_key(root_secret: bytes, purpose: str) -> bytes:
    return hmac.new(
        root_secret,
        b"gameforge-local-api@1\x00" + purpose.encode("ascii"),
        sha256,
    ).digest()


class _LocalSpanExporter:
    def __init__(self, store: LocalTelemetryStore) -> None:
        self._store = store

    def export(self, spans: Sequence[SpanDataV1]) -> None:
        for span in spans:
            self._store.put(span)


class _SqlAuditVerifier:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def verify_chain(self, chain_id: str) -> bool:
        with Session(self._engine) as session:
            return SqlAuditSink(session).verify_chain(chain_id)


@dataclass(frozen=True, slots=True)
class LocalApiResources:
    dependencies: ApiDependencies
    engine: Engine
    object_store: LocalObjectStore
    telemetry_store: LocalTelemetryStore
    audit_cache: AuditVerificationCache
    audit_chain_ids: tuple[str, ...]

    def refresh_audit_cache(self) -> None:
        self.audit_cache.refresh(
            chain_ids=self.audit_chain_ids,
            verifier=_SqlAuditVerifier(self.engine),
        )

    def close(self) -> None:
        self.telemetry_store.close()
        self.engine.dispose()


class _PrincipalGet:
    """Adapt the transaction identity capability to the principal-get Protocol."""

    def __init__(self, identities: object) -> None:
        self._identities = identities

    def get(self, principal_id: str) -> object | None:
        return self._identities.project(principal_id)  # type: ignore[attr-defined]


class _SqlWorkflowGovernanceProvider:
    """Resolve the exact current :class:`WorkflowGovernance` from the policy authority.

    The registry and roles are resolved from the configured role-policy ref (and the
    registry ref it declares); the DomainRoutePolicy and ApprovalPolicy are resolved
    from the configured governance pointers. Resolution runs in a fresh short read
    transaction per request, so it never touches an unmigrated database at build time
    and always reflects the retained immutable policy snapshots — never a fixture.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        clock: SystemUtcClock,
        role_policy_version: str,
        role_policy_digest: str,
        route_policy_version: str,
        route_policy_digest: str,
        approval_policy_version: str,
        approval_policy_digest: str,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._role_policy_version = role_policy_version
        self._role_policy_digest = role_policy_digest
        self._route_policy_version = route_policy_version
        self._route_policy_digest = route_policy_digest
        self._approval_policy_version = approval_policy_version
        self._approval_policy_digest = approval_policy_digest

    def current(self) -> WorkflowGovernance:
        with Session(self._engine) as session:
            policies = SqlPolicySnapshotRepository(session, clock=self._clock)
            roles = policies.get_role_policy(
                self._role_policy_version,
                self._role_policy_digest,
            )
            if not isinstance(roles, RolePolicy):
                raise DependencyUnavailable(
                    "workflow role policy is unavailable",
                    component="workflow_governance",
                )
            registry = policies.get_domain_registry(roles.domain_registry_ref)
            if not isinstance(registry, DomainRegistryV1):
                raise DependencyUnavailable(
                    "workflow domain registry is unavailable",
                    component="workflow_governance",
                )
            route = policies.get_domain_route_policy(
                DomainRoutePolicyRefV1(
                    route_version=self._route_policy_version,
                    route_digest=self._route_policy_digest,
                    domain_registry_ref=roles.domain_registry_ref,
                )
            )
            if not isinstance(route, DomainRoutePolicy):
                raise DependencyUnavailable(
                    "workflow domain route policy is unavailable",
                    component="workflow_governance",
                )
            approval = policies.get_approval_policy(
                ApprovalPolicyRefV1(
                    policy_version=self._approval_policy_version,
                    policy_digest=self._approval_policy_digest,
                )
            )
            if not isinstance(approval, ApprovalPolicyV1):
                raise DependencyUnavailable(
                    "workflow approval policy is unavailable",
                    component="workflow_governance",
                )
            return WorkflowGovernance(
                registry=registry,
                route=route,
                roles=roles,
                approval=approval,
            )


class _RoutePolicyDomainScopeResolver:
    """Derive patch/rollback scope from the exact immutable content Artifact.

    The route policy decides *who* reviews a known affected domain; it is not a
    substitute for resource identity. Patch and rollback requests therefore inherit
    the canonical domain binding of their exact base/target Artifact and merely prove
    that the retained route policy covers that scope. Missing, inactive, forged, or
    unrouted bindings fail closed instead of expanding to every active domain.
    """

    def __init__(self, governance: WorkflowGovernanceProvider) -> None:
        self._governance = governance

    def resolve_patch_scope(self, *, base_artifact: object, patch: object) -> DomainScope:
        if not isinstance(base_artifact, ArtifactV2) or not isinstance(patch, PatchV2):
            raise IntegrityViolation("patch scope resolution requires exact typed inputs")
        if patch.base_snapshot_id != base_artifact.version_tuple.ir_snapshot_id:
            raise Conflict("Patch does not bind the exact base snapshot identity")
        return self._scope_for_artifact(base_artifact, "patch")

    def resolve_rollback_scope(self, *, target_artifact: object, request: object) -> DomainScope:
        if not isinstance(target_artifact, ArtifactV2) or not isinstance(
            request, RollbackRequestV1
        ):
            raise IntegrityViolation("rollback scope resolution requires exact typed inputs")
        if request.target_artifact_id != target_artifact.artifact_id:
            raise Conflict("Rollback request does not bind the exact target Artifact")
        return self._scope_for_artifact(target_artifact, "rollback_request")

    def _scope_for_artifact(
        self,
        artifact: ArtifactV2,
        subject_kind: str,
    ) -> DomainScope:
        governance = self._governance.current()
        raw_scope = artifact.meta.get("domain_scope")
        try:
            scope = DomainScope.model_validate(raw_scope)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "workflow subject Artifact has no valid domain binding",
                artifact_id=artifact.artifact_id,
            ) from exc
        if raw_scope != scope.model_dump(mode="json"):
            raise IntegrityViolation("workflow subject Artifact domain binding is noncanonical")
        active = {
            definition.domain_id
            for definition in governance.registry.definitions
            if definition.status == "active"
        }
        if not set(scope.domain_ids).issubset(active):
            raise Conflict("workflow subject selects an inactive or unknown domain")
        routed: set[str] = set()
        for rule in governance.route.rules:
            if subject_kind not in rule.subject_kinds:
                continue
            if rule.domain_selector == "all":
                routed |= active
            else:
                routed |= {
                    domain_id
                    for domain_id in rule.domain_selector.domain_ids
                    if domain_id in active
                }
        if not set(scope.domain_ids).issubset(routed):
            raise DependencyUnavailable(
                "workflow route policy does not cover the subject Artifact domain",
                component="workflow_domain_scope",
            )
        return scope


class _ValidationStartWriter:
    """Complete the ``:validate`` composition: CAS ``draft→validating`` IN the Run UoW.

    Design §"validation start" (m4 design L116/L315) makes ``POST /{subject}:validate``
    atomically (1) create the queued validation Run and (2) CAS the ApprovalItem
    ``draft→validating`` bound to that Run — ONE all-or-nothing UnitOfWork. The
    :class:`RunAdmissionEngine` owns the Run create and invokes this writer as its
    ``companion_write`` INSIDE the same transaction that queues the Run (via
    ``RunCommandService.create_run``), so a crash between the two writes can never
    orphan the Run (queued with the subject stranded in ``draft``). The writer receives
    the bound transaction and CASes ``item.workflow_revision → +1``, binding
    ``active_validation_run_id`` + an ``approval.validation_started`` audit. It is
    idempotent (a ``:validate`` replay whose item is already ``validating`` on this Run
    is a no-op) and fails closed on a non-current / non-draft / stale-revision subject.
    """

    def __init__(self, *, clock: SystemUtcClock, audit_chain_id: str) -> None:
        self._clock = clock
        self._audit_chain_id = audit_chain_id

    def start(
        self,
        transaction: object,
        *,
        item: object,
        run_id: str,
        actor: object,
        request_id: str | None,
        trace_id: str | None,
    ) -> None:
        approvals = transaction.approvals  # type: ignore[attr-defined]
        current = approvals.get(item.approval_id)  # type: ignore[attr-defined]
        if current is None:
            raise Conflict(
                "validation start subject is unavailable",
                approval_id=item.approval_id,  # type: ignore[attr-defined]
            )
        if current.status == "validating" and current.active_validation_run_id == run_id:
            # Idempotent ``:validate`` replay: the Run was already started on this item.
            return
        head = approvals.get_subject_head(current.subject_series_id)
        if head is None or head.current_approval_id != current.approval_id:
            raise Conflict(
                "validation start subject is not the current head",
                approval_id=current.approval_id,
            )
        expected = item.workflow_revision  # type: ignore[attr-defined]
        if current.status != "draft" or current.workflow_revision != expected:
            raise Conflict(
                "validation start requires the exact current draft revision",
                approval_id=current.approval_id,
                expected_revision=expected,
                actual_revision=current.workflow_revision,
                status=current.status,
            )
        validate_status_transition(
            current="draft", target="validating", subject_kind=current.subject_kind
        )
        replacement = current.model_copy(
            update={
                "status": "validating",
                "workflow_revision": expected + 1,
                "active_validation_run_id": run_id,
                "last_validation_failure_artifact_id": None,
            }
        )
        approvals.compare_and_set(current.approval_id, expected, replacement)
        AuditGate(sink=transaction.audit, clock=self._clock).append(  # type: ignore[attr-defined]
            chain_id=self._audit_chain_id,
            actor=AuditActor(
                principal_id=actor.principal.id,  # type: ignore[attr-defined]
                principal_kind=actor.principal.kind,  # type: ignore[attr-defined]
            ),
            initiated_by=None,
            action="approval.validation_started",
            subject=AuditSubject(resource_kind="approval", resource_id=current.approval_id),
            correlation=AuditCorrelation(
                request_id=request_id,
                run_id=run_id,
                trace_id=trace_id,
            ),
        )


def _build_run_admission_engine(
    *,
    config: LocalApiConfig,
    clock: SystemUtcClock,
    engine: Engine,
    object_store: LocalObjectStore,
    unit_of_work: SqliteUnitOfWork,
    registry: object,
    execution_profile_catalog: object,
    legacy_import_authority: LegacyImportAuthority | None,
) -> RunAdmissionEngine:
    """Compose the real Run admission engine over the write UoW + object store.

    Closes the Task-7 ``admission=None`` seam: the three ``*.validate`` operations and
    every §5.3 Run-creating endpoint now create real queued Runs (202) instead of
    failing closed. Provisioned deployments must have seeded the built-in
    execution-profile catalog (governance provisioning); an un-provisioned deployment
    fails closed at profile resolution rather than fabricating authority.
    """

    run_commands = RunCommandService(
        unit_of_work=unit_of_work,
        bind_capabilities=build_admission_capability_binder(
            registry=registry,  # type: ignore[arg-type]
            clock=clock,
            audit_chain_id=config.audit_chain_id,
        ),
        clock=clock,
    )
    goal_writer = AuthenticatedGoalSourceWriter(
        policy=GoalProvenancePolicy(registry=build_source_kind_registry())
    )
    cursor_key = _derive_key(config.root_secret, "workflow-cursor")

    @contextmanager
    def admission_read_scope():
        with Session(engine) as session:
            cursor_signer = CursorSigner(signing_key=cursor_key, clock=clock)
            object_bindings = SqlObjectBindingRepository(
                session, object_store, config.object_store_id
            )
            yield AdmissionReadPort(
                policies=SqlPolicySnapshotRepository(session, clock=clock),
                approvals=SqlApprovalRepository(session),
                artifacts=SqlArtifactRepository(
                    session,
                    binding_repository=object_bindings,
                    cursor_signer=cursor_signer,
                    clock=clock,
                ),
                refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=clock),
                object_bindings=object_bindings,
                findings=SqlFindingRepository(
                    session,
                    cursor_signer=cursor_signer,
                    clock=clock,
                ),
                finding_links=SqlRunRepository(session),
                runs=SqlRunRepository(session),
                routing=SqlCostLedger(session, clock=clock),
            )

    return RunAdmissionEngine(
        run_commands=run_commands,
        unit_of_work=unit_of_work,
        read_scope=admission_read_scope,
        registry=registry,  # type: ignore[arg-type]
        execution_profile_catalog=execution_profile_catalog,  # type: ignore[arg-type]
        goal_writer=goal_writer,
        object_store=object_store,
        clock=clock,
        source_uow_capabilities=lambda transaction: _SourceWriteCapabilities(
            artifacts=transaction.artifacts,  # type: ignore[attr-defined]
            object_bindings=transaction.object_bindings,  # type: ignore[attr-defined]
        ),
        current_principal_resolver=lambda transaction, actor: transaction.identity.project(  # type: ignore[attr-defined]
            actor.principal.id
        ),
        role_policy_version=config.role_policy_version,
        role_policy_digest=config.role_policy_digest,
        # The ``:validate`` admission CASes the ApprovalItem draft→validating in the SAME
        # UoW that queues the Run (design §"validation start"). Generic ``POST /runs``
        # kinds never carry a validation subject, so the writer only fires on the three
        # ``*.validate`` operations.
        validation_start_writer=_ValidationStartWriter(
            clock=clock, audit_chain_id=config.audit_chain_id
        ),
        legacy_import_authority=legacy_import_authority,
    )


def _build_workflow_command_service(
    *,
    config: LocalApiConfig,
    clock: SystemUtcClock,
    engine: Engine,
    object_store: LocalObjectStore,
    unit_of_work: SqliteUnitOfWork,
    execution_profile_catalog: object,
    admission: object,
    config_exporter: object,
) -> WorkflowCommandAdapter:
    """Compose the synchronous workflow-command port over the real write UoW.

    Workflow governance (registry/route/roles/approval snapshot stamped on new drafts)
    and patch/rollback domain-scope resolution are resolved from the authoritative
    policy snapshot repository when the config declares its governance pointers, so
    every draft/rebase op is functional against real SQLite. Run admission
    (``*.validate``) stays deferred (interface-present) until Task 8 wires it; those
    ops fail closed. Spec upload, submit/decide, apply/publish and rebase operate on
    the real SQLite authority.
    """

    draft_verifier = WorkflowDraftLineageVerifier()

    def readers(transaction: object) -> WorkflowTypedReaders:
        return WorkflowTypedReaders(
            artifacts=transaction.artifacts,  # type: ignore[attr-defined]
            bindings=transaction.object_bindings,  # type: ignore[attr-defined]
            objects=object_store,
        )

    def command_capabilities(transaction: object) -> ApprovalCommandCapabilities:
        bound = readers(transaction)
        return ApprovalCommandCapabilities(
            approvals=transaction.approvals,  # type: ignore[attr-defined]
            policies=transaction.policies,  # type: ignore[attr-defined]
            artifacts=transaction.artifacts,  # type: ignore[attr-defined]
            object_bindings=transaction.object_bindings,  # type: ignore[attr-defined]
            idempotency=transaction.idempotency,  # type: ignore[attr-defined]
            audit=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
            runs=None,
            subjects=bound,
            lineage=draft_verifier,
            evidence=bound,
            refs=transaction.refs,  # type: ignore[attr-defined]
            principals=_PrincipalGet(transaction.identity),  # type: ignore[attr-defined]
        )

    def apply_capabilities(transaction: object) -> ApprovedApplyCapabilities:
        bound = readers(transaction)
        return ApprovedApplyCapabilities(
            approvals=transaction.approvals,  # type: ignore[attr-defined]
            policies=transaction.policies,  # type: ignore[attr-defined]
            principals=_PrincipalGet(transaction.identity),  # type: ignore[attr-defined]
            artifacts=transaction.artifacts,  # type: ignore[attr-defined]
            refs=transaction.refs,  # type: ignore[attr-defined]
            transitions=transaction.ref_transitions,  # type: ignore[attr-defined]
            idempotency=transaction.idempotency,  # type: ignore[attr-defined]
            audit=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
            subjects=bound,
            evidence=bound,
            targets=bound,
            rollback_execution=ExactRollbackExecutionVerifier(
                runs=transaction.runs,  # type: ignore[attr-defined]
                profiles=transaction.policies,  # type: ignore[attr-defined]
            ),
        )

    def spec_capabilities(transaction: object) -> SpecUploadCapabilities:
        return SpecUploadCapabilities(
            refs=transaction.refs,  # type: ignore[attr-defined]
            artifacts=transaction.artifacts,  # type: ignore[attr-defined]
            object_bindings=transaction.object_bindings,  # type: ignore[attr-defined]
            audit=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
            idempotency=transaction.idempotency,  # type: ignore[attr-defined]
        )

    commands = ApprovalCommandService(
        unit_of_work=unit_of_work,
        bind_capabilities=command_capabilities,
        clock=clock,
        audit_chain_id=config.audit_chain_id,
    )
    applies = ApprovedApplyService(
        unit_of_work=unit_of_work,
        bind_capabilities=apply_capabilities,
        clock=clock,
        audit_chain_id=config.audit_chain_id,
    )
    spec_service = SpecUploadService(
        unit_of_work=unit_of_work,
        bind_capabilities=spec_capabilities,
        clock=clock,
        audit_chain_id=config.audit_chain_id,
    )

    cursor_key = _derive_key(config.root_secret, "workflow-cursor")

    class _RebasePayloads:
        def load_patch(self, artifact: object) -> object:
            with Session(engine) as session:
                return _workflow_readers(
                    session, object_store, config.object_store_id, cursor_key, clock
                ).load_patch(artifact)  # type: ignore[arg-type]

        def load_snapshot(self, artifact: object) -> object:
            with Session(engine) as session:
                return _workflow_readers(
                    session, object_store, config.object_store_id, cursor_key, clock
                ).load_snapshot(artifact)  # type: ignore[arg-type]

    def rebase_capabilities(transaction: object) -> RebaseWorkflowCapabilities:
        return RebaseWorkflowCapabilities(
            approval=command_capabilities(transaction),
            conflicts=transaction.conflicts,  # type: ignore[attr-defined]
        )

    rebase_service = RebaseWorkflowService(
        unit_of_work=unit_of_work,
        bind_capabilities=rebase_capabilities,
        approval_commands=commands,
        payloads=_RebasePayloads(),
        clock=clock,
        audit_chain_id=config.audit_chain_id,
    )

    @contextmanager
    def read_scope():
        with Session(engine) as session:
            object_bindings = SqlObjectBindingRepository(
                session, object_store, config.object_store_id
            )
            cursor_signer = CursorSigner(signing_key=cursor_key, clock=clock)
            artifacts = SqlArtifactRepository(
                session,
                binding_repository=object_bindings,
                cursor_signer=cursor_signer,
                clock=clock,
            )
            policies = SqlPolicySnapshotRepository(session, clock=clock)
            identities = SqlIdentityRepository(session, clock=clock)
            yield WorkflowReadPort(
                artifacts=artifacts,
                refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=clock),
                approvals=SqlApprovalRepository(session),
                policies=policies,
                readers=WorkflowTypedReaders(
                    artifacts=artifacts, bindings=object_bindings, objects=object_store
                ),
                progress_projector=CurrentApprovalProgressProjector(
                    policy_repository=policies,
                    role_policy_version=config.role_policy_version,
                    role_policy_digest=config.role_policy_digest,
                    principal_resolver=identities.project,
                ),
            )

    governance_provider: WorkflowGovernanceProvider | None = None
    scope_resolver: _RoutePolicyDomainScopeResolver | None = None
    if (
        config.workflow_route_policy_version is not None
        and config.workflow_route_policy_digest is not None
        and config.workflow_approval_policy_version is not None
        and config.workflow_approval_policy_digest is not None
    ):
        governance_provider = _SqlWorkflowGovernanceProvider(
            engine=engine,
            clock=clock,
            role_policy_version=config.role_policy_version,
            role_policy_digest=config.role_policy_digest,
            route_policy_version=config.workflow_route_policy_version,
            route_policy_digest=config.workflow_route_policy_digest,
            approval_policy_version=config.workflow_approval_policy_version,
            approval_policy_digest=config.workflow_approval_policy_digest,
        )
        scope_resolver = _RoutePolicyDomainScopeResolver(governance_provider)

    service = WorkflowCommandService(
        clock=clock,
        object_store=object_store,
        read_scope=read_scope,
        approval_commands=commands,
        apply_service=applies,
        rebase_service=rebase_service,
        spec_service=spec_service,
        governance=governance_provider,
        scope_resolver=scope_resolver,
        admission=admission,
        execution_profile_catalog=execution_profile_catalog,
        config_exporter=config_exporter,  # type: ignore[arg-type]
    )
    return WorkflowCommandAdapter(service)


def _workflow_readers(
    session: Session,
    object_store: LocalObjectStore,
    store_id: str,
    cursor_key: bytes,
    clock: SystemUtcClock,
) -> WorkflowTypedReaders:
    object_bindings = SqlObjectBindingRepository(session, object_store, store_id)
    artifacts = SqlArtifactRepository(
        session,
        binding_repository=object_bindings,
        cursor_signer=CursorSigner(signing_key=cursor_key, clock=clock),
        clock=clock,
    )
    return WorkflowTypedReaders(artifacts=artifacts, bindings=object_bindings, objects=object_store)


def build_local_api_resources(
    config: LocalApiConfig,
    *,
    trusted_components: TrustedComponentMaps | None = None,
    legacy_import_authority: LegacyImportAuthority | None = None,
) -> LocalApiResources:
    if not isinstance(config, LocalApiConfig):
        raise LocalApiConfigurationError("local API requires an exact LocalApiConfig")
    components = trusted_components or TrustedComponentMaps()
    if not isinstance(components, TrustedComponentMaps):
        raise LocalApiConfigurationError("trusted_components must be an exact TrustedComponentMaps")

    clock = SystemUtcClock()
    engine = get_engine(config.database_url)
    if engine.dialect.name != "sqlite":
        engine.dispose()
        raise LocalApiConfigurationError("local API composition requires SQLite")

    object_store = LocalObjectStore(
        config.object_store_root,
        store_id=config.object_store_id,
        clock=clock,
        cursor_signing_key=_derive_key(config.root_secret, "object-store-cursor"),
    )
    telemetry_store = LocalTelemetryStore(
        config.telemetry_db_path,
        clock=clock,
        signing_key=_derive_key(config.root_secret, "telemetry-cursor"),
    )
    token_runtime = SessionTokenRuntime(
        key_set_resolver=config.session_signing_keys.resolve,
        token_digest_key=_derive_key(config.root_secret, "session-token-digest"),
        csrf_digest_key=_derive_key(config.root_secret, "session-csrf-digest"),
    )
    api_key_runtime = ApiKeyRuntime(digest_key=_derive_key(config.root_secret, "api-key-digest"))
    password_runtime = Argon2PasswordRuntime()

    def capability_factory(session: Session) -> TransactionCapabilities:
        cursor_signer = CursorSigner(
            signing_key=_derive_key(config.root_secret, "workflow-cursor"),
            clock=clock,
        )
        object_bindings = SqlObjectBindingRepository(
            session,
            object_store,
            config.object_store_id,
        )
        return TransactionCapabilities(
            refs=SqlRefStore(session, cursor_signer=cursor_signer, clock=clock),
            audit=SqlAuditSink(session),
            approvals=SqlApprovalRepository(session),
            lineage=None,
            object_bindings=object_bindings,
            runs=SqlRunRepository(session),
            cost=SqlCostLedger(session, clock=clock),
            slo=SqlSloRepository(session),
            identity=SqlIdentityRepository(session, clock=clock),
            auth=SqlAuthRepository(session, clock=clock),
            policies=SqlPolicySnapshotRepository(session, clock=clock),
            idempotency=SqlIdempotencyRepository(session, clock=clock),
            artifacts=SqlArtifactRepository(
                session,
                binding_repository=object_bindings,
                cursor_signer=cursor_signer,
                clock=clock,
            ),
            conflicts=SqlConflictSetRepository(session, cursor_signer=cursor_signer, clock=clock),
            ref_transitions=SqlRefTransitionRepository(session),
        )

    unit_of_work = SqliteUnitOfWork(engine, capability_factory)

    def session_runtime(transaction: object) -> LocalSessionRuntime:
        return LocalSessionRuntime(
            auth_repository=transaction.auth,  # type: ignore[attr-defined]
            identity_repository=transaction.identity,  # type: ignore[attr-defined]
            session_policy_resolver=transaction.policies.get_session_policy,  # type: ignore[attr-defined]
            token_runtime=token_runtime,
            clock=clock,
            session_id_generator=lambda: f"session:{secrets.token_hex(16)}",
        )

    def bind_sessions(transaction: object) -> SessionAuthenticationCapabilities:
        current_policy = transaction.policies.get_password_hash_policy(  # type: ignore[attr-defined]
            config.current_password_hash_policy_version
        )
        if current_policy is None:
            raise IntegrityViolation("current password hash policy is unavailable")
        return SessionAuthenticationCapabilities(
            password_authenticator=LocalPasswordAuthenticator(
                auth_repository=transaction.auth,  # type: ignore[attr-defined]
                identity_repository=transaction.identity,  # type: ignore[attr-defined]
                normalization_policy_resolver=lambda version, digest: (
                    transaction.policies.get_login_name_normalization_policy(  # type: ignore[attr-defined]
                        policy_version=version,
                        policy_digest=digest,
                    )
                ),
                hash_policy_resolver=transaction.policies.get_password_hash_policy,  # type: ignore[attr-defined]
                current_hash_policy=current_policy,
                password_runtime=password_runtime,
                clock=clock,
            ),
            session_runtime=session_runtime(transaction),
            identities=transaction.identity,  # type: ignore[attr-defined]
            audit=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
        )

    def bind_api_keys(transaction: object) -> ApiKeyAuthenticationCapabilities:
        return ApiKeyAuthenticationCapabilities(
            authenticator=LocalApiKeyAuthenticator(
                auth_repository=transaction.auth,  # type: ignore[attr-defined]
                identity_repository=transaction.identity,  # type: ignore[attr-defined]
                api_key_runtime=api_key_runtime,
                clock=clock,
            ),
            identities=transaction.identity,  # type: ignore[attr-defined]
        )

    def bind_logout(transaction: object) -> LogoutCapabilities:
        return LogoutCapabilities(
            session_runtime=session_runtime(transaction),
            session_records=transaction.auth,  # type: ignore[attr-defined]
            identities=transaction.identity,  # type: ignore[attr-defined]
            idempotency=transaction.idempotency,  # type: ignore[attr-defined]
            audit=AuditGate(sink=transaction.audit, clock=clock),  # type: ignore[attr-defined]
        )

    session_authentication = SessionAuthenticationService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind_sessions,
        session_policy_version=config.session_policy_version,
        audit_chain_id=config.audit_chain_id,
    )
    api_key_authentication = ApiKeyAuthenticationService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind_api_keys,
    )
    logout_commands = LogoutCommandService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind_logout,
        audit_chain_id=config.audit_chain_id,
    )

    slo_service = SLODefinitionService(
        descriptor_retainer=telemetry_store,
        unit_of_work=unit_of_work,
        bind_capabilities=lambda transaction: SLODefinitionCapabilities(
            definitions=transaction.slo  # type: ignore[attr-defined]
        ),
    )
    builtin_registry = build_builtin_registry()
    persisted_catalogs = ()
    # Keep an unmigrated deployment constructible so `/readyz` can report the
    # migration-head dependency without creating tables.  Once policy storage
    # exists, compose every retained catalog into the immutable process registry;
    # exact historical REPLAY then works through the real local API rather than
    # only in a hand-built test engine.
    if inspect(engine).has_table("policy_snapshots"):
        with Session(engine) as session:
            persisted_catalogs = SqlPolicySnapshotRepository(
                session,
                clock=clock,
            ).list_execution_profile_catalogs()
    if persisted_catalogs:
        builtin_registry = builtin_registry.with_execution_profile_catalogs(
            persisted_catalogs,
            replace=True,
        )
    execution_profile_catalogs = builtin_registry.list_execution_profile_catalogs()
    if not execution_profile_catalogs:
        raise LocalApiConfigurationError("local API requires an execution-profile catalog")
    current_execution_profile_catalog = max(
        execution_profile_catalogs,
        key=lambda item: item.catalog_version,
    )
    registry_validator = PlatformReadinessValidator(
        registry=builtin_registry,
        components=components,
    )
    audit_cache = AuditVerificationCache()
    readiness = ReadinessService(
        ReadinessChecks(
            migration_head=MigrationHeadReadinessProbe(
                engine,
                expected_heads=migrations_api.expected_heads(config.database_url),
            ),
            database=DatabaseReadinessProbe(engine),
            object_store=LocalObjectStoreReadinessProbe(object_store),
            cost_ledger=CostLedgerReadinessProbe(engine),
            registry=RegistryReadinessProbe(registry_validator),
            slo_retention=SloRetentionReadinessProbe(slo_service),
            audit_cache=audit_cache.check_ready,
        )
    )
    read_services = build_local_read_services(
        engine=engine,
        object_store=object_store,
        object_store_id=config.object_store_id,
        telemetry_store=telemetry_store,
        role_policy_version=config.role_policy_version,
        role_policy_digest=config.role_policy_digest,
        execution_profile_catalog=current_execution_profile_catalog,
        cursor_signing_key=_derive_key(config.root_secret, "api-read-cursor"),
        clock=clock,
    )
    run_admission = _build_run_admission_engine(
        config=config,
        clock=clock,
        engine=engine,
        object_store=object_store,
        unit_of_work=unit_of_work,
        registry=builtin_registry,
        execution_profile_catalog=current_execution_profile_catalog,
        legacy_import_authority=legacy_import_authority,
    )
    # The synchronous ``:validate`` admission atomically starts validation: its injected
    # ``_ValidationStartWriter`` CASes the ApprovalItem ``draft→validating`` INSIDE the
    # same UoW that queues the Run (design §"validation start"). The same engine backs
    # ``dependencies.run_admission`` (generic ``POST /runs``) — those kinds carry no
    # validation subject, so the writer only fires on the ``*.validate`` operations.
    workflow_commands = _build_workflow_command_service(
        config=config,
        clock=clock,
        engine=engine,
        object_store=object_store,
        unit_of_work=unit_of_work,
        execution_profile_catalog=current_execution_profile_catalog,
        admission=run_admission,
        config_exporter=build_aureus_config_exporter(builtin_registry),
    )

    @contextmanager
    def run_event_read_scope() -> Iterator[RunEventReadScope]:
        with Session(engine) as session:
            yield RunEventReadScope(
                runs=SqlRunRepository(session),
                policies=SqlPolicySnapshotRepository(session, clock=clock),
                approvals=SqlApprovalRepository(session),
            )

    run_event_stream = RunEventStreamService(
        read_scope=run_event_read_scope,
        role_policy_version=config.role_policy_version,
        role_policy_digest=config.role_policy_digest,
    )
    run_event_notifier = RunEventNotifier()
    dependencies = ApiDependencies(
        session_authentication=session_authentication,
        api_key_authentication=api_key_authentication,
        logout_commands=logout_commands,
        readiness=readiness,
        content_reads=read_services.content,
        workflow_reads=read_services.workflows,
        observability_reads=read_services.observability,
        workflow_commands=workflow_commands,
        run_admission=run_admission,
        run_event_stream=run_event_stream,
        run_event_notifier=run_event_notifier,
        tracer=Tracer(
            exporter=_LocalSpanExporter(telemetry_store),
            sampler=AlwaysOnSampler(),
            resource={"service.name": "gameforge-api"},
        ),
        allowed_websocket_origins=config.allowed_websocket_origins,
    )
    return LocalApiResources(
        dependencies=dependencies,
        engine=engine,
        object_store=object_store,
        telemetry_store=telemetry_store,
        audit_cache=audit_cache,
        audit_chain_ids=(config.audit_chain_id,),
    )


def _local_lifespan(resources: LocalApiResources):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        await run_in_threadpool(resources.refresh_audit_cache)
        try:
            yield
        finally:
            await run_in_threadpool(resources.close)

    return lifespan


def create_local_app(
    config: LocalApiConfig | None = None,
    *,
    trusted_components: TrustedComponentMaps | None = None,
    legacy_import_authority: LegacyImportAuthority | None = None,
) -> FastAPI:
    resources = build_local_api_resources(
        config or LocalApiConfig.from_environment(),
        trusted_components=trusted_components,
        legacy_import_authority=legacy_import_authority,
    )
    app = create_app(resources.dependencies, lifespan=_local_lifespan(resources))
    app.state.local_resources = resources
    return app


def create_readiness_closed_local_app(
    config: LocalApiConfig | None = None,
    *,
    legacy_import_authority: LegacyImportAuthority | None = None,
) -> FastAPI:
    """Compose the local API with the canonical readiness-closing trusted components.

    The API process never EXECUTES Runs — it admits and serves reads — so it needs only
    the exact component KEY-SET to close the soft ``/readyz`` registry probe. A KEY-ONLY
    :class:`TrustedComponentMaps` (each key -> sentinel, from
    :func:`gameforge.platform.registry.build_readiness_component_maps`) is threaded so
    ``/readyz`` closes without importing the worker's executor graph into the API process.
    The worker (``apps/worker``) remains the sole process that supplies real executors.
    """

    return create_local_app(
        config,
        trusted_components=build_readiness_component_maps(build_builtin_registry()),
        legacy_import_authority=legacy_import_authority,
    )


__all__ = [
    "ALLOWED_WEBSOCKET_ORIGINS_ENV",
    "LOCAL_ROOT_SECRET_ENV",
    "OBJECT_STORE_ID_ENV",
    "OBJECT_STORE_ROOT_ENV",
    "SESSION_POLICY_VERSION_ENV",
    "TELEMETRY_DB_PATH_ENV",
    "WORKFLOW_APPROVAL_POLICY_DIGEST_ENV",
    "WORKFLOW_APPROVAL_POLICY_VERSION_ENV",
    "WORKFLOW_ROUTE_POLICY_DIGEST_ENV",
    "WORKFLOW_ROUTE_POLICY_VERSION_ENV",
    "LocalApiConfig",
    "LocalApiConfigurationError",
    "LocalApiResources",
    "build_local_api_resources",
    "create_local_app",
    "create_readiness_closed_local_app",
]
