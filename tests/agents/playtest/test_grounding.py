from gameforge.agents.playtest.grounding import ground_target, make_unreachable_finding
from gameforge.contracts.env_types import Observation
from gameforge.contracts.world import GridSpec
from gameforge.game.aureus.grid import AureusNav, Grid


def _obs(player_pos=(0, 0), reachable_targets=None):
    return Observation(tick=0, player_pos=player_pos, reachable_targets=reachable_targets or [])


def test_reachable_target_via_nav_bfs_continues():
    # Open 5x5 grid, no walls: npc has a clear path from the player.
    grid = Grid(GridSpec(width=5, height=5, blocked=[]))
    nav = AureusNav(grid, {"npc:reachable": (3, 3)})
    obs = _obs(player_pos=(0, 0))

    verdict = ground_target("npc:reachable", obs, nav)

    assert verdict.target == "npc:reachable"
    assert verdict.oracle_reachable is True
    assert verdict.action == "continue"


def test_walled_off_target_aborts_quest():
    # A solid wall column at x=1 splits the grid: (2, 0) is unreachable from (0, 0).
    grid = Grid(GridSpec(width=3, height=3, blocked=[(1, 0), (1, 1), (1, 2)]))
    nav = AureusNav(grid, {"npc:walled": (2, 0)})
    obs = _obs(player_pos=(0, 0))

    verdict = ground_target("npc:walled", obs, nav)

    assert verdict.oracle_reachable is False
    assert verdict.action == "abort_quest"

    finding = make_unreachable_finding("npc:walled", "sha256:s")
    assert finding.defect_class == "unreachable_target"
    assert finding.oracle_type == "deterministic"
    assert finding.source == "playtest"
    assert finding.status == "confirmed"
    assert finding.entities == ["npc:walled"]


def test_target_with_no_nav_position_and_absent_from_observation_aborts():
    grid = Grid(GridSpec(width=3, height=3, blocked=[]))
    nav = AureusNav(grid, {})  # target has no known position at all
    obs = _obs(player_pos=(0, 0), reachable_targets=[])

    verdict = ground_target("npc:ghost", obs, nav)

    assert verdict.oracle_reachable is False
    assert verdict.action == "abort_quest"


def test_target_in_live_observation_without_nav_position_still_continues():
    # reachable_targets is the authoritative live view: even if the nav layer
    # has no static position on file for this target, the observation says
    # it is currently reachable, so the oracle must trust it.
    grid = Grid(GridSpec(width=3, height=3, blocked=[]))
    nav = AureusNav(grid, {})
    obs = _obs(player_pos=(0, 0), reachable_targets=["npc:ghost"])

    verdict = ground_target("npc:ghost", obs, nav)

    assert verdict.oracle_reachable is True
    assert verdict.action == "continue"


def test_ground_target_signature_takes_no_llm_belief_input():
    # The guarantee IS the signature: no llm/belief parameter exists for the
    # oracle to be swayed by, so it is structurally impossible for an LLM's
    # opinion to influence the verdict.
    import inspect

    params = list(inspect.signature(ground_target).parameters)
    assert params == ["target", "obs", "nav"]
