from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from moex_scalper.domain import InstrumentSpec
from moex_scalper.tbank import (
    _should_run_poll_fallback,
    orderbook_response_to_snapshot,
    poll_orderbooks_once,
)


def build_instrument(
    *,
    ticker: str = "SBER",
    instrument_id: str = "instrument-sber",
) -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id=instrument_id,
        ticker=ticker,
        class_code="TQBR",
        figi="FIGI",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        name=ticker,
    )


class _Price:
    def __init__(self, units: int, nano: int = 0) -> None:
        self.units = units
        self.nano = nano


class _Level:
    def __init__(self, price: _Price, quantity: int) -> None:
        self.price = price
        self.quantity = quantity


class _OrderBook:
    def __init__(self, *, bids: list[_Level], asks: list[_Level], at: datetime | None = None) -> None:
        self.bids = bids
        self.asks = asks
        self.time = at or datetime(2026, 6, 15, 10, 15, tzinfo=timezone.utc)


class _MarketData:
    def __init__(self, responses: dict[str, _OrderBook]) -> None:
        self._responses = responses

    async def get_order_book(self, *, instrument_id: str, depth: int) -> _OrderBook:
        assert depth == 1
        return self._responses[instrument_id]


class _Services:
    def __init__(self, responses: dict[str, _OrderBook]) -> None:
        self.market_data = _MarketData(responses)


class OrderbookFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_orderbook_response_to_snapshot_returns_none_without_both_sides(self) -> None:
        instrument = build_instrument()
        orderbook = _OrderBook(
            bids=[],
            asks=[_Level(_Price(100, 0), 10)],
        )

        snapshot = orderbook_response_to_snapshot(orderbook, instrument=instrument)

        self.assertIsNone(snapshot)

    def test_orderbook_response_to_snapshot_builds_snapshot(self) -> None:
        instrument = build_instrument()
        orderbook = _OrderBook(
            bids=[_Level(_Price(99, 990000000), 25)],
            asks=[_Level(_Price(100, 10000000), 20)],
        )

        snapshot = orderbook_response_to_snapshot(orderbook, instrument=instrument)

        assert snapshot is not None
        self.assertEqual(snapshot.instrument.ticker, "SBER")
        self.assertEqual(snapshot.bid_price, Decimal("99.99"))
        self.assertEqual(snapshot.ask_price, Decimal("100.01"))
        self.assertEqual(snapshot.bid_quantity, 25)
        self.assertEqual(snapshot.ask_quantity, 20)

    async def test_poll_orderbooks_once_yields_snapshots(self) -> None:
        first = build_instrument(ticker="SBER", instrument_id="instrument-sber")
        second = build_instrument(ticker="GAZP", instrument_id="instrument-gazp")
        services = _Services(
            {
                first.instrument_id: _OrderBook(
                    bids=[_Level(_Price(99, 990000000), 25)],
                    asks=[_Level(_Price(100, 10000000), 20)],
                ),
                second.instrument_id: _OrderBook(
                    bids=[_Level(_Price(200, 0), 15)],
                    asks=[_Level(_Price(200, 20000000), 14)],
                ),
            }
        )

        snapshots = [item async for item in poll_orderbooks_once(services, [first, second], depth=1)]

        self.assertEqual([item.instrument.ticker for item in snapshots], ["SBER", "GAZP"])

    def test_should_run_poll_fallback_respects_interval(self) -> None:
        self.assertTrue(
            _should_run_poll_fallback(
                now_monotonic=10.0,
                last_poll_monotonic=None,
                min_interval_seconds=5.0,
            )
        )
        self.assertFalse(
            _should_run_poll_fallback(
                now_monotonic=12.0,
                last_poll_monotonic=10.0,
                min_interval_seconds=5.0,
            )
        )
        self.assertTrue(
            _should_run_poll_fallback(
                now_monotonic=15.0,
                last_poll_monotonic=10.0,
                min_interval_seconds=5.0,
            )
        )


if __name__ == "__main__":
    unittest.main()
