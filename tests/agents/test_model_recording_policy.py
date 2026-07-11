from __future__ import annotations

import pytest

from gameforge.agents import harness, playtest_harness
from gameforge.agents.base import DEFAULT_SNAPSHOT, M2_REPLAY_SNAPSHOT
from gameforge.runtime.model_router.openai_responses_transport import (
    OpenAIResponsesTransport,
)


@pytest.mark.parametrize("module", [harness, playtest_harness])
def test_record_router_uses_new_default_over_responses_transport(
    module, monkeypatch, tmp_path
):
    monkeypatch.setenv("GAMEFORGE_LLM_KEY", "test-key")

    router = module.record_router(str(tmp_path))

    assert router.default_model_snapshot == DEFAULT_SNAPSHOT
    assert isinstance(router._transport, OpenAIResponsesTransport)


@pytest.mark.parametrize("module", [harness, playtest_harness])
def test_replay_router_pins_the_historical_m2_snapshot(module, tmp_path):
    router = module.replay_router(str(tmp_path))

    assert router.default_model_snapshot == M2_REPLAY_SNAPSHOT
