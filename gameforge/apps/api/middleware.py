"""Ordered ASGI middleware for request context, errors, and authentication."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gameforge.apps.api.dependencies import ApiDependencies
from gameforge.apps.api.errors import problem_response
from gameforge.contracts.auth import ApiKeyAuthRequestV1, ApiKeySecret, SecretText, SessionToken
from gameforge.contracts.errors import (
    AuthError,
    AuthFailed,
    CsrfFailed,
    DependencyUnavailable,
    IntegrityViolation,
    PayloadTooLarge,
)


_HEALTH_PATHS = frozenset({"/livez", "/readyz"})
_LOGIN_PATH = "/api/v1/auth/login"
_LOGOUT_PATH = "/api/v1/auth/logout"
MAX_HTTP_REQUEST_BODY_BYTES = 8 * 1024 * 1024


def _state(scope: Scope) -> MutableMapping[str, Any]:
    state = scope.setdefault("state", {})
    if not isinstance(state, MutableMapping):
        raise RuntimeError("ASGI state must be mutable")
    return state


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, dependencies: ApiDependencies) -> None:
        self._app = app
        self._dependencies = dependencies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if scope.get("path") == "/livez":
            await self._app(scope, receive, send)
            return
        request_id = self._dependencies.request_id_factory()
        if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
            request_id = "request:unavailable"
        state = _state(scope)
        state["request_id"] = request_id
        metric_recorded = False

        with self._dependencies.tracer.span(
            "http.request",
            attributes={
                "http.request.method": str(scope.get("method", ""))[:32],
                "url.path": str(scope.get("path", ""))[:2048],
            },
        ) as span:
            context = span.context
            trace_id = None if context is None else context.trace_id
            state["trace_id"] = trace_id

            async def send_with_context(message: Message) -> None:
                nonlocal metric_recorded
                if message["type"] == "http.response.start":
                    raw_status = message.get("status")
                    response_status = (
                        raw_status
                        if isinstance(raw_status, int)
                        and not isinstance(raw_status, bool)
                        and 100 <= raw_status <= 599
                        else 500
                    )
                    span.set_attribute("http.response.status_code", response_status)
                    if response_status >= 500:
                        span.set_status("error")
                    elif response_status < 400:
                        span.set_status("ok")
                    headers = MutableHeaders(scope=message)
                    headers["X-Request-ID"] = request_id
                    if trace_id is not None:
                        headers["X-Trace-ID"] = trace_id
                    if not metric_recorded and self._dependencies.operational_metrics is not None:
                        metric_recorded = True
                        try:
                            self._dependencies.operational_metrics.record_api_request(
                                method=str(scope.get("method", "")),
                                status_code=response_status,
                            )
                        except Exception:
                            # Metric export is best-effort and cannot alter the
                            # already-selected authoritative HTTP result.
                            pass
                await send(message)

            await self._app(scope, receive, send_with_context)


class ProblemMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        try:
            await self._app(scope, receive, send)
        except Exception as error:
            response = problem_response(scope, error)
            await response(scope, receive, send)


class _RequestBodyLimitExceeded(BaseException):
    """Escape Starlette's body-parser ``except Exception`` and translate immediately."""


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared, 10) > self._max_body_bytes:
                    raise PayloadTooLarge("HTTP request body exceeds its wire-size bound")
            except ValueError:
                pass

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body_bytes:
                    raise _RequestBodyLimitExceeded
            return message

        try:
            await self._app(scope, limited_receive, send)
        except _RequestBodyLimitExceeded:
            raise PayloadTooLarge("HTTP request body exceeds its wire-size bound") from None


class AuthenticationMiddleware:
    def __init__(self, app: ASGIApp, *, dependencies: ApiDependencies) -> None:
        self._app = app
        self._dependencies = dependencies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            origin = Headers(scope=scope).get("origin")
            if origin not in self._dependencies.allowed_websocket_origins:
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "origin_rejected",
                    }
                )
                return
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http" or scope.get("path") in _HEALTH_PATHS:
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        cookies = _parse_cookies(headers.get("cookie"))
        session_value = cookies.get(self._dependencies.session_cookie.name)
        authorization = headers.get("authorization")
        if session_value is not None and authorization is not None:
            raise AuthFailed("multiple HTTP credential mechanisms are forbidden")
        if scope.get("path") == _LOGIN_PATH:
            if authorization is not None:
                raise AuthFailed("password login does not accept API-key credentials")
            if session_value is None:
                await self._app(scope, receive, send)
                return

        state = _state(scope)
        request_id = state.get("request_id")
        if not isinstance(request_id, str):
            raise IntegrityViolation("request context is unavailable before authentication")

        if session_value is not None:
            token = SessionToken(session_value)
            state["session_token"] = token
            if scope.get("path") != _LOGOUT_PATH:
                service = self._dependencies.session_authentication
                if service is None:
                    raise DependencyUnavailable("session authentication is not configured")
                csrf_value = headers.get("x-csrf-token")
                try:
                    actor = await run_in_threadpool(
                        service.resolve,
                        token,
                        csrf_token=(None if csrf_value is None else SecretText(csrf_value)),
                        request_method=str(scope.get("method", "GET")),
                        request_id=request_id,
                    )
                except CsrfFailed:
                    raise
                except AuthError:
                    if scope.get("path") != _LOGIN_PATH:
                        raise
                    state.pop("session_token", None)
                    await self._app(scope, receive, send)
                    return
                if actor.principal.kind != "human" or actor.authentication.mechanism != "session":
                    raise IntegrityViolation(
                        "HTTP session authentication returned a non-human actor"
                    )
                state["actor"] = actor
        elif authorization is not None:
            scheme, separator, secret = authorization.partition(" ")
            if (
                scheme != "ApiKey"
                or not separator
                or not secret
                or len(secret) > 4096
                or " " in secret
            ):
                raise AuthFailed("API-key authorization header is invalid")
            service = self._dependencies.api_key_authentication
            if service is None:
                raise DependencyUnavailable("API-key authentication is not configured")
            actor = await run_in_threadpool(
                service.authenticate,
                ApiKeyAuthRequestV1(api_key=ApiKeySecret(secret)),
                request_id=request_id,
            )
            if actor.principal.kind != "service" or actor.authentication.mechanism != "api_key":
                raise IntegrityViolation("HTTP API-key authentication returned a non-service actor")
            state["actor"] = actor

        await self._app(scope, receive, send)


def _parse_cookies(value: str | None) -> dict[str, str]:
    if value is None:
        return {}
    parsed: dict[str, str] = {}
    for item in value.split(";"):
        name, separator, content = item.strip().partition("=")
        if separator and name and name not in parsed:
            parsed[name] = content
    return parsed


__all__ = [
    "AuthenticationMiddleware",
    "MAX_HTTP_REQUEST_BODY_BYTES",
    "ProblemMiddleware",
    "RequestBodyLimitMiddleware",
    "RequestContextMiddleware",
]
