from __future__ import annotations

import asyncio
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
    position: Position | None = None
    trades_today: list[ClosedTrade] = None
    snapshots_processed: int = 0
    signals_detected: int = 0
    blocked_reasons: Counter[str] = field(default_factory=Counter)
    last_snapshot_summary: dict[str, str] = field(default_factory=dict)

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
                executor = PaperExecutor(commission_model=commission_model)

            started = datetime.now(timezone.utc)
            try:
                async for snapshot in stream_orderbooks(
                    services,
                    instruments,
                    depth=self.config.orderbook_depth,
                    stop_event=self.stop_event,
                ):
                    await self._handle_snapshot(snapshot, executor)
                    self._maybe_log_heartbeat(snapshot.at)
                    if (
                        self.config.run_duration_seconds > 0
                        and (snapshot.at - started).total_seconds() >= self.config.run_duration_seconds
                    ):
                        break
            finally:
                self.stop_event.set()
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
        self.state.last_snapshot_summary[snapshot.instrument.ticker] = (
            f"bid={snapshot.bid_price} ask={snapshot.ask_price} "
            f"spread_bps={snapshot.spread_bps:.2f} imbalance={snapshot.imbalance:.3f}"
        )
        position = self.state.position
        if position and position.instrument.instrument_id == snapshot.instrument.instrument_id:
            exit_decision = self.strategy.evaluate_exit(position, snapshot)
            if exit_decision:
                await self._close_position(snapshot, exit_decision.reason, executor)
            return

        if position is not None:
            return

        can_open, reason = self.risk.can_open(snapshot, open_positions=1 if position else 0)
        if not can_open:
            self.state.blocked_reasons[reason] += 1
            return

        signal, block_reason, metrics = self.strategy.diagnose_entry(
            snapshot,
            has_open_position=False,
        )
        if signal is None:
            self.state.blocked_reasons[block_reason] += 1
            return

        self.state.signals_detected += 1
        report = await executor.execute_entry(snapshot, self.config.order_quantity_lots)
        position = Position(
            instrument=snapshot.instrument,
            side=signal.side,
            quantity_lots=self.config.order_quantity_lots,
            entry_price=report.fill_price,
            opened_at=report.filled_at,
            take_profit_bps=signal.take_profit_bps,
            stop_loss_bps=signal.stop_loss_bps,
            time_stop_seconds=signal.time_stop_seconds,
            entry_fee_rub=report.fee_rub,
            reason=signal.reason,
            metadata={"mode": self.config.mode},
        )
        self.state.position = position
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
        position = self.state.position
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
        self.state.position = None
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
            self.state.position.instrument.ticker if self.state.position else "none",
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
            self.state.position.instrument.ticker if self.state.position else "none",
        )
