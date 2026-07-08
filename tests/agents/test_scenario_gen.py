"""M2b-1b: deterministic quest-chain scenario generator.

TDD anchors for `gameforge.agents.scenario_gen.generate_chains`:
  1. exactly `n` snapshots, pairwise GENUINELY distinct (non-empty graph diff)
  2. determinism: same `(seed, n)` -> identical `snapshot_id`s
  3. buildability: every snapshot -> `snapshot_to_world` -> `AureusEnv` w/o raising
  4. completability: chains driven to `done` by the deterministic `ScriptedDriver`
     (the validity anchor for the ≥20-chain Playtest completion denominator)
  5. length variety: the set contains a LONG chain (>=60 atomic driver actions)
     and a SHORT chain (<20) so the corpus spans the harness length buckets.
"""

from __future__ import annotations

from gameforge.agents.scenario_gen import generate_chain, generate_chains
from gameforge.apps.cli.driver import ScriptedDriver
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.ir import NodeType
from gameforge.game.aureus.kernel import AureusEnv


def _structural_signature(snapshot) -> tuple:
    """A hashable structural fingerprint of a generated chain, deliberately
    ignoring ids/positions/gold/stat VALUES (which vary per chain purely by
    construction — disjoint id namespaces — even when the underlying shape is
    identical). Two chains that are "distinct" only modulo ids collapse to the
    SAME signature here, which is exactly the defect this test guards against.
    """
    g = snapshot.to_graph()
    num_quests = len(g.nodes_of_type(NodeType.QUEST))
    steps = g.nodes_of_type(NodeType.QUEST_STEP)
    step_kinds = [s.attrs.get("kind") for s in steps]
    num_fights = sum(1 for k in step_kinds if k == "fight")
    region = g.nodes_of_type(NodeType.REGION)[0]
    grid = region.attrs.get("grid", {})
    return (
        num_quests,
        len(steps),
        grid.get("width"),
        grid.get("height"),
        num_fights,
        tuple(sorted(step_kinds)),
    )


def _drive(snapshot) -> tuple[bool, int]:
    """Build the world, drive it with the ScriptedDriver, return
    (all_quests_completed, atomic_action_count). Mirrors `run_slice`."""
    world = snapshot_to_world(snapshot)
    env = AureusEnv(world)
    env.reset(world.scenario.scenario_id, 0)
    result = ScriptedDriver(world).run(env)
    return env._all_quests_completed(), len(result["trajectory"])


def test_generate_chains_count() -> None:
    chains = generate_chains(0, 20)
    assert len(chains) == 20


def test_generate_chains_pairwise_distinct() -> None:
    chains = generate_chains(0, 20)
    graphs = [c.to_graph() for c in chains]
    for i in range(len(graphs)):
        for j in range(i + 1, len(graphs)):
            diff = graphs[i].diff(graphs[j])
            assert diff.is_empty() is False, f"chains {i} and {j} are not distinct"


def test_generate_chains_structurally_distinct() -> None:
    """Stronger than `test_generate_chains_pairwise_distinct`: a non-empty
    graph diff is satisfied trivially by disjoint ids alone, so it does not
    catch chains that are structurally identical modulo ids (the defect a
    review found among the original 20: chains 3/15, 9/12, 6/18 shared a
    signature). Require all 20 structural signatures to be pairwise unique."""
    chains = generate_chains(0, 20)
    sigs = [_structural_signature(c) for c in chains]
    dupes = {
        sig: [i for i, s in enumerate(sigs) if s == sig]
        for sig in set(sigs)
        if sigs.count(sig) > 1
    }
    assert len(set(sigs)) == len(chains), f"duplicate structural signatures: {dupes}"


def test_determinism_same_seed_same_snapshot_ids() -> None:
    a = generate_chains(0, 5)
    b = generate_chains(0, 5)
    assert [s.snapshot_id for s in a] == [s.snapshot_id for s in b]
    # per-chain constructor is deterministic too
    assert generate_chain(7, 3).snapshot_id == generate_chain(7, 3).snapshot_id


def test_different_seed_differs() -> None:
    a = generate_chains(0, 5)
    b = generate_chains(1, 5)
    assert [s.snapshot_id for s in a] != [s.snapshot_id for s in b]


def test_every_snapshot_is_buildable() -> None:
    for snap in generate_chains(0, 20):
        world = snapshot_to_world(snap)  # must not raise
        AureusEnv(world)  # must construct


def test_generated_chains_are_completable() -> None:
    chains = generate_chains(0, 20)
    completed = [c for c in chains if _drive(c)[0]]
    # Validity anchor: the whole point is a legitimate completion denominator.
    assert len(completed) >= 3
    # We intend EVERY generated chain to be genuinely playable.
    assert len(completed) == len(chains), (
        f"only {len(completed)}/{len(chains)} generated chains completed"
    )


def test_length_variety() -> None:
    chains = generate_chains(0, 20)
    action_counts = [_drive(c)[1] for c in chains]
    assert max(action_counts) >= 60, f"no long chain (>=60 actions): {action_counts}"
    assert min(action_counts) < 20, f"no short chain (<20 actions): {action_counts}"
