"""Model Router request/response schema (contract §7) — single source of truth.

Only the deterministic request_hash lives here; HTTP-to-gateway + record/replay
are runtime/ concerns. request_hash EXCLUDES cache_key / schema_version — it is
exactly the set of fields that determine the model's output (contract §7).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

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


ModelRequestV1 = ModelRequest


PrefixHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


def compute_prefix_hash(messages: Sequence[Message]) -> str:
    payload = [message.model_dump() for message in messages]
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class PrefixCacheDirectiveV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    directive_schema_version: Literal["prefix-cache-directive@1"] = "prefix-cache-directive@1"
    prefix_message_count: int = Field(ge=1, le=256)
    prefix_hash: PrefixHash
    provider_scope: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    policy_version: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class ModelRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    model_router_schema_version: Literal["model-router@2"] = "model-router@2"
    model_snapshot: ModelSnapshot
    messages: list[Message]
    params: dict[str, Any] = Field(default_factory=dict)
    tool_schemas: list[ToolSchemaRef] = Field(default_factory=list)
    agent_node_id: str
    prompt_version: str
    prefix_cache_directive: PrefixCacheDirectiveV1 | None = None

    @model_validator(mode="after")
    def validate_prefix_directive(self) -> ModelRequestV2:
        directive = self.prefix_cache_directive
        if directive is None:
            return self
        if directive.prefix_message_count > len(self.messages):
            raise ValueError("prefix_message_count exceeds request messages")
        expected = compute_prefix_hash(self.messages[: directive.prefix_message_count])
        if directive.prefix_hash != expected:
            raise ValueError("prefix_hash does not match exact request message prefix")
        if directive.provider_scope != self.model_snapshot.provider:
            raise ValueError("prefix cache provider scope differs from model provider")
        return self


@dataclass(frozen=True, slots=True)
class ModelBridgeCallRequestV1:
    """Exact executor-to-worker request for one ordered model call."""

    model_request: ModelRequestV2
    source_artifact_ids: tuple[str, ...]
    idempotency_scope: str
    idempotency_key: str
    route_ordinal: int = 1
    deadline_utc: datetime | None = None

    def __post_init__(self) -> None:
        if self.source_artifact_ids != tuple(sorted(set(self.source_artifact_ids))) or not (
            self.source_artifact_ids
        ):
            raise ValueError("model-call source_artifact_ids must be stable-unique")


class ModelResponse(BaseModel):
    response_normalized: str
    raw_response: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    token_usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


def request_hash(req: ModelRequest | ModelRequestV2) -> str:
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


def parse_model_request(payload: Mapping[str, Any]) -> ModelRequest | ModelRequestV2:
    version = payload.get("model_router_schema_version")
    if version == MODEL_ROUTER_SCHEMA_VERSION:
        return ModelRequest.model_validate(payload)
    if version == "model-router@2":
        return ModelRequestV2.model_validate(payload)
    raise ValueError(f"unsupported model router schema version: {version!r}")


__all__ = [
    "Message",
    "ModelBridgeCallRequestV1",
    "ModelRequest",
    "ModelRequestV1",
    "ModelRequestV2",
    "ModelResponse",
    "ModelSnapshot",
    "PrefixCacheDirectiveV1",
    "ToolSchemaRef",
    "compute_prefix_hash",
    "parse_model_request",
    "request_hash",
]
