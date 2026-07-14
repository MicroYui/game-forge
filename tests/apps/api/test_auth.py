from __future__ import annotations

from fastapi.testclient import TestClient

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies
from gameforge.apps.api.health import ReadinessChecks, ReadinessService
from gameforge.contracts.auth import (
    ApiKeyAuthRequestV1,
    PasswordAuthRequestV1,
    SecretText,
    SessionIssueV1,
    SessionToken,
)
from gameforge.contracts.errors import AuthFailed
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    Principal,
)
from gameforge.contracts.jobs import Problem


def _noop() -> None:
    return None


def _principal(principal_id: str, kind: str, display_name: str) -> Principal:
    return Principal(
        id=principal_id,
        kind=kind,
        display_name=display_name,
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=(),
    )


class _SessionAuth:
    def __init__(self) -> None:
        self._ordinal = 0
        self._sessions: dict[str, Principal] = {}

    def login(
        self,
        request: PasswordAuthRequestV1,
        *,
        request_id: str,
    ) -> SessionIssueV1:
        del request_id
        password = request.password.get_secret_value()
        if password != "correct-password" or request.login_name not in {"alice", "bob"}:
            raise AuthFailed("private authentication reason")
        self._ordinal += 1
        token = f"session-token-{self._ordinal}"
        self._sessions[token] = _principal(
            f"human:{request.login_name}",
            "human",
            request.login_name.title(),
        )
        return SessionIssueV1(
            session_id=f"session:{self._ordinal}",
            session_token=SessionToken(token),
            csrf_token=SecretText(f"csrf-token-{self._ordinal}"),
            absolute_expires_at="2099-07-15T00:00:00Z",
            idle_expires_at="2099-07-14T01:00:00Z",
        )

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
        request_id: str,
    ) -> ActorContext:
        del csrf_token, request_method
        value = token.get_secret_value()
        principal = self._sessions.get(value)
        if principal is None:
            raise AuthFailed("private session reason")
        return ActorContext(
            principal=principal,
            authentication=AuthenticationContext(
                mechanism="session",
                credential_id=f"password:{principal.id}",
            ),
            session_id=f"session:{value}",
            request_id=request_id,
        )


class _ApiKeyAuth:
    def authenticate(
        self,
        request: ApiKeyAuthRequestV1,
        *,
        request_id: str,
    ) -> ActorContext:
        if request.api_key.get_secret_value() != "gfk_service.valid-key":
            raise AuthFailed("private key reason")
        principal = _principal("service:automation", "service", "Automation")
        return ActorContext(
            principal=principal,
            authentication=AuthenticationContext(
                mechanism="api_key",
                credential_id="api-key:automation",
            ),
            request_id=request_id,
        )


def _app(session_auth: _SessionAuth | None = None):
    return create_app(
        ApiDependencies(
            session_authentication=session_auth or _SessionAuth(),
            api_key_authentication=_ApiKeyAuth(),
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


def test_two_https_clients_receive_isolated_secure_browser_sessions() -> None:
    session_auth = _SessionAuth()
    app = _app(session_auth)

    with (
        TestClient(app, base_url="https://gameforge.test") as alice,
        TestClient(app, base_url="https://gameforge.test") as bob,
    ):
        alice_login = alice.post(
            "/api/v1/auth/login",
            json={"login_name": "alice", "password": "correct-password"},
        )
        bob_login = bob.post(
            "/api/v1/auth/login",
            json={"login_name": "bob", "password": "correct-password"},
        )
        alice_me = alice.get("/api/v1/auth/me")
        bob_me = bob.get("/api/v1/auth/me")

    assert alice_login.status_code == bob_login.status_code == 204
    assert alice_login.content == bob_login.content == b""
    assert alice_login.headers["X-CSRF-Token"] == "csrf-token-1"
    assert bob_login.headers["X-CSRF-Token"] == "csrf-token-2"
    assert alice_login.headers["Cache-Control"] == "no-store"
    assert bob_login.headers["Cache-Control"] == "no-store"
    assert alice.cookies.get("gameforge_session") != bob.cookies.get("gameforge_session")
    for response in (alice_login, bob_login):
        cookie = response.headers["Set-Cookie"]
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=strict" in cookie
        assert "Path=/" in cookie
        assert "session-token" not in response.text
        assert "csrf-token" not in response.text
    assert alice_me.status_code == bob_me.status_code == 200
    assert alice_me.headers["Cache-Control"] == "no-store"
    assert bob_me.headers["Cache-Control"] == "no-store"
    assert alice_me.json()["id"] == "human:alice"
    assert bob_me.json()["id"] == "human:bob"
    assert "authentication" not in alice_me.json()
    assert "session_id" not in alice_me.json()


def test_api_key_me_returns_current_service_identity_without_session_semantics() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "ApiKey gfk_service.valid-key"},
        )

    assert response.status_code == 200
    assert response.json()["id"] == "service:automation"
    assert response.json()["kind"] == "service"
    assert "Set-Cookie" not in response.headers
    assert "session_id" not in response.json()


def test_missing_credentials_and_login_failures_are_uniformly_redacted() -> None:
    app = _app()
    with TestClient(app, base_url="https://gameforge.test") as client:
        missing = client.get("/api/v1/auth/me")
        wrong_password = client.post(
            "/api/v1/auth/login",
            json={"login_name": "alice", "password": "wrong-password"},
        )
        unknown_login = client.post(
            "/api/v1/auth/login",
            json={"login_name": "unknown", "password": "wrong-password"},
        )
        bad_key = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "ApiKey gfk_service.invalid"},
        )
        oversized_key = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "ApiKey " + ("x" * 4097)},
        )

    assert missing.status_code == 401
    assert Problem.model_validate(missing.json()).code == "auth_required"
    for response in (wrong_password, unknown_login, bad_key, oversized_key):
        assert response.status_code == 401
        problem = Problem.model_validate(response.json())
        assert problem.code == "auth_failed"
        assert "private" not in response.text
        assert "unknown" not in response.text
        assert "wrong-password" not in response.text


def test_invalid_stale_cookie_does_not_block_password_reauthentication() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "expired-or-revoked-session")
        response = client.post(
            "/api/v1/auth/login",
            json={"login_name": "alice", "password": "correct-password"},
        )

    assert response.status_code == 204
    assert "session-token-1" in response.headers["Set-Cookie"]
