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

Corpus source: `default_chain_snapshots()` loads the existing caravan + outpost
scenarios so `--record`/`--replay` have something to run today. The ≥20-chain
generator is a LATER task and will replace this default; nothing here blocks on
it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from dataclasses import dataclass

from gameforge.agents.playtest.agent import PlaytestAgent
from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.agent_io import PlaytestInput
from gameforge.contracts.env_types import Action, Interact, NavigateTo, Observe, Observation
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.spine.ingestion.csv_format import read_workbook
from gameforge.spine.ingestion.format_schema import FormatSchema
from gameforge.spine.ir.loader import load_scenario
from gameforge.spine.ir.snapshot import Snapshot

_CASSETTES_ROOT = "cassettes/playtest"

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
    seed: int = 0,
    max_steps: int = 200,
) -> PlaytestCorpusResult:
    """Run the Playtest agent over every chain snapshot and aggregate.

    Each chain: `snapshot_to_world` → fresh `AureusEnv` → `reset(scenario, seed)`
    → `PlaytestAgent.run`. `completed` is the env's verdict; `steps` is the
    action-trace length. Nothing here re-decides completion — it only measures.
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
            memory=(memory_factory() if memory_factory else None),
            max_steps=max_steps,
        )
        outcomes.append((bool(report.completed), len(report.action_trace)))
    return _aggregate(outcomes)


# --- no-LLM random baseline (completion floor) ------------------------------
def _random_action(obs: Observation, rng: random.Random) -> Action:
    """Pick a uniformly-random LEGAL action: navigate to any reachable target or
    interact with anything available at the current tile; `observe` when neither
    exists. Both candidate lists come from the env already sorted, so the choice
    is fully reproducible under a seeded RNG."""
    candidates: list[Action] = [NavigateTo(target=t) for t in obs.reachable_targets]
    candidates += [Interact(target=t) for t in obs.available_interactions]
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


# --- corpus source (placeholder until the ≥20-chain generator lands) ---------
def _load_workbook_snapshot(dir_path: str) -> Snapshot:
    """Load a CSV-workbook scenario dir into a Snapshot (same intermediates as
    `apps.cli.run_slice.run_slice_workbook`)."""
    with open(os.path.join(dir_path, "format_schema.json"), encoding="utf-8") as fh:
        schema = FormatSchema.model_validate(json.load(fh))
    workbook = read_workbook(dir_path, schema)
    return AureusCsvAdapter().to_ir(workbook, file_ref=dir_path)


def default_chain_snapshots() -> list[Snapshot]:
    """Existing scenarios as a stand-in corpus so `--record`/`--replay` have
    something to run. The generator (a LATER task) replaces this."""
    return [
        load_scenario("scenarios/caravan.yaml"),
        _load_workbook_snapshot("scenarios/outpost"),
    ]


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


# --- CLI --------------------------------------------------------------------
def _run_corpus_and_report(router: ModelRouter) -> PlaytestCorpusResult:
    corpus = default_chain_snapshots()
    result = run_playtest_corpus(corpus, router)
    print(format_result(result))
    baseline = random_baseline(default_chain_snapshots())
    print()
    print(format_result(baseline, title="Random Baseline (no-LLM floor)"))
    return result


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

    print("Recording playtest corpus (live gateway)…")
    _run_corpus_and_report(record_router())
    return 0


def _run_replay() -> int:
    _run_corpus_and_report(replay_router())
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
