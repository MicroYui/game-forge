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
  ``RunEvent`` payload, the WS server frame ``oneOf``, and the full ``RunCommandV1``
  client command shared by WS and REST cancel). These never serialize a worker-only
  record, so no lease/fencing field can appear.

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
from typing import Any, Literal

from pydantic import BaseModel, TypeAdapter

from gameforge.apps.api.app import create_app
from gameforge.contracts.api import RunCommandServerFrame
from gameforge.contracts.jobs import (
    MAX_RUN_COMMAND_CLIENT_SEQ,
    Problem,
    RunCommandV1,
    RunEvent,
    frozen_run_command_payload_schemas_v1,
    frozen_run_event_definitions_v1,
)


OPENAPI_KEY = "openapi-v1.json"
SSE_EVENT_KEY = "schemas/sse-run-event-v1.json"
WS_SERVER_FRAME_KEY = "schemas/ws-server-frame-v1.json"
WS_CLIENT_COMMAND_KEY = "schemas/ws-client-command-v1.json"

_JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SCHEMA_ID_BASE = "https://gameforge.dev/api/schemas/"
_PROBLEM_MEDIA_TYPE = "application/problem+json"
_PROBLEM_REF = "#/components/schemas/Problem"
_JSON_MEDIA_TYPE = "application/json"
_UINT64_MAX = (1 << 64) - 1
_MAX_SSE_CURSOR = (1 << 63) - 1

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
            "HttpOnly, Secure, SameSite=Strict, Path=/. Non-safe HTTP methods must also "
            "present the session-bound X-CSRF-Token header returned by login; "
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
    "auth_login": ("400", "401", "403", "413", "422", "429", "500", "503"),
    "auth_logout": ("401", "403", "422", "429", "500", "503"),
    "auth_me": ("401", "403", "429", "500", "503"),
    "read_item": _BASE_ERROR_STATUSES + ("404",),
    "execution_profile_binding_read": _BASE_ERROR_STATUSES + ("404", "409"),
    "read_page": _BASE_ERROR_STATUSES + ("400", "410"),
    "read_parent_page": _BASE_ERROR_STATUSES + ("400", "404", "410"),
    "observability": _BASE_ERROR_STATUSES + ("400", "404", "410"),
    "write_command": _BASE_ERROR_STATUSES + ("400", "404", "409", "410", "413"),
    "run_admission": _BASE_ERROR_STATUSES + ("404", "409", "413"),
    "execution_option_resolve": _BASE_ERROR_STATUSES + ("409", "413"),
    "cancel": _BASE_ERROR_STATUSES + ("404", "409", "413"),
    "sse": ("400", "401", "403", "404", "410", "422", "500", "503"),
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
    "X-CSRF-Token": _header("Session-bound CSRF token to echo on mutating requests."),
    "Cache-Control": _header("Always `no-store` for the authentication response."),
}
_LOGOUT_HEADERS = {
    "Set-Cookie": _header(f"Clears the `{_SESSION_COOKIE_NAME}` session cookie."),
    "Cache-Control": _header("Always `no-store` for the authentication response."),
}
_NO_STORE_HEADERS = {"Cache-Control": _header("Always `no-store`.")}
_EXECUTION_OPTION_HEADERS = {
    "Cache-Control": _header("Always `private, no-store` for execution options.")
}
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
        "Session-bound CSRF token from login. Required when authenticating with the "
        "session cookie with a non-safe HTTP method, including a read-only POST "
        "resolver; ignored for ApiKey service clients."
    ),
    "schema": {"type": "string", "maxLength": 4096},
}
_LOGOUT_CSRF_REQUEST_HEADER = {
    **_CSRF_REQUEST_HEADER,
    "required": True,
    "description": "Session-bound CSRF token returned by login; required for logout.",
}

_IDEMPOTENCY_REQUEST_HEADER = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": True,
    "description": "Bounded idempotency key for exact command replay.",
    "schema": {"type": "string", "minLength": 1, "maxLength": 512},
}

_LAST_EVENT_ID_REQUEST_HEADER = {
    "name": "Last-Event-ID",
    "in": "header",
    "required": False,
    "description": (
        "Last committed SSE event sequence received; omit only for a fresh stream. "
        "The raw header is canonical base-10 with no sign or leading zeroes, except `0`."
    ),
    "schema": {
        "type": "integer",
        "minimum": 0,
        "maximum": _MAX_SSE_CURSOR,
    },
}

_PARENT_BOUND_PAGE_PATHS = frozenset(
    {
        "/api/v1/specs/{artifact_id}/graph",
        "/api/v1/diff",
        "/api/v1/artifacts/{artifact_id}/lineage",
        "/api/v1/refs/{ref_name}/history",
        "/api/v1/runs/{run_id}/findings",
        "/api/v1/runs/{run_id}/finding-links",
        "/api/v1/runs/{run_id}/commands",
        "/api/v1/conflict-sets/{conflict_set_id}/conflicts",
    }
)

_EXECUTION_PROFILE_BINDING_PATHS = frozenset(
    {
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/constraint-validation-binding",
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/task-suite-derivation-binding",
    }
)

_SSE_DESCRIPTION = (
    "A stream of Server-Sent Events (`text/event-stream`). Each event's `data:` line is a "
    "canonical-JSON RunEvent (see schemas/sse-run-event-v1.json); the SSE `id:` is the "
    "persisted event `seq`, echoed via `Last-Event-ID` to resume. Comment lines "
    "(`:` keep-alive) do not advance the cursor."
)


# ── classification ──────────────────────────────────────────────────────────
def _operation_class(method: str, path: str, operation: Mapping[str, Any]) -> str:
    tags = operation.get("tags", []) or []
    if method == "get" and path in _EXECUTION_PROFILE_BINDING_PATHS:
        return "execution_profile_binding_read"
    if method == "post" and path == "/api/v1/execution-options:resolve":
        return "execution_option_resolve"
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
    if _is_page_operation(operation):
        return "read_parent_page" if path in _PARENT_BOUND_PAGE_PATHS else "read_page"
    return "read_item"


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
    elif operation_class in ("read_item", "execution_profile_binding_read"):
        _set_headers(operation, "200", _ETAG_HEADERS)
    elif operation_class in ("read_page", "read_parent_page"):
        _set_headers(operation, "200", _PAGE_HEADERS)
    elif operation_class == "write_command":
        code = _success_code(operation)
        if code is not None:
            _set_headers(operation, code, _ETAG_HEADERS)
    elif operation_class == "run_admission":
        _set_headers(operation, "202", _ADMISSION_HEADERS)
    elif operation_class == "execution_option_resolve":
        _set_headers(operation, "200", _EXECUTION_OPTION_HEADERS)
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


def _append_parameter(operation: dict[str, Any], parameter: Mapping[str, Any]) -> None:
    parameters = operation.setdefault("parameters", [])
    for existing in parameters:
        if existing.get("name") == parameter.get("name") and existing.get("in") == parameter.get(
            "in"
        ):
            return
    parameters.append(copy.deepcopy(dict(parameter)))


def _inject_request_headers(operation: dict[str, Any], operation_class: str) -> None:
    if operation_class == "auth_logout":
        _append_parameter(operation, _LOGOUT_CSRF_REQUEST_HEADER)
    elif operation_class in (
        "write_command",
        "run_admission",
        "execution_option_resolve",
        "cancel",
    ):
        _append_parameter(operation, _CSRF_REQUEST_HEADER)
    if operation_class == "auth_logout":
        _append_parameter(operation, _IDEMPOTENCY_REQUEST_HEADER)
    if operation_class == "sse":
        _append_parameter(operation, _LAST_EVENT_ID_REQUEST_HEADER)


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


def _pointer_part(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _resolve_document_ref(document: Mapping[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise RuntimeError(f"only local schema refs are supported: {ref}")
    node: Any = document
    for raw_part in ref[2:].split("/"):
        part = _pointer_part(raw_part)
        if not isinstance(node, Mapping) or part not in node:
            raise RuntimeError(f"schema ref does not resolve: {ref}")
        node = node[part]
    if not isinstance(node, dict):
        raise RuntimeError(f"schema ref does not resolve to an object: {ref}")
    return node


def _require_const_field(schema: dict[str, Any], name: str, expected: str) -> None:
    properties = schema.get("properties")
    field = properties.get(name) if isinstance(properties, Mapping) else None
    if not isinstance(field, Mapping) or field.get("const") != expected:
        raise RuntimeError(f"{name} const schema is unavailable")
    required = schema.setdefault("required", [])
    if name not in required:
        required.append(name)


def _require_discriminator_tags(
    document: dict[str, Any], union: Mapping[str, Any], *, tag_name: str
) -> None:
    discriminator = union.get("discriminator")
    mapping = discriminator.get("mapping") if isinstance(discriminator, Mapping) else None
    if not isinstance(mapping, Mapping) or not mapping:
        raise RuntimeError(f"{tag_name} discriminator mapping is unavailable")
    for tag_value, ref in mapping.items():
        if not isinstance(tag_value, str) or not isinstance(ref, str):
            raise RuntimeError(f"{tag_name} discriminator mapping is malformed")
        variant = _resolve_document_ref(document, ref)
        properties = variant.get("properties")
        tag = properties.get(tag_name) if isinstance(properties, Mapping) else None
        if not isinstance(tag, Mapping) or tag.get("const") != tag_value:
            raise RuntimeError(f"{tag_name} discriminator variant differs from its mapping")
        required = variant.setdefault("required", [])
        if tag_name not in required:
            required.append(tag_name)


def _close_run_command_schema(document: dict[str, Any], command_schema: dict[str, Any]) -> None:
    properties = command_schema.get("properties")
    payload = properties.get("payload") if isinstance(properties, Mapping) else None
    if not isinstance(payload, Mapping):
        raise RuntimeError("RunCommandV1 payload schema is unavailable")
    _require_discriminator_tags(document, payload, tag_name="schema_version")
    discriminator = payload["discriminator"]
    mapping = discriminator["mapping"]
    branches: list[dict[str, Any]] = []
    for command_type, payload_schema_id in frozen_run_command_payload_schemas_v1():
        payload_ref = mapping.get(payload_schema_id)
        if not isinstance(payload_ref, str):
            raise RuntimeError("RunCommandV1 payload mapping is incomplete")
        branches.append(
            {
                "type": "object",
                "required": ["type", "payload_schema_id", "payload"],
                "properties": {
                    "type": {"const": command_type},
                    "payload_schema_id": {"const": payload_schema_id},
                    "payload": {"$ref": payload_ref},
                },
            }
        )
    command_schema["oneOf"] = branches


def _close_run_event_schema(document: dict[str, Any]) -> None:
    properties = document.get("properties")
    data = properties.get("data") if isinstance(properties, Mapping) else None
    if not isinstance(data, Mapping):
        raise RuntimeError("RunEvent data schema is unavailable")
    _require_discriminator_tags(document, data, tag_name="data_schema_version")
    mapping = data["discriminator"]["mapping"]
    branches: list[dict[str, Any]] = []
    for definition in frozen_run_event_definitions_v1():
        data_ref = mapping.get(definition.data_schema_id)
        if not isinstance(data_ref, str):
            raise RuntimeError("RunEvent data mapping is incomplete")
        branch_properties: dict[str, Any] = {
            "event_type": {"const": definition.event_type},
            "data_schema_version": {"const": definition.data_schema_id},
            "data": {"$ref": data_ref},
        }
        required = ["event_type", "data_schema_version", "data"]
        if definition.attempt_scope == "attempt":
            branch_properties["attempt_no"] = {"type": "integer", "minimum": 1}
            required.append("attempt_no")
        elif definition.attempt_scope == "run":
            branch_properties["attempt_no"] = {"type": "null"}
        branches.append(
            {
                "type": "object",
                "required": required,
                "properties": branch_properties,
            }
        )
    document["oneOf"] = branches


def _restrict_rest_cancel(operation: dict[str, Any]) -> None:
    content = operation.get("requestBody", {}).get("content", {})
    media = content.get(_JSON_MEDIA_TYPE)
    schema = media.get("schema") if isinstance(media, Mapping) else None
    if schema != {"$ref": "#/components/schemas/RunCommandV1"}:
        raise RuntimeError("REST cancel no longer uses the full RunCommandV1 envelope")
    media["schema"] = {
        "allOf": [
            {"$ref": "#/components/schemas/RunCommandV1"},
            {
                "type": "object",
                "required": ["type", "payload_schema_id", "payload"],
                "properties": {
                    "type": {"const": "cancel"},
                    "payload_schema_id": {"const": "run-cancel@1"},
                    "payload": {"$ref": "#/components/schemas/CancelRunPayloadV1"},
                },
            },
        ]
    }


def _restore_exact_large_integer_bounds(value: Any) -> None:
    if isinstance(value, dict):
        maximum = value.get("maximum")
        if maximum == float(MAX_RUN_COMMAND_CLIENT_SEQ):
            value["maximum"] = MAX_RUN_COMMAND_CLIENT_SEQ
        elif maximum == float(_UINT64_MAX):
            value["maximum"] = _UINT64_MAX
        for child in value.values():
            _restore_exact_large_integer_bounds(child)
    elif isinstance(value, list):
        for child in value:
            _restore_exact_large_integer_bounds(child)


def build_openapi() -> dict[str, Any]:
    """Build the frozen, injected OpenAPI document from the real route table."""

    document = copy.deepcopy(create_app().openapi())
    _inject_problem_schema(document)
    components = document.setdefault("components", {})
    components["securitySchemes"] = copy.deepcopy(_SECURITY_SCHEMES)
    document["security"] = copy.deepcopy(_SESSION_OR_API_KEY)
    schemas = components.setdefault("schemas", {})
    command_schema = schemas.get("RunCommandV1")
    if not isinstance(command_schema, dict):
        raise RuntimeError("OpenAPI is missing RunCommandV1")
    _close_run_command_schema(document, command_schema)
    ack_schema = schemas.get("RunCommandAckV1")
    if not isinstance(ack_schema, dict):
        raise RuntimeError("OpenAPI is missing RunCommandAckV1")
    _require_const_field(ack_schema, "ack_schema_version", "run-command-ack@1")
    _restore_exact_large_integer_bounds(document)

    for path, path_item in document.get("paths", {}).items():
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_class = _operation_class(method, path, operation)
            operation["security"] = _security_for(operation_class)
            _inject_error_responses(operation, operation_class)
            _inject_success_headers(operation, operation_class)
            _inject_request_headers(operation, operation_class)
            if operation_class == "cancel":
                _restrict_rest_cancel(operation)
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
    document = {
        "$schema": _JSON_SCHEMA_DIALECT,
        "$id": _schema_id("ws-server-frame-v1.json"),
        "title": "RunCommandServerFrame",
        "description": (
            "One server frame on WS /runs/{id}/commands: exactly one of a "
            "RunCommandAckV1 (accepted/duplicate) or a RunCommandProblemV1 (RFC 9457 "
            "problem). REST cancel returns the ack as JSON and errors as HTTP Problem "
            "responses. Lease/fencing worker columns are structurally absent."
        ),
        "oneOf": copy.deepcopy(variants),
        "$defs": raw.get("$defs", {}),
    }
    definitions = document["$defs"]
    _require_const_field(definitions["RunCommandAckV1"], "ack_schema_version", "run-command-ack@1")
    _require_const_field(
        definitions["RunCommandProblemV1"],
        "problem_schema_version",
        "run-command-problem@1",
    )
    return document


def build_streaming_schemas() -> dict[str, dict[str, Any]]:
    event = _model_schema_document(
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
    )
    _require_const_field(event, "event_schema_version", "run-event@1")
    _close_run_event_schema(event)
    command = _model_schema_document(
        RunCommandV1,
        "ws-client-command-v1.json",
        title="RunCommandV1",
        description=(
            "The full command envelope shared by WS /runs/{id}/commands and "
            "POST /runs/{id}:cancel (where type must be cancel). `payload` is a "
            "discriminated (`schema_version`) RunCommandPayload union."
        ),
    )
    _close_run_command_schema(command, command)
    return {
        SSE_EVENT_KEY: event,
        WS_SERVER_FRAME_KEY: _server_frame_document(),
        WS_CLIENT_COMMAND_KEY: command,
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
    generated = generate()
    for key, document in generated.items():
        target = base / key
        if not target.is_file():
            continue
        try:
            previous = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CompatibilityError(f"cannot read existing frozen artifact {key}") from exc
        breaks = check_compatibility(previous, document)
        if breaks:
            summary = "; ".join(f"{change.kind} at {change.location}" for change in breaks[:10])
            raise CompatibilityError(
                f"refusing backward-incompatible overwrite of {key}: {summary}"
            )
    written: list[str] = []
    for key, document in generated.items():
        target = base / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(serialize(document), encoding="utf-8")
        written.append(key)
    return written


def check(base_dir: Path | None = None) -> list[str]:
    """Regenerate IN MEMORY and diff against the committed artifacts (no writes)."""

    base = base_dir if base_dir is not None else docs_api_dir()
    problems: list[str] = []
    generated = generate()
    expected_keys = set(generated)
    if base.is_dir():
        actual_keys = {
            path.relative_to(base).as_posix() for path in base.rglob("*.json") if path.is_file()
        }
        for key in sorted(actual_keys - expected_keys):
            problems.append(f"UNEXPECTED {key}")
    for key, document in generated.items():
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


class CompatibilityError(RuntimeError):
    """Raised when ``--write`` would overwrite a frozen v1 contract incompatibly."""


def _is_openapi(document: Any) -> bool:
    return isinstance(document, Mapping) and "openapi" in document and "paths" in document


def check_compatibility(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[BreakingChange]:
    """Return the breaking changes going OLD → NEW (empty list ⇒ backward compatible).

    PERMITS additive changes (new path/method/operation, new optional field, new enum
    value, new request-union variant). REJECTS removed paths/methods/response-statuses,
    narrowed enums/literals/bounds, newly-required request fields, weakened response
    guarantees, changed discriminators, and incompatible request/response transport schemas.
    """

    changes: list[BreakingChange] = []
    if _is_openapi(old) and _is_openapi(new):
        _diff_openapi(old, new, changes)
    else:
        for key in ("$schema", "$id"):
            if key in old and old.get(key) != new.get(key):
                changes.append(
                    BreakingChange(
                        "schema_identity_changed",
                        f"$.{key}",
                        f"{old.get(key)!r}->{new.get(key)!r}",
                    )
                )
        artifact_id = str(old.get("$id", ""))
        mode: SchemaMode = (
            "request" if artifact_id.endswith("ws-client-command-v1.json") else "response"
        )
        _diff_schema(old, new, old, new, "$", changes, set(), mode)
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
    if old.get("openapi") != new.get("openapi"):
        changes.append(
            BreakingChange(
                "openapi_version_changed",
                "openapi",
                f"{old.get('openapi')!r}->{new.get('openapi')!r}",
            )
        )
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
            _diff_operation(
                old_op,
                new_op,
                old_item,
                new_item,
                old,
                new,
                f"paths[{path}].{method}",
                changes,
            )

    old_schemes = old.get("components", {}).get("securitySchemes", {})
    new_schemes = new.get("components", {}).get("securitySchemes", {})
    for name, old_scheme in old_schemes.items():
        new_scheme = new_schemes.get(name)
        if not isinstance(new_scheme, Mapping):
            changes.append(BreakingChange("security_scheme_removed", f"securitySchemes.{name}"))
            continue
        structural_keys = ("type", "in", "name", "scheme", "bearerFormat", "openIdConnectUrl")
        for key in structural_keys:
            if old_scheme.get(key) != new_scheme.get(key):
                changes.append(
                    BreakingChange(
                        "security_scheme_changed",
                        f"securitySchemes.{name}.{key}",
                        f"{old_scheme.get(key)!r}->{new_scheme.get(key)!r}",
                    )
                )


def _diff_operation(
    old_op: Mapping[str, Any],
    new_op: Mapping[str, Any],
    old_path_item: Mapping[str, Any],
    new_path_item: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    _diff_security(old_op, new_op, old_root, new_root, location, changes)
    _diff_parameters(
        old_path_item,
        new_path_item,
        old_op,
        new_op,
        old_root,
        new_root,
        location,
        changes,
    )
    _diff_request_body(old_op, new_op, old_root, new_root, location, changes)

    old_responses = old_op.get("responses", {})
    new_responses = new_op.get("responses", {})
    for status, old_response in old_responses.items():
        new_response = new_responses.get(status)
        response_location = f"{location}.responses[{status}]"
        if not isinstance(new_response, Mapping):
            changes.append(BreakingChange("response_status_removed", response_location))
            continue
        if not isinstance(old_response, Mapping):
            continue
        _diff_content(
            old_response.get("content"),
            new_response.get("content"),
            old_root,
            new_root,
            f"{response_location}.content",
            changes,
            mode="response",
        )
        _diff_response_headers(
            old_response,
            new_response,
            old_root,
            new_root,
            response_location,
            changes,
        )


def _canonical_security(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    normalized: set[str] = set()
    for requirement in value:
        if not isinstance(requirement, Mapping):
            continue
        normalized.add(
            json.dumps(
                {key: sorted(scopes) for key, scopes in requirement.items()},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return normalized


def _allows_anonymous(value: Any) -> bool:
    return (
        value is None
        or value == []
        or (
            isinstance(value, list)
            and any(isinstance(requirement, Mapping) and not requirement for requirement in value)
        )
    )


def _diff_security(
    old_op: Mapping[str, Any],
    new_op: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    old_security = old_op.get("security", old_root.get("security"))
    new_security = new_op.get("security", new_root.get("security"))
    if _allows_anonymous(old_security) and not _allows_anonymous(new_security):
        changes.append(BreakingChange("security_required_added", f"{location}.security"))
    removed = sorted(_canonical_security(old_security) - _canonical_security(new_security))
    if removed:
        changes.append(
            BreakingChange("security_alternative_removed", f"{location}.security", repr(removed))
        )


def _resolved_object(value: Any, root: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    ref = value.get("$ref")
    if isinstance(ref, str):
        return _resolve_ref(ref, root)
    return value


def _parameter_map(
    path_item: Mapping[str, Any], operation: Mapping[str, Any], root: Mapping[str, Any]
) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    raw_parameters = [
        *(path_item.get("parameters", []) or []),
        *(operation.get("parameters", []) or []),
    ]
    for raw in raw_parameters:
        parameter = _resolved_object(raw, root)
        if parameter is None:
            continue
        name = parameter.get("name")
        location = parameter.get("in")
        if not isinstance(name, str) or not isinstance(location, str):
            continue
        normalized_name = name.casefold() if location == "header" else name
        result[(location, normalized_name)] = parameter
    return result


def _parameter_serialization(parameter: Mapping[str, Any]) -> tuple[Any, ...]:
    location = parameter.get("in")
    default_style = "form" if location in ("query", "cookie") else "simple"
    style = parameter.get("style", default_style)
    return (
        style,
        parameter.get("explode", style == "form"),
        parameter.get("allowReserved", False),
        parameter.get("allowEmptyValue", False),
    )


def _diff_parameters(
    old_path_item: Mapping[str, Any],
    new_path_item: Mapping[str, Any],
    old_op: Mapping[str, Any],
    new_op: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    old_parameters = _parameter_map(old_path_item, old_op, old_root)
    new_parameters = _parameter_map(new_path_item, new_op, new_root)
    for key, old_parameter in old_parameters.items():
        parameter_location = f"{location}.parameters[{key[0]}:{key[1]}]"
        new_parameter = new_parameters.get(key)
        if new_parameter is None:
            changes.append(BreakingChange("parameter_removed", parameter_location))
            continue
        if not old_parameter.get("required", False) and new_parameter.get("required", False):
            changes.append(BreakingChange("parameter_became_required", parameter_location))
        old_serialization = _parameter_serialization(old_parameter)
        new_serialization = _parameter_serialization(new_parameter)
        if old_serialization != new_serialization:
            changes.append(
                BreakingChange(
                    "parameter_serialization_changed",
                    parameter_location,
                    f"{old_serialization!r}->{new_serialization!r}",
                )
            )
        _diff_schema(
            old_parameter.get("schema"),
            new_parameter.get("schema"),
            old_root,
            new_root,
            f"{parameter_location}.schema",
            changes,
            set(),
            "request",
        )
        _diff_content(
            old_parameter.get("content"),
            new_parameter.get("content"),
            old_root,
            new_root,
            f"{parameter_location}.content",
            changes,
            mode="request",
        )
    for key, new_parameter in new_parameters.items():
        if key not in old_parameters and new_parameter.get("required", False):
            changes.append(
                BreakingChange(
                    "required_parameter_added",
                    f"{location}.parameters[{key[0]}:{key[1]}]",
                )
            )


def _diff_request_body(
    old_op: Mapping[str, Any],
    new_op: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    old_body = _resolved_object(old_op.get("requestBody"), old_root)
    new_body = _resolved_object(new_op.get("requestBody"), new_root)
    body_location = f"{location}.requestBody"
    if old_body is not None and new_body is None:
        changes.append(BreakingChange("request_body_removed", body_location))
        return
    if old_body is None:
        if new_body is not None and new_body.get("required", False):
            changes.append(BreakingChange("required_request_body_added", body_location))
        return
    if new_body is None:
        return
    if not old_body.get("required", False) and new_body.get("required", False):
        changes.append(BreakingChange("request_body_became_required", body_location))
    _diff_content(
        old_body.get("content"),
        new_body.get("content"),
        old_root,
        new_root,
        f"{body_location}.content",
        changes,
        mode="request",
    )


def _diff_content(
    old_content: Any,
    new_content: Any,
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    *,
    mode: SchemaMode,
) -> None:
    if not isinstance(old_content, Mapping):
        return
    if not isinstance(new_content, Mapping):
        if old_content:
            changes.append(BreakingChange("content_removed", location))
        return
    for media_type, old_media in old_content.items():
        new_media = new_content.get(media_type)
        media_location = f"{location}[{media_type}]"
        if not isinstance(new_media, Mapping):
            changes.append(BreakingChange("media_type_removed", media_location))
            continue
        if not isinstance(old_media, Mapping):
            continue
        _diff_schema(
            old_media.get("schema"),
            new_media.get("schema"),
            old_root,
            new_root,
            f"{media_location}.schema",
            changes,
            set(),
            mode,
        )


def _diff_response_headers(
    old_response: Mapping[str, Any],
    new_response: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
) -> None:
    old_headers = {
        name.casefold(): value for name, value in (old_response.get("headers", {}) or {}).items()
    }
    new_headers = {
        name.casefold(): value for name, value in (new_response.get("headers", {}) or {}).items()
    }
    for name, old_header_raw in old_headers.items():
        header_location = f"{location}.headers[{name}]"
        new_header_raw = new_headers.get(name)
        if new_header_raw is None:
            changes.append(BreakingChange("response_header_removed", header_location))
            continue
        old_header = _resolved_object(old_header_raw, old_root)
        new_header = _resolved_object(new_header_raw, new_root)
        if old_header is None or new_header is None:
            changes.append(BreakingChange("response_header_changed", header_location))
            continue
        _diff_schema(
            old_header.get("schema"),
            new_header.get("schema"),
            old_root,
            new_root,
            f"{header_location}.schema",
            changes,
            set(),
            "response",
        )


def _resolve_ref(ref: str, root: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    node: Any = root
    for raw_part in ref[2:].split("/"):
        part = _pointer_part(raw_part)
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, Mapping) else None


SchemaMode = Literal["request", "response"]


def _diff_schema(
    old_schema: Any,
    new_schema: Any,
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str, SchemaMode]],
    mode: SchemaMode,
) -> None:
    if old_schema is None:
        return
    if new_schema is None:
        changes.append(BreakingChange("schema_removed", location))
        return
    if isinstance(old_schema, bool) or isinstance(new_schema, bool):
        if old_schema != new_schema:
            changes.append(BreakingChange("schema_changed", location))
        return
    if not isinstance(old_schema, Mapping) or not isinstance(new_schema, Mapping):
        changes.append(BreakingChange("schema_changed", location))
        return

    old_ref = old_schema.get("$ref")
    new_ref = new_schema.get("$ref")
    if isinstance(old_ref, str) and isinstance(new_ref, str):
        if old_ref != new_ref:
            changes.append(BreakingChange("schema_ref_changed", location, f"{old_ref}->{new_ref}"))
        key = (old_ref, new_ref, mode)
        old_siblings = {name: value for name, value in old_schema.items() if name != "$ref"}
        new_siblings = {name: value for name, value in new_schema.items() if name != "$ref"}
        _diff_schema(
            old_siblings,
            new_siblings,
            old_root,
            new_root,
            f"{location}.$ref-siblings",
            changes,
            seen | {key},
            mode,
        )
        if key in seen:
            return
        old_target = _resolve_ref(old_ref, old_root)
        new_target = _resolve_ref(new_ref, new_root)
        if old_target is None or new_target is None:
            changes.append(
                BreakingChange(
                    "schema_ref_target_removed",
                    location,
                    new_ref if new_target is None else old_ref,
                )
            )
            return
        _diff_schema(
            old_target,
            new_target,
            old_root,
            new_root,
            location,
            changes,
            seen | {key},
            mode,
        )
        return
    if isinstance(old_ref, str):
        old_target = _resolve_ref(old_ref, old_root)
        if old_target is None:
            changes.append(BreakingChange("schema_ref_target_removed", location, old_ref))
            return
        old_schema = old_target
    if isinstance(new_ref, str):
        new_target = _resolve_ref(new_ref, new_root)
        if new_target is None:
            changes.append(BreakingChange("schema_ref_target_removed", location, new_ref))
            return
        new_schema = new_target
    if not isinstance(old_schema, Mapping) or not isinstance(new_schema, Mapping):
        return

    _diff_enum(old_schema, new_schema, location, changes, mode)
    _diff_const(old_schema, new_schema, location, changes, mode)
    _diff_discriminator(old_schema, new_schema, location, changes)
    _diff_type(old_schema, new_schema, location, changes, mode)
    _diff_constraints(old_schema, new_schema, location, changes, mode)
    _diff_object_key_schemas(
        old_schema, new_schema, old_root, new_root, location, changes, seen, mode
    )
    if old_schema.get("writeOnly") is True and new_schema.get("writeOnly") is not True:
        changes.append(BreakingChange("write_only_removed", f"{location}.writeOnly"))
    _diff_required_and_properties(
        old_schema, new_schema, old_root, new_root, location, changes, seen, mode
    )
    _diff_additional_properties(
        old_schema, new_schema, old_root, new_root, location, changes, seen, mode
    )
    _diff_items(old_schema, new_schema, old_root, new_root, location, changes, seen, mode)
    _diff_unions(old_schema, new_schema, old_root, new_root, location, changes, seen, mode)
    _diff_all_of(old_schema, new_schema, old_root, new_root, location, changes, seen, mode)


def _diff_enum(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    mode: SchemaMode,
) -> None:
    old_enum = old_schema.get("enum")
    new_enum = new_schema.get("enum")
    if mode == "response":
        removed = (
            [value for value in old_enum if value not in new_enum]
            if isinstance(old_enum, list) and isinstance(new_enum, list)
            else []
        )
        if removed:
            changes.append(BreakingChange("enum_narrowed", f"{location}.enum", repr(removed)))
        elif old_enum != new_enum and (isinstance(old_enum, list) or isinstance(new_enum, list)):
            changes.append(
                BreakingChange(
                    "response_enum_widened",
                    f"{location}.enum",
                    f"{old_enum!r}->{new_enum!r}",
                )
            )
    elif not isinstance(old_enum, list) and isinstance(new_enum, list):
        changes.append(BreakingChange("enum_narrowed", f"{location}.enum", repr(new_enum)))
    elif isinstance(old_enum, list) and isinstance(new_enum, list):
        removed = [value for value in old_enum if value not in new_enum]
        if removed:
            changes.append(BreakingChange("enum_narrowed", f"{location}.enum", repr(removed)))


def _diff_const(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    mode: SchemaMode,
) -> None:
    if (
        mode == "response"
        and "const" in old_schema
        and old_schema.get("const") != new_schema.get("const")
    ):
        changes.append(
            BreakingChange(
                "response_const_weakened",
                f"{location}.const",
                f"{old_schema.get('const')!r}->{new_schema.get('const')!r}",
            )
        )
    elif "const" not in old_schema and "const" in new_schema:
        changes.append(
            BreakingChange("const_added", f"{location}.const", repr(new_schema["const"]))
        )
    elif (
        "const" in old_schema
        and "const" in new_schema
        and old_schema["const"] != new_schema["const"]
    ):
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
    changed = sorted(key for key in old_map.keys() & new_map.keys() if old_map[key] != new_map[key])
    if changed:
        changes.append(BreakingChange("discriminator_mapping_changed", location, repr(changed)))


def _diff_type(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    mode: SchemaMode,
) -> None:
    def types(value: Any) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, list):
            return {item for item in value if isinstance(item, str)}
        return set()

    old_types = types(old_schema.get("type"))
    new_types = types(new_schema.get("type"))
    if mode == "response":
        incompatible = bool(old_types) and (not new_types or not new_types <= old_types)
        kind = "response_type_widened"
    else:
        incompatible = bool(new_types) and (not old_types or not old_types <= new_types)
        kind = "type_narrowed"
    if incompatible:
        changes.append(BreakingChange(kind, location, f"{sorted(old_types)}->{sorted(new_types)}"))


def _diff_constraints(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    mode: SchemaMode,
) -> None:
    upper_bounds = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")
    lower_bounds = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
    for key in upper_bounds:
        old_value = old_schema.get(key)
        new_value = new_schema.get(key)
        if mode == "response":
            incompatible = isinstance(old_value, (int, float)) and (
                not isinstance(new_value, (int, float)) or new_value > old_value
            )
        else:
            incompatible = isinstance(new_value, (int, float)) and (
                not isinstance(old_value, (int, float)) or new_value < old_value
            )
        if incompatible:
            changes.append(
                BreakingChange("bound_changed", f"{location}.{key}", f"{old_value}->{new_value}")
            )
    for key in lower_bounds:
        old_value = old_schema.get(key)
        new_value = new_schema.get(key)
        if mode == "response":
            incompatible = isinstance(old_value, (int, float)) and (
                not isinstance(new_value, (int, float)) or new_value < old_value
            )
        else:
            incompatible = isinstance(new_value, (int, float)) and (
                not isinstance(old_value, (int, float)) or new_value > old_value
            )
        if incompatible:
            changes.append(
                BreakingChange("bound_changed", f"{location}.{key}", f"{old_value}->{new_value}")
            )
    for key in ("pattern", "format", "multipleOf"):
        if mode == "response":
            incompatible = old_schema.get(key) != new_schema.get(key) and (
                key in old_schema or key in new_schema
            )
        else:
            incompatible = key in new_schema and old_schema.get(key) != new_schema.get(key)
        if incompatible:
            changes.append(
                BreakingChange(
                    "constraint_changed",
                    f"{location}.{key}",
                    f"{old_schema.get(key)!r}->{new_schema.get(key)!r}",
                )
            )
    old_unique = old_schema.get("uniqueItems") is True
    new_unique = new_schema.get("uniqueItems") is True
    if (mode == "request" and new_unique and not old_unique) or (
        mode == "response" and old_unique and not new_unique
    ):
        changes.append(BreakingChange("unique_items_weakened", f"{location}.uniqueItems"))
    old_additional = old_schema.get("additionalProperties", True)
    new_additional = new_schema.get("additionalProperties", True)
    if mode == "response":
        if old_additional is False and new_additional is not False:
            changes.append(BreakingChange("response_additional_properties_opened", location))
    elif old_additional is not False and new_additional is False:
        changes.append(BreakingChange("additional_properties_closed", location))


def _diff_object_key_schemas(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str, SchemaMode]],
    mode: SchemaMode,
) -> None:
    old_names = old_schema.get("propertyNames")
    new_names = new_schema.get("propertyNames")
    changed = (mode == "request" and old_names is None and new_names is not None) or (
        mode == "response" and old_names is not None and new_names is None
    )
    if changed:
        changes.append(BreakingChange("property_names_changed", f"{location}.propertyNames"))
    elif old_names is not None and new_names is not None:
        _diff_schema(
            old_names,
            new_names,
            old_root,
            new_root,
            f"{location}.propertyNames",
            changes,
            seen,
            mode,
        )
    old_patterns = old_schema.get("patternProperties") or {}
    new_patterns = new_schema.get("patternProperties") or {}
    if not isinstance(old_patterns, Mapping) or not isinstance(new_patterns, Mapping):
        changes.append(BreakingChange("pattern_properties_changed", location))
        return
    old_keys = set(old_patterns)
    new_keys = set(new_patterns)
    fail_closed = old_keys != new_keys and (
        old_schema.get("additionalProperties") is False
        or new_schema.get("additionalProperties") is False
    )
    directional = new_keys - old_keys if mode == "request" else old_keys - new_keys
    if fail_closed or directional:
        changes.append(
            BreakingChange(
                "pattern_properties_changed",
                f"{location}.patternProperties",
                repr(sorted(old_keys ^ new_keys)),
            )
        )
    for pattern in sorted(old_keys & new_keys):
        _diff_schema(
            old_patterns[pattern],
            new_patterns[pattern],
            old_root,
            new_root,
            f"{location}.patternProperties[{pattern}]",
            changes,
            seen,
            mode,
        )


def _diff_required_and_properties(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str, SchemaMode]],
    mode: SchemaMode,
) -> None:
    old_required = set(old_schema.get("required", []) or [])
    new_required = set(new_schema.get("required", []) or [])
    if mode == "request":
        for name in sorted(new_required - old_required):
            changes.append(BreakingChange("required_added", f"{location}.required", name))
    if mode == "response":
        for name in sorted(old_required - new_required):
            changes.append(
                BreakingChange("response_required_removed", f"{location}.required", name)
            )

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
            mode,
        )


def _diff_additional_properties(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str, SchemaMode]],
    mode: SchemaMode,
) -> None:
    old_additional = old_schema.get("additionalProperties", True)
    new_additional = new_schema.get("additionalProperties", True)
    if isinstance(old_additional, Mapping) and isinstance(new_additional, Mapping):
        _diff_schema(
            old_additional,
            new_additional,
            old_root,
            new_root,
            f"{location}.additionalProperties",
            changes,
            seen,
            mode,
        )
        return
    if isinstance(old_additional, Mapping):
        incompatible = (mode == "request" and new_additional is False) or (
            mode == "response" and new_additional is not False
        )
    elif isinstance(new_additional, Mapping):
        incompatible = (mode == "request" and old_additional is not False) or (
            mode == "response" and old_additional is False
        )
    else:
        return
    if incompatible:
        changes.append(BreakingChange("additional_properties_schema_changed", location))


def _diff_items(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str, SchemaMode]],
    mode: SchemaMode,
) -> None:
    old_items = old_schema.get("items")
    new_items = new_schema.get("items")
    if old_items is not None:
        _diff_schema(
            old_items,
            new_items,
            old_root,
            new_root,
            f"{location}.items",
            changes,
            seen,
            mode,
        )


def _branch_key(entry: Any) -> str:
    if not isinstance(entry, Mapping):
        return "raw:" + repr(entry)
    ref = entry.get("$ref")
    if isinstance(ref, str):
        return "ref:" + ref
    if "const" in entry:
        return "const:" + json.dumps(entry["const"], sort_keys=True)
    properties = entry.get("properties")
    if isinstance(properties, Mapping):
        constants = {
            name: schema["const"]
            for name, schema in properties.items()
            if isinstance(schema, Mapping) and "const" in schema
        }
        if constants:
            return "properties:" + json.dumps(constants, sort_keys=True, separators=(",", ":"))
    schema_type = entry.get("type")
    if isinstance(schema_type, (str, list)):
        return "type:" + json.dumps(schema_type, sort_keys=True)
    return "schema:" + json.dumps(entry, sort_keys=True, separators=(",", ":"))


def _diff_unions(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str, SchemaMode]],
    mode: SchemaMode,
) -> None:
    for keyword in ("oneOf", "anyOf"):
        old_list = old_schema.get(keyword)
        new_list = new_schema.get(keyword)
        if not isinstance(old_list, list):
            if isinstance(new_list, list):
                changes.append(BreakingChange("union_introduced", f"{location}.{keyword}"))
            continue
        if not isinstance(new_list, list):
            changes.append(BreakingChange("union_removed", f"{location}.{keyword}"))
            continue
        old_by_key = {_branch_key(entry): entry for entry in old_list}
        new_by_key = {_branch_key(entry): entry for entry in new_list}
        for key in sorted(set(old_by_key) - set(new_by_key)):
            changes.append(BreakingChange("variant_removed", f"{location}.{keyword}.{key}"))
        if mode == "response":
            for key in sorted(set(new_by_key) - set(old_by_key)):
                changes.append(
                    BreakingChange("response_variant_added", f"{location}.{keyword}.{key}")
                )
        for key in sorted(set(old_by_key) & set(new_by_key)):
            _diff_schema(
                old_by_key[key],
                new_by_key[key],
                old_root,
                new_root,
                f"{location}.{keyword}.{key}",
                changes,
                seen,
                mode,
            )


def _diff_all_of(
    old_schema: Mapping[str, Any],
    new_schema: Mapping[str, Any],
    old_root: Mapping[str, Any],
    new_root: Mapping[str, Any],
    location: str,
    changes: list[BreakingChange],
    seen: set[tuple[str, str, SchemaMode]],
    mode: SchemaMode,
) -> None:
    old_list = old_schema.get("allOf")
    new_list = new_schema.get("allOf")
    if not isinstance(old_list, list):
        if isinstance(new_list, list):
            changes.append(BreakingChange("all_of_introduced", f"{location}.allOf"))
        return
    if not isinstance(new_list, list):
        changes.append(BreakingChange("all_of_removed", f"{location}.allOf"))
        return
    old_by_key = {_branch_key(entry): entry for entry in old_list}
    new_by_key = {_branch_key(entry): entry for entry in new_list}
    for key in sorted(set(old_by_key) - set(new_by_key)):
        changes.append(BreakingChange("all_of_branch_removed", f"{location}.allOf.{key}"))
    for key in sorted(set(new_by_key) - set(old_by_key)):
        changes.append(BreakingChange("all_of_branch_added", f"{location}.allOf.{key}"))
    for key in sorted(set(old_by_key) & set(new_by_key)):
        _diff_schema(
            old_by_key[key],
            new_by_key[key],
            old_root,
            new_root,
            f"{location}.allOf.{key}",
            changes,
            seen,
            mode,
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
    "CompatibilityError",
    "OPENAPI_KEY",
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
