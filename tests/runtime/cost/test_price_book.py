from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from gameforge.contracts.cost import PriceQuoteV1, PriceUnavailableV1
from gameforge.runtime.cost.price_book import StaticPriceBook, UnavailablePriceBook


NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _quote(**changes: object) -> PriceQuoteV1:
    values: dict[str, object] = {
        "price_book_version": "prices@2026-07-14",
        "provider": "openai",
        "model_snapshot": "openai:sha256:" + "1" * 64,
        "effective_from": NOW,
        "effective_to": NOW + timedelta(days=1),
        "currency": "USD",
        "rate_unit": 1_000_000,
        "input_rate": Decimal("1.25"),
        "output_rate": Decimal("5.00"),
        "cache_read_rate": Decimal("0.20"),
    }
    values.update(changes)
    return PriceQuoteV1(**values)


def test_default_price_book_is_honestly_unavailable() -> None:
    result = UnavailablePriceBook().lookup(
        "openai",
        "openai:sha256:" + "1" * 64,
        NOW,
    )
    assert result == PriceUnavailableV1(reason_code="price_book_unavailable")


def test_static_price_book_requires_exact_provider_model_and_effective_interval() -> None:
    quote = _quote()
    book = StaticPriceBook((quote,))

    assert book.lookup(quote.provider, quote.model_snapshot, NOW) == quote
    assert (
        book.lookup(
            quote.provider,
            quote.model_snapshot,
            quote.effective_to - timedelta(microseconds=1),
        )
        == quote
    )
    assert isinstance(
        book.lookup("anthropic", quote.model_snapshot, NOW),
        PriceUnavailableV1,
    )
    assert isinstance(
        book.lookup(quote.provider, "openai:sha256:" + "2" * 64, NOW),
        PriceUnavailableV1,
    )
    assert isinstance(
        book.lookup(quote.provider, quote.model_snapshot, quote.effective_to),
        PriceUnavailableV1,
    )


def test_static_price_book_rejects_overlapping_exact_quote_identity() -> None:
    with pytest.raises(ValueError, match="overlap"):
        StaticPriceBook(
            (
                _quote(),
                _quote(
                    price_book_version="prices@overlap",
                    effective_from=NOW + timedelta(hours=12),
                    effective_to=NOW + timedelta(days=2),
                ),
            )
        )


def test_static_price_book_allows_adjacent_and_open_ended_intervals() -> None:
    first = _quote(effective_to=NOW + timedelta(hours=1))
    second = _quote(
        price_book_version="prices@next",
        effective_from=NOW + timedelta(hours=1),
        effective_to=None,
    )
    book = StaticPriceBook((second, first))
    assert book.lookup(first.provider, first.model_snapshot, first.effective_to) == second


def test_price_lookup_rejects_non_utc_observation_time() -> None:
    with pytest.raises(ValueError, match="UTC"):
        StaticPriceBook((_quote(),)).lookup(
            "openai",
            "openai:sha256:" + "1" * 64,
            datetime(2026, 7, 14),
        )
