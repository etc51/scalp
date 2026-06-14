from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ScalperConfig
from .market_history import load_snapshots_from_paths
from .optimizer import filter_snapshots_for_entry_window, resolve_snapshot_files


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
    for ticker in sorted(frame["ticker"].unique()):
        ticker_frame = frame.loc[frame["ticker"] == ticker].copy()
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
            continue

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
            rs = avg_gain / avg_loss.replace(0, pd.NA)
            minute["rsi14"] = 100 - (100 / (1 + rs))
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            minute["macd"] = ema12 - ema26
            minute["macd_signal"] = minute["macd"].ewm(span=9, adjust=False).mean()
            minute["macd_hist"] = minute["macd"] - minute["macd_signal"]

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
                "trend_label": classify_trend(
                    rsi14=rsi14,
                    ema_gap_bps=ema_gap_bps,
                    macd_hist=macd_hist_value,
                ),
            }
        )

    ticker_reports.sort(key=lambda item: item["ticker"])
    summary = build_research_summary(ticker_reports, snapshot_count=len(snapshots))
    focus = build_research_focus(ticker_reports)
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
        "tickers": ticker_reports[: max(1, top_n)],
        "all_tickers_count": len(ticker_reports),
    }
    maybe_write_report(config.runtime_dir, payload, enabled=write_report)
    return payload


def build_research_summary(tickers: list[dict[str, Any]], *, snapshot_count: int) -> dict[str, Any]:
    bullish = sum(1 for item in tickers if item["trend_label"] == "bullish")
    bearish = sum(1 for item in tickers if item["trend_label"] == "bearish")
    neutral = sum(1 for item in tickers if item["trend_label"] == "neutral")
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
    }


def build_research_focus(tickers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not tickers:
        return []
    focus: list[dict[str, Any]] = []
    strongest = _best_by(tickers, "session_return_bps", reverse=True)
    weakest = _best_by(tickers, "session_return_bps", reverse=False)
    highest_vol = _best_by(tickers, "realized_volatility_bps", reverse=True)
    highest_rsi = _best_by(tickers, "rsi14", reverse=True)

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
    return focus[:4]


def classify_trend(*, rsi14: float | None, ema_gap_bps: float | None, macd_hist: float | None) -> str:
    if rsi14 is None or ema_gap_bps is None or macd_hist is None:
        return "neutral"
    if rsi14 >= 55 and ema_gap_bps > 0 and macd_hist > 0:
        return "bullish"
    if rsi14 <= 45 and ema_gap_bps < 0 and macd_hist < 0:
        return "bearish"
    return "neutral"


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
