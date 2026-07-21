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
from functools import cache
import json
from pathlib import Path
from typing import Any

import pytest

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

# Every §5.3 operation on its frozen path.
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
    ("post", "/api/v1/constraint-proposals:propose", "202"),
    ("post", "/api/v1/constraint-proposals/{artifact_id}:revise", "201"),
    ("post", "/api/v1/constraint-proposals/{artifact_id}:validate", "202"),
    ("post", "/api/v1/constraint-proposals/{artifact_id}:submit-for-approval", "200"),
    ("post", "/api/v1/constraint-proposals/{artifact_id}:publish", "200"),
    ("post", "/api/v1/generation:propose", "202"),
    ("get", "/api/v1/reviews", "200"),
    ("get", "/api/v1/reviews/{artifact_id}", "200"),
    ("get", "/api/v1/reviews/{artifact_id}/producer-binding", "200"),
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
    ("get", "/api/v1/workflow-subjects/{artifact_id}/approval-binding", "200"),
    ("post", "/api/v1/approvals/{approval_id}:approve", "200"),
    ("post", "/api/v1/approvals/{approval_id}:reject", "200"),
    ("post", "/api/v1/approvals/{approval_id}:request_changes", "200"),
    ("post", "/api/v1/runs", "202"),
    ("post", "/api/v1/execution-options:resolve", "200"),
    ("get", "/api/v1/runs", "200"),
    ("get", "/api/v1/runs/{run_id}", "200"),
    ("get", "/api/v1/runs/{run_id}/findings", "200"),
    ("get", "/api/v1/runs/{run_id}/finding-links", "200"),
    ("get", "/api/v1/runs/{run_id}/events", "200"),
    ("get", "/api/v1/runs/{run_id}/traces", "200"),
    ("get", "/api/v1/runs/{run_id}/commands", "200"),
    ("post", "/api/v1/runs/{run_id}:cancel", "200"),
    ("get", "/api/v1/bench/report", "200"),
    ("get", "/api/v1/execution-profiles", "200"),
    ("get", "/api/v1/execution-profiles/{profile_id}/versions/{version}", "200"),
    (
        "get",
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/constraint-validation-binding",
        "200",
    ),
    (
        "get",
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/task-suite-derivation-binding",
        "200",
    ),
    ("get", "/api/v1/traces/{trace_id}", "200"),
    ("get", "/api/v1/traces/{trace_id}/spans", "200"),
    ("get", "/api/v1/logs/query", "200"),
    ("get", "/api/v1/metrics/descriptors", "200"),
    ("get", "/api/v1/metrics/query", "200"),
    ("get", "/api/v1/cost/{run_id}", "200"),
)

_PROBLEM_MEDIA = "application/problem+json"


@cache
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

    expected = {(method, path) for method, path, _success in _EXPECTED_OPERATIONS}
    actual = {
        (method, path)
        for path, path_item in paths.items()
        for method in path_item
        if method in api_schema._HTTP_METHODS
    }
    assert actual == expected


def test_proposal_endpoints_do_not_retain_drifted_aliases() -> None:
    paths = _openapi()["paths"]
    assert "/api/v1/generations:propose" not in paths
    assert "/api/v1/constraints:propose" not in paths


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
    session_or_api_key = [{"SessionCookie": []}, {"ApiKeyAuth": []}]
    for method, path, _success in _EXPECTED_OPERATIONS:
        if path == "/api/v1/auth/login":
            continue
        expected_security = (
            [{"SessionCookie": []}] if path == "/api/v1/auth/logout" else session_or_api_key
        )
        assert paths[path][method].get("security") == expected_security, (
            f"{method.upper()} {path} has a drifted security contract"
        )


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


def test_only_exact_execution_profile_binding_reads_add_conflict_response() -> None:
    paths = _openapi()["paths"]
    binding_paths = (
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/constraint-validation-binding",
        "/api/v1/execution-profiles/{profile_id}/versions/{version}/task-suite-derivation-binding",
    )

    for binding_path in binding_paths:
        binding_responses = paths[binding_path]["get"]["responses"]
        assert binding_responses["409"]["content"][_PROBLEM_MEDIA]["schema"] == {
            "$ref": "#/components/schemas/Problem"
        }

    ordinary_profile_responses = paths[
        "/api/v1/execution-profiles/{profile_id}/versions/{version}"
    ]["get"]["responses"]
    assert "409" not in ordinary_profile_responses


def test_only_request_body_operations_document_wire_size_rejection() -> None:
    for path, path_item in _openapi()["paths"].items():
        for method, operation in path_item.items():
            if method not in api_schema._HTTP_METHODS:
                continue
            response = operation["responses"].get("413")
            assert (response is not None) is ("requestBody" in operation), (
                f"{method.upper()} {path} has a drifted HTTP body-limit contract"
            )
            if response is None:
                continue
            assert response["content"][_PROBLEM_MEDIA]["schema"] == {
                "$ref": "#/components/schemas/Problem"
            }


def test_write_ops_document_idempotency_etag_and_if_match() -> None:
    paths = _openapi()["paths"]
    create_paths = (
        "/api/v1/specs",
        "/api/v1/patches",
        "/api/v1/constraint-proposals",
        "/api/v1/refs/{ref_name}/rollback-requests",
    )
    for path in create_paths:
        create_params = {
            (parameter["name"], parameter["in"]): parameter
            for parameter in paths[path]["post"]["parameters"]
        }
        assert create_params[("Idempotency-Key", "header")]["required"] is True
        assert ("If-Match", "header") not in create_params
        assert create_params[("X-CSRF-Token", "header")]["required"] is False

    versioned_paths = (
        "/api/v1/patches/{artifact_id}:validate",
        "/api/v1/patches/{artifact_id}:submit-for-approval",
        "/api/v1/patches/{artifact_id}:apply",
        "/api/v1/patches/{artifact_id}:rebase",
        "/api/v1/patches/{artifact_id}:resolve-conflicts",
        "/api/v1/constraint-proposals/{artifact_id}:revise",
        "/api/v1/constraint-proposals/{artifact_id}:validate",
        "/api/v1/constraint-proposals/{artifact_id}:submit-for-approval",
        "/api/v1/constraint-proposals/{artifact_id}:publish",
        "/api/v1/approvals/{approval_id}:approve",
        "/api/v1/approvals/{approval_id}:reject",
        "/api/v1/approvals/{approval_id}:request_changes",
        "/api/v1/rollback-requests/{artifact_id}:validate",
        "/api/v1/rollback-requests/{artifact_id}:submit-for-approval",
        "/api/v1/rollback-requests/{artifact_id}:apply",
    )
    for path in versioned_paths:
        versioned_params = {
            (parameter["name"], parameter["in"]): parameter
            for parameter in paths[path]["post"]["parameters"]
        }
        assert versioned_params[("Idempotency-Key", "header")]["required"] is True
        assert versioned_params[("If-Match", "header")]["required"] is True

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

    # REST cancel carries the complete RunCommandV1, including its idempotency key.
    # It must not document the obsolete synthesized-command header contract.
    cancel = paths["/api/v1/runs/{run_id}:cancel"]["post"]
    cancel_params = {(p["name"], p["in"]): p for p in cancel["parameters"]}
    assert ("Idempotency-Key", "header") not in cancel_params
    request_schema = cancel["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["allOf"][0] == {"$ref": "#/components/schemas/RunCommandV1"}
    cancel_constraint = request_schema["allOf"][1]
    assert set(cancel_constraint["required"]) == {"type", "payload_schema_id", "payload"}
    assert cancel_constraint["properties"] == {
        "type": {"const": "cancel"},
        "payload_schema_id": {"const": "run-cancel@1"},
        "payload": {"$ref": "#/components/schemas/CancelRunPayloadV1"},
    }
    for status in ("409", "422"):
        problem_schema = cancel["responses"][status]["content"][_PROBLEM_MEDIA]["schema"]
        assert problem_schema == {"$ref": "#/components/schemas/Problem"}


def test_execution_option_resolver_documents_only_non_safe_method_csrf() -> None:
    operation = _openapi()["paths"]["/api/v1/execution-options:resolve"]["post"]
    parameters = {(item["name"], item["in"]): item for item in operation["parameters"]}

    assert parameters == {
        ("X-CSRF-Token", "header"): {
            **parameters[("X-CSRF-Token", "header")],
            "required": False,
        }
    }
    assert "non-safe HTTP method" in parameters[("X-CSRF-Token", "header")]["description"]
    assert operation["responses"]["200"]["headers"] == {
        "Cache-Control": {
            "description": "Always `private, no-store` for execution options.",
            "schema": {"type": "string"},
        }
    }


def test_bench_report_documents_exact_selected_artifact_identity_without_body_wrapper() -> None:
    response = _openapi()["paths"]["/api/v1/bench/report"]["get"]["responses"]["200"]

    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BenchReport"
    }
    assert response["headers"]["X-Artifact-ID"] == {
        "description": (
            "Exact selected BenchReport Artifact ID; use it with "
            "`/api/v1/artifacts/{artifact_id}` for provenance and lineage."
        ),
        "schema": {"type": "string", "minLength": 1, "maxLength": 512},
    }


def test_run_cost_documents_safe_settlement_state_and_missing_usage_evidence() -> None:
    document = _openapi()
    operation = document["paths"]["/api/v1/cost/{run_id}"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "discriminator": {
            "mapping": {
                "run-cost-view@1": "#/components/schemas/RunCostViewV1",
                "run-cost-view@2": "#/components/schemas/RunCostViewV2",
            },
            "propertyName": "view_schema_version",
        },
        "oneOf": [
            {"$ref": "#/components/schemas/RunCostViewV1"},
            {"$ref": "#/components/schemas/RunCostViewV2"},
        ],
        "title": "Response Get Run Cost Api V1 Cost  Run Id  Get",
    }
    version_parameter = next(
        item for item in operation["parameters"] if item["name"] == "view_schema_version"
    )
    assert version_parameter["in"] == "query"
    assert version_parameter["required"] is False
    assert version_parameter["schema"] == {
        "default": "run-cost-view@1",
        "enum": ["run-cost-view@1", "run-cost-view@2"],
        "title": "View Schema Version",
        "type": "string",
    }
    schemas = document["components"]["schemas"]
    run_cost = schemas["RunCostViewV2"]
    assert run_cost["properties"]["settlement_summary"] == {
        "$ref": "#/components/schemas/CostSettlementSummaryV1"
    }
    assert "settlement_summary" in run_cost["required"]

    summary = schemas["CostSettlementSummaryV1"]
    assert summary["properties"]["usage_evidence_status"] == {
        "type": "string",
        "enum": ["recorded", "not_recorded"],
        "title": "Usage Evidence Status",
    }
    assert summary["properties"]["late_adjustment_usage_count"]["minimum"] == 0
    assert summary["properties"]["held_unknown_group_count"]["minimum"] == 0
    assert summary["properties"]["group_counts"]["maxItems"] == 18

    public_settlement_wire = json.dumps(
        {name: schema for name, schema in schemas.items() if name.startswith("CostSettlement")}
    )
    for internal_field in (
        "reservation_group_id",
        "request_hash",
        "routing_decision_id",
        "fencing_token",
    ):
        assert internal_field not in public_settlement_wire


def test_login_documents_session_cookie_contract() -> None:
    document = _openapi()
    login = document["paths"]["/api/v1/auth/login"]["post"]
    headers = login["responses"]["204"]["headers"]
    assert "Set-Cookie" in headers
    assert "X-CSRF-Token" in headers
    cookie_scheme = document["components"]["securitySchemes"]["SessionCookie"]
    assert cookie_scheme["name"] == "gameforge_session"


def test_logout_and_sse_document_runtime_required_headers_and_errors() -> None:
    paths = _openapi()["paths"]
    logout = paths["/api/v1/auth/logout"]["post"]
    logout_parameters = {(item["name"], item["in"]): item for item in logout["parameters"]}
    assert logout_parameters[("X-CSRF-Token", "header")]["required"] is True
    assert logout_parameters[("Idempotency-Key", "header")]["required"] is True
    assert logout_parameters[("Idempotency-Key", "header")]["schema"]["maxLength"] == 512

    events = paths["/api/v1/runs/{run_id}/events"]["get"]
    event_parameters = {(item["name"], item["in"]): item for item in events["parameters"]}
    cursor = event_parameters[("Last-Event-ID", "header")]
    assert cursor["required"] is False
    assert cursor["schema"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": (1 << 63) - 1,
    }
    assert "400" in events["responses"]


def test_parent_bound_pages_document_not_found() -> None:
    paths = _openapi()["paths"]
    parent_pages = (
        "/api/v1/specs/{artifact_id}/graph",
        "/api/v1/diff",
        "/api/v1/artifacts/{artifact_id}/lineage",
        "/api/v1/refs/{ref_name}/history",
        "/api/v1/runs/{run_id}/findings",
        "/api/v1/runs/{run_id}/finding-links",
        "/api/v1/runs/{run_id}/commands",
        "/api/v1/conflict-sets/{conflict_set_id}/conflicts",
    )
    for path in parent_pages:
        assert "404" in paths[path]["get"]["responses"], path
        assert "410" in paths[path]["get"]["responses"], path


def test_run_finding_link_read_exposes_exact_revision_digest_and_evidence() -> None:
    document = _openapi()
    operation = document["paths"]["/api/v1/runs/{run_id}/finding-links"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]

    schema = document["components"]["schemas"]["RunFindingLinkViewV1"]
    assert set(schema["required"]) == {
        "attempt_no",
        "evidence_artifact_id",
        "finding",
        "finding_digest",
        "ordinal",
        "run_id",
    }
    assert schema["properties"]["view_schema_version"]["const"] == ("run-finding-link-view@1")
    assert schema["properties"]["finding_digest"]["pattern"] == "^[0-9a-f]{64}$"
    assert schema["properties"]["finding"]["$ref"].endswith("/FindingRevisionV1")


def test_large_integer_bounds_remain_exact_in_openapi() -> None:
    schemas = _openapi()["components"]["schemas"]
    expected_client_seq = (1 << 63) - 1
    for name in ("RunCommandV1", "RunCommandAckV1", "RunCommandViewV1"):
        maximum = schemas[name]["properties"]["client_seq"]["maximum"]
        assert type(maximum) is int and maximum == expected_client_seq

    expected_seed = (1 << 64) - 1
    for name in (
        "ConstraintValidationAdmissionRequestV1",
        "PatchRepairRequestV1",
        "PatchValidationAdmissionRequestV1",
        "PlaytestRunRequestV1",
        "RollbackValidationAdmissionRequestV1",
        "RunSubmissionRequestV1",
    ):
        seed_schema = schemas[name]["properties"]["seed"]
        variants = seed_schema.get("anyOf", [seed_schema])
        integer = next(item for item in variants if item.get("type") == "integer")
        assert type(integer["maximum"]) is int and integer["maximum"] == expected_seed


def test_run_command_ack_version_is_a_response_guarantee() -> None:
    schema = _openapi()["components"]["schemas"]["RunCommandAckV1"]
    assert schema["properties"]["ack_schema_version"]["const"] == "run-command-ack@1"
    assert "ack_schema_version" in schema["required"]


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


def test_every_path_and_query_parameter_is_bounded() -> None:
    document = _openapi()
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method not in api_schema._HTTP_METHODS:
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("in") not in {"path", "query"}:
                    continue
                _assert_parameter_schema_bounded(
                    parameter["schema"],
                    document,
                    location=f"{method.upper()} {path} {parameter['name']}",
                )


def _assert_parameter_schema_bounded(
    schema: dict[str, Any],
    document: dict[str, Any],
    *,
    location: str,
) -> None:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        node: Any = document
        for part in ref.removeprefix("#/").split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        _assert_parameter_schema_bounded(node, document, location=location)
        return
    for keyword in ("anyOf", "oneOf"):
        for branch in schema.get(keyword, []):
            _assert_parameter_schema_bounded(branch, document, location=location)

    schema_type = schema.get("type")
    if schema_type == "string" and schema.get("format") != "date-time":
        assert "maxLength" in schema or "enum" in schema or "const" in schema, (
            f"{location} has an unbounded string"
        )
    elif schema_type == "array":
        assert "maxItems" in schema, f"{location} has an unbounded array"
        _assert_parameter_schema_bounded(schema["items"], document, location=location)
    elif schema_type in {"integer", "number"}:
        assert {"minimum", "exclusiveMinimum"} & schema.keys(), f"{location} has no lower bound"
        assert {"maximum", "exclusiveMaximum"} & schema.keys(), f"{location} has no upper bound"


def test_check_rejects_unexpected_frozen_json_artifacts(tmp_path: Path) -> None:
    api_schema.write(tmp_path)
    stale = tmp_path / "schemas" / "stale-v0.json"
    stale.write_text("{}\n", encoding="utf-8")
    assert f"UNEXPECTED {stale.relative_to(tmp_path).as_posix()}" in api_schema.check(tmp_path)


def test_write_refuses_a_breaking_frozen_contract_overwrite(tmp_path: Path) -> None:
    api_schema.write(tmp_path)
    target = tmp_path / api_schema.OPENAPI_KEY
    previous = json.loads(target.read_text(encoding="utf-8"))
    previous["paths"]["/api/v1/legacy-contract"] = {
        "get": {"responses": {"200": {"description": "legacy"}}}
    }
    target.write_text(api_schema.serialize(previous), encoding="utf-8")

    with pytest.raises(api_schema.CompatibilityError, match="path_removed"):
        api_schema.write(tmp_path)


# ── compatibility checker ────────────────────────────────────────────────────
def test_compatibility_identical_is_compatible() -> None:
    document = _openapi()
    assert api_schema.check_compatibility(document, copy.deepcopy(document)) == []


def test_compatibility_additive_new_optional_field_is_allowed() -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    new["components"]["schemas"]["RunAcceptedV1"]["properties"]["new_optional"] = {"type": "string"}
    assert api_schema.check_compatibility(old, new) == []


def test_compatibility_respects_request_and_response_required_direction() -> None:
    old = _openapi()

    relaxed_request = copy.deepcopy(old)
    request = relaxed_request["components"]["schemas"]["RunSubmissionRequestV1"]
    request["required"].remove("params")
    assert api_schema.check_compatibility(old, relaxed_request) == []

    additive_response = copy.deepcopy(old)
    response = additive_response["components"]["schemas"]["RunViewV1"]
    response["properties"]["new_guaranteed_field"] = {"type": "string"}
    response["required"].append("new_guaranteed_field")
    assert api_schema.check_compatibility(old, additive_response) == []

    weakened_response = copy.deepcopy(old)
    weakened_response["components"]["schemas"]["RunViewV1"]["required"].remove("run_id")
    breaks = api_schema.check_compatibility(old, weakened_response)
    assert any(change.kind == "response_required_removed" for change in breaks)


def test_compatibility_permits_stronger_response_bound() -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    new["components"]["schemas"]["RunAcceptedV1"]["properties"]["run_id"]["maxLength"] = 256

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


@pytest.mark.parametrize(
    "case",
    (
        "required_parameter_removed",
        "required_parameter_added",
        "request_body_removed",
        "request_schema_changed",
        "request_allof_branch_removed",
        "response_media_removed",
        "response_schema_changed",
        "response_header_removed",
        "security_alternative_removed",
        "public_security_required",
        "parameter_serialization_changed",
    ),
)
def test_compatibility_rejects_breaking_operation_contract_changes(case: str) -> None:
    old = _openapi()
    new = copy.deepcopy(old)

    if case in {"required_parameter_removed", "required_parameter_added"}:
        operation = new["paths"]["/api/v1/patches/{artifact_id}:validate"]["post"]
        if case == "required_parameter_removed":
            operation["parameters"] = [
                parameter
                for parameter in operation["parameters"]
                if (parameter.get("name"), parameter.get("in")) != ("Idempotency-Key", "header")
            ]
        else:
            operation["parameters"].append(
                {
                    "name": "X-New-Required",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                }
            )
    elif case in {"request_body_removed", "request_schema_changed"}:
        operation = new["paths"]["/api/v1/runs"]["post"]
        if case == "request_body_removed":
            del operation["requestBody"]
        else:
            operation["requestBody"]["content"]["application/json"]["schema"] = {
                "$ref": "#/components/schemas/Problem"
            }
    elif case == "request_allof_branch_removed":
        operation = new["paths"]["/api/v1/runs/{run_id}:cancel"]["post"]
        operation["requestBody"]["content"]["application/json"]["schema"]["allOf"].pop()
    elif case in {
        "response_media_removed",
        "response_schema_changed",
        "response_header_removed",
    }:
        if case == "response_header_removed":
            response = new["paths"]["/api/v1/patches/{artifact_id}:validate"]["post"]["responses"][
                "202"
            ]
            del response["headers"]["ETag"]
        else:
            response = new["paths"]["/api/v1/runs/{run_id}"]["get"]["responses"]["200"]
            if case == "response_media_removed":
                del response["content"]["application/json"]
            else:
                response["content"]["application/json"]["schema"] = {
                    "$ref": "#/components/schemas/Problem"
                }
    elif case == "security_alternative_removed":
        operation = new["paths"]["/api/v1/runs/{run_id}"]["get"]
        operation["security"] = [
            requirement for requirement in operation["security"] if "ApiKeyAuth" not in requirement
        ]
    elif case == "public_security_required":
        new["paths"]["/api/v1/auth/login"]["post"]["security"] = [{"SessionCookie": []}]
    else:
        operation = new["paths"]["/api/v1/runs"]["get"]
        cursor = next(
            parameter for parameter in operation["parameters"] if parameter["name"] == "cursor"
        )
        cursor["style"] = "spaceDelimited"

    breaks = api_schema.check_compatibility(old, new)
    assert breaks, f"{case} must be rejected as an operation-level breaking change"


@pytest.mark.parametrize(
    "case",
    (
        "union_removed",
        "inline_union_variant_removed",
        "discriminator_mapping_target_changed",
        "max_length_tightened",
        "property_name_bound_tightened",
        "response_pattern_value_widened",
        "write_only_removed",
        "unique_items_added",
        "referenced_component_removed",
        "enum_introduced",
        "additional_properties_schema_narrowed",
    ),
)
def test_compatibility_rejects_breaking_schema_contract_changes(case: str) -> None:
    old = _openapi()
    new = copy.deepcopy(old)
    schemas = new["components"]["schemas"]

    if case == "union_removed":
        del schemas["RunSubmissionRequestV1"]["properties"]["params"]["oneOf"]
    elif case == "inline_union_variant_removed":
        nullable_run_id = schemas["Problem"]["properties"]["run_id"]
        nullable_run_id["anyOf"] = nullable_run_id["anyOf"][:1]
    elif case == "discriminator_mapping_target_changed":
        mapping = schemas["RunSubmissionRequestV1"]["properties"]["params"]["discriminator"][
            "mapping"
        ]
        variant = next(iter(mapping))
        mapping[variant] = "#/components/schemas/Problem"
    elif case == "max_length_tightened":
        schemas["HumanSpecUploadRequestV1"]["properties"]["ref_name"]["maxLength"] = 10
    elif case == "property_name_bound_tightened":
        content = schemas["HumanSpecUploadRequestV1"]["properties"]["content_payload"]
        content["propertyNames"]["maxLength"] = 1
    elif case == "response_pattern_value_widened":
        labels = schemas["MetricSeriesV1"]["properties"]["labels"]
        pattern = next(iter(labels["patternProperties"]))
        labels["patternProperties"][pattern] = {}
    elif case == "write_only_removed":
        del schemas["PasswordAuthRequestV1"]["properties"]["password"]["writeOnly"]
    elif case == "unique_items_added":
        field = schemas["HumanPatchDraftRequestV1"]["properties"]["expected_to_fix"]
        field["uniqueItems"] = True
    elif case == "referenced_component_removed":
        del schemas["Problem"]
    elif case == "enum_introduced":
        schemas["RunAcceptedV1"]["properties"]["run_id"]["enum"] = ["run:only"]
    else:
        content = schemas["HumanSpecUploadRequestV1"]["properties"]["content_payload"]
        content["additionalProperties"] = {"type": "string"}

    breaks = api_schema.check_compatibility(old, new)
    assert breaks, f"{case} must be rejected as a schema-level breaking change"


def test_compatibility_checks_ref_siblings_inside_recursive_schemas() -> None:
    old = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://gameforge.dev/api/schemas/ws-client-command-v1.json",
        "$ref": "#/$defs/Node",
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            }
        },
    }
    new = copy.deepcopy(old)
    new["$defs"]["Node"]["properties"]["child"]["maxProperties"] = 1
    assert api_schema.check_compatibility(old, new)


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
