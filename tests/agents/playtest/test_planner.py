import json

from gameforge.agents.playtest.planner import Planner
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


def test_planner_returns_subgoal_and_request_hash(tmp_path):
    payload = json.dumps({"quest": "q1", "step_kind": "collect", "need_item": "item:herb"})
    subgoal, h = Planner().plan("state: tick=3 quests=[q1]", _router(payload, tmp_path))
    assert subgoal["quest"] == "q1"
    assert subgoal["step_kind"] == "collect"
    assert subgoal["need_item"] == "item:herb"
    assert not subgoal.get("_fallback")
    assert isinstance(h, str) and h


def test_planner_falls_back_to_advance_on_unparseable_output(tmp_path):
    subgoal, h = Planner().plan("state: tick=9", _router("not json", tmp_path))
    assert subgoal == {"quest": None, "step_kind": "advance", "_fallback": True}
    assert isinstance(h, str) and h
