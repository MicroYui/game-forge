"""The fixed model reading the browser journeys route to instead of a gateway.

Two models with two different API surfaces, so a deterministic journey exercises
the same multi-model catalog, per-model routing policy and picker shape a real
deployment seeds from the live gateway — with no network at all.
"""

from __future__ import annotations

from gameforge.contracts.routing import GatewayModelV1

STUB_MODELS: tuple[GatewayModelV1, ...] = (
    GatewayModelV1(
        model="gpt-5.6-sol",
        display_name="GPT-5.6 Sol",
        vendor="OpenAI",
        served_version="gpt-5.6-sol",
        tier="powerful",
        api_flavor="responses",
        capabilities=("reasoning", "structured_outputs", "tool_calls"),
        context_limit=1_050_000,
        max_output_tokens=128_000,
        prompt_cache_support=True,
        preview=False,
    ),
    GatewayModelV1(
        model="claude-opus-5",
        display_name="Claude Opus 5",
        vendor="Anthropic",
        served_version="claude-opus-5",
        tier="powerful",
        api_flavor="anthropic_messages",
        capabilities=("reasoning", "structured_outputs", "tool_calls"),
        context_limit=1_000_000,
        max_output_tokens=64_000,
        prompt_cache_support=True,
        preview=False,
    ),
)

__all__ = ["STUB_MODELS"]
