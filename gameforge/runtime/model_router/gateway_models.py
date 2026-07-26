"""Read the gateway's own `/v1/models` listing.

The gateway is the only thing that knows which models it currently serves and on
which endpoint each one answers. Everything downstream — the model catalog a run
freezes, the transport a request is dispatched to, the picker a planner chooses
from — is derived from this one reading, so none of them can disagree about what
`gpt-5.6-sol` or `claude-opus-5` is or how to reach it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.routing import ApiFlavor, GatewayModelV1

GATEWAY_MODELS_PATH = "/v1/models"
# One deployment, one gateway: the worker calls models through it and the API reads
# which models it serves, so both processes are configured from the same two names.
MODEL_GATEWAY_BASE_URL_ENV = "GAMEFORGE_LLM_BASE_URL"
MODEL_GATEWAY_API_KEY_ENV = "GAMEFORGE_LLM_KEY"
DEFAULT_MODEL_GATEWAY_BASE_URL = "http://localhost:4141"

# A gateway lists every endpoint a model serves; we send it on the most native one
# we speak. Anthropic's Messages API carries system prompts and cache breakpoints
# the OpenAI-compatible shim flattens, and OpenAI's Responses API is the only
# surface its current models answer on at all.
_ENDPOINT_FLAVORS: tuple[tuple[str, ApiFlavor], ...] = (
    ("/v1/messages", "anthropic_messages"),
    ("/responses", "responses"),
    ("/chat/completions", "chat_completions"),
)
# Reported by the gateway alongside the request/response capabilities we read from
# `supports`; `reasoning_effort` is a list of levels rather than a boolean.
_SUPPORT_CAPABILITIES = ("structured_outputs", "tool_calls", "vision")


class _GatewayLimits(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_context_window_tokens: int | None = None
    max_output_tokens: int | None = None


class _GatewayCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    limits: _GatewayLimits = Field(default_factory=_GatewayLimits)
    supports: dict[str, Any] = Field(default_factory=dict)


class _GatewayPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: str | None = None


class _GatewayEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str | None = None
    name: str | None = None
    vendor: str | None = None
    version: str | None = None
    model_picker_category: str | None = None
    preview: bool = False
    supported_endpoints: tuple[str, ...] | None = None
    capabilities: _GatewayCapabilities = Field(default_factory=_GatewayCapabilities)
    policy: _GatewayPolicy | None = None


def parse_gateway_models(payload: object) -> tuple[GatewayModelV1, ...]:
    """Read every model this gateway can serve an agent call on."""

    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), Sequence):
        raise IntegrityViolation("model gateway listing has an unreadable shape")
    entries = tuple(_GatewayEntry.model_validate(item) for item in payload["data"])
    models = tuple(
        model for model in (_read_entry(entry) for entry in entries) if model is not None
    )
    if not models:
        raise IntegrityViolation("model gateway lists no callable chat model")
    return tuple(sorted(models, key=lambda model: model.model))


def fetch_gateway_models(
    *,
    base_url: str,
    api_key: str,
    client: Any | None = None,
    timeout_s: float = 30.0,
) -> tuple[GatewayModelV1, ...]:
    """Read the live listing from the gateway this deployment calls."""

    http = client if client is not None else httpx.Client(timeout=timeout_s)
    try:
        response = http.get(
            f"{base_url.rstrip('/')}{GATEWAY_MODELS_PATH}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        return parse_gateway_models(response.json())
    finally:
        if client is None:
            http.close()


def gateway_model_reader(
    environment: Mapping[str, str] | None = None,
) -> Callable[[], tuple[GatewayModelV1, ...]] | None:
    """A live reader, or None when this deployment reaches no gateway.

    Reading is deliberately not cached: the picker is opened to find out what is
    served right now, and a stale list offers a model the gateway has dropped.
    """

    source = os.environ if environment is None else environment
    api_key = source.get(MODEL_GATEWAY_API_KEY_ENV)
    if not api_key:
        return None
    base_url = source.get(MODEL_GATEWAY_BASE_URL_ENV, DEFAULT_MODEL_GATEWAY_BASE_URL)
    return lambda: fetch_gateway_models(base_url=base_url, api_key=api_key)


def _read_entry(entry: _GatewayEntry) -> GatewayModelV1 | None:
    if entry.capabilities.type != "chat":
        return None
    if entry.policy is not None and entry.policy.state not in (None, "enabled"):
        return None
    flavor = _api_flavor(entry.supported_endpoints or ())
    if flavor is None:
        return None
    limits = entry.capabilities.limits
    if not limits.max_context_window_tokens or not limits.max_output_tokens:
        raise IntegrityViolation(
            "model gateway serves a chat model it reports no token limits for",
            model=entry.id,
        )
    return GatewayModelV1(
        model=entry.id,
        display_name=entry.display_name or entry.name or entry.id,
        vendor=entry.vendor or "unknown",
        served_version=entry.version or entry.id,
        # The gateway's own tiering ("powerful"/"versatile"/"lightweight"); routing
        # decisions record it, so it must be the gateway's word and not ours.
        tier=entry.model_picker_category or "unclassified",
        api_flavor=flavor,
        capabilities=_capabilities(entry.capabilities.supports),
        context_limit=limits.max_context_window_tokens,
        max_output_tokens=limits.max_output_tokens,
        # Only the native surfaces carry the prefix-cache directive; the
        # OpenAI-compatible chat shim has nowhere to put it.
        prompt_cache_support=flavor != "chat_completions",
        preview=entry.preview,
    )


def _api_flavor(endpoints: Sequence[str]) -> ApiFlavor | None:
    served = set(endpoints)
    for endpoint, flavor in _ENDPOINT_FLAVORS:
        if endpoint in served:
            return flavor
    return None


def _capabilities(supports: Mapping[str, Any]) -> tuple[str, ...]:
    reported = ["reasoning"] if supports.get("reasoning_effort") else []
    reported.extend(name for name in _SUPPORT_CAPABILITIES if supports.get(name))
    return tuple(sorted(reported))


__all__ = [
    "DEFAULT_MODEL_GATEWAY_BASE_URL",
    "GATEWAY_MODELS_PATH",
    "MODEL_GATEWAY_API_KEY_ENV",
    "MODEL_GATEWAY_BASE_URL_ENV",
    "fetch_gateway_models",
    "gateway_model_reader",
    "parse_gateway_models",
]
