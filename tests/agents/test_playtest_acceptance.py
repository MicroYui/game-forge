"""M2b-1c acceptance scaffold: playtest-corpus completion + planner/executor
ablation, ALL zero-live-network (REPLAY only).

Tests 1/2 depend on the playtest cassettes a human records with

    GAMEFORGE_LLM_LIVE=1 GAMEFORGE_LLM_KEY=<key> \\
        uv run python -m gameforge.agents.playtest_harness --record

Before those cassettes exist, running the corpus under REPLAY raises
`CassetteReplayMiss` (the CORRECT pre-record RED state — it proves the harness
is wired to real cassettes, not a stub). They are `skipif`-guarded on the
presence of a non-empty `cassettes/playtest/` dir so the suite stays GREEN
before recording and both activate automatically the moment cassettes land.

Test 3 needs NO cassettes — it exercises `wilson_ci` numerics and
`random_baseline` (no router at all), so it passes now.
"""

from __future__ import annotations

import os

import pytest

from gameforge.agents.playtest_harness import (
    PlaytestCorpusResult,
    default_chain_snapshots,
    random_baseline,
    replay_router,
    run_playtest_corpus,
    wilson_ci,
)
from gameforge.runtime.model_router.router import ModelRouter

PLAYTEST_CASSETTES = "cassettes/playtest"

# Bounded depth mirroring `RECORD_MAX_STEPS` in `playtest_harness.py` — REPLAY
# must reuse the same bound a `--record` pass used so the reproduced action
# traces (and hence step counts / completion verdicts) match the recording.
_MAX_STEPS = 60

needs_cassettes = pytest.mark.skipif(
    not os.path.isdir(PLAYTEST_CASSETTES) or not os.listdir(PLAYTEST_CASSETTES),
    reason="playtest cassettes not recorded yet — run "
    "`GAMEFORGE_LLM_LIVE=1 GAMEFORGE_LLM_KEY=<key> "
    "uv run python -m gameforge.agents.playtest_harness --record`",
)


def _replay_router() -> ModelRouter:
    return replay_router(PLAYTEST_CASSETTES)


# --------------------------------------------------------------------------
# 1. Completion is reproducible under REPLAY and beats/ties the random floor
#    (honest bar per design D2 — NO fixed high % threshold).
# --------------------------------------------------------------------------
@needs_cassettes
def test_playtest_completion_reproducible_and_beats_baseline():
    corpus = default_chain_snapshots()

    r1 = run_playtest_corpus(
        corpus, _replay_router(), use_planner=True, max_steps=_MAX_STEPS
    )
    r2 = run_playtest_corpus(
        corpus, _replay_router(), use_planner=True, max_steps=_MAX_STEPS
    )
    assert r1 == r2  # byte-for-byte reproducible under REPLAY

    baseline = random_baseline(corpus, max_steps=_MAX_STEPS)
    assert r1.completion_rate >= baseline.completion_rate


# --------------------------------------------------------------------------
# 2. Planner/executor ablation is reported: both modes produce a real result
#    and the delta is a real number in range. NO sign asserted — the honest
#    recorded numbers decide, not a prior expectation.
# --------------------------------------------------------------------------
@needs_cassettes
def test_planner_executor_ablation_reported():
    corpus = default_chain_snapshots()
    router = _replay_router()

    layered = run_playtest_corpus(
        corpus, router, use_planner=True, max_steps=_MAX_STEPS
    )
    flat = run_playtest_corpus(
        corpus, router, use_planner=False, max_steps=_MAX_STEPS
    )

    assert isinstance(layered, PlaytestCorpusResult)
    assert isinstance(flat, PlaytestCorpusResult)
    assert 0.0 <= layered.completion_rate <= 1.0
    assert 0.0 <= flat.completion_rate <= 1.0

    delta = layered.completion_rate - flat.completion_rate
    assert -1.0 <= delta <= 1.0  # a real, reportable number — sign not asserted


# --------------------------------------------------------------------------
# 3. wilson_ci monotonic sanity + random_baseline over a small generated
#    corpus — no cassettes needed, passes now.
# --------------------------------------------------------------------------
def test_wilson_ci_and_baseline_sane():
    n = 10
    # Monotonic in k: as successes increase (n fixed), both bounds never
    # decrease — a strictly-more-successful count can't yield a lower CI.
    prev_low, prev_high = wilson_ci(0, n)
    for k in range(1, n + 1):
        low, high = wilson_ci(k, n)
        assert low >= prev_low
        assert high >= prev_high
        assert 0.0 <= low <= high <= 1.0
        prev_low, prev_high = low, high

    chains = default_chain_snapshots(n=2)
    result = random_baseline(chains)
    assert isinstance(result, PlaytestCorpusResult)
    assert result.n_chains == 2
    assert 0.0 <= result.completion_rate <= 1.0
