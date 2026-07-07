"""Model Router request/response schema (contract §7) — single source of truth.

Only the deterministic request_hash lives here; HTTP-to-gateway + record/replay
are runtime/ concerns. request_hash EXCLUDES cache_key / schema_version — it is
exactly the set of fields that determine the model's output (contract §7).
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.versions import MODEL_ROUTER_SCHEMA_VERSION


class ModelSnapshot(BaseModel):
    provider: str
    model: str
    snapshot_tag: str  # pins a served version; guards against silent upgrades


class ToolSchemaRef(BaseModel):
    name: str
    version: str


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ModelRequest(BaseModel):
    model_router_schema_version: str = MODEL_ROUTER_SCHEMA_VERSION
    model_snapshot: ModelSnapshot
    messages: list[Message]
    params: dict[str, Any] = Field(default_factory=dict)
    tool_schemas: list[ToolSchemaRef] = Field(default_factory=list)
    agent_node_id: str
    prompt_version: str
    cache_key: str | None = None  # semantic-cache hint; NOT part of request_hash


class ModelResponse(BaseModel):
    response_normalized: str
    raw_response: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    token_usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


def request_hash(req: ModelRequest) -> str:
    payload = {
        "model_snapshot": req.model_snapshot.model_dump(),
        "messages": [m.model_dump() for m in req.messages],
        "tool_schema_versions": [[t.name, t.version] for t in req.tool_schemas],
        "params": req.params,
        "agent_node_id": req.agent_node_id,
        "prompt_version": req.prompt_version,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
