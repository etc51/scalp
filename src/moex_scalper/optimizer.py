from __future__ import annotations

import itertools
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .commission import CommissionModel
from .config import ScalperConfig
from .domain import ClosedTrade, Position
from .execution import PaperExecutor
from .market_history import load_snapshots, snapshot_path_for_date
from .risk import RiskManager
from .strategy import ModerateScalpingStrategy


def optimize_parameters(
    config: ScalperConfig,
    *,
    date_key: str | None,
    input_path: str | None,
    top_n: int,
    write_report: bool,
) -> dict[str, Any]:
    snapshot_file = Path(input_path) if input_path else snapshot_path_for_date(config.runtime_dir, resolve_date_key(config, date_key))
    snapshots = load_snapshots(snapshot_file)
    if not snapshots:
        payload = {
            "status": "no_data",
            "snapshot_file": str(snapshot_file),
            "snapshot_count": 0,
            "message": "No recorded market snapshots found for analysis.",
        }
        maybe_write_report(config.runtime_dir, payload, date_key=resolve_date_key(config, date_key), enabled=write_report)
        return payload

    candidates = build_candidate_configs(config)
    current_signature = parameter_signature(config)
    results = []
    baseline = None
    for candidate in candidates:
        result = simulate_candidate(candidate, snapshots)
        result["is_current_config"] = parameter_signature(candidate) == current_signature
        if result["is_current_config"]:
            baseline = result
        results.append(result)
    results.sort(
        key=lambda item: (
            Decimal(str(item["equity_delta_rub"])),
            Decimal(str(item["net_pnl_rub"])),
            int(item["trade_count"]),
        ),
        reverse=True,
    )

    payload = {
        "status": "ok",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "snapshot_file": str(snapshot_file),
        "snapshot_count": len(snapshots),
        "candidate_count": len(results),
        "top": results[: max(1, top_n)],
        "baseline": baseline,
    }
    maybe_write_report(config.runtime_dir, payload, date_key=resolve_date_key(config, date_key), enabled=write_report)
    return payload


def resolve_date_key(config: ScalperConfig, explicit: str | None) -> str:
    if explicit:
        return explicit
    return datetime.now(config.timezone).date().isoformat()


def build_candidate_configs(base: ScalperConfig) -> list[ScalperConfig]:
    candidates: list[ScalperConfig] = [base]
    variants: dict[str, list[Any]] = {
        "max_spread_bps": [Decimal("1.5"), Decimal("2.5"), Decimal("4.0"), Decimal("5.5")],
        "min_imbalance": [Decimal("0.50"), Decimal("0.55"), Decimal("0.60"), Decimal("0.66")],
        "min_impulse_bps": [Decimal("1.5"), Decimal("2.5"), Decimal("4.0")],
        "take_profit_bps": [Decimal("8"), Decimal("10"), Decimal("12"), Decimal("14")],
        "stop_loss_bps": [Decimal("6"), Decimal("8"), Decimal("10")],
        "time_stop_seconds": [4.0, 6.0, 8.0],
        "min_expected_edge_bps": [Decimal("6"), Decimal("8"), Decimal("10")],
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
            "stop_loss_bps": Decimal("6"),
            "time_stop_seconds": 4.0,
            "min_expected_edge_bps": Decimal("10"),
        },
        {
            "max_spread_bps": Decimal("2.5"),
            "min_imbalance": Decimal("0.58"),
            "min_impulse_bps": Decimal("2.5"),
            "take_profit_bps": Decimal("10"),
            "stop_loss_bps": Decimal("8"),
            "time_stop_seconds": 6.0,
            "min_expected_edge_bps": Decimal("8"),
        },
        {
            "max_spread_bps": Decimal("5.5"),
            "min_imbalance": Decimal("0.50"),
            "min_impulse_bps": Decimal("1.5"),
            "take_profit_bps": Decimal("8"),
            "stop_loss_bps": Decimal("6"),
            "time_stop_seconds": 8.0,
            "min_expected_edge_bps": Decimal("6"),
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
        ]
    )


def simulate_candidate(config: ScalperConfig, snapshots: list[Any]) -> dict[str, Any]:
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
            continue

        entry_allowed, entry_reason = risk.entry_allowed_at(snapshot.at)
        if not entry_allowed:
            blocked[entry_reason] += 1
            continue

        signal, block_reason, _ = strategy.diagnose_entry(snapshot, has_open_position=False)
        if signal is None:
            blocked[block_reason] += 1
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
            continue

        can_open, reason = risk.can_open(
            snapshot,
            open_positions=len(positions),
            planned_notional_rub=planned_notional_rub,
        )
        if not can_open:
            blocked[reason] += 1
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
    wins = sum(1 for trade in trades if trade.net_pnl_rub > 0)
    losses = sum(1 for trade in trades if trade.net_pnl_rub < 0)

    return {
        "parameters": {
            "max_spread_bps": str(config.max_spread_bps),
            "min_imbalance": str(config.min_imbalance),
            "min_impulse_bps": str(config.min_impulse_bps),
            "take_profit_bps": str(config.take_profit_bps),
            "stop_loss_bps": str(config.stop_loss_bps),
            "time_stop_seconds": config.time_stop_seconds,
            "min_expected_edge_bps": str(config.min_expected_edge_bps),
        },
        "trade_count": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round((wins / len(trades) * 100), 2) if trades else 0.0,
        "signals_detected": signals_detected,
        "net_pnl_rub": str(risk.realized_pnl_rub),
        "unrealized_pnl_rub": str(unrealized_pnl),
        "equity_delta_rub": str(equity_delta),
        "fees_rub": str(fees_total),
        "turnover_rub": str(turnover),
        "open_positions": len(positions),
        "blocked_top": dict(blocked.most_common(5)),
    }


def maybe_write_report(runtime_dir: Path, payload: dict[str, Any], *, date_key: str, enabled: bool) -> None:
    if not enabled:
        return
    optimizer_dir = runtime_dir / "optimizer"
    optimizer_dir.mkdir(parents=True, exist_ok=True)
    report_path = optimizer_dir / f"optimize-{date_key}.json"
    latest_path = optimizer_dir / "latest.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    report_path.write_text(body, encoding="utf-8")
    latest_path.write_text(body, encoding="utf-8")
