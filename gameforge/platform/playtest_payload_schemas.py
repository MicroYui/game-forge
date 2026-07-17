"""Versioned, registry-selected validation for reset, action, and oracle payloads.

The schema id carried by an environment/oracle is immutable authority, while the
validator implementation is selected only through the trusted component map.  No
handler branches on a game name or accepts a process-local default schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pydantic import BaseModel, JsonValue, ValidationError

from gameforge.contracts.canonical import canonical_json, typed_canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.playtest import (
    BoundedProgressCompletionParamsV1,
    GenericEnvironmentActionPayloadV1,
    GenericEnvironmentResetPayloadV1,
    PlaytestPayloadContextBindingV1,
    PlaytestPayloadSchemaPurposeV1,
    StatePredicateCompletionParamsV1,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry


@dataclass(frozen=True, slots=True)
class ExactModelPayloadValidator:
    schema_id: str
    purpose: PlaytestPayloadSchemaPurposeV1
    model: type[BaseModel]

    def validate(self, payload: JsonValue) -> JsonValue:
        try:
            parsed = self.model.model_validate(payload)
        except ValidationError as exc:
            raise IntegrityViolation(
                "playtest payload does not match its exact schema",
                schema_id=self.schema_id,
            ) from exc
        return parsed.model_dump(mode="json", exclude_none=True)


def build_builtin_playtest_payload_validators() -> dict[str, ExactModelPayloadValidator]:
    """Return the complete executable map for the built-in schema registry."""

    return {
        "generic_env_reset_payload@1": ExactModelPayloadValidator(
            schema_id="generic-env-reset@1",
            purpose="scenario_reset",
            model=GenericEnvironmentResetPayloadV1,
        ),
        "generic_env_action_payload@1": ExactModelPayloadValidator(
            schema_id="generic-env-action@1",
            purpose="environment_action",
            model=GenericEnvironmentActionPayloadV1,
        ),
        "state_predicate_params@1": ExactModelPayloadValidator(
            schema_id="state-predicate-params@1",
            purpose="completion_oracle_params",
            model=StatePredicateCompletionParamsV1,
        ),
        "bounded_progress_params@1": ExactModelPayloadValidator(
            schema_id="bounded-progress-params@1",
            purpose="completion_oracle_params",
            model=BoundedProgressCompletionParamsV1,
        ),
    }


@dataclass(frozen=True, slots=True)
class PlaytestPayloadValidationService:
    registry: ImmutablePlatformRegistry
    validators: Mapping[str, ExactModelPayloadValidator]

    def validate(
        self,
        *,
        schema_id: str,
        purpose: PlaytestPayloadSchemaPurposeV1,
        payload: JsonValue,
    ) -> JsonValue:
        definition = self.registry.get_playtest_payload_schema(schema_id)
        if definition is None or definition.purpose != purpose:
            raise IntegrityViolation(
                "playtest payload schema is unavailable for its exact purpose",
                schema_id=schema_id,
                purpose=purpose,
            )
        validator = self.validators.get(definition.validator_key)
        if (
            validator is None
            or validator.schema_id != definition.schema_id
            or validator.purpose != definition.purpose
        ):
            raise IntegrityViolation(
                "playtest payload validator does not close its retained schema",
                schema_id=schema_id,
            )
        return validator.validate(payload)

    def validate_exact(
        self,
        *,
        schema_id: str,
        purpose: PlaytestPayloadSchemaPurposeV1,
        payload: JsonValue,
    ) -> JsonValue:
        validated = self.validate(
            schema_id=schema_id,
            purpose=purpose,
            payload=payload,
        )
        _require_exact_canonical_value(
            payload=payload,
            validated=validated,
            schema_id=schema_id,
        )
        return validated

    def validate_contextual(
        self,
        *,
        schema_id: str,
        purpose: PlaytestPayloadSchemaPurposeV1,
        payload: JsonValue,
        context: Mapping[str, JsonValue],
    ) -> JsonValue:
        """Validate shape plus the retained schema's exact contextual equalities."""

        validated = self.validate(
            schema_id=schema_id,
            purpose=purpose,
            payload=payload,
        )
        policy = self.registry.get_playtest_payload_context_policy(schema_id)
        _validate_contextual_bindings(
            payload=validated,
            context=context,
            bindings=policy.contextual_bindings if policy is not None else (),
            schema_id=schema_id,
        )
        return validated

    def validate_exact_contextual(
        self,
        *,
        schema_id: str,
        purpose: PlaytestPayloadSchemaPurposeV1,
        payload: JsonValue,
        context: Mapping[str, JsonValue],
    ) -> JsonValue:
        """Validate contextual authority and reject any non-canonical coercion."""

        validated = self.validate_contextual(
            schema_id=schema_id,
            purpose=purpose,
            payload=payload,
            context=context,
        )
        _require_exact_canonical_value(
            payload=payload,
            validated=validated,
            schema_id=schema_id,
        )
        return validated


def _require_exact_canonical_value(
    *,
    payload: JsonValue,
    validated: JsonValue,
    schema_id: str,
) -> None:
    try:
        # Artifact blobs use the repository's historical canonical wire
        # representation (including ``f:<decimal>`` float tags).  Comparing
        # canonical wire bytes preserves exact schema shape while allowing a
        # schema-aware validator to restore that one numeric representation.
        if canonical_json(validated) != canonical_json(payload):
            raise IntegrityViolation(
                "playtest payload is not the exact canonical schema value",
                schema_id=schema_id,
            )
    except (TypeError, ValueError) as exc:
        raise IntegrityViolation(
            "playtest payload cannot be canonically validated",
            schema_id=schema_id,
        ) from exc


def _resolve_json_pointer(*, payload: JsonValue, pointer: str, schema_id: str) -> JsonValue:
    current: JsonValue = payload
    for encoded_token in pointer[1:].split("/"):
        token = encoded_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise IntegrityViolation(
                    "playtest payload contextual binding does not resolve",
                    schema_id=schema_id,
                    payload_pointer=pointer,
                )
            current = current[token]
            continue
        if isinstance(current, list):
            if (
                not token.isascii()
                or not token.isdecimal()
                or (len(token) > 1 and token.startswith("0"))
            ):
                raise IntegrityViolation(
                    "playtest payload contextual binding has an invalid array index",
                    schema_id=schema_id,
                    payload_pointer=pointer,
                )
            index = int(token)
            if index >= len(current):
                raise IntegrityViolation(
                    "playtest payload contextual binding does not resolve",
                    schema_id=schema_id,
                    payload_pointer=pointer,
                )
            current = current[index]
            continue
        raise IntegrityViolation(
            "playtest payload contextual binding traverses a scalar",
            schema_id=schema_id,
            payload_pointer=pointer,
        )
    return current


def _validate_contextual_bindings(
    *,
    payload: JsonValue,
    context: Mapping[str, JsonValue],
    bindings: tuple[PlaytestPayloadContextBindingV1, ...],
    schema_id: str,
) -> None:
    expected_keys = {binding.context_key for binding in bindings}
    if any(not isinstance(key, str) for key in context):
        raise IntegrityViolation(
            "playtest payload contextual binding keys must be strings",
            schema_id=schema_id,
        )
    actual_keys = set(context)
    if actual_keys != expected_keys:
        raise IntegrityViolation(
            "playtest payload contextual binding key set does not close exactly",
            schema_id=schema_id,
            missing_context_keys=sorted(expected_keys - actual_keys),
            extra_context_keys=sorted(actual_keys - expected_keys),
        )
    for binding in bindings:
        actual = _resolve_json_pointer(
            payload=payload,
            pointer=binding.payload_pointer,
            schema_id=schema_id,
        )
        try:
            # Context authority must not inherit Python equality aliases such as
            # ``True == 1`` or conflate integer, float, string, and null values.
            equal = typed_canonical_json(actual) == typed_canonical_json(
                context[binding.context_key]
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "playtest payload contextual binding is not canonical JSON",
                schema_id=schema_id,
                context_key=binding.context_key,
            ) from exc
        if not equal:
            raise IntegrityViolation(
                "playtest payload contextual binding does not match trusted context",
                schema_id=schema_id,
                context_key=binding.context_key,
                payload_pointer=binding.payload_pointer,
            )


__all__ = [
    "ExactModelPayloadValidator",
    "PlaytestPayloadValidationService",
    "build_builtin_playtest_payload_validators",
]
