import json

from gameforge.agents.extraction.proposer import ExtractionProposer
from gameforge.contracts.agent_io import DesignDocInput
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


def test_extraction_keeps_compilable_drops_invalid(tmp_path):
    payload = json.dumps([
        {"proposed_id": "C_cap", "kind": "numeric", "assert_expr": "reward_gold <= 80", "rationale": "cap"},
        {"proposed_id": "C_bad", "kind": "numeric", "assert_expr": "__import__('os').system('x')", "rationale": "evil"},
    ])
    res = ExtractionProposer().run(DesignDocInput(doc_text="doc", doc_version="v1"), _router(payload, tmp_path))
    props = res.produced["proposals"]
    assert len(props) == 1  # only the compilable constraint survives the oracle
    assert props[0]["proposed_id"] == "C_cap"
    assert props[0]["needs_human_authoring"] is True  # LLM proposes; human authors authoritative
    assert res.produced["dropped"] == 1
    assert res.fallback_taken is False
    assert res.role == "extraction"
    assert len(res.request_hashes) == 1


def test_extraction_fallback_on_unparseable_output(tmp_path):
    res = ExtractionProposer().run(DesignDocInput(doc_text="d", doc_version="v1"),
                                   _router("sorry, no json here", tmp_path))
    assert res.fallback_taken is True
    assert res.produced["proposals"] == []
