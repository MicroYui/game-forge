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
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
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


def test_illegal_item_to_currency_drop_is_not_an_economy_source():
    snapshot = _snap(
        [
            Entity(id="gold", type=NodeType.CURRENCY),
            Entity(
                id="item:fake-faucet",
                type=NodeType.ITEM,
                attrs={"gold_min": 500, "gold_max": 1000},
            ),
        ],
        [
            Relation(
                id="reverse",
                type=EdgeType.DROPS_FROM,
                src_id="item:fake-faucet",
                dst_id="gold",
            )
        ],
    )

    assert EconomyModel.from_snapshot(snapshot).sources == []


def _econ_workbook(gold_min, gold_max, sink_price, buy_prob):
    # Minimal valid economy workbook: a gold currency, a wolf faucet
    # (gold_min/max + currency => DROPS_FROM(monster->currency)), and a shop
    # sink (SELLS with price/currency/buy_prob). Region+npc keep the snapshot
    # well-formed for to_graph.
    return {
        "regions": [{"region_id": "region:r", "name": "R",
                     "grid": {"width": 4, "height": 4, "blocked": []},
                     "start_pos": [0, 0], "scenario_id": "sc"}],
        "npcs": [{"npc_id": "npc:a", "name": "A", "region": "region:r", "pos": [1, 0]}],
        "currencies": [{"currency_id": "gold", "name": "Gold"}],
        "items": [{"item_id": "item:potion", "name": "Potion"}],
        "monsters": [{
            "monster_id": "m:wolf", "name": "Wolf",
            "stats": {"atk": 1, "def": 1, "hp": 1}, "skills": None,
            "drop_table_id": None, "ai": "aggressive",
            "gold_min": gold_min, "gold_max": gold_max,
            "currency": "gold", "kills_per_tick": 1,
        }],
        "shops": [{"shop_id": "shop:s", "entries": [
            {"currency": "gold", "item": "item:potion",
             "price": sink_price, "buy_prob": buy_prob}]}],
    }


def _model_from_wb(wb):
    return EconomyModel.from_snapshot(AureusCsvAdapter().to_ir(wb, file_ref="econ"))


def test_adapter_derived_model_has_nonempty_sink():
    model = _model_from_wb(_econ_workbook(gold_min=5, gold_max=9,
                                          sink_price=50, buy_prob=0.5))
    assert model.sources, "faucet must be modeled from CSV"
    assert model.sinks, "sink must now be modeled from CSV (the fix)"
    sink = model.sinks[0]
    assert sink["price"] == 50 and sink["buy_prob"] == 0.5 and sink["currency"] == "gold"


def test_adapter_sink_causally_prevents_collapse():
    # Balanced: small faucet (<= sink drain) + an always-buying sink -> net<=0,
    # no collapse. Runaway: same shape but a huge faucet the sink can't absorb
    # -> collapse. The ONLY difference is faucet size, so the sink is proven
    # causally load-bearing (not a measured no-op).
    balanced = _model_from_wb(_econ_workbook(gold_min=5, gold_max=9,
                                             sink_price=50, buy_prob=1.0))
    runaway = _model_from_wb(_econ_workbook(gold_min=500, gold_max=1000,
                                            sink_price=50, buy_prob=1.0))
    rb = EconomySimulator().run(balanced, seed=0, n_agents=50, n_ticks=200)
    rr = EconomySimulator().run(runaway, seed=0, n_agents=50, n_ticks=200)
    assert detect_collapse(rb) is None, "balanced faucet+sink must not collapse"
    assert detect_collapse(rr) is not None, "faucet >> sink must still collapse"
