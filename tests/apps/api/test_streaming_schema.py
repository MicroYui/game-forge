"""RED→GREEN: frozen SSE/WS JSON Schemas are byte-stable, safe, and validating.

These schemas are the versioned transport contract for the streaming surfaces:

* ``schemas/sse-run-event-v1.json``      — the ``data:`` payload object of an SSE ``RunEvent``
* ``schemas/ws-server-frame-v1.json``    — ``RunCommandServerFrame`` (ack | problem) ``oneOf``
* ``schemas/ws-client-command-v1.json``  — the full ``RunCommandV1`` shared by WS and REST

They must never leak a lease/fencing/secret worker field, must regenerate byte-for-byte,
and a real DTO instance must validate against its own exported schema.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

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

_COMMAND_PAYLOAD_BY_TYPE = {
    "cancel": ("run-cancel@1", "CancelRunPayloadV1"),
    "provide_input": ("playtest-provide-input@1", "PlaytestProvideInputPayloadV1"),
}

_EVENT_SCHEMA_BY_TYPE = {
    "run.queued": ("run-queued@1", "RunQueuedDataV1", "run"),
    "run.cancel_requested": ("cancel-requested@1", "CancelRequestedDataV1", "run"),
    "run.command_accepted": ("command-accepted@1", "CommandAcceptedDataV1", "run"),
    "attempt.leased": ("attempt-leased@1", "AttemptLeasedDataV1", "attempt"),
    "attempt.started": ("attempt-started@1", "AttemptStartedDataV1", "attempt"),
    "attempt.progress": ("attempt-progress@1", "AttemptProgressDataV1", "attempt"),
    "attempt.lease_expired": ("lease-expired@1", "LeaseExpiredDataV1", "attempt"),
    "attempt.retry_scheduled": ("retry-scheduled@1", "RetryScheduledDataV1", "attempt"),
    "run.command_applied": ("command-outcome@1", "CommandOutcomeDataV1", "either"),
    "run.command_rejected": ("command-outcome@1", "CommandOutcomeDataV1", "either"),
    "run.succeeded": ("run-succeeded@1", "RunSucceededDataV1", "attempt"),
    "run.failed": ("run-terminated@1", "RunTerminatedDataV1", "either"),
    "run.cancelled": ("run-terminated@1", "RunTerminatedDataV1", "either"),
    "run.timed_out": ("run-terminated@1", "RunTerminatedDataV1", "either"),
}


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


# ── tests ──────────────────────────────────────────────────────────────────────
def test_all_streaming_schema_files_are_committed_and_byte_stable() -> None:
    generated = api_schema.generate()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT):
        assert key in generated, f"missing generated artifact {key}"
        frozen_path = api_schema.docs_api_dir() / key
        assert frozen_path.is_file(), f"frozen artifact not committed: {frozen_path}"
        assert frozen_path.read_text(encoding="utf-8") == api_schema.serialize(generated[key])


def test_streaming_schemas_regenerate_byte_stable() -> None:
    first = api_schema.generate()
    second = api_schema.generate()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT):
        assert api_schema.serialize(first[key]) == api_schema.serialize(second[key])


def test_streaming_schemas_are_versioned() -> None:
    generated = api_schema.generate()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT):
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


def test_full_command_schema_also_validates_the_rest_cancel_body() -> None:
    document = api_schema.generate()[_WS_CLIENT]
    _validate(json.loads(_real_command().model_dump_json()), document, document)


def test_discriminator_tags_are_required_in_every_streaming_union_variant() -> None:
    generated = api_schema.generate()
    for key, property_name, tag_name in (
        (_SSE, "data", "data_schema_version"),
        (_WS_CLIENT, "payload", "schema_version"),
    ):
        document = generated[key]
        union = document["properties"][property_name]
        mapping = union["discriminator"]["mapping"]
        assert mapping, f"{key}: discriminator mapping must not be empty"
        for tag_value, ref in mapping.items():
            variant = _resolve(ref, document)
            assert tag_name in variant.get("required", []), (
                f"{key}: {tag_value!r} maps to a variant whose discriminator "
                f"{tag_name!r} is optional"
            )
            assert variant["properties"][tag_name]["const"] == tag_value


def test_ws_client_command_schema_closes_type_payload_schema_triples() -> None:
    document = api_schema.generate()[_WS_CLIENT]
    branches = document.get("oneOf")
    assert isinstance(branches, list) and len(branches) == len(_COMMAND_PAYLOAD_BY_TYPE), (
        "RunCommandV1 needs one closed top-level branch per command type; independent "
        "type/payload_schema_id/payload unions accept combinations Pydantic rejects"
    )

    actual: dict[str, tuple[str, str]] = {}
    for branch in branches:
        required = set(branch.get("required", []))
        assert {"type", "payload_schema_id", "payload"} <= required
        properties = branch["properties"]
        command_type = properties["type"]["const"]
        payload_schema_id = properties["payload_schema_id"]["const"]
        payload_ref = properties["payload"]["$ref"]
        actual[command_type] = (payload_schema_id, payload_ref.rsplit("/", 1)[-1])

    assert actual == _COMMAND_PAYLOAD_BY_TYPE


def test_sse_schema_closes_event_type_data_schema_and_attempt_scope() -> None:
    document = api_schema.generate()[_SSE]
    branches = document.get("oneOf")
    assert isinstance(branches, list) and len(branches) == len(_EVENT_SCHEMA_BY_TYPE), (
        "RunEvent needs one closed top-level branch per event type; the independent "
        "event/data schemas do not encode the Pydantic event registry"
    )

    actual: dict[str, tuple[str, str, str]] = {}
    for branch in branches:
        required = set(branch.get("required", []))
        assert {"event_type", "data_schema_version", "data"} <= required
        properties = branch["properties"]
        event_type = properties["event_type"]["const"]
        data_schema_id = properties["data_schema_version"]["const"]
        data_ref = properties["data"]["$ref"]
        expected_scope = _EVENT_SCHEMA_BY_TYPE[event_type][2]
        if expected_scope == "attempt":
            assert "attempt_no" in required, f"{event_type}: attempt number must be required"
        elif expected_scope == "run":
            attempt_schema = properties.get("attempt_no")
            assert isinstance(attempt_schema, dict) and (
                attempt_schema.get("type") == "null"
                or ("const" in attempt_schema and attempt_schema["const"] is None)
            ), f"{event_type}: run-scoped envelope must forbid a positive attempt number"
        actual[event_type] = (data_schema_id, data_ref.rsplit("/", 1)[-1], expected_scope)

    assert actual == _EVENT_SCHEMA_BY_TYPE


def test_no_fencing_or_secret_fields_in_streaming_schemas() -> None:
    generated = api_schema.generate()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT):
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


def test_compatibility_rejects_sse_data_union_variant_removed() -> None:
    old = api_schema.generate()[_SSE]
    new = copy.deepcopy(old)
    new["properties"]["data"]["oneOf"].pop(0)
    assert api_schema.check_compatibility(old, new), (
        "removing a RunEventData oneOf branch must be breaking"
    )


def test_compatibility_rejects_sse_discriminator_mapping_target_change() -> None:
    old = api_schema.generate()[_SSE]
    new = copy.deepcopy(old)
    mapping = new["properties"]["data"]["discriminator"]["mapping"]
    mapping["run-queued@1"] = "#/$defs/CancelRequestedDataV1"
    assert api_schema.check_compatibility(old, new), (
        "retargeting a stable discriminator value to another payload must be breaking"
    )


def test_compatibility_rejects_ws_server_union_variant_removed() -> None:
    old = api_schema.generate()[_WS_SERVER]
    new = copy.deepcopy(old)
    new["oneOf"].pop()
    assert api_schema.check_compatibility(old, new), (
        "removing a WebSocket server-frame variant must be breaking"
    )


def test_compatibility_rejects_ws_client_numeric_bound_narrowing() -> None:
    old = api_schema.generate()[_WS_CLIENT]
    new = copy.deepcopy(old)
    new["properties"]["client_seq"]["maximum"] = 1
    assert api_schema.check_compatibility(old, new), (
        "narrowing a previously accepted RunCommand bound must be breaking"
    )


@pytest.mark.parametrize(
    "case",
    (
        "type_widened",
        "enum_widened",
        "bound_widened",
        "const_removed",
        "additional_properties_opened",
    ),
)
def test_compatibility_rejects_weakened_ws_server_response_guarantees(case: str) -> None:
    old = api_schema.generate()[_WS_SERVER]
    new = copy.deepcopy(old)
    ack = new["$defs"]["RunCommandAckV1"]
    if case == "type_widened":
        ack["properties"]["client_seq"]["type"] = ["integer", "string"]
    elif case == "enum_widened":
        ack["properties"]["status"]["enum"].append("future")
    elif case == "bound_widened":
        ack["properties"]["client_seq"]["maximum"] = (1 << 64) - 1
    elif case == "const_removed":
        del ack["properties"]["ack_schema_version"]["const"]
    else:
        ack["additionalProperties"] = True
    assert api_schema.check_compatibility(old, new), (
        f"{case} must not weaken a server-frame response guarantee"
    )
