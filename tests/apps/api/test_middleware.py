from __future__ import annotations

import asyncio
from collections.abc import Iterator
import json

from fastapi import Depends, Request
from fastapi.testclient import TestClient
import pytest

from gameforge.apps.api import middleware as api_middleware
from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies
from gameforge.apps.api.health import ReadinessChecks, ReadinessService
from gameforge.contracts.auth import SecretText, SessionToken
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    Principal,
)
from gameforge.runtime.observability import InMemoryExporter, Tracer
from gameforge.runtime.observability.context import current_trace_context


def _noop() -> None:
    return None


class _Ids:
    def __init__(self) -> None:
        self._trace = 0
        self._span = 0

    def new_trace_id(self) -> str:
        self._trace += 1
        return f"{self._trace:032x}"

    def new_span_id(self) -> str:
        self._span += 1
        return f"{self._span:016x}"


class _RequestIds:
    def __init__(self) -> None:
        self._ordinal = 0

    def __call__(self) -> str:
        self._ordinal += 1
        return f"request:server:{self._ordinal}"


class _SessionAuth:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.request_ids: list[str] = []

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
        request_id: str,
    ) -> ActorContext:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("synchronous authentication must not block the event loop")
        assert token.get_secret_value() == "session-secret"
        assert csrf_token is None
        assert request_method == "GET"
        self.events.append("authn")
        self.request_ids.append(request_id)
        return _human_actor(request_id=request_id)


def _human_actor(*, request_id: str) -> ActorContext:
    return ActorContext(
        principal=Principal(
            id="human:alice",
            kind="human",
            display_name="Alice",
            status="active",
            revision=1,
            credential_epoch=1,
            authz_revision=1,
            roles=(),
        ),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="password:alice",
        ),
        session_id="session:alice:1",
        request_id=request_id,
    )


def _ready() -> ReadinessService:
    return ReadinessService(
        ReadinessChecks(
            migration_head=_noop,
            database=_noop,
            object_store=_noop,
            cost_ledger=_noop,
            registry=_noop,
            slo_retention=_noop,
            audit_cache=_noop,
        )
    )


def test_actual_request_context_authn_dependency_and_handler_order() -> None:
    events: list[str] = []
    session_auth = _SessionAuth(events)
    exporter = InMemoryExporter(capacity=10)
    app = create_app(
        ApiDependencies(
            session_authentication=session_auth,
            tracer=Tracer(exporter=exporter, id_generator=_Ids()),
            request_id_factory=_RequestIds(),
            readiness=_ready(),
        )
    )

    def resource_authz(request: Request) -> None:
        events.append("authz")
        assert request.state.actor.principal.id == "human:alice"
        assert current_trace_context() is not None

    @app.get("/api/v1/_middleware-order", dependencies=[Depends(resource_authz)])
    def handler(request: Request) -> dict[str, str]:
        events.append("handler")
        context = current_trace_context()
        assert context is not None
        return {
            "request_id": request.state.request_id,
            "trace_id": context.trace_id,
        }

    with TestClient(app, base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "session-secret")
        response = client.get(
            "/api/v1/_middleware-order",
            headers={
                "X-Request-ID": "client-controlled-request",
                "X-Trace-ID": "f" * 32,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "request:server:1",
        "trace_id": "00000000000000000000000000000001",
    }
    assert response.headers["X-Request-ID"] == "request:server:1"
    assert response.headers["X-Trace-ID"] == "00000000000000000000000000000001"
    assert session_auth.request_ids == ["request:server:1"]
    assert events == ["authn", "authz", "handler"]
    assert len(exporter.spans) == 1
    assert exporter.spans[0].name == "http.request"
    assert exporter.spans[0].attributes["http.response.status_code"] == 200
    assert exporter.spans[0].status == "ok"


def test_problem_wrapper_is_outside_authentication_middleware() -> None:
    class BrokenSessionAuth:
        def resolve(self, *args: object, **kwargs: object) -> ActorContext:
            raise RuntimeError("private authentication implementation detail")

    exporter = InMemoryExporter(capacity=10)
    app = create_app(
        ApiDependencies(
            session_authentication=BrokenSessionAuth(),
            tracer=Tracer(exporter=exporter, id_generator=_Ids()),
            request_id_factory=_RequestIds(),
            readiness=_ready(),
        )
    )

    @app.get("/api/v1/protected")
    def protected() -> dict[str, bool]:
        raise AssertionError("handler must not run")

    with TestClient(app, base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "session-secret")
        response = client.get("/api/v1/protected")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert "private authentication implementation detail" not in response.text
    assert response.json()["request_id"] == "request:server:1"
    assert exporter.spans[0].attributes["http.response.status_code"] == 500
    assert exporter.spans[0].status == "error"


def test_client_error_span_retains_status_without_blame_as_server_error() -> None:
    exporter = InMemoryExporter(capacity=10)
    app = create_app(
        ApiDependencies(
            tracer=Tracer(exporter=exporter, id_generator=_Ids()),
            request_id_factory=_RequestIds(),
            readiness=_ready(),
        )
    )

    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert exporter.spans[0].attributes["http.response.status_code"] == 404
    assert exporter.spans[0].status == "unset"


def test_http_request_metric_is_emitted_after_response_and_is_best_effort() -> None:
    class Metrics:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def record_api_request(self, *, method: str, status_code: int) -> None:
            self.calls.append((method, status_code))
            raise OSError("metric exporter unavailable")

    metrics = Metrics()
    app = create_app(
        ApiDependencies(
            tracer=Tracer(exporter=InMemoryExporter(capacity=10), id_generator=_Ids()),
            request_id_factory=_RequestIds(),
            readiness=_ready(),
            operational_metrics=metrics,
        )
    )

    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert metrics.calls == [("GET", 404)]


def test_http_middleware_contains_no_cost_governor() -> None:
    app = create_app(
        ApiDependencies(
            tracer=Tracer(exporter=InMemoryExporter(capacity=10), id_generator=_Ids()),
            request_id_factory=_RequestIds(),
            readiness=_ready(),
        )
    )

    middleware_names = tuple(item.cls.__name__ for item in app.user_middleware)

    assert middleware_names == (
        "RequestContextMiddleware",
        "ProblemMiddleware",
        "RequestBodyLimitMiddleware",
        "AuthenticationMiddleware",
    )
    assert all("cost" not in name.lower() for name in middleware_names)


def test_declared_oversized_body_is_rejected_before_downstream() -> None:
    entered = False

    async def endpoint(scope: object, receive: object, send: object) -> None:
        nonlocal entered
        entered = True

    async def receive() -> dict[str, object]:
        raise AssertionError("declared oversized body must fail before receive")

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    app = api_middleware.ProblemMiddleware(
        api_middleware.RequestBodyLimitMiddleware(endpoint, max_body_bytes=8)
    )
    scope = _http_scope(headers=[(b"content-length", b"9")])
    asyncio.run(app(scope, receive, send))

    assert entered is False
    _assert_payload_too_large(sent)


@pytest.mark.parametrize("headers", ([], [(b"content-length", b"1")]))
def test_streamed_body_limit_cannot_be_bypassed(
    headers: list[tuple[bytes, bytes]],
) -> None:
    completed = False
    messages = [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"6789", "more_body": False},
    ]

    async def endpoint(scope: object, receive: object, send: object) -> None:
        nonlocal completed
        while True:
            message = await receive()  # type: ignore[operator]
            if not message.get("more_body", False):
                break
        completed = True

    async def receive() -> dict[str, object]:
        return messages.pop(0)

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    app = api_middleware.ProblemMiddleware(
        api_middleware.RequestBodyLimitMiddleware(endpoint, max_body_bytes=8)
    )
    asyncio.run(app(_http_scope(headers=headers), receive, send))

    assert completed is False
    _assert_payload_too_large(sent)


@pytest.mark.parametrize("declared_length", (None, "1"))
def test_composed_body_limit_keeps_streamed_overflow_as_413(
    declared_length: str | None,
) -> None:
    app = create_app(ApiDependencies(request_id_factory=lambda: "request:body-limit"))
    headers = {"Content-Type": "application/json"}
    if declared_length is not None:
        headers["Content-Length"] = declared_length

    def oversized_body() -> Iterator[bytes]:
        yield b'{"password":"'
        yield b"x" * (api_middleware.MAX_HTTP_REQUEST_BODY_BYTES + 1)
        yield b'"}'

    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.post(
            "/api/v1/auth/login",
            content=oversized_body(),
            headers=headers,
        )

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
    assert response.headers["X-Request-ID"] == "request:body-limit"


def _http_scope(*, headers: list[tuple[bytes, bytes]]) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/v1/test",
        "raw_path": b"/api/v1/test",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("gameforge.test", 443),
        "state": {"request_id": "request:body-limit"},
    }


def _assert_payload_too_large(sent: list[dict[str, object]]) -> None:
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = next(message for message in sent if message["type"] == "http.response.body")
    assert start["status"] == 413
    payload = json.loads(body["body"])
    assert payload["code"] == "payload_too_large"
    assert payload["request_id"] == "request:body-limit"
