"""Agent-metric aggregation for GameForge-Bench (M3a Task 8 / design §6).

The ONLY `bench` module that imports `gameforge.agents`. Per the user's M3
decision, the expensive agent metrics (Fix Pass Rate, Playtest completion +
planner/executor & memory ablations) are reported on the BOUNDED cassette
subset already recorded in M2 — recomputed here under REPLAY (zero live calls,
zero LLM SDK). Each group is guarded on its cassettes: a missing set is skipped
(the metric is simply absent), never a live call. Sample sizes are small and
honestly reported (n on every `Metric`).
"""
from __future__ import annotations

import os

from gameforge.agents import harness as _repair
from gameforge.agents import playtest_harness as _pt
from gameforge.bench.metrics import Metric
from gameforge.runtime.model_router.router import CassetteReplayMiss
from gameforge.spine.stats import wilson_ci

_CONSTRAINTS = "scenarios/constraints"


def _has_cassettes(path: str) -> bool:
    return os.path.isdir(path) and bool(os.listdir(path))


def _metric(name: str, k: int, n: int) -> Metric:
    low, high = wilson_ci(k, n)
    return Metric(name=name, defect_class=None, n=n, k=k,
                  rate=(k / n if n else 0.0), ci_low=low, ci_high=high, bucket="agent")


def aggregate_agent_metrics() -> list[Metric]:
    """REPLAY-recompute the committed M2 agent results as bench `Metric`s
    (`bucket="agent"`). Each group is skipped if its cassettes are absent or a
    replay misses — never falling back to a live call."""
    metrics: list[Metric] = []

    # --- repair Fix Pass Rate (M2a-part2 repair harness) ---
    if _has_cassettes(_repair._CASSETTES_ROOT):
        try:
            r = _repair.run_repair_corpus(
                _repair.default_scenario_dirs(), _CONSTRAINTS, _repair.replay_router()
            )
            metrics.append(_metric("fix_pass_rate", r.passed, r.attempted))
        except CassetteReplayMiss:
            pass

    # --- Playtest completion + planner/executor + memory ablations (M2b) ---
    if _has_cassettes(_pt._CASSETTES_ROOT):
        try:
            corpus = _pt.default_chain_snapshots()
            layered = _pt.run_playtest_corpus(
                corpus, _pt.replay_router(), use_planner=True, max_steps=_pt.RECORD_MAX_STEPS
            )
            flat = _pt.run_playtest_corpus(
                corpus, _pt.replay_router(), use_planner=False, max_steps=_pt.RECORD_MAX_STEPS
            )
            base, mem_on, _ = _pt.memory_ablation(
                corpus, _pt.replay_router(), max_steps=_pt.RECORD_MAX_STEPS
            )
            # layered == memory-ablation base (planner-on, memory-off); report the
            # three constituent completion rates — the report layer derives the
            # planner (layered−flat) and memory (mem_on−base) ablation deltas.
            metrics.append(_metric("playtest_completion_layered", layered.completed, layered.n_chains))
            metrics.append(_metric("playtest_completion_flat", flat.completed, flat.n_chains))
            metrics.append(_metric("playtest_completion_mem_on", mem_on.completed, mem_on.n_chains))
        except CassetteReplayMiss:
            pass

    return metrics
