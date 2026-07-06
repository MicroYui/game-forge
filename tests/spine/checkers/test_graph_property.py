"""Property tests (M1 Task 4 / §12A.1): hand-rolled graph algorithms cross-checked
against independent naive reference implementations on random graphs.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from gameforge.spine.checkers.graph import find_cycles, reachable_set


def _naive_has_cycle(adj: dict) -> bool:
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict = {}

    def dfs(u):
        color[u] = GRAY
        for v in adj.get(u, []):
            if color.get(v, WHITE) == GRAY:
                return True
            if color.get(v, WHITE) == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    return any(color.get(u, WHITE) == WHITE and dfs(u) for u in list(adj))


def _naive_reachable(adj: dict, src) -> set:
    """Independent recursive-DFS reference (reachable_set under test is BFS)."""
    seen: set = set()

    def dfs(u):
        if u in seen:
            return
        seen.add(u)
        for v in adj.get(u, []):
            dfs(v)

    dfs(src)
    return seen


_adj_strategy = st.dictionaries(
    st.integers(0, 8), st.lists(st.integers(0, 8), max_size=4), max_size=9
)


@given(_adj_strategy)
def test_cycle_detection_matches_naive(adj):
    assert bool(find_cycles(adj)) == _naive_has_cycle(adj)


@given(_adj_strategy, st.integers(0, 8))
def test_reachable_set_matches_naive_dfs(adj, src):
    assert reachable_set(adj, src) == _naive_reachable(adj, src)
