from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import ScalperConfig
from .domain import ClosedTrade, MarketSnapshot


def trading_day_key(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


@dataclass(slots=True)
class RiskManager:
    config: ScalperConfig
    realized_pnl_rub: Decimal = Decimal("0")
    current_day: str = field(default_factory=lambda: trading_day_key(datetime.now(timezone.utc)))
    cooldown_until: dict[str, datetime] = field(default_factory=dict)

    def _roll_day(self, now: datetime) -> None:
        day = trading_day_key(now)
        if day != self.current_day:
            self.current_day = day
            self.realized_pnl_rub = Decimal("0")
            self.cooldown_until.clear()

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
        if planned_notional_rub > self.config.max_position_notional_rub:
            return False, "max_position_notional"
        cooldown_until = self.cooldown_until.get(snapshot.instrument.instrument_id)
        if cooldown_until and snapshot.at < cooldown_until:
            return False, "cooldown"
        return True, "ok"

    def note_closed_trade(self, trade: ClosedTrade) -> None:
        self._roll_day(trade.closed_at)
        self.realized_pnl_rub += trade.net_pnl_rub
        self.cooldown_until[trade.instrument.instrument_id] = trade.closed_at + timedelta(
            seconds=self.config.cooldown_seconds
        )

    def restore_state(
        self,
        *,
        realized_pnl_rub: Decimal,
        current_day: str,
        cooldown_until: dict[str, datetime],
        now: datetime | None = None,
    ) -> None:
        self.realized_pnl_rub = realized_pnl_rub
        self.current_day = current_day
        self.cooldown_until = dict(cooldown_until)
        self._roll_day(now or datetime.now(timezone.utc))
