"""Versioned authority for the Agent graph an admitted Run may execute.

``ExecutionVersionPlanV1`` freezes model-routing choices for one Run, but a client
cannot be the authority for which Agent nodes, prompt versions, or tool versions a
registered executor actually implements.  These pure contracts retain that second
half of the closure.  Platform admission resolves an exact graph by
``(RunKindRef, agent_graph_version)`` and compares every planned node before a Run is
made executable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.execution_profiles import JsonPointer, RunKindRef


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _json_data(value: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    raw = _json_value(value)
    if not isinstance(raw, dict):  # pragma: no cover - type boundary is explicit
        raise TypeError("Agent execution graph digest requires an object payload")
    return raw


class AgentExecutionNodeV1(_FrozenModel):
    """One callable node in a retained Agent graph."""

    node_schema_version: Literal["agent-execution-node@1"] = "agent-execution-node@1"
    agent_node_id: NonEmptyStr
    prompt_version: NonEmptyStr
    tool_version: NonEmptyStr
    required_capabilities: tuple[NonEmptyStr, ...] = ()

    @field_validator("required_capabilities")
    @classmethod
    def _canonical_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if len(canonical) != len(value):
            raise ValueError("Agent execution node capabilities must be unique")
        return canonical


class AgentExecutionProfileSelectorV1(_FrozenModel):
    """Select a graph from one exact resolved profile's immutable config."""

    selector_schema_version: Literal["agent-graph-profile-selector@1"] = (
        "agent-graph-profile-selector@1"
    )
    profile_field_path: JsonPointer
    config_pointer: JsonPointer
    expected_value: NonEmptyStr

    @field_validator("profile_field_path", "config_pointer")
    @classmethod
    def _non_root_pointer(cls, value: str) -> str:
        if not value:
            raise ValueError("Agent graph profile selectors require non-root pointers")
        return value


def agent_execution_graph_digest(value: Mapping[str, Any] | BaseModel) -> str:
    """Digest a graph after canonical node ordering, excluding its digest field."""

    raw = _json_data(value)
    raw.pop("graph_digest", None)
    raw.setdefault("graph_schema_version", "agent-execution-graph@1")
    raw["nodes"] = sorted(raw.get("nodes", ()), key=lambda item: item["agent_node_id"])
    return canonical_sha256(raw)


class AgentExecutionGraphV1(_FrozenModel):
    """Exact node/prompt/tool graph supported by one versioned Run executor."""

    graph_schema_version: Literal["agent-execution-graph@1"] = "agent-execution-graph@1"
    agent_graph_version: NonEmptyStr
    run_kind: RunKindRef
    executor_key: NonEmptyStr
    status: Literal["active", "replay_only", "disabled"] = "active"
    profile_selector: AgentExecutionProfileSelectorV1 | None = None
    nodes: tuple[AgentExecutionNodeV1, ...] = Field(min_length=1, max_length=128)
    graph_digest: Sha256Hex

    @field_validator("nodes")
    @classmethod
    def _canonical_nodes(
        cls, value: tuple[AgentExecutionNodeV1, ...]
    ) -> tuple[AgentExecutionNodeV1, ...]:
        node_ids = [item.agent_node_id for item in value]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Agent execution graph node ids must be unique")
        return tuple(sorted(value, key=lambda item: item.agent_node_id))

    @model_validator(mode="after")
    def _closed_digest(self) -> "AgentExecutionGraphV1":
        if self.graph_digest != agent_execution_graph_digest(self):
            raise ValueError("graph_digest does not match Agent execution graph")
        return self


__all__ = [
    "AgentExecutionGraphV1",
    "AgentExecutionNodeV1",
    "AgentExecutionProfileSelectorV1",
    "agent_execution_graph_digest",
]
