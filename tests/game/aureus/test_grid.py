from gameforge.contracts.world import GridSpec
from gameforge.game.aureus.grid import AureusNav, Grid


def test_shortest_path_length_and_determinism():
    g = Grid(GridSpec(width=5, height=5, blocked=[]))
    p1 = g.shortest_path((0, 0), (2, 0))
    assert p1 == g.shortest_path((0, 0), (2, 0))  # deterministic
    assert p1[0] == (0, 0) and p1[-1] == (2, 0) and len(p1) == 3


def test_blocked_cells_route_around_or_unreachable():
    g = Grid(GridSpec(width=3, height=3, blocked=[[1, 0], [1, 1], [1, 2]]))
    assert g.shortest_path((0, 0), (2, 0)) is None  # wall splits the grid
    assert not g.is_walkable((1, 1))


def test_route_around_partial_wall():
    g = Grid(GridSpec(width=3, height=3, blocked=[[1, 0], [1, 1]]))
    path = g.shortest_path((0, 0), (2, 0))
    assert path is not None and path[0] == (0, 0) and path[-1] == (2, 0)
    # must detour through the open row y=2
    assert (1, 2) in path


def test_same_cell_path_is_singleton():
    g = Grid(GridSpec(width=3, height=3, blocked=[]))
    assert g.shortest_path((1, 1), (1, 1)) == [(1, 1)]


def test_reachable_positions_finds_only_requested_walkable_targets():
    g = Grid(GridSpec(width=3, height=3, blocked=[[1, 0], [1, 1], [1, 2]]))

    assert g.reachable_positions((0, 0), {(0, 0), (0, 2), (2, 0), (1, 1)}) == {
        (0, 0),
        (0, 2),
    }
    assert g.reachable_positions((0, 0), ()) == set()
    assert g.reachable_positions((1, 1), {(0, 0)}) == set()


def test_reachable_positions_stops_after_the_last_sparse_target(monkeypatch):
    g = Grid(GridSpec(width=256, height=256, blocked=[]))
    original = g.is_walkable
    checks = 0

    def counting_is_walkable(pos):
        nonlocal checks
        checks += 1
        return original(pos)

    monkeypatch.setattr(g, "is_walkable", counting_is_walkable)

    assert g.reachable_positions((0, 0), {(1, 0)}) == {(1, 0)}
    assert checks <= 8


def test_nav_provider_reachable_and_pos_of():
    g = Grid(GridSpec(width=5, height=5, blocked=[]))
    nav = AureusNav(g, {"npc:a": (3, 3)})
    assert nav.pos_of("npc:a") == (3, 3)
    assert nav.pos_of("missing") is None
    assert nav.reachable((0, 0), (3, 3)) is True
