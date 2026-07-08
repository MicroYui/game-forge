"""Deterministic quest-chain scenario generator (M2b-1b).

Produces GENUINELY distinct, ScriptedDriver-completable quest-chain IR snapshots
of varied length so the Playtest regression harness (`agents.playtest_harness`)
runs against a ≥20-chain corpus instead of the two hand-authored scenarios.

Design principle (do NOT invent a new world format): every emitted snapshot
mirrors the EXACT IR shape that `apps.cli.ir_to_world.snapshot_to_world` reads
and that the `ScriptedDriver` completes for the caravan/outpost templates —
a primary `REGION` carrying grid metadata, positioned `NPC`/`INTERACTABLE`/
`BATTLE_ENCOUNTER` placements, `QUEST` + ordered `QUEST_STEP`s wired by
`HAS_STEP`/`PRECEDES`, and `MONSTER`/`DROP_TABLE` content for fight steps.
Only the specifics vary (quest count, per-quest step shape, ids, grid positions,
collect counts, monster stats), driven by a per-chain seeded `random.Random`.

Determinism: `generate_chain(seed, index)` seeds `random.Random(seed*1000+index)`
and emits entities/relations in a fixed order, so `(seed, n)` fully determines
every snapshot (positions, ids, and thus `snapshot_id`). No LLM, stdlib RNG only.

Genuine structural distinctness (not just distinct ids): `num_quests` is a
DETERMINISTIC function of `index` (`_quest_count`, not an RNG draw from a tiny
per-class pool), assigned from globally disjoint per-length-class ranges — so
`num_quests` alone is unique across every chain in a `generate_chains(seed, n)`
corpus (`n<=20`). Since a chain's structural signature (quest count, total step
count, grid dims, fight count, step-kind multiset) starts with `num_quests`,
a globally-unique quest count is sufficient to make every signature pairwise
distinct by construction, regardless of which per-quest shape the RNG then
picks. Every length class draws from the SAME shape pool (`_SHAPES`, including
`fight`) so short chains get the same per-quest variety as long ones; only
`_spacing`/grid layout and the quest-count range differ by class.

This module lives under `agents` (not `game`) because it imports the spine
`Snapshot`; the `game` layer is forbidden from importing `spine` (import-linter
contract 4), whereas `agents → spine` is allowed. It reaches no LLM SDK.
"""

from __future__ import annotations

import math
import random

from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.snapshot import Snapshot

_ADAPTER_HINT = "m2b-scenario-gen"

# Per-quest step shapes. Every quest opens with `talk` (to the giver, which both
# accepts and completes the accept-step, exactly as the kernel expects) and ends
# with `turn_in` (to the same giver) — the completability invariant the
# ScriptedDriver relies on. The middle is what varies. Shared by every length
# class (short chains get the full mix too — see module docstring).
_SHAPES = (
    ("talk", "turn_in"),
    ("talk", "collect", "turn_in"),
    ("talk", "fight", "turn_in"),
    ("talk", "collect", "fight", "turn_in"),
)

# Globally-disjoint per-class quest-count ranges (see module docstring): with
# `index // 3` (a chain's position within its own length-class bucket) added
# to the class base, `n=20` maps to exactly one chain per count in 1..20 —
# short 1..7, medium 8..14, long 15..20 — so `num_quests` is unique per chain.
_QUEST_COUNT_BASE = {"short": 1, "medium": 8, "long": 15}


class _Builder:
    """Accumulates entities/relations with stable, unique relation ids."""

    def __init__(self) -> None:
        self.entities: list[Entity] = []
        self.relations: list[Relation] = []
        self._n = 0

    def entity(self, id: str, type: NodeType, attrs: dict) -> None:
        self.entities.append(Entity(id=id, type=type, attrs=attrs))

    def relation(self, etype: EdgeType, src: str, dst: str, attrs: dict | None = None) -> None:
        rid = f"rel:{etype.value}:{src}->{dst}:{self._n}"
        self._n += 1
        self.relations.append(
            Relation(id=rid, type=etype, src_id=src, dst_id=dst, attrs=attrs)
        )


def _length_class(index: int) -> str:
    """Length mix by index so a 20-chain corpus spans the harness buckets."""
    return ("short", "medium", "long")[index % 3]


def _quest_count(index: int, klass: str) -> int:
    """Deterministic (not RNG-drawn) quest count from a per-class range that
    is globally disjoint across classes — see module docstring. `cycle` is
    this chain's position within its own class's bucket (0-based)."""
    cycle = index // 3
    return _QUEST_COUNT_BASE[klass] + cycle


def _spacing(klass: str) -> int:
    # Larger spacing => longer navigation => more atomic actions per quest.
    return {"short": 2, "medium": 3, "long": 4}[klass]


def _giver_pos(q: int, cols: int, gap: int) -> tuple[int, int]:
    return (1 + (q % cols) * gap, 1 + (q // cols) * gap)


def generate_chain(seed: int, index: int) -> Snapshot:
    """Build ONE valid, completable quest-chain snapshot deterministically.

    Every id embeds `(seed, index, quest)` so any two generated chains have
    disjoint id sets (guaranteeing a non-empty graph diff), while `num_quests`
    is a deterministic, globally-unique-per-index function (`_quest_count`) —
    so the structure itself, not just the ids, differs chain to chain.
    """
    rng = random.Random(seed * 1000 + index)
    klass = _length_class(index)
    k = _quest_count(index, klass)
    gap = _spacing(klass)
    cols = max(1, math.ceil(math.sqrt(k)))
    shapes = _SHAPES

    b = _Builder()
    region_id = f"region:{seed}_{index}"
    scenario_id = f"pt_{seed}_{index}"

    all_positions: list[tuple[int, int]] = [(0, 0)]

    for q in range(k):
        gx, gy = _giver_pos(q, cols, gap)
        all_positions.append((gx, gy))
        giver_id = f"npc:{seed}_{index}_{q}"
        quest_id = f"quest:{seed}_{index}_{q}"
        reward_item = f"item:{seed}_{index}_{q}_reward"
        reward_gold = rng.randint(20, 120)

        # --- giver NPC ---
        b.entity(
            giver_id,
            NodeType.NPC,
            {"name": f"Giver {q}", "pos": [gx, gy], "region": region_id},
        )
        b.relation(EdgeType.LOCATED_IN, giver_id, region_id)

        # --- reward item ---
        b.entity(reward_item, NodeType.ITEM, {"name": f"Reward {q}"})

        # --- quest ---
        b.entity(
            quest_id,
            NodeType.QUEST,
            {
                "title": f"Chain {seed}/{index} Quest {q}",
                "region": region_id,
                "giver": giver_id,
                "reward": {"gold": reward_gold, "item": reward_item},
            },
        )
        b.relation(EdgeType.STARTS_AT, quest_id, giver_id)
        b.relation(EdgeType.REWARDS, quest_id, reward_item)

        shape = rng.choice(shapes)
        prev_step: str | None = None
        for si, kind in enumerate(shape):
            step_id = f"step:{seed}_{index}_{q}_{si}"
            attrs: dict = {"kind": kind}
            if kind in ("talk", "turn_in"):
                attrs["target"] = giver_id
            elif kind == "collect":
                item_id = f"item:{seed}_{index}_{q}_c"
                count = rng.randint(1, 4)
                sx, sy = gx + 1, gy
                all_positions.append((sx, sy))
                # gather source (INTERACTABLE) + the collected ITEM
                b.entity(item_id, NodeType.ITEM, {"name": f"Collectible {q}"})
                source_id = f"gather:{seed}_{index}_{q}"
                b.entity(
                    source_id,
                    NodeType.INTERACTABLE,
                    {
                        "kind": "gather",
                        "pos": [sx, sy],
                        "region": region_id,
                        "yields_item": item_id,
                        "yields_count": count,
                    },
                )
                b.relation(EdgeType.GRANTS, source_id, item_id)
                attrs["item"] = item_id
                attrs["count"] = count
            elif kind == "fight":
                ex, ey = gx, gy + 1
                all_positions.append((ex, ey))
                monster_id = f"mon:{seed}_{index}_{q}"
                enc_id = f"enc:{seed}_{index}_{q}"
                drop_table_id = f"dt:{seed}_{index}_{q}"
                drop_item = f"item:{seed}_{index}_{q}_drop"
                # weak, always-beatable monster (the kernel has no player death,
                # and damage is `max(1, atk-def)`, so any monster is killable
                # within the driver's combat budget — kept light regardless).
                hp = rng.choice((15, 20, 25, 30))
                b.entity(
                    monster_id,
                    NodeType.MONSTER,
                    {
                        "name": f"Beast {q}",
                        "stats": {"hp": hp, "atk": rng.randint(2, 6), "def": rng.randint(0, 3)},
                        "skills": [],
                        "drop_table_id": drop_table_id,
                        "ai": "aggressive",
                    },
                )
                b.entity(drop_item, NodeType.ITEM, {"name": f"Loot {q}"})
                b.entity(
                    drop_table_id,
                    NodeType.DROP_TABLE,
                    {"entries": [{"item": drop_item, "probability": 1.0}]},
                )
                b.relation(EdgeType.DROPS_FROM, monster_id, drop_item)
                b.entity(
                    enc_id,
                    NodeType.BATTLE_ENCOUNTER,
                    {
                        "monsters": [monster_id],
                        "reward": {"gold": rng.randint(10, 60)},
                        "pos": [ex, ey],
                    },
                )
                attrs["encounter"] = enc_id

            b.entity(step_id, NodeType.QUEST_STEP, attrs)
            b.relation(EdgeType.HAS_STEP, quest_id, step_id)
            if prev_step is not None:
                b.relation(EdgeType.PRECEDES, prev_step, step_id)
            if kind in ("talk", "turn_in"):
                b.relation(EdgeType.TALKS_TO, step_id, giver_id)
            elif kind == "collect":
                b.relation(EdgeType.REQUIRES, step_id, attrs["item"])
            prev_step = step_id

    # --- primary region carries grid metadata (must be big enough + walkable) ---
    dim = max(max(x, y) for x, y in all_positions) + 2
    b.entity(
        region_id,
        NodeType.REGION,
        {
            "name": f"Zone {seed}/{index}",
            "grid": {"width": dim, "height": dim, "blocked": []},
            "start_pos": [0, 0],
            "scenario_id": scenario_id,
        },
    )

    return Snapshot.from_entities_relations(b.entities, b.relations)


def generate_chains(seed: int, n: int = 20) -> list[Snapshot]:
    """`n` deterministic, distinct, completable quest-chain snapshots."""
    return [generate_chain(seed, i) for i in range(n)]
