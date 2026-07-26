"""Route each model request to the API flavour that model actually speaks.

Providers do not agree on one surface, and the same gateway can expose several:
`gpt-5.6-sol` is rejected by `/chat/completions` and answers only on `/responses`,
`claude-opus-5` answers on `/chat/completions` and `/messages`. The model catalog
declares the flavour; this transport dispatches on that declaration instead of
letting each call site pick an endpoint that may not accept the chosen model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelRequest, ModelResponse
from gameforge.contracts.routing import ModelCatalogSnapshotV1
from gameforge.runtime.model_router.transport import LlmTransport

ApiFlavor = Literal["chat_completions", "responses", "anthropic_messages"]


def api_flavors_from_catalog(catalog: ModelCatalogSnapshotV1) -> dict[str, str]:
    """Model name -> declared API flavour, taken from the catalog that is authority."""

    return {
        descriptor.model_snapshot.split(":", 1)[1]: descriptor.api_flavor
        for descriptor in catalog.models
    }


class ApiFlavorRoutedTransport:
    """Dispatch by declared API flavour; never guess an endpoint for a model."""

    def __init__(
        self,
        *,
        flavors: Mapping[str, str],
        transports: Mapping[str, LlmTransport],
    ) -> None:
        self._flavors = dict(flavors)
        self._transports = dict(transports)

    def complete(self, req: ModelRequest) -> ModelResponse:
        return self._transport_for(req).complete(req)

    def complete_with_timeout(self, req: ModelRequest, *, timeout_s: float) -> ModelResponse:
        transport = self._transport_for(req)
        complete_with_timeout = getattr(transport, "complete_with_timeout", None)
        if callable(complete_with_timeout):
            return complete_with_timeout(req, timeout_s=timeout_s)
        return transport.complete(req)

    def close(self) -> None:
        for transport in self._transports.values():
            close = getattr(transport, "close", None)
            if callable(close):
                close()

    def _transport_for(self, req: ModelRequest) -> LlmTransport:
        model = req.model_snapshot.model
        flavor = self._flavors.get(model)
        if flavor is None:
            raise IntegrityViolation(
                "model catalog declares no api flavour for this model",
                model=model,
            )
        transport = self._transports.get(flavor)
        if transport is None:
            raise IntegrityViolation(
                "no transport is provisioned for the declared api flavour",
                model=model,
                api_flavor=flavor,
            )
        return transport


__all__ = ["ApiFlavor", "ApiFlavorRoutedTransport", "api_flavors_from_catalog"]
