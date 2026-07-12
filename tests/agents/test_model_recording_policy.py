from __future__ import annotations

import pytest

from gameforge.agents import harness, playtest_harness
from gameforge.agents.base import DEFAULT_SNAPSHOT, M2_REPLAY_SNAPSHOT
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.openai_responses_transport import (
    OpenAIResponsesTransport,
)
from gameforge.runtime.model_router.router import ModelRouter, RouterMode


@pytest.mark.parametrize("module", [harness, playtest_harness])
def test_record_router_uses_new_default_over_responses_transport(
    module, monkeypatch, tmp_path
):
    monkeypatch.setenv("GAMEFORGE_LLM_KEY", "test-key")

    router = module.record_router(str(tmp_path))

    assert router.default_model_snapshot == DEFAULT_SNAPSHOT
    assert isinstance(router._transport, OpenAIResponsesTransport)


def test_repair_replay_uses_active_default(tmp_path):
    router = harness.replay_router(str(tmp_path))

    assert router.default_model_snapshot == DEFAULT_SNAPSHOT


def test_historical_agent_samples_keep_m2_snapshot(tmp_path):
    router = harness.historical_replay_router(str(tmp_path))

    assert router.default_model_snapshot == M2_REPLAY_SNAPSHOT


def test_playtest_replay_keeps_m2_snapshot(tmp_path):
    router = playtest_harness.replay_router(str(tmp_path))

    assert router.default_model_snapshot == M2_REPLAY_SNAPSHOT


class _RecordingJsonTransport:
    def __init__(self):
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized="[]")


def test_agent_sample_recording_routes_only_generation_to_active_model(tmp_path):
    active_transport = _RecordingJsonTransport()
    historical_transport = _RecordingJsonTransport()
    active_router = ModelRouter(
        active_transport,
        CassetteStore(tmp_path / "active"),
        mode=RouterMode.PASSTHROUGH,
        default_model_snapshot=DEFAULT_SNAPSHOT,
    )
    historical_router = ModelRouter(
        historical_transport,
        CassetteStore(tmp_path / "historical"),
        mode=RouterMode.PASSTHROUGH,
        default_model_snapshot=M2_REPLAY_SNAPSHOT,
    )

    harness._record_agent_samples(active_router, historical_router)

    assert {req.agent_node_id for req in active_transport.calls} == {"generation"}
    historical_nodes = {req.agent_node_id for req in historical_transport.calls}
    assert historical_nodes == {"extraction", "consistency"}
    assert all(req.model_snapshot == DEFAULT_SNAPSHOT for req in active_transport.calls)
    assert all(
        req.model_snapshot == M2_REPLAY_SNAPSHOT
        for req in historical_transport.calls
    )
