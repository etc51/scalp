from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from .domain import Side
from .indicators import compute_indicator_state


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
TEN_THOUSAND = Decimal("10000")
BB_LOOKBACK = 20
STOCH_LOOKBACK = 14
OPENING_RANGE_MINUTES = 5
ALLOWED_STRATEGY_OVERLAY_MODES = frozenset(
    {
        "off",
        "trend_pullback_long",
        "trend_pullback_short",
        "stoch_trend_long",
        "opening_range_breakout_long",
        "opening_range_breakdown_short",
        "mean_reversion_long_short",
        "session_twap_reclaim_long",
        "session_twap_reject_short",
    }
)


@dataclass(slots=True, frozen=True)
class MinuteBar:
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def compute_overlay_indicator_state(bars: Sequence[MinuteBar]) -> dict[str, Any]:
    closes = [bar.close for bar in bars]
    base = compute_indicator_state(closes)
    if not bars:
        return {
            **base,
            "bb_pos": None,
            "stoch_k": None,
            "stoch_d": None,
            "session_return_bps": None,
            "session_twap": None,
            "session_twap_gap_bps": None,
            "atr14_bps": None,
            "opening_range_high": None,
            "opening_range_low": None,
            "opening_range_breakout_bps": None,
            "opening_range_breakdown_bps": None,
            "opening_range_ready": False,
        }

    last_close = bars[-1].close
    bb_pos = _compute_bollinger_position(closes)
    stoch_k, stoch_d = _compute_stochastic(bars)
    current_session = [bar for bar in bars if bar.at.date() == bars[-1].at.date()]
    session_return_bps = _compute_session_return_bps(current_session, last_close)
    session_twap = _compute_session_twap(current_session)
    session_twap_gap_bps = None
    if session_twap not in {None, ZERO}:
        session_twap_gap_bps = ((last_close / session_twap) - ONE) * TEN_THOUSAND
    atr14_bps = _compute_atr_bps(bars)
    opening_range_high, opening_range_low, opening_range_ready = _compute_opening_range(current_session)
    opening_range_breakout_bps = None
    opening_range_breakdown_bps = None
    if opening_range_high not in {None, ZERO}:
        opening_range_breakout_bps = ((last_close / opening_range_high) - ONE) * TEN_THOUSAND
    if opening_range_low not in {None, ZERO}:
        opening_range_breakdown_bps = ((last_close / opening_range_low) - ONE) * TEN_THOUSAND

    return {
        **base,
        "bb_pos": bb_pos,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "session_return_bps": session_return_bps,
        "session_twap": session_twap,
        "session_twap_gap_bps": session_twap_gap_bps,
        "atr14_bps": atr14_bps,
        "opening_range_high": opening_range_high,
        "opening_range_low": opening_range_low,
        "opening_range_breakout_bps": opening_range_breakout_bps,
        "opening_range_breakdown_bps": opening_range_breakdown_bps,
        "opening_range_ready": opening_range_ready,
    }


def evaluate_strategy_overlay(
    overlay_mode: str,
    *,
    indicator_state: dict[str, Any],
    signal_side: Side,
) -> tuple[bool, str | None, dict[str, Decimal | str]]:
    metrics: dict[str, Decimal | str] = {
        "strategy_overlay_mode": overlay_mode,
        "strategy_overlay_signal_side": signal_side.value,
    }
    if overlay_mode == "off":
        return True, "ok", metrics

    trend_label = str(indicator_state.get("trend_label") or "neutral")
    rsi14 = _coerce_decimal(indicator_state.get("rsi14"))
    ema_gap_bps = _coerce_decimal(indicator_state.get("ema_gap_bps"))
    macd_hist = _coerce_decimal(indicator_state.get("macd_hist"))
    bb_pos = _coerce_decimal(indicator_state.get("bb_pos"))
    stoch_k = _coerce_decimal(indicator_state.get("stoch_k"))
    session_return_bps = _coerce_decimal(indicator_state.get("session_return_bps"))
    session_twap_gap_bps = _coerce_decimal(indicator_state.get("session_twap_gap_bps"))
    atr14_bps = _coerce_decimal(indicator_state.get("atr14_bps"))
    opening_range_breakout_bps = _coerce_decimal(indicator_state.get("opening_range_breakout_bps"))
    opening_range_breakdown_bps = _coerce_decimal(indicator_state.get("opening_range_breakdown_bps"))
    opening_range_ready = bool(indicator_state.get("opening_range_ready"))

    metrics.update(
        {
            "strategy_overlay_trend_label": trend_label,
            "strategy_overlay_rsi14": rsi14 if rsi14 is not None else "none",
            "strategy_overlay_ema_gap_bps": ema_gap_bps if ema_gap_bps is not None else "none",
            "strategy_overlay_macd_hist": macd_hist if macd_hist is not None else "none",
            "strategy_overlay_bb_pos": bb_pos if bb_pos is not None else "none",
            "strategy_overlay_stoch_k": stoch_k if stoch_k is not None else "none",
            "strategy_overlay_session_return_bps": (
                session_return_bps if session_return_bps is not None else "none"
            ),
            "strategy_overlay_session_twap_gap_bps": (
                session_twap_gap_bps if session_twap_gap_bps is not None else "none"
            ),
            "strategy_overlay_atr14_bps": atr14_bps if atr14_bps is not None else "none",
            "strategy_overlay_opening_range_ready": str(opening_range_ready).lower(),
        }
    )

    if overlay_mode == "trend_pullback_long":
        if signal_side is not Side.BUY:
            return False, "strategy_overlay_long_only", metrics
        if trend_label != "bullish":
            return False, "strategy_overlay_trend_not_bullish", metrics
        if ema_gap_bps is None or ema_gap_bps <= Decimal("0.5"):
            return False, "strategy_overlay_ema_gap_too_small", metrics
        if macd_hist is None or macd_hist <= ZERO:
            return False, "strategy_overlay_macd_not_positive", metrics
        if not _between(rsi14, Decimal("48"), Decimal("72")):
            return False, "strategy_overlay_rsi_not_pullback_band", metrics
        if not _between(bb_pos, Decimal("0.25"), Decimal("0.80")):
            return False, "strategy_overlay_bb_not_pullback_band", metrics
        return True, "ok", metrics

    if overlay_mode == "trend_pullback_short":
        if signal_side is not Side.SELL:
            return False, "strategy_overlay_short_only", metrics
        if trend_label != "bearish":
            return False, "strategy_overlay_trend_not_bearish", metrics
        if ema_gap_bps is None or ema_gap_bps >= Decimal("-0.5"):
            return False, "strategy_overlay_ema_gap_not_negative_enough", metrics
        if macd_hist is None or macd_hist >= ZERO:
            return False, "strategy_overlay_macd_not_negative", metrics
        if not _between(rsi14, Decimal("35"), Decimal("60")):
            return False, "strategy_overlay_rsi_not_short_pullback_band", metrics
        if not _between(bb_pos, Decimal("0.35"), Decimal("0.90")):
            return False, "strategy_overlay_bb_not_short_pullback_band", metrics
        return True, "ok", metrics

    if overlay_mode == "stoch_trend_long":
        if signal_side is not Side.BUY:
            return False, "strategy_overlay_long_only", metrics
        if ema_gap_bps is None or ema_gap_bps <= ZERO:
            return False, "strategy_overlay_ema_gap_not_positive", metrics
        if macd_hist is None or macd_hist <= ZERO:
            return False, "strategy_overlay_macd_not_positive", metrics
        if not _between(stoch_k, Decimal("55"), Decimal("95")):
            return False, "strategy_overlay_stoch_not_bullish", metrics
        if session_return_bps is not None and session_return_bps < Decimal("-5"):
            return False, "strategy_overlay_session_drift_negative", metrics
        return True, "ok", metrics

    if overlay_mode == "opening_range_breakout_long":
        if signal_side is not Side.BUY:
            return False, "strategy_overlay_long_only", metrics
        if not opening_range_ready:
            return False, "strategy_overlay_opening_range_warmup", metrics
        if opening_range_breakout_bps is None or opening_range_breakout_bps <= ONE:
            return False, "strategy_overlay_no_breakout", metrics
        if ema_gap_bps is None or ema_gap_bps <= ZERO:
            return False, "strategy_overlay_ema_gap_not_positive", metrics
        if macd_hist is None or macd_hist <= ZERO:
            return False, "strategy_overlay_macd_not_positive", metrics
        if session_return_bps is None or session_return_bps <= ZERO:
            return False, "strategy_overlay_session_not_positive", metrics
        return True, "ok", metrics

    if overlay_mode == "opening_range_breakdown_short":
        if signal_side is not Side.SELL:
            return False, "strategy_overlay_short_only", metrics
        if not opening_range_ready:
            return False, "strategy_overlay_opening_range_warmup", metrics
        if opening_range_breakdown_bps is None or opening_range_breakdown_bps >= -ONE:
            return False, "strategy_overlay_no_breakdown", metrics
        if ema_gap_bps is None or ema_gap_bps >= ZERO:
            return False, "strategy_overlay_ema_gap_not_negative", metrics
        if macd_hist is None or macd_hist >= ZERO:
            return False, "strategy_overlay_macd_not_negative", metrics
        if atr14_bps is None or atr14_bps < Decimal("4"):
            return False, "strategy_overlay_atr_too_low", metrics
        if session_return_bps is None or session_return_bps >= ZERO:
            return False, "strategy_overlay_session_not_negative", metrics
        return True, "ok", metrics

    if overlay_mode == "mean_reversion_long_short":
        if signal_side is Side.BUY:
            if not _between(bb_pos, Decimal("-0.50"), Decimal("0.45")):
                return False, "strategy_overlay_long_not_discounted", metrics
            if not _between(rsi14, Decimal("20"), Decimal("55")):
                return False, "strategy_overlay_long_rsi_not_discounted", metrics
            if not _between(stoch_k, ZERO, Decimal("45")):
                return False, "strategy_overlay_long_stoch_not_discounted", metrics
            return True, "ok", metrics
        if not _between(bb_pos, Decimal("0.55"), Decimal("1.50")):
            return False, "strategy_overlay_short_not_premium", metrics
        if not _between(rsi14, Decimal("45"), Decimal("85")):
            return False, "strategy_overlay_short_rsi_not_premium", metrics
        if not _between(stoch_k, Decimal("55"), HUNDRED):
            return False, "strategy_overlay_short_stoch_not_premium", metrics
        if (
            opening_range_ready
            and opening_range_breakdown_bps is not None
            and opening_range_breakdown_bps > Decimal("5")
        ):
            return False, "strategy_overlay_short_breakdown_too_deep", metrics
        return True, "ok", metrics

    if overlay_mode == "session_twap_reclaim_long":
        if signal_side is not Side.BUY:
            return False, "strategy_overlay_long_only", metrics
        if trend_label != "bullish":
            return False, "strategy_overlay_trend_not_bullish", metrics
        if session_twap_gap_bps is None or not _between(
            session_twap_gap_bps,
            Decimal("0.25"),
            Decimal("12"),
        ):
            return False, "strategy_overlay_twap_gap_not_long_band", metrics
        if macd_hist is None or macd_hist <= ZERO:
            return False, "strategy_overlay_macd_not_positive", metrics
        if atr14_bps is None or atr14_bps < Decimal("4"):
            return False, "strategy_overlay_atr_too_low", metrics
        if not _between(rsi14, Decimal("45"), Decimal("70")):
            return False, "strategy_overlay_rsi_not_twap_long_band", metrics
        return True, "ok", metrics

    if overlay_mode == "session_twap_reject_short":
        if signal_side is not Side.SELL:
            return False, "strategy_overlay_short_only", metrics
        if trend_label != "bearish":
            return False, "strategy_overlay_trend_not_bearish", metrics
        if session_twap_gap_bps is None or not _between(
            session_twap_gap_bps,
            Decimal("-12"),
            Decimal("-0.25"),
        ):
            return False, "strategy_overlay_twap_gap_not_short_band", metrics
        if macd_hist is None or macd_hist >= ZERO:
            return False, "strategy_overlay_macd_not_negative", metrics
        if atr14_bps is None or atr14_bps < Decimal("4"):
            return False, "strategy_overlay_atr_too_low", metrics
        if not _between(rsi14, Decimal("30"), Decimal("55")):
            return False, "strategy_overlay_rsi_not_twap_short_band", metrics
        return True, "ok", metrics

    return True, "ok", metrics


def _compute_bollinger_position(closes: Sequence[Decimal]) -> Decimal | None:
    if len(closes) < BB_LOOKBACK:
        return None
    sample = list(closes[-BB_LOOKBACK:])
    basis = sum(sample, start=ZERO) / Decimal(BB_LOOKBACK)
    variance = sum(((value - basis) ** 2 for value in sample), start=ZERO) / Decimal(BB_LOOKBACK)
    std_dev = variance.sqrt() if variance >= ZERO else ZERO
    upper = basis + (std_dev * Decimal("2"))
    lower = basis - (std_dev * Decimal("2"))
    width = upper - lower
    if width == ZERO:
        return None
    return (sample[-1] - lower) / width


def _compute_stochastic(bars: Sequence[MinuteBar]) -> tuple[Decimal | None, Decimal | None]:
    if len(bars) < STOCH_LOOKBACK:
        return None, None
    recent = list(bars[-STOCH_LOOKBACK:])
    highest = max((bar.high for bar in recent), default=None)
    lowest = min((bar.low for bar in recent), default=None)
    if highest is None or lowest is None or highest == lowest:
        return None, None
    current = recent[-1].close
    stoch_k = ((current - lowest) / (highest - lowest)) * HUNDRED
    if len(bars) < STOCH_LOOKBACK + 2:
        return stoch_k, None
    recent_k_values: list[Decimal] = []
    for offset in range(3):
        window = list(bars[-(STOCH_LOOKBACK + offset): len(bars) - offset if offset else None])
        if len(window) < STOCH_LOOKBACK:
            break
        window = window[-STOCH_LOOKBACK:]
        window_high = max((bar.high for bar in window), default=None)
        window_low = min((bar.low for bar in window), default=None)
        if window_high is None or window_low is None or window_high == window_low:
            break
        window_close = window[-1].close
        recent_k_values.append(((window_close - window_low) / (window_high - window_low)) * HUNDRED)
    if len(recent_k_values) < 3:
        return stoch_k, None
    stoch_d = sum(recent_k_values, start=ZERO) / Decimal(len(recent_k_values))
    return stoch_k, stoch_d


def _compute_session_return_bps(current_session: Sequence[MinuteBar], last_close: Decimal) -> Decimal | None:
    if not current_session:
        return None
    session_open = current_session[0].open
    if session_open == ZERO:
        return None
    return ((last_close / session_open) - ONE) * TEN_THOUSAND


def _compute_session_twap(current_session: Sequence[MinuteBar]) -> Decimal | None:
    if not current_session:
        return None
    closes = [bar.close for bar in current_session if bar.close is not None]
    if not closes:
        return None
    return sum(closes, start=ZERO) / Decimal(len(closes))


def _compute_atr_bps(bars: Sequence[MinuteBar], *, period: int = 14) -> Decimal | None:
    if len(bars) < period + 1:
        return None
    sample = list(bars[-(period + 1):])
    true_ranges: list[Decimal] = []
    prev_close = sample[0].close
    for bar in sample[1:]:
        high_low = bar.high - bar.low
        high_prev_close = abs(bar.high - prev_close)
        low_prev_close = abs(bar.low - prev_close)
        true_ranges.append(max(high_low, high_prev_close, low_prev_close))
        prev_close = bar.close
    if not true_ranges or sample[-1].close == ZERO:
        return None
    atr = sum(true_ranges, start=ZERO) / Decimal(len(true_ranges))
    return (atr / sample[-1].close) * TEN_THOUSAND


def _compute_opening_range(current_session: Sequence[MinuteBar]) -> tuple[Decimal | None, Decimal | None, bool]:
    if not current_session:
        return None, None, False
    sample = list(current_session[:OPENING_RANGE_MINUTES])
    opening_range_high = max((bar.high for bar in sample), default=None)
    opening_range_low = min((bar.low for bar in sample), default=None)
    return opening_range_high, opening_range_low, len(current_session) >= OPENING_RANGE_MINUTES


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None or value == "none":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _between(value: Decimal | None, lower: Decimal, upper: Decimal) -> bool:
    if value is None:
        return False
    return lower <= value <= upper
