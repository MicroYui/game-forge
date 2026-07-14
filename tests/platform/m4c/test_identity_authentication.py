from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.auth import ApiKeyAuthRequestV1, ApiKeyRecordV1
from gameforge.contracts.errors import CredentialDisabled, IntegrityViolation
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.identity.authentication import (
    ApiKeyAuthenticationCapabilities,
    ApiKeyAuthenticationService,
    IdentityProjectionCapabilities,
    TrustedSystemActorFactory,
)
from gameforge.runtime.auth.local import LocalApiKeyAuthenticator
from gameforge.runtime.auth.tokens import ApiKeyRuntime
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base
from gameforge.runtime.persistence.transaction import TransactionCapabilities
from gameforge.runtime.persistence.uow import SqliteUnitOfWork


T0 = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)


@dataclass
class _Clock:
    current: datetime = T0

    def now_utc(self) -> datetime:
        return self.current


class _Entropy:
    def __init__(self) -> None:
        self._ordinal = 0

    def __call__(self, size: int) -> bytes:
        self._ordinal += 1
        block = hashlib.sha512(f"api-key:{self._ordinal}".encode()).digest()
        return (block * ((size // len(block)) + 1))[:size]


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'identity-authentication.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _uow(engine: Engine, clock: _Clock) -> SqliteUnitOfWork:
    def capabilities(session: Session) -> TransactionCapabilities:
        return TransactionCapabilities(
            refs=None,
            audit=None,
            approvals=None,
            lineage=None,
            object_bindings=None,
            runs=None,
            cost=None,
            identity=SqlIdentityRepository(session, clock=clock),
            auth=SqlAuthRepository(session, clock=clock),
        )

    return SqliteUnitOfWork(engine, capabilities)


def test_api_key_authentication_builds_service_actor_from_current_authority(
    engine: Engine,
) -> None:
    clock = _Clock()
    key_runtime = ApiKeyRuntime(digest_key=b"a" * 32, random_bytes=_Entropy())
    issued = key_runtime.issue()
    with Session(engine) as session:
        identities = SqlIdentityRepository(session, clock=clock)
        auth = SqlAuthRepository(session, clock=clock)
        principal = identities.create(
            principal_id="service:worker",
            kind="service",
            display_name="Worker",
        )
        auth.create_api_key(
            ApiKeyRecordV1(
                api_key_id="api-key:worker:1",
                principal_id=principal.principal_id,
                key_prefix=issued.key_prefix,
                key_digest=issued.key_digest,
                credential_version=1,
                status="active",
                created_at="2026-07-14T08:00:00Z",
                revision=1,
            )
        )
        identities.grant(
            assignment_id="role:worker:tooling",
            principal_id=principal.principal_id,
            role="tooling",
            scope=None,
            granted_by=AuditActor(
                principal_id="system:bootstrap",
                principal_kind="system",
            ),
            expected_principal_revision=principal.revision,
        )
        session.commit()

    unit_of_work = _uow(engine, clock)

    def bind(transaction: object) -> ApiKeyAuthenticationCapabilities:
        return ApiKeyAuthenticationCapabilities(
            authenticator=LocalApiKeyAuthenticator(
                auth_repository=transaction.auth,  # type: ignore[attr-defined]
                identity_repository=transaction.identity,  # type: ignore[attr-defined]
                api_key_runtime=key_runtime,
                clock=clock,
            ),
            identities=transaction.identity,  # type: ignore[attr-defined]
        )

    service = ApiKeyAuthenticationService(
        unit_of_work=unit_of_work,
        bind_capabilities=bind,
    )
    actor = service.authenticate(
        ApiKeyAuthRequestV1(api_key=issued.api_key),
        request_id="request:worker:1",
    )

    assert actor.principal.kind == "service"
    assert actor.authentication.mechanism == "api_key"
    assert actor.authentication.credential_id == "api-key:worker:1"
    assert actor.session_id is None
    assert [assignment.role for assignment in actor.principal.roles] == ["tooling"]

    with Session(engine) as session:
        identities = SqlIdentityRepository(session, clock=clock)
        assignment = identities.get_assignment("role:worker:tooling")
        current = identities.get("service:worker")
        assert assignment is not None and current is not None
        identities.revoke(
            assignment_id=assignment.assignment_id,
            revoked_by=AuditActor(
                principal_id="system:bootstrap",
                principal_kind="system",
            ),
            revoke_reason="least_privilege",
            expected_principal_revision=current.revision,
            expected_assignment_revision=assignment.revision,
        )
        session.commit()

    refreshed = service.authenticate(
        ApiKeyAuthRequestV1(api_key=issued.api_key),
        request_id="request:worker:2",
    )
    assert refreshed.principal.roles == ()
    assert refreshed.principal.authz_revision > actor.principal.authz_revision


def test_api_key_authentication_rejects_disabled_principal_immediately(engine: Engine) -> None:
    clock = _Clock()
    key_runtime = ApiKeyRuntime(digest_key=b"a" * 32, random_bytes=_Entropy())
    issued = key_runtime.issue()
    with Session(engine) as session:
        identities = SqlIdentityRepository(session, clock=clock)
        auth = SqlAuthRepository(session, clock=clock)
        principal = identities.create(
            principal_id="service:worker",
            kind="service",
            display_name="Worker",
        )
        auth.create_api_key(
            ApiKeyRecordV1(
                api_key_id="api-key:worker:1",
                principal_id=principal.principal_id,
                key_prefix=issued.key_prefix,
                key_digest=issued.key_digest,
                credential_version=1,
                status="active",
                created_at="2026-07-14T08:00:00Z",
                revision=1,
            )
        )
        identities.disable(
            principal.principal_id,
            disabled_reason="incident",
            expected_revision=principal.revision,
        )
        session.commit()

    unit_of_work = _uow(engine, clock)
    service = ApiKeyAuthenticationService(
        unit_of_work=unit_of_work,
        bind_capabilities=lambda transaction: ApiKeyAuthenticationCapabilities(
            authenticator=LocalApiKeyAuthenticator(
                auth_repository=transaction.auth,
                identity_repository=transaction.identity,
                api_key_runtime=key_runtime,
                clock=clock,
            ),
            identities=transaction.identity,
        ),
    )
    with pytest.raises(CredentialDisabled):
        service.authenticate(
            ApiKeyAuthRequestV1(api_key=issued.api_key),
            request_id="request:disabled",
        )


def test_trusted_system_actor_has_no_http_credential_construction_path(engine: Engine) -> None:
    clock = _Clock()
    with Session(engine) as session:
        identities = SqlIdentityRepository(session, clock=clock)
        identities.create(
            principal_id="system:worker-supervisor",
            kind="system",
            display_name="Worker Supervisor",
        )
        identities.create(
            principal_id="human:alice",
            kind="human",
            display_name="Alice",
        )
        session.commit()

    unit_of_work = _uow(engine, clock)
    bind = lambda transaction: IdentityProjectionCapabilities(  # noqa: E731
        identities=transaction.identity
    )
    with pytest.raises(TypeError, match="trusted composition root"):
        TrustedSystemActorFactory(  # type: ignore[call-arg]
            unit_of_work=unit_of_work,
            bind_capabilities=bind,
        )

    factory = TrustedSystemActorFactory.from_trusted_composition_root(
        unit_of_work=unit_of_work,
        bind_capabilities=bind,
    )
    actor = factory.actor_for(
        principal_id="system:worker-supervisor",
        request_id="worker-loop:1",
    )
    assert actor.principal.kind == "system"
    assert actor.authentication.mechanism == "trusted_internal"
    assert actor.authentication.credential_id is None
    assert actor.session_id is None

    with pytest.raises(IntegrityViolation, match="system principal"):
        factory.actor_for(principal_id="human:alice", request_id="worker-loop:2")
