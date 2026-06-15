from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from .commission import CommissionModel
from .config import ScalperConfig
from .domain import EntrySignal, ExitDecision, MarketSnapshot, Position, Side
from .indicators import compute_indicator_state


@dataclass(slots=True)
class InstrumentMomentumState:
    history: deque[tuple[object, Decimal]] = field(default_factory=deque)
    current_minute_at: object | None = None
    current_minute_close: Decimal | None = None
    completed_minute_closes: deque[Decimal] = field(default_factory=lambda: deque(maxlen=64))


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
        self._update_minute_state(state, snapshot)
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

        expected_edge_bps = min(self._config.take_profit_bps, impulse_bps * Decimal("1.5"))
        metrics["expected_edge_bps"] = expected_edge_bps
        if expected_edge_bps < self._config.min_expected_edge_bps:
            return None, "expected_edge_too_low", metrics

        net_take_profit_bps = self._config.take_profit_bps - self._commission_model.roundtrip_bps
        metrics["net_take_profit_bps"] = net_take_profit_bps
        if net_take_profit_bps < self._config.min_net_take_profit_bps:
            return None, "net_take_profit_too_low", metrics

        regime_allowed, regime_reason, regime_metrics = self._check_regime_filter(state)
        metrics.update(regime_metrics)
        if not regime_allowed:
            return None, regime_reason, metrics

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

    def _update_minute_state(self, state: InstrumentMomentumState, snapshot: MarketSnapshot) -> None:
        local_minute = snapshot.at.astimezone(self._config.timezone).replace(second=0, microsecond=0)
        mid_price = snapshot.mid_price
        if state.current_minute_at is None:
            state.current_minute_at = local_minute
            state.current_minute_close = mid_price
            return

        if local_minute == state.current_minute_at:
            state.current_minute_close = mid_price
            return

        if state.current_minute_close is not None:
            state.completed_minute_closes.append(state.current_minute_close)
        state.current_minute_at = local_minute
        state.current_minute_close = mid_price

    def _check_regime_filter(
        self,
        state: InstrumentMomentumState,
    ) -> tuple[bool, str, dict[str, Decimal | str]]:
        metrics: dict[str, Decimal | str] = {
            "regime_filter_mode": self._config.regime_filter_mode,
        }
        mode = self._config.regime_filter_mode
        if mode == "off":
            return True, "ok", metrics

        indicator_state = compute_indicator_state(list(state.completed_minute_closes))
        trend_label = indicator_state.get("trend_label")
        rsi14 = indicator_state.get("rsi14")
        macd_hist = indicator_state.get("macd_hist")
        ema_gap_bps = indicator_state.get("ema_gap_bps")
        if trend_label is None and rsi14 is None and macd_hist is None:
            return False, "regime_prev_minute_warmup", metrics

        metrics.update(
            {
                "regime_trend_label": trend_label or "neutral",
                "regime_rsi14": rsi14,
                "regime_macd_hist": macd_hist,
                "regime_ema_gap_bps": ema_gap_bps,
            }
        )

        if mode == "trend_not_bearish":
            if trend_label == "bearish":
                return False, "regime_prev_minute_bearish", metrics
            return True, "ok", metrics

        if mode == "trend_bullish":
            if trend_label != "bullish":
                return False, "regime_prev_minute_not_bullish", metrics
            return True, "ok", metrics

        if mode == "macd_positive":
            if macd_hist is None or macd_hist <= 0:
                return False, "regime_prev_minute_macd_non_positive", metrics
            return True, "ok", metrics

        if mode == "rsi_50_70":
            if rsi14 is None or rsi14 < Decimal("50") or rsi14 > Decimal("70"):
                return False, "regime_prev_minute_rsi_out_of_band", metrics
            return True, "ok", metrics

        return True, "ok", metrics
