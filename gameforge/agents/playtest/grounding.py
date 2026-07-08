"""Verifier-grounding (PRD §7.8): deterministic reachability oracle overrides
any LLM belief about whether a target is reachable / a quest is dead.

`ground_target` deliberately takes NO llm/belief argument at all — the absence
of that parameter IS the guarantee. There is no channel through which an LLM's
opinion about reachability could reach this function; the verdict can only
ever be computed from the live `Observation` (`reachable_targets`, the
authoritative current view) and the deterministic BFS grid oracle
(`AureusNav`). Pure, no LLM, no RNG.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from gameforge.contracts.env_types import Observation
from gameforge.contracts.findings import Finding
from gameforge.game.aureus.grid import AureusNav


@dataclass
class GroundedVerdict:
    target: str
    oracle_reachable: bool
    action: Literal["continue", "abort_quest"]


def ground_target(target: str, obs: Observation, nav: AureusNav) -> GroundedVerdict:
    """Ground `target`'s reachability against the deterministic oracle only.

    `reachable_targets` is the authoritative live view (trusted even without a
    static nav position). Otherwise fall back to a BFS path check from the
    player's current position via `AureusNav`. No nav position at all (and not
    already in the live view) means not reachable.
    """
    oracle_reachable = target in obs.reachable_targets
    if not oracle_reachable:
        pos = nav.pos_of(target)
        oracle_reachable = pos is not None and nav.reachable(obs.player_pos, pos)

    action: Literal["continue", "abort_quest"] = "continue" if oracle_reachable else "abort_quest"
    return GroundedVerdict(target=target, oracle_reachable=oracle_reachable, action=action)


def make_unreachable_finding(target: str, snapshot_id: str) -> Finding:
    """Build the confirmed `unreachable_target` Finding for an oracle-dead target."""
    return Finding(
        id=f"playtest-unreachable:{target}",
        source="playtest",
        producer_id="playtest.grounding",
        producer_run_id="playtest.grounding",
        oracle_type="deterministic",
        defect_class="unreachable_target",
        severity="major",
        snapshot_id=snapshot_id,
        entities=[target],
        status="confirmed",
        message=(
            f"Target '{target}' is unreachable per the deterministic nav oracle "
            "(BFS grid reachability); quest aborted."
        ),
    )
