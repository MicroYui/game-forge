from __future__ import annotations

from fastapi.testclient import TestClient

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies
from gameforge.apps.api.health import ReadinessChecks, ReadinessService
from gameforge.contracts.auth import ApiKeyAuthRequestV1, SecretText, SessionToken
from gameforge.contracts.errors import CredentialDisabled
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    Principal,
    RoleAssignmentV1,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.jobs import Problem


def _noop() -> None:
    return None


def _principal(
    *,
    principal_id: str,
    kind: str,
    authz_revision: int,
    with_tooling: bool,
) -> Principal:
    roles = ()
    if with_tooling:
        roles = (
            RoleAssignmentV1(
                assignment_id=f"assignment:{principal_id}:tooling",
                principal_id=principal_id,
                role="tooling",
                scope=None,
                status="active",
                revision=1,
                granted_at="2026-07-14T00:00:00Z",
                granted_by=AuditActor(
                    principal_id="system:bootstrap",
                    principal_kind="system",
                ),
            ),
        )
    return Principal(
        id=principal_id,
        kind=kind,
        display_name=principal_id,
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=authz_revision,
        roles=roles,
    )


class _MutableSessionAuth:
    def __init__(self) -> None:
        self.principal = _principal(
            principal_id="human:alice",
            kind="human",
            authz_revision=1,
            with_tooling=False,
        )
        self.failure: BaseException | None = None
        self.resolve_calls = 0

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
        request_id: str,
    ) -> ActorContext:
        del csrf_token, request_method
        assert token.get_secret_value() == "valid-session"
        self.resolve_calls += 1
        if self.failure is not None:
            raise self.failure
        return ActorContext(
            principal=self.principal,
            authentication=AuthenticationContext(
                mechanism="session",
                credential_id="password:alice",
            ),
            session_id="session:alice",
            request_id=request_id,
        )


class _MutableApiKeyAuth:
    def __init__(self) -> None:
        self.failure: BaseException | None = None

    def authenticate(
        self,
        request: ApiKeyAuthRequestV1,
        *,
        request_id: str,
    ) -> ActorContext:
        assert request.api_key.get_secret_value() == "gfk_service.valid"
        if self.failure is not None:
            raise self.failure
        return ActorContext(
            principal=_principal(
                principal_id="service:worker",
                kind="service",
                authz_revision=1,
                with_tooling=True,
            ),
            authentication=AuthenticationContext(
                mechanism="api_key",
                credential_id="api-key:worker",
            ),
            request_id=request_id,
        )


def _app(session_auth: object, api_key_auth: object):
    return create_app(
        ApiDependencies(
            session_authentication=session_auth,
            api_key_authentication=api_key_auth,
            readiness=ReadinessService(
                ReadinessChecks(
                    migration_head=_noop,
                    database=_noop,
                    object_store=_noop,
                    cost_ledger=_noop,
                    registry=_noop,
                    slo_retention=_noop,
                    audit_cache=_noop,
                )
            ),
        )
    )


def test_me_reloads_current_roles_and_credential_state_on_every_request() -> None:
    sessions = _MutableSessionAuth()
    app = _app(sessions, _MutableApiKeyAuth())

    with TestClient(app, base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "valid-session")
        initial = client.get("/api/v1/auth/me")
        sessions.principal = _principal(
            principal_id="human:alice",
            kind="human",
            authz_revision=2,
            with_tooling=True,
        )
        after_grant = client.get("/api/v1/auth/me")
        sessions.failure = CredentialDisabled("private rotation/disable reason")
        after_disable = client.get("/api/v1/auth/me")

    assert initial.status_code == 200
    assert initial.json()["roles"] == []
    assert after_grant.status_code == 200
    assert after_grant.json()["authz_revision"] == 2
    assert after_grant.json()["roles"][0]["role"] == "tooling"
    assert sessions.resolve_calls == 3
    assert after_disable.status_code == 401
    assert Problem.model_validate(after_disable.json()).code == "auth_failed"
    assert "private" not in after_disable.text


def test_api_key_disable_or_rotation_takes_effect_without_cache_lag() -> None:
    keys = _MutableApiKeyAuth()
    app = _app(_MutableSessionAuth(), keys)
    headers = {"Authorization": "ApiKey gfk_service.valid"}

    with TestClient(app, base_url="https://gameforge.test") as client:
        initial = client.get("/api/v1/auth/me", headers=headers)
        keys.failure = CredentialDisabled("private key rotation reason")
        after_rotation = client.get("/api/v1/auth/me", headers=headers)

    assert initial.status_code == 200
    assert after_rotation.status_code == 401
    assert Problem.model_validate(after_rotation.json()).code == "auth_failed"
    assert "private" not in after_rotation.text


def test_http_authentication_cannot_materialize_a_system_actor() -> None:
    system = ActorContext(
        principal=_principal(
            principal_id="system:worker",
            kind="system",
            authz_revision=1,
            with_tooling=False,
        ),
        authentication=AuthenticationContext(mechanism="trusted_internal"),
        request_id="request:system",
    )

    class InvalidSessionAuth:
        def resolve(self, *args: object, **kwargs: object) -> ActorContext:
            return system

    app = _app(InvalidSessionAuth(), _MutableApiKeyAuth())
    with TestClient(app, base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "valid-session")
        response = client.get("/api/v1/auth/me")

    assert response.status_code == 500
    assert Problem.model_validate(response.json()).code == "integrity_violation"
    assert "system:worker" not in response.text
