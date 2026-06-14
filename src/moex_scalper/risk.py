from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import ScalperConfig
from .domain import ClosedTrade, MarketSnapshot, Position


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

    def can_open(self, snapshot: MarketSnapshot, open_positions: int) -> tuple[bool, str]:
        self._roll_day(snapshot.at)

        if open_positions >= self.config.max_open_positions:
            return False, "max_open_positions"
        if self.realized_pnl_rub <= -self.config.daily_loss_limit_rub:
            return False, "daily_loss_limit"
        if snapshot.buy_notional_rub > self.config.max_position_notional_rub:
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
