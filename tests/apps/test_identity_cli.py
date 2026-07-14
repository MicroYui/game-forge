from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gameforge.apps.cli.__main__ import main as cli_main
from gameforge.apps.cli.identity import (
    LOGIN_NORMALIZATION_POLICY_DIGEST_ENV,
    LOGIN_NORMALIZATION_POLICY_VERSION_ENV,
    PASSWORD_HASH_POLICY_VERSION_ENV,
    ROLE_POLICY_DIGEST_ENV,
    ROLE_POLICY_VERSION_ENV,
)
from gameforge.apps.identity_cli import main
from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    SecretText,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.errors import Conflict
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    Permission,
    RolePolicy,
    compute_domain_registry_digest,
    compute_role_policy_digest,
)
from gameforge.platform.identity.bootstrap import (
    BootstrapAdminRequest,
    BootstrapResult,
)
from gameforge.runtime.persistence.engine import DATABASE_URL_ENV, get_engine
from gameforge.runtime.persistence.models import (
    AuditRow,
    Base,
    PasswordCredentialRow,
    PrincipalRow,
    RoleAssignmentRow,
)
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)


class _Clock:
    def now_utc(self) -> datetime:
        return NOW


def _seed_retained_cli_policies(
    database_url: str,
) -> tuple[
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    RolePolicy,
]:
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    normalization_payload = {
        "policy_version": "login-normalization@cli-test",
        "unicode_normalization": "NFKC",
        "trim_unicode_whitespace": True,
        "case_mapping": "unicode_casefold",
        "reject_categories": ("control", "private_use", "surrogate"),
        "minimum_codepoints": 1,
        "maximum_codepoints": 128,
    }
    normalization = LoginNameNormalizationPolicyV1(
        **normalization_payload,
        policy_digest=compute_login_name_normalization_policy_digest(normalization_payload),
    )
    password_hash = PasswordHashPolicyV1(
        policy_version="argon2id@cli-test",
        algorithm="argon2id",
        memory_kib=8192,
        iterations=1,
        parallelism=1,
        salt_bytes=16,
        rehash_on_login=True,
        effective_from="2026-07-14T00:00:00Z",
    )
    definitions = (
        DomainDefinitionV1(
            domain_id="game-content",
            display_name="Game Content",
            tags=(),
            status="active",
        ),
    )
    registry = DomainRegistryV1(
        registry_version="domains@cli-test",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(
            "domains@cli-test",
            definitions,
        ),
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
    roles = RolePolicy(
        policy_version="roles@cli-test",
        domain_registry_ref=registry_ref,
        grants=grants,
        effective_from="2026-07-14T00:00:00Z",
        policy_digest=compute_role_policy_digest(
            "roles@cli-test",
            registry_ref,
            grants,
            "2026-07-14T00:00:00Z",
        ),
    )
    with Session(engine) as session, session.begin():
        policies = SqlPolicySnapshotRepository(session, clock=_Clock())
        policies.put_login_name_normalization_policy(normalization)
        policies.put_password_hash_policy(password_hash)
        policies.put_domain_registry(registry)
        policies.put_role_policy(roles)
    engine.dispose()
    return normalization, password_hash, roles


class _BootstrapService:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.requests: list[BootstrapAdminRequest] = []

    def bootstrap(self, request: BootstrapAdminRequest) -> BootstrapResult:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return BootstrapResult(
            principal_id="human:admin",
            principal_revision=4,
            password_credential_id="password:admin:1",
            roles=("identity_admin", "tooling"),
        )


def test_identity_cli_bootstrap_calls_only_the_platform_service_and_redacts_secret(
    capsys,
) -> None:
    service = _BootstrapService()
    answers = iter(("not-printed", "not-printed"))

    exit_code = main(
        ["bootstrap", "--display-name", "Admin", "--login-name", "admin"],
        service=service,
        password_reader=lambda prompt: next(answers),
    )

    assert exit_code == 0
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.display_name == "Admin"
    assert request.login_name == "admin"
    assert isinstance(request.password, SecretText)
    assert request.password.get_secret_value() == "not-printed"
    output = capsys.readouterr().out
    assert "not-printed" not in output
    assert json.loads(output) == {
        "password_credential_id": "password:admin:1",
        "principal_id": "human:admin",
        "principal_revision": 4,
        "roles": ["identity_admin", "tooling"],
        "status": "created",
    }


def test_identity_cli_rejects_password_confirmation_mismatch_without_calling_service(
    capsys,
) -> None:
    service = _BootstrapService()
    answers = iter(("first", "second"))

    exit_code = main(
        ["bootstrap", "--display-name", "Admin", "--login-name", "admin"],
        service=service,
        password_reader=lambda prompt: next(answers),
    )

    assert exit_code == 2
    assert service.requests == []
    assert json.loads(capsys.readouterr().err) == {
        "code": "password_confirmation_mismatch",
        "status": "rejected",
    }


def test_identity_cli_has_no_system_or_service_credential_bootstrap_option() -> None:
    service = _BootstrapService()

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "bootstrap",
                "--display-name",
                "System",
                "--login-name",
                "system",
                "--kind",
                "system",
            ],
            service=service,
            password_reader=lambda prompt: "unused",
        )

    assert raised.value.code == 2
    assert service.requests == []


def test_identity_cli_reports_existing_store_conflict_without_retrying(capsys) -> None:
    service = _BootstrapService(
        failure=Conflict("identity store is not empty", existing_principal_id="human:first")
    )
    answers = iter(("secret", "secret"))

    exit_code = main(
        ["bootstrap", "--display-name", "Admin", "--login-name", "admin"],
        service=service,
        password_reader=lambda prompt: next(answers),
    )

    assert exit_code == 1
    assert len(service.requests) == 1
    assert json.loads(capsys.readouterr().err) == {
        "code": "revision_conflict",
        "status": "rejected",
    }


def test_gameforge_cli_dispatch_constructs_real_bootstrap_service_when_none_is_injected(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'identity-cli.db'}"
    normalization, password_hash, roles = _seed_retained_cli_policies(database_url)
    environment = {
        DATABASE_URL_ENV: database_url,
        LOGIN_NORMALIZATION_POLICY_VERSION_ENV: normalization.policy_version,
        LOGIN_NORMALIZATION_POLICY_DIGEST_ENV: normalization.policy_digest,
        PASSWORD_HASH_POLICY_VERSION_ENV: password_hash.policy_version,
        ROLE_POLICY_VERSION_ENV: roles.policy_version,
        ROLE_POLICY_DIGEST_ENV: roles.policy_digest,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    answers = iter(("correct horse battery staple", "correct horse battery staple"))

    exit_code = cli_main(
        [
            "identity",
            "bootstrap",
            "--display-name",
            "Platform Admin",
            "--login-name",
            "admin",
        ],
        identity_password_reader=lambda prompt: next(answers),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "correct horse battery staple" not in output
    result = json.loads(output)
    assert result["status"] == "created"
    assert result["roles"] == ["identity_admin", "tooling"]
    engine = get_engine(database_url)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PrincipalRow)) == 1
        assert session.scalar(select(func.count()).select_from(RoleAssignmentRow)) == 2
        assert session.scalar(select(func.count()).select_from(PasswordCredentialRow)) == 1
        assert session.scalar(select(func.count()).select_from(AuditRow)) == 1
    engine.dispose()
