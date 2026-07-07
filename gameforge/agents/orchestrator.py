"""Deterministic agent orchestration (决策B: 自研状态机, 不引 LangGraph).

Nodes run in the given order, no concurrency, no hidden state — so a run under
REPLAY reproduces byte-identically. Each node is a typed I/O contract that
reaches the LLM only through ModelRouter (never a direct SDK call).
"""
from __future__ import annotations

from typing import Protocol

from gameforge.contracts.agent_io import AgentNodeResult
from gameforge.runtime.model_router.router import ModelRouter


class AgentNode(Protocol):
    node_id: str

    def run(self, input: object, router: ModelRouter) -> AgentNodeResult: ...


def run_graph(
    nodes: list[AgentNode],
    inputs: dict[str, object],
    router: ModelRouter,
) -> list[AgentNodeResult]:
    return [node.run(inputs[node.node_id], router) for node in nodes]
