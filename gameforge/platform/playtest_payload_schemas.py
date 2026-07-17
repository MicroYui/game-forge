"""Versioned, registry-selected validation for reset and oracle JSON payloads.

The schema id carried by an environment/oracle is immutable authority, while the
validator implementation is selected only through the trusted component map.  No
handler branches on a game name or accepts a process-local default schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pydantic import BaseModel, JsonValue, ValidationError

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.playtest import (
    BoundedProgressCompletionParamsV1,
    GenericEnvironmentResetPayloadV1,
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
        return parsed.model_dump(mode="json")


def build_builtin_playtest_payload_validators() -> dict[str, ExactModelPayloadValidator]:
    """Return the complete executable map for the built-in schema registry."""

    return {
        "generic_env_reset_payload@1": ExactModelPayloadValidator(
            schema_id="generic-env-reset@1",
            purpose="scenario_reset",
            model=GenericEnvironmentResetPayloadV1,
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
        return validated


__all__ = [
    "ExactModelPayloadValidator",
    "PlaytestPayloadValidationService",
    "build_builtin_playtest_payload_validators",
]
