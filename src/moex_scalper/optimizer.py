from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .commission import CommissionModel
from .config import ScalperConfig
from .domain import ClosedTrade, MarketSnapshot, Position
from .execution import PaperExecutor
from .market_history import load_snapshots_from_paths, snapshot_path_for_date
from .risk import RiskManager
from .strategy import ModerateScalpingStrategy


def optimize_parameters(
    config: ScalperConfig,
    *,
    date_key: str | None,
    input_path: str | None,
    top_n: int,
    days: int,
    min_trades: int,
    write_report: bool,
) -> dict[str, Any]:
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
            "snapshot_files": [str(path) for path in snapshot_files],
            "snapshot_count": 0,
            "message": "No recorded market snapshots found for analysis.",
        }
        maybe_write_report(
            config.runtime_dir,
            payload,
            report_key=build_report_key(config, date_key=date_key, days=days),
            enabled=write_report,
        )
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
        maybe_write_report(
            config.runtime_dir,
            payload,
            report_key=build_report_key(config, date_key=date_key, days=days),
            enabled=write_report,
        )
        return payload
    signal_coverage = build_signal_coverage(config, snapshots, top_n=top_n)

    candidates = build_candidate_configs(config)
    current_signature = parameter_signature(config)
    results: list[dict[str, Any]] = []
    baseline: dict[str, Any] | None = None
    for candidate in candidates:
        result = simulate_candidate(candidate, snapshots)
        result["is_current_config"] = parameter_signature(candidate) == current_signature
        if result["is_current_config"]:
            baseline = result
        results.append(result)

    results.sort(key=sort_key, reverse=True)
    top_results = results[: max(1, top_n)]
    recommendation = build_recommendation(
        top_result=top_results[0] if top_results else None,
        baseline=baseline,
        min_trades=min_trades,
    )

    payload = {
        "status": "ok",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "snapshot_files": [str(path) for path in snapshot_files],
        "raw_snapshot_count": len(raw_snapshots),
        "snapshot_count": len(snapshots),
        "entry_window_summary": entry_window_summary,
        "signal_coverage": signal_coverage,
        "candidate_count": len(results),
        "top": top_results,
        "baseline": baseline,
        "recommendation": recommendation,
    }
    maybe_write_report(
        config.runtime_dir,
        payload,
        report_key=build_report_key(config, date_key=date_key, days=days),
        enabled=write_report,
    )
    return payload


def build_report_key(config: ScalperConfig, *, date_key: str | None, days: int) -> str:
    resolved_date = resolve_date_key(config, date_key)
    return f"{resolved_date}-d{days}"


def resolve_snapshot_files(
    config: ScalperConfig,
    *,
    date_key: str | None,
    input_path: str | None,
    days: int,
) -> list[Path]:
    if input_path:
        path = Path(input_path)
        if path.is_dir():
            return sorted(path.glob("*.jsonl"))
        return [path]

    days = max(1, days)
    market_dir = config.runtime_dir / "market"
    resolved_date = date.fromisoformat(resolve_date_key(config, date_key))
    paths: list[Path] = []
    cursor = resolved_date
    while len(paths) < days:
        candidate = snapshot_path_for_date(config.runtime_dir, cursor.isoformat())
        if candidate.exists():
            paths.append(candidate)
        cursor -= timedelta(days=1)
        if cursor < resolved_date - timedelta(days=21):
            break
    paths.sort()
    return paths


def resolve_date_key(config: ScalperConfig, explicit: str | None) -> str:
    if explicit:
        return explicit
    return datetime.now(config.timezone).date().isoformat()


def filter_snapshots_for_entry_window(
    config: ScalperConfig,
    snapshots: list[MarketSnapshot],
) -> tuple[list[MarketSnapshot], dict[str, Any]]:
    included: list[MarketSnapshot] = []
    excluded_reasons: Counter[str] = Counter()
    included_dates: set[str] = set()
    excluded_dates: set[str] = set()

    for snapshot in snapshots:
        allowed, reason = snapshot_in_entry_window(config, snapshot.at)
        local_date = snapshot.at.astimezone(config.timezone).date().isoformat()
        if allowed:
            included.append(snapshot)
            included_dates.add(local_date)
        else:
            excluded_reasons[reason] += 1
            excluded_dates.add(local_date)

    summary = {
        "timezone": config.timezone_name,
        "weekdays": list(config.entry_weekdays),
        "start": config.entry_start_time.isoformat(timespec="minutes"),
        "end": config.entry_end_time.isoformat(timespec="minutes"),
        "raw_snapshot_count": len(snapshots),
        "included_snapshot_count": len(included),
        "excluded_snapshot_count": len(snapshots) - len(included),
        "included_dates": sorted(included_dates),
        "excluded_dates": sorted(excluded_dates),
        "excluded_reasons": dict(excluded_reasons),
    }
    return included, summary


def snapshot_in_entry_window(config: ScalperConfig, moment: datetime) -> tuple[bool, str]:
    local_now = moment.astimezone(config.timezone)
    if local_now.weekday() not in config.entry_weekdays:
        return False, "entry_weekday_closed"
    current_time = local_now.time().replace(tzinfo=None)
    if current_time < config.entry_start_time:
        return False, "entry_before_window"
    if current_time > config.entry_end_time:
        return False, "entry_after_window"
    return True, "ok"


def build_signal_coverage(
    config: ScalperConfig,
    snapshots: list[MarketSnapshot],
    *,
    top_n: int,
) -> dict[str, Any]:
    strategy = ModerateScalpingStrategy(config)
    totals = _new_coverage_bucket()
    by_ticker: dict[str, dict[str, Any]] = {}
    by_hour: dict[str, dict[str, Any]] = {}

    for snapshot in snapshots:
        local_hour = snapshot.at.astimezone(config.timezone).strftime("%H:00")
        ticker_bucket = by_ticker.setdefault(snapshot.instrument.ticker, _new_coverage_bucket())
        hour_bucket = by_hour.setdefault(local_hour, _new_coverage_bucket())
        signal, block_reason, metrics = strategy.diagnose_entry(snapshot, has_open_position=False)
        _update_coverage_bucket(totals, snapshot, signal=signal, block_reason=block_reason, metrics=metrics, config=config)
        _update_coverage_bucket(ticker_bucket, snapshot, signal=signal, block_reason=block_reason, metrics=metrics, config=config)
        _update_coverage_bucket(hour_bucket, snapshot, signal=signal, block_reason=block_reason, metrics=metrics, config=config)

    return {
        "config_signature": parameter_signature(config),
        "summary": _finalize_coverage_bucket("all", totals),
        "by_ticker": build_ranked_coverage(by_ticker, top_n=top_n),
        "by_hour": build_ranked_coverage(by_hour, top_n=top_n),
    }


def build_ranked_coverage(items: dict[str, dict[str, Any]], *, top_n: int) -> dict[str, Any]:
    finalized = [_finalize_coverage_bucket(key, payload) for key, payload in items.items()]
    worst = sorted(
        finalized,
        key=lambda item: (
            Decimal(str(item["signal_ready_rate_pct"])),
            -int(item["snapshot_count"]),
            int(item["signal_ready_count"]),
        ),
    )[:top_n]
    best = sorted(
        finalized,
        key=lambda item: (
            Decimal(str(item["signal_ready_rate_pct"])),
            int(item["signal_ready_count"]),
            -int(item["snapshot_count"]),
        ),
        reverse=True,
    )[:top_n]
    return {
        "count": len(finalized),
        "worst": worst,
        "best": best,
    }


def _new_coverage_bucket() -> dict[str, Any]:
    return {
        "snapshot_count": 0,
        "signal_ready_count": 0,
        "spread_pass_count": 0,
        "imbalance_pass_count": 0,
        "impulse_pass_count": 0,
        "expected_edge_pass_count": 0,
        "blocked": Counter(),
        "sum_spread_bps": Decimal("0"),
        "sum_imbalance": Decimal("0"),
        "sum_impulse_bps": Decimal("0"),
    }


def _update_coverage_bucket(
    bucket: dict[str, Any],
    snapshot: MarketSnapshot,
    *,
    signal: Any,
    block_reason: str,
    metrics: dict[str, Any],
    config: ScalperConfig,
) -> None:
    spread_bps = Decimal(str(metrics.get("spread_bps", snapshot.spread_bps)))
    imbalance = Decimal(str(metrics.get("imbalance", snapshot.imbalance)))
    impulse_bps = Decimal(str(metrics.get("impulse_bps", "0")))
    expected_edge_raw = metrics.get("expected_edge_bps")
    expected_edge_bps = Decimal(str(expected_edge_raw)) if expected_edge_raw is not None else None

    bucket["snapshot_count"] += 1
    bucket["sum_spread_bps"] += spread_bps
    bucket["sum_imbalance"] += imbalance
    bucket["sum_impulse_bps"] += impulse_bps

    spread_pass = spread_bps <= config.max_spread_bps
    imbalance_pass = spread_pass and imbalance >= config.min_imbalance
    impulse_pass = imbalance_pass and impulse_bps >= config.min_impulse_bps
    expected_edge_pass = signal is not None or (
        impulse_pass
        and expected_edge_bps is not None
        and expected_edge_bps >= config.min_expected_edge_bps
    )

    if spread_pass:
        bucket["spread_pass_count"] += 1
    if imbalance_pass:
        bucket["imbalance_pass_count"] += 1
    if impulse_pass:
        bucket["impulse_pass_count"] += 1
    if expected_edge_pass:
        bucket["expected_edge_pass_count"] += 1

    if signal is not None:
        bucket["signal_ready_count"] += 1
    else:
        bucket["blocked"][block_reason] += 1


def _finalize_coverage_bucket(key: str, bucket: dict[str, Any]) -> dict[str, Any]:
    snapshot_count = int(bucket["snapshot_count"])
    signal_ready_count = int(bucket["signal_ready_count"])
    return {
        "key": key,
        "snapshot_count": snapshot_count,
        "signal_ready_count": signal_ready_count,
        "signal_ready_rate_pct": _pct(signal_ready_count, snapshot_count),
        "spread_pass_rate_pct": _pct(int(bucket["spread_pass_count"]), snapshot_count),
        "imbalance_pass_rate_pct": _pct(int(bucket["imbalance_pass_count"]), snapshot_count),
        "impulse_pass_rate_pct": _pct(int(bucket["impulse_pass_count"]), snapshot_count),
        "expected_edge_pass_rate_pct": _pct(int(bucket["expected_edge_pass_count"]), snapshot_count),
        "average_spread_bps": _avg_decimal(bucket["sum_spread_bps"], snapshot_count),
        "average_imbalance": _avg_decimal(bucket["sum_imbalance"], snapshot_count),
        "average_impulse_bps": _avg_decimal(bucket["sum_impulse_bps"], snapshot_count),
        "top_blocked_reasons": [
            {"reason": reason, "count": count}
            for reason, count in bucket["blocked"].most_common(3)
        ],
    }


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 2)


def _avg_decimal(total: Decimal, count: int) -> str:
    if count <= 0:
        return "0"
    return str(total / Decimal(count))


def build_candidate_configs(base: ScalperConfig) -> list[ScalperConfig]:
    candidates: list[ScalperConfig] = [base]
    variants: dict[str, list[Any]] = {
        "max_spread_bps": [Decimal("1.5"), Decimal("2.5"), Decimal("4.0"), Decimal("5.5")],
        "min_imbalance": [Decimal("0.45"), Decimal("0.50"), Decimal("0.55"), Decimal("0.60"), Decimal("0.66")],
        "min_impulse_bps": [Decimal("1.0"), Decimal("1.5"), Decimal("2.5"), Decimal("4.0")],
        "take_profit_bps": [Decimal("8"), Decimal("10"), Decimal("12"), Decimal("14")],
        "stop_loss_bps": [Decimal("4"), Decimal("6"), Decimal("8"), Decimal("10")],
        "time_stop_seconds": [3.0, 4.0, 6.0, 8.0],
        "min_expected_edge_bps": [Decimal("4"), Decimal("6"), Decimal("8"), Decimal("10")],
        "cooldown_seconds": [0.0, 1.0, 3.0, 5.0],
    }
    for field_name, values in variants.items():
        current_value = getattr(base, field_name)
        for value in values:
            if value == current_value:
                continue
            candidate = replace(base, **{field_name: value})
            if candidate.stop_loss_bps > candidate.take_profit_bps:
                continue
            candidates.append(candidate)

    preset_overrides = [
        {
            "max_spread_bps": Decimal("1.5"),
            "min_imbalance": Decimal("0.66"),
            "min_impulse_bps": Decimal("4.0"),
            "take_profit_bps": Decimal("12"),
            "stop_loss_bps": Decimal("4"),
            "time_stop_seconds": 3.0,
            "min_expected_edge_bps": Decimal("10"),
            "cooldown_seconds": 5.0,
        },
        {
            "max_spread_bps": Decimal("2.5"),
            "min_imbalance": Decimal("0.55"),
            "min_impulse_bps": Decimal("2.5"),
            "take_profit_bps": Decimal("10"),
            "stop_loss_bps": Decimal("6"),
            "time_stop_seconds": 4.0,
            "min_expected_edge_bps": Decimal("6"),
            "cooldown_seconds": 1.0,
        },
        {
            "max_spread_bps": Decimal("5.5"),
            "min_imbalance": Decimal("0.45"),
            "min_impulse_bps": Decimal("1.0"),
            "take_profit_bps": Decimal("8"),
            "stop_loss_bps": Decimal("6"),
            "time_stop_seconds": 8.0,
            "min_expected_edge_bps": Decimal("4"),
            "cooldown_seconds": 0.0,
        },
    ]
    for overrides in preset_overrides:
        candidate = replace(base, **overrides)
        if candidate.stop_loss_bps > candidate.take_profit_bps:
            continue
        candidates.append(candidate)

    deduped: list[ScalperConfig] = []
    seen: set[str] = set()
    for candidate in candidates:
        signature = parameter_signature(candidate)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(candidate)
    return deduped


def parameter_signature(config: ScalperConfig) -> str:
    return "|".join(
        [
            str(config.max_spread_bps),
            str(config.min_imbalance),
            str(config.min_impulse_bps),
            str(config.take_profit_bps),
            str(config.stop_loss_bps),
            str(config.time_stop_seconds),
            str(config.min_expected_edge_bps),
            str(config.cooldown_seconds),
        ]
    )


def sort_key(item: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, int]:
    return (
        Decimal(str(item["score"])),
        Decimal(str(item["equity_delta_rub"])),
        Decimal(str(item["profit_factor"])),
        int(item["trade_count"]),
    )


def simulate_candidate(
    config: ScalperConfig,
    snapshots: list[MarketSnapshot],
    *,
    entry_filter: Callable[[MarketSnapshot, Any], tuple[bool, str | None]] | None = None,
) -> dict[str, Any]:
    strategy = ModerateScalpingStrategy(config)
    risk = RiskManager(config)
    executor = PaperExecutor(
        commission_model=CommissionModel(config.premium_share_commission_bps),
        initial_cash_rub=config.paper_initial_cash_rub,
    )
    positions: dict[str, Position] = {}
    trades: list[ClosedTrade] = []
    blocked: Counter[str] = Counter()
    latest_bid_by_instrument: dict[str, Decimal] = {}
    signals_detected = 0
    filtered_signal_count = 0
    peak_equity = executor.initial_cash_rub
    max_drawdown_rub = Decimal("0")
    equity_curve: list[Decimal] = []

    for snapshot in snapshots:
        latest_bid_by_instrument[snapshot.instrument.instrument_id] = snapshot.bid_price
        position = positions.get(snapshot.instrument.instrument_id)
        if position is not None:
            exit_decision = strategy.evaluate_exit(position, snapshot)
            if exit_decision:
                report = executor.execute_exit_sync(snapshot, position.quantity_lots)
                gross_pnl = (
                    (report.fill_price - position.entry_price)
                    * Decimal(position.instrument.lot_size)
                    * Decimal(position.quantity_lots)
                )
                fees = position.entry_fee_rub + report.fee_rub
                net_pnl = gross_pnl - fees
                trade = ClosedTrade(
                    instrument=position.instrument,
                    side=position.side,
                    quantity_lots=position.quantity_lots,
                    entry_price=position.entry_price,
                    exit_price=report.fill_price,
                    opened_at=position.opened_at,
                    closed_at=report.filled_at,
                    gross_pnl_rub=gross_pnl,
                    fees_rub=fees,
                    net_pnl_rub=net_pnl,
                    entry_reason=position.reason,
                    exit_reason=exit_decision.reason,
                )
                positions.pop(snapshot.instrument.instrument_id, None)
                trades.append(trade)
                risk.note_closed_trade(trade)
            equity = executor.equity_rub(list(positions.values()), latest_bid_by_instrument)
            peak_equity = max(peak_equity, equity)
            max_drawdown_rub = max(max_drawdown_rub, peak_equity - equity)
            equity_curve.append(equity)
            continue

        entry_allowed, entry_reason = risk.entry_allowed_at(snapshot.at)
        if not entry_allowed:
            blocked[entry_reason] += 1
            equity = executor.equity_rub(list(positions.values()), latest_bid_by_instrument)
            peak_equity = max(peak_equity, equity)
            max_drawdown_rub = max(max_drawdown_rub, peak_equity - equity)
            equity_curve.append(equity)
            continue

        signal, block_reason, _ = strategy.diagnose_entry(snapshot, has_open_position=False)
        if signal is None:
            blocked[block_reason] += 1
            equity = executor.equity_rub(list(positions.values()), latest_bid_by_instrument)
            peak_equity = max(peak_equity, equity)
            max_drawdown_rub = max(max_drawdown_rub, peak_equity - equity)
            equity_curve.append(equity)
            continue
        if entry_filter is not None:
            filter_allowed, filter_reason = entry_filter(snapshot, signal)
            if not filter_allowed:
                filtered_signal_count += 1
                blocked[filter_reason or "entry_filter_blocked"] += 1
                equity = executor.equity_rub(list(positions.values()), latest_bid_by_instrument)
                peak_equity = max(peak_equity, equity)
                max_drawdown_rub = max(max_drawdown_rub, peak_equity - equity)
                equity_curve.append(equity)
                continue

        quantity_lots, planned_notional_rub, sizing_reason = executor.plan_entry(
            snapshot,
            open_positions=len(positions),
            max_open_positions=config.max_open_positions,
            default_quantity_lots=config.order_quantity_lots,
            max_position_notional_rub=config.max_position_notional_rub,
            position_sizing_mode=config.position_sizing_mode,
        )
        if quantity_lots <= 0:
            blocked[sizing_reason] += 1
            equity = executor.equity_rub(list(positions.values()), latest_bid_by_instrument)
            peak_equity = max(peak_equity, equity)
            max_drawdown_rub = max(max_drawdown_rub, peak_equity - equity)
            equity_curve.append(equity)
            continue

        can_open, reason = risk.can_open(
            snapshot,
            open_positions=len(positions),
            planned_notional_rub=planned_notional_rub,
        )
        if not can_open:
            blocked[reason] += 1
            equity = executor.equity_rub(list(positions.values()), latest_bid_by_instrument)
            peak_equity = max(peak_equity, equity)
            max_drawdown_rub = max(max_drawdown_rub, peak_equity - equity)
            equity_curve.append(equity)
            continue

        signals_detected += 1
        report = executor.execute_entry_sync(snapshot, quantity_lots)
        positions[snapshot.instrument.instrument_id] = Position(
            instrument=snapshot.instrument,
            side=signal.side,
            quantity_lots=quantity_lots,
            entry_price=report.fill_price,
            opened_at=report.filled_at,
            take_profit_bps=signal.take_profit_bps,
            stop_loss_bps=signal.stop_loss_bps,
            time_stop_seconds=signal.time_stop_seconds,
            entry_fee_rub=report.fee_rub,
            reason=signal.reason,
            metadata={"mode": "paper"},
        )
        equity = executor.equity_rub(list(positions.values()), latest_bid_by_instrument)
        peak_equity = max(peak_equity, equity)
        max_drawdown_rub = max(max_drawdown_rub, peak_equity - equity)
        equity_curve.append(equity)

    market_value = executor.market_value_rub(list(positions.values()), latest_bid_by_instrument)
    unrealized_pnl = executor.unrealized_pnl_rub(list(positions.values()), latest_bid_by_instrument)
    equity = executor.equity_rub(list(positions.values()), latest_bid_by_instrument)
    equity_delta = equity - executor.initial_cash_rub
    fees_total = sum((trade.fees_rub for trade in trades), start=Decimal("0"))
    turnover = sum(
        (
            (trade.entry_price + trade.exit_price)
            * Decimal(trade.instrument.lot_size)
            * Decimal(trade.quantity_lots)
            for trade in trades
        ),
        start=Decimal("0"),
    )
    gross_wins = sum((trade.net_pnl_rub for trade in trades if trade.net_pnl_rub > 0), start=Decimal("0"))
    gross_losses = sum((-trade.net_pnl_rub for trade in trades if trade.net_pnl_rub < 0), start=Decimal("0"))
    wins = sum(1 for trade in trades if trade.net_pnl_rub > 0)
    losses = sum(1 for trade in trades if trade.net_pnl_rub < 0)
    average_trade = (
        risk.realized_pnl_rub / Decimal(len(trades))
        if trades
        else Decimal("0")
    )
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else (Decimal("999") if gross_wins > 0 else Decimal("0"))
    expectancy_bps = (
        (risk.realized_pnl_rub / turnover) * Decimal("10000")
        if turnover > 0
        else Decimal("0")
    )
    score = equity_delta - (max_drawdown_rub * Decimal("0.60")) + (Decimal(len(trades)) * Decimal("2"))

    return {
        "parameters": {
            "max_spread_bps": str(config.max_spread_bps),
            "min_imbalance": str(config.min_imbalance),
            "min_impulse_bps": str(config.min_impulse_bps),
            "take_profit_bps": str(config.take_profit_bps),
            "stop_loss_bps": str(config.stop_loss_bps),
            "time_stop_seconds": config.time_stop_seconds,
            "min_expected_edge_bps": str(config.min_expected_edge_bps),
            "cooldown_seconds": config.cooldown_seconds,
        },
        "trade_count": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round((wins / len(trades) * 100), 2) if trades else 0.0,
        "signals_detected": signals_detected,
        "filtered_signal_count": filtered_signal_count,
        "net_pnl_rub": str(risk.realized_pnl_rub),
        "unrealized_pnl_rub": str(unrealized_pnl),
        "equity_delta_rub": str(equity_delta),
        "fees_rub": str(fees_total),
        "turnover_rub": str(turnover),
        "open_positions": len(positions),
        "blocked_top": dict(blocked.most_common(5)),
        "max_drawdown_rub": str(max_drawdown_rub),
        "profit_factor": str(profit_factor),
        "expectancy_bps": str(expectancy_bps),
        "average_trade_rub": str(average_trade),
        "score": str(score),
        "market_value_rub": str(market_value),
    }


def build_recommendation(
    *,
    top_result: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
    min_trades: int,
) -> dict[str, Any]:
    if top_result is None:
        return {"eligible": False, "reason": "no_top_result"}

    top_equity = Decimal(str(top_result["equity_delta_rub"]))
    top_drawdown = Decimal(str(top_result["max_drawdown_rub"]))
    top_profit_factor = Decimal(str(top_result["profit_factor"]))
    baseline_equity = Decimal(str((baseline or {}).get("equity_delta_rub", "0")))

    if top_result.get("is_current_config"):
        return {
            "eligible": False,
            "reason": "current_config_already_best",
            "candidate": top_result,
        }
    if int(top_result["trade_count"]) < min_trades:
        return {
            "eligible": False,
            "reason": "not_enough_trades",
            "candidate": top_result,
        }
    if top_equity <= 0:
        return {
            "eligible": False,
            "reason": "candidate_not_profitable",
            "candidate": top_result,
        }
    if top_profit_factor <= Decimal("1.05"):
        return {
            "eligible": False,
            "reason": "profit_factor_too_low",
            "candidate": top_result,
        }
    if top_drawdown > top_equity:
        return {
            "eligible": False,
            "reason": "drawdown_exceeds_profit",
            "candidate": top_result,
        }
    if top_equity <= baseline_equity:
        return {
            "eligible": False,
            "reason": "not_better_than_baseline",
            "candidate": top_result,
        }
    return {
        "eligible": True,
        "reason": "promote_candidate",
        "candidate": top_result,
        "delta_vs_baseline_rub": str(top_equity - baseline_equity),
    }


def maybe_write_report(runtime_dir: Path, payload: dict[str, Any], *, report_key: str, enabled: bool) -> None:
    if not enabled:
        return
    optimizer_dir = runtime_dir / "optimizer"
    optimizer_dir.mkdir(parents=True, exist_ok=True)
    report_path = optimizer_dir / f"optimize-{report_key}.json"
    latest_path = optimizer_dir / "latest.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    report_path.write_text(body, encoding="utf-8")
    latest_path.write_text(body, encoding="utf-8")
