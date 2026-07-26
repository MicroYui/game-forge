"""The gateway's own listing is the authority on what can be called, and how.

`/v1/models` reports, per model, the endpoints it serves — `gpt-5.6-sol` only on
`/responses`, `claude-opus-5` on `/v1/messages` and `/chat/completions`, Gemini
only on `/chat/completions`. Reading that report is how the product learns which
models a planner may pick and which transport each one needs; hand-maintaining a
second table of the same fact is how the two drift.
"""

from __future__ import annotations

from typing import Any

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.runtime.model_router.gateway_models import (
    fetch_gateway_models,
    parse_gateway_models,
)


def _entry(
    model_id: str,
    *,
    vendor: str,
    endpoints: list[str] | None,
    kind: str = "chat",
    context: int | None = 1_000_000,
    output: int | None = 64_000,
    supports: dict[str, Any] | None = None,
    state: str = "enabled",
    preview: bool = False,
) -> dict[str, Any]:
    limits: dict[str, Any] = {}
    if context is not None:
        limits["max_context_window_tokens"] = context
    if output is not None:
        limits["max_output_tokens"] = output
    return {
        "id": model_id,
        "display_name": model_id.upper(),
        "name": model_id.upper(),
        "vendor": vendor,
        "version": model_id,
        "model_picker_category": "powerful",
        "preview": preview,
        "object": "model",
        "supported_endpoints": endpoints,
        "policy": {"state": state},
        "capabilities": {
            "type": kind,
            "limits": limits,
            "supports": supports if supports is not None else {"tool_calls": True},
        },
    }


def _listing(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"object": "list", "data": list(entries)}


def test_each_model_is_read_with_the_surface_it_serves() -> None:
    models = parse_gateway_models(
        _listing(
            _entry("gpt-5.6-sol", vendor="OpenAI", endpoints=["/responses", "ws:/responses"]),
            _entry(
                "claude-opus-5",
                vendor="Anthropic",
                endpoints=["/v1/messages", "/chat/completions"],
            ),
            _entry("gemini-3.6-flash", vendor="Google", endpoints=["/chat/completions"]),
        )
    )

    assert {model.model: model.api_flavor for model in models} == {
        "gpt-5.6-sol": "responses",
        # Anthropic serves both; the native Messages surface is the one we speak.
        "claude-opus-5": "anthropic_messages",
        "gemini-3.6-flash": "chat_completions",
    }


def test_models_that_cannot_serve_an_agent_call_are_left_out() -> None:
    models = parse_gateway_models(
        _listing(
            _entry("gpt-5.6-sol", vendor="OpenAI", endpoints=["/responses"]),
            _entry(
                "text-embedding-3-small", vendor="Azure OpenAI", endpoints=None, kind="embeddings"
            ),
            _entry("gemini-2.5-pro", vendor="Google", endpoints=None),
            _entry("retired-model", vendor="OpenAI", endpoints=["/responses"], state="disabled"),
        )
    )

    assert [model.model for model in models] == ["gpt-5.6-sol"]


def test_a_callable_model_without_token_limits_fails_closed() -> None:
    # Routing compares a request against the model's limits; a model we could call
    # but cannot bound is worse than one we skip.
    with pytest.raises(IntegrityViolation, match="token limits"):
        parse_gateway_models(
            _listing(_entry("gpt-5.6-sol", vendor="OpenAI", endpoints=["/responses"], output=None))
        )


def test_a_listing_with_nothing_callable_fails_closed() -> None:
    with pytest.raises(IntegrityViolation, match="callable"):
        parse_gateway_models(_listing(_entry("ada", vendor="OpenAI", endpoints=None)))


def test_what_the_gateway_reports_becomes_what_the_planner_sees() -> None:
    (model,) = parse_gateway_models(
        _listing(
            _entry(
                "claude-opus-5",
                vendor="Anthropic",
                endpoints=["/v1/messages"],
                context=1_000_000,
                output=64_000,
                preview=True,
                supports={
                    "reasoning_effort": ["low", "high"],
                    "structured_outputs": True,
                    "tool_calls": True,
                    "vision": False,
                },
            )
        )
    )

    assert model.display_name == "CLAUDE-OPUS-5"
    assert model.vendor == "Anthropic"
    assert model.served_version == "claude-opus-5"
    assert model.tier == "powerful"
    assert model.context_limit == 1_000_000
    assert model.max_output_tokens == 64_000
    assert model.preview is True
    assert model.capabilities == ("reasoning", "structured_outputs", "tool_calls")
    # The Messages surface carries our prefix-cache directive; chat completions does not.
    assert model.prompt_cache_support is True


def test_prompt_cache_follows_the_surface_that_carries_the_directive() -> None:
    (model,) = parse_gateway_models(
        _listing(_entry("gemini-3.6-flash", vendor="Google", endpoints=["/chat/completions"]))
    )

    assert model.prompt_cache_support is False


def test_the_listing_is_read_over_http_with_the_gateway_key() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def get(self, url: str, **kwargs: Any) -> Any:
            self.calls.append((url, kwargs))
            return _Response(
                _listing(_entry("gpt-5.6-sol", vendor="OpenAI", endpoints=["/responses"]))
            )

    class _Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    client = _Client()
    models = fetch_gateway_models(
        base_url="http://localhost:4141/",
        api_key="sk-test",
        client=client,
    )

    assert [model.model for model in models] == ["gpt-5.6-sol"]
    url, kwargs = client.calls[0]
    assert url == "http://localhost:4141/v1/models"
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
