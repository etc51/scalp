from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(slots=True, frozen=True)
class InstrumentSpec:
    instrument_id: str
    ticker: str
    class_code: str
    figi: str
    lot_size: int
    min_price_increment: Decimal
    currency: str
    name: str


@dataclass(slots=True, frozen=True)
class MarketSnapshot:
    instrument: InstrumentSpec
    bid_price: Decimal
    ask_price: Decimal
    bid_quantity: int
    ask_quantity: int
    at: datetime

    @property
    def mid_price(self) -> Decimal:
        return (self.bid_price + self.ask_price) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price

    @property
    def spread_bps(self) -> Decimal:
        if self.mid_price <= 0:
            return Decimal("0")
        return (self.spread / self.mid_price) * Decimal("10000")

    @property
    def imbalance(self) -> Decimal:
        total = self.bid_quantity + self.ask_quantity
        if total <= 0:
            return Decimal("0.5")
        return Decimal(self.bid_quantity) / Decimal(total)

    @property
    def buy_notional_rub(self) -> Decimal:
        return self.ask_price * Decimal(self.instrument.lot_size)

    @property
    def sell_notional_rub(self) -> Decimal:
        return self.bid_price * Decimal(self.instrument.lot_size)


@dataclass(slots=True, frozen=True)
class EntrySignal:
    side: Side
    expected_edge_bps: Decimal
    take_profit_bps: Decimal
    stop_loss_bps: Decimal
    time_stop_seconds: float
    reason: str
    profile: str = "strict"


@dataclass(slots=True, frozen=True)
class ExitDecision:
    reason: str


@dataclass(slots=True)
class Position:
    instrument: InstrumentSpec
    side: Side
    quantity_lots: int
    entry_price: Decimal
    opened_at: datetime
    take_profit_bps: Decimal
    stop_loss_bps: Decimal
    time_stop_seconds: float
    entry_fee_rub: Decimal
    reason: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ExecutionReport:
    side: Side
    quantity_lots: int
    fill_price: Decimal
    fee_rub: Decimal
    status: str
    submitted_at: datetime
    filled_at: datetime
    post_order_async_ms: float
    to_fill_ms: float
    order_id: str | None = None
    order_request_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ClosedTrade:
    instrument: InstrumentSpec
    side: Side
    quantity_lots: int
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    gross_pnl_rub: Decimal
    fees_rub: Decimal
    net_pnl_rub: Decimal
    entry_reason: str
    exit_reason: str
