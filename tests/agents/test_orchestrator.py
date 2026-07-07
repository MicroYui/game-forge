from gameforge.contracts.agent_io import AgentNodeResult
from gameforge.contracts.model_router import Message, ModelRequest, ModelResponse, ModelSnapshot, request_hash
from gameforge.agents.orchestrator import run_graph
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.runtime.model_router.transport import StubTransport


class _EchoNode:
    node_id = "echo"

    def run(self, input, router):
        req = ModelRequest(
            model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
            messages=[Message(role="user", content=input["text"])],
            agent_node_id=self.node_id, prompt_version="echo@1",
        )
        resp = router.call(req)
        return AgentNodeResult(
            role="triage", model_run_id="run1", request_hashes=[request_hash(req)],
            produced={"answer": resp.response_normalized},
        )


def test_run_graph_is_deterministic_under_replay(tmp_path):
    node = _EchoNode()
    probe_req = ModelRequest(
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        messages=[Message(role="user", content="hi")],
        agent_node_id="echo", prompt_version="echo@1",
    )
    stub = StubTransport({request_hash(probe_req): ModelResponse(response_normalized="pong")})
    store = CassetteStore(tmp_path)

    rec = ModelRouter(stub, store, mode=RouterMode.RECORD)
    out1 = run_graph([node], {"echo": {"text": "hi"}}, rec)

    rep = ModelRouter(StubTransport({}), store, mode=RouterMode.REPLAY)
    out2 = run_graph([node], {"echo": {"text": "hi"}}, rep)

    assert out1[0].produced == {"answer": "pong"}
    assert out1[0].model_dump() == out2[0].model_dump()  # replay reproduces byte-identically


class _RecordingNode:
    """Node that records call order — used to prove run_graph is sequential and
    keys each node's input by its own node_id (not by list position)."""

    def __init__(self, node_id, log):
        self.node_id = node_id
        self._log = log

    def run(self, input, router):
        self._log.append(self.node_id)
        return AgentNodeResult(
            role="triage", model_run_id=self.node_id, produced={"got": input["v"]},
        )


def test_run_graph_executes_nodes_in_order_and_keys_input_by_node_id(tmp_path):
    log: list[str] = []
    nodes = [_RecordingNode("a", log), _RecordingNode("b", log), _RecordingNode("c", log)]
    inputs = {"c": {"v": 3}, "a": {"v": 1}, "b": {"v": 2}}  # deliberately out of nodes-order
    router = ModelRouter(StubTransport({}), CassetteStore(tmp_path), mode=RouterMode.REPLAY)

    out = run_graph(nodes, inputs, router)

    assert log == ["a", "b", "c"]  # sequential, follows `nodes` order, not `inputs` order
    assert [r.produced["got"] for r in out] == [1, 2, 3]  # each node got its own keyed input
