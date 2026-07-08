import json

from gameforge.agents.playtest.executor import Executor
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode


class _FixedTransport:
    """Returns a canned response for any request (agent-logic test double, no network)."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=self.text)


def _router(text, tmp_path):
    return ModelRouter(_FixedTransport(text), CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)


def test_executor_returns_action_and_request_hash(tmp_path):
    payload = json.dumps({"kind": "navigate_to", "target": "npc:qi"})
    action, h = Executor().act(
        {"quest": "q1", "step_kind": "talk"}, "state text", _router(payload, tmp_path)
    )
    assert action.kind == "navigate_to"
    assert action.target == "npc:qi"
    assert isinstance(h, str) and h


def test_executor_falls_back_to_observe_on_unparseable_output(tmp_path):
    action, h = Executor().act(
        {"quest": "q1", "step_kind": "talk"}, "state text", _router("not json", tmp_path)
    )
    assert action.kind == "observe"
    assert isinstance(h, str) and h


def test_executor_falls_back_to_observe_on_missing_required_field(tmp_path):
    payload = json.dumps({"kind": "navigate_to"})  # missing target
    action, h = Executor().act(
        {"quest": "q1", "step_kind": "talk"}, "state text", _router(payload, tmp_path)
    )
    assert action.kind == "observe"
    assert isinstance(h, str) and h


def test_executor_falls_back_to_observe_on_unknown_kind(tmp_path):
    payload = json.dumps({"kind": "bogus"})
    action, h = Executor().act(
        {"quest": "q1", "step_kind": "talk"}, "state text", _router(payload, tmp_path)
    )
    assert action.kind == "observe"
    assert isinstance(h, str) and h
