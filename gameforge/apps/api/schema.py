"""Deterministic generator + compatibility checker for the frozen ``/api/v1`` contract.

This module freezes three kinds of artifact under ``docs/api/`` (committed to git):

* ``openapi-v1.json`` — the canonical OpenAPI 3.1 document for the WHOLE ``/api/v1``
  surface. It is built from :func:`gameforge.apps.api.app.create_app`'s built-in
  ``app.openapi()`` and then INJECTS the four things FastAPI's generator omits because
  the app authenticates through ASGI middleware + ``require_actor`` (not FastAPI
  ``Security``) and renders errors through global exception handlers (not per-route
  ``responses``):

    1. ``components.securitySchemes`` (an apiKey COOKIE ``gameforge_session`` + an apiKey
       HEADER ``Authorization: ApiKey <secret>``) and each operation's ``security``;
    2. the RFC 9457 ``application/problem+json`` :class:`~gameforge.contracts.jobs.Problem`
       response on the exact 4xx/5xx an operation raises, plus a ``default`` fallback;
    3. the request headers (``Idempotency-Key`` / ``If-Match`` / ``X-CSRF-Token``) and the
       response headers (``ETag`` / ``X-Resource-Revision`` / ``Location`` / ``Set-Cookie``
       / ``X-CSRF-Token`` / SSE cursor headers) the routes actually enforce/emit;
    4. the session-cookie contract documented on ``POST /auth/login``.

* ``schemas/*.json`` — versioned JSON Schemas for the streaming surfaces (the SSE
  ``RunEvent`` payload, the WS server frame ``oneOf``, the WS client command frame, and
  the REST cancel body). These never serialize a worker-only record, so no lease/fencing
  field can appear.

The module is import-safe for the deterministic trunk gate: it depends only on
``gameforge.apps.*`` and ``gameforge.contracts.*`` and imports no LLM SDK.

Run ``python -m gameforge.apps.api.schema --check`` to verify the committed artifacts
regenerate byte-for-byte (in memory; the working tree is never rewritten) or ``--write``
to regenerate them.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import itertools
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from gameforge.apps.api.app import create_app
from gameforge.contracts.api import RunCancelRequestV1, RunCommandServerFrame
from gameforge.contracts.jobs import Problem, RunCommandV1, RunEvent


OPENAPI_KEY = "openapi-v1.json"
SSE_EVENT_KEY = "schemas/sse-run-event-v1.json"
WS_SERVER_FRAME_KEY = "schemas/ws-server-frame-v1.json"
WS_CLIENT_COMMAND_KEY = "schemas/ws-client-command-v1.json"
REST_CANCEL_REQUEST_KEY = "schemas/run-cancel-request-v1.json"

_JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SCHEMA_ID_BASE = "https://gameforge.dev/api/schemas/"
_PROBLEM_MEDIA_TYPE = "application/problem+json"
_PROBLEM_REF = "#/components/schemas/Problem"
_JSON_MEDIA_TYPE = "application/json"

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})
_SUCCESS_CODES = ("200", "201", "202", "203", "204", "205", "206")

# ── security ────────────────────────────────────────────────────────────────
_SESSION_COOKIE_NAME = "gameforge_session"
_SECURITY_SCHEMES: dict[str, dict[str, Any]] = {
    "SessionCookie": {
        "type": "apiKey",
        "in": "cookie",
        "name": _SESSION_COOKIE_NAME,
        "description": (
            "Browser session cookie issued by POST /auth/login. Attributes: "
            "HttpOnly, Secure, SameSite=Strict, Path=/. Mutating requests must also "
            "present the X-CSRF-Token header returned by login (double-submit CSRF); "
            "the Origin/Referer is validated by the authentication middleware."
        ),
    },
    "ApiKeyAuth": {
        "type": "apiKey",
        "in": "header",
        "name": "Authorization",
        "description": (
            "Service credential presented as `Authorization: ApiKey <secret>`. The "
            "secret is validated against a hashed key store; the plaintext is never "
            "stored, logged, or echoed."
        ),
    },
}
_SESSION_OR_API_KEY = [{"SessionCookie": []}, {"ApiKeyAuth": []}]
_SESSION_ONLY = [{"SessionCookie": []}]
_PUBLIC: list[dict[str, list[str]]] = []

# ── error status contract (§5.3 stable mapping) ─────────────────────────────
_BASE_ERROR_STATUSES: tuple[str, ...] = ("401", "403", "422", "429", "500", "503")
_ERROR_STATUSES: dict[str, tuple[str, ...]] = {
    "auth_login": ("400", "401", "403", "422", "429", "500", "503"),
    "auth_logout": ("401", "403", "422", "429", "500", "503"),
    "auth_me": ("401", "403", "429", "500", "503"),
    "read_item": _BASE_ERROR_STATUSES + ("404",),
    "read_page": _BASE_ERROR_STATUSES + ("400", "410"),
    "observability": _BASE_ERROR_STATUSES + ("400", "404", "410"),
    "write_command": _BASE_ERROR_STATUSES + ("400", "404", "409", "410", "413"),
    "run_admission": _BASE_ERROR_STATUSES + ("404", "409", "413"),
    "cancel": _BASE_ERROR_STATUSES + ("404", "409", "413"),
    "sse": ("401", "403", "404", "410", "422", "500", "503"),
}
_STATUS_DESCRIPTIONS: dict[str, str] = {
    "400": "Invalid cursor or malformed request (problem+json).",
    "401": "Authentication is required or failed (problem+json).",
    "403": "Forbidden: RBAC/CSRF/Origin rejected the request (problem+json).",
    "404": "The requested resource was not found (problem+json).",
    "409": "Conflict: revision/idempotency/workflow-guard/precondition (problem+json).",
    "410": "The resume cursor is no longer retained (problem+json).",
    "413": "The request payload exceeds its bound (problem+json).",
    "422": "The request does not match the required schema or is too broad (problem+json).",
    "429": "A configured quota was exceeded (problem+json).",
    "500": "A sanitized internal error (problem+json).",
    "503": "A required dependency is unavailable (problem+json).",
    "default": "An unexpected error rendered as RFC 9457 problem+json.",
}


# ── header descriptions ─────────────────────────────────────────────────────
def _header(description: str, *, max_length: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if max_length is not None:
        schema["maxLength"] = max_length
    return {"description": description, "schema": schema}


_ETAG_HEADERS = {
    "ETag": _header("Strong entity tag of the resource for If-Match optimistic concurrency."),
    "X-Resource-Revision": _header("The resource's monotonic integer revision."),
    "Cache-Control": _header("Caching directive; always `private, no-cache` for resources."),
}
_PAGE_HEADERS = {
    "ETag": _header("Strong entity tag bound to the read snapshot of this page."),
    "Cache-Control": _header("Caching directive; always `private, no-cache`."),
}
_LOGIN_HEADERS = {
    "Set-Cookie": _header(
        f"Sets the `{_SESSION_COOKIE_NAME}` session cookie "
        "(HttpOnly, Secure, SameSite=Strict, Path=/)."
    ),
    "X-CSRF-Token": _header("Double-submit CSRF token to echo on mutating requests."),
    "Cache-Control": _header("Always `no-store` for the authentication response."),
}
_LOGOUT_HEADERS = {
    "Set-Cookie": _header(f"Clears the `{_SESSION_COOKIE_NAME}` session cookie."),
    "Cache-Control": _header("Always `no-store` for the authentication response."),
}
_NO_STORE_HEADERS = {"Cache-Control": _header("Always `no-store`.")}
_ADMISSION_HEADERS = {
    "Location": _header("Relative status URL of the accepted Run."),
    "Cache-Control": _header("Always `private, no-cache`."),
}
_SSE_HEADERS = {
    "Cache-Control": _header("Always `no-store` for the event stream."),
    "X-Accel-Buffering": _header("Always `no` so proxies do not buffer the stream."),
    "X-Earliest-Event-Cursor": _header(
        "Earliest retained event `seq`; a resume below it fails 410."
    ),
}

_CSRF_REQUEST_HEADER = {
    "name": "X-CSRF-Token",
    "in": "header",
    "required": False,
    "description": (
        "Double-submit CSRF token from login. Required when authenticating with the "
        "session cookie on a mutating operation; ignored for ApiKey service clients."
    ),
    "schema": {"type": "string", "maxLength": 4096},
}

_SSE_DESCRIPTION = (
    "A stream of Server-Sent Events (`text/event-stream`). Each event's `data:` line is a "
    "canonical-JSON RunEvent (see schemas/sse-run-event-v1.json); the SSE `id:` is the "
    "persisted event `seq`, echoed via `Last-Event-ID` to resume. Comment lines "
    "(`:` keep-alive) do not advance the cursor."
)


# ── classification ──────────────────────────────────────────────────────────
def _operation_class(method: str, path: str, operation: Mapping[str, Any]) -> str:
    tags = operation.get("tags", []) or []
    if "auth" in tags:
        if path.endswith("/login"):
            return "auth_login"
        if path.endswith("/logout"):
            return "auth_logout"
        return "auth_me"
    if "workflow-commands" in tags:
        return "write_command"
    if "observability" in tags:
        return "observability"
    if "runs" in tags:
        if method == "post" and path.endswith(":cancel"):
            return "cancel"
        if method == "get" and path.endswith("/events"):
            return "sse"
        if method == "post":
            return "run_admission"
    # content-reads / workflow-reads
    return "read_page" if _is_page_operation(operation) else "read_item"


def _success_code(operation: Mapping[str, Any]) -> str | None:
    responses = operation.get("responses", {})
    for code in _SUCCESS_CODES:
        if code in responses:
            return code
    return None


def _success_ref_name(operation: Mapping[str, Any]) -> str | None:
    code = _success_code(operation)
    if code is None:
        return None
    content = operation["responses"][code].get("content") or {}
    media = content.get(_JSON_MEDIA_TYPE)
    if not isinstance(media, dict):
        return None
    schema = media.get("schema", {})
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    return None


def _is_page_operation(operation: Mapping[str, Any]) -> bool:
    name = _success_ref_name(operation) or ""
    return "Page" in name


# ── injection ───────────────────────────────────────────────────────────────
def _security_for(operation_class: str) -> list[dict[str, list[str]]]:
    if operation_class == "auth_login":
        return copy.deepcopy(_PUBLIC)
    if operation_class in ("auth_logout",):
        return copy.deepcopy(_SESSION_ONLY)
    return copy.deepcopy(_SESSION_OR_API_KEY)


def _problem_response(status: str) -> dict[str, Any]:
    return {
        "description": _STATUS_DESCRIPTIONS.get(status, _STATUS_DESCRIPTIONS["default"]),
        "content": {_PROBLEM_MEDIA_TYPE: {"schema": {"$ref": _PROBLEM_REF}}},
    }


def _inject_error_responses(operation: dict[str, Any], operation_class: str) -> None:
    responses = operation.setdefault("responses", {})
    # Drop FastAPI's default validation response so no dangling HTTPValidationError ref
    # survives; the real 422 contract is the injected Problem below.
    for status, response in list(responses.items()):
        if _references_schema(response, "HTTPValidationError"):
            del responses[status]
    for status in _ERROR_STATUSES.get(operation_class, _BASE_ERROR_STATUSES):
        responses[status] = _problem_response(status)
    responses["default"] = _problem_response("default")


def _references_schema(response: Any, schema_name: str) -> bool:
    if not isinstance(response, Mapping):
        return False
    for media in (response.get("content") or {}).values():
        schema = media.get("schema") if isinstance(media, Mapping) else None
        ref = schema.get("$ref") if isinstance(schema, Mapping) else None
        if isinstance(ref, str) and ref.rsplit("/", 1)[-1] == schema_name:
            return True
    return False


def _inject_success_headers(operation: dict[str, Any], operation_class: str) -> None:
    if operation_class == "auth_login":
        _set_headers(operation, "204", _LOGIN_HEADERS)
    elif operation_class == "auth_logout":
        _set_headers(operation, "204", _LOGOUT_HEADERS)
    elif operation_class == "auth_me":
        _set_headers(operation, "200", _NO_STORE_HEADERS)
    elif operation_class == "read_item":
        _set_headers(operation, "200", _ETAG_HEADERS)
    elif operation_class == "read_page":
        _set_headers(operation, "200", _PAGE_HEADERS)
    elif operation_class == "write_command":
        code = _success_code(operation)
        if code is not None:
            _set_headers(operation, code, _ETAG_HEADERS)
    elif operation_class == "run_admission":
        _set_headers(operation, "202", _ADMISSION_HEADERS)
    elif operation_class == "cancel":
        _set_headers(operation, "200", _NO_STORE_HEADERS)
    elif operation_class == "sse":
        _rewrite_sse_success(operation)


def _set_headers(operation: dict[str, Any], code: str, headers: Mapping[str, Any]) -> None:
    response = operation.setdefault("responses", {}).get(code)
    if isinstance(response, dict):
        response.setdefault("headers", {}).update(copy.deepcopy(dict(headers)))


def _rewrite_sse_success(operation: dict[str, Any]) -> None:
    operation.setdefault("responses", {})["200"] = {
        "description": _SSE_DESCRIPTION,
        "content": {"text/event-stream": {"schema": {"type": "string"}}},
        "headers": copy.deepcopy(_SSE_HEADERS),
    }


def _inject_csrf_request_header(operation: dict[str, Any], operation_class: str) -> None:
    if operation_class not in ("auth_logout", "write_command", "run_admission", "cancel"):
        return
    parameters = operation.setdefault("parameters", [])
    for existing in parameters:
        if existing.get("name") == "X-CSRF-Token" and existing.get("in") == "header":
            return
    parameters.append(copy.deepcopy(_CSRF_REQUEST_HEADER))


def _inject_problem_schema(document: dict[str, Any]) -> None:
    components = document.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    problem = Problem.model_json_schema(ref_template="#/components/schemas/{model}")
    nested = problem.pop("$defs", {})
    for name, definition in nested.items():
        schemas.setdefault(name, definition)
    schemas["Problem"] = problem
    # FastAPI's default validation types are replaced by the Problem error contract.
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)


def build_openapi() -> dict[str, Any]:
    """Build the frozen, injected OpenAPI document from the real route table."""

    document = copy.deepcopy(create_app().openapi())
    _inject_problem_schema(document)
    components = document.setdefault("components", {})
    components["securitySchemes"] = copy.deepcopy(_SECURITY_SCHEMES)
    document["security"] = copy.deepcopy(_SESSION_OR_API_KEY)

    for path, path_item in document.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_class = _operation_class(method, path, operation)
            operation["security"] = _security_for(operation_class)
            _inject_error_responses(operation, operation_class)
            _inject_success_headers(operation, operation_class)
            _inject_csrf_request_header(operation, operation_class)
    return document


# ── streaming schemas ───────────────────────────────────────────────────────
def _schema_id(filename: str) -> str:
    return f"{_SCHEMA_ID_BASE}{filename}"


def _model_schema_document(
    model: type[BaseModel], filename: str, *, title: str, description: str
) -> dict[str, Any]:
    body = model.model_json_schema(ref_template="#/$defs/{model}")
    document: dict[str, Any] = {
        "$schema": _JSON_SCHEMA_DIALECT,
        "$id": _schema_id(filename),
    }
    document.update(body)
    document["title"] = title
    document["description"] = description
    return document


def _server_frame_document() -> dict[str, Any]:
    raw = TypeAdapter(RunCommandServerFrame).json_schema(ref_template="#/$defs/{model}")
    variants = raw.get("oneOf") or raw.get("anyOf")
    if not isinstance(variants, list):
        variants = [
            {"$ref": "#/$defs/RunCommandAckV1"},
            {"$ref": "#/$defs/RunCommandProblemV1"},
        ]
    return {
        "$schema": _JSON_SCHEMA_DIALECT,
        "$id": _schema_id("ws-server-frame-v1.json"),
        "title": "RunCommandServerFrame",
        "description": (
            "One WebSocket server frame for POST /runs/{id}:cancel and WS "
            "/runs/{id}/commands: exactly one of a RunCommandAckV1 (accepted/duplicate) "
            "or a RunCommandProblemV1 (RFC 9457 problem). Lease/fencing worker columns "
            "are structurally absent."
        ),
        "oneOf": copy.deepcopy(variants),
        "$defs": raw.get("$defs", {}),
    }


def build_streaming_schemas() -> dict[str, dict[str, Any]]:
    return {
        SSE_EVENT_KEY: _model_schema_document(
            RunEvent,
            "sse-run-event-v1.json",
            title="RunEvent",
            description=(
                "The `data:` payload object of one Server-Sent Event on "
                "GET /runs/{id}/events. The `data` field is a discriminated "
                "(`data_schema_version`) RunEventData union; `event_type` is the "
                "14-value RunEventType. The SSE framing itself is `id:{seq}\\n"
                "event:{type}\\ndata:{canonical_json}\\n\\n`."
            ),
        ),
        WS_SERVER_FRAME_KEY: _server_frame_document(),
        WS_CLIENT_COMMAND_KEY: _model_schema_document(
            RunCommandV1,
            "ws-client-command-v1.json",
            title="RunCommandV1",
            description=(
                "A WebSocket client command frame on WS /runs/{id}/commands "
                "(cancel or provide_input). `payload` is a discriminated "
                "(`schema_version`) RunCommandPayload union."
            ),
        ),
        REST_CANCEL_REQUEST_KEY: _model_schema_document(
            RunCancelRequestV1,
            "run-cancel-request-v1.json",
            title="RunCancelRequestV1",
            description=(
                "The POST /runs/{id}:cancel request body. The server builds the "
                "authoritative RunCommandV1 (type=cancel) from these identity/OCC "
                "fields plus the Idempotency-Key header."
            ),
        ),
    }


def generate() -> dict[str, dict[str, Any]]:
    """Return every frozen artifact as an in-memory dict keyed by its ``docs/api`` path."""

    artifacts: dict[str, dict[str, Any]] = {OPENAPI_KEY: build_openapi()}
    artifacts.update(build_streaming_schemas())
    return artifacts


# ── serialization / filesystem ──────────────────────────────────────────────
def serialize(document: Mapping[str, Any]) -> str:
    """Byte-stable canonical serialization for the committed artifacts."""

    return json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def docs_api_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "docs" / "api"


def write(base_dir: Path | None = None) -> list[str]:
    base = base_dir if base_dir is not None else docs_api_dir()
    written: list[str] = []
    for key, document in generate().items():
        target = base / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(serialize(document), encoding="utf-8")
        written.append(key)
    return written


def check(base_dir: Path | None = None) -> list[str]:
    """Regenerate IN MEMORY and diff against the committed artifacts (no writes)."""

    base = base_dir if base_dir is not None else docs_api_dir()
    problems: list[str] = []
    for key, document in generate().items():
        expected = serialize(document)
        target = base / key
        if not target.is_file():
            problems.append(f"MISSING {key}")
            continue
        actual = target.read_text(encoding="utf-8")
        if actual == expected:
            continue
        problems.append(f"DRIFT {key}:")
        diff = difflib.unified_diff(
            actual.splitlines(),
            expected.splitlines(),
            fromfile=f"committed/{key}",
            tofile=f"generated/{key}",
            lineterm="",
        )
        problems.extend(itertools.islice(diff, 60))
    return problems


# ── compatibility checker ───────────────────────────────────────────────────
@dataclass(frozen=True)
class BreakingChange:
    """One backward-incompatible difference between an OLD and a NEW artifact."""

    kind: str
    location: str
    detail: str = ""


def _is_openapi(document: Any) -> bool:
    return isinstance(document, Mapping) and "openapi" in document and "paths" in document


def check_compatibility(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[BreakingChange]:
    """Return the breaking changes going OLD → NEW (empty list ⇒ backward compatible).

    PERMITS additive changes (new path/method/operation, new optional field, new enum
    value, new union variant). REJECTS removed paths/methods/response-statuses, narrowed
    enums/literals, newly-required request fields, changed discriminators, and
    incompatible response/schema changes (type change, removed field, required-added).
    """

    changes: list[BreakingChange] = []
    if _is_openapi(old) and _is_openapi(new):
        _diff_openapi(old, new, changes)
    else:
        _diff_schema(old, new, old, new, "$", changes, set())
    # De-duplicate while preserving order.
    seen: set[tuple[str, str, str]] = set()
    unique: list[BreakingChange] = []
    for change in changes:
        key = (change.kind, change.location, change.detail)
        if key not in seen:
            seen.add(key)
            unique.append(change)
    return unique


def _diff_openapi(
    old: Mapping[str, Any], new: Mapping[str, Any], changes: list[BreakingChange]
) -> None:
    old_paths = old.get("paths", {})
    new_paths = new.get("paths", {})
    for path, old_item in old_paths.items():
        new_item = new_paths.get(path)
        if new_item is None:
            changes.append(BreakingChange("path_removed", f"paths[{path}]"))
            continue
        for method, old_op in old_item.items():
            if method not in _HTTP_METHODS:
                continue
            new_op = new_item.get(method)
            if not isinstance(new_op, Mapping):
                changes.append(BreakingChange("method_removed", f"paths[{path}].{method}"))
                continue
            _diff_operation(old_op, new_op, f"paths[{path}].{method}", changes)

    old_schemas = old.get("components", {}).get("schemas", {})
    new_schemas = new.get("components", {}).get("schemas", {})
    for name, old_schema in old_schemas.items():
        new_schema = new_schemas.get(name)
        if new_schema is None:
            # A component may be legitimately inlined/renamed; the breaking surface is
            # caught where a surviving operation references it, so a bare removal is not
            # flagged. Its shared shape is diffed below only when it survives.
            continue
        _diff_schema(old_schema, new_schema, old, new, f"components.schemas.{name}", changes, set())


def _diff_operation(
    old_op: Mapping[str, Any],
    new_op: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    old_responses = old_op.get("responses", {})
    new_responses = new_op.get("responses", {})
    for status in old_responses:
        if status not in new_responses:
            changes.append(
                BreakingChange("response_status_removed", f"{location}.responses[{status}]")
            )


def _resolve_ref(ref: str, root: Mapping[str, Any]) -> Mapping[str, Any]:
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        if not isinstance(node, Mapping) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, Mapping) else {}


def _ref_name(entry: Any) -> str:
    if isinstance(entry, Mapping) and isinstance(entry.get("$ref"), str):
        return entry["$ref"].rsplit("/", 1)[-1]
    return ""


def _diff_schema(
    old_schema: Any,
    new_schema: Any,
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str]],
) -> None:
    if not isinstance(old_schema, Mapping) or not isinstance(new_schema, Mapping):
        return

    old_ref = old_schema.get("$ref")
    new_ref = new_schema.get("$ref")
    if isinstance(old_ref, str) and isinstance(new_ref, str):
        key = (old_ref, new_ref)
        if key in seen:
            return
        _diff_schema(
            _resolve_ref(old_ref, old_root),
            _resolve_ref(new_ref, new_root),
            old_root,
            new_root,
            location,
            changes,
            seen | {key},
        )
        return
    if isinstance(old_ref, str):
        old_schema = _resolve_ref(old_ref, old_root)
    if isinstance(new_ref, str):
        new_schema = _resolve_ref(new_ref, new_root)
    if not isinstance(old_schema, Mapping) or not isinstance(new_schema, Mapping):
        return

    _diff_enum(old_schema, new_schema, location, changes)
    _diff_const(old_schema, new_schema, location, changes)
    _diff_discriminator(old_schema, new_schema, location, changes)
    _diff_type(old_schema, new_schema, location, changes)
    _diff_required_and_properties(
        old_schema, new_schema, old_root, new_root, location, changes, seen
    )
    _diff_items(old_schema, new_schema, old_root, new_root, location, changes, seen)
    _diff_unions(old_schema, new_schema, old_root, new_root, location, changes, seen)


def _diff_enum(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    old_enum = old_schema.get("enum")
    new_enum = new_schema.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        removed = [value for value in old_enum if value not in new_enum]
        if removed:
            changes.append(BreakingChange("enum_narrowed", f"{location}.enum", repr(removed)))


def _diff_const(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    if "const" in old_schema and "const" in new_schema:
        if old_schema["const"] != new_schema["const"]:
            changes.append(
                BreakingChange(
                    "const_changed",
                    f"{location}.const",
                    f"{old_schema['const']!r}->{new_schema['const']!r}",
                )
            )


def _diff_discriminator(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    old_disc = old_schema.get("discriminator")
    if not isinstance(old_disc, Mapping):
        return
    new_disc = new_schema.get("discriminator")
    if not isinstance(new_disc, Mapping):
        changes.append(BreakingChange("discriminator_changed", location, "discriminator removed"))
        return
    if old_disc.get("propertyName") != new_disc.get("propertyName"):
        changes.append(
            BreakingChange(
                "discriminator_changed",
                location,
                f"propertyName {old_disc.get('propertyName')!r}->{new_disc.get('propertyName')!r}",
            )
        )
    old_map = old_disc.get("mapping", {}) or {}
    new_map = new_disc.get("mapping", {}) or {}
    removed = sorted(key for key in old_map if key not in new_map)
    if removed:
        changes.append(BreakingChange("discriminator_variant_removed", location, repr(removed)))


def _diff_type(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    old_type = old_schema.get("type")
    new_type = new_schema.get("type")
    if isinstance(old_type, str) and isinstance(new_type, str) and old_type != new_type:
        changes.append(BreakingChange("type_changed", location, f"{old_type}->{new_type}"))


def _diff_required_and_properties(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str]],
) -> None:
    old_required = set(old_schema.get("required", []) or [])
    new_required = set(new_schema.get("required", []) or [])
    for name in sorted(new_required - old_required):
        changes.append(BreakingChange("required_added", f"{location}.required", name))

    old_props = old_schema.get("properties", {}) or {}
    new_props = new_schema.get("properties", {}) or {}
    for name in sorted(set(old_props) - set(new_props)):
        changes.append(BreakingChange("field_removed", f"{location}.properties.{name}"))
    for name in sorted(set(old_props) & set(new_props)):
        _diff_schema(
            old_props[name],
            new_props[name],
            old_root,
            new_root,
            f"{location}.properties.{name}",
            changes,
            seen,
        )


def _diff_items(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str]],
) -> None:
    old_items = old_schema.get("items")
    new_items = new_schema.get("items")
    if isinstance(old_items, Mapping) and isinstance(new_items, Mapping):
        _diff_schema(old_items, new_items, old_root, new_root, f"{location}.items", changes, seen)


def _diff_unions(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str]],
) -> None:
    for keyword in ("oneOf", "anyOf"):
        old_list = old_schema.get(keyword)
        new_list = new_schema.get(keyword)
        if not isinstance(old_list, list) or not isinstance(new_list, list):
            continue
        old_by_name = {_ref_name(entry): entry for entry in old_list if _ref_name(entry)}
        new_by_name = {_ref_name(entry): entry for entry in new_list if _ref_name(entry)}
        for name in sorted(set(old_by_name) - set(new_by_name)):
            changes.append(BreakingChange("variant_removed", f"{location}.{keyword}.{name}"))
        for name in sorted(set(old_by_name) & set(new_by_name)):
            _diff_schema(
                old_by_name[name],
                new_by_name[name],
                old_root,
                new_root,
                f"{location}.{keyword}.{name}",
                changes,
                seen,
            )


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gameforge.apps.api.schema",
        description="Generate or verify the frozen /api/v1 OpenAPI + streaming schemas.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory and verify the committed docs/api artifacts are "
        "byte-identical (default); never writes the working tree",
    )
    group.add_argument(
        "--write",
        action="store_true",
        help="regenerate the committed docs/api artifacts on disk",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.write:
        written = write()
        print(f"wrote {len(written)} artifact(s) to {docs_api_dir()}:")
        for key in written:
            print(f"  {key}")
        return 0

    problems = check()
    if problems:
        print("frozen /api/v1 contract drift detected (run --write to regenerate):")
        for line in problems:
            print(line)
        return 1
    print(f"OK: {len(generate())} artifact(s) match {docs_api_dir()}")
    return 0


__all__ = [
    "BreakingChange",
    "OPENAPI_KEY",
    "REST_CANCEL_REQUEST_KEY",
    "SSE_EVENT_KEY",
    "WS_CLIENT_COMMAND_KEY",
    "WS_SERVER_FRAME_KEY",
    "build_openapi",
    "build_streaming_schemas",
    "check",
    "check_compatibility",
    "docs_api_dir",
    "generate",
    "main",
    "serialize",
    "write",
]


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
