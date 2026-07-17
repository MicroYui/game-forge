from __future__ import annotations

from pydantic import ValidationError
import pytest

from gameforge.contracts.playtest import (
    GenericEnvironmentActionPayloadV1,
    PlaytestPayloadContextBindingV1,
    PlaytestPayloadContextPolicyV1,
    PlaytestPayloadSchemaDefinitionV1,
    PlaytestPayloadSchemaRegistryV1,
    compute_playtest_payload_schema_registry_digest,
)


_HISTORICAL_V1_DIGEST = "278c22f784608bf3c02bc474312bc41282227efeb75eb6a30ee2461668bf1c57"


def test_historical_payload_schema_registry_v1_wire_remains_readable() -> None:
    historical_wire = {
        "registry_version": 1,
        "definitions": [
            {
                "schema_id": "generic-env-reset@1",
                "purpose": "scenario_reset",
                "validator_key": "generic_env_reset_payload@1",
            },
            {
                "schema_id": "state-predicate-params@1",
                "purpose": "completion_oracle_params",
                "validator_key": "state_predicate_params@1",
            },
            {
                "schema_id": "bounded-progress-params@1",
                "purpose": "completion_oracle_params",
                "validator_key": "bounded_progress_params@1",
            },
        ],
        "registry_digest": _HISTORICAL_V1_DIGEST,
    }

    retained = PlaytestPayloadSchemaRegistryV1.model_validate(historical_wire)

    assert retained.registry_version == 1
    assert retained.registry_digest == _HISTORICAL_V1_DIGEST
    assert "context_policies" not in retained.model_dump(mode="json")


def test_payload_context_policy_is_digest_bound_in_the_additive_registry() -> None:
    definition = PlaytestPayloadSchemaDefinitionV1(
        schema_id="reset@1",
        purpose="scenario_reset",
        validator_key="reset_validator@1",
    )
    policy = PlaytestPayloadContextPolicyV1(
        schema_id=definition.schema_id,
        contextual_bindings=(
            PlaytestPayloadContextBindingV1(
                context_key="scenario",
                payload_pointer="/scenario_id",
            ),
        ),
    )
    payload = {
        "registry_version": 2,
        "definitions": (definition,),
        "context_policies": (policy,),
    }
    digest = compute_playtest_payload_schema_registry_digest(payload)
    PlaytestPayloadSchemaRegistryV1(**payload, registry_digest=digest)

    tampered = {
        **payload,
        "context_policies": (
            policy.model_copy(
                update={
                    "contextual_bindings": (
                        policy.contextual_bindings[0].model_copy(
                            update={"payload_pointer": "/config_export_artifact_id"}
                        ),
                    )
                }
            ),
        ),
    }
    with pytest.raises(ValidationError, match="registry_digest"):
        PlaytestPayloadSchemaRegistryV1(**tampered, registry_digest=digest)


@pytest.mark.parametrize(
    "payload",
    (
        {"kind": "observe"},
        {"kind": "navigate_to", "target": "npc:guide"},
        {"kind": "interact", "target": "interactable:gate"},
        {"kind": "choose", "option_id": "dialogue-option:1"},
        {"kind": "attack", "target_id": "monster:slime"},
        {
            "kind": "cast_skill",
            "skill_id": "skill:fireball",
            "target_id": "monster:slime",
        },
        {"kind": "use", "item_id": "item:potion"},
        {"kind": "use", "item_id": "item:key", "target": "door:1"},
        {"kind": "pickup", "item_id": "item:coin"},
        {"kind": "equip", "item_id": "equipment:sword"},
        {"kind": "buy", "shop_id": "shop:1", "item_id": "item:potion", "count": 1},
        {
            "kind": "sell",
            "shop_id": "shop:1",
            "item_id": "item:potion",
            "count": 1_000_000,
        },
        {"kind": "wait", "ticks": 0},
        {"kind": "wait", "ticks": 1_000_000},
    ),
)
def test_generic_environment_action_payload_accepts_every_frozen_atomic_action(
    payload: dict[str, object],
) -> None:
    parsed = GenericEnvironmentActionPayloadV1.model_validate(payload)

    assert parsed.model_dump(mode="json", exclude_none=True) == payload


@pytest.mark.parametrize(
    "payload",
    (
        {"kind": "observe", "forged": True},
        {"kind": "navigate_to"},
        {"kind": "navigate_to", "target": ""},
        {"kind": "navigate_to", "target": "x" * 513},
        {"kind": "cast_skill", "skill_id": "", "target_id": "monster:slime"},
        {"kind": "use", "item_id": "item:key", "target": ""},
        {"kind": "buy", "shop_id": "shop:1", "item_id": "item:potion", "count": 0},
        {
            "kind": "sell",
            "shop_id": "shop:1",
            "item_id": "item:potion",
            "count": 1_000_001,
        },
        {"kind": "buy", "shop_id": "shop:1", "item_id": "item:potion", "count": True},
        {"kind": "wait", "ticks": -1},
        {"kind": "wait", "ticks": 1_000_001},
        {"kind": "wait", "ticks": False},
        {"kind": "talk", "target": "npc:guide"},
    ),
)
def test_generic_environment_action_payload_rejects_noncanonical_or_unbounded_actions(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        GenericEnvironmentActionPayloadV1.model_validate(payload)


@pytest.mark.parametrize(
    "pointer",
    ("", "/", "scenario_id", "/scenario_id/*", "/scenario_id/~", "/scenario_id/~2"),
)
def test_payload_context_bindings_reject_unsafe_json_pointers(pointer: str) -> None:
    with pytest.raises(ValidationError):
        PlaytestPayloadContextBindingV1(
            context_key="expected_scenario_id",
            payload_pointer=pointer,
        )


def test_payload_context_policy_bindings_are_canonical_and_one_to_one() -> None:
    scenario = PlaytestPayloadContextBindingV1(
        context_key="expected_scenario_id",
        payload_pointer="/scenario_id",
    )
    config = PlaytestPayloadContextBindingV1(
        context_key="expected_config_export_artifact_id",
        payload_pointer="/config_export_artifact_id",
    )
    policy = PlaytestPayloadContextPolicyV1(
        schema_id="generic-env-reset@1",
        contextual_bindings=(scenario, config),
    )

    assert policy.contextual_bindings == (config, scenario)
    with pytest.raises(ValidationError, match="context keys"):
        PlaytestPayloadContextPolicyV1(
            schema_id="reset@1",
            contextual_bindings=(scenario, scenario.model_copy(update={"payload_pointer": "/x"})),
        )
    with pytest.raises(ValidationError, match="context pointers"):
        PlaytestPayloadContextPolicyV1(
            schema_id="reset@1",
            contextual_bindings=(scenario, scenario.model_copy(update={"context_key": "other"})),
        )
