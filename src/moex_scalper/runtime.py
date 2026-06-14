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
from .market_history import MarketSnapshotRecorder
from .persistence import PaperRuntimeStore, restore_runtime_entities
from .restrictions import load_active_restrictions, restriction_reason, serialize_restrictions
from .risk import RiskManager
from .strategy import ModerateScalpingStrategy
from .tbank import open_client, resolve_instruments, stream_orderbooks, validate_account
from .tuning import current_strategy_parameters


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
    last_market_data_at: datetime | None = None
    stats: dict[str, dict[str, object]] = field(default_factory=dict)

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
        self.started_at: datetime | None = None
        self.paper_store = PaperRuntimeStore(config.runtime_dir, config.timezone)
        self.snapshot_recorder = MarketSnapshotRecorder(config.runtime_dir, config.timezone)
        self.active_restrictions = load_active_restrictions(config.runtime_dir)

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
        LOGGER.info(
            "Entry schedule timezone=%s weekdays=%s window=%s-%s",
            self.config.timezone_name,
            ",".join(str(day) for day in self.config.entry_weekdays),
            self.config.entry_start_time.isoformat(timespec="minutes"),
            self.config.entry_end_time.isoformat(timespec="minutes"),
        )
        LOGGER.info(
            "Active restrictions tickers=%s hours=%s",
            ",".join(self.active_restrictions.disabled_tickers) or "none",
            ",".join(str(hour) for hour in self.active_restrictions.blocked_entry_hours) or "none",
        )

        self.started_at = datetime.now(timezone.utc)
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
                self._restore_paper_state(
                    executor,
                    instruments={item.instrument_id: item for item in instruments},
                )
                self._refresh_paper_stats()

            self._write_runtime_state(datetime.now(timezone.utc), executor)
            state_writer_task = asyncio.create_task(self._periodic_state_writer(executor))
            started = datetime.now(timezone.utc)
            try:
                async for snapshot in stream_orderbooks(
                    services,
                    instruments,
                    depth=self.config.orderbook_depth,
                    stop_event=self.stop_event,
                    idle_timeout_seconds=self.config.stream_idle_reconnect_seconds,
                    reconnect_delay_seconds=self.config.stream_reconnect_delay_seconds,
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
                state_writer_task.cancel()
                try:
                    await state_writer_task
                except asyncio.CancelledError:
                    pass
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
        self.state.last_market_data_at = datetime.now(timezone.utc)
        try:
            self.snapshot_recorder.append(snapshot)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to persist market snapshot")
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

        entry_allowed, entry_reason = self.risk.entry_allowed_at(snapshot.at)
        if not entry_allowed:
            self.state.blocked_reasons[entry_reason] += 1
            return
        local_hour = snapshot.at.astimezone(self.config.timezone).hour
        restriction = restriction_reason(
            self.active_restrictions,
            ticker=snapshot.instrument.ticker,
            local_hour=local_hour,
        )
        if restriction is not None:
            self.state.blocked_reasons[restriction] += 1
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
        if isinstance(executor, PaperExecutor):
            self._record_paper_trade(trade)
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

    async def _periodic_state_writer(self, executor: Any) -> None:
        interval = max(1.0, self.config.state_heartbeat_seconds)
        while not self.stop_event.is_set():
            now = datetime.now(timezone.utc)
            self._write_runtime_state(now, executor)
            self._last_state_write_at = now
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

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
            self._save_paper_session(executor)
            self._refresh_paper_stats()
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
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "updated_at": now.isoformat(),
            "mode": self.config.mode,
            "watchlist": list(self.config.watchlist),
            "position_sizing_mode": self.config.position_sizing_mode,
            "strategy_parameters": current_strategy_parameters(self.config),
            "active_restrictions": serialize_restrictions(self.active_restrictions),
            "entry_schedule": {
                "timezone": self.config.timezone_name,
                "weekdays": list(self.config.entry_weekdays),
                "start": self.config.entry_start_time.isoformat(timespec="minutes"),
                "end": self.config.entry_end_time.isoformat(timespec="minutes"),
            },
            "snapshots_processed": self.state.snapshots_processed,
            "signals_detected": self.state.signals_detected,
            "realized_pnl_rub": str(self.risk.realized_pnl_rub),
            "market_data": {
                "last_received_at": self.state.last_market_data_at.isoformat() if self.state.last_market_data_at else None,
                "age_seconds": (
                    round((now - self.state.last_market_data_at).total_seconds(), 3)
                    if self.state.last_market_data_at is not None
                    else None
                ),
                "stale_after_seconds": self.config.watchdog_max_market_data_age_seconds,
            },
            "blocked_reasons": dict(self.state.blocked_reasons),
            "stats": self.state.stats,
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

    def _restore_paper_state(
        self,
        executor: PaperExecutor,
        *,
        instruments: dict[str, Any],
    ) -> None:
        try:
            payload = self.paper_store.load_session()
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to load paper session state")
            return
        if not payload:
            return

        restored = restore_runtime_entities(
            payload,
            instruments=instruments,
            timezone_info=self.config.timezone,
        )
        executor.restore_cash(restored["cash_rub"])
        self.state.positions = {
            position.instrument.instrument_id: position
            for position in restored["positions"]
        }
        self.risk.restore_state(
            realized_pnl_rub=restored["risk_realized_pnl_rub"],
            current_day=restored["risk_current_day"],
            cooldown_until=restored["cooldown_until"],
            now=datetime.now(timezone.utc),
        )
        self.state.trades_today = [
            trade
            for trade in restored["trades_today"]
            if trade.closed_at.astimezone(self.config.timezone).date().isoformat() == self.risk.current_day
        ]
        if self.paper_store.seed_history_if_empty(self.state.trades_today):
            LOGGER.info("Seeded paper trade history from restored session trades=%s", len(self.state.trades_today))
        self.state.blocked_reasons = restored["blocked_reasons"]
        self.state.snapshots_processed = restored["snapshots_processed"]
        self.state.signals_detected = restored["signals_detected"]
        LOGGER.info(
            "Restored paper session cash=%s positions=%s trades_today=%s realized_today=%s",
            executor.cash_rub,
            len(self.state.positions),
            len(self.state.trades_today),
            self.risk.realized_pnl_rub,
        )

    def _save_paper_session(self, executor: PaperExecutor) -> None:
        try:
            self.paper_store.save_session(
                cash_rub=executor.cash_rub,
                positions=list(self.state.positions.values()),
                trades_today=self.state.trades_today,
                current_day=self.risk.current_day,
                realized_pnl_rub=self.risk.realized_pnl_rub,
                cooldown_until=self.risk.cooldown_until,
                blocked_reasons=self.state.blocked_reasons,
                snapshots_processed=self.state.snapshots_processed,
                signals_detected=self.state.signals_detected,
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to save paper session state")

    def _record_paper_trade(self, trade: ClosedTrade) -> None:
        try:
            self.paper_store.append_trade(trade)
            self._refresh_paper_stats()
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to persist paper trade")

    def _refresh_paper_stats(self) -> None:
        try:
            self.state.stats = self.paper_store.load_stats(self.risk.current_day)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to load paper stats")
