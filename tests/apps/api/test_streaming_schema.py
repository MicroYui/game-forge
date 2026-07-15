"""RED→GREEN: frozen SSE/WS JSON Schemas are byte-stable, safe, and validating.

These schemas are the versioned transport contract for the streaming surfaces:

* ``schemas/sse-run-event-v1.json``      — the ``data:`` payload object of an SSE ``RunEvent``
* ``schemas/ws-server-frame-v1.json``    — ``RunCommandServerFrame`` (ack | problem) ``oneOf``
* ``schemas/ws-client-command-v1.json``  — the ``RunCommandV1`` client command frame
* ``schemas/run-cancel-request-v1.json`` — the ``RunCancelRequestV1`` REST cancel body

They must never leak a lease/fencing/secret worker field, must regenerate byte-for-byte,
and a real DTO instance must validate against its own exported schema.
"""

from __future__ import annotations

import json
from typing import Any

from gameforge.apps.api import schema as api_schema
from gameforge.contracts.jobs import (
    CancelRunPayloadV1,
    CommandAcceptedDataV1,
    Problem,
    RunCommandAckV1,
    RunCommandProblemV1,
    RunCommandV1,
    RunEvent,
)
from gameforge.contracts.api import RunCancelRequestV1


_FORBIDDEN_FIELD_SUBSTRINGS = (
    "claimed_fencing_token",
    "fencing_token",
    "claimed_attempt_no",
    "claimed_at",
    "lease_id",
    "handler_key",
    "secret_plaintext",
    "get_secret_value",
)

_SSE = "schemas/sse-run-event-v1.json"
_WS_SERVER = "schemas/ws-server-frame-v1.json"
_WS_CLIENT = "schemas/ws-client-command-v1.json"
_REST_CANCEL = "schemas/run-cancel-request-v1.json"


# ── a tiny draft-2020-12 subset validator (jsonschema is not a dependency) ──────
def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    assert ref.startswith("#/"), ref
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _validate(instance: Any, schema: dict[str, Any], root: dict[str, Any], loc: str = "$") -> None:
    if "$ref" in schema:
        _validate(instance, _resolve(schema["$ref"], root), root, loc)
        return
    if "allOf" in schema:
        for sub in schema["allOf"]:
            _validate(instance, sub, root, loc)
    for key in ("oneOf", "anyOf"):
        if key in schema:
            matched = 0
            errors: list[str] = []
            for sub in schema[key]:
                try:
                    _validate(instance, sub, root, loc)
                    matched += 1
                except AssertionError as exc:  # noqa: PERF203 - test clarity
                    errors.append(str(exc))
            assert matched >= 1, f"{loc}: no {key} branch matched: {errors}"
    if "const" in schema:
        assert instance == schema["const"], f"{loc}: const {schema['const']!r} != {instance!r}"
    if "enum" in schema:
        assert instance in schema["enum"], f"{loc}: {instance!r} not in enum {schema['enum']}"
    schema_type = schema.get("type")
    if schema_type == "object":
        assert isinstance(instance, dict), f"{loc}: expected object, got {type(instance)}"
        for name in schema.get("required", []):
            assert name in instance, f"{loc}: missing required property {name!r}"
        for name, prop_schema in schema.get("properties", {}).items():
            if name in instance and instance[name] is not None:
                _validate(instance[name], prop_schema, root, f"{loc}.{name}")
    elif schema_type == "array":
        assert isinstance(instance, list), f"{loc}: expected array, got {type(instance)}"
        items = schema.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(instance):
                _validate(element, items, root, f"{loc}[{index}]")
    elif schema_type == "string":
        assert isinstance(instance, str), f"{loc}: expected string"
    elif schema_type == "integer":
        assert isinstance(instance, int) and not isinstance(instance, bool), f"{loc}: expected int"


def _blob(document: dict[str, Any]) -> str:
    return json.dumps(document)


def _real_run_event() -> RunEvent:
    return RunEvent(
        run_id="run:stream",
        seq=7,
        event_type="run.command_accepted",
        occurred_at="2026-07-16T00:00:00Z",
        data_schema_version="command-accepted@1",
        data=CommandAcceptedDataV1(command_id="cmd:1", command_type="cancel", command_revision=1),
    )


def _real_ack() -> RunCommandAckV1:
    return RunCommandAckV1(
        command_id="cmd:1",
        client_id="browser:a",
        client_seq=1,
        status="accepted",
        persisted_status="applied",
        command_revision=1,
        run_revision=2,
    )


def _real_problem_frame() -> RunCommandProblemV1:
    return RunCommandProblemV1(
        command_id="cmd:1",
        client_seq=1,
        problem=Problem(
            type="urn:gameforge:problem:conflict",
            title="Conflict",
            status=409,
            detail="The run command is not permitted in the current run state.",
            instance="urn:gameforge:request:req-1",
            code="conflict",
            request_id="req-1",
        ),
    )


def _real_command() -> RunCommandV1:
    return RunCommandV1(
        command_id="cmd:1",
        client_id="browser:a",
        client_seq=1,
        idempotency_key="idem-1",
        expected_run_revision=1,
        type="cancel",
        payload_schema_id="run-cancel@1",
        payload=CancelRunPayloadV1(reason_code="user_requested"),
    )


def _real_cancel_request() -> RunCancelRequestV1:
    return RunCancelRequestV1(
        command_id="cmd:1",
        client_id="browser:a",
        client_seq=1,
        expected_run_revision=1,
        payload=CancelRunPayloadV1(reason_code="user_requested"),
    )


# ── tests ──────────────────────────────────────────────────────────────────────
def test_all_streaming_schema_files_are_committed_and_byte_stable() -> None:
    generated = api_schema.generate()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT, _REST_CANCEL):
        assert key in generated, f"missing generated artifact {key}"
        frozen_path = api_schema.docs_api_dir() / key
        assert frozen_path.is_file(), f"frozen artifact not committed: {frozen_path}"
        assert frozen_path.read_text(encoding="utf-8") == api_schema.serialize(generated[key])


def test_streaming_schemas_regenerate_byte_stable() -> None:
    first = api_schema.generate()
    second = api_schema.generate()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT, _REST_CANCEL):
        assert api_schema.serialize(first[key]) == api_schema.serialize(second[key])


def test_streaming_schemas_are_versioned() -> None:
    generated = api_schema.generate()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT, _REST_CANCEL):
        document = generated[key]
        assert document.get("$schema"), f"{key}: missing $schema"
        assert document.get("$id", "").endswith(key.split("/")[-1]), f"{key}: $id not versioned"


def test_sse_event_schema_validates_a_real_event() -> None:
    document = api_schema.generate()[_SSE]
    instance = json.loads(_real_run_event().model_dump_json())
    _validate(instance, document, document)
    # The discriminated RunEventData union must expose the 14-value RunEventType too.
    blob = _blob(document)
    for event_type in ("run.queued", "run.succeeded", "run.failed", "attempt.leased"):
        assert event_type in blob


def test_ws_server_frame_schema_is_a_oneof_of_both_variants() -> None:
    document = api_schema.generate()[_WS_SERVER]
    assert "oneOf" in document, "server frame must be a oneOf of ack|problem"
    _validate(json.loads(_real_ack().model_dump_json()), document, document)
    _validate(json.loads(_real_problem_frame().model_dump_json()), document, document)


def test_ws_client_command_schema_validates_a_real_command() -> None:
    document = api_schema.generate()[_WS_CLIENT]
    _validate(json.loads(_real_command().model_dump_json()), document, document)


def test_rest_cancel_request_schema_validates_a_real_body() -> None:
    document = api_schema.generate()[_REST_CANCEL]
    _validate(json.loads(_real_cancel_request().model_dump_json()), document, document)


def test_no_fencing_or_secret_fields_in_streaming_schemas() -> None:
    generated = api_schema.generate()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT, _REST_CANCEL):
        blob = _blob(generated[key])
        for needle in _FORBIDDEN_FIELD_SUBSTRINGS:
            assert needle not in blob, f"{key} leaks forbidden field {needle!r}"


def test_compatibility_permits_identical_streaming_schema() -> None:
    document = api_schema.generate()[_SSE]
    assert api_schema.check_compatibility(document, json.loads(json.dumps(document))) == []


def test_compatibility_rejects_run_event_data_discriminator_change() -> None:
    old = api_schema.generate()[_SSE]
    new = json.loads(json.dumps(old))
    # RunEventData is a Field(discriminator="data_schema_version") union under `data`.
    mapping = new["properties"]["data"]["discriminator"]["mapping"]
    mapping.pop(next(iter(mapping)))
    breaks = api_schema.check_compatibility(old, new)
    assert any(
        b.kind in ("discriminator_variant_removed", "discriminator_changed") for b in breaks
    ), breaks


def test_compatibility_rejects_run_command_payload_variant_removed() -> None:
    old = api_schema.generate()[_WS_CLIENT]
    new = json.loads(json.dumps(old))
    payload = new["properties"]["payload"]
    payload["discriminator"]["mapping"].pop(next(iter(payload["discriminator"]["mapping"])))
    breaks = api_schema.check_compatibility(old, new)
    assert breaks, "removing a RunCommandPayload variant must be breaking"
