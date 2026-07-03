from gameforge.contracts.ir import NodeType
from gameforge.contracts.world import (
    GridSpec, Placement, QuestSpec, QuestStepSpec, ScenarioConfig, WorldConfig,
)


def _wc():
    return WorldConfig(
        scenario=ScenarioConfig(scenario_id="s1", start_pos=(0, 0)),
        grid=GridSpec(width=5, height=5, blocked=[]),
        placements=[Placement(entity_id="npc:a", type=NodeType.NPC, pos=(1, 1), attrs={})],
        quests=[
            QuestSpec(
                quest_id="q1", giver="npc:a",
                steps=[QuestStepSpec(step_id="s1", kind="talk", target="npc:a")],
                reward={"gold": 50},
            )
        ],
    )


def test_worldconfig_env_contract_version_default():
    assert _wc().env_contract_version == "env@1"


def test_worldconfig_roundtrips_via_pydantic():
    wc = _wc()
    again = WorldConfig.model_validate(wc.model_dump())
    assert again.quests[0].steps[0].kind == "talk"
    assert again.grid.width == 5


def test_quest_step_kinds_restricted():
    import pytest
    from pydantic import ValidationError

    for kind in ["talk", "collect", "turn_in"]:
        QuestStepSpec(step_id="s", kind=kind)
    with pytest.raises(ValidationError):
        QuestStepSpec(step_id="s", kind="fight")  # combat step is M0b, not a valid M0a kind
