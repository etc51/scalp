from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import ClosedTrade, InstrumentSpec, Position, Side
from .risk import trading_day_key


def _decimal(value: str | int | float | Decimal | None, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _utc_iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def serialize_position(position: Position) -> dict[str, Any]:
    return {
        "instrument_id": position.instrument.instrument_id,
        "side": position.side.value,
        "quantity_lots": position.quantity_lots,
        "entry_price": str(position.entry_price),
        "opened_at": position.opened_at.isoformat(),
        "take_profit_bps": str(position.take_profit_bps),
        "stop_loss_bps": str(position.stop_loss_bps),
        "time_stop_seconds": position.time_stop_seconds,
        "entry_fee_rub": str(position.entry_fee_rub),
        "reason": position.reason,
        "metadata": dict(position.metadata),
    }


def deserialize_position(payload: dict[str, Any], instruments: dict[str, InstrumentSpec]) -> Position | None:
    instrument_id = str(payload.get("instrument_id", ""))
    instrument = instruments.get(instrument_id)
    if instrument is None:
        return None
    return Position(
        instrument=instrument,
        side=Side(str(payload.get("side", Side.BUY.value))),
        quantity_lots=int(payload.get("quantity_lots", 0)),
        entry_price=_decimal(payload.get("entry_price")),
        opened_at=datetime.fromisoformat(str(payload.get("opened_at"))),
        take_profit_bps=_decimal(payload.get("take_profit_bps")),
        stop_loss_bps=_decimal(payload.get("stop_loss_bps")),
        time_stop_seconds=float(payload.get("time_stop_seconds", 0)),
        entry_fee_rub=_decimal(payload.get("entry_fee_rub")),
        reason=str(payload.get("reason", "")),
        metadata={str(key): str(value) for key, value in dict(payload.get("metadata", {})).items()},
    )


def serialize_trade(trade: ClosedTrade) -> dict[str, Any]:
    return {
        "instrument_id": trade.instrument.instrument_id,
        "ticker": trade.instrument.ticker,
        "side": trade.side.value,
        "quantity_lots": trade.quantity_lots,
        "entry_price": str(trade.entry_price),
        "exit_price": str(trade.exit_price),
        "opened_at": trade.opened_at.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "gross_pnl_rub": str(trade.gross_pnl_rub),
        "fees_rub": str(trade.fees_rub),
        "net_pnl_rub": str(trade.net_pnl_rub),
        "entry_reason": trade.entry_reason,
        "exit_reason": trade.exit_reason,
        "hold_seconds": round((trade.closed_at - trade.opened_at).total_seconds(), 3),
    }


def deserialize_trade(payload: dict[str, Any], instruments: dict[str, InstrumentSpec]) -> ClosedTrade | None:
    instrument_id = str(payload.get("instrument_id", ""))
    instrument = instruments.get(instrument_id)
    if instrument is None:
        return None
    return ClosedTrade(
        instrument=instrument,
        side=Side(str(payload.get("side", Side.BUY.value))),
        quantity_lots=int(payload.get("quantity_lots", 0)),
        entry_price=_decimal(payload.get("entry_price")),
        exit_price=_decimal(payload.get("exit_price")),
        opened_at=datetime.fromisoformat(str(payload.get("opened_at"))),
        closed_at=datetime.fromisoformat(str(payload.get("closed_at"))),
        gross_pnl_rub=_decimal(payload.get("gross_pnl_rub")),
        fees_rub=_decimal(payload.get("fees_rub")),
        net_pnl_rub=_decimal(payload.get("net_pnl_rub")),
        entry_reason=str(payload.get("entry_reason", "")),
        exit_reason=str(payload.get("exit_reason", "")),
    )


def _empty_summary(scope: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "trade_count": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "flat_trades": 0,
        "gross_pnl_rub": "0",
        "net_pnl_rub": "0",
        "fees_rub": "0",
        "turnover_rub": "0",
        "hold_seconds_sum": 0.0,
        "average_hold_seconds": 0.0,
        "win_rate_pct": 0.0,
        "best_trade_rub": None,
        "worst_trade_rub": None,
        "last_trade_at": None,
        "last_ticker": None,
    }


class PaperRuntimeStore:
    def __init__(self, runtime_dir: Path, timezone_info: object) -> None:
        self.runtime_dir = runtime_dir
        self.timezone_info = timezone_info
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.runtime_dir / "paper_session.json"
        self.trades_path = self.runtime_dir / "paper_trades.jsonl"
        self.stats_dir = self.runtime_dir / "stats"
        self.daily_dir = self.stats_dir / "daily"
        self.overview_path = self.stats_dir / "overview.json"

    def load_session(self) -> dict[str, Any] | None:
        if not self.session_path.exists():
            return None
        try:
            return json.loads(self.session_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def save_session(
        self,
        *,
        cash_rub: Decimal,
        positions: list[Position],
        trades_today: list[ClosedTrade],
        current_day: str,
        realized_pnl_rub: Decimal,
        cooldown_until: dict[str, datetime],
        ticker_guard_cooldown_until: dict[str, datetime],
        ticker_guard_loss_anchor_rub: dict[str, Decimal],
        blocked_reasons: Counter[str],
        snapshots_processed: int,
        signal_candidates_detected: int,
        signals_detected: int,
        execution_blocked_signals: int,
        execution_blocked_reasons: Counter[str],
        recorded_market_snapshots_total: int,
        recorded_market_snapshots_today: int,
        skipped_market_snapshots_total: int,
        recorded_market_snapshot_day: str | None,
        last_recorded_market_snapshot_at: datetime | None,
    ) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cash_rub": str(cash_rub),
            "positions": [serialize_position(item) for item in positions],
            "trades_today": [serialize_trade(item) for item in trades_today],
            "risk": {
                "current_day": current_day,
                "realized_pnl_rub": str(realized_pnl_rub),
                "cooldown_until": {
                    instrument_id: moment.isoformat()
                    for instrument_id, moment in cooldown_until.items()
                },
                "ticker_guard_cooldown_until": {
                    ticker: moment.isoformat()
                    for ticker, moment in ticker_guard_cooldown_until.items()
                },
                "ticker_guard_loss_anchor_rub": {
                    ticker: str(value)
                    for ticker, value in ticker_guard_loss_anchor_rub.items()
                },
            },
            "blocked_reasons": dict(blocked_reasons),
            "snapshots_processed": snapshots_processed,
            "signal_candidates_detected": signal_candidates_detected,
            "signals_detected": signals_detected,
            "execution_blocked_signals": execution_blocked_signals,
            "execution_blocked_reasons": dict(execution_blocked_reasons),
            "market_history": {
                "recorded_snapshots_total": recorded_market_snapshots_total,
                "recorded_snapshots_today": recorded_market_snapshots_today,
                "skipped_snapshots_total": skipped_market_snapshots_total,
                "current_day": recorded_market_snapshot_day,
                "last_recorded_at": (
                    last_recorded_market_snapshot_at.isoformat()
                    if last_recorded_market_snapshot_at is not None
                    else None
                ),
            },
        }
        _atomic_write_json(self.session_path, payload)

    def append_trade(self, trade: ClosedTrade) -> None:
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trades_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(serialize_trade(trade), ensure_ascii=False) + "\n")
        day_key = trading_day_key(trade.closed_at, self.timezone_info)
        self._update_summary(self._daily_summary_path(day_key), trade, scope=day_key)
        self._update_summary(self.overview_path, trade, scope="all_time")

    def load_stats(self, current_day: str) -> dict[str, dict[str, Any]]:
        overview = self._read_summary(self.overview_path, scope="all_time")
        today = self._read_summary(self._daily_summary_path(current_day), scope=current_day)
        if not self.overview_path.exists():
            _atomic_write_json(self.overview_path, overview)
        daily_path = self._daily_summary_path(current_day)
        if not daily_path.exists():
            _atomic_write_json(daily_path, today)
        return {"overall": overview, "today": today}

    def seed_history_if_empty(self, trades: list[ClosedTrade]) -> bool:
        if not trades:
            return False
        overview = self._read_summary(self.overview_path, scope="all_time")
        has_trade_log = self.trades_path.exists() and self.trades_path.stat().st_size > 0
        if has_trade_log or int(overview.get("trade_count", 0)) > 0:
            return False
        for trade in trades:
            self.append_trade(trade)
        return True

    def _daily_summary_path(self, day_key: str) -> Path:
        return self.daily_dir / f"{day_key}.json"

    def _read_summary(self, path: Path, *, scope: str) -> dict[str, Any]:
        if not path.exists():
            return _empty_summary(scope)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return _empty_summary(scope)
        payload.setdefault("scope", scope)
        return payload

    def _update_summary(self, path: Path, trade: ClosedTrade, *, scope: str) -> None:
        summary = self._read_summary(path, scope=scope)
        trade_count = int(summary.get("trade_count", 0)) + 1
        net_pnl_rub = _decimal(summary.get("net_pnl_rub")) + trade.net_pnl_rub
        gross_pnl_rub = _decimal(summary.get("gross_pnl_rub")) + trade.gross_pnl_rub
        fees_rub = _decimal(summary.get("fees_rub")) + trade.fees_rub
        turnover_rub = _decimal(summary.get("turnover_rub")) + (
            (trade.entry_price + trade.exit_price)
            * Decimal(trade.instrument.lot_size)
            * Decimal(trade.quantity_lots)
        )
        hold_seconds_sum = float(summary.get("hold_seconds_sum", 0.0)) + (
            trade.closed_at - trade.opened_at
        ).total_seconds()
        best_trade = trade.net_pnl_rub
        worst_trade = trade.net_pnl_rub
        if summary.get("best_trade_rub") is not None:
            best_trade = max(best_trade, _decimal(summary.get("best_trade_rub")))
        if summary.get("worst_trade_rub") is not None:
            worst_trade = min(worst_trade, _decimal(summary.get("worst_trade_rub")))

        if trade.net_pnl_rub > 0:
            summary["winning_trades"] = int(summary.get("winning_trades", 0)) + 1
        elif trade.net_pnl_rub < 0:
            summary["losing_trades"] = int(summary.get("losing_trades", 0)) + 1
        else:
            summary["flat_trades"] = int(summary.get("flat_trades", 0)) + 1

        summary["scope"] = scope
        summary["trade_count"] = trade_count
        summary["gross_pnl_rub"] = str(gross_pnl_rub)
        summary["net_pnl_rub"] = str(net_pnl_rub)
        summary["fees_rub"] = str(fees_rub)
        summary["turnover_rub"] = str(turnover_rub)
        summary["hold_seconds_sum"] = hold_seconds_sum
        summary["average_hold_seconds"] = round(hold_seconds_sum / trade_count, 3) if trade_count else 0.0
        summary["win_rate_pct"] = round(
            (int(summary.get("winning_trades", 0)) / trade_count) * 100,
            2,
        ) if trade_count else 0.0
        summary["best_trade_rub"] = str(best_trade)
        summary["worst_trade_rub"] = str(worst_trade)
        summary["last_trade_at"] = trade.closed_at.isoformat()
        summary["last_ticker"] = trade.instrument.ticker
        _atomic_write_json(path, summary)


def restore_runtime_entities(
    payload: dict[str, Any],
    *,
    instruments: dict[str, InstrumentSpec],
    timezone_info: object,
) -> dict[str, Any]:
    positions = [
        position
        for position in (
            deserialize_position(item, instruments)
            for item in list(payload.get("positions", []))
        )
        if position is not None
    ]
    trades_today = [
        trade
        for trade in (
            deserialize_trade(item, instruments)
            for item in list(payload.get("trades_today", []))
        )
        if trade is not None
    ]
    risk_payload = dict(payload.get("risk", {}))
    market_history_payload = dict(payload.get("market_history", {}))
    cooldown_until = {
        instrument_id: restored
        for instrument_id, restored in (
            (
                str(instrument_id),
                _utc_iso_to_datetime(str(moment)),
            )
            for instrument_id, moment in dict(risk_payload.get("cooldown_until", {})).items()
        )
        if restored is not None
    }
    ticker_guard_cooldown_until = {
        str(ticker).upper(): restored
        for ticker, restored in (
            (
                str(ticker),
                _utc_iso_to_datetime(str(moment)),
            )
            for ticker, moment in dict(risk_payload.get("ticker_guard_cooldown_until", {})).items()
        )
        if restored is not None
    }
    ticker_guard_loss_anchor_rub = {
        str(ticker).upper(): _decimal(value)
        for ticker, value in dict(risk_payload.get("ticker_guard_loss_anchor_rub", {})).items()
    }
    restored_signals_detected = int(payload.get("signals_detected", 0))
    restored_signal_candidates = payload.get("signal_candidates_detected")
    return {
        "cash_rub": _decimal(payload.get("cash_rub"), default="0"),
        "positions": positions,
        "trades_today": trades_today,
        "risk_current_day": str(
            risk_payload.get("current_day", trading_day_key(datetime.now(timezone.utc), timezone_info))
        ),
        "risk_realized_pnl_rub": _decimal(risk_payload.get("realized_pnl_rub")),
        "cooldown_until": cooldown_until,
        "ticker_guard_cooldown_until": ticker_guard_cooldown_until,
        "ticker_guard_loss_anchor_rub": ticker_guard_loss_anchor_rub,
        "blocked_reasons": Counter(dict(payload.get("blocked_reasons", {}))),
        "snapshots_processed": int(payload.get("snapshots_processed", 0)),
        "signal_candidates_detected": (
            restored_signals_detected
            if restored_signal_candidates is None
            else int(restored_signal_candidates)
        ),
        "signals_detected": restored_signals_detected,
        "execution_blocked_signals": int(payload.get("execution_blocked_signals", 0)),
        "execution_blocked_reasons": Counter(dict(payload.get("execution_blocked_reasons", {}))),
        "recorded_market_snapshots_total": int(market_history_payload.get("recorded_snapshots_total", 0)),
        "recorded_market_snapshots_today": int(market_history_payload.get("recorded_snapshots_today", 0)),
        "skipped_market_snapshots_total": int(market_history_payload.get("skipped_snapshots_total", 0)),
        "recorded_market_snapshot_day": (
            str(market_history_payload.get("current_day"))
            if market_history_payload.get("current_day") is not None
            else None
        ),
        "last_recorded_market_snapshot_at": _utc_iso_to_datetime(
            market_history_payload.get("last_recorded_at")
        ),
    }
