"""Tests for `gameforge.spine.sim.economy` (M1 Task 8).

Two inline scenarios built directly from Entity/Relation +
`Snapshot.from_entities_relations`:

  * `_balanced_snapshot()` — a monster whose per-kill gold roughly matches a
    shop sink's price/buy-probability, plus a gacha pool whose expectation is
    comfortably under its pity threshold and a monotonic equipment curve.
    Expected: no collapse, every invariant holds, seed-reproducible.
  * `_collapse_snapshot()` — a monster with a huge gold_min/gold_max and no
    sink at all (source >>> sink). Expected: `detect_collapse` reproduces a
    collapse with an early_warning_tick strictly before the collapse_tick,
    and `to_findings` yields an `economy_collapse` simulation Finding.
"""

from __future__ import annotations

from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.sim.economy import (
    EconomyModel,
    EconomySimulator,
    detect_collapse,
    to_findings,
)


def _snap(entities, relations) -> Snapshot:
    return Snapshot.from_entities_relations(entities, relations)


def _balanced_snapshot() -> Snapshot:
    entities = [
        Entity(id="gold", type=NodeType.CURRENCY, attrs={"output_rate_cap": 50.0}),
        Entity(id="m1", type=NodeType.MONSTER,
               attrs={"gold_min": 5, "gold_max": 9, "kills_per_tick": 1}),
        Entity(id="shop1", type=NodeType.SHOP, attrs={}),
        Entity(id="potion", type=NodeType.ITEM, attrs={}),
        Entity(id="pool1", type=NodeType.GACHA_POOL,
               attrs={"base_rate": 0.06, "pity_threshold": 90,
                      "cost_per_draw": 3, "draw_prob": 0.0}),
        Entity(id="eq_t1", type=NodeType.EQUIPMENT, attrs={"tier": 1, "power": 10}),
        Entity(id="eq_t2", type=NodeType.EQUIPMENT, attrs={"tier": 2, "power": 20}),
        Entity(id="eq_t3", type=NodeType.EQUIPMENT, attrs={"tier": 3, "power": 35}),
    ]
    relations = [
        Relation(id="r_drop_gold", type=EdgeType.DROPS_FROM, src_id="m1", dst_id="gold"),
        Relation(id="r_sell_potion", type=EdgeType.SELLS, src_id="shop1", dst_id="potion",
                  attrs={"price": 7, "currency": "gold", "buy_prob": 1.0}),
    ]
    return _snap(entities, relations)


def _collapse_snapshot() -> Snapshot:
    entities = [
        Entity(id="gold", type=NodeType.CURRENCY, attrs={"output_rate_cap": 50.0}),
        Entity(id="m1", type=NodeType.MONSTER,
               attrs={"gold_min": 500, "gold_max": 1000, "kills_per_tick": 1}),
    ]
    relations = [
        Relation(id="r_drop_gold", type=EdgeType.DROPS_FROM, src_id="m1", dst_id="gold"),
    ]
    return _snap(entities, relations)


def test_balanced_economy_has_no_collapse_and_is_seed_reproducible():
    model = EconomyModel.from_snapshot(_balanced_snapshot())
    a = EconomySimulator().run(model, seed=1, n_agents=50, n_ticks=200)
    b = EconomySimulator().run(model, seed=1, n_agents=50, n_ticks=200)

    assert a.distributions == b.distributions  # replay-reproducible
    assert detect_collapse(a) is None
    assert all(inv.ok for inv in a.invariants), [
        (inv.name, inv.observed, inv.threshold) for inv in a.invariants if not inv.ok
    ]


def test_balanced_economy_different_seed_can_differ():
    model = EconomyModel.from_snapshot(_balanced_snapshot())
    a = EconomySimulator().run(model, seed=1, n_agents=50, n_ticks=200)
    c = EconomySimulator().run(model, seed=2, n_agents=50, n_ticks=200)
    # Not required to differ in every field, but the raw draw sequence should
    # not be seed-independent — the two trajectories should not be identical.
    assert a.distributions != c.distributions


def test_reproduces_one_collapse_with_early_warning():
    model = EconomyModel.from_snapshot(_collapse_snapshot())
    res = EconomySimulator().run(model, seed=1, n_agents=50, n_ticks=200)

    rep = detect_collapse(res)
    assert rep is not None
    assert rep.early_warning_tick < rep.collapse_tick

    fs = to_findings(res, "sha256:collapse-snap")
    assert any(
        f.defect_class == "economy_collapse" and f.oracle_type == "simulation"
        for f in fs
    )


def test_collapse_is_seed_reproducible():
    model = EconomyModel.from_snapshot(_collapse_snapshot())
    a = EconomySimulator().run(model, seed=7, n_agents=20, n_ticks=100)
    b = EconomySimulator().run(model, seed=7, n_agents=20, n_ticks=100)
    assert a.distributions == b.distributions
    rep_a, rep_b = detect_collapse(a), detect_collapse(b)
    assert rep_a is not None and rep_b is not None
    assert rep_a.collapse_tick == rep_b.collapse_tick
    assert rep_a.early_warning_tick == rep_b.early_warning_tick


def test_to_findings_never_gives_prescriptive_numbers():
    model = EconomyModel.from_snapshot(_collapse_snapshot())
    res = EconomySimulator().run(model, seed=1, n_agents=50, n_ticks=200)
    fs = to_findings(res, "sha256:collapse-snap")
    assert fs
    for f in fs:
        assert f.source == "sim"
        assert f.producer_id == "economy_sim"
        assert f.oracle_type == "simulation"
        # Descriptive-only: no "change X to Y" prescriptive phrasing.
        assert "change" not in f.message.lower() or "no prescriptive" in f.message.lower()


def test_collapse_finding_carries_faucet_entities_when_model_given():
    # A repair agent must be able to target the runaway faucet; the collapse
    # finding therefore names its source (and sink) entities when the model is
    # supplied. Without a model, entities stay empty (backward-compatible).
    model = EconomyModel.from_snapshot(_collapse_snapshot())
    res = EconomySimulator().run(model, seed=1, n_agents=50, n_ticks=200)

    with_model = next(
        f for f in to_findings(res, "sha256:collapse-snap", model=model)
        if f.defect_class == "economy_collapse"
    )
    producers = {s["producer"] for s in model.sources}
    assert producers  # the collapse snapshot has at least one faucet
    assert producers.issubset(set(with_model.entities))

    without_model = next(
        f for f in to_findings(res, "sha256:collapse-snap")
        if f.defect_class == "economy_collapse"
    )
    assert without_model.entities == []
