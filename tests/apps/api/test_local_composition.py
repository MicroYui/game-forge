from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from gameforge.apps.api.local import (
    LOCAL_ROOT_SECRET_ENV,
    SESSION_POLICY_VERSION_ENV,
    LocalApiConfig,
    LocalApiConfigurationError,
    create_local_app,
)
from gameforge.apps.cli.identity import (
    PASSWORD_HASH_POLICY_VERSION_ENV,
    IdentityBootstrapConfig,
    build_bootstrap_service,
)
from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    SecretText,
    SessionPolicyV1,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    Permission,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.contracts.jobs import Problem
from gameforge.platform.identity.bootstrap import BootstrapAdminRequest
from gameforge.runtime.auth.tokens import SessionSigningKey, SessionSigningKeySet
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.persistence import migrations_api
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.engine import DATABASE_URL_ENV, get_engine
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository
from gameforge.runtime.secrets.session_keys import (
    SESSION_SIGNING_KEY_SETS_ENV,
    SessionSigningKeyProvider,
)


def _normalization_policy() -> LoginNameNormalizationPolicyV1:
    payload = {
        "policy_version": "login-normalization@1",
        "unicode_normalization": "NFKC",
        "trim_unicode_whitespace": True,
        "case_mapping": "unicode_casefold",
        "reject_categories": ("control", "private_use", "surrogate"),
        "minimum_codepoints": 1,
        "maximum_codepoints": 128,
    }
    return LoginNameNormalizationPolicyV1(
        **payload,
        policy_digest=compute_login_name_normalization_policy_digest(payload),
    )


def _password_policy() -> PasswordHashPolicyV1:
    return PasswordHashPolicyV1(
        policy_version="argon2id@1",
        algorithm="argon2id",
        memory_kib=8192,
        iterations=1,
        parallelism=1,
        salt_bytes=16,
        rehash_on_login=True,
        effective_from="2026-07-14T00:00:00Z",
    )


def _session_policy() -> SessionPolicyV1:
    return SessionPolicyV1(
        policy_version="session@1",
        absolute_ttl_s=3600,
        idle_ttl_s=600,
        touch_interval_s=60,
        signing_key_set_version="keys@1",
        csrf_mode="synchronizer_token",
        same_site="strict",
        secure_cookie_required=True,
    )


def _domain_and_role_policy() -> tuple[DomainRegistryV1, RolePolicy]:
    definitions = (
        DomainDefinitionV1(
            domain_id="game-content",
            display_name="Game Content",
            tags=(),
            status="active",
        ),
    )
    registry = DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )
    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    grants = {
        "identity_admin": (
            Permission(
                action="identity.manage",
                resource_kind="identity",
                domain_scope=None,
            ),
        ),
        "tooling": (
            Permission(
                action="run",
                resource_kind="tooling",
                domain_scope="all",
            ),
        ),
    }
    effective_from = "2026-07-14T00:00:00Z"
    policy = RolePolicy(
        policy_version="roles@1",
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from=effective_from,
        policy_digest=compute_role_policy_digest(
            "roles@1",
            registry_ref,
            grants,
            effective_from,
        ),
    )
    return registry, policy


def _signing_keys() -> SessionSigningKeyProvider:
    return SessionSigningKeyProvider(
        (
            SessionSigningKeySet(
                key_set_version="keys@1",
                keys=(
                    SessionSigningKey(
                        key_id="session-key-1",
                        secret=b"s" * 32,
                        status="active",
                    ),
                ),
            ),
        )
    )


def _config(tmp_path, database_url: str) -> LocalApiConfig:
    return LocalApiConfig(
        database_url=database_url,
        object_store_root=tmp_path / "objects",
        object_store_id="local:test",
        telemetry_db_path=tmp_path / "telemetry.sqlite3",
        current_password_hash_policy_version="argon2id@1",
        session_policy_version="session@1",
        audit_chain_id="identity",
        root_secret=b"r" * 32,
        session_signing_keys=_signing_keys(),
        allowed_websocket_origins=frozenset({"https://console.gameforge.test"}),
    )


def _seed_and_bootstrap(database_url: str) -> None:
    migrations_api.upgrade(database_url)
    engine = get_engine(database_url)
    normalization = _normalization_policy()
    password = _password_policy()
    session_policy = _session_policy()
    domain_registry, role_policy = _domain_and_role_policy()
    with Session(engine) as session, session.begin():
        policies = SqlPolicySnapshotRepository(session, clock=SystemUtcClock())
        policies.put_login_name_normalization_policy(normalization)
        policies.put_password_hash_policy(password)
        policies.put_session_policy(session_policy)
        policies.put_domain_registry(domain_registry)
        policies.put_role_policy(role_policy)
    engine.dispose()

    service = build_bootstrap_service(
        IdentityBootstrapConfig(
            database_url=database_url,
            login_normalization_policy_version=normalization.policy_version,
            login_normalization_policy_digest=normalization.policy_digest,
            password_hash_policy_version=password.policy_version,
            role_policy_version=role_policy.policy_version,
            role_policy_digest=role_policy.policy_digest,
            audit_chain_id="identity",
        )
    )
    service.bootstrap(
        BootstrapAdminRequest(
            display_name="Local Admin",
            login_name="admin",
            password=SecretText("correct-password"),
        )
    )


def test_environment_configuration_requires_stable_secret_without_leaking_it(tmp_path) -> None:
    secret = b"z" * 32
    encoded = base64.b64encode(secret).decode("ascii")
    signing = json.dumps(
        [
            {
                "key_set_version": "keys@1",
                "keys": [
                    {
                        "key_id": "session-key-1",
                        "secret_base64": base64.b64encode(b"s" * 32).decode("ascii"),
                        "status": "active",
                    }
                ],
            }
        ]
    )
    config = LocalApiConfig.from_environment(
        {
            DATABASE_URL_ENV: f"sqlite:///{tmp_path / 'configured.db'}",
            PASSWORD_HASH_POLICY_VERSION_ENV: "argon2id@1",
            SESSION_POLICY_VERSION_ENV: "session@1",
            LOCAL_ROOT_SECRET_ENV: encoded,
            SESSION_SIGNING_KEY_SETS_ENV: signing,
        }
    )

    rendered = repr(config)
    assert config.root_secret == secret
    assert encoded not in rendered
    assert secret.hex() not in rendered

    with pytest.raises(LocalApiConfigurationError, match=LOCAL_ROOT_SECRET_ENV):
        LocalApiConfig.from_environment(
            {
                PASSWORD_HASH_POLICY_VERSION_ENV: "argon2id@1",
                SESSION_POLICY_VERSION_ENV: "session@1",
                LOCAL_ROOT_SECRET_ENV: "not-base64",
                SESSION_SIGNING_KEY_SETS_ENV: signing,
            }
        )


def test_real_local_composition_runs_login_me_logout_and_honest_readiness(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'gameforge.db'}"
    _seed_and_bootstrap(database_url)
    config = _config(tmp_path, database_url)
    app = create_local_app(config=config)

    with TestClient(app, base_url="https://gameforge.test") as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "admin", "password": "correct-password"},
        )
        me = client.get("/api/v1/auth/me")
        readiness = client.get("/readyz")
        logout = client.post(
            "/api/v1/auth/logout",
            headers={
                "Idempotency-Key": "logout:local-admin:1",
                "X-CSRF-Token": login.headers["X-CSRF-Token"],
            },
        )
        after_logout = client.get("/api/v1/auth/me")

    assert login.status_code == 204
    assert me.status_code == 200
    assert me.json()["display_name"] == "Local Admin"
    assert logout.status_code == 204
    assert after_logout.status_code == 401
    assert readiness.status_code == 503
    readiness_problem = Problem.model_validate(readiness.json())
    assert readiness_problem.code == "dependency_unavailable"
    assert readiness_problem.errors == ({"component": "registry"},)

    engine = get_engine(database_url)
    with Session(engine) as session:
        assert SqlAuditSink(session).verify_chain("identity") is True
    engine.dispose()


def test_local_composition_never_auto_migrates_authoritative_database(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'unmigrated.db'}"
    app = create_local_app(config=_config(tmp_path, database_url))

    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    problem = Problem.model_validate(response.json())
    assert problem.errors == ({"component": "migration_head"},)
    engine = get_engine(database_url)
    assert "alembic_version" not in inspect(engine).get_table_names()
    engine.dispose()
