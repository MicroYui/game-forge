"""Exact-match local PriceBook adapters."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

from gameforge.contracts.cost import PriceQuoteV1, PriceUnavailableV1


class UnavailablePriceBook:
    """Honest local default when no versioned provider price evidence exists."""

    def lookup(
        self,
        provider: str,
        model_snapshot: str,
        observed_at_utc: datetime,
    ) -> PriceUnavailableV1:
        _require_utc(observed_at_utc)
        return PriceUnavailableV1(reason_code="price_book_unavailable")


class StaticPriceBook:
    """Immutable exact provider/model/effective-time quote registry."""

    def __init__(self, quotes: Sequence[PriceQuoteV1]) -> None:
        grouped: dict[tuple[str, str], list[PriceQuoteV1]] = defaultdict(list)
        for quote in quotes:
            grouped[(quote.provider, quote.model_snapshot)].append(quote)
        frozen: dict[tuple[str, str], tuple[PriceQuoteV1, ...]] = {}
        for identity, values in grouped.items():
            ordered = tuple(
                sorted(
                    values,
                    key=lambda item: (
                        item.effective_from,
                        item.effective_to or datetime.max.replace(tzinfo=UTC),
                        item.price_book_version,
                    ),
                )
            )
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if previous.effective_to is None or current.effective_from < previous.effective_to:
                    raise ValueError(
                        "price quote effective intervals overlap for exact provider/model"
                    )
            frozen[identity] = ordered
        self._quotes = frozen

    def lookup(
        self,
        provider: str,
        model_snapshot: str,
        observed_at_utc: datetime,
    ) -> PriceQuoteV1 | PriceUnavailableV1:
        observed_at = _require_utc(observed_at_utc)
        for quote in self._quotes.get((provider, model_snapshot), ()):
            if quote.effective_from <= observed_at and (
                quote.effective_to is None or observed_at < quote.effective_to
            ):
                return quote
        return PriceUnavailableV1(reason_code="no_exact_price_quote")


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("price observation timestamp must be timezone-aware UTC")
    return value.astimezone(UTC)


__all__ = ["StaticPriceBook", "UnavailablePriceBook"]
