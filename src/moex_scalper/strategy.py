from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from .commission import CommissionModel
from .config import ScalperConfig
from .domain import EntrySignal, ExitDecision, MarketSnapshot, Position, Side


@dataclass(slots=True)
class InstrumentMomentumState:
    history: deque[tuple[object, Decimal]] = field(default_factory=deque)


class ModerateScalpingStrategy:
    def __init__(self, config: ScalperConfig) -> None:
        self._config = config
        self._commission_model = CommissionModel(config.premium_share_commission_bps)
        self._states: dict[str, InstrumentMomentumState] = {}

    def _state_for(self, instrument_id: str) -> InstrumentMomentumState:
        return self._states.setdefault(instrument_id, InstrumentMomentumState())

    def evaluate_entry(
        self,
        snapshot: MarketSnapshot,
        *,
        has_open_position: bool,
    ) -> EntrySignal | None:
        signal, _, _ = self.diagnose_entry(snapshot, has_open_position=has_open_position)
        return signal

    def diagnose_entry(
        self,
        snapshot: MarketSnapshot,
        *,
        has_open_position: bool,
    ) -> tuple[EntrySignal | None, str, dict[str, Decimal | str]]:
        if has_open_position:
            return None, "already_in_position", {}

        state = self._state_for(snapshot.instrument.instrument_id)
        state.history.append((snapshot.at, snapshot.mid_price))
        cutoff = snapshot.at - timedelta(seconds=self._config.impulse_window_seconds)
        while state.history and state.history[0][0] < cutoff:
            state.history.popleft()

        oldest_mid = state.history[0][1]
        if oldest_mid <= 0:
            return None, "invalid_oldest_mid", {}

        impulse_bps = ((snapshot.mid_price - oldest_mid) / oldest_mid) * Decimal("10000")
        metrics: dict[str, Decimal | str] = {
            "spread_bps": snapshot.spread_bps,
            "imbalance": snapshot.imbalance,
            "impulse_bps": impulse_bps,
            "roundtrip_commission_bps": self._commission_model.roundtrip_bps,
        }
        if snapshot.spread_bps > self._config.max_spread_bps:
            return None, "spread_too_wide", metrics
        if snapshot.imbalance < self._config.min_imbalance:
            return None, "imbalance_too_low", metrics
        if impulse_bps < self._config.min_impulse_bps:
            return None, "impulse_too_small", metrics

        expected_edge_bps = max(self._config.take_profit_bps, impulse_bps * Decimal("1.5"))
        metrics["expected_edge_bps"] = expected_edge_bps
        if expected_edge_bps < self._config.min_expected_edge_bps:
            return None, "expected_edge_too_low", metrics

        net_take_profit_bps = self._config.take_profit_bps - self._commission_model.roundtrip_bps
        metrics["net_take_profit_bps"] = net_take_profit_bps
        if net_take_profit_bps < self._config.min_net_take_profit_bps:
            return None, "net_take_profit_too_low", metrics

        reason = (
            f"impulse_bps={impulse_bps:.2f} spread_bps={snapshot.spread_bps:.2f} "
            f"imbalance={snapshot.imbalance:.3f} net_tp_bps={net_take_profit_bps:.2f}"
        )
        return EntrySignal(
            side=Side.BUY,
            expected_edge_bps=expected_edge_bps,
            take_profit_bps=self._config.take_profit_bps,
            stop_loss_bps=self._config.stop_loss_bps,
            time_stop_seconds=self._config.time_stop_seconds,
            reason=reason,
        ), "ok", metrics

    def evaluate_exit(self, position: Position, snapshot: MarketSnapshot) -> ExitDecision | None:
        if position.side is not Side.BUY:
            return None

        if snapshot.bid_price <= 0:
            return None

        target_price = position.entry_price * (
            Decimal("1") + position.take_profit_bps / Decimal("10000")
        )
        stop_price = position.entry_price * (
            Decimal("1") - position.stop_loss_bps / Decimal("10000")
        )

        if snapshot.bid_price >= target_price:
            return ExitDecision(reason="take_profit")
        if snapshot.bid_price <= stop_price:
            return ExitDecision(reason="stop_loss")
        if (snapshot.at - position.opened_at).total_seconds() >= position.time_stop_seconds:
            return ExitDecision(reason="time_stop")
        return None
