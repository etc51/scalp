from __future__ import annotations

from decimal import Decimal
from typing import Sequence

try:
    import pandas as _pd  # type: ignore
except ImportError:
    _pd = None

try:
    import pandas_ta as _pandas_ta  # type: ignore
except ImportError:
    try:
        import pandas_ta_classic as _pandas_ta  # type: ignore
    except ImportError:
        _pandas_ta = None


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
TEN_THOUSAND = Decimal("10000")


def classify_trend(
    *,
    rsi14: Decimal | float | None,
    ema_gap_bps: Decimal | float | None,
    macd_hist: Decimal | float | None,
) -> str:
    if rsi14 is None or ema_gap_bps is None or macd_hist is None:
        return "neutral"
    rsi = Decimal(str(rsi14))
    ema_gap = Decimal(str(ema_gap_bps))
    macd = Decimal(str(macd_hist))
    if rsi >= Decimal("55") and ema_gap > ZERO and macd > ZERO:
        return "bullish"
    if rsi <= Decimal("45") and ema_gap < ZERO and macd < ZERO:
        return "bearish"
    return "neutral"


def compute_indicator_state(closes: Sequence[Decimal]) -> dict[str, Decimal | str | None]:
    series = [Decimal(item) for item in closes if item is not None]
    if not series:
        return {
            "rsi14": None,
            "ema9": None,
            "ema21": None,
            "ema_gap_bps": None,
            "macd": None,
            "macd_signal": None,
            "macd_hist": None,
            "trend_label": None,
        }

    pandas_ta_state = _compute_indicator_state_with_pandas_ta(series)
    if pandas_ta_state is not None:
        ema9 = pandas_ta_state["ema9"]
        ema21 = pandas_ta_state["ema21"]
        ema_gap_bps = pandas_ta_state["ema_gap_bps"]
        macd = pandas_ta_state["macd"]
        macd_signal = pandas_ta_state["macd_signal"]
        macd_hist = pandas_ta_state["macd_hist"]
        rsi14 = pandas_ta_state["rsi14"]
    else:
        ema9 = _ema(series, period=9)
        ema21 = _ema(series, period=21)
        ema_gap_bps = None
        if ema9 is not None and ema21 not in {None, ZERO}:
            ema_gap_bps = ((ema9 / ema21) - ONE) * TEN_THOUSAND

        macd_series = _macd_series(series)
        macd = macd_series[-1] if macd_series else None
        macd_signal = _ema(macd_series, period=9) if macd_series else None
        macd_hist = (macd - macd_signal) if macd is not None and macd_signal is not None else None
        rsi14 = _rsi(series, period=14)
    trend_label = classify_trend(
        rsi14=rsi14,
        ema_gap_bps=ema_gap_bps,
        macd_hist=macd_hist,
    )
    return {
        "rsi14": rsi14,
        "ema9": ema9,
        "ema21": ema21,
        "ema_gap_bps": ema_gap_bps,
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "trend_label": trend_label,
    }


def _compute_indicator_state_with_pandas_ta(
    values: Sequence[Decimal],
) -> dict[str, Decimal | None] | None:
    if _pd is None or _pandas_ta is None:
        return None
    close = _pd.Series([float(item) for item in values], dtype="float64")
    ema9_series = _pandas_ta.ema(close, length=9)
    ema21_series = _pandas_ta.ema(close, length=21)
    rsi_series = _pandas_ta.rsi(close, length=14)
    macd_frame = _pandas_ta.macd(close, fast=12, slow=26, signal=9)
    if ema9_series is None or ema21_series is None or rsi_series is None:
        return None

    ema9 = _to_decimal(ema9_series.iloc[-1])
    ema21 = _to_decimal(ema21_series.iloc[-1])
    rsi14 = _to_decimal(rsi_series.iloc[-1])
    if ema9 is None or ema21 is None or rsi14 is None:
        return None

    macd = None
    macd_signal = None
    macd_hist = None
    if macd_frame is not None and not macd_frame.empty:
        macd = _to_decimal(macd_frame.iloc[-1, 0])
        macd_signal = _to_decimal(macd_frame.iloc[-1, 1])
        macd_hist = _to_decimal(macd_frame.iloc[-1, 2])

    ema_gap_bps = None
    if ema21 != ZERO:
        ema_gap_bps = ((ema9 / ema21) - ONE) * TEN_THOUSAND
    return {
        "rsi14": rsi14,
        "ema9": ema9,
        "ema21": ema21,
        "ema_gap_bps": ema_gap_bps,
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
    }


def _ema(values: Sequence[Decimal], *, period: int) -> Decimal | None:
    if not values:
        return None
    alpha = Decimal("2") / Decimal(period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (ONE - alpha) * result
    return result


def _macd_series(values: Sequence[Decimal]) -> list[Decimal]:
    if not values:
        return []
    alpha_fast = Decimal("2") / Decimal(12 + 1)
    alpha_slow = Decimal("2") / Decimal(26 + 1)
    ema_fast = values[0]
    ema_slow = values[0]
    series: list[Decimal] = []
    for value in values:
        ema_fast = alpha_fast * value + (ONE - alpha_fast) * ema_fast
        ema_slow = alpha_slow * value + (ONE - alpha_slow) * ema_slow
        series.append(ema_fast - ema_slow)
    return series


def _rsi(values: Sequence[Decimal], *, period: int) -> Decimal | None:
    if len(values) < period + 1:
        return None
    changes = [current - previous for previous, current in zip(values[:-1], values[1:])]
    gains = [change if change > ZERO else ZERO for change in changes]
    losses = [(-change) if change < ZERO else ZERO for change in changes]
    alpha = ONE / Decimal(period)
    avg_gain = gains[0]
    avg_loss = losses[0]
    for gain, loss in zip(gains[1:], losses[1:]):
        avg_gain = alpha * gain + (ONE - alpha) * avg_gain
        avg_loss = alpha * loss + (ONE - alpha) * avg_loss
    if avg_loss == ZERO:
        return HUNDRED
    rs = avg_gain / avg_loss
    return HUNDRED - (HUNDRED / (ONE + rs))


def _to_decimal(value: object) -> Decimal | None:
    try:
        if value is None:
            return None
        decimal_value = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    if decimal_value.is_nan():
        return None
    return decimal_value
