"""RED→GREEN: the frozen ``/api/v1`` OpenAPI describes the ACTUAL served surface.

The frozen ``docs/api/openapi-v1.json`` must (a) enumerate every §5.3 operation under
``/api/v1`` (frozen to the CODE's real routes), (b) inject the four things FastAPI omits
— securitySchemes, RFC 9457 ``Problem`` responses, request/response header + ETag +
idempotency contracts, and the session-cookie contract — (c) leak NO internal callable /
secret / fencing field, (d) keep bounded strings/arrays bounded, and (e) regenerate
byte-for-byte. A pure compatibility checker must PERMIT additive changes and REJECT
removed paths/methods/statuses, narrowed enums, newly-required request fields, and
changed discriminators.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from gameforge.apps.api import schema as api_schema


_FORBIDDEN_FIELD_SUBSTRINGS = (
    "claimed_fencing_token",
    "fencing_token",
    "claimed_attempt_no",
    "claimed_at",
    "lease_id",
    "handler_key",
    "secret_plaintext",
    "get_secret_value",
    "RunCommandRecordV1",
)

# Every §5.3 operation, FROZEN TO THE CODE's real paths (see the report for the two
# spec-prose path discrepancies flagged for the product owner):
#   code /generations:propose   vs prose /generation:propose
#   code /constraints:propose   vs prose /constraint-proposals:propose
_EXPECTED_OPERATIONS: tuple[tuple[str, str, str], ...] = (
    ("post", "/api/v1/auth/login", "204"),
    ("post", "/api/v1/auth/logout", "204"),
    ("get", "/api/v1/auth/me", "200"),
    ("get", "/api/v1/specs", "200"),
    ("post", "/api/v1/specs", "201"),
    ("get", "/api/v1/specs/{artifact_id}", "200"),
    ("get", "/api/v1/specs/{artifact_id}/graph", "200"),
    ("get", "/api/v1/schema-registry/{version}", "200"),
    ("get", "/api/v1/constraints", "200"),
    ("get", "/api/v1/constraints/{artifact_id}", "200"),
    ("get", "/api/v1/constraint-proposals", "200"),
    ("post", "/api/v1/constraint-proposals", "201"),
    ("get", "/api/v1/constraint-proposals/{artifact_id}", "200"),
    ("post", "/api/v1/constraints:propose", "202"),
    ("post", "/api/v1/constraint-proposals/{artifact_id}:revise", "201"),
    ("post", "/api/v1/constraint-proposals/{artifact_id}:validate", "202"),
    ("post", "/api/v1/constraint-proposals/{artifact_id}:submit-for-approval", "200"),
    ("post", "/api/v1/constraint-proposals/{artifact_id}:publish", "200"),
    ("post", "/api/v1/generations:propose", "202"),
    ("get", "/api/v1/reviews", "200"),
    ("get", "/api/v1/reviews/{artifact_id}", "200"),
    ("get", "/api/v1/findings", "200"),
    ("get", "/api/v1/findings/{finding_id}", "200"),
    ("get", "/api/v1/findings/{finding_id}/revisions/{revision}", "200"),
    ("get", "/api/v1/task-suites", "200"),
    ("get", "/api/v1/task-suites/{artifact_id}", "200"),
    ("post", "/api/v1/task-suites:derive", "202"),
    ("post", "/api/v1/playtest:run", "202"),
    ("get", "/api/v1/playtest/{run_id}/result", "200"),
    ("get", "/api/v1/patches", "200"),
    ("post", "/api/v1/patches", "201"),
    ("get", "/api/v1/patches/{artifact_id}", "200"),
    ("post", "/api/v1/patches/{artifact_id}:repair", "202"),
    ("post", "/api/v1/patches/{artifact_id}:validate", "202"),
    ("post", "/api/v1/patches/{artifact_id}:submit-for-approval", "200"),
    ("get", "/api/v1/diff", "200"),
    ("post", "/api/v1/patches/{artifact_id}:rebase", "200"),
    ("post", "/api/v1/patches/{artifact_id}:resolve-conflicts", "200"),
    ("get", "/api/v1/conflict-sets/{conflict_set_id}/conflicts", "200"),
    ("post", "/api/v1/patches/{artifact_id}:apply", "200"),
    ("get", "/api/v1/rollback-requests", "200"),
    ("get", "/api/v1/rollback-requests/{artifact_id}", "200"),
    ("post", "/api/v1/refs/{ref_name}/rollback-requests", "201"),
    ("post", "/api/v1/rollback-requests/{artifact_id}:validate", "202"),
    ("post", "/api/v1/rollback-requests/{artifact_id}:submit-for-approval", "200"),
    ("post", "/api/v1/rollback-requests/{artifact_id}:apply", "200"),
    ("get", "/api/v1/artifacts/{artifact_id}", "200"),
    ("get", "/api/v1/artifacts/{artifact_id}/lineage", "200"),
    ("get", "/api/v1/refs/{ref_name}/history", "200"),
    ("get", "/api/v1/approvals", "200"),
    ("get", "/api/v1/approvals/{approval_id}", "200"),
    ("post", "/api/v1/approvals/{approval_id}:approve", "200"),
    ("post", "/api/v1/approvals/{approval_id}:reject", "200"),
    ("post", "/api/v1/approvals/{approval_id}:request_changes", "200"),
    ("post", "/api/v1/runs", "202"),
    ("get", "/api/v1/runs", "200"),
    ("get", "/api/v1/runs/{run_id}", "200"),
    ("get", "/api/v1/runs/{run_id}/findings", "200"),
    ("get", "/api/v1/runs/{run_id}/events", "200"),
    ("get", "/api/v1/runs/{run_id}/traces", "200"),
    ("get", "/api/v1/runs/{run_id}/commands", "200"),
    ("post", "/api/v1/runs/{run_id}:cancel", "200"),
    ("get", "/api/v1/bench/report", "200"),
    ("get", "/api/v1/execution-profiles", "200"),
    ("get", "/api/v1/execution-profiles/{profile_id}/versions/{version}", "200"),
    ("get", "/api/v1/traces/{trace_id}", "200"),
    ("get", "/api/v1/traces/{trace_id}/spans", "200"),
    ("get", "/api/v1/logs/query", "200"),
    ("get", "/api/v1/metrics/descriptors", "200"),
    ("get", "/api/v1/metrics/query", "200"),
    ("get", "/api/v1/cost/{run_id}", "200"),
)

_PROBLEM_MEDIA = "application/problem+json"


def _openapi() -> dict[str, Any]:
    return api_schema.generate()[api_schema.OPENAPI_KEY]


def test_frozen_openapi_is_committed_and_byte_stable() -> None:
    document = _openapi()
    frozen_path = api_schema.docs_api_dir() / api_schema.OPENAPI_KEY
    assert frozen_path.is_file(), f"frozen OpenAPI not committed: {frozen_path}"
    assert frozen_path.read_text(encoding="utf-8") == api_schema.serialize(document)


def test_openapi_regeneration_is_byte_stable() -> None:
    assert api_schema.serialize(
        api_schema.generate()[api_schema.OPENAPI_KEY]
    ) == api_schema.serialize(api_schema.generate()[api_schema.OPENAPI_KEY])


def test_every_5_3_operation_is_present() -> None:
    paths = _openapi()["paths"]
    for method, path, success in _EXPECTED_OPERATIONS:
        assert path in paths, f"missing path {path}"
        assert method in paths[path], f"missing {method.upper()} {path}"
        responses = paths[path][method]["responses"]
        assert success in responses, f"{method.upper()} {path} missing success status {success}"


def test_security_schemes_and_per_operation_security() -> None:
    document = _openapi()
    schemes = document["components"]["securitySchemes"]
    cookie = schemes["SessionCookie"]
    assert cookie == {
        **cookie,
        "type": "apiKey",
        "in": "cookie",
        "name": "gameforge_session",
    }
    header = schemes["ApiKeyAuth"]
    assert header["type"] == "apiKey" and header["in"] == "header"
    assert header["name"] == "Authorization"

    paths = document["paths"]
    # Login is public.
    assert paths["/api/v1/auth/login"]["post"]["security"] == []
    # Every other operation requires session-or-apikey (or session for logout).
    me_security = paths["/api/v1/auth/me"]["get"]["security"]
    assert {"SessionCookie": []} in me_security and {"ApiKeyAuth": []} in me_security
    for method, path, _success in _EXPECTED_OPERATIONS:
        if path == "/api/v1/auth/login":
            continue
        assert paths[path][method].get("security"), f"{method.upper()} {path} missing security"


def test_problem_responses_present_on_error_statuses() -> None:
    paths = _openapi()["paths"]
    # A representative authenticated read carries 401/403 + default → Problem.
    get_run = paths["/api/v1/runs/{run_id}"]["get"]["responses"]
    for status in ("401", "403", "404", "default"):
        assert status in get_run, f"GET /runs/{{run_id}} missing {status}"
        media = get_run[status]["content"][_PROBLEM_MEDIA]
        assert media["schema"]["$ref"].endswith("/Problem")
    # A write op carries a conflict + payload-too-large + Problem-typed 422.
    validate = paths["/api/v1/patches/{artifact_id}:validate"]["post"]["responses"]
    for status in ("409", "413", "422"):
        assert status in validate, f"patch:validate missing {status}"
        assert _PROBLEM_MEDIA in validate[status]["content"]


def test_write_ops_document_idempotency_etag_and_if_match() -> None:
    paths = _openapi()["paths"]
    validate = paths["/api/v1/patches/{artifact_id}:validate"]["post"]
    params = {(p["name"], p["in"]): p for p in validate["parameters"]}
    assert params[("Idempotency-Key", "header")]["required"] is True
    assert params[("If-Match", "header")]["required"] is True
    success = validate["responses"]["202"]
    assert "ETag" in success["headers"]
    assert "X-Resource-Revision" in success["headers"]

    # POST /runs requires Idempotency-Key but has no If-Match.
    submit = paths["/api/v1/runs"]["post"]
    submit_params = {(p["name"], p["in"]): p for p in submit["parameters"]}
    assert submit_params[("Idempotency-Key", "header")]["required"] is True
    assert ("If-Match", "header") not in submit_params
    assert "Location" in submit["responses"]["202"]["headers"]


def test_login_documents_session_cookie_contract() -> None:
    document = _openapi()
    login = document["paths"]["/api/v1/auth/login"]["post"]
    headers = login["responses"]["204"]["headers"]
    assert "Set-Cookie" in headers
    assert "X-CSRF-Token" in headers
    cookie_scheme = document["components"]["securitySchemes"]["SessionCookie"]
    assert cookie_scheme["name"] == "gameforge_session"


def test_no_internal_secret_or_fencing_fields_anywhere() -> None:
    blob = json.dumps(_openapi())
    for needle in _FORBIDDEN_FIELD_SUBSTRINGS:
        assert needle not in blob, f"frozen OpenAPI leaks forbidden token {needle!r}"
    # FastAPI's default validation schema must be replaced by the Problem contract.
    schemas = _openapi()["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas
    assert "Problem" in schemas


def test_bounded_strings_and_arrays_stay_bounded() -> None:
    blob = json.dumps(_openapi())
    assert blob.count("maxLength") > 100
    assert blob.count("maxItems") > 10
    # A concrete bounded id field carries maxLength.
    run_accepted = _openapi()["components"]["schemas"]["RunAcceptedV1"]
    assert run_accepted["properties"]["run_id"]["maxLength"] == 512


# ── compatibility checker ────────────────────────────────────────────────────
def test_compatibility_identical_is_compatible() -> None:
    document = _openapi()
    assert api_schema.check_compatibility(document, copy.deepcopy(document)) == []


def test_compatibility_additive_new_optional_field_is_allowed() -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    new["components"]["schemas"]["RunAcceptedV1"]["properties"]["new_optional"] = {"type": "string"}
    assert api_schema.check_compatibility(old, new) == []


def test_compatibility_rejects_removed_path() -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    del new["paths"]["/api/v1/runs/{run_id}"]
    breaks = api_schema.check_compatibility(old, new)
    assert any(b.kind == "path_removed" for b in breaks), breaks


def test_compatibility_rejects_removed_method() -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    del new["paths"]["/api/v1/specs"]["post"]
    breaks = api_schema.check_compatibility(old, new)
    assert any(b.kind == "method_removed" for b in breaks), breaks


def test_compatibility_rejects_removed_response_status() -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    del new["paths"]["/api/v1/runs/{run_id}"]["get"]["responses"]["404"]
    breaks = api_schema.check_compatibility(old, new)
    assert any(b.kind == "response_status_removed" for b in breaks), breaks


def test_compatibility_rejects_narrowed_enum() -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    # Narrow the RunEventType enum inside the RunView/RunAccepted status literal.
    target = None
    for name, sch in new["components"]["schemas"].items():
        blob = json.dumps(sch)
        if '"enum"' in blob and "run-cancel@1" not in blob:
            target = name
            break
    assert target is not None
    _narrow_first_enum(new["components"]["schemas"][target])
    breaks = api_schema.check_compatibility(old, new)
    assert any(b.kind == "enum_narrowed" for b in breaks), breaks


def test_compatibility_rejects_new_required_request_field() -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    submission = new["components"]["schemas"]["RunSubmissionRequestV1"]
    submission.setdefault("required", [])
    submission["required"].append("brand_new_required")
    submission.setdefault("properties", {})["brand_new_required"] = {"type": "string"}
    breaks = api_schema.check_compatibility(old, new)
    assert any(b.kind == "required_added" for b in breaks), breaks


def test_compatibility_rejects_changed_discriminator() -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    # RunSubmissionRequestV1.params is a Field(discriminator="schema_version") union.
    params = new["components"]["schemas"]["RunSubmissionRequestV1"]["properties"]["params"]
    mapping = params["discriminator"]["mapping"]
    mapping.pop(next(iter(mapping)))
    breaks = api_schema.check_compatibility(old, new)
    assert any(
        b.kind in ("discriminator_variant_removed", "discriminator_changed") for b in breaks
    ), breaks


def _narrow_first_enum(node: Any) -> bool:
    if isinstance(node, dict):
        enum = node.get("enum")
        if isinstance(enum, list) and len(enum) > 1:
            node["enum"] = enum[:-1]
            return True
        for value in node.values():
            if _narrow_first_enum(value):
                return True
    elif isinstance(node, list):
        for value in node:
            if _narrow_first_enum(value):
                return True
    return False
