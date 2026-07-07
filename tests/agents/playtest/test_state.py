from gameforge.agents.playtest.state import abstract_state
from gameforge.contracts.env_types import Observation


def test_abstract_state_is_compact_deterministic_and_covers_progress():
    obs = Observation(
        tick=3, player_pos=(1, 2), active_quests=["q1"], known_quests=["q1"],
        completed_quests=[], quest_state={"q1": {"status": "active", "step_kind": "collect", "step_id": "s2"}},
        reachable_targets=["npc:qi", "src:herb"], available_interactions=["npc:qi"],
        inventory={"item:herb": 1}, hp=30, nearby_entities=["npc:qi"],
        last_action_result="arrived", logs=["a", "b", "c", "d", "e", "f"],
    )
    s = abstract_state(obs)
    assert abstract_state(obs) == s                 # deterministic
    assert "q1" in s and "collect" in s and "npc:qi" in s and "src:herb" in s
    assert "tick=3" in s
    assert "f" in s and "a" not in s.split("logs")[-1]  # only last 5 logs (a dropped)
