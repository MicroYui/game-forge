"""M2a-part1 地基验收：Model Router + Cassette + 编排 的确定性复现 (零实网)。"""
from gameforge.contracts.agent_io import AgentNodeResult
from gameforge.contracts.model_router import Message, ModelRequest, ModelResponse, ModelSnapshot, request_hash
from gameforge.agents.orchestrator import run_graph
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.runtime.model_router.transport import StubTransport

_SNAP = ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1")


class _TwoCallNode:
    node_id = "twocall"

    def run(self, input, router):
        hashes = []
        answers = []
        for turn in input["turns"]:
            req = ModelRequest(
                model_snapshot=_SNAP,
                messages=[Message(role="user", content=turn)],
                agent_node_id=self.node_id, prompt_version="twocall@1",
            )
            answers.append(router.call(req).response_normalized)
            hashes.append(request_hash(req))
        return AgentNodeResult(role="triage", model_run_id="r", request_hashes=hashes,
                               produced={"answers": answers})


def _req(text):
    return ModelRequest(model_snapshot=_SNAP, messages=[Message(role="user", content=text)],
                        agent_node_id="twocall", prompt_version="twocall@1")


def test_foundations_record_then_replay_reproduces_byte_identical(tmp_path):
    turns = ["look around", "open the door"]
    canned = {request_hash(_req(t)): ModelResponse(response_normalized=f"did:{t}") for t in turns}
    store = CassetteStore(tmp_path)

    rec_out = run_graph([_TwoCallNode()], {"twocall": {"turns": turns}},
                        ModelRouter(StubTransport(canned), store, mode=RouterMode.RECORD))

    class _NoLive:
        def complete(self, r): raise AssertionError("REPLAY hit the network")

    rep_out = run_graph([_TwoCallNode()], {"twocall": {"turns": turns}},
                        ModelRouter(_NoLive(), store, mode=RouterMode.REPLAY))

    assert rec_out[0].produced == {"answers": ["did:look around", "did:open the door"]}
    assert rec_out[0].model_dump() == rep_out[0].model_dump()
    # every LLM call is a committed cassette file
    assert len(list(tmp_path.glob("*.json"))) == 2
