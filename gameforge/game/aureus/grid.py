"""Deterministic grid navigation + spatial nav derived view (Aureus M0a).

BFS 4-neighbour pathfinding with a fixed neighbour order (N, E, S, W) so the
shortest path is deterministic. `AureusNav` satisfies the spine `NavProvider`
shape by structural duck-typing (game must not import spine).
"""

from __future__ import annotations

from collections import deque

from gameforge.contracts.world import GridSpec

Pos = tuple[int, int]
# Fixed neighbour order → deterministic tie-break: North, East, South, West.
_NEIGHBOURS = ((0, -1), (1, 0), (0, 1), (-1, 0))


class Grid:
    def __init__(self, spec: GridSpec) -> None:
        self.width = spec.width
        self.height = spec.height
        self.blocked: set[Pos] = {(int(x), int(y)) for x, y in spec.blocked}

    def in_bounds(self, pos: Pos) -> bool:
        x, y = pos
        return 0 <= x < self.width and 0 <= y < self.height

    def is_walkable(self, pos: Pos) -> bool:
        return self.in_bounds(pos) and (pos[0], pos[1]) not in self.blocked

    def shortest_path(self, src: Pos, dst: Pos) -> list[Pos] | None:
        src, dst = (int(src[0]), int(src[1])), (int(dst[0]), int(dst[1]))
        if not self.is_walkable(src) or not self.is_walkable(dst):
            return None
        if src == dst:
            return [src]
        parent: dict[Pos, Pos] = {src: src}
        q: deque[Pos] = deque([src])
        while q:
            cur = q.popleft()
            if cur == dst:
                break
            for dx, dy in _NEIGHBOURS:
                nxt = (cur[0] + dx, cur[1] + dy)
                if nxt in parent or not self.is_walkable(nxt):
                    continue
                parent[nxt] = cur
                q.append(nxt)
        if dst not in parent:
            return None
        path: list[Pos] = [dst]
        while path[-1] != src:
            path.append(parent[path[-1]])
        path.reverse()
        return path


class AureusNav:
    """Spatial derived view (spine.ir.store.NavProvider shape)."""

    def __init__(self, grid: Grid, positions: dict[str, Pos]) -> None:
        self._grid = grid
        self._pos = {k: (int(v[0]), int(v[1])) for k, v in positions.items()}

    def pos_of(self, entity_id: str) -> Pos | None:
        return self._pos.get(entity_id)

    def reachable(self, src_pos: Pos, dst_pos: Pos) -> bool:
        return self._grid.shortest_path(src_pos, dst_pos) is not None
