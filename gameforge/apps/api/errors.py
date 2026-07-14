"""RFC 9457 error mapping for both framework and platform failures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gameforge.contracts.errors import (
    AuthError,
    AuthRequired,
    Conflict,
    CsrfFailed,
    CursorExpired,
    CursorInvalid,
    DependencyUnavailable,
    Forbidden,
    GameForgeError,
    IntegrityViolation,
    NotFound,
    PayloadTooLarge,
    QueryTooBroad,
    QuotaExceeded,
    RequestSchemaInvalid,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.jobs import MAX_COLLECTION_ITEMS, MAX_JSON_BYTES, Problem


_PROBLEM_MEDIA_TYPE = "application/problem+json"
_SAFE_PROTOCOL_HEADERS = frozenset({"allow", "retry-after", "www-authenticate"})


def _state_value(scope: Mapping[str, Any], name: str) -> str | None:
    state = scope.get("state")
    if not isinstance(state, Mapping):
        return None
    value = state.get(name)
    return value if isinstance(value, str) and value else None


def _mapping(error: BaseException) -> tuple[int, str, str, str]:
    if isinstance(error, CursorInvalid):
        return 400, "invalid_cursor", "Invalid cursor", "The cursor is invalid."
    if isinstance(error, AuthRequired):
        return 401, "auth_required", "Authentication required", "Authentication is required."
    if isinstance(error, CsrfFailed):
        return 403, "csrf_failed", "CSRF validation failed", "CSRF validation failed."
    if isinstance(error, AuthError):
        return 401, "auth_failed", "Authentication failed", "Authentication failed."
    if isinstance(error, Forbidden):
        return 403, error.code, "Forbidden", "The operation is not permitted."
    if isinstance(error, NotFound):
        return 404, "not_found", "Not found", "The requested resource was not found."
    if isinstance(error, CursorExpired):
        return 410, "cursor_expired", "Cursor expired", "The cursor has expired."
    if isinstance(error, Conflict):
        return 409, error.code, "Conflict", "The request conflicts with current state."
    if isinstance(error, QueryTooBroad):
        return 422, "query_too_broad", "Query too broad", "The query exceeds its bounds."
    if isinstance(error, PayloadTooLarge):
        return 413, "payload_too_large", "Payload too large", "The request payload is too large."
    if isinstance(error, RequestSchemaInvalid):
        return (
            422,
            "request_schema_invalid",
            "Request schema invalid",
            "The request does not match the required schema.",
        )
    if isinstance(error, QuotaExceeded):
        return 429, "quota_exceeded", "Quota exceeded", "The configured quota was exceeded."
    if isinstance(error, DependencyUnavailable):
        return (
            503,
            "dependency_unavailable",
            "Dependency unavailable",
            "A required dependency is unavailable.",
        )
    if isinstance(error, IntegrityViolation):
        return (
            500,
            "integrity_violation",
            "Integrity violation",
            "An internal integrity check failed.",
        )
    if isinstance(error, GameForgeError):
        return 500, error.code, "Platform error", "An internal platform error occurred."
    return 500, "internal_error", "Internal error", "An internal error occurred."


def _safe_context(error: BaseException) -> tuple[dict[str, str], ...] | None:
    if isinstance(error, DependencyUnavailable):
        component = error.context.get("component")
        if isinstance(component, str) and 0 < len(component) <= 512:
            return ({"component": component},)
    return None


def _problem(
    scope: Mapping[str, Any],
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    errors: tuple[dict[str, Any], ...] | None = None,
) -> Problem:
    request_id = _state_value(scope, "request_id") or "request:unavailable"
    trace_id = _state_value(scope, "trace_id")
    return Problem(
        type=f"urn:gameforge:problem:{code}",
        title=title,
        status=status,
        detail=detail,
        instance=f"urn:gameforge:request:{request_id}",
        code=code,
        request_id=request_id,
        trace_id=trace_id,
        errors=errors,
    )


def problem_response(scope: Mapping[str, Any], error: BaseException) -> JSONResponse:
    status, code, title, detail = _mapping(error)
    problem = _problem(
        scope,
        status=status,
        code=code,
        title=title,
        detail=detail,
        errors=_safe_context(error),
    )
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type=_PROBLEM_MEDIA_TYPE,
    )


def _framework_mapping(status: int) -> tuple[str, str, str]:
    by_status = {
        400: ("bad_request", "Bad request", "The request is invalid."),
        401: ("auth_required", "Authentication required", "Authentication is required."),
        403: ("forbidden", "Forbidden", "The operation is not permitted."),
        404: ("not_found", "Not found", "The requested resource was not found."),
        405: (
            "method_not_allowed",
            "Method not allowed",
            "The request method is not allowed for this resource.",
        ),
        413: ("payload_too_large", "Payload too large", "The request payload is too large."),
    }
    return by_status.get(
        status,
        ("http_error", "HTTP error", "The request could not be completed."),
    )


async def platform_error_handler(request: Request, error: GameForgeError) -> JSONResponse:
    return problem_response(request.scope, error)


async def http_error_handler(
    request: Request,
    error: StarletteHTTPException,
) -> JSONResponse:
    code, title, detail = _framework_mapping(error.status_code)
    problem = _problem(
        request.scope,
        status=error.status_code,
        code=code,
        title=title,
        detail=detail,
    )
    headers: dict[str, str] = {}
    for name, value in (error.headers or {}).items():
        if (
            isinstance(name, str)
            and name.lower() in _SAFE_PROTOCOL_HEADERS
            and isinstance(value, str)
            and len(value) <= 4096
            and "\r" not in value
            and "\n" not in value
        ):
            headers[name] = value
    return JSONResponse(
        status_code=error.status_code,
        content=problem.model_dump(mode="json", exclude_none=True),
        headers=headers,
        media_type=_PROBLEM_MEDIA_TYPE,
    )


async def validation_error_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    sanitized: list[dict[str, Any]] = []
    for item in error.errors()[:MAX_COLLECTION_ITEMS]:
        candidate = {
            "loc": [str(value)[:128] for value in item.get("loc", ())[:8]],
            "msg": str(item.get("msg", "Invalid value."))[:512],
            "type": str(item.get("type", "value_error"))[:256],
        }
        if len(canonical_json([*sanitized, candidate]).encode("utf-8")) > MAX_JSON_BYTES:
            break
        sanitized.append(candidate)
    problem = _problem(
        request.scope,
        status=422,
        code="request_schema_invalid",
        title="Request schema invalid",
        detail="The request does not match the required schema.",
        errors=tuple(sanitized),
    )
    return JSONResponse(
        status_code=422,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type=_PROBLEM_MEDIA_TYPE,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(GameForgeError, platform_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]


__all__ = ["install_error_handlers", "problem_response"]
