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
    cash_rub: Decimal = field(init=False)

    def __post_init__(self) -> None:
        self.cash_rub = self.initial_cash_rub

    @property
    def available_cash_rub(self) -> Decimal:
        return self.cash_rub

    def plan_entry(
        self,
        snapshot: MarketSnapshot,
        *,
        open_positions: int,
        max_open_positions: int,
        default_quantity_lots: int,
        max_position_notional_rub: Decimal,
        position_sizing_mode: str,
    ) -> tuple[int, Decimal, str]:
        if position_sizing_mode == "fixed_lots":
            quantity_lots = default_quantity_lots
        else:
            remaining_slots = max(1, max_open_positions - open_positions)
            target_budget = self.cash_rub
            if position_sizing_mode == "equal_weight_cash":
                target_budget = self.cash_rub / Decimal(remaining_slots)
            target_budget = min(target_budget, max_position_notional_rub)
            quantity_lots = self._max_affordable_lots(snapshot, target_budget)

        if quantity_lots <= 0:
            return 0, Decimal("0"), "insufficient_cash"

        entry_notional = snapshot.buy_notional_rub * Decimal(quantity_lots)
        total_cost = entry_notional + self.commission_model.fee_rub(entry_notional)
        if total_cost > self.cash_rub:
            quantity_lots = self._max_affordable_lots(snapshot, self.cash_rub)
            if quantity_lots <= 0:
                return 0, Decimal("0"), "insufficient_cash"
            entry_notional = snapshot.buy_notional_rub * Decimal(quantity_lots)

        return quantity_lots, entry_notional, "ok"

    async def execute_entry(self, snapshot: MarketSnapshot, quantity_lots: int) -> ExecutionReport:
        report = self._build_report(snapshot, quantity_lots, Side.BUY, snapshot.ask_price)
        total_cost = report.fill_price * Decimal(snapshot.instrument.lot_size) * Decimal(quantity_lots) + report.fee_rub
        self.cash_rub -= total_cost
        report.metadata["cash_after_rub"] = str(self.cash_rub)
        return report

    async def execute_exit(self, snapshot: MarketSnapshot, quantity_lots: int) -> ExecutionReport:
        report = self._build_report(snapshot, quantity_lots, Side.SELL, snapshot.bid_price)
        proceeds = report.fill_price * Decimal(snapshot.instrument.lot_size) * Decimal(quantity_lots)
        self.cash_rub += proceeds - report.fee_rub
        report.metadata["cash_after_rub"] = str(self.cash_rub)
        return report

    def market_value_rub(self, positions: list[Any], latest_prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for position in positions:
            price = latest_prices.get(position.instrument.instrument_id, position.entry_price)
            total += price * Decimal(position.instrument.lot_size) * Decimal(position.quantity_lots)
        return total

    def unrealized_pnl_rub(self, positions: list[Any], latest_prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for position in positions:
            price = latest_prices.get(position.instrument.instrument_id, position.entry_price)
            total += (
                (price - position.entry_price)
                * Decimal(position.instrument.lot_size)
                * Decimal(position.quantity_lots)
            )
        return total

    def equity_rub(self, positions: list[Any], latest_prices: dict[str, Decimal]) -> Decimal:
        return self.cash_rub + self.market_value_rub(positions, latest_prices)

    def _max_affordable_lots(self, snapshot: MarketSnapshot, budget_rub: Decimal) -> int:
        lot_notional = snapshot.buy_notional_rub
        if lot_notional <= 0 or budget_rub <= 0:
            return 0
        total_per_lot = lot_notional + self.commission_model.fee_rub(lot_notional)
        if total_per_lot <= 0:
            return 0
        return int(budget_rub / total_per_lot)

    def _build_report(
        self,
        snapshot: MarketSnapshot,
        quantity_lots: int,
        side: Side,
        fill_price: Decimal,
    ) -> ExecutionReport:
        notional = fill_price * Decimal(snapshot.instrument.lot_size) * Decimal(quantity_lots)
        fee_rub = self.commission_model.fee_rub(notional)
        moment = utc_now()
        return ExecutionReport(
            side=side,
            quantity_lots=quantity_lots,
            fill_price=fill_price,
            fee_rub=fee_rub,
            status="EXECUTION_REPORT_STATUS_FILL",
            submitted_at=moment,
            filled_at=moment,
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

    async def execute_entry(self, snapshot: MarketSnapshot, quantity_lots: int) -> ExecutionReport:
        return await self._execute_market(snapshot, quantity_lots, Side.BUY)

    async def execute_exit(self, snapshot: MarketSnapshot, quantity_lots: int) -> ExecutionReport:
        return await self._execute_market(snapshot, quantity_lots, Side.SELL)

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
