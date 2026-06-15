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
    ticker_guard_cooldown_until: dict[str, datetime] = field(default_factory=dict)
    ticker_guard_loss_anchor_rub: dict[str, Decimal] = field(default_factory=dict)
    ticker_realized_pnl_rub: dict[str, Decimal] = field(default_factory=dict)
    ticker_consecutive_losses: dict[str, int] = field(default_factory=dict)
    ticker_consecutive_time_stop_losses: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.current_day = trading_day_key(datetime.now(timezone.utc), self.config.timezone)

    def _roll_day(self, now: datetime) -> None:
        day = trading_day_key(now, self.config.timezone)
        if day != self.current_day:
            self.current_day = day
            self.realized_pnl_rub = Decimal("0")
            self.cooldown_until.clear()
            self.ticker_guard_cooldown_until.clear()
            self.ticker_guard_loss_anchor_rub.clear()
            self.ticker_realized_pnl_rub.clear()
            self.ticker_consecutive_losses.clear()
            self.ticker_consecutive_time_stop_losses.clear()

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
        self._release_expired_ticker_guards(snapshot.at)

        if open_positions >= self.config.max_open_positions:
            return False, "max_open_positions"
        if self.realized_pnl_rub <= -self.config.daily_loss_limit_rub:
            return False, "daily_loss_limit"
        session_guard_reason = self.session_guard_reason(now=snapshot.at)
        if session_guard_reason is not None:
            return False, session_guard_reason
        ticker_guard_reason = self.ticker_guard_reason(snapshot.instrument.ticker, now=snapshot.at)
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
        self._arm_temporary_ticker_guard(trade.instrument.ticker, trade.closed_at)
        self.cooldown_until[trade.instrument.instrument_id] = trade.closed_at + timedelta(
            seconds=self.config.cooldown_seconds
        )

    def restore_state(
        self,
        *,
        realized_pnl_rub: Decimal,
        current_day: str,
        cooldown_until: dict[str, datetime],
        ticker_guard_cooldown_until: dict[str, datetime] | None = None,
        ticker_guard_loss_anchor_rub: dict[str, Decimal] | None = None,
        trades_today: list[ClosedTrade] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.realized_pnl_rub = realized_pnl_rub
        self.current_day = current_day
        self.cooldown_until = dict(cooldown_until)
        self.ticker_guard_cooldown_until = {
            str(ticker).upper(): moment
            for ticker, moment in (ticker_guard_cooldown_until or {}).items()
        }
        self.ticker_guard_loss_anchor_rub = {
            str(ticker).upper(): Decimal(value)
            for ticker, value in (ticker_guard_loss_anchor_rub or {}).items()
        }
        self._rebuild_ticker_state(trades_today or [])
        reference_now = now or datetime.now(timezone.utc)
        self._roll_day(reference_now)
        self._restore_temporary_ticker_guards(trades_today or [], reference_now)

    def ticker_guard_reason(self, ticker: str, *, now: datetime | None = None) -> str | None:
        normalized_ticker = ticker.upper()
        moment = now or datetime.now(timezone.utc)
        self._release_expired_ticker_guards(moment)
        reasons = self._ticker_guard_reasons(normalized_ticker)
        if not reasons:
            return None
        if self._uses_temporary_ticker_guards():
            guard_until = self.ticker_guard_cooldown_until.get(normalized_ticker)
            if guard_until is None:
                return None
            if moment >= guard_until:
                self._rearm_ticker_guard(normalized_ticker)
                return None
        return reasons[0]

    def active_ticker_guards(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        moment = now or datetime.now(timezone.utc)
        self._release_expired_ticker_guards(moment)
        active_tickers = sorted(
            set(self.ticker_realized_pnl_rub)
            | set(self.ticker_consecutive_losses)
            | set(self.ticker_consecutive_time_stop_losses)
        )
        result: list[dict[str, object]] = []
        for ticker in active_tickers:
            reasons = self._ticker_guard_reasons(ticker)
            if not reasons:
                continue
            guard_until = self.ticker_guard_cooldown_until.get(ticker)
            if self._uses_temporary_ticker_guards():
                if guard_until is None or moment >= guard_until:
                    continue
            result.append(
                {
                    "ticker": ticker,
                    "realized_pnl_rub": str(self.ticker_realized_pnl_rub.get(ticker, Decimal("0"))),
                    "guard_realized_pnl_rub": str(self._ticker_guard_realized_pnl_rub(ticker)),
                    "consecutive_losses": self.ticker_consecutive_losses.get(ticker, 0),
                    "consecutive_time_stop_losses": self.ticker_consecutive_time_stop_losses.get(ticker, 0),
                    "reasons": reasons,
                    "guard_cooldown_until": guard_until.isoformat() if guard_until is not None else None,
                }
            )
        return result

    def session_guard_reason(self, *, now: datetime | None = None) -> str | None:
        max_guarded = self.config.intraday_session_max_guarded_tickers
        if max_guarded <= 0:
            return None
        if len(self.active_ticker_guards(now=now)) >= max_guarded:
            return "session_guarded_tickers_limit"
        return None

    def active_session_guards(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        reason = self.session_guard_reason(now=now)
        if reason is None:
            return []
        return [
            {
                "reason": reason,
                "guarded_tickers": len(self.active_ticker_guards(now=now)),
                "max_guarded_tickers": self.config.intraday_session_max_guarded_tickers,
            }
        ]

    def _rebuild_ticker_state(self, trades: list[ClosedTrade]) -> None:
        self.ticker_realized_pnl_rub.clear()
        self.ticker_consecutive_losses.clear()
        self.ticker_consecutive_time_stop_losses.clear()
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
        if trade.exit_reason == "time_stop" and trade.net_pnl_rub < 0:
            self.ticker_consecutive_time_stop_losses[ticker] = (
                self.ticker_consecutive_time_stop_losses.get(ticker, 0) + 1
            )
        else:
            self.ticker_consecutive_time_stop_losses[ticker] = 0

    def _uses_temporary_ticker_guards(self) -> bool:
        return self.config.mode == "paper" and self.config.paper_ticker_guard_cooldown_seconds > 0

    def _ticker_guard_realized_pnl_rub(self, ticker: str) -> Decimal:
        normalized_ticker = ticker.upper()
        realized = self.ticker_realized_pnl_rub.get(normalized_ticker, Decimal("0"))
        anchor = self.ticker_guard_loss_anchor_rub.get(normalized_ticker, Decimal("0"))
        return realized - anchor

    def _ticker_guard_reasons(self, ticker: str) -> list[str]:
        normalized_ticker = ticker.upper()
        reasons: list[str] = []
        realized = self._ticker_guard_realized_pnl_rub(normalized_ticker)
        loss_limit = self.config.intraday_ticker_loss_limit_rub
        if loss_limit > 0 and realized <= -loss_limit:
            reasons.append("ticker_intraday_loss_limit")
        max_losses = self.config.intraday_ticker_max_consecutive_losses
        if max_losses > 0 and self.ticker_consecutive_losses.get(normalized_ticker, 0) >= max_losses:
            reasons.append("ticker_consecutive_losses_limit")
        max_time_stop_losses = self.config.intraday_ticker_max_consecutive_time_stop_losses
        if (
            max_time_stop_losses > 0
            and self.ticker_consecutive_time_stop_losses.get(normalized_ticker, 0) >= max_time_stop_losses
        ):
            reasons.append("ticker_consecutive_time_stop_losses_limit")
        return reasons

    def _arm_temporary_ticker_guard(self, ticker: str, now: datetime) -> None:
        if not self._uses_temporary_ticker_guards():
            return
        normalized_ticker = ticker.upper()
        if not self._ticker_guard_reasons(normalized_ticker):
            self.ticker_guard_cooldown_until.pop(normalized_ticker, None)
            return
        self.ticker_guard_cooldown_until[normalized_ticker] = now + timedelta(
            seconds=self.config.paper_ticker_guard_cooldown_seconds
        )

    def _rearm_ticker_guard(self, ticker: str) -> None:
        normalized_ticker = ticker.upper()
        self.ticker_guard_loss_anchor_rub[normalized_ticker] = self.ticker_realized_pnl_rub.get(
            normalized_ticker,
            Decimal("0"),
        )
        self.ticker_consecutive_losses.pop(normalized_ticker, None)
        self.ticker_consecutive_time_stop_losses.pop(normalized_ticker, None)
        self.ticker_guard_cooldown_until.pop(normalized_ticker, None)

    def _release_expired_ticker_guards(self, now: datetime) -> None:
        if not self._uses_temporary_ticker_guards():
            return
        expired_tickers = [
            ticker
            for ticker, guard_until in self.ticker_guard_cooldown_until.items()
            if guard_until <= now
        ]
        for ticker in expired_tickers:
            self._rearm_ticker_guard(ticker)

    def _restore_temporary_ticker_guards(self, trades: list[ClosedTrade], now: datetime) -> None:
        if not self._uses_temporary_ticker_guards():
            self.ticker_guard_cooldown_until.clear()
            self.ticker_guard_loss_anchor_rub.clear()
            return
        last_closed_at_by_ticker: dict[str, datetime] = {}
        for trade in sorted(trades, key=lambda item: item.closed_at):
            last_closed_at_by_ticker[trade.instrument.ticker.upper()] = trade.closed_at
        for ticker, last_closed_at in last_closed_at_by_ticker.items():
            if not self._ticker_guard_reasons(ticker):
                self.ticker_guard_cooldown_until.pop(ticker, None)
                continue
            self.ticker_guard_cooldown_until.setdefault(
                ticker,
                last_closed_at + timedelta(seconds=self.config.paper_ticker_guard_cooldown_seconds),
            )
        self._release_expired_ticker_guards(now)
