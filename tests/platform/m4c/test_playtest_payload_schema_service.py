from __future__ import annotations

from dataclasses import replace

import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.playtest import (
    GenericEnvironmentResetPayloadV1,
    PlaytestPayloadContextBindingV1,
    PlaytestPayloadContextPolicyV1,
    PlaytestPayloadSchemaDefinitionV1,
    PlaytestPayloadSchemaRegistryV1,
    compute_playtest_payload_schema_registry_digest,
)
from gameforge.platform.playtest_payload_schemas import (
    ExactModelPayloadValidator,
    PlaytestPayloadValidationService,
    build_builtin_playtest_payload_validators,
)
from gameforge.platform.registry import (
    PlatformReadinessValidator,
    build_builtin_registry,
    build_readiness_component_maps,
)


_HISTORICAL_V1_DIGEST = "278c22f784608bf3c02bc474312bc41282227efeb75eb6a30ee2461668bf1c57"


def _service() -> PlaytestPayloadValidationService:
    return PlaytestPayloadValidationService(
        registry=build_builtin_registry(),
        validators=build_builtin_playtest_payload_validators(),
    )


def test_builtin_action_schema_is_retained_and_uses_an_executable_trusted_validator() -> None:
    registry = build_builtin_registry()
    definition = registry.get_playtest_payload_schema("generic-env-action@1")
    validators = build_builtin_playtest_payload_validators()

    assert definition == PlaytestPayloadSchemaDefinitionV1(
        schema_id="generic-env-action@1",
        purpose="environment_action",
        validator_key="generic_env_action_payload@1",
    )
    validator = validators[definition.validator_key]
    assert validator.schema_id == definition.schema_id
    assert validator.purpose == definition.purpose
    assert _service().validate_exact(
        schema_id=definition.schema_id,
        purpose=definition.purpose,
        payload={"kind": "wait", "ticks": 0},
    ) == {"kind": "wait", "ticks": 0}


def test_builtin_registry_preserves_v1_and_adds_action_context_authority_in_v2() -> None:
    registries = build_builtin_registry().list_playtest_payload_schema_registries()

    assert tuple(item.registry_version for item in registries) == (1, 2)
    assert registries[0].registry_digest == _HISTORICAL_V1_DIGEST
    assert {item.schema_id for item in registries[0].definitions} == {
        "generic-env-reset@1",
        "state-predicate-params@1",
        "bounded-progress-params@1",
    }
    assert {item.schema_id for item in registries[1].definitions} == {
        "generic-env-reset@1",
        "generic-env-action@1",
        "state-predicate-params@1",
        "bounded-progress-params@1",
    }
    assert tuple(item.schema_id for item in registries[1].context_policies) == (
        "generic-env-reset@1",
    )


@pytest.mark.parametrize(
    "payload",
    (
        {"kind": "observe", "forged": True},
        {"kind": "navigate_to", "target": ""},
        {"kind": "buy", "shop_id": "shop:1", "item_id": "item:1", "count": 0},
        {"kind": "wait", "ticks": 1_000_001},
    ),
)
def test_builtin_action_validator_fails_closed(payload: dict[str, object]) -> None:
    with pytest.raises(IntegrityViolation, match="exact schema"):
        _service().validate_exact(
            schema_id="generic-env-action@1",
            purpose="environment_action",
            payload=payload,
        )


def test_generic_reset_context_is_selected_by_retained_schema_metadata() -> None:
    registry = build_builtin_registry()
    policy = registry.get_playtest_payload_context_policy("generic-env-reset@1")
    assert policy is not None
    assert policy.contextual_bindings == (
        PlaytestPayloadContextBindingV1(
            context_key="expected_config_export_artifact_id",
            payload_pointer="/config_export_artifact_id",
        ),
        PlaytestPayloadContextBindingV1(
            context_key="expected_scenario_id",
            payload_pointer="/scenario_id",
        ),
    )
    payload = {
        "scenario_id": "scenario:quest-main",
        "config_export_artifact_id": "artifact:config",
        "quest_ids": ["quest:main"],
        "start_seed": 0,
    }

    assert (
        _service().validate_exact_contextual(
            schema_id="generic-env-reset@1",
            purpose="scenario_reset",
            payload=payload,
            context={
                "expected_scenario_id": "scenario:quest-main",
                "expected_config_export_artifact_id": "artifact:config",
            },
        )
        == payload
    )

    with pytest.raises(IntegrityViolation, match="contextual binding"):
        _service().validate_exact_contextual(
            schema_id="generic-env-reset@1",
            purpose="scenario_reset",
            payload=payload,
            context={
                "expected_scenario_id": "scenario:other",
                "expected_config_export_artifact_id": "artifact:config",
            },
        )
    with pytest.raises(IntegrityViolation, match="contextual binding"):
        _service().validate_exact_contextual(
            schema_id="generic-env-reset@1",
            purpose="scenario_reset",
            payload=payload,
            context={"expected_scenario_id": "scenario:quest-main"},
        )
    with pytest.raises(IntegrityViolation, match="contextual binding"):
        _service().validate_exact_contextual(
            schema_id="generic-env-reset@1",
            purpose="scenario_reset",
            payload=payload,
            context={
                "expected_scenario_id": "scenario:quest-main",
                "expected_config_export_artifact_id": "artifact:config",
                "unexpected": "forged",
            },
        )


def test_readiness_requires_the_environment_action_validator_key() -> None:
    registry = build_builtin_registry()
    components = build_readiness_component_maps(registry)
    missing_action = {
        key: value
        for key, value in components.playtest_payload_validators.items()
        if key != "generic_env_action_payload@1"
    }

    with pytest.raises(IntegrityViolation, match="playtest payload validator"):
        PlatformReadinessValidator(
            registry=registry,
            components=replace(
                components,
                playtest_payload_validators=missing_action,
            ),
        ).validate()


class _PayloadSchemaRegistryOverride:
    def __init__(
        self,
        source: object,
        payload_registry: PlaytestPayloadSchemaRegistryV1,
    ) -> None:
        self._source = source
        self._payload_registry = payload_registry

    def list_playtest_payload_schema_registries(
        self,
    ) -> tuple[PlaytestPayloadSchemaRegistryV1, ...]:
        return (self._payload_registry,)

    def get_playtest_payload_schema_registry(
        self,
        registry_version: int,
        registry_digest: str,
    ) -> PlaytestPayloadSchemaRegistryV1 | None:
        if (
            registry_version == self._payload_registry.registry_version
            and registry_digest == self._payload_registry.registry_digest
        ):
            return self._payload_registry
        return None

    def get_playtest_payload_schema(
        self,
        schema_id: str,
    ) -> PlaytestPayloadSchemaDefinitionV1 | None:
        return next(
            (
                definition
                for definition in self._payload_registry.definitions
                if definition.schema_id == schema_id
            ),
            None,
        )

    def get_playtest_payload_context_policy(
        self,
        schema_id: str,
    ) -> PlaytestPayloadContextPolicyV1 | None:
        return next(
            (
                policy
                for policy in self._payload_registry.context_policies
                if policy.schema_id == schema_id
            ),
            None,
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self._source, name)


def test_readiness_rejects_environment_action_schema_with_wrong_purpose() -> None:
    registry = build_builtin_registry()
    original = registry.list_playtest_payload_schema_registries()[-1]
    definitions = tuple(
        definition.model_copy(update={"purpose": "completion_oracle_params"})
        if definition.schema_id == "generic-env-action@1"
        else definition
        for definition in original.definitions
    )
    payload = {
        "registry_version": original.registry_version,
        "definitions": definitions,
        "context_policies": original.context_policies,
    }
    malformed = PlaytestPayloadSchemaRegistryV1(
        **payload,
        registry_digest=compute_playtest_payload_schema_registry_digest(payload),
    )

    with pytest.raises(IntegrityViolation, match="action schema"):
        PlatformReadinessValidator(
            registry=_PayloadSchemaRegistryOverride(registry, malformed),  # type: ignore[arg-type]
            components=build_readiness_component_maps(registry),
        ).validate()


def test_readiness_rejects_generic_reset_without_frozen_context_bindings() -> None:
    registry = build_builtin_registry()
    original = registry.list_playtest_payload_schema_registries()[-1]
    payload = {
        "registry_version": original.registry_version,
        "definitions": original.definitions,
        "context_policies": (),
    }
    malformed = PlaytestPayloadSchemaRegistryV1(
        **payload,
        registry_digest=compute_playtest_payload_schema_registry_digest(payload),
    )

    with pytest.raises(IntegrityViolation, match="contextual bindings"):
        PlatformReadinessValidator(
            registry=_PayloadSchemaRegistryOverride(registry, malformed),  # type: ignore[arg-type]
            components=build_readiness_component_maps(registry),
        ).validate()


def test_reset_binding_payload_hash_remains_canonical_after_context_validation() -> None:
    payload = {
        "scenario_id": "scenario:quest-main",
        "config_export_artifact_id": "artifact:config",
        "quest_ids": [],
        "start_seed": 0,
    }
    validated = _service().validate_exact_contextual(
        schema_id="generic-env-reset@1",
        purpose="scenario_reset",
        payload=payload,
        context={
            "expected_scenario_id": "scenario:quest-main",
            "expected_config_export_artifact_id": "artifact:config",
        },
    )

    assert canonical_sha256(validated) == canonical_sha256(payload)


class _SinglePayloadSchemaRegistry:
    def __init__(
        self,
        definition: PlaytestPayloadSchemaDefinitionV1,
        context_policy: PlaytestPayloadContextPolicyV1,
    ) -> None:
        self._definition = definition
        self._context_policy = context_policy

    def get_playtest_payload_schema(
        self,
        schema_id: str,
    ) -> PlaytestPayloadSchemaDefinitionV1 | None:
        return self._definition if self._definition.schema_id == schema_id else None

    def get_playtest_payload_context_policy(
        self,
        schema_id: str,
    ) -> PlaytestPayloadContextPolicyV1 | None:
        return self._context_policy if self._context_policy.schema_id == schema_id else None


def test_contextual_equality_does_not_alias_boolean_and_integer_values() -> None:
    definition = PlaytestPayloadSchemaDefinitionV1(
        schema_id="typed-context-reset@1",
        purpose="scenario_reset",
        validator_key="typed_context_reset@1",
    )
    context_policy = PlaytestPayloadContextPolicyV1(
        schema_id=definition.schema_id,
        contextual_bindings=(
            PlaytestPayloadContextBindingV1(
                context_key="expected_seed",
                payload_pointer="/start_seed",
            ),
        ),
    )
    service = PlaytestPayloadValidationService(
        registry=_SinglePayloadSchemaRegistry(  # type: ignore[arg-type]
            definition,
            context_policy,
        ),
        validators={
            "typed_context_reset@1": ExactModelPayloadValidator(
                schema_id=definition.schema_id,
                purpose=definition.purpose,
                model=GenericEnvironmentResetPayloadV1,
            )
        },
    )
    payload = {
        "scenario_id": "scenario:quest-main",
        "config_export_artifact_id": "artifact:config",
        "quest_ids": [],
        "start_seed": 0,
    }

    with pytest.raises(IntegrityViolation, match="contextual binding"):
        service.validate_exact_contextual(
            schema_id=definition.schema_id,
            purpose=definition.purpose,
            payload=payload,
            context={"expected_seed": False},
        )
