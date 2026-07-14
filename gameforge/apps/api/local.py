"""Real local composition for the API process.

This module owns environment reads and concrete adapters. ``app.create_app``
stays side-effect free for tests and later production composition roots.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import os
from pathlib import Path
import secrets

from fastapi import FastAPI
from sqlalchemy import Engine
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
from gameforge.apps.cli.identity import (
    AUDIT_CHAIN_ID_ENV,
    PASSWORD_HASH_POLICY_VERSION_ENV,
    ROLE_POLICY_DIGEST_ENV,
    ROLE_POLICY_VERSION_ENV,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.observability import SpanDataV1
from gameforge.platform.audit.gate import AuditGate
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
)
from gameforge.platform.slo.service import (
    SLODefinitionCapabilities,
    SLODefinitionService,
)
from gameforge.runtime.auth.local import (
    LocalApiKeyAuthenticator,
    LocalPasswordAuthenticator,
    LocalSessionRuntime,
)
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime
from gameforge.runtime.auth.tokens import ApiKeyRuntime, SessionTokenRuntime
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.object_store import LocalObjectStore
from gameforge.runtime.observability import AlwaysOnSampler, Tracer
from gameforge.runtime.observability.local_store import LocalTelemetryStore
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import DATABASE_URL_ENV, DEFAULT_URL, get_engine
from gameforge.runtime.persistence.idempotency import SqlIdempotencyRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
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


def build_local_api_resources(
    config: LocalApiConfig,
    *,
    trusted_components: TrustedComponentMaps | None = None,
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
        return TransactionCapabilities(
            refs=None,
            audit=SqlAuditSink(session),
            approvals=None,
            lineage=None,
            object_bindings=None,
            runs=None,
            cost=SqlCostLedger(session, clock=clock),
            slo=SqlSloRepository(session),
            identity=SqlIdentityRepository(session, clock=clock),
            auth=SqlAuthRepository(session, clock=clock),
            policies=SqlPolicySnapshotRepository(session, clock=clock),
            idempotency=SqlIdempotencyRepository(session, clock=clock),
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
    execution_profile_catalogs = builtin_registry.list_execution_profile_catalogs()
    if len(execution_profile_catalogs) != 1:
        raise LocalApiConfigurationError(
            "local API requires exactly one built-in execution-profile catalog"
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
        execution_profile_catalog=execution_profile_catalogs[0],
        cursor_signing_key=_derive_key(config.root_secret, "api-read-cursor"),
        clock=clock,
    )
    dependencies = ApiDependencies(
        session_authentication=session_authentication,
        api_key_authentication=api_key_authentication,
        logout_commands=logout_commands,
        readiness=readiness,
        content_reads=read_services.content,
        workflow_reads=read_services.workflows,
        observability_reads=read_services.observability,
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
) -> FastAPI:
    resources = build_local_api_resources(
        config or LocalApiConfig.from_environment(),
        trusted_components=trusted_components,
    )
    app = create_app(resources.dependencies, lifespan=_local_lifespan(resources))
    app.state.local_resources = resources
    return app


__all__ = [
    "ALLOWED_WEBSOCKET_ORIGINS_ENV",
    "LOCAL_ROOT_SECRET_ENV",
    "OBJECT_STORE_ID_ENV",
    "OBJECT_STORE_ROOT_ENV",
    "SESSION_POLICY_VERSION_ENV",
    "TELEMETRY_DB_PATH_ENV",
    "LocalApiConfig",
    "LocalApiConfigurationError",
    "LocalApiResources",
    "build_local_api_resources",
    "create_local_app",
]
