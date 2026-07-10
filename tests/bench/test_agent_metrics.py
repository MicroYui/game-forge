"""M3a Task 8: agent-metric aggregation (REPLAY-only bridge to M2)."""
from __future__ import annotations

import pytest

from gameforge.bench.agent_metrics import aggregate_agent_metrics


def test_agent_metrics_replay_playtest_and_fix_pass_rate():
    metrics = aggregate_agent_metrics()
    names = {m.name for m in metrics}
    if "playtest_completion_layered" not in names:
        pytest.skip("playtest cassettes absent — record first")

    layered = next(m for m in metrics if m.name == "playtest_completion_layered")
    mem_on = next(m for m in metrics if m.name == "playtest_completion_mem_on")
    assert layered.rate == 0.7   # matches the committed M2b record (14/20)
    assert mem_on.rate == 0.75   # memory-on (15/20)
    assert mem_on.rate - layered.rate == pytest.approx(0.05)  # +5pp memory ablation

    for m in metrics:
        assert m.bucket == "agent"
        assert 0.0 <= m.ci_low <= m.ci_high <= 1.0
        assert m.k <= m.n


def test_agent_metrics_reproducible():
    a = {(m.name, m.rate) for m in aggregate_agent_metrics()}
    b = {(m.name, m.rate) for m in aggregate_agent_metrics()}
    assert a == b
