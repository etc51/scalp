from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from t_tech.invest.schemas import OrderDirection, OrderIdType, OrderType, PostOrderAsyncRequest

from tbank_latency_check.checker import (
    OrderStateInbox,
    wait_for_filled_order_state,
    wait_for_order_registration,
)

from .commission import CommissionModel
from .domain import ExecutionReport, MarketSnapshot, Side
from .tbank import quotation_to_decimal


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class PaperExecutor:
    commission_model: CommissionModel
    initial_cash_rub: Decimal = Decimal("300000")
    max_gross_leverage: Decimal = Decimal("1.0")
    cash_rub: Decimal = field(init=False)

    def __post_init__(self) -> None:
        self.cash_rub = self.initial_cash_rub

    @property
    def available_cash_rub(self) -> Decimal:
        return self.cash_rub

    @property
    def borrowed_cash_rub(self) -> Decimal:
        return max(Decimal("0"), -self.cash_rub)

    def restore_cash(self, cash_rub: Decimal) -> None:
        self.cash_rub = cash_rub

    def plan_entry(
        self,
        snapshot: MarketSnapshot,
        *,
        side: Side,
        open_positions: int,
        max_open_positions: int,
        default_quantity_lots: int,
        max_position_notional_rub: Decimal,
        position_sizing_mode: str,
        positions: list[Any],
        latest_prices: dict[str, Decimal],
    ) -> tuple[int, Decimal, str]:
        buying_power_rub = self.remaining_buying_power_rub(positions, latest_prices)
        if position_sizing_mode == "fixed_lots":
            quantity_lots = default_quantity_lots
        else:
            remaining_slots = max(1, max_open_positions - open_positions)
            target_budget = buying_power_rub
            if position_sizing_mode == "equal_weight_cash":
                target_budget = buying_power_rub / Decimal(remaining_slots)
            target_budget = min(target_budget, max_position_notional_rub)
            quantity_lots = self._max_affordable_lots(snapshot, target_budget, side=side)

        if quantity_lots <= 0:
            return 0, Decimal("0"), "insufficient_buying_power"

        entry_notional = self._entry_notional_rub(snapshot, quantity_lots, side=side)
        return quantity_lots, entry_notional, "ok"

    def execute_entry_sync(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        side: Side = Side.BUY,
    ) -> ExecutionReport:
        fill_price = snapshot.ask_price if side is Side.BUY else snapshot.bid_price
        report = self._build_report(
            snapshot,
            quantity_lots,
            side,
            fill_price,
            moment=snapshot.at,
        )
        notional = report.fill_price * Decimal(snapshot.instrument.lot_size) * Decimal(quantity_lots)
        if side is Side.BUY:
            self.cash_rub -= notional + report.fee_rub
        else:
            self.cash_rub += notional - report.fee_rub
        report.metadata["cash_after_rub"] = str(self.cash_rub)
        return report

    async def execute_entry(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        side: Side = Side.BUY,
    ) -> ExecutionReport:
        return self.execute_entry_sync(snapshot, quantity_lots, side)

    def execute_exit_sync(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        position_side: Side = Side.BUY,
    ) -> ExecutionReport:
        exit_side = Side.SELL if position_side is Side.BUY else Side.BUY
        fill_price = snapshot.bid_price if position_side is Side.BUY else snapshot.ask_price
        report = self._build_report(
            snapshot,
            quantity_lots,
            exit_side,
            fill_price,
            moment=snapshot.at,
        )
        notional = report.fill_price * Decimal(snapshot.instrument.lot_size) * Decimal(quantity_lots)
        if position_side is Side.BUY:
            self.cash_rub += notional - report.fee_rub
        else:
            self.cash_rub -= notional + report.fee_rub
        report.metadata["cash_after_rub"] = str(self.cash_rub)
        return report

    async def execute_exit(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        position_side: Side = Side.BUY,
    ) -> ExecutionReport:
        return self.execute_exit_sync(snapshot, quantity_lots, position_side)

    def market_value_rub(self, positions: list[Any], latest_prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for position in positions:
            price = latest_prices.get(position.instrument.instrument_id, position.entry_price)
            signed_notional = price * Decimal(position.instrument.lot_size) * Decimal(position.quantity_lots)
            if position.side is Side.SELL:
                signed_notional *= Decimal("-1")
            total += signed_notional
        return total

    def unrealized_pnl_rub(self, positions: list[Any], latest_prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for position in positions:
            price = latest_prices.get(position.instrument.instrument_id, position.entry_price)
            pnl_per_share = price - position.entry_price
            if position.side is Side.SELL:
                pnl_per_share = position.entry_price - price
            total += pnl_per_share * Decimal(position.instrument.lot_size) * Decimal(position.quantity_lots)
        return total

    def equity_rub(self, positions: list[Any], latest_prices: dict[str, Decimal]) -> Decimal:
        return self.cash_rub + self.market_value_rub(positions, latest_prices)

    def gross_exposure_rub(self, positions: list[Any], latest_prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for position in positions:
            price = latest_prices.get(position.instrument.instrument_id, position.entry_price)
            total += price * Decimal(position.instrument.lot_size) * Decimal(position.quantity_lots)
        return total

    def max_gross_exposure_rub(self, positions: list[Any], latest_prices: dict[str, Decimal]) -> Decimal:
        equity = self.equity_rub(positions, latest_prices)
        if equity <= 0:
            return Decimal("0")
        return equity * self.max_gross_leverage

    def remaining_buying_power_rub(self, positions: list[Any], latest_prices: dict[str, Decimal]) -> Decimal:
        max_exposure = self.max_gross_exposure_rub(positions, latest_prices)
        gross_exposure = self.gross_exposure_rub(positions, latest_prices)
        return max(Decimal("0"), max_exposure - gross_exposure)

    def _max_affordable_lots(self, snapshot: MarketSnapshot, budget_rub: Decimal, *, side: Side) -> int:
        lot_notional = snapshot.buy_notional_rub if side is Side.BUY else snapshot.sell_notional_rub
        if lot_notional <= 0 or budget_rub <= 0:
            return 0
        total_per_lot = lot_notional + self.commission_model.fee_rub(lot_notional)
        if total_per_lot <= 0:
            return 0
        return int(budget_rub / total_per_lot)

    def _entry_notional_rub(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        *,
        side: Side,
    ) -> Decimal:
        lot_notional = snapshot.buy_notional_rub if side is Side.BUY else snapshot.sell_notional_rub
        return lot_notional * Decimal(quantity_lots)

    def _build_report(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        side: Side,
        fill_price: Decimal,
        *,
        moment: datetime | None = None,
    ) -> ExecutionReport:
        notional = fill_price * Decimal(snapshot.instrument.lot_size) * Decimal(quantity_lots)
        fee_rub = self.commission_model.fee_rub(notional)
        executed_at = moment or utc_now()
        return ExecutionReport(
            side=side,
            quantity_lots=quantity_lots,
            fill_price=fill_price,
            fee_rub=fee_rub,
            status="EXECUTION_REPORT_STATUS_FILL",
            submitted_at=executed_at,
            filled_at=executed_at,
            post_order_async_ms=0.0,
            to_fill_ms=0.0,
            metadata={"mode": "paper"},
        )


@dataclass(slots=True)
class LiveExecutor:
    services: Any
    account_id: str
    inbox: OrderStateInbox
    commission_model: CommissionModel
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def execute_entry(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        side: Side = Side.BUY,
    ) -> ExecutionReport:
        return await self._execute_market(snapshot, quantity_lots, side)

    async def execute_exit(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        position_side: Side = Side.BUY,
    ) -> ExecutionReport:
        exit_side = Side.SELL if position_side is Side.BUY else Side.BUY
        return await self._execute_market(snapshot, quantity_lots, exit_side)

    async def _execute_market(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        side: Side,
    ) -> ExecutionReport:
        async with self.lock:
            order_id = str(uuid.uuid4())
            direction = (
                OrderDirection.ORDER_DIRECTION_BUY
                if side is Side.BUY
                else OrderDirection.ORDER_DIRECTION_SELL
            )
            started_at = utc_now()
            started_ns = __import__("time").perf_counter_ns()
            response = await self.services.orders.post_order_async(
                PostOrderAsyncRequest(
                    instrument_id=snapshot.instrument.instrument_id,
                    quantity=quantity_lots,
                    direction=direction,
                    account_id=self.account_id,
                    order_type=OrderType.ORDER_TYPE_MARKET,
                    order_id=order_id,
                    confirm_margin_trade=False,
                )
            )
            post_ms = (__import__("time").perf_counter_ns() - started_ns) / 1_000_000
            request_identifier = response.order_request_id or order_id
            _, registration = await wait_for_order_registration(
                self.services,
                self.inbox,
                account_id=self.account_id,
                request_identifier=request_identifier,
                timeout_seconds=5.0,
                since_ns=started_ns,
            )
            exchange_order_id = registration.get("order_id")
            if exchange_order_id == request_identifier:
                exchange_order_id = None

            fill_ms, fill_event = await wait_for_filled_order_state(
                self.services,
                self.inbox,
                account_id=self.account_id,
                request_identifier=request_identifier,
                order_identifier=exchange_order_id,
                timeout_seconds=5.0,
                since_ns=started_ns,
            )
            state = await self._get_order_state(exchange_order_id, request_identifier)
            fill_price = quotation_to_decimal(state.executed_order_price)
            notional = fill_price * Decimal(snapshot.instrument.lot_size) * Decimal(quantity_lots)
            fee_rub = self.commission_model.fee_rub(notional)
            return ExecutionReport(
                side=side,
                quantity_lots=quantity_lots,
                fill_price=fill_price,
                fee_rub=fee_rub,
                status=fill_event["execution_report_status"],
                submitted_at=started_at,
                filled_at=utc_now(),
                post_order_async_ms=round(post_ms, 3),
                to_fill_ms=fill_ms,
                order_id=exchange_order_id or fill_event.get("order_id"),
                order_request_id=request_identifier,
                metadata={"mode": "live"},
            )

    async def _get_order_state(self, exchange_order_id: str | None, request_identifier: str) -> Any:
        if exchange_order_id:
            try:
                return await self.services.orders.get_order_state(
                    account_id=self.account_id,
                    order_id=exchange_order_id,
                    order_id_type=OrderIdType.ORDER_ID_TYPE_EXCHANGE,
                )
            except Exception:  # noqa: BLE001
                pass
        return await self.services.orders.get_order_state(
            account_id=self.account_id,
            order_id=request_identifier,
            order_id_type=OrderIdType.ORDER_ID_TYPE_REQUEST,
        )
