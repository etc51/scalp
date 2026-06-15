from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import ScalperConfig
from .domain import ClosedTrade, MarketSnapshot


def trading_day_key(moment: datetime, timezone_info: object) -> str:
    return moment.astimezone(timezone_info).date().isoformat()


@dataclass(slots=True)
class RiskManager:
    config: ScalperConfig
    realized_pnl_rub: Decimal = Decimal("0")
    current_day: str = field(init=False)
    cooldown_until: dict[str, datetime] = field(default_factory=dict)
    ticker_realized_pnl_rub: dict[str, Decimal] = field(default_factory=dict)
    ticker_consecutive_losses: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.current_day = trading_day_key(datetime.now(timezone.utc), self.config.timezone)

    def _roll_day(self, now: datetime) -> None:
        day = trading_day_key(now, self.config.timezone)
        if day != self.current_day:
            self.current_day = day
            self.realized_pnl_rub = Decimal("0")
            self.cooldown_until.clear()
            self.ticker_realized_pnl_rub.clear()
            self.ticker_consecutive_losses.clear()

    def entry_allowed_at(self, now: datetime) -> tuple[bool, str]:
        local_now = now.astimezone(self.config.timezone)
        if local_now.weekday() not in self.config.entry_weekdays:
            return False, "entry_weekday_closed"
        current_time = local_now.time().replace(tzinfo=None)
        if current_time < self.config.entry_start_time:
            return False, "entry_before_window"
        if current_time > self.config.entry_end_time:
            return False, "entry_after_window"
        return True, "ok"

    def can_open(
        self,
        snapshot: MarketSnapshot,
        *,
        open_positions: int,
        planned_notional_rub: Decimal,
    ) -> tuple[bool, str]:
        self._roll_day(snapshot.at)

        if open_positions >= self.config.max_open_positions:
            return False, "max_open_positions"
        if self.realized_pnl_rub <= -self.config.daily_loss_limit_rub:
            return False, "daily_loss_limit"
        ticker_guard_reason = self.ticker_guard_reason(snapshot.instrument.ticker)
        if ticker_guard_reason is not None:
            return False, ticker_guard_reason
        if planned_notional_rub > self.config.max_position_notional_rub:
            return False, "max_position_notional"
        cooldown_until = self.cooldown_until.get(snapshot.instrument.instrument_id)
        if cooldown_until and snapshot.at < cooldown_until:
            return False, "cooldown"
        return True, "ok"

    def note_closed_trade(self, trade: ClosedTrade) -> None:
        self._roll_day(trade.closed_at)
        self.realized_pnl_rub += trade.net_pnl_rub
        self._apply_ticker_trade(trade)
        self.cooldown_until[trade.instrument.instrument_id] = trade.closed_at + timedelta(
            seconds=self.config.cooldown_seconds
        )

    def restore_state(
        self,
        *,
        realized_pnl_rub: Decimal,
        current_day: str,
        cooldown_until: dict[str, datetime],
        trades_today: list[ClosedTrade] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.realized_pnl_rub = realized_pnl_rub
        self.current_day = current_day
        self.cooldown_until = dict(cooldown_until)
        self._rebuild_ticker_state(trades_today or [])
        self._roll_day(now or datetime.now(timezone.utc))

    def ticker_guard_reason(self, ticker: str) -> str | None:
        normalized_ticker = ticker.upper()
        realized = self.ticker_realized_pnl_rub.get(normalized_ticker, Decimal("0"))
        loss_limit = self.config.intraday_ticker_loss_limit_rub
        if loss_limit > 0 and realized <= -loss_limit:
            return "ticker_intraday_loss_limit"
        max_losses = self.config.intraday_ticker_max_consecutive_losses
        if max_losses > 0 and self.ticker_consecutive_losses.get(normalized_ticker, 0) >= max_losses:
            return "ticker_consecutive_losses_limit"
        return None

    def active_ticker_guards(self) -> list[dict[str, object]]:
        active_tickers = sorted(
            set(self.ticker_realized_pnl_rub) | set(self.ticker_consecutive_losses)
        )
        result: list[dict[str, object]] = []
        for ticker in active_tickers:
            reasons: list[str] = []
            realized = self.ticker_realized_pnl_rub.get(ticker, Decimal("0"))
            consecutive_losses = self.ticker_consecutive_losses.get(ticker, 0)
            if self.config.intraday_ticker_loss_limit_rub > 0 and realized <= -self.config.intraday_ticker_loss_limit_rub:
                reasons.append("ticker_intraday_loss_limit")
            if (
                self.config.intraday_ticker_max_consecutive_losses > 0
                and consecutive_losses >= self.config.intraday_ticker_max_consecutive_losses
            ):
                reasons.append("ticker_consecutive_losses_limit")
            if not reasons:
                continue
            result.append(
                {
                    "ticker": ticker,
                    "realized_pnl_rub": str(realized),
                    "consecutive_losses": consecutive_losses,
                    "reasons": reasons,
                }
            )
        return result

    def _rebuild_ticker_state(self, trades: list[ClosedTrade]) -> None:
        self.ticker_realized_pnl_rub.clear()
        self.ticker_consecutive_losses.clear()
        for trade in sorted(trades, key=lambda item: item.closed_at):
            self._apply_ticker_trade(trade)

    def _apply_ticker_trade(self, trade: ClosedTrade) -> None:
        ticker = trade.instrument.ticker.upper()
        self.ticker_realized_pnl_rub[ticker] = (
            self.ticker_realized_pnl_rub.get(ticker, Decimal("0")) + trade.net_pnl_rub
        )
        if trade.net_pnl_rub < 0:
            self.ticker_consecutive_losses[ticker] = self.ticker_consecutive_losses.get(ticker, 0) + 1
        elif trade.net_pnl_rub > 0:
            self.ticker_consecutive_losses[ticker] = 0
