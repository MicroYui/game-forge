"""Local session and API-key authentication endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response, status

from gameforge.apps.api.dependencies import (
    ApiDependencies,
    api_dependencies,
    require_actor,
)
from gameforge.contracts.auth import PasswordAuthRequestV1, SecretText, SessionToken
from gameforge.contracts.errors import (
    AuthRequired,
    CsrfFailed,
    DependencyUnavailable,
    IntegrityViolation,
    RequestSchemaInvalid,
)
from gameforge.contracts.identity import ActorContext, Principal


SESSION_COOKIE_NAME = "gameforge_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
IDEMPOTENCY_HEADER_NAME = "Idempotency-Key"


def _parse_utc(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise IntegrityViolation("session service returned an invalid expiry") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise IntegrityViolation("session service returned a non-UTC expiry")
    return parsed


def auth_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

    @router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
    def login(
        payload: PasswordAuthRequestV1,
        request: Request,
        response: Response,
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> None:
        service = dependencies.session_authentication
        if service is None:
            raise DependencyUnavailable("session authentication is not configured")
        issue = service.login(payload, request_id=request.state.request_id)
        response.set_cookie(
            key=dependencies.session_cookie.name,
            value=issue.session_token.get_secret_value(),
            expires=_parse_utc(issue.absolute_expires_at),
            path=dependencies.session_cookie.path,
            secure=dependencies.session_cookie.secure,
            httponly=dependencies.session_cookie.http_only,
            samesite=dependencies.session_cookie.same_site,
        )
        response.headers[CSRF_HEADER_NAME] = issue.csrf_token.get_secret_value()
        response.headers["Cache-Control"] = "no-store"

    @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        request: Request,
        response: Response,
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> None:
        service = dependencies.logout_commands
        if service is None:
            raise DependencyUnavailable("logout command service is not configured")
        token = getattr(request.state, "session_token", None)
        if not isinstance(token, SessionToken):
            raise AuthRequired("browser session is required")
        csrf_value = request.headers.get(CSRF_HEADER_NAME)
        if csrf_value is None or not csrf_value:
            raise CsrfFailed("CSRF token is required")
        idempotency_key = request.headers.get(IDEMPOTENCY_HEADER_NAME)
        if idempotency_key is None or not idempotency_key or len(idempotency_key) > 512:
            raise RequestSchemaInvalid("Idempotency-Key is required and bounded")
        service.logout(
            token,
            csrf_token=SecretText(csrf_value),
            idempotency_key=idempotency_key,
            request_id=request.state.request_id,
        )
        response.delete_cookie(
            key=dependencies.session_cookie.name,
            path=dependencies.session_cookie.path,
            secure=dependencies.session_cookie.secure,
            httponly=dependencies.session_cookie.http_only,
            samesite=dependencies.session_cookie.same_site,
        )
        response.headers["Cache-Control"] = "no-store"

    @router.get("/me", response_model=Principal)
    def me(
        response: Response,
        actor: ActorContext = Depends(require_actor),
    ) -> Principal:
        response.headers["Cache-Control"] = "no-store"
        return actor.principal

    return router


__all__ = [
    "CSRF_HEADER_NAME",
    "IDEMPOTENCY_HEADER_NAME",
    "SESSION_COOKIE_NAME",
    "auth_router",
]
