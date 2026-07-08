"""Deterministic state abstraction: Observation -> compact reasoning text.

Pure function (no LLM, no RNG) so a playtest run stays byte-reproducible under
cassette REPLAY. Compresses the decision-relevant slice of the Observation.
"""
from __future__ import annotations

from gameforge.contracts.env_types import Observation


_FIGHT_PROTOCOL_HINT = (
    "FIGHT PROTOCOL: to defeat a pending_fight_target, first navigate_to it "
    "until last_action_result is 'arrived' (you must stand on its tile), THEN "
    "attack it until 'victory'; last_action_result 'not_in_combat' means you "
    "are NOT yet on its tile — navigate_to it first."
)


def abstract_state(obs: Observation) -> str:
    lines = [f"tick={obs.tick} pos={obs.player_pos} hp={obs.hp}"]
    lines.append(f"active_quests={obs.active_quests} known={obs.known_quests} done={obs.completed_quests}")
    any_fight_step = False
    for qid, st in sorted(obs.quest_state.items()):
        lines.append(f"quest {qid}: status={st.get('status')} step_kind={st.get('step_kind')} step_id={st.get('step_id')}")
        if st.get("step_kind") == "fight":
            any_fight_step = True
    lines.append(f"reachable_targets={obs.reachable_targets}")
    lines.append(f"available_interactions={obs.available_interactions}")
    lines.append(f"pending_fight_targets={obs.pending_fight_targets}")
    lines.append(f"inventory={dict(sorted(obs.inventory.items()))}")
    lines.append(f"nearby={obs.nearby_entities}")
    lines.append(f"last_action_result={obs.last_action_result}")
    lines.append(f"logs={obs.logs[-5:]}")
    if obs.pending_fight_targets or any_fight_step:
        lines.append(_FIGHT_PROTOCOL_HINT)
    return "\n".join(lines)
