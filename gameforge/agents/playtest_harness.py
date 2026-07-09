"""M2b-1 Task 7: Playtest regression harness — run the Playtest agent over a
corpus of quest-chain snapshots and report the completion rate with a 95% Wilson
score CI, alongside a no-LLM random baseline (the completion floor) and a
RECORD/REPLAY entrypoint.

Mirrors the M2a-part2 repair harness (`gameforge/agents/harness.py`): pass/fail
is the deterministic ENV's verdict (`PlaytestReport.completed`, itself read back
from `AureusEnv._all_quests_completed()`), never a model claim. REPLAY reads
`cassettes/playtest/` with zero live calls; RECORD hits the live gateway only
when `GAMEFORGE_LLM_LIVE=1` AND `GAMEFORGE_LLM_KEY` are both present. The
deterministic trunk (`spine`) is never touched — only `agents.*` reach the
router (hard rule 4 / import-linter).

Corpus source: `default_chain_snapshots()` returns the ≥20-chain generated
corpus (`gameforge.agents.scenario_gen.generate_chains`, M2b-1b) — genuinely
distinct, `ScriptedDriver`-completable quest chains spanning short/medium/long
buckets, so `--record`/`--replay` exercise the real generated corpus rather
than the couple of hand-authored scenarios.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from dataclasses import dataclass

from gameforge.agents.playtest.agent import PlaytestAgent
from gameforge.agents.scenario_gen import generate_chains
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.agent_io import PlaytestInput
from gameforge.contracts.env_types import (
    Action, Attack, Interact, NavigateTo, Observe, Observation,
)
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.ir.snapshot import Snapshot

_CASSETTES_ROOT = "cassettes/playtest"

# `--record` bounds every corpus run to this many steps (both ablation modes +
# the random baseline) so a RECORD pass over 20 chains is finite: short/medium
# chains complete well under the cap, long chains honestly hit it rather than
# running unbounded — the §7.8 completion-cliff is a real recorded data point,
# not an artifact of an open-ended loop. Raised 60→150 (corpus median
# scripted-optimal length is 137 steps once fight steps are driven via the
# navigate_to-then-attack protocol; 60 made 14/20 chains impossible to finish
# regardless of agent quality) so the shorter chains keep plenty of headroom
# while the longest chains still honestly hit the cap rather than running
# unbounded. `--replay` MUST reuse the same bound (recorded cassettes are keyed
# by request content, not by max_steps, but the action trace/step count they
# reproduce only matches the record run when the loop is bounded identically).
RECORD_MAX_STEPS = 150

# Action-trace length buckets (short/medium/long) — completion rate is reported
# per bucket so a corpus of mixed chain lengths surfaces where the agent stalls.
_SHORT_MAX = 20  # steps <20   → short
_MEDIUM_MAX = 60  # 20..59      → medium; >=60 → long
_BUCKET_ORDER = ("short", "medium", "long")


# --- Wilson score interval --------------------------------------------------
def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for `k` successes of `n` trials.

    Pure stdlib. `n == 0` → `(0.0, 0.0)`. The bounds are clamped to `[0, 1]` and
    pinned to bracket the point estimate p̂ (a Wilson interval always contains
    p̂ mathematically; the pin removes floating-point noise at the p̂∈{0,1}
    extremes, where the exact bounds are 0.0 and 1.0 respectively).
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = p + z2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    low = (center - spread) / denom
    high = (center + spread) / denom
    return (max(0.0, min(low, p)), min(1.0, max(high, p)))


# --- result aggregate -------------------------------------------------------
@dataclass
class PlaytestCorpusResult:
    n_chains: int
    completed: int
    completion_rate: float
    per_chain: list[dict]
    by_length: dict[str, dict]
    mean_steps: float


def _length_bucket(steps: int) -> str:
    if steps < _SHORT_MAX:
        return "short"
    if steps < _MEDIUM_MAX:
        return "medium"
    return "long"


def _aggregate(outcomes: list[tuple[bool, int]]) -> PlaytestCorpusResult:
    """Fold per-chain `(completed, steps)` pairs into a `PlaytestCorpusResult`,
    including a per-length-bucket Wilson CI. The completion verdict is passed in
    from the env; this only measures."""
    n = len(outcomes)
    per_chain: list[dict] = []
    buckets: dict[str, dict[str, int]] = {}
    completed_total = 0
    steps_total = 0

    for i, (done, steps) in enumerate(outcomes):
        bucket = _length_bucket(steps)
        per_chain.append(
            {"index": i, "length_bucket": bucket, "completed": done, "steps": steps}
        )
        completed_total += int(done)
        steps_total += steps
        agg = buckets.setdefault(bucket, {"n": 0, "completed": 0})
        agg["n"] += 1
        agg["completed"] += int(done)

    completion_rate = completed_total / n if n else 0.0
    mean_steps = steps_total / n if n else 0.0

    by_length: dict[str, dict] = {}
    for bucket in _BUCKET_ORDER:  # fixed order → deterministic dict
        if bucket not in buckets:
            continue
        agg = buckets[bucket]
        low, high = wilson_ci(agg["completed"], agg["n"])
        by_length[bucket] = {
            "n": agg["n"],
            "completed": agg["completed"],
            "rate": agg["completed"] / agg["n"] if agg["n"] else 0.0,
            "ci_low": low,
            "ci_high": high,
        }

    return PlaytestCorpusResult(
        n_chains=n,
        completed=completed_total,
        completion_rate=completion_rate,
        per_chain=per_chain,
        by_length=by_length,
        mean_steps=mean_steps,
    )


def run_playtest_corpus(
    chain_snapshots: list[Snapshot],
    router: ModelRouter,
    *,
    use_planner: bool = True,
    memory_factory=None,
    memory: object = None,
    seed: int = 0,
    max_steps: int = 200,
) -> PlaytestCorpusResult:
    """Run the Playtest agent over every chain snapshot and aggregate.

    Each chain: `snapshot_to_world` → fresh `AureusEnv` → `reset(scenario, seed)`
    → `PlaytestAgent.run`. `completed` is the env's verdict; `steps` is the
    action-trace length. Nothing here re-decides completion — it only measures.

    `memory_factory`, when given, is called ONCE PER CHAIN to build a fresh
    `MemTrace` instance (the memory-on ablation mode, Task 7/8) so state never
    leaks across chains. `memory` is the plain per-run value used when no
    factory is given; it defaults to `None` (M2b-1's memory-off shape,
    byte-identical requests — see `test_playtest_recall_injection.py`'s
    regression lock). `memory_factory` always wins when both are given.
    """
    agent = PlaytestAgent()
    outcomes: list[tuple[bool, int]] = []
    for snapshot in chain_snapshots:
        world = snapshot_to_world(snapshot)
        env = AureusEnv(world)
        env.reset(world.scenario.scenario_id, seed)
        report = agent.run(
            PlaytestInput(scenario=world.scenario.scenario_id, seed=seed),
            env,
            router,
            use_planner=use_planner,
            memory=(memory_factory() if memory_factory else memory),
            max_steps=max_steps,
        )
        outcomes.append((bool(report.completed), len(report.action_trace)))
    return _aggregate(outcomes)


# --- no-LLM random baseline (completion floor) ------------------------------
def _random_action(obs: Observation, rng: random.Random) -> Action:
    """Pick a uniformly-random LEGAL action: navigate to any reachable target,
    interact with anything available at the current tile, or attack any
    reachable-but-not-interactable target; `observe` when nothing exists.

    The attack candidates are exactly the entries in `reachable_targets` that
    are NOT in `available_interactions` — during an active fight this is
    precisely the alive monster ids `AureusEnv.observe()` unions into
    `reachable_targets` (see `kernel.py`'s `observe()`/`_maybe_activate_encounter`),
    since monsters are never NPCs/interactables. Before a fight is triggered
    this candidate set is instead the not-yet-reached NPCs/interactables/battle
    placements, so an `attack` there is a legal-but-harmless no-op (the kernel
    answers `not_in_combat`/`no_target`, never raises) — without this the
    random floor could only ever emit navigate/interact/observe and was
    structurally barred from completing any fight-bearing chain, which
    misrepresented the floor. All candidate lists come from the env already
    sorted, so the choice is fully reproducible under a seeded RNG."""
    candidates: list[Action] = [NavigateTo(target=t) for t in obs.reachable_targets]
    candidates += [Interact(target=t) for t in obs.available_interactions]
    attackable = [t for t in obs.reachable_targets if t not in obs.available_interactions]
    candidates += [Attack(target_id=t) for t in attackable]
    if not candidates:
        return Observe()
    return rng.choice(candidates)


def _run_random_chain(env: AureusEnv, *, seed: int, max_steps: int) -> tuple[bool, int]:
    rng = random.Random(seed)
    obs = env.observe()
    steps = 0
    done = False
    for _ in range(max_steps):
        result = env.step(_random_action(obs, rng))
        obs = result.observation
        steps += 1
        if result.done:
            done = True
            break
    return done, steps


def random_baseline(
    chain_snapshots: list[Snapshot],
    seed: int = 0,
    max_steps: int = 200,
) -> PlaytestCorpusResult:
    """A NO-LLM agent that takes uniformly-random legal actions — the lower-bound
    the Playtest agent must beat. Drives the same env the same way (fresh env +
    `reset(scenario, seed)` per chain) so its `PlaytestCorpusResult` is directly
    comparable to `run_playtest_corpus`'s. No router is involved."""
    outcomes: list[tuple[bool, int]] = []
    for snapshot in chain_snapshots:
        world = snapshot_to_world(snapshot)
        env = AureusEnv(world)
        env.reset(world.scenario.scenario_id, seed)
        outcomes.append(_run_random_chain(env, seed=seed, max_steps=max_steps))
    return _aggregate(outcomes)


# --- corpus source: the ≥20-chain generated corpus (M2b-1b) ----------------
def default_chain_snapshots(seed: int = 0, n: int = 20) -> list[Snapshot]:
    """The default corpus `--record`/`--replay` run against: `n` genuinely
    distinct, `ScriptedDriver`-completable generated quest chains spanning
    short/medium/long buckets (`gameforge.agents.scenario_gen.generate_chains`).
    `seed`/`n` are overridable for callers that want a different-sized or
    differently-seeded corpus; the CLI entrypoints use the defaults (seed=0,
    n=20)."""
    return generate_chains(seed=seed, n=n)


# --- router construction ----------------------------------------------------
class _NoLiveTransport:
    """REPLAY must never touch the network: a cassette miss raises
    `CassetteReplayMiss` inside the router BEFORE any transport call, so this
    `complete` should never run. If it ever does, fail loud rather than silently
    reaching the gateway."""

    def complete(self, req):  # noqa: ANN001, ANN201 — Protocol shape only
        raise RuntimeError(
            "REPLAY router attempted a live transport call — this is a bug "
            "(a cassette miss must surface as CassetteReplayMiss, never a live call)"
        )


def replay_router(cassettes_root: str = _CASSETTES_ROOT) -> ModelRouter:
    """REPLAY router over `cassettes/playtest/` — zero live calls (CI / acceptance)."""
    return ModelRouter(
        _NoLiveTransport(), CassetteStore(cassettes_root), mode=RouterMode.REPLAY
    )


def record_router(cassettes_root: str = _CASSETTES_ROOT) -> ModelRouter:
    """RECORD router over the live gateway — the ONLY place a live call happens.

    Imported lazily so importing this module (e.g. in REPLAY tests) never pulls
    in the HTTP transport or requires a key.
    """
    from gameforge.runtime.model_router.anthropic_transport import (
        AnthropicMessagesTransport,
    )
    from gameforge.runtime.secrets.env import get_llm_key

    return ModelRouter(
        AnthropicMessagesTransport(base_url="http://localhost:4141", api_key=get_llm_key()),
        CassetteStore(cassettes_root),
        mode=RouterMode.RECORD,
        resume=True,
    )


# --- reporting --------------------------------------------------------------
def format_result(result: PlaytestCorpusResult, *, title: str = "Playtest Corpus Result") -> str:
    lines = [
        f"=== {title} ===",
        f"n_chains:        {result.n_chains}",
        f"completed:       {result.completed}",
        f"completion_rate: {result.completion_rate:.1%}",
        f"mean_steps:      {result.mean_steps:.2f}",
        "by_length:",
    ]
    for bucket, b in result.by_length.items():
        lines.append(
            f"  [{bucket:<6}] n={b['n']} completed={b['completed']} "
            f"rate={b['rate']:.1%} 95%CI=[{b['ci_low']:.2f}, {b['ci_high']:.2f}]"
        )
    lines.append("per-chain:")
    for row in result.per_chain:
        mark = "DONE" if row["completed"] else "FAIL"
        lines.append(
            f"  #{row['index']:<3} [{mark}] bucket={row['length_bucket']:<6} "
            f"steps={row['steps']}"
        )
    return "\n".join(lines)


def format_ablation_report(
    layered: PlaytestCorpusResult,
    flat: PlaytestCorpusResult,
    baseline: PlaytestCorpusResult,
) -> str:
    """Combined report for the planner/executor ablation: both full corpus
    breakdowns, the ablation delta (layered − flat completion_rate), and the
    no-LLM random baseline floor. Identical shape whether the corpora came from
    RECORD or REPLAY — REPLAY reproduces this byte-for-byte."""
    delta = layered.completion_rate - flat.completion_rate
    lines = [
        format_result(layered, title="Layered (planner+executor)"),
        "",
        format_result(flat, title="Flat (executor-only)"),
        "",
        "=== Planner/Executor Ablation ===",
        f"layered completion_rate: {layered.completion_rate:.1%}",
        f"flat completion_rate:    {flat.completion_rate:.1%}",
        f"ablation delta (layered - flat): {delta:+.1%}",
        "",
        format_result(baseline, title="Random Baseline (no-LLM floor)"),
    ]
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------
def _run_ablation_and_report(
    router: ModelRouter,
) -> tuple[PlaytestCorpusResult, PlaytestCorpusResult, PlaytestCorpusResult]:
    """Run BOTH ablation modes — layered (planner+executor) and flat
    (executor-only) — over the same corpus at `RECORD_MAX_STEPS`, plus the
    no-LLM random baseline, and print the combined report. `router` is reused
    for both LLM-backed runs (one shared `CassetteStore` — the two modes'
    request_hashes differ by node/prompt so there is no collision); the random
    baseline needs no router. Called identically by `--record` and `--replay`
    so the printed report is reproducible under REPLAY."""
    corpus = default_chain_snapshots()
    layered = run_playtest_corpus(
        corpus, router, use_planner=True, max_steps=RECORD_MAX_STEPS
    )
    flat = run_playtest_corpus(
        corpus, router, use_planner=False, max_steps=RECORD_MAX_STEPS
    )
    baseline = random_baseline(corpus, max_steps=RECORD_MAX_STEPS)
    print(format_ablation_report(layered, flat, baseline))
    return layered, flat, baseline


def _run_record() -> int:
    if os.environ.get("GAMEFORGE_LLM_LIVE") != "1":
        print(
            "RECORD refused: live LLM calls are gated. Re-run with "
            "GAMEFORGE_LLM_LIVE=1 (and GAMEFORGE_LLM_KEY set) to record cassettes.",
            file=sys.stderr,
        )
        return 2
    try:
        from gameforge.runtime.secrets.env import get_llm_key

        get_llm_key()  # presence check only — never printed, never written to disk
    except RuntimeError as exc:
        print(f"RECORD refused: {exc}", file=sys.stderr)
        return 2

    print(
        "Recording playtest corpus (live gateway) — layered + flat ablation, "
        f"max_steps={RECORD_MAX_STEPS}…"
    )
    _run_ablation_and_report(record_router())
    return 0


def _run_replay() -> int:
    _run_ablation_and_report(replay_router())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gameforge.agents.playtest_harness",
        description="GameForge M2b-1 Playtest regression harness (record / replay).",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--record",
        action="store_true",
        help="RECORD mode: live gateway calls (gated on GAMEFORGE_LLM_LIVE=1 + key).",
    )
    group.add_argument(
        "--replay",
        action="store_true",
        help="REPLAY mode (default): read cassettes/playtest/, zero live calls.",
    )
    args = parser.parse_args(argv)
    if args.record:
        return _run_record()
    return _run_replay()


if __name__ == "__main__":
    raise SystemExit(main())
