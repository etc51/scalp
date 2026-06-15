from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import ScalperConfig
from .domain import Side
from .indicators import classify_trend
from .market_history import load_snapshots_from_paths
from .optimizer import filter_snapshots_for_entry_window, resolve_snapshot_files, simulate_candidate


REGIME_PREVIEW_MIN_TRADES = 3
STRATEGY_LAB_MIN_TRADES = 3
REGIME_FILTERS: list[dict[str, str]] = [
    {
        "name": "current_profile",
        "description": "Текущий paper-профиль.",
        "mode": "current",
    },
    {
        "name": "long_only_off",
        "description": "Long-only без regime-filter.",
        "mode": "off",
        "allow_short": False,
    },
    {
        "name": "long_only_non_bearish",
        "description": "Long-only: предыдущая 1m-свеча не bearish.",
        "mode": "trend_not_bearish",
        "allow_short": False,
    },
    {
        "name": "long_only_bullish",
        "description": "Long-only: предыдущая 1m-свеча bullish.",
        "mode": "trend_bullish",
        "allow_short": False,
    },
    {
        "name": "long_only_macd_positive",
        "description": "Long-only: MACD histogram предыдущей 1m-свечи положительный.",
        "mode": "macd_positive",
        "allow_short": False,
    },
    {
        "name": "long_only_rsi_50_70",
        "description": "Long-only: RSI14 предыдущей 1m-свечи в диапазоне 50-70.",
        "mode": "rsi_50_70",
        "allow_short": False,
    },
    {
        "name": "long_short_side_aware",
        "description": "Long+short: trend filter учитывает сторону сигнала.",
        "mode": "trend_side_aware",
        "allow_short": True,
    },
]
STRATEGY_IDEAS: list[dict[str, Any]] = [
    {
        "name": "trend_pullback_long",
        "description": "Trend pullback: EMA gap + MACD + умеренный RSI + средняя позиция в Bollinger.",
        "family": "trend_pullback",
        "entry_modes": "long_only",
        "allow_short": False,
        "overlay_mode": "trend_pullback_long",
    },
    {
        "name": "trend_pullback_short",
        "description": "Bearish pullback: отрицательный EMA gap + MACD + откат вверх внутри нисходящего тренда.",
        "family": "trend_pullback",
        "entry_modes": "short_only",
        "allow_short": True,
        "overlay_mode": "trend_pullback_short",
    },
    {
        "name": "stoch_trend_long",
        "description": "Trend continuation: стохастик в бычьей зоне с подтверждением EMA и MACD.",
        "family": "stoch_trend",
        "entry_modes": "long_only",
        "allow_short": False,
        "overlay_mode": "stoch_trend_long",
    },
    {
        "name": "opening_range_breakout_long",
        "description": "Opening-range breakout вверх с подтверждением импульса и EMA/MACD.",
        "family": "opening_range_breakout",
        "entry_modes": "long_only",
        "allow_short": False,
        "overlay_mode": "opening_range_breakout_long",
    },
    {
        "name": "opening_range_breakdown_short",
        "description": "Opening-range breakdown вниз с подтверждением ATR, EMA и MACD.",
        "family": "opening_range_breakdown",
        "entry_modes": "short_only",
        "allow_short": True,
        "overlay_mode": "opening_range_breakdown_short",
    },
    {
        "name": "mean_reversion_long_short",
        "description": "Mean reversion от крайних Bollinger/RSI/Stoch состояний, long+short.",
        "family": "mean_reversion",
        "entry_modes": "long+short",
        "allow_short": True,
        "overlay_mode": "mean_reversion_long_short",
    },
    {
        "name": "session_twap_reclaim_long",
        "description": "Bullish TWAP reclaim: цена удерживается выше intraday TWAP при положительном MACD и достаточном ATR.",
        "family": "session_twap",
        "entry_modes": "long_only",
        "allow_short": False,
        "overlay_mode": "session_twap_reclaim_long",
    },
    {
        "name": "session_twap_reject_short",
        "description": "Bearish TWAP reject: цена остаётся ниже intraday TWAP при отрицательном MACD и достаточном ATR.",
        "family": "session_twap",
        "entry_modes": "short_only",
        "allow_short": True,
        "overlay_mode": "session_twap_reject_short",
    },
]


def build_indicator_research(
    config: ScalperConfig,
    *,
    date_key: str | None,
    input_path: str | None,
    top_n: int,
    days: int,
    write_report: bool,
) -> dict[str, Any]:
    try:
        import pandas as pd
    except ImportError:
        payload = {
            "status": "dependency_missing",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "dependency": "pandas",
            "message": "Install pandas to enable indicator research.",
        }
        maybe_write_report(config.runtime_dir, payload, enabled=write_report)
        return payload

    snapshot_files = resolve_snapshot_files(
        config,
        date_key=date_key,
        input_path=input_path,
        days=days,
    )
    raw_snapshots = load_snapshots_from_paths(snapshot_files)
    if not raw_snapshots:
        payload = {
            "status": "no_data",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "snapshot_files": [str(path) for path in snapshot_files],
            "snapshot_count": 0,
            "message": "No recorded market snapshots found for research.",
        }
        maybe_write_report(config.runtime_dir, payload, enabled=write_report)
        return payload

    snapshots, entry_window_summary = filter_snapshots_for_entry_window(config, raw_snapshots)
    if not snapshots:
        payload = {
            "status": "no_entry_window_data",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "snapshot_files": [str(path) for path in snapshot_files],
            "raw_snapshot_count": len(raw_snapshots),
            "snapshot_count": 0,
            "entry_window_summary": entry_window_summary,
            "message": "Recorded snapshots exist, but none fall inside the configured entry window.",
        }
        maybe_write_report(config.runtime_dir, payload, enabled=write_report)
        return payload

    frame = pd.DataFrame(
        [
            {
                "snapshot_key": build_snapshot_key(snapshot),
                "at": snapshot.at.astimezone(config.timezone),
                "ticker": snapshot.instrument.ticker,
                "mid_price": float(snapshot.mid_price),
                "spread_bps": float(snapshot.spread_bps),
                "imbalance": float(snapshot.imbalance),
            }
            for snapshot in snapshots
        ]
    )
    if frame.empty:
        payload = {
            "status": "empty_frame",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "snapshot_files": [str(path) for path in snapshot_files],
            "raw_snapshot_count": len(raw_snapshots),
            "snapshot_count": len(snapshots),
            "entry_window_summary": entry_window_summary,
            "message": "Snapshots were loaded, but no rows were available for research frame.",
        }
        maybe_write_report(config.runtime_dir, payload, enabled=write_report)
        return payload

    frame["at"] = pd.to_datetime(frame["at"])
    frame = frame.sort_values(["ticker", "at"])
    indicator_backend = "pandas_native"
    try:
        import pandas_ta as pandas_ta  # type: ignore

        indicator_backend = "pandas_ta"
    except ImportError:
        pandas_ta = None

    ticker_reports: list[dict[str, Any]] = []
    enriched_parts: list[Any] = []
    for ticker in sorted(frame["ticker"].unique()):
        ticker_frame = frame.loc[frame["ticker"] == ticker].copy()
        minute = build_minute_indicator_frame(
            ticker_frame,
            pandas_ta=pandas_ta,
            pd_module=pd,
        )
        if minute.empty:
            continue

        first_open = float(minute["open"].iloc[0])
        last_close = float(minute["close"].iloc[-1])
        session_return_bps = ((last_close / first_open) - 1) * 10000 if first_open > 0 else 0.0
        high_price = float(minute["high"].max())
        low_price = float(minute["low"].min())
        session_range_bps = ((high_price / low_price) - 1) * 10000 if low_price > 0 else 0.0
        realized_vol_bps = float(minute["return_bps"].std(skipna=True) or 0.0)

        ema9 = _series_last(minute["ema9"])
        ema21 = _series_last(minute["ema21"])
        ema_gap_bps = ((ema9 / ema21) - 1) * 10000 if ema9 is not None and ema21 not in {None, 0} else None
        rsi14 = _series_last(minute["rsi14"])
        macd_value = _series_last(minute["macd"])
        macd_signal_value = _series_last(minute["macd_signal"])
        macd_hist_value = _series_last(minute["macd_hist"])
        session_twap = _series_last(minute["session_twap"])
        session_twap_gap_bps = _series_last(minute["session_twap_gap_bps"])
        atr14_bps = _series_last(minute["atr14_bps"])

        ticker_reports.append(
            {
                "ticker": ticker,
                "minute_bars": int(len(minute)),
                "snapshot_count": int(minute["snapshot_count"].sum()),
                "first_at": minute.index[0].isoformat(),
                "last_at": minute.index[-1].isoformat(),
                "last_close": _float_or_none(last_close),
                "session_return_bps": _float_or_none(session_return_bps),
                "session_range_bps": _float_or_none(session_range_bps),
                "realized_volatility_bps": _float_or_none(realized_vol_bps),
                "average_spread_bps": _float_or_none(float(minute["average_spread_bps"].mean() or 0.0)),
                "average_imbalance": _float_or_none(float(minute["average_imbalance"].mean() or 0.0)),
                "rsi14": _float_or_none(rsi14),
                "ema9": _float_or_none(ema9),
                "ema21": _float_or_none(ema21),
                "ema_gap_bps": _float_or_none(ema_gap_bps),
                "macd": _float_or_none(macd_value),
                "macd_signal": _float_or_none(macd_signal_value),
                "macd_hist": _float_or_none(macd_hist_value),
                "session_twap": _float_or_none(session_twap),
                "session_twap_gap_bps": _float_or_none(session_twap_gap_bps),
                "atr14_bps": _float_or_none(atr14_bps),
                "trend_label": classify_trend(
                    rsi14=rsi14,
                    ema_gap_bps=ema_gap_bps,
                    macd_hist=macd_hist_value,
                ),
            }
        )
        enriched_parts.append(
            enrich_snapshot_frame_with_regime(
                ticker_frame,
                minute,
                pd_module=pd,
            )
        )

    ticker_reports.sort(key=lambda item: item["ticker"])
    regime_replay = build_regime_replay(
        config,
        snapshots,
        top_n=top_n,
    )
    strategy_lab = build_strategy_lab(
        config,
        snapshots,
        top_n=top_n,
    )
    summary = build_research_summary(
        ticker_reports,
        snapshot_count=len(snapshots),
        regime_replay=regime_replay,
        strategy_lab=strategy_lab,
    )
    focus = build_research_focus(
        ticker_reports,
        regime_replay=regime_replay,
        strategy_lab=strategy_lab,
    )
    payload = {
        "status": "ok",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "snapshot_files": [str(path) for path in snapshot_files],
        "raw_snapshot_count": len(raw_snapshots),
        "snapshot_count": len(snapshots),
        "entry_window_summary": entry_window_summary,
        "indicator_backend": indicator_backend,
        "timeframe": "1min",
        "summary": summary,
        "focus": focus,
        "regime_replay": regime_replay,
        "strategy_lab": strategy_lab,
        "tickers": ticker_reports[: max(1, top_n)],
        "all_tickers_count": len(ticker_reports),
    }
    maybe_write_report(config.runtime_dir, payload, enabled=write_report)
    return payload


def build_research_summary(
    tickers: list[dict[str, Any]],
    *,
    snapshot_count: int,
    regime_replay: dict[str, Any] | None,
    strategy_lab: dict[str, Any] | None,
) -> dict[str, Any]:
    bullish = sum(1 for item in tickers if item["trend_label"] == "bullish")
    bearish = sum(1 for item in tickers if item["trend_label"] == "bearish")
    neutral = sum(1 for item in tickers if item["trend_label"] == "neutral")
    recommendation = ((regime_replay or {}).get("recommendation") or {}) if regime_replay else {}
    best_regime = recommendation.get("candidate")
    if best_regime is None and regime_replay:
        best_regime = next(
            (
                item
                for item in list((regime_replay or {}).get("top") or [])
                if not item.get("is_baseline", False)
            ),
            None,
        )
    strategy_recommendation = ((strategy_lab or {}).get("recommendation") or {}) if strategy_lab else {}
    best_strategy = strategy_recommendation.get("candidate")
    if best_strategy is None and strategy_lab:
        best_strategy = next(
            (
                item
                for item in list((strategy_lab or {}).get("top") or [])
                if not item.get("is_baseline", False)
            ),
            None,
        )
    return {
        "ticker_count": len(tickers),
        "snapshot_count": snapshot_count,
        "minute_bars": sum(int(item["minute_bars"]) for item in tickers),
        "bullish_tickers": bullish,
        "bearish_tickers": bearish,
        "neutral_tickers": neutral,
        "strongest_return_ticker": _best_by(tickers, "session_return_bps", reverse=True),
        "weakest_return_ticker": _best_by(tickers, "session_return_bps", reverse=False),
        "highest_rsi_ticker": _best_by(tickers, "rsi14", reverse=True),
        "lowest_rsi_ticker": _best_by(tickers, "rsi14", reverse=False),
        "highest_volatility_ticker": _best_by(tickers, "realized_volatility_bps", reverse=True),
        "best_regime_candidate": best_regime,
        "regime_recommendation": recommendation if regime_replay else None,
        "best_strategy_lab_candidate": best_strategy,
        "strategy_lab_recommendation": strategy_recommendation if strategy_lab else None,
    }


def build_research_focus(
    tickers: list[dict[str, Any]],
    *,
    regime_replay: dict[str, Any] | None,
    strategy_lab: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not tickers:
        return []
    focus: list[dict[str, Any]] = []
    strongest = _best_by(tickers, "session_return_bps", reverse=True)
    weakest = _best_by(tickers, "session_return_bps", reverse=False)
    highest_vol = _best_by(tickers, "realized_volatility_bps", reverse=True)
    highest_rsi = _best_by(tickers, "rsi14", reverse=True)

    strategy_recommendation = ((strategy_lab or {}).get("recommendation") or {}) if strategy_lab else {}
    strategy_candidate = dict(strategy_recommendation.get("candidate") or {})
    if strategy_recommendation.get("eligible") and strategy_candidate:
        focus.append(
            {
                "type": "strategy_lab_candidate",
                "message": (
                    f"Strategy lab лидирует {strategy_candidate['name']} "
                    f"[{strategy_candidate.get('entry_modes', '—')}] "
                    f"({strategy_candidate['delta_vs_baseline_rub']} RUB vs lab baseline)."
                ),
            }
        )
    elif strategy_candidate and strategy_candidate.get("delta_vs_baseline_rub") not in {None, "0"}:
        focus.append(
            {
                "type": "strategy_lab_preview",
                "message": (
                    f"Strategy lab preview: {strategy_candidate['name']} "
                    f"[{strategy_candidate.get('entry_modes', '—')}] "
                    f"{strategy_candidate['delta_vs_baseline_rub']} RUB vs lab baseline."
                ),
            }
        )
    recommendation = ((regime_replay or {}).get("recommendation") or {}) if regime_replay else {}
    candidate = dict(recommendation.get("candidate") or {})
    if recommendation.get("eligible") and candidate:
        focus.append(
            {
                "type": "regime_candidate",
                "message": (
                    f"Лучший regime-filter preview: {candidate['name']} "
                    f"[{candidate.get('entry_modes', '—')}] "
                    f"({candidate['delta_vs_baseline_rub']} RUB vs baseline)."
                ),
            }
        )
    elif candidate and candidate.get("delta_vs_baseline_rub") not in {None, "0"}:
        focus.append(
            {
                "type": "regime_preview",
                "message": (
                    f"Regime preview лидирует {candidate['name']}, но sample пока мал: "
                    f"[{candidate.get('entry_modes', '—')}] "
                    f"{candidate['delta_vs_baseline_rub']} RUB vs baseline."
                ),
            }
        )
    if strongest is not None:
        focus.append(
            {
                "type": "strongest_return",
                "message": f"Лучшая intraday-динамика у {strongest['ticker']}: {strongest['session_return_bps']:.2f} bps.",
            }
        )
    if weakest is not None:
        focus.append(
            {
                "type": "weakest_return",
                "message": f"Самая слабая intraday-динамика у {weakest['ticker']}: {weakest['session_return_bps']:.2f} bps.",
            }
        )
    if highest_vol is not None:
        focus.append(
            {
                "type": "volatility",
                "message": f"Самый волатильный тикер окна: {highest_vol['ticker']}.",
            }
        )
    if highest_rsi is not None and highest_rsi.get("rsi14") is not None:
        focus.append(
            {
                "type": "rsi",
                "message": f"Максимальный RSI14 сейчас у {highest_rsi['ticker']}: {highest_rsi['rsi14']:.1f}.",
            }
        )
    return focus[:5]


def build_minute_indicator_frame(
    ticker_frame: Any,
    *,
    pandas_ta: Any,
    pd_module: Any,
) -> Any:
    minute = (
        ticker_frame.set_index("at")
        .resample("1min")
        .agg(
            open=("mid_price", "first"),
            high=("mid_price", "max"),
            low=("mid_price", "min"),
            close=("mid_price", "last"),
            average_spread_bps=("spread_bps", "mean"),
            average_imbalance=("imbalance", "mean"),
            snapshot_count=("mid_price", "count"),
        )
        .dropna(subset=["close"])
    )
    if minute.empty:
        return minute

    close = minute["close"]
    minute["return_bps"] = close.pct_change() * 10000
    minute["ema9"] = close.ewm(span=9, adjust=False).mean()
    minute["ema21"] = close.ewm(span=21, adjust=False).mean()

    if pandas_ta is not None:
        minute["rsi14"] = pandas_ta.rsi(close, length=14)
        macd = pandas_ta.macd(close, fast=12, slow=26, signal=9)
        if macd is not None and not macd.empty:
            minute["macd"] = macd.iloc[:, 0]
            minute["macd_signal"] = macd.iloc[:, 1]
            minute["macd_hist"] = macd.iloc[:, 2]
        else:
            minute["macd"] = math.nan
            minute["macd_signal"] = math.nan
            minute["macd_hist"] = math.nan
    else:
        delta = close.diff()
        gains = delta.clip(lower=0)
        losses = -delta.clip(upper=0)
        avg_gain = gains.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        avg_loss = losses.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, pd_module.NA)
        minute["rsi14"] = 100 - (100 / (1 + rs))
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        minute["macd"] = ema12 - ema26
        minute["macd_signal"] = minute["macd"].ewm(span=9, adjust=False).mean()
        minute["macd_hist"] = minute["macd"] - minute["macd_signal"]

    minute["ema_gap_bps"] = (
        ((minute["ema9"] / minute["ema21"]) - 1) * 10000
    ).where(minute["ema21"] != 0)
    rolling_basis = close.rolling(window=20, min_periods=20).mean()
    rolling_std = close.rolling(window=20, min_periods=20).std(ddof=0)
    minute["bb_mid"] = rolling_basis
    minute["bb_upper"] = rolling_basis + (rolling_std * 2)
    minute["bb_lower"] = rolling_basis - (rolling_std * 2)
    bb_width = minute["bb_upper"] - minute["bb_lower"]
    minute["bb_pos"] = (
        (close - minute["bb_lower"]) / bb_width
    ).where(bb_width != 0)

    rolling_low = minute["low"].rolling(window=14, min_periods=14).min()
    rolling_high = minute["high"].rolling(window=14, min_periods=14).max()
    stoch_range = rolling_high - rolling_low
    minute["stoch_k"] = (
        ((close - rolling_low) / stoch_range) * 100
    ).where(stoch_range != 0)
    minute["stoch_d"] = minute["stoch_k"].rolling(window=3, min_periods=3).mean()

    minute["session_date"] = minute.index.tz_localize(None).normalize()
    minute["session_bar_index"] = minute.groupby("session_date").cumcount()
    minute["session_open"] = minute.groupby("session_date")["open"].transform("first")
    minute["session_return_bps"] = (
        ((close / minute["session_open"]) - 1) * 10000
    ).where(minute["session_open"] != 0)
    minute["session_twap"] = minute.groupby("session_date")["close"].transform(
        lambda series: series.expanding().mean()
    )
    minute["session_twap_gap_bps"] = (
        ((close / minute["session_twap"]) - 1) * 10000
    ).where(minute["session_twap"] != 0)

    prev_close = close.shift(1)
    true_range = pd_module.concat(
        [
            minute["high"] - minute["low"],
            (minute["high"] - prev_close).abs(),
            (minute["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    minute["atr14"] = true_range.rolling(window=14, min_periods=14).mean()
    minute["atr14_bps"] = (
        (minute["atr14"] / close) * 10000
    ).where(close != 0)

    opening_window_mask = minute["session_bar_index"] < 5
    minute["opening_range_high"] = (
        minute["high"]
        .where(opening_window_mask)
        .groupby(minute["session_date"])
        .transform("max")
    )
    minute["opening_range_low"] = (
        minute["low"]
        .where(opening_window_mask)
        .groupby(minute["session_date"])
        .transform("min")
    )
    minute["opening_range_ready"] = minute["session_bar_index"] >= 5
    minute["opening_range_breakout_bps"] = (
        ((close / minute["opening_range_high"]) - 1) * 10000
    ).where(minute["opening_range_high"] != 0)
    minute["opening_range_breakdown_bps"] = (
        ((close / minute["opening_range_low"]) - 1) * 10000
    ).where(minute["opening_range_low"] != 0)
    minute["trend_label"] = minute.apply(
        lambda row: classify_trend(
            rsi14=_coerce_float(row.get("rsi14")),
            ema_gap_bps=_coerce_float(row.get("ema_gap_bps")),
            macd_hist=_coerce_float(row.get("macd_hist")),
        ),
        axis=1,
    )
    return minute


def enrich_snapshot_frame_with_regime(
    ticker_frame: Any,
    minute: Any,
    *,
    pd_module: Any,
) -> Any:
    indicator_columns = [
        "at",
        "trend_label",
        "rsi14",
        "ema_gap_bps",
        "macd_hist",
        "bb_pos",
        "stoch_k",
        "stoch_d",
        "session_return_bps",
        "session_twap",
        "session_twap_gap_bps",
        "atr14_bps",
        "opening_range_high",
        "opening_range_low",
        "opening_range_breakout_bps",
        "opening_range_breakdown_bps",
        "opening_range_ready",
    ]
    indicator_frame = minute.reset_index()[indicator_columns].copy()
    indicator_frame["minute_at"] = indicator_frame["at"] + pd_module.Timedelta(minutes=1)
    indicator_frame = indicator_frame.drop(columns=["at"])

    enriched = ticker_frame.copy()
    enriched["minute_at"] = enriched["at"].dt.floor("min")
    return enriched.merge(indicator_frame, on="minute_at", how="left")


def build_indicator_lookup(
    enriched_parts: list[Any],
    *,
    pd_module: Any,
) -> dict[str, dict[str, Any]]:
    if not enriched_parts:
        return {}
    merged = pd_module.concat(enriched_parts, ignore_index=True)
    if merged.empty or "snapshot_key" not in merged.columns:
        return {}

    preferred_columns = [
        "snapshot_key",
        "trend_label",
        "rsi14",
        "ema_gap_bps",
        "macd_hist",
        "bb_pos",
        "stoch_k",
        "stoch_d",
        "session_return_bps",
        "opening_range_high",
        "opening_range_low",
        "opening_range_breakout_bps",
        "opening_range_breakdown_bps",
        "opening_range_ready",
    ]
    available_columns = [column for column in preferred_columns if column in merged.columns]
    trimmed = merged[available_columns].drop_duplicates(subset=["snapshot_key"], keep="last")
    lookup: dict[str, dict[str, Any]] = {}
    for row in trimmed.to_dict("records"):
        snapshot_key = str(row.get("snapshot_key") or "").strip()
        if not snapshot_key:
            continue
        payload = dict(row)
        payload.pop("snapshot_key", None)
        lookup[snapshot_key] = payload
    return lookup


def build_strategy_lab(
    config: ScalperConfig,
    snapshots: list[Any],
    *,
    top_n: int,
) -> dict[str, Any]:
    baseline_config = replace(
        config,
        regime_filter_mode="off",
        strategy_overlay_mode="off",
    )
    baseline_raw = simulate_candidate(
        baseline_config,
        snapshots,
    )
    baseline_item = summarize_strategy_candidate(
        {
            "name": "baseline_signal_engine",
            "description": "Текущий signal-engine без дополнительного TA overlay.",
            "family": "baseline",
            "entry_modes": "long+short" if baseline_config.allow_short else "long_only",
            "allow_short": baseline_config.allow_short,
            "overlay_mode": "baseline",
            "is_baseline": True,
        },
        baseline_raw,
        candidate_config=baseline_config,
    )

    results = [baseline_item]
    for candidate in STRATEGY_IDEAS:
        candidate_config = build_strategy_lab_candidate_config(config, candidate)
        raw = simulate_candidate(candidate_config, snapshots)
        results.append(
            summarize_strategy_candidate(
                candidate,
                raw,
                candidate_config=candidate_config,
            )
        )

    baseline_net = Decimal(str(baseline_item.get("net_pnl_rub", "0")))
    for item in results:
        item["delta_vs_baseline_rub"] = str(Decimal(str(item.get("net_pnl_rub", "0"))) - baseline_net)

    ranked = sorted(results, key=strategy_lab_sort_key, reverse=True)
    recommendation = build_strategy_lab_recommendation(
        ranked,
        baseline=baseline_item,
    )
    return {
        "status": "ok",
        "candidate_count": len(results),
        "baseline": baseline_item,
        "top": ranked[: max(1, top_n)],
        "recommendation": recommendation,
    }


def build_strategy_lab_candidate_config(
    config: ScalperConfig,
    candidate: dict[str, Any],
) -> ScalperConfig:
    return replace(
        config,
        regime_filter_mode="off",
        strategy_overlay_mode=str(candidate.get("overlay_mode") or "off"),
        allow_short=bool(candidate.get("allow_short", False)),
    )


def summarize_strategy_candidate(
    candidate: dict[str, Any],
    raw: dict[str, Any],
    *,
    candidate_config: ScalperConfig,
) -> dict[str, Any]:
    is_baseline = bool(candidate.get("is_baseline", False))
    return {
        "name": candidate["name"],
        "description": candidate["description"],
        "family": candidate.get("family"),
        "overlay_mode": candidate.get("overlay_mode"),
        "allow_short": candidate_config.allow_short,
        "entry_modes": (
            str(candidate.get("entry_modes"))
            if candidate.get("entry_modes")
            else ("long+short" if candidate_config.allow_short else "long_only")
        ),
        "regime_filter_mode": candidate_config.regime_filter_mode,
        "strategy_overlay_mode": candidate_config.strategy_overlay_mode,
        "is_baseline": is_baseline,
        "trade_count": int(raw.get("trade_count", 0)),
        "wins": int(raw.get("wins", 0)),
        "losses": int(raw.get("losses", 0)),
        "win_rate_pct": raw.get("win_rate_pct"),
        "signals_detected": int(raw.get("signals_detected", 0)),
        "filtered_signal_count": int(raw.get("filtered_signal_count", 0)),
        "net_pnl_rub": raw.get("net_pnl_rub"),
        "equity_delta_rub": raw.get("equity_delta_rub"),
        "profit_factor": raw.get("profit_factor"),
        "expectancy_bps": raw.get("expectancy_bps"),
        "average_trade_rub": raw.get("average_trade_rub"),
        "max_drawdown_rub": raw.get("max_drawdown_rub"),
        "blocked_top": dict(raw.get("blocked_top") or {}),
        "score": raw.get("score"),
    }


def strategy_lab_sort_key(item: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, int]:
    return (
        Decimal(str(item.get("score", "0"))),
        Decimal(str(item.get("net_pnl_rub", "0"))),
        Decimal(str(item.get("profit_factor", "0"))),
        int(item.get("trade_count", 0)),
    )


def build_strategy_lab_recommendation(
    ranked: list[dict[str, Any]],
    *,
    baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    non_baseline = [item for item in ranked if not item.get("is_baseline", False)]
    if not non_baseline:
        return {"eligible": False, "reason": "no_candidates", "candidate": None}
    top = non_baseline[0]
    delta = Decimal(str(top.get("delta_vs_baseline_rub", "0")))
    if int(top.get("trade_count", 0)) < STRATEGY_LAB_MIN_TRADES:
        return {"eligible": False, "reason": "insufficient_trade_sample", "candidate": top}
    if baseline is None:
        return {"eligible": False, "reason": "missing_baseline", "candidate": top}
    if delta <= 0:
        return {"eligible": False, "reason": "no_positive_delta_vs_baseline", "candidate": top}
    return {"eligible": True, "reason": "best_positive_strategy_overlay", "candidate": top}


def build_strategy_entry_filter(
    overlay_mode: str,
    *,
    indicator_lookup: dict[str, dict[str, Any]],
):
    def entry_filter(snapshot: Any, signal: Any) -> tuple[bool, str | None]:
        indicator_state = indicator_lookup.get(build_snapshot_key(snapshot))
        if indicator_state is None:
            return False, "strategy_lab_missing_indicator_state"
        return evaluate_strategy_overlay(
            overlay_mode,
            indicator_state=indicator_state,
            signal_side=signal.side,
        )

    return entry_filter


def evaluate_strategy_overlay(
    overlay_mode: str,
    *,
    indicator_state: dict[str, Any],
    signal_side: Side,
) -> tuple[bool, str | None]:
    trend_label = str(indicator_state.get("trend_label") or "neutral")
    rsi14 = _coerce_float(indicator_state.get("rsi14"))
    ema_gap_bps = _coerce_float(indicator_state.get("ema_gap_bps"))
    macd_hist = _coerce_float(indicator_state.get("macd_hist"))
    bb_pos = _coerce_float(indicator_state.get("bb_pos"))
    stoch_k = _coerce_float(indicator_state.get("stoch_k"))
    session_return_bps = _coerce_float(indicator_state.get("session_return_bps"))
    opening_range_breakout_bps = _coerce_float(indicator_state.get("opening_range_breakout_bps"))
    opening_range_breakdown_bps = _coerce_float(indicator_state.get("opening_range_breakdown_bps"))
    opening_range_ready = _as_bool(indicator_state.get("opening_range_ready"))

    if overlay_mode == "trend_pullback_long":
        if signal_side is not Side.BUY:
            return False, "strategy_lab_long_only"
        if trend_label != "bullish":
            return False, "strategy_lab_trend_not_bullish"
        if ema_gap_bps is None or ema_gap_bps <= 0.5:
            return False, "strategy_lab_ema_gap_too_small"
        if macd_hist is None or macd_hist <= 0:
            return False, "strategy_lab_macd_not_positive"
        if not _between(rsi14, 48, 72):
            return False, "strategy_lab_rsi_not_pullback_band"
        if not _between(bb_pos, 0.25, 0.80):
            return False, "strategy_lab_bb_not_pullback_band"
        return True, None

    if overlay_mode == "trend_pullback_short":
        if signal_side is not Side.SELL:
            return False, "strategy_lab_short_only"
        if trend_label != "bearish":
            return False, "strategy_lab_trend_not_bearish"
        if ema_gap_bps is None or ema_gap_bps >= -0.5:
            return False, "strategy_lab_ema_gap_not_negative_enough"
        if macd_hist is None or macd_hist >= 0:
            return False, "strategy_lab_macd_not_negative"
        if not _between(rsi14, 35, 60):
            return False, "strategy_lab_rsi_not_short_pullback_band"
        if not _between(bb_pos, 0.35, 0.90):
            return False, "strategy_lab_bb_not_short_pullback_band"
        return True, None

    if overlay_mode == "stoch_trend_long":
        if signal_side is not Side.BUY:
            return False, "strategy_lab_long_only"
        if ema_gap_bps is None or ema_gap_bps <= 0:
            return False, "strategy_lab_ema_gap_not_positive"
        if macd_hist is None or macd_hist <= 0:
            return False, "strategy_lab_macd_not_positive"
        if not _between(stoch_k, 55, 95):
            return False, "strategy_lab_stoch_not_bullish"
        if session_return_bps is not None and session_return_bps < -5:
            return False, "strategy_lab_session_drift_negative"
        return True, None

    if overlay_mode == "opening_range_breakout_long":
        if signal_side is not Side.BUY:
            return False, "strategy_lab_long_only"
        if not opening_range_ready:
            return False, "strategy_lab_opening_range_warmup"
        if opening_range_breakout_bps is None or opening_range_breakout_bps <= 1.0:
            return False, "strategy_lab_no_breakout"
        if ema_gap_bps is None or ema_gap_bps <= 0:
            return False, "strategy_lab_ema_gap_not_positive"
        if macd_hist is None or macd_hist <= 0:
            return False, "strategy_lab_macd_not_positive"
        if session_return_bps is None or session_return_bps <= 0:
            return False, "strategy_lab_session_not_positive"
        return True, None

    if overlay_mode == "opening_range_breakdown_short":
        atr14_bps = _coerce_float(indicator_state.get("atr14_bps"))
        if signal_side is not Side.SELL:
            return False, "strategy_lab_short_only"
        if not opening_range_ready:
            return False, "strategy_lab_opening_range_warmup"
        if opening_range_breakdown_bps is None or opening_range_breakdown_bps >= -1.0:
            return False, "strategy_lab_no_breakdown"
        if ema_gap_bps is None or ema_gap_bps >= 0:
            return False, "strategy_lab_ema_gap_not_negative"
        if macd_hist is None or macd_hist >= 0:
            return False, "strategy_lab_macd_not_negative"
        if atr14_bps is None or atr14_bps < 4.0:
            return False, "strategy_lab_atr_too_low"
        if session_return_bps is None or session_return_bps >= 0:
            return False, "strategy_lab_session_not_negative"
        return True, None

    if overlay_mode == "mean_reversion_long_short":
        if signal_side is Side.BUY:
            if not _between(bb_pos, -0.50, 0.45):
                return False, "strategy_lab_long_not_discounted"
            if not _between(rsi14, 20, 55):
                return False, "strategy_lab_long_rsi_not_discounted"
            if not _between(stoch_k, 0, 45):
                return False, "strategy_lab_long_stoch_not_discounted"
            return True, None
        if not _between(bb_pos, 0.55, 1.50):
            return False, "strategy_lab_short_not_premium"
        if not _between(rsi14, 45, 85):
            return False, "strategy_lab_short_rsi_not_premium"
        if not _between(stoch_k, 55, 100):
            return False, "strategy_lab_short_stoch_not_premium"
        if opening_range_ready and opening_range_breakdown_bps is not None and opening_range_breakdown_bps > 5:
            return False, "strategy_lab_short_breakdown_too_deep"
        return True, None

    if overlay_mode == "session_twap_reclaim_long":
        session_twap_gap_bps = _coerce_float(indicator_state.get("session_twap_gap_bps"))
        atr14_bps = _coerce_float(indicator_state.get("atr14_bps"))
        if signal_side is not Side.BUY:
            return False, "strategy_lab_long_only"
        if trend_label != "bullish":
            return False, "strategy_lab_trend_not_bullish"
        if session_twap_gap_bps is None or not _between(session_twap_gap_bps, 0.25, 12.0):
            return False, "strategy_lab_twap_gap_not_long_band"
        if macd_hist is None or macd_hist <= 0:
            return False, "strategy_lab_macd_not_positive"
        if atr14_bps is None or atr14_bps < 4.0:
            return False, "strategy_lab_atr_too_low"
        if not _between(rsi14, 45, 70):
            return False, "strategy_lab_rsi_not_twap_long_band"
        return True, None

    if overlay_mode == "session_twap_reject_short":
        session_twap_gap_bps = _coerce_float(indicator_state.get("session_twap_gap_bps"))
        atr14_bps = _coerce_float(indicator_state.get("atr14_bps"))
        if signal_side is not Side.SELL:
            return False, "strategy_lab_short_only"
        if trend_label != "bearish":
            return False, "strategy_lab_trend_not_bearish"
        if session_twap_gap_bps is None or not _between(session_twap_gap_bps, -12.0, -0.25):
            return False, "strategy_lab_twap_gap_not_short_band"
        if macd_hist is None or macd_hist >= 0:
            return False, "strategy_lab_macd_not_negative"
        if atr14_bps is None or atr14_bps < 4.0:
            return False, "strategy_lab_atr_too_low"
        if not _between(rsi14, 30, 55):
            return False, "strategy_lab_rsi_not_twap_short_band"
        return True, None

    return True, None


def build_regime_replay(
    config: ScalperConfig,
    snapshots: list[Any],
    *,
    top_n: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    baseline_result: dict[str, Any] | None = None
    seen_profiles: set[str] = set()
    for candidate in REGIME_FILTERS:
        candidate_config = build_regime_candidate_config(config, candidate)
        profile_signature = build_regime_profile_signature(candidate_config)
        if profile_signature in seen_profiles:
            continue
        seen_profiles.add(profile_signature)

        raw = simulate_candidate(candidate_config, snapshots)
        item = summarize_regime_candidate(candidate, raw, candidate_config=candidate_config)
        if candidate["name"] == "current_profile":
            baseline_result = item
        results.append(item)

    baseline_net = Decimal(str((baseline_result or {}).get("net_pnl_rub", "0")))
    for item in results:
        item["delta_vs_baseline_rub"] = str(Decimal(str(item["net_pnl_rub"])) - baseline_net)

    ranked = sorted(results, key=regime_sort_key, reverse=True)
    recommendation = build_regime_recommendation(ranked, baseline=baseline_result)
    return {
        "status": "ok",
        "candidate_count": len(results),
        "baseline": baseline_result,
        "top": ranked[: max(1, top_n)],
        "recommendation": recommendation,
    }


def build_regime_candidate_config(config: ScalperConfig, candidate: dict[str, Any]) -> ScalperConfig:
    if candidate["name"] == "current_profile":
        return config
    allow_short = candidate.get("allow_short")
    return replace(
        config,
        regime_filter_mode=str(candidate["mode"]),
        allow_short=config.allow_short if allow_short is None else bool(allow_short),
    )


def build_regime_profile_signature(config: ScalperConfig) -> str:
    return f"{config.regime_filter_mode}|{int(config.allow_short)}"


def summarize_regime_candidate(
    candidate: dict[str, Any],
    raw: dict[str, Any],
    *,
    candidate_config: ScalperConfig,
) -> dict[str, Any]:
    return {
        "name": candidate["name"],
        "description": candidate["description"],
        "mode": candidate_config.regime_filter_mode,
        "allow_short": candidate_config.allow_short,
        "entry_modes": "long+short" if candidate_config.allow_short else "long_only",
        "is_baseline": candidate["name"] == "current_profile",
        "trade_count": int(raw.get("trade_count", 0)),
        "wins": int(raw.get("wins", 0)),
        "losses": int(raw.get("losses", 0)),
        "win_rate_pct": raw.get("win_rate_pct"),
        "signals_detected": int(raw.get("signals_detected", 0)),
        "filtered_signal_count": int(raw.get("filtered_signal_count", 0)),
        "net_pnl_rub": raw.get("net_pnl_rub"),
        "equity_delta_rub": raw.get("equity_delta_rub"),
        "profit_factor": raw.get("profit_factor"),
        "expectancy_bps": raw.get("expectancy_bps"),
        "average_trade_rub": raw.get("average_trade_rub"),
        "max_drawdown_rub": raw.get("max_drawdown_rub"),
        "blocked_top": dict(raw.get("blocked_top") or {}),
        "score": raw.get("score"),
    }


def regime_sort_key(item: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, int]:
    return (
        Decimal(str(item.get("score", "0"))),
        Decimal(str(item.get("net_pnl_rub", "0"))),
        Decimal(str(item.get("profit_factor", "0"))),
        int(item.get("trade_count", 0)),
    )


def build_regime_recommendation(
    ranked: list[dict[str, Any]],
    *,
    baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    non_baseline = [item for item in ranked if not item.get("is_baseline", False)]
    if not non_baseline:
        return {"eligible": False, "reason": "no_candidates", "candidate": None}
    top = non_baseline[0]
    delta = Decimal(str(top.get("delta_vs_baseline_rub", "0")))
    if int(top.get("trade_count", 0)) < REGIME_PREVIEW_MIN_TRADES:
        return {"eligible": False, "reason": "insufficient_trade_sample", "candidate": top}
    if baseline is None:
        return {"eligible": False, "reason": "missing_baseline", "candidate": top}
    if delta <= 0:
        return {"eligible": False, "reason": "no_positive_delta_vs_baseline", "candidate": top}
    return {"eligible": True, "reason": "best_positive_regime_filter", "candidate": top}


def maybe_write_report(runtime_dir: Path, payload: dict[str, Any], *, enabled: bool) -> None:
    if not enabled:
        return
    research_dir = runtime_dir / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    latest_path = research_dir / "latest.json"
    history_path = research_dir / "history.jsonl"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    latest_path.write_text(body, encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _series_last(series: Any) -> float | None:
    cleaned = series.dropna()
    if cleaned.empty:
        return None
    return float(cleaned.iloc[-1])


def _float_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return round(float(value), 6)


def _best_by(items: list[dict[str, Any]], key: str, *, reverse: bool) -> dict[str, Any] | None:
    candidates = [item for item in items if item.get(key) is not None]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: float(item[key]), reverse=reverse)[0]


def build_snapshot_key(snapshot: Any) -> str:
    return "|".join(
        [
            snapshot.instrument.instrument_id,
            snapshot.at.isoformat(),
            str(snapshot.bid_price),
            str(snapshot.ask_price),
            str(snapshot.bid_quantity),
            str(snapshot.ask_quantity),
        ]
    )


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    return numeric


def _between(value: float | None, lower: float, upper: float) -> bool:
    if value is None:
        return False
    return lower <= value <= upper


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}
