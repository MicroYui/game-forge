from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

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
from gameforge.contracts.errors import CsrfFailed
from gameforge.contracts.identity import ActorContext, AuthenticationContext, Principal
from gameforge.contracts.jobs import Problem


def _noop() -> None:
    return None


def _principal(kind: str) -> Principal:
    return Principal(
        id=f"{kind}:csrf-test",
        kind=kind,
        display_name="CSRF Test",
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=(),
    )


class _SessionAuth:
    def login(
        self,
        request: PasswordAuthRequestV1,
        *,
        request_id: str,
    ) -> SessionIssueV1:
        assert request_id.startswith("request:")
        assert request.login_name == "alice"
        assert request.password.get_secret_value() == "correct-password"
        return SessionIssueV1(
            session_id="session:csrf-test:replacement",
            session_token=SessionToken("replacement-session"),
            csrf_token=SecretText("replacement-csrf"),
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
        assert token.get_secret_value() == "valid-session"
        if request_method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            if csrf_token is None or csrf_token.get_secret_value() != "valid-csrf":
                raise CsrfFailed("private csrf mismatch")
        return ActorContext(
            principal=_principal("human"),
            authentication=AuthenticationContext(
                mechanism="session",
                credential_id="password:csrf-test",
            ),
            session_id="session:csrf-test",
            request_id=request_id,
        )


class _ApiKeyAuth:
    def authenticate(
        self,
        request: ApiKeyAuthRequestV1,
        *,
        request_id: str,
    ) -> ActorContext:
        assert request.api_key.get_secret_value() == "gfk_service.valid"
        return ActorContext(
            principal=_principal("service"),
            authentication=AuthenticationContext(
                mechanism="api_key",
                credential_id="api-key:csrf-test",
            ),
            request_id=request_id,
        )


class _Logout:
    def logout(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText,
        idempotency_key: str,
        request_id: str,
    ) -> object:
        del request_id
        assert token.get_secret_value() == "valid-session"
        if csrf_token.get_secret_value() != "valid-csrf":
            raise CsrfFailed("private csrf mismatch")
        assert idempotency_key == "logout-1"
        return object()


def _app(*, allowed_websocket_origins: frozenset[str] = frozenset()):
    app = create_app(
        ApiDependencies(
            session_authentication=_SessionAuth(),
            api_key_authentication=_ApiKeyAuth(),
            logout_commands=_Logout(),
            allowed_websocket_origins=allowed_websocket_origins,
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

    @app.post("/api/v1/_unsafe-test")
    def unsafe(request: Request) -> dict[str, str]:
        return {"principal_id": request.state.actor.principal.id}

    return app


def test_unsafe_session_request_requires_synchronizer_csrf_token() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "valid-session")
        missing = client.post("/api/v1/_unsafe-test")
        wrong = client.post(
            "/api/v1/_unsafe-test",
            headers={"X-CSRF-Token": "wrong-csrf"},
        )
        accepted = client.post(
            "/api/v1/_unsafe-test",
            headers={"X-CSRF-Token": "valid-csrf"},
        )

    for response in (missing, wrong):
        assert response.status_code == 403
        assert Problem.model_validate(response.json()).code == "csrf_failed"
        assert "private" not in response.text
    assert accepted.status_code == 200
    assert accepted.json()["principal_id"] == "human:csrf-test"


def test_api_key_request_never_gains_browser_session_or_csrf_semantics() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        response = client.post(
            "/api/v1/_unsafe-test",
            headers={"Authorization": "ApiKey gfk_service.valid"},
        )

    assert response.status_code == 200
    assert response.json()["principal_id"] == "service:csrf-test"
    assert "Set-Cookie" not in response.headers


def test_logout_requires_csrf_and_clears_the_browser_cookie() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "valid-session")
        missing = client.post(
            "/api/v1/auth/logout",
            headers={"Idempotency-Key": "logout-1"},
        )
        client.cookies.set("gameforge_session", "valid-session")
        accepted = client.post(
            "/api/v1/auth/logout",
            headers={
                "Idempotency-Key": "logout-1",
                "X-CSRF-Token": "valid-csrf",
            },
        )
        client.cookies.set("gameforge_session", "valid-session")
        replay = client.post(
            "/api/v1/auth/logout",
            headers={
                "Idempotency-Key": "logout-1",
                "X-CSRF-Token": "valid-csrf",
            },
        )

    assert missing.status_code == 403
    assert Problem.model_validate(missing.json()).code == "csrf_failed"
    assert accepted.status_code == 204
    assert replay.status_code == 204
    assert accepted.content == b""
    assert "gameforge_session=" in accepted.headers["Set-Cookie"]
    assert "Max-Age=0" in accepted.headers["Set-Cookie"]
    assert "HttpOnly" in accepted.headers["Set-Cookie"]
    assert "Secure" in accepted.headers["Set-Cookie"]


def test_mixed_session_and_api_key_credentials_fail_closed() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "valid-session")
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "ApiKey gfk_service.valid"},
        )

    assert response.status_code == 401
    assert Problem.model_validate(response.json()).code == "auth_failed"


def test_existing_browser_session_cannot_bypass_csrf_on_ordinary_login() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "valid-session")
        response = client.post(
            "/api/v1/auth/login",
            json={"login_name": "alice", "password": "correct-password"},
        )

    assert response.status_code == 403
    assert Problem.model_validate(response.json()).code == "csrf_failed"


def test_explicit_password_reauthentication_issues_a_fresh_session_without_old_csrf() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "valid-session")
        response = client.post(
            "/api/v1/auth/login",
            headers={"X-GameForge-Reauthentication": "password"},
            json={"login_name": "alice", "password": "correct-password"},
        )

    assert response.status_code == 204
    assert response.headers["X-CSRF-Token"] == "replacement-csrf"
    assert "replacement-session" in response.headers["Set-Cookie"]


def test_password_login_rejects_api_key_authentication_context() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"Authorization": "ApiKey gfk_service.valid"},
            json={"login_name": "alice", "password": "irrelevant"},
        )

    assert response.status_code == 401
    assert Problem.model_validate(response.json()).code == "auth_failed"


def test_websocket_origin_is_checked_before_a_route_can_accept() -> None:
    app = _app(allowed_websocket_origins=frozenset({"https://console.gameforge.test"}))

    @app.websocket("/api/v1/_ws-origin-test")
    async def ws_origin_test(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("accepted")

    with TestClient(app, base_url="https://gameforge.test") as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/api/v1/_ws-origin-test",
                headers={"Origin": "https://evil.invalid"},
            ):
                pass
        with client.websocket_connect(
            "/api/v1/_ws-origin-test",
            headers={"Origin": "https://console.gameforge.test"},
        ) as accepted:
            assert accepted.receive_text() == "accepted"

    assert rejected.value.code == 1008
