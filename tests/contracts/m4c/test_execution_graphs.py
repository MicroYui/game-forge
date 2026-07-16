from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.execution_graphs import (
    AgentExecutionGraphV1,
    AgentExecutionNodeV1,
    AgentExecutionProfileSelectorV1,
    agent_execution_graph_digest,
)
from gameforge.contracts.execution_profiles import RunKindRef


def _node(node_id: str) -> AgentExecutionNodeV1:
    return AgentExecutionNodeV1(
        agent_node_id=node_id,
        prompt_version=f"{node_id}-prompt@1",
        tool_version=f"{node_id}-tool@1",
        required_capabilities=("reasoning",),
    )


def _graph(*nodes: AgentExecutionNodeV1) -> AgentExecutionGraphV1:
    body = {
        "agent_graph_version": "test-graph@1",
        "run_kind": RunKindRef(kind="generation.propose", version=1),
        "executor_key": "generation_proposer@1",
        "status": "active",
        "profile_selector": None,
        "nodes": nodes,
    }
    return AgentExecutionGraphV1(
        **body,
        graph_digest=agent_execution_graph_digest(body),
    )


def test_execution_graph_digest_and_nodes_are_canonical_across_input_order() -> None:
    first = _graph(_node("zeta"), _node("alpha"))
    second = _graph(_node("alpha"), _node("zeta"))

    assert first == second
    assert first.graph_digest == second.graph_digest
    assert tuple(node.agent_node_id for node in first.nodes) == ("alpha", "zeta")


def test_execution_graph_rejects_stale_digest() -> None:
    payload = _graph(_node("generation")).model_dump(mode="python")
    payload["graph_digest"] = "f" * 64

    with pytest.raises(ValidationError, match="graph_digest"):
        AgentExecutionGraphV1.model_validate(payload)


def test_execution_graph_rejects_duplicate_node_ids() -> None:
    node = _node("generation")
    body = {
        "agent_graph_version": "test-graph@1",
        "run_kind": RunKindRef(kind="generation.propose", version=1),
        "executor_key": "generation_proposer@1",
        "nodes": (node, node),
    }

    with pytest.raises(ValidationError, match="node ids must be unique"):
        AgentExecutionGraphV1(
            **body,
            graph_digest=agent_execution_graph_digest(body),
        )


@pytest.mark.parametrize("field", ["profile_field_path", "config_pointer"])
def test_execution_graph_selector_rejects_root_pointer(field: str) -> None:
    payload = {
        "profile_field_path": "/params/planner_policy",
        "config_pointer": "/memory_mode",
        "expected_value": "off",
    }
    payload[field] = ""

    with pytest.raises(ValidationError, match="non-root pointers"):
        AgentExecutionProfileSelectorV1.model_validate(payload)
