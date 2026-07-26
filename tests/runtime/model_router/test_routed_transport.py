"""A request must reach the API flavour its model actually speaks.

The local gateway does not expose one uniform surface: `gpt-5.6-sol` is rejected by
`/chat/completions` and only answers on `/responses`, while `claude-opus-5` answers
on both `/chat/completions` and `/messages`. Picking the transport per call site —
as every harness used to — makes the choice of model and the choice of protocol two
independent decisions that silently disagree.
"""

from __future__ import annotations

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import Message, ModelRequest, ModelResponse, ModelSnapshot
from gameforge.runtime.model_router.routed_transport import ApiFlavorRoutedTransport


class _RecordingTransport:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[ModelRequest] = []

    def complete(self, req: ModelRequest) -> ModelResponse:
        self.calls.append(req)
        return ModelResponse(
            response_normalized=self.name,
            raw_response={},
            latency_ms=1,
            token_usage={},
            finish_reason="stop",
            tool_calls=[],
        )

    def complete_with_timeout(self, req: ModelRequest, *, timeout_s: float) -> ModelResponse:
        del timeout_s
        return self.complete(req)


def _request(model: str) -> ModelRequest:
    return ModelRequest(
        model_snapshot=ModelSnapshot(provider="openai", model=model, snapshot_tag="local@1"),
        messages=[Message(role="user", content="ping")],
        agent_node_id="extraction_proposer",
        prompt_version="extraction@1",
    )


def _routed() -> tuple[ApiFlavorRoutedTransport, dict[str, _RecordingTransport]]:
    transports = {
        "chat_completions": _RecordingTransport("chat"),
        "responses": _RecordingTransport("responses"),
        "anthropic_messages": _RecordingTransport("messages"),
    }
    routed = ApiFlavorRoutedTransport(
        flavors={"gpt-5.6-sol": "responses", "claude-opus-5": "anthropic_messages"},
        transports=transports,
    )
    return routed, transports


def test_each_model_reaches_the_api_it_speaks() -> None:
    routed, transports = _routed()

    assert routed.complete(_request("gpt-5.6-sol")).response_normalized == "responses"
    assert routed.complete(_request("claude-opus-5")).response_normalized == "messages"
    assert [req.model_snapshot.model for req in transports["responses"].calls] == ["gpt-5.6-sol"]
    assert [req.model_snapshot.model for req in transports["anthropic_messages"].calls] == [
        "claude-opus-5"
    ]
    assert transports["chat_completions"].calls == []


def test_a_model_without_a_declared_flavour_fails_closed() -> None:
    routed, _ = _routed()

    # Guessing an API for an undeclared model is how a request reaches an endpoint
    # that rejects it; the catalog must say which surface the model answers on.
    with pytest.raises(IntegrityViolation, match="api flavour"):
        routed.complete(_request("gemini-3.6-flash"))


def test_a_declared_flavour_without_a_transport_fails_closed() -> None:
    routed = ApiFlavorRoutedTransport(
        flavors={"gpt-5.6-sol": "responses"},
        transports={"chat_completions": _RecordingTransport("chat")},
    )

    with pytest.raises(IntegrityViolation, match="transport"):
        routed.complete(_request("gpt-5.6-sol"))


def test_timeouts_reach_the_same_routed_transport() -> None:
    routed, transports = _routed()

    routed.complete_with_timeout(_request("claude-opus-5"), timeout_s=5.0)

    assert len(transports["anthropic_messages"].calls) == 1


def test_dispatch_table_is_derived_from_the_model_catalog() -> None:
    """The catalog is the authority on which surface each model answers on."""

    from gameforge.contracts.routing import ModelCatalogSnapshotV1, ModelDescriptorV1
    from gameforge.contracts.routing import compute_model_catalog_digest
    from gameforge.runtime.model_router.routed_transport import api_flavors_from_catalog
    from datetime import UTC, datetime

    def descriptor(provider: str, model: str, flavor: str) -> ModelDescriptorV1:
        return ModelDescriptorV1(
            provider=provider,
            model_snapshot=f"{provider}:{model}",
            tier="best",
            capabilities=("reasoning",),
            context_limit=200_000,
            max_output_tokens=32_000,
            prompt_cache_support=True,
            status="active",
            api_flavor=flavor,
        )

    body = {
        "catalog_version": 1,
        "models": (
            descriptor("openai", "gpt-5.6-sol", "responses"),
            descriptor("anthropic", "claude-opus-5", "anthropic_messages"),
        ),
        "created_at": datetime(2026, 7, 26, tzinfo=UTC),
    }
    catalog = ModelCatalogSnapshotV1(**body, catalog_digest=compute_model_catalog_digest(body))

    assert api_flavors_from_catalog(catalog) == {
        "gpt-5.6-sol": "responses",
        "claude-opus-5": "anthropic_messages",
    }
