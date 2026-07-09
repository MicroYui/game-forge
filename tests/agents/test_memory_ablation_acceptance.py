"""M2b-2 Task 7/8b acceptance scaffold: memory ablation (mem-on vs mem-off) +
compaction-strategy comparison (DeterministicCompactor vs LLMCompactor), ALL
zero-live-network (REPLAY only).

Tests 1/2 depend on the mem-on + compactor-comparison cassettes a human
records with

    GAMEFORGE_LLM_LIVE=1 GAMEFORGE_LLM_KEY=<key> \\
        uv run python -m gameforge.agents.playtest_harness --record

`cassettes/playtest/` ALREADY carries the M2b-1 mem-off cassettes (layered +
flat, recorded before this milestone), so a plain "dir non-empty" skipif —
`test_playtest_acceptance.py`'s pattern — can't tell whether the NEW mem-on /
compactor-comparison request hashes have landed: they are distinct entries in
the SAME store (the mem-on requests differ from the mem-off ones only by the
injected recall text). Instead, each cassette-dependent test attempts the
REPLAY run and treats a `CassetteReplayMiss` as the "not recorded yet" signal,
skipping dynamically via `pytest.skip(...)`. Both tests flip from SKIP to PASS
the moment the human record pass lands `cassettes/playtest/*` for the new
modes — zero code change here.

Test 3 needs NO cassettes — it exercises `AblationReport`'s dataclass/delta
arithmetic and `memory_ablation`'s wiring against a monkeypatched
`run_playtest_corpus` (no live call, no cassette store touched), so it passes
now.
"""

from __future__ import annotations

import pytest

from gameforge.agents.playtest.memory import LLMCompactor, MemTrace
from gameforge.agents.playtest_harness import (
    RECORD_MAX_STEPS,
    AblationReport,
    PlaytestCorpusResult,
    default_chain_snapshots,
    format_compaction_comparison,
    memory_ablation,
    replay_router,
    run_playtest_corpus,
)
import gameforge.agents.playtest_harness as playtest_harness
from gameforge.runtime.model_router.router import CassetteReplayMiss, ModelRouter
from gameforge.spine.ir.loader import load_scenario

PLAYTEST_CASSETTES = "cassettes/playtest"


def _replay_router() -> ModelRouter:
    return replay_router(PLAYTEST_CASSETTES)


def _run_or_skip(fn, *args, **kwargs):
    """Run `fn`; a `CassetteReplayMiss` means the mem-on / compactor-comparison
    request hashes haven't been recorded yet in `cassettes/playtest/` — treat
    that as "not recorded yet" and skip dynamically (see module docstring for
    why a static dir-emptiness skipif can't express this)."""
    try:
        return fn(*args, **kwargs)
    except CassetteReplayMiss as exc:
        pytest.skip(
            "memory-on / compactor-comparison cassettes not recorded yet — run "
            "`GAMEFORGE_LLM_LIVE=1 GAMEFORGE_LLM_KEY=<key> "
            f"uv run python -m gameforge.agents.playtest_harness --record` ({exc})"
        )


# ---------------------------------------------------------------------------
# 1. Memory ablation is reported under REPLAY: mem-off base vs mem-on
#    (DeterministicCompactor), both real PlaytestCorpusResults, delta reported,
#    mem-on NOT WORSE than mem-off (design §9 anchor 4 — honest, no fixed
#    threshold). Reproducible under REPLAY.
# ---------------------------------------------------------------------------
def test_memory_ablation_reported_and_on_not_worse():
    corpus = default_chain_snapshots()

    base1, mem_on1, report1 = _run_or_skip(
        memory_ablation, corpus, _replay_router(), max_steps=RECORD_MAX_STEPS
    )
    base2, mem_on2, report2 = memory_ablation(
        corpus, _replay_router(), max_steps=RECORD_MAX_STEPS
    )

    assert isinstance(base1, PlaytestCorpusResult)
    assert isinstance(mem_on1, PlaytestCorpusResult)
    assert isinstance(report1, AblationReport)
    assert 0.0 <= base1.completion_rate <= 1.0
    assert 0.0 <= mem_on1.completion_rate <= 1.0

    # reproducible under REPLAY (field-equal across two independent routers).
    assert base1 == base2
    assert mem_on1 == mem_on2
    assert report1 == report2

    # the delta is a real, reported number...
    assert report1.delta_vs_base == mem_on1.completion_rate - base1.completion_rate
    # ...and mem-on is NOT WORSE than mem-off — honest bar (design §9 anchor 4:
    # "on 不劣于 off"), no fixed % threshold; the recorded numbers decide.
    assert mem_on1.completion_rate >= base1.completion_rate, (
        f"mem-on {mem_on1.completion_rate:.0%} must not be worse than mem-off "
        f"{base1.completion_rate:.0%}"
    )


# ---------------------------------------------------------------------------
# 2. Compactor-strategy comparison is reported under REPLAY: mem-on with
#    DeterministicCompactor vs mem-on with LLMCompactor, both real results,
#    reproducible, comparison reportable. NO sign asserted — the recorded
#    numbers pick the winner (Task 8b).
# ---------------------------------------------------------------------------
def test_compactor_comparison_reported():
    corpus = default_chain_snapshots()

    det1 = _run_or_skip(
        run_playtest_corpus,
        corpus,
        _replay_router(),
        use_planner=True,
        memory_factory=lambda: MemTrace(),
        max_steps=RECORD_MAX_STEPS,
    )
    llm1 = _run_or_skip(
        run_playtest_corpus,
        corpus,
        _replay_router(),
        use_planner=True,
        memory_factory=lambda: MemTrace(compactor=LLMCompactor()),
        max_steps=RECORD_MAX_STEPS,
    )
    det2 = run_playtest_corpus(
        corpus,
        _replay_router(),
        use_planner=True,
        memory_factory=lambda: MemTrace(),
        max_steps=RECORD_MAX_STEPS,
    )
    llm2 = run_playtest_corpus(
        corpus,
        _replay_router(),
        use_planner=True,
        memory_factory=lambda: MemTrace(compactor=LLMCompactor()),
        max_steps=RECORD_MAX_STEPS,
    )

    assert isinstance(det1, PlaytestCorpusResult)
    assert isinstance(llm1, PlaytestCorpusResult)
    assert 0.0 <= det1.completion_rate <= 1.0
    assert 0.0 <= llm1.completion_rate <= 1.0

    # both strategies reproduce under REPLAY (two runs field-equal).
    assert det1 == det2
    assert llm1 == llm2

    # the comparison is reportable — a real delta; NO sign asserted (the
    # recorded numbers decide the winner, per Task 8b).
    delta = llm1.completion_rate - det1.completion_rate
    assert -1.0 <= delta <= 1.0
    report_text = format_compaction_comparison(det1, llm1)
    assert "Compactor Comparison" in report_text
    assert f"{delta:+.1%}" in report_text


# ---------------------------------------------------------------------------
# 3. AblationReport construction + delta_vs_base arithmetic + memory_ablation
#    wiring — exercised structurally with a monkeypatched `run_playtest_corpus`
#    (no live call, no cassette store touched). Passes now.
# ---------------------------------------------------------------------------
def _canned_result(completed: int, n: int) -> PlaytestCorpusResult:
    return PlaytestCorpusResult(
        n_chains=n,
        completed=completed,
        completion_rate=completed / n if n else 0.0,
        per_chain=[],
        by_length={},
        mean_steps=10.0,
    )


def test_ablation_report_and_memory_ablation_wiring_no_cassettes(monkeypatch):
    # -- AblationReport construction + delta_vs_base arithmetic, standalone --
    report = AblationReport(
        variant="mem-on", completion_rate=0.75, ci=(0.4, 0.9), delta_vs_base=0.75 - 0.5
    )
    assert report.variant == "mem-on"
    assert report.completion_rate == 0.75
    assert report.ci == (0.4, 0.9)
    assert report.delta_vs_base == pytest.approx(0.25)

    # -- memory_ablation wiring: the mem-on call must build a REAL MemTrace via
    #    memory_factory(), and both calls must carry use_planner=True and the
    #    same max_steps, differing ONLY in memory_factory (None vs a MemTrace
    #    builder) — exercised with a monkeypatched `run_playtest_corpus` so no
    #    live call/env/router/cassette is ever touched. --
    calls: list[dict] = []

    def _fake_run_playtest_corpus(
        chain_snapshots, router, *, use_planner=True, memory_factory=None,
        memory=None, seed=0, max_steps=200,
    ):
        instance = memory_factory() if memory_factory is not None else memory
        calls.append(
            {
                "use_planner": use_planner,
                "memory_factory": memory_factory,
                "instance": instance,
                "max_steps": max_steps,
            }
        )
        n = len(chain_snapshots)
        # mem-on (memory_factory given) completes strictly better than base —
        # a deterministic, canned stand-in for the real corpus run.
        completed = n if memory_factory is not None else max(n - 1, 0)
        return _canned_result(completed, n)

    monkeypatch.setattr(playtest_harness, "run_playtest_corpus", _fake_run_playtest_corpus)

    corpus = [load_scenario("scenarios/caravan.yaml") for _ in range(4)]
    base, mem_on, ablation = memory_ablation(corpus, object(), max_steps=42)

    assert len(calls) == 2
    base_call, mem_on_call = calls
    assert base_call["use_planner"] is True
    assert base_call["memory_factory"] is None
    assert base_call["instance"] is None
    assert base_call["max_steps"] == 42
    assert mem_on_call["use_planner"] is True
    assert mem_on_call["memory_factory"] is not None
    assert isinstance(mem_on_call["instance"], MemTrace)  # the mem-on path is real
    assert mem_on_call["max_steps"] == 42

    assert isinstance(base, PlaytestCorpusResult)
    assert isinstance(mem_on, PlaytestCorpusResult)
    assert isinstance(ablation, AblationReport)
    assert ablation.variant == "mem-on"
    assert ablation.completion_rate == mem_on.completion_rate
    assert ablation.delta_vs_base == pytest.approx(
        mem_on.completion_rate - base.completion_rate
    )
    assert mem_on.completion_rate > base.completion_rate  # per the canned rates (4/4 vs 3/4)
