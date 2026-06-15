from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from .commission import CommissionModel
from .config import ScalperConfig
from .domain import EntrySignal, ExitDecision, MarketSnapshot, Position, Side
from .indicators import compute_indicator_state
from .strategy_overlay import MinuteBar, compute_overlay_indicator_state, evaluate_strategy_overlay

ADAPTIVE_MAX_SPREAD_BPS = Decimal("2.5")
ADAPTIVE_LATE_SESSION_MAX_SPREAD_BPS = Decimal("2.0")
ADAPTIVE_MIN_IMBALANCE = Decimal("0.52")
ADAPTIVE_LATE_SESSION_MIN_IMBALANCE = Decimal("0.55")
ADAPTIVE_MIN_IMPULSE_BPS = Decimal("1.5")
ADAPTIVE_LATE_SESSION_MIN_IMPULSE_BPS = Decimal("2.0")
ADAPTIVE_MIN_EXPECTED_EDGE_BPS = Decimal("6")
ADAPTIVE_LATE_SESSION_MIN_EXPECTED_EDGE_BPS = Decimal("8")
ADAPTIVE_COST_HEADROOM_FLOOR_BPS = Decimal("4")
ADAPTIVE_EDGE_MULTIPLIER = Decimal("2.5")
ADAPTIVE_LATE_SESSION_START_HOUR = 16
ADAPTIVE_SCRATCH_MIN_SECONDS = 4.0
ADAPTIVE_SCRATCH_MAX_SECONDS = 8.0
ADAPTIVE_SCRATCH_MIN_ADVERSE_BPS = Decimal("2")
ADAPTIVE_SCRATCH_LONG_OPPOSING_IMBALANCE = Decimal("0.46")
ADAPTIVE_SCRATCH_SHORT_OPPOSING_IMBALANCE = Decimal("0.54")
ADAPTIVE_FAIL_FAST_MIN_SECONDS = 6.0
ADAPTIVE_FAIL_FAST_MAX_SECONDS = 12.0
ADAPTIVE_FAIL_FAST_STOP_LOSS_SHARE = Decimal("0.20")
ADAPTIVE_PROOF_MIN_MFE_BPS = Decimal("2.5")
ADAPTIVE_PROOF_FLOW_LONG_IMBALANCE = Decimal("0.52")
ADAPTIVE_PROOF_FLOW_SHORT_IMBALANCE = Decimal("0.48")
ADAPTIVE_PROOF_TWAP_GAP_BPS = Decimal("1.0")
ADAPTIVE_EXTENSION_MIN_SECONDS = 6.0
ADAPTIVE_EXTENSION_MAX_SECONDS = 10.0
ADAPTIVE_EXTENSION_MAX_ADVERSE_STOP_LOSS_SHARE = Decimal("0.25")
ADAPTIVE_EXTENSION_REQUIRED_PROOFS = 2


@dataclass(slots=True)
class InstrumentMomentumState:
    history: deque[tuple[object, Decimal]] = field(default_factory=deque)
    current_minute_at: object | None = None
    current_minute_open: Decimal | None = None
    current_minute_high: Decimal | None = None
    current_minute_low: Decimal | None = None
    current_minute_close: Decimal | None = None
    completed_minute_bars: deque[MinuteBar] = field(default_factory=lambda: deque(maxlen=128))


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
        local_hour = snapshot.at.astimezone(self._config.timezone).hour
        late_session = local_hour >= ADAPTIVE_LATE_SESSION_START_HOUR
        adaptive_enabled = self._config.mode == "paper"
        adaptive_spread_bps = self._config.max_spread_bps
        adaptive_min_imbalance = self._config.min_imbalance
        adaptive_min_impulse_bps = self._config.min_impulse_bps
        adaptive_take_profit_bps = self._config.take_profit_bps
        adaptive_stop_loss_bps = self._config.stop_loss_bps
        adaptive_min_expected_edge_bps = self._config.min_expected_edge_bps
        adaptive_time_stop_seconds = self._config.time_stop_seconds
        adaptive_edge_multiplier = Decimal("1.5")
        if adaptive_enabled:
            # Adaptive stays more active than strict, but it now needs clearer
            # follow-through instead of fee-burning micro-noise.
            adaptive_spread_bps = min(
                self._config.max_spread_bps + Decimal("0.50"),
                ADAPTIVE_MAX_SPREAD_BPS,
            )
            adaptive_min_imbalance = max(self._config.min_imbalance, ADAPTIVE_MIN_IMBALANCE)
            adaptive_min_impulse_bps = max(self._config.min_impulse_bps, ADAPTIVE_MIN_IMPULSE_BPS)
            adaptive_take_profit_bps = self._config.take_profit_bps
            adaptive_stop_loss_bps = self._config.stop_loss_bps
            adaptive_min_expected_edge_bps = max(
                self._config.min_expected_edge_bps,
                ADAPTIVE_MIN_EXPECTED_EDGE_BPS,
            )
            adaptive_edge_multiplier = ADAPTIVE_EDGE_MULTIPLIER
            if late_session:
                adaptive_spread_bps = min(
                    adaptive_spread_bps,
                    ADAPTIVE_LATE_SESSION_MAX_SPREAD_BPS,
                )
                adaptive_min_imbalance = max(
                    adaptive_min_imbalance,
                    ADAPTIVE_LATE_SESSION_MIN_IMBALANCE,
                )
                adaptive_min_impulse_bps = max(
                    adaptive_min_impulse_bps,
                    ADAPTIVE_LATE_SESSION_MIN_IMPULSE_BPS,
                )
                adaptive_min_expected_edge_bps = max(
                    adaptive_min_expected_edge_bps,
                    ADAPTIVE_LATE_SESSION_MIN_EXPECTED_EDGE_BPS,
                )
        metrics: dict[str, Decimal | str] = {
            "local_hour": str(local_hour),
            "late_session": str(late_session).lower(),
            "spread_bps": snapshot.spread_bps,
            "imbalance": snapshot.imbalance,
            "impulse_bps": impulse_bps,
            "roundtrip_commission_bps": self._commission_model.roundtrip_bps,
            "adaptive_enabled": str(adaptive_enabled).lower(),
            "adaptive_spread_bps": adaptive_spread_bps,
            "adaptive_min_imbalance": adaptive_min_imbalance,
            "adaptive_min_impulse_bps": adaptive_min_impulse_bps,
            "adaptive_take_profit_bps": adaptive_take_profit_bps,
            "adaptive_stop_loss_bps": adaptive_stop_loss_bps,
            "adaptive_min_expected_edge_bps": adaptive_min_expected_edge_bps,
        }
        strict_spread_pass = snapshot.spread_bps <= self._config.max_spread_bps
        adaptive_spread_pass = snapshot.spread_bps <= adaptive_spread_bps
        metrics["strict_spread_pass"] = str(strict_spread_pass).lower()
        metrics["adaptive_spread_pass"] = str(adaptive_spread_pass).lower()
        if not adaptive_spread_pass:
            return None, "spread_too_wide", metrics

        long_imbalance_pass = snapshot.imbalance >= self._config.min_imbalance
        short_imbalance_pass = (
            self._config.allow_short
            and snapshot.imbalance <= (Decimal("1") - self._config.min_imbalance)
        )
        adaptive_long_imbalance_pass = snapshot.imbalance >= adaptive_min_imbalance
        adaptive_short_imbalance_pass = (
            self._config.allow_short
            and snapshot.imbalance <= (Decimal("1") - adaptive_min_imbalance)
        )
        metrics["long_imbalance_pass"] = str(long_imbalance_pass).lower()
        metrics["short_imbalance_pass"] = str(short_imbalance_pass).lower()
        metrics["adaptive_long_imbalance_pass"] = str(adaptive_long_imbalance_pass).lower()
        metrics["adaptive_short_imbalance_pass"] = str(adaptive_short_imbalance_pass).lower()
        if not adaptive_long_imbalance_pass and not adaptive_short_imbalance_pass:
            return None, "imbalance_too_low", metrics

        long_impulse_pass = impulse_bps >= self._config.min_impulse_bps
        short_impulse_pass = self._config.allow_short and impulse_bps <= -self._config.min_impulse_bps
        adaptive_long_impulse_pass = impulse_bps >= adaptive_min_impulse_bps
        adaptive_short_impulse_pass = self._config.allow_short and impulse_bps <= -adaptive_min_impulse_bps
        metrics["long_impulse_pass"] = str(long_impulse_pass).lower()
        metrics["short_impulse_pass"] = str(short_impulse_pass).lower()
        metrics["adaptive_long_impulse_pass"] = str(adaptive_long_impulse_pass).lower()
        metrics["adaptive_short_impulse_pass"] = str(adaptive_short_impulse_pass).lower()
        if not adaptive_long_impulse_pass and not adaptive_short_impulse_pass:
            return None, "impulse_too_small", metrics

        strict_long_ready = strict_spread_pass and long_imbalance_pass and long_impulse_pass
        strict_short_ready = strict_spread_pass and short_imbalance_pass and short_impulse_pass
        adaptive_long_ready = adaptive_spread_pass and adaptive_long_imbalance_pass and adaptive_long_impulse_pass
        adaptive_short_ready = (
            adaptive_spread_pass and adaptive_short_imbalance_pass and adaptive_short_impulse_pass
        )

        signal_side = Side.BUY
        directional_impulse_bps = impulse_bps
        entry_profile = "strict"
        if strict_short_ready:
            signal_side = Side.SELL
            directional_impulse_bps = -impulse_bps
        elif strict_long_ready:
            signal_side = Side.BUY
        elif adaptive_short_ready:
            signal_side = Side.SELL
            directional_impulse_bps = -impulse_bps
            entry_profile = "adaptive"
        elif adaptive_long_ready:
            signal_side = Side.BUY
            entry_profile = "adaptive"
        else:
            return None, "direction_not_confirmed", metrics

        effective_take_profit_bps = (
            self._config.take_profit_bps if entry_profile == "strict" else adaptive_take_profit_bps
        )
        effective_stop_loss_bps = (
            self._config.stop_loss_bps if entry_profile == "strict" else adaptive_stop_loss_bps
        )
        effective_time_stop_seconds = (
            self._config.time_stop_seconds if entry_profile == "strict" else adaptive_time_stop_seconds
        )
        effective_min_expected_edge_bps = (
            self._config.min_expected_edge_bps
            if entry_profile == "strict"
            else adaptive_min_expected_edge_bps
        )
        metrics["signal_side"] = signal_side.value
        metrics["entry_profile"] = entry_profile
        imbalance_pressure_bps = abs(snapshot.imbalance - Decimal("0.5")) * Decimal("20")
        metrics["imbalance_pressure_bps"] = imbalance_pressure_bps
        expected_edge_bps = min(
            effective_take_profit_bps,
            directional_impulse_bps * (
                Decimal("1.5") if entry_profile == "strict" else adaptive_edge_multiplier
            )
            + (Decimal("0") if entry_profile == "strict" else imbalance_pressure_bps),
        )
        metrics["expected_edge_bps"] = expected_edge_bps
        if expected_edge_bps < effective_min_expected_edge_bps:
            return None, "expected_edge_too_low", metrics

        net_take_profit_bps = effective_take_profit_bps - self._commission_model.roundtrip_bps
        metrics["net_take_profit_bps"] = net_take_profit_bps
        if net_take_profit_bps < self._config.min_net_take_profit_bps:
            return None, "net_take_profit_too_low", metrics
        if entry_profile == "adaptive":
            net_take_profit_after_costs_bps = net_take_profit_bps - snapshot.spread_bps
            adaptive_cost_headroom_floor_bps = max(
                ADAPTIVE_COST_HEADROOM_FLOOR_BPS,
                self._config.target_net_take_profit_buffer_bps,
            )
            metrics["net_take_profit_after_costs_bps"] = net_take_profit_after_costs_bps
            metrics["adaptive_cost_headroom_floor_bps"] = adaptive_cost_headroom_floor_bps
            if net_take_profit_after_costs_bps < adaptive_cost_headroom_floor_bps:
                return None, "adaptive_cost_headroom_too_low", metrics

        regime_allowed, regime_reason, regime_metrics = self._check_regime_filter(
            state,
            signal_side=signal_side,
            entry_profile=entry_profile,
        )
        metrics.update(regime_metrics)
        if not regime_allowed:
            return None, regime_reason, metrics

        overlay_allowed, overlay_reason, overlay_metrics = self._check_strategy_overlay(
            state,
            signal_side=signal_side,
            entry_profile=entry_profile,
        )
        metrics.update(overlay_metrics)
        if not overlay_allowed:
            return None, overlay_reason, metrics

        reason = (
            f"profile={entry_profile} side={signal_side.value} impulse_bps={impulse_bps:.2f} spread_bps={snapshot.spread_bps:.2f} "
            f"imbalance={snapshot.imbalance:.3f} net_tp_bps={net_take_profit_bps:.2f}"
        )
        if entry_profile == "adaptive":
            reason += f" net_tp_after_costs_bps={net_take_profit_after_costs_bps:.2f}"
        return EntrySignal(
            side=signal_side,
            expected_edge_bps=expected_edge_bps,
            take_profit_bps=effective_take_profit_bps,
            stop_loss_bps=effective_stop_loss_bps,
            time_stop_seconds=effective_time_stop_seconds,
            reason=reason,
            profile=entry_profile,
        ), "ok", metrics

    def evaluate_exit(self, position: Position, snapshot: MarketSnapshot) -> ExitDecision | None:
        state = self._state_for(snapshot.instrument.instrument_id)
        self._update_minute_state(state, snapshot)
        current_price: Decimal
        if position.side is Side.BUY:
            if snapshot.bid_price <= 0:
                return None
            current_price = snapshot.bid_price
            target_price = position.entry_price * (
                Decimal("1") + position.take_profit_bps / Decimal("10000")
            )
            stop_price = position.entry_price * (
                Decimal("1") - position.stop_loss_bps / Decimal("10000")
            )
            if current_price >= target_price:
                return ExitDecision(reason="take_profit")
            if current_price <= stop_price:
                return ExitDecision(reason="stop_loss")
        else:
            if snapshot.ask_price <= 0:
                return None
            current_price = snapshot.ask_price
            target_price = position.entry_price * (
                Decimal("1") - position.take_profit_bps / Decimal("10000")
            )
            stop_price = position.entry_price * (
                Decimal("1") + position.stop_loss_bps / Decimal("10000")
            )
            if current_price <= target_price:
                return ExitDecision(reason="take_profit")
            if current_price >= stop_price:
                return ExitDecision(reason="stop_loss")
        elapsed_seconds = (snapshot.at - position.opened_at).total_seconds()
        directional_move_bps = self._directional_move_bps(position, current_price=current_price)
        self._update_position_tracking(position, directional_move_bps=directional_move_bps)
        scratch_exit = self._maybe_adaptive_scratch(
            position,
            snapshot=snapshot,
            current_price=current_price,
            elapsed_seconds=elapsed_seconds,
        )
        if scratch_exit is not None:
            return scratch_exit
        fail_fast_exit = self._maybe_adaptive_fail_fast(
            position,
            snapshot=snapshot,
            state=state,
            current_price=current_price,
            directional_move_bps=directional_move_bps,
            elapsed_seconds=elapsed_seconds,
        )
        if fail_fast_exit is not None:
            return fail_fast_exit
        if elapsed_seconds < position.time_stop_seconds:
            return None

        gross_pnl_rub = self._estimate_gross_pnl_rub(position, current_price=current_price)
        estimated_exit_fee_rub = self._commission_model.fee_rub(
            current_price * Decimal(position.instrument.lot_size) * Decimal(position.quantity_lots)
        )
        estimated_net_pnl_rub = gross_pnl_rub - position.entry_fee_rub - estimated_exit_fee_rub
        adaptive_timeout_exit = self._maybe_adaptive_timeout_extension(
            position,
            snapshot=snapshot,
            state=state,
            current_price=current_price,
            directional_move_bps=directional_move_bps,
            gross_pnl_rub=gross_pnl_rub,
            estimated_net_pnl_rub=estimated_net_pnl_rub,
            elapsed_seconds=elapsed_seconds,
        )
        if adaptive_timeout_exit is not None:
            return adaptive_timeout_exit
        if gross_pnl_rub <= 0:
            return ExitDecision(reason="time_stop")
        if estimated_net_pnl_rub >= 0:
            return ExitDecision(reason="time_stop")
        if elapsed_seconds >= (position.time_stop_seconds * 2):
            return ExitDecision(reason="time_stop")
        return None

    def _maybe_adaptive_scratch(
        self,
        position: Position,
        *,
        snapshot: MarketSnapshot,
        current_price: Decimal,
        elapsed_seconds: float,
    ) -> ExitDecision | None:
        if position.metadata.get("entry_profile") != "adaptive":
            return None

        scratch_delay_seconds = min(
            ADAPTIVE_SCRATCH_MAX_SECONDS,
            max(ADAPTIVE_SCRATCH_MIN_SECONDS, position.time_stop_seconds * 0.30),
        )
        if elapsed_seconds < scratch_delay_seconds:
            return None

        directional_move_bps = ((current_price / position.entry_price) - Decimal("1")) * Decimal("10000")
        opposing_imbalance = snapshot.imbalance <= ADAPTIVE_SCRATCH_LONG_OPPOSING_IMBALANCE
        if position.side is Side.SELL:
            directional_move_bps = ((position.entry_price / current_price) - Decimal("1")) * Decimal("10000")
            opposing_imbalance = snapshot.imbalance >= ADAPTIVE_SCRATCH_SHORT_OPPOSING_IMBALANCE

        adverse_move_floor_bps = max(
            ADAPTIVE_SCRATCH_MIN_ADVERSE_BPS,
            position.stop_loss_bps * Decimal("0.45"),
        )
        if opposing_imbalance and directional_move_bps <= Decimal("0"):
            return ExitDecision(reason="adaptive_scratch")
        if directional_move_bps <= -adverse_move_floor_bps:
            return ExitDecision(reason="adaptive_scratch")
        return None

    def _maybe_adaptive_fail_fast(
        self,
        position: Position,
        *,
        snapshot: MarketSnapshot,
        state: InstrumentMomentumState,
        current_price: Decimal,
        directional_move_bps: Decimal,
        elapsed_seconds: float,
    ) -> ExitDecision | None:
        if not self._is_adaptive_position(position):
            return None
        fail_fast_seconds = min(
            ADAPTIVE_FAIL_FAST_MAX_SECONDS,
            max(ADAPTIVE_FAIL_FAST_MIN_SECONDS, position.time_stop_seconds * 0.60),
        )
        if elapsed_seconds < fail_fast_seconds or elapsed_seconds >= position.time_stop_seconds:
            return None

        proof = self._evaluate_adaptive_proof_of_life(
            position,
            snapshot=snapshot,
            state=state,
            current_price=current_price,
            directional_move_bps=directional_move_bps,
        )
        opposing_imbalance = self._is_opposing_imbalance(position.side, snapshot.imbalance)
        adverse_move_floor_bps = max(
            Decimal("0.5"),
            position.stop_loss_bps * ADAPTIVE_FAIL_FAST_STOP_LOSS_SHARE,
        )
        mfe_bps = self._metadata_decimal(position.metadata.get("max_favorable_bps")) or Decimal("0")
        if (
            proof["proof_count"] <= 0
            and mfe_bps < proof["mfe_floor_bps"]
            and directional_move_bps <= Decimal("0")
        ):
            return ExitDecision(reason="adaptive_fail_fast")
        if proof["proof_count"] <= 1 and opposing_imbalance and directional_move_bps <= -adverse_move_floor_bps:
            return ExitDecision(reason="adaptive_fail_fast")
        return None

    def _maybe_adaptive_timeout_extension(
        self,
        position: Position,
        *,
        snapshot: MarketSnapshot,
        state: InstrumentMomentumState,
        current_price: Decimal,
        directional_move_bps: Decimal,
        gross_pnl_rub: Decimal,
        estimated_net_pnl_rub: Decimal,
        elapsed_seconds: float,
    ) -> ExitDecision | None:
        if not self._is_adaptive_position(position):
            return None
        if estimated_net_pnl_rub >= 0:
            return ExitDecision(reason="time_stop")

        proof = self._evaluate_adaptive_proof_of_life(
            position,
            snapshot=snapshot,
            state=state,
            current_price=current_price,
            directional_move_bps=directional_move_bps,
        )
        allowed_adverse_bps = max(
            Decimal("0.5"),
            position.stop_loss_bps * ADAPTIVE_EXTENSION_MAX_ADVERSE_STOP_LOSS_SHARE,
        )
        extension_seconds = min(
            ADAPTIVE_EXTENSION_MAX_SECONDS,
            max(ADAPTIVE_EXTENSION_MIN_SECONDS, position.time_stop_seconds * 0.50),
        )
        hard_timeout_seconds = position.time_stop_seconds + extension_seconds

        if proof["proof_count"] < ADAPTIVE_EXTENSION_REQUIRED_PROOFS:
            return ExitDecision(reason="time_stop")
        if directional_move_bps <= -allowed_adverse_bps:
            return ExitDecision(reason="time_stop")
        if gross_pnl_rub <= 0 and proof["friction_paid"] is False:
            return ExitDecision(reason="time_stop")
        if elapsed_seconds >= hard_timeout_seconds:
            return ExitDecision(reason="time_stop")
        return None

    def _estimate_gross_pnl_rub(self, position: Position, *, current_price: Decimal) -> Decimal:
        pnl_per_share = current_price - position.entry_price
        if position.side is Side.SELL:
            pnl_per_share = position.entry_price - current_price
        return (
            pnl_per_share
            * Decimal(position.instrument.lot_size)
            * Decimal(position.quantity_lots)
        )

    def _update_minute_state(self, state: InstrumentMomentumState, snapshot: MarketSnapshot) -> None:
        local_minute = snapshot.at.astimezone(self._config.timezone).replace(second=0, microsecond=0)
        mid_price = snapshot.mid_price
        if state.current_minute_at is None:
            state.current_minute_at = local_minute
            state.current_minute_open = mid_price
            state.current_minute_high = mid_price
            state.current_minute_low = mid_price
            state.current_minute_close = mid_price
            return

        if local_minute == state.current_minute_at:
            state.current_minute_high = max(state.current_minute_high or mid_price, mid_price)
            state.current_minute_low = min(state.current_minute_low or mid_price, mid_price)
            state.current_minute_close = mid_price
            return

        if (
            state.current_minute_open is not None
            and state.current_minute_high is not None
            and state.current_minute_low is not None
            and state.current_minute_close is not None
            and state.current_minute_at is not None
        ):
            state.completed_minute_bars.append(
                MinuteBar(
                    at=state.current_minute_at,
                    open=state.current_minute_open,
                    high=state.current_minute_high,
                    low=state.current_minute_low,
                    close=state.current_minute_close,
                )
            )
        state.current_minute_at = local_minute
        state.current_minute_open = mid_price
        state.current_minute_high = mid_price
        state.current_minute_low = mid_price
        state.current_minute_close = mid_price

    def _is_adaptive_position(self, position: Position) -> bool:
        entry_profile = str(position.metadata.get("entry_profile") or "").lower()
        if entry_profile == "adaptive":
            return True
        return "profile=adaptive" in str(position.reason)

    def _directional_move_bps(self, position: Position, *, current_price: Decimal) -> Decimal:
        if position.side is Side.BUY:
            return ((current_price / position.entry_price) - Decimal("1")) * Decimal("10000")
        return ((position.entry_price / current_price) - Decimal("1")) * Decimal("10000")

    def _update_position_tracking(self, position: Position, *, directional_move_bps: Decimal) -> None:
        existing_max = self._metadata_decimal(position.metadata.get("max_favorable_bps"))
        existing_min = self._metadata_decimal(position.metadata.get("min_directional_bps"))
        next_max = directional_move_bps if existing_max is None else max(existing_max, directional_move_bps)
        next_min = directional_move_bps if existing_min is None else min(existing_min, directional_move_bps)
        position.metadata["max_favorable_bps"] = str(next_max)
        position.metadata["min_directional_bps"] = str(next_min)

    def _evaluate_adaptive_proof_of_life(
        self,
        position: Position,
        *,
        snapshot: MarketSnapshot,
        state: InstrumentMomentumState,
        current_price: Decimal,
        directional_move_bps: Decimal,
    ) -> dict[str, object]:
        indicator_state = self._current_indicator_state(state)
        session_twap_gap_bps = self._coerce_decimal(indicator_state.get("session_twap_gap_bps"))
        entry_spread_bps = self._metadata_decimal(position.metadata.get("entry_spread_bps")) or Decimal("0")
        mfe_bps = self._metadata_decimal(position.metadata.get("max_favorable_bps")) or directional_move_bps
        mfe_floor_bps = max(
            ADAPTIVE_PROOF_MIN_MFE_BPS,
            entry_spread_bps + Decimal("0.75"),
        )
        friction_paid = mfe_bps >= mfe_floor_bps
        flow_support = self._is_flow_supportive(position.side, snapshot.imbalance)
        twap_support = False
        if session_twap_gap_bps is not None:
            if position.side is Side.BUY:
                twap_support = session_twap_gap_bps >= ADAPTIVE_PROOF_TWAP_GAP_BPS
            else:
                twap_support = session_twap_gap_bps <= -ADAPTIVE_PROOF_TWAP_GAP_BPS
        proof_count = sum(1 for flag in (friction_paid, flow_support, twap_support) if flag)
        position.metadata["last_proof_of_life_count"] = str(proof_count)
        position.metadata["last_session_twap_gap_bps"] = (
            str(session_twap_gap_bps) if session_twap_gap_bps is not None else ""
        )
        return {
            "proof_count": proof_count,
            "friction_paid": friction_paid,
            "flow_support": flow_support,
            "twap_support": twap_support,
            "mfe_floor_bps": mfe_floor_bps,
        }

    def _current_indicator_state(self, state: InstrumentMomentumState) -> dict[str, Decimal | str | None]:
        bars = list(state.completed_minute_bars)
        if (
            state.current_minute_at is not None
            and state.current_minute_open is not None
            and state.current_minute_high is not None
            and state.current_minute_low is not None
            and state.current_minute_close is not None
        ):
            bars.append(
                MinuteBar(
                    at=state.current_minute_at,
                    open=state.current_minute_open,
                    high=state.current_minute_high,
                    low=state.current_minute_low,
                    close=state.current_minute_close,
                )
            )
        if not bars:
            return {}
        return compute_overlay_indicator_state(bars)

    def _is_flow_supportive(self, side: Side, imbalance: Decimal) -> bool:
        if side is Side.BUY:
            return imbalance >= ADAPTIVE_PROOF_FLOW_LONG_IMBALANCE
        return imbalance <= ADAPTIVE_PROOF_FLOW_SHORT_IMBALANCE

    def _is_opposing_imbalance(self, side: Side, imbalance: Decimal) -> bool:
        if side is Side.BUY:
            return imbalance <= ADAPTIVE_PROOF_FLOW_SHORT_IMBALANCE
        return imbalance >= ADAPTIVE_PROOF_FLOW_LONG_IMBALANCE

    def _coerce_decimal(self, value: object) -> Decimal | None:
        try:
            if value is None or value == "":
                return None
            decimal_value = Decimal(str(value))
        except Exception:  # noqa: BLE001
            return None
        if decimal_value.is_nan():
            return None
        return decimal_value

    def _metadata_decimal(self, value: object) -> Decimal | None:
        return self._coerce_decimal(value)

    def _check_regime_filter(
        self,
        state: InstrumentMomentumState,
        *,
        signal_side: Side,
        entry_profile: str,
    ) -> tuple[bool, str, dict[str, Decimal | str]]:
        metrics: dict[str, Decimal | str] = {
            "regime_filter_mode": self._config.regime_filter_mode,
            "regime_signal_side": signal_side.value,
            "regime_entry_profile": entry_profile,
        }
        mode = self._config.regime_filter_mode
        if mode == "off":
            return True, "ok", metrics

        completed_closes = [bar.close for bar in state.completed_minute_bars]
        indicator_state = compute_indicator_state(completed_closes)
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

        if mode == "trend_side_aware":
            if signal_side is Side.BUY:
                if trend_label != "bullish":
                    return False, "regime_prev_minute_not_bullish", metrics
                metrics["regime_filter_profile"] = "strict_side_aware"
                return True, "ok", metrics
            if trend_label != "bearish":
                return False, "regime_prev_minute_not_bearish_short", metrics
            metrics["regime_filter_profile"] = "strict_side_aware"
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

    def _check_strategy_overlay(
        self,
        state: InstrumentMomentumState,
        *,
        signal_side: Side,
        entry_profile: str,
    ) -> tuple[bool, str, dict[str, Decimal | str]]:
        mode = self._config.strategy_overlay_mode
        metrics: dict[str, Decimal | str] = {
            "strategy_overlay_mode": mode,
            "strategy_overlay_signal_side": signal_side.value,
            "strategy_overlay_entry_profile": entry_profile,
        }
        if mode == "off":
            return True, "ok", metrics
        if mode == "adaptive_twap_trend" and entry_profile != "adaptive":
            metrics["strategy_overlay_profile_policy"] = "strict_bypass"
            return True, "ok", metrics

        if not state.completed_minute_bars:
            return False, "strategy_overlay_prev_minute_warmup", metrics

        indicator_state = compute_overlay_indicator_state(list(state.completed_minute_bars))
        allowed, reason, overlay_metrics = evaluate_strategy_overlay(
            mode,
            indicator_state=indicator_state,
            signal_side=signal_side,
        )
        metrics.update(overlay_metrics)
        if not allowed:
            return False, str(reason or "strategy_overlay_blocked"), metrics
        return True, "ok", metrics
