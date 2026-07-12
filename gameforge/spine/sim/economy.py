"""Economy simulator (M1 Task 8): Monte-Carlo + agent-based currency-flow
simulation, invariant checking (contract §7.4), and collapse reproduction with
an early warning tick strictly before the collapse tick.

Reads economy-relevant entities straight out of the IR (`CURRENCY` / `SHOP` /
`DROP_TABLE` / `GACHA_POOL` / `MONSTER` / `ITEM` / `EQUIPMENT`) via
`spine.ir.snapshot.Snapshot`; drives randomness through the spine-local
`spine.sim.rng.SimRandom` (M1-D6 — never `gameforge.game.aureus.rng`, spine
must not import `gameforge.game`). None of the types here are part of the
cross-milestone `contracts` schema (contract §6 only fixes `Finding`/`Patch`),
so plain dataclasses are used rather than pydantic models.

Determinism: `EconomySimulator.run` draws exclusively from a single
`SimRandom(seed)` in a fixed (tick, agent, source, sink) iteration order with
no wall-clock/external entropy anywhere in the loop — same `(model, seed,
n_agents, n_ticks)` always reproduces bit-identical `distributions`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.sim.rng import SimRandom

# --------------------------------------------------------------------------
# Tunable defaults for invariant thresholds / collapse detection. These are
# simulator policy, not contract-fixed constants — descriptive analysis
# knobs, never surfaced as a prescriptive "change X to Y" number.
# --------------------------------------------------------------------------
_DEFAULT_SINK_SOURCE_BAND = (0.5, 1.5)
_DEFAULT_INFLATION_THRESHOLD = 3.0
_DEFAULT_MIN_YIELD_RATE = 0.01
_BASELINE_WINDOW = 5
_SLOPE_WINDOW = 5
_COLLAPSE_MULTIPLIER = 8.0
_WARNING_FRACTION = 0.3
_DROP_PRODUCER_TYPES = frozenset({
    NodeType.MONSTER,
    NodeType.DROP_TABLE,
    NodeType.INTERACTABLE,
    NodeType.EVENT,
    NodeType.BATTLE_ENCOUNTER,
})


@dataclass
class EconomyModel:
    """Extracted economy: sources/sinks/gacha/equipment-curve, straight from IR attrs."""

    currencies: dict[str, dict[str, Any]] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    sinks: list[dict[str, Any]] = field(default_factory=list)
    gacha: dict[str, Any] | None = None
    equipment_curve: list[float] = field(default_factory=list)

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot) -> "EconomyModel":
        g = snapshot.to_graph()

        currencies = {e.id: dict(e.attrs) for e in g.nodes_of_type(NodeType.CURRENCY)}
        default_currency = next(iter(currencies), "gold")

        # --- sources: MONSTER/DROP_TABLE --DROPS_FROM--> CURRENCY ---
        sources: list[dict[str, Any]] = []
        for r in sorted(g.all_relations(), key=lambda r: r.id):
            if r.type is not EdgeType.DROPS_FROM:
                continue
            dst = g.get_node(r.dst_id)
            if dst is None or dst.type is not NodeType.CURRENCY:
                continue
            producer = g.get_node(r.src_id)
            if producer is None or producer.type not in _DROP_PRODUCER_TYPES:
                continue
            gold_min = producer.attrs.get("gold_min", 0)
            gold_max = producer.attrs.get("gold_max", gold_min)
            sources.append({
                "relation_id": r.id,
                "producer": r.src_id,
                "currency": r.dst_id,
                "gold_min": gold_min,
                "gold_max": gold_max,
                "kills_per_tick": producer.attrs.get("kills_per_tick", 1),
            })

        # --- sinks: SHOP --SELLS--> ITEM/EQUIPMENT (relation carries price) ---
        sinks: list[dict[str, Any]] = []
        for r in sorted(g.all_relations(), key=lambda r: r.id):
            if r.type is not EdgeType.SELLS:
                continue
            shop = g.get_node(r.src_id)
            if shop is None or shop.type is not NodeType.SHOP:
                continue
            attrs = r.attrs or {}
            price = attrs.get("price")
            if price is None:
                continue
            sinks.append({
                "relation_id": r.id,
                "shop": r.src_id,
                "target": r.dst_id,
                "price": price,
                "currency": attrs.get("currency", default_currency),
                "buy_prob": attrs.get("buy_prob", 0.5),
            })

        # --- gacha: pity/expectation (closed-form geometric-with-pity, same
        # formula as SMTChecker's gacha_expectation call) ---
        gacha: dict[str, Any] | None = None
        pools = sorted(g.nodes_of_type(NodeType.GACHA_POOL), key=lambda e: e.id)
        if pools:
            pool = pools[0]
            p = pool.attrs.get("base_rate")
            n = pool.attrs.get("pity_threshold")
            cost = pool.attrs.get("cost_per_draw", 0)
            draw_prob = pool.attrs.get("draw_prob", 0.0)
            expected = (1 - (1 - p) ** n) / p if p and n else None
            gacha = {
                "pool_id": pool.id, "base_rate": p, "pity_threshold": n,
                "cost_per_draw": cost, "draw_prob": draw_prob,
                "expected_draws": expected,
            }
            if draw_prob > 0 and cost:
                sinks.append({
                    "relation_id": f"gacha::{pool.id}",
                    "shop": pool.id, "target": pool.id, "price": cost,
                    "currency": pool.attrs.get("currency", default_currency),
                    "buy_prob": draw_prob,
                })

        # --- equipment strength curve: EQUIPMENT entities sorted by tier ---
        equipment = sorted(
            g.nodes_of_type(NodeType.EQUIPMENT), key=lambda e: e.attrs.get("tier", 0)
        )
        equipment_curve = [float(e.attrs.get("power", 0.0)) for e in equipment]

        return cls(
            currencies=currencies, sources=sources, sinks=sinks,
            gacha=gacha, equipment_curve=equipment_curve,
        )


@dataclass
class InvariantCheck:
    name: str
    ok: bool
    observed: float
    threshold: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimResult:
    distributions: dict[str, Any]
    invariants: list[InvariantCheck]
    sensitivity: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollapseReport:
    collapse_tick: int
    early_warning_tick: int
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _baseline(balances: list[float], window: int = _BASELINE_WINDOW) -> float:
    if not balances:
        return 0.0
    w = min(window, len(balances))
    return sum(balances[:w]) / w


def _steady_state_phase_means(
    balances: list[float], warmup_frac: float = 0.1, window_frac: float = 0.2,
) -> tuple[float, float]:
    """Mean of an early vs. a late window, after discarding the initial
    `warmup_frac` of ticks (agents ramp up from balance 0 toward whatever
    equilibrium the source/sink rates imply; that ramp is not "inflation").
    Falls back to (first, last) sample for very short trajectories.
    """
    n = len(balances)
    if n == 0:
        return 0.0, 0.0
    body = balances[int(n * warmup_frac):]
    if len(body) < 4:
        return body[0], body[-1]
    w = max(1, int(len(body) * window_frac))
    early = sum(body[:w]) / w
    late = sum(body[-w:]) / w
    return early, late


class EconomySimulator:
    """Agent-based Monte-Carlo economy simulation.

    Each tick, every agent earns currency from every configured source
    (`kills_per_tick` draws of `randint(gold_min, gold_max)` each) and then,
    for every configured sink, spends `price` with probability `buy_prob`
    provided the agent can afford it. Aggregate trajectories are tracked in
    `SimResult.distributions` for invariant checking and collapse detection.
    """

    def run(self, model: EconomyModel, seed: int, n_agents: int, n_ticks: int) -> SimResult:
        rng = SimRandom(seed)
        balances = [0.0] * n_agents
        avg_balance_per_tick: list[float] = []
        total_source_per_tick: list[float] = []
        total_sink_per_tick: list[float] = []

        for _tick in range(n_ticks):
            tick_source = 0.0
            tick_sink = 0.0
            for a in range(n_agents):
                income = 0.0
                for src in model.sources:
                    kills = int(src.get("kills_per_tick", 1))
                    lo, hi = int(src["gold_min"]), int(src["gold_max"])
                    for _ in range(kills):
                        income += rng.randint(lo, hi)
                balances[a] += income
                tick_source += income

                for sink in model.sinks:
                    prob = sink.get("buy_prob", 0.0)
                    price = sink["price"]
                    if prob <= 0:
                        continue
                    if rng.random() < prob and balances[a] >= price:
                        balances[a] -= price
                        tick_sink += price

            avg_balance_per_tick.append(sum(balances) / n_agents if n_agents else 0.0)
            total_source_per_tick.append(tick_source)
            total_sink_per_tick.append(tick_sink)

        distributions = {
            "avg_balance_per_tick": avg_balance_per_tick,
            "total_source_per_tick": total_source_per_tick,
            "total_sink_per_tick": total_sink_per_tick,
        }
        invariants = _compute_invariants(model, distributions, n_agents, n_ticks)
        sensitivity = _compute_sensitivity(distributions)
        return SimResult(distributions=distributions, invariants=invariants, sensitivity=sensitivity)


def _compute_invariants(
    model: EconomyModel, distributions: dict[str, Any], n_agents: int, n_ticks: int,
) -> list[InvariantCheck]:
    checks: list[InvariantCheck] = []
    total_source_per_tick = distributions["total_source_per_tick"]
    total_sink_per_tick = distributions["total_sink_per_tick"]
    source_total = sum(total_source_per_tick)
    sink_total = sum(total_sink_per_tick)

    # 1. currency sink/source balance
    if source_total > 0:
        ratio = sink_total / source_total
    else:
        ratio = 1.0 if sink_total == 0 else float("inf")
    lo_band, hi_band = _DEFAULT_SINK_SOURCE_BAND
    checks.append(InvariantCheck(
        name="currency_sink_source_balance",
        ok=lo_band <= ratio <= hi_band,
        observed=ratio, threshold=hi_band,
        evidence={"source_total": source_total, "sink_total": sink_total,
                  "band": [lo_band, hi_band]},
    ))

    # 2. inflation rate: late-phase vs. early-phase steady-state average,
    # skipping the initial ramp-up window (agents start at balance 0, so the
    # first ticks rising toward equilibrium is not "inflation").
    balances = distributions["avg_balance_per_tick"]
    early_ref, late_ref = _steady_state_phase_means(balances)
    if early_ref > 1e-9:
        inflation_ratio = late_ref / early_ref
    else:
        inflation_ratio = 1.0 if late_ref <= 1e-9 else float("inf")
    checks.append(InvariantCheck(
        name="inflation_rate",
        ok=inflation_ratio <= _DEFAULT_INFLATION_THRESHOLD,
        observed=inflation_ratio, threshold=_DEFAULT_INFLATION_THRESHOLD,
        evidence={"early_phase_avg": early_ref, "late_phase_avg": late_ref},
    ))

    # 3. drop-source existence & yield rate
    currency_ids = set(model.currencies.keys())
    covered = {s["currency"] for s in model.sources}
    missing = sorted(currency_ids - covered)
    yield_rate = source_total / (n_agents * n_ticks) if n_agents and n_ticks else 0.0
    checks.append(InvariantCheck(
        name="drop_source_existence_and_yield_rate",
        ok=(not missing) and yield_rate >= _DEFAULT_MIN_YIELD_RATE,
        observed=yield_rate, threshold=_DEFAULT_MIN_YIELD_RATE,
        evidence={"currencies_without_source": missing,
                  "yield_rate_per_agent_tick": yield_rate},
    ))

    # 4. equipment strength curve monotonic (non-decreasing by tier)
    curve = model.equipment_curve
    violations = [i for i in range(len(curve) - 1) if curve[i] > curve[i + 1]]
    checks.append(InvariantCheck(
        name="equipment_strength_curve_monotonic",
        ok=not violations, observed=float(len(violations)), threshold=0.0,
        evidence={"curve": curve, "violation_indices": violations},
    ))

    # 5. gacha expectation vs. pity
    if model.gacha and model.gacha.get("expected_draws") is not None:
        expected = model.gacha["expected_draws"]
        pity = model.gacha["pity_threshold"]
        checks.append(InvariantCheck(
            name="gacha_expectation_vs_pity",
            ok=expected <= pity, observed=expected, threshold=float(pity),
            evidence={"base_rate": model.gacha["base_rate"], "pity_threshold": pity},
        ))
    else:
        checks.append(InvariantCheck(
            name="gacha_expectation_vs_pity", ok=True, observed=0.0, threshold=0.0,
            evidence={"reason": "no gacha pool in model"},
        ))

    # 6. resource output-rate cap (peak observed per-agent per-tick income)
    caps = [c["output_rate_cap"] for c in model.currencies.values()
            if c.get("output_rate_cap") is not None]
    cap = min(caps) if caps else float("inf")
    peak = max((t / n_agents for t in total_source_per_tick), default=0.0) if n_agents else 0.0
    checks.append(InvariantCheck(
        name="resource_output_rate_cap",
        ok=peak <= cap, observed=peak, threshold=cap,
        evidence={"caps": caps},
    ))

    return checks


def _compute_sensitivity(distributions: dict[str, Any]) -> dict[str, Any]:
    source_total = sum(distributions["total_source_per_tick"])
    sink_total = sum(distributions["total_sink_per_tick"])
    return {
        "source_total": source_total,
        "sink_total": sink_total,
        "sink_source_ratio": (sink_total / source_total) if source_total else None,
    }


def detect_collapse(result: SimResult) -> CollapseReport | None:
    """Detect a sink/source imbalance driving unbounded currency growth.

    `collapse_tick` = first tick the avg-per-agent-balance trajectory crosses
    `_COLLAPSE_MULTIPLIER`x its early (tick `0..BASELINE_WINDOW-1`) baseline.
    `early_warning_tick` = an *earlier* tick where the trailing slope first
    crosses a smaller warning threshold — always < `collapse_tick` by
    construction (the warning slope is a strict fraction of the slope that
    would be needed to reach the collapse threshold by `collapse_tick`, and
    the search window is bounded to `[_SLOPE_WINDOW, collapse_tick)`).
    """
    balances = result.distributions.get("avg_balance_per_tick", [])
    if len(balances) < _BASELINE_WINDOW + 1:
        return None

    baseline = _baseline(balances)
    threshold_baseline = baseline if baseline > 1e-6 else 1e-6
    collapse_threshold = threshold_baseline * _COLLAPSE_MULTIPLIER

    collapse_tick = next((t for t, v in enumerate(balances) if v > collapse_threshold), None)
    if collapse_tick is None:
        return None

    warning_slope = ((collapse_threshold - threshold_baseline) / max(collapse_tick, 1)) * _WARNING_FRACTION
    early_warning_tick = None
    for t in range(_SLOPE_WINDOW, collapse_tick):
        slope = (balances[t] - balances[t - _SLOPE_WINDOW]) / _SLOPE_WINDOW
        if slope >= warning_slope:
            early_warning_tick = t
            break
    if early_warning_tick is None:
        early_warning_tick = max(collapse_tick - 1, 0)

    return CollapseReport(
        collapse_tick=collapse_tick,
        early_warning_tick=early_warning_tick,
        reason=(
            f"average per-agent currency balance crossed {collapse_threshold:.2f} "
            f"(> {_COLLAPSE_MULTIPLIER:.1f}x the tick 0..{_BASELINE_WINDOW - 1} baseline "
            f"of {threshold_baseline:.2f}) at tick {collapse_tick}, consistent with a "
            f"sustained currency source/sink imbalance (unbounded growth trend)"
        ),
        evidence={
            "baseline": threshold_baseline,
            "collapse_threshold": collapse_threshold,
            "balance_at_collapse_tick": balances[collapse_tick],
            "balance_at_warning_tick": balances[early_warning_tick],
        },
    )


def to_findings(
    result: SimResult, snapshot_id: str, model: "EconomyModel | None" = None
) -> list[Finding]:
    """Descriptive what-if Findings only — never a prescriptive "change X to
    Y" number. Violated invariants and a detected collapse each become one
    `oracle_type="simulation"`, `source="sim"`, `producer_id="economy_sim"`
    Finding.

    When `model` is supplied, the collapse finding names its faucet/sink
    entities (and the source relations) so a downstream repair agent can target
    the runaway faucet — without it, `entities` stays empty (backward-compatible).
    """
    run_id = f"sim@{snapshot_id[:23]}"
    findings: list[Finding] = []
    counter = 0

    for inv in result.invariants:
        if inv.ok:
            continue
        findings.append(Finding(
            id=f"{run_id}#{counter}", source="sim", producer_id="economy_sim",
            producer_run_id=run_id, oracle_type="simulation",
            defect_class=inv.name, severity="major", snapshot_id=snapshot_id,
            evidence={"observed": inv.observed, "threshold": inv.threshold, **inv.evidence},
            status="confirmed",
            message=(
                f"Economy invariant {inv.name!r} observed={inv.observed:.4g} "
                f"vs. threshold={inv.threshold:.4g} over the simulated horizon "
                f"(descriptive what-if only, no prescriptive fix)"
            ),
        ))
        counter += 1

    collapse = detect_collapse(result)
    if collapse is not None:
        faucet_entities: list[str] = []
        source_relations: list[str] = []
        collapse_evidence: dict[str, Any] = {
            "collapse_tick": collapse.collapse_tick,
            "early_warning_tick": collapse.early_warning_tick,
            **collapse.evidence,
        }
        if model is not None:
            faucet_entities = sorted(
                {s["producer"] for s in model.sources} | {s["shop"] for s in model.sinks}
            )
            source_relations = sorted({s["relation_id"] for s in model.sources})
            # descriptive summary of the imbalance drivers (no prescriptive numbers)
            collapse_evidence["faucets"] = [
                {"producer": s["producer"], "gold_min": s["gold_min"], "gold_max": s["gold_max"]}
                for s in model.sources
            ]
            collapse_evidence["sinks"] = [
                {"shop": s["shop"], "price": s["price"]} for s in model.sinks
            ]
        findings.append(Finding(
            id=f"{run_id}#{counter}", source="sim", producer_id="economy_sim",
            producer_run_id=run_id, oracle_type="simulation",
            defect_class="economy_collapse", severity="critical", snapshot_id=snapshot_id,
            entities=faucet_entities, relations=source_relations,
            evidence=collapse_evidence,
            status="confirmed",
            message=(
                f"Simulated economy trajectory shows a collapse at tick "
                f"{collapse.collapse_tick} (early warning at tick "
                f"{collapse.early_warning_tick}): {collapse.reason}. "
                f"Descriptive what-if only — no prescriptive fix given."
            ),
        ))
        counter += 1

    return findings
