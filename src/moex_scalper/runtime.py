from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tbank_latency_check.checker import OrderStateInbox
from t_tech.invest.schemas import OrderStateStreamRequest

from .commission import CommissionModel
from .config import ScalperConfig
from .domain import ClosedTrade, MarketSnapshot, Position, Side
from .execution import LiveExecutor, PaperExecutor
from .risk import RiskManager
from .strategy import ModerateScalpingStrategy
from .tbank import open_client, resolve_instruments, stream_orderbooks, validate_account


LOGGER = logging.getLogger("moex_scalper")


def setup_logging(runtime_dir: Path) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "scalper.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


@dataclass(slots=True)
class BotState:
    positions: dict[str, Position] = field(default_factory=dict)
    trades_today: list[ClosedTrade] = None
    snapshots_processed: int = 0
    signals_detected: int = 0
    blocked_reasons: Counter[str] = field(default_factory=Counter)
    last_snapshot_summary: dict[str, str] = field(default_factory=dict)
    last_snapshots: dict[str, MarketSnapshot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.trades_today is None:
            self.trades_today = []


class ScalperRuntime:
    def __init__(self, config: ScalperConfig) -> None:
        self.config = config
        self.strategy = ModerateScalpingStrategy(config)
        self.risk = RiskManager(config)
        self.state = BotState()
        self.stop_event = asyncio.Event()
        self._last_heartbeat_at: datetime | None = None
        self._last_state_write_at: datetime | None = None

    async def run(self) -> None:
        setup_logging(self.config.runtime_dir)
        commission_model = CommissionModel(self.config.premium_share_commission_bps)
        LOGGER.info(
            "Starting scalper mode=%s watchlist=%s premium_commission_bps=%s min_expected_edge_bps=%s",
            self.config.mode,
            ",".join(self.config.watchlist),
            self.config.premium_share_commission_bps,
            self.config.min_expected_edge_bps,
        )

        async with open_client(self.config) as services:
            if self.config.mode == "live":
                account_info = await validate_account(services, self.config.account_id)
                LOGGER.info("Live account %s %s", account_info["name"], account_info["id"])

            instruments = await resolve_instruments(services, self.config)
            LOGGER.info(
                "Resolved instruments: %s",
                ", ".join(f"{item.ticker}:{item.instrument_id}" for item in instruments),
            )

            inbox = OrderStateInbox()
            order_task = None
            executor: Any
            if self.config.mode == "live":
                order_task = asyncio.create_task(self._consume_order_state_stream(services, inbox))
                executor = LiveExecutor(
                    services=services,
                    account_id=self.config.account_id,
                    inbox=inbox,
                    commission_model=commission_model,
                )
            else:
                executor = PaperExecutor(
                    commission_model=commission_model,
                    initial_cash_rub=self.config.paper_initial_cash_rub,
                )

            started = datetime.now(timezone.utc)
            try:
                async for snapshot in stream_orderbooks(
                    services,
                    instruments,
                    depth=self.config.orderbook_depth,
                    stop_event=self.stop_event,
                ):
                    await self._handle_snapshot(snapshot, executor)
                    self._maybe_write_runtime_state(snapshot.at, executor)
                    self._maybe_log_heartbeat(snapshot.at)
                    if (
                        self.config.run_duration_seconds > 0
                        and (snapshot.at - started).total_seconds() >= self.config.run_duration_seconds
                    ):
                        break
            finally:
                self.stop_event.set()
                self._write_runtime_state(datetime.now(timezone.utc), executor)
                self._log_shutdown_summary()
                if order_task is not None:
                    order_task.cancel()
                    try:
                        await order_task
                    except asyncio.CancelledError:
                        pass

    async def _consume_order_state_stream(self, services: Any, inbox: OrderStateInbox) -> None:
        async for event in services.orders_stream.order_state_stream(
            request=OrderStateStreamRequest(
                accounts=[self.config.account_id],
                ping_delay_millis=1000,
            ),
        ):
            await inbox.add(event)

    async def _handle_snapshot(self, snapshot: MarketSnapshot, executor: Any) -> None:
        self.state.snapshots_processed += 1
        self.state.last_snapshots[snapshot.instrument.instrument_id] = snapshot
        self.state.last_snapshot_summary[snapshot.instrument.ticker] = (
            f"bid={snapshot.bid_price} ask={snapshot.ask_price} "
            f"spread_bps={snapshot.spread_bps:.2f} imbalance={snapshot.imbalance:.3f}"
        )
        position = self.state.positions.get(snapshot.instrument.instrument_id)
        if position is not None:
            exit_decision = self.strategy.evaluate_exit(position, snapshot)
            if exit_decision:
                await self._close_position(snapshot, exit_decision.reason, executor)
            return

        signal, block_reason, metrics = self.strategy.diagnose_entry(
            snapshot,
            has_open_position=False,
        )
        if signal is None:
            self.state.blocked_reasons[block_reason] += 1
            return

        quantity_lots, planned_notional_rub, sizing_reason = self._plan_entry(snapshot, executor)
        if quantity_lots <= 0:
            self.state.blocked_reasons[sizing_reason] += 1
            return

        can_open, reason = self.risk.can_open(
            snapshot,
            open_positions=len(self.state.positions),
            planned_notional_rub=planned_notional_rub,
        )
        if not can_open:
            self.state.blocked_reasons[reason] += 1
            return

        self.state.signals_detected += 1
        report = await executor.execute_entry(snapshot, quantity_lots)
        position = Position(
            instrument=snapshot.instrument,
            side=signal.side,
            quantity_lots=quantity_lots,
            entry_price=report.fill_price,
            opened_at=report.filled_at,
            take_profit_bps=signal.take_profit_bps,
            stop_loss_bps=signal.stop_loss_bps,
            time_stop_seconds=signal.time_stop_seconds,
            entry_fee_rub=report.fee_rub,
            reason=signal.reason,
            metadata={"mode": self.config.mode},
        )
        self.state.positions[position.instrument.instrument_id] = position
        LOGGER.info(
            "OPEN %s %s qty=%s price=%s fee=%s reason=%s",
            position.instrument.ticker,
            position.side.value,
            position.quantity_lots,
            position.entry_price,
            position.entry_fee_rub,
            position.reason,
        )

    async def _close_position(self, snapshot: MarketSnapshot, reason: str, executor: Any) -> None:
        position = self.state.positions.get(snapshot.instrument.instrument_id)
        if position is None:
            return

        report = await executor.execute_exit(snapshot, position.quantity_lots)
        gross_pnl = (
            (report.fill_price - position.entry_price)
            * Decimal(position.instrument.lot_size)
            * Decimal(position.quantity_lots)
        )
        fees = position.entry_fee_rub + report.fee_rub
        net_pnl = gross_pnl - fees
        trade = ClosedTrade(
            instrument=position.instrument,
            side=position.side,
            quantity_lots=position.quantity_lots,
            entry_price=position.entry_price,
            exit_price=report.fill_price,
            opened_at=position.opened_at,
            closed_at=report.filled_at,
            gross_pnl_rub=gross_pnl,
            fees_rub=fees,
            net_pnl_rub=net_pnl,
            entry_reason=position.reason,
            exit_reason=reason,
        )
        self.state.positions.pop(snapshot.instrument.instrument_id, None)
        self.state.trades_today.append(trade)
        self.risk.note_closed_trade(trade)
        LOGGER.info(
            "CLOSE %s reason=%s exit_price=%s gross=%s fees=%s net=%s realized_today=%s",
            trade.instrument.ticker,
            reason,
            trade.exit_price,
            trade.gross_pnl_rub,
            trade.fees_rub,
            trade.net_pnl_rub,
            self.risk.realized_pnl_rub,
        )

    def _maybe_log_heartbeat(self, now: datetime) -> None:
        if self._last_heartbeat_at is not None and (now - self._last_heartbeat_at).total_seconds() < 15:
            return
        self._last_heartbeat_at = now

        top_reasons = ", ".join(
            f"{name}={count}"
            for name, count in self.state.blocked_reasons.most_common(4)
        ) or "none"
        snapshots = " | ".join(
            f"{ticker}: {summary}"
            for ticker, summary in sorted(self.state.last_snapshot_summary.items())
        )
        LOGGER.info(
            "HEARTBEAT snapshots=%s signals=%s open_position=%s realized_today=%s blocked=%s",
            self.state.snapshots_processed,
            self.state.signals_detected,
            ",".join(sorted(position.instrument.ticker for position in self.state.positions.values())) or "none",
            self.risk.realized_pnl_rub,
            top_reasons,
        )
        if snapshots:
            LOGGER.info("MARKET %s", snapshots)

    def _log_shutdown_summary(self) -> None:
        LOGGER.info(
            "STOP snapshots=%s trades=%s signals=%s realized_today=%s open_position=%s",
            self.state.snapshots_processed,
            len(self.state.trades_today),
            self.state.signals_detected,
            self.risk.realized_pnl_rub,
            ",".join(sorted(position.instrument.ticker for position in self.state.positions.values())) or "none",
        )

    def _plan_entry(self, snapshot: MarketSnapshot, executor: Any) -> tuple[int, Decimal, str]:
        if isinstance(executor, PaperExecutor):
            return executor.plan_entry(
                snapshot,
                open_positions=len(self.state.positions),
                max_open_positions=self.config.max_open_positions,
                default_quantity_lots=self.config.order_quantity_lots,
                max_position_notional_rub=self.config.max_position_notional_rub,
                position_sizing_mode=self.config.position_sizing_mode,
            )

        quantity_lots = self.config.order_quantity_lots
        planned_notional_rub = snapshot.buy_notional_rub * Decimal(quantity_lots)
        return quantity_lots, planned_notional_rub, "ok"

    def _maybe_write_runtime_state(self, now: datetime, executor: Any) -> None:
        if self._last_state_write_at is not None and (now - self._last_state_write_at).total_seconds() < 1:
            return
        self._write_runtime_state(now, executor)
        self._last_state_write_at = now

    def _write_runtime_state(self, now: datetime, executor: Any) -> None:
        latest_prices = {
            instrument_id: snapshot.bid_price
            for instrument_id, snapshot in self.state.last_snapshots.items()
        }
        positions = list(self.state.positions.values())
        portfolio: dict[str, object]
        if isinstance(executor, PaperExecutor):
            market_value_rub = executor.market_value_rub(positions, latest_prices)
            unrealized_pnl_rub = executor.unrealized_pnl_rub(positions, latest_prices)
            equity_rub = executor.equity_rub(positions, latest_prices)
            portfolio = {
                "initial_cash_rub": str(executor.initial_cash_rub),
                "cash_rub": str(executor.cash_rub),
                "market_value_rub": str(market_value_rub),
                "unrealized_pnl_rub": str(unrealized_pnl_rub),
                "equity_rub": str(equity_rub),
                "deployment_pct": str(
                    (market_value_rub / executor.initial_cash_rub * Decimal("100"))
                    if executor.initial_cash_rub > 0
                    else Decimal("0")
                ),
            }
        else:
            portfolio = {
                "initial_cash_rub": None,
                "cash_rub": None,
                "market_value_rub": None,
                "unrealized_pnl_rub": None,
                "equity_rub": None,
                "deployment_pct": None,
            }

        payload = {
            "updated_at": now.isoformat(),
            "mode": self.config.mode,
            "watchlist": list(self.config.watchlist),
            "position_sizing_mode": self.config.position_sizing_mode,
            "snapshots_processed": self.state.snapshots_processed,
            "signals_detected": self.state.signals_detected,
            "realized_pnl_rub": str(self.risk.realized_pnl_rub),
            "blocked_reasons": dict(self.state.blocked_reasons),
            "portfolio": portfolio,
            "positions": [
                {
                    "ticker": position.instrument.ticker,
                    "side": position.side.value,
                    "quantity_lots": position.quantity_lots,
                    "entry_price": str(position.entry_price),
                    "current_bid": str(
                        self.state.last_snapshots.get(position.instrument.instrument_id, None).bid_price
                        if position.instrument.instrument_id in self.state.last_snapshots
                        else position.entry_price
                    ),
                    "opened_at": position.opened_at.isoformat(),
                    "entry_fee_rub": str(position.entry_fee_rub),
                    "entry_reason": position.reason,
                }
                for position in sorted(self.state.positions.values(), key=lambda item: item.instrument.ticker)
            ],
            "trades_today": [
                {
                    "ticker": trade.instrument.ticker,
                    "side": trade.side.value,
                    "quantity_lots": trade.quantity_lots,
                    "entry_price": str(trade.entry_price),
                    "exit_price": str(trade.exit_price),
                    "opened_at": trade.opened_at.isoformat(),
                    "closed_at": trade.closed_at.isoformat(),
                    "gross_pnl_rub": str(trade.gross_pnl_rub),
                    "fees_rub": str(trade.fees_rub),
                    "net_pnl_rub": str(trade.net_pnl_rub),
                    "entry_reason": trade.entry_reason,
                    "exit_reason": trade.exit_reason,
                }
                for trade in self.state.trades_today[-100:]
            ],
            "market": [
                {
                    "ticker": snapshot.instrument.ticker,
                    "bid_price": str(snapshot.bid_price),
                    "ask_price": str(snapshot.ask_price),
                    "spread_bps": str(snapshot.spread_bps),
                    "imbalance": str(snapshot.imbalance),
                    "at": snapshot.at.isoformat(),
                }
                for snapshot in sorted(
                    self.state.last_snapshots.values(),
                    key=lambda item: item.instrument.ticker,
                )
            ],
        }

        state_path = self.config.runtime_dir / "dashboard_state.json"
        tmp_path = state_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(state_path)
