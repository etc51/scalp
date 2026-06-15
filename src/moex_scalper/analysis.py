from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .config import ScalperConfig
from .entry_window import moment_in_entry_window


def _decimal(value: str | int | float | Decimal | None, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _median_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


@dataclass(slots=True, frozen=True)
class TradeRecord:
    instrument_id: str
    ticker: str
    side: str
    quantity_lots: int
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    gross_pnl_rub: Decimal
    fees_rub: Decimal
    net_pnl_rub: Decimal
    entry_reason: str
    exit_reason: str
    hold_seconds: float


def analyze_trades(
    config: ScalperConfig,
    *,
    date_key: str | None,
    input_path: str | None,
    top_n: int,
    days: int,
    write_report: bool,
) -> dict[str, Any]:
    trades_path = resolve_trade_path(config.runtime_dir, input_path)
    records = load_trade_records(trades_path)
    report_key = build_report_key(config, date_key=date_key, days=days)

    if not records:
        payload = {
            "status": "no_data",
            "trade_file": str(trades_path),
            "trade_count": 0,
            "message": "No recorded paper trades found for analysis.",
        }
        maybe_write_report(config.runtime_dir, payload, report_key=report_key, enabled=write_report)
        return payload

    resolved_end_date = date.fromisoformat(resolve_date_key(config, date_key))
    rolling_days = max(1, days)
    start_date = resolved_end_date - timedelta(days=rolling_days - 1)
    selected = [
        record
        for record in records
        if start_date <= record.closed_at.astimezone(config.timezone).date() <= resolved_end_date
    ]

    if not selected:
        payload = {
            "status": "no_window_data",
            "trade_file": str(trades_path),
            "trade_count": 0,
            "window": {
                "start_date": start_date.isoformat(),
                "end_date": resolved_end_date.isoformat(),
                "days_requested": rolling_days,
                "days_with_trades": 0,
                "included_dates": [],
                "timezone": config.timezone_name,
            },
            "message": "Recorded trades exist, but none fall into the requested rolling window.",
        }
        maybe_write_report(config.runtime_dir, payload, report_key=report_key, enabled=write_report)
        return payload

    selected_dates = sorted(
        {
            record.closed_at.astimezone(config.timezone).date().isoformat()
            for record in selected
        }
    )
    filtered, entry_window_summary = filter_trade_records_for_entry_window(config, selected)
    if not filtered:
        payload = {
            "status": "no_entry_window_data",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "trade_file": str(trades_path),
            "raw_trade_count": len(selected),
            "trade_count": 0,
            "assessment": "no_entry_window_data",
            "window": {
                "start_date": start_date.isoformat(),
                "end_date": resolved_end_date.isoformat(),
                "days_requested": rolling_days,
                "days_with_trades": len(selected_dates),
                "included_dates": selected_dates,
                "timezone": config.timezone_name,
            },
            "entry_window_summary": entry_window_summary,
            "message": "Recorded trades exist in the rolling window, but none were opened inside the configured entry window.",
        }
        maybe_write_report(config.runtime_dir, payload, report_key=report_key, enabled=write_report)
        return payload

    included_dates = sorted(
        {
            record.closed_at.astimezone(config.timezone).date().isoformat()
            for record in filtered
        }
    )
    summary = summarize_records(filtered)
    ticker_stats = build_breakdown(filtered, key_fn=lambda item: item.ticker)
    hour_stats = build_breakdown(
        filtered,
        key_fn=lambda item: item.opened_at.astimezone(config.timezone).strftime("%H:00"),
    )
    ticker_hour_stats = build_breakdown(
        filtered,
        key_fn=lambda item: (
            f"{item.ticker}@{item.opened_at.astimezone(config.timezone).strftime('%H:00')}"
        ),
    )
    exit_reason_stats = build_breakdown(filtered, key_fn=lambda item: item.exit_reason)
    focus = build_focus(
        summary,
        ticker_stats=ticker_stats,
        hour_stats=hour_stats,
        ticker_hour_stats=ticker_hour_stats,
        exit_reason_stats=exit_reason_stats,
    )

    payload = {
        "status": "ok",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "trade_file": str(trades_path),
        "raw_trade_count": len(selected),
        "trade_count": len(filtered),
        "window": {
            "start_date": start_date.isoformat(),
            "end_date": resolved_end_date.isoformat(),
            "days_requested": rolling_days,
            "days_with_trades": len(included_dates),
            "included_dates": included_dates,
            "timezone": config.timezone_name,
        },
        "entry_window_summary": entry_window_summary,
        "summary": summary,
        "assessment": classify_assessment(summary),
        "focus": focus,
        "by_ticker": build_ranked_section(ticker_stats, top_n=top_n),
        "by_hour": build_ranked_section(hour_stats, top_n=top_n),
        "by_ticker_hour": build_ranked_section(ticker_hour_stats, top_n=top_n),
        "by_exit_reason": build_ranked_section(exit_reason_stats, top_n=top_n),
        "largest_losses": [
            serialize_trade_record(record)
            for record in sorted(filtered, key=lambda item: item.net_pnl_rub)[:top_n]
        ],
        "largest_wins": [
            serialize_trade_record(record)
            for record in sorted(filtered, key=lambda item: item.net_pnl_rub, reverse=True)[:top_n]
        ],
    }
    maybe_write_report(config.runtime_dir, payload, report_key=report_key, enabled=write_report)
    return payload


def resolve_trade_path(runtime_dir: Path, input_path: str | None) -> Path:
    if input_path:
        candidate = Path(input_path)
        if candidate.is_dir():
            return candidate / "paper_trades.jsonl"
        return candidate
    return runtime_dir / "paper_trades.jsonl"


def resolve_date_key(config: ScalperConfig, explicit: str | None) -> str:
    if explicit:
        return explicit
    return datetime.now(config.timezone).date().isoformat()


def build_report_key(config: ScalperConfig, *, date_key: str | None, days: int) -> str:
    return f"{resolve_date_key(config, date_key)}-d{max(1, days)}"


def load_trade_records(path: Path) -> list[TradeRecord]:
    if not path.exists():
        return []
    records: list[TradeRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
                opened_at = datetime.fromisoformat(str(item["opened_at"]))
                closed_at = datetime.fromisoformat(str(item["closed_at"]))
                hold_seconds = float(item.get("hold_seconds", (closed_at - opened_at).total_seconds()))
                records.append(
                    TradeRecord(
                        instrument_id=str(item.get("instrument_id", "")),
                        ticker=str(item.get("ticker", "")),
                        side=str(item.get("side", "")),
                        quantity_lots=int(item.get("quantity_lots", 0)),
                        entry_price=_decimal(item.get("entry_price")),
                        exit_price=_decimal(item.get("exit_price")),
                        opened_at=opened_at,
                        closed_at=closed_at,
                        gross_pnl_rub=_decimal(item.get("gross_pnl_rub")),
                        fees_rub=_decimal(item.get("fees_rub")),
                        net_pnl_rub=_decimal(item.get("net_pnl_rub")),
                        entry_reason=str(item.get("entry_reason", "")),
                        exit_reason=str(item.get("exit_reason", "")),
                        hold_seconds=hold_seconds,
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError):
                continue
    return records


def filter_trade_records_for_entry_window(
    config: ScalperConfig,
    records: list[TradeRecord],
) -> tuple[list[TradeRecord], dict[str, Any]]:
    included: list[TradeRecord] = []
    excluded_reasons: Counter[str] = Counter()
    included_dates: set[str] = set()
    excluded_dates: set[str] = set()

    for record in records:
        allowed, reason = moment_in_entry_window(config, record.opened_at)
        local_date = record.opened_at.astimezone(config.timezone).date().isoformat()
        if allowed:
            included.append(record)
            included_dates.add(local_date)
        else:
            excluded_reasons[reason] += 1
            excluded_dates.add(local_date)

    summary = {
        "timezone": config.timezone_name,
        "weekdays": list(config.entry_weekdays),
        "start": config.entry_start_time.isoformat(timespec="minutes"),
        "end": config.entry_end_time.isoformat(timespec="minutes"),
        "filter_basis": "opened_at",
        "raw_trade_count": len(records),
        "included_trade_count": len(included),
        "excluded_trade_count": len(records) - len(included),
        "included_dates": sorted(included_dates),
        "excluded_dates": sorted(excluded_dates),
        "excluded_reasons": dict(excluded_reasons),
    }
    return included, summary


def summarize_records(records: list[TradeRecord]) -> dict[str, Any]:
    trade_count = len(records)
    net_pnl_rub = sum((record.net_pnl_rub for record in records), start=Decimal("0"))
    gross_pnl_rub = sum((record.gross_pnl_rub for record in records), start=Decimal("0"))
    fees_rub = sum((record.fees_rub for record in records), start=Decimal("0"))
    hold_seconds_sum = sum(record.hold_seconds for record in records)
    wins = [record for record in records if record.net_pnl_rub > 0]
    losses = [record for record in records if record.net_pnl_rub < 0]
    flats = [record for record in records if record.net_pnl_rub == 0]
    gross_wins = sum((record.net_pnl_rub for record in wins), start=Decimal("0"))
    gross_losses = sum((-record.net_pnl_rub for record in losses), start=Decimal("0"))
    total_losses = sum((record.net_pnl_rub for record in losses), start=Decimal("0"))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else (Decimal("999") if gross_wins > 0 else Decimal("0"))
    latest_trade = max(records, key=lambda item: item.closed_at)
    pnls = [record.net_pnl_rub for record in records]

    return {
        "trade_count": trade_count,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "flat_trades": len(flats),
        "win_rate_pct": round((len(wins) / trade_count) * 100, 2) if trade_count else 0.0,
        "net_pnl_rub": str(net_pnl_rub),
        "gross_pnl_rub": str(gross_pnl_rub),
        "fees_rub": str(fees_rub),
        "expectancy_rub": str((net_pnl_rub / Decimal(trade_count)) if trade_count else Decimal("0")),
        "average_win_rub": str((gross_wins / Decimal(len(wins))) if wins else Decimal("0")),
        "average_loss_rub": str((total_losses / Decimal(len(losses))) if losses else Decimal("0")),
        "median_trade_rub": str(_median_decimal(pnls)),
        "profit_factor": str(profit_factor),
        "average_hold_seconds": round(hold_seconds_sum / trade_count, 3) if trade_count else 0.0,
        "best_trade_rub": str(max(pnls)) if pnls else "0",
        "worst_trade_rub": str(min(pnls)) if pnls else "0",
        "last_trade_at": latest_trade.closed_at.isoformat(),
        "last_ticker": latest_trade.ticker,
    }


def build_breakdown(
    records: list[TradeRecord],
    *,
    key_fn: Callable[[TradeRecord], str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[TradeRecord]] = defaultdict(list)
    for record in records:
        grouped[key_fn(record)].append(record)

    breakdown: list[dict[str, Any]] = []
    for key, items in grouped.items():
        summary = summarize_records(items)
        summary["key"] = key
        breakdown.append(summary)
    return breakdown


def build_ranked_section(items: list[dict[str, Any]], *, top_n: int) -> dict[str, Any]:
    return {
        "worst": sorted(items, key=lambda item: (Decimal(str(item["net_pnl_rub"])), item["trade_count"]))[:top_n],
        "best": sorted(
            items,
            key=lambda item: (Decimal(str(item["net_pnl_rub"])), item["trade_count"]),
            reverse=True,
        )[:top_n],
        "count": len(items),
    }


def build_focus(
    summary: dict[str, Any],
    *,
    ticker_stats: list[dict[str, Any]],
    hour_stats: list[dict[str, Any]],
    ticker_hour_stats: list[dict[str, Any]],
    exit_reason_stats: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    focus: list[dict[str, Any]] = []
    trade_count = int(summary.get("trade_count", 0))
    net_pnl_rub = _decimal(summary.get("net_pnl_rub"))
    profit_factor = _decimal(summary.get("profit_factor"))

    if trade_count < 5:
        focus.append(
            {
                "type": "sample",
                "message": "Нужно хотя бы 5 сделок в окне, чтобы выводы стали устойчивее.",
                "trade_count": trade_count,
            }
        )
    elif net_pnl_rub <= 0 and profit_factor < Decimal("1"):
        focus.append(
            {
                "type": "expectancy",
                "message": "Пока отрицательное expectancy: убытки по размеру перевешивают победы.",
                "net_pnl_rub": str(net_pnl_rub),
                "profit_factor": str(profit_factor),
            }
        )

    worst_ticker = next(
        (
            item
            for item in sorted(ticker_stats, key=lambda row: Decimal(str(row["net_pnl_rub"])))
            if _decimal(item["net_pnl_rub"]) < 0
        ),
        None,
    )
    if worst_ticker is not None:
        focus.append(
            {
                "type": "ticker",
                "message": f"Самый слабый тикер окна: {worst_ticker['key']}.",
                "key": worst_ticker["key"],
                "net_pnl_rub": worst_ticker["net_pnl_rub"],
                "trade_count": worst_ticker["trade_count"],
            }
        )

    worst_ticker_hour = next(
        (
            item
            for item in sorted(ticker_hour_stats, key=lambda row: Decimal(str(row["net_pnl_rub"])))
            if _decimal(item["net_pnl_rub"]) < 0
        ),
        None,
    )
    if worst_ticker_hour is not None:
        focus.append(
            {
                "type": "ticker_hour",
                "message": f"Самая слабая связка окна: {worst_ticker_hour['key']}.",
                "key": worst_ticker_hour["key"],
                "net_pnl_rub": worst_ticker_hour["net_pnl_rub"],
                "trade_count": worst_ticker_hour["trade_count"],
            }
        )

    worst_hour = next(
        (
            item
            for item in sorted(hour_stats, key=lambda row: Decimal(str(row["net_pnl_rub"])))
            if _decimal(item["net_pnl_rub"]) < 0
        ),
        None,
    )
    if worst_hour is not None:
        focus.append(
            {
                "type": "hour",
                "message": f"Самый слабый час входа: {worst_hour['key']}.",
                "key": worst_hour["key"],
                "net_pnl_rub": worst_hour["net_pnl_rub"],
                "trade_count": worst_hour["trade_count"],
            }
        )

    worst_exit = next(
        (
            item
            for item in sorted(exit_reason_stats, key=lambda row: Decimal(str(row["net_pnl_rub"])))
            if _decimal(item["net_pnl_rub"]) < 0
        ),
        None,
    )
    if worst_exit is not None:
        focus.append(
            {
                "type": "exit_reason",
                "message": f"Наибольший ущерб сейчас дает выход {worst_exit['key']}.",
                "key": worst_exit["key"],
                "net_pnl_rub": worst_exit["net_pnl_rub"],
                "trade_count": worst_exit["trade_count"],
            }
        )

    return focus[:5]


def classify_assessment(summary: dict[str, Any]) -> str:
    trade_count = int(summary.get("trade_count", 0))
    net_pnl_rub = _decimal(summary.get("net_pnl_rub"))
    profit_factor = _decimal(summary.get("profit_factor"))

    if trade_count == 0:
        return "no_trades"
    if trade_count < 5:
        return "insufficient_sample"
    if net_pnl_rub > 0 and profit_factor > Decimal("1"):
        return "positive_expectancy_so_far"
    if net_pnl_rub > 0:
        return "positive_pnl_but_fragile"
    if profit_factor < Decimal("1"):
        return "negative_expectancy_so_far"
    return "mixed_sample"


def serialize_trade_record(record: TradeRecord) -> dict[str, Any]:
    return {
        "ticker": record.ticker,
        "side": record.side,
        "quantity_lots": record.quantity_lots,
        "entry_price": str(record.entry_price),
        "exit_price": str(record.exit_price),
        "opened_at": record.opened_at.isoformat(),
        "closed_at": record.closed_at.isoformat(),
        "gross_pnl_rub": str(record.gross_pnl_rub),
        "fees_rub": str(record.fees_rub),
        "net_pnl_rub": str(record.net_pnl_rub),
        "exit_reason": record.exit_reason,
        "hold_seconds": round(record.hold_seconds, 3),
    }


def maybe_write_report(runtime_dir: Path, payload: dict[str, Any], *, report_key: str, enabled: bool) -> None:
    if not enabled:
        return
    analysis_dir = runtime_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    report_path = analysis_dir / f"analyze-{report_key}.json"
    latest_path = analysis_dir / "latest.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    report_path.write_text(body, encoding="utf-8")
    latest_path.write_text(body, encoding="utf-8")
