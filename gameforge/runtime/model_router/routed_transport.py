"""Route each model request to the API flavour that model actually speaks.

Providers do not agree on one surface, and the same gateway can expose several:
`gpt-5.6-sol` is rejected by `/chat/completions` and answers only on `/responses`,
`claude-opus-5` answers on `/chat/completions` and `/v1/messages`. The deployment
declares which surface it reaches each model on — read once from the gateway's own
listing — and this transport dispatches on that declaration instead of letting each
call site pick an endpoint that may not accept the chosen model.
"""

from __future__ import annotations

from collections.abc import Mapping

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelRequest, ModelResponse
from gameforge.runtime.model_router.transport import LlmTransport


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
                "this deployment declares no api flavour for this model",
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


__all__ = ["ApiFlavorRoutedTransport"]
