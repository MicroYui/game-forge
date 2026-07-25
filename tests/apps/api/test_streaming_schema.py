"""RED→GREEN: frozen SSE/WS JSON Schemas are byte-stable and structurally closed.

These schemas are the versioned transport contract for the streaming surfaces:

* ``schemas/sse-run-event-v1.json``      — the ``data:`` payload object of an SSE ``RunEvent``
* ``schemas/ws-server-frame-v1.json``    — ``RunCommandServerFrame`` (ack | problem) ``oneOf``
* ``schemas/ws-client-command-v1.json``  — the full ``RunCommandV1`` shared by WS and REST

They must never leak a lease/fencing/secret worker field and must regenerate byte-for-byte.
"""

from __future__ import annotations

from functools import cache
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


def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    assert ref.startswith("#/"), ref
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _blob(document: dict[str, Any]) -> str:
    return json.dumps(document)


@cache
def _schemas() -> dict[str, dict[str, Any]]:
    return api_schema.generate()


# ── tests ──────────────────────────────────────────────────────────────────────
def test_all_streaming_schema_files_are_committed_and_byte_stable() -> None:
    generated = _schemas()
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
    generated = _schemas()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT):
        document = generated[key]
        assert document.get("$schema"), f"{key}: missing $schema"
        assert document.get("$id", "").endswith(key.split("/")[-1]), f"{key}: $id not versioned"


def test_response_schema_version_fields_are_required() -> None:
    generated = _schemas()
    event = generated[_SSE]
    assert event["properties"]["event_schema_version"]["const"] == "run-event@1"
    assert "event_schema_version" in event["required"]

    frame = generated[_WS_SERVER]
    for model, field, value in (
        ("RunCommandAckV1", "ack_schema_version", "run-command-ack@1"),
        ("RunCommandProblemV1", "problem_schema_version", "run-command-problem@1"),
    ):
        schema = frame["$defs"][model]
        assert schema["properties"][field]["const"] == value
        assert field in schema["required"]


def test_ws_server_frame_schema_has_exactly_ack_and_problem_variants() -> None:
    variants = _schemas()[_WS_SERVER]["oneOf"]
    assert {variant["$ref"] for variant in variants} == {
        "#/$defs/RunCommandAckV1",
        "#/$defs/RunCommandProblemV1",
    }


def test_discriminator_tags_are_required_in_every_streaming_union_variant() -> None:
    generated = _schemas()
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
    document = _schemas()[_WS_CLIENT]
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
    document = _schemas()[_SSE]
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
    generated = _schemas()
    for key in (_SSE, _WS_SERVER, _WS_CLIENT):
        blob = _blob(generated[key])
        for needle in _FORBIDDEN_FIELD_SUBSTRINGS:
            assert needle not in blob, f"{key} leaks forbidden field {needle!r}"
