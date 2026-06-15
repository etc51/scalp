from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(slots=True, frozen=True)
class CollectionGuardPolicy:
    min_trades: int
    min_trade_share_pct: Decimal
    min_signal_share_pct: Decimal


def evaluate_collection_guard(
    *,
    baseline_trade_count: Any,
    candidate_trade_count: Any,
    baseline_signals_detected: Any,
    candidate_signals_detected: Any,
    policy: CollectionGuardPolicy,
) -> dict[str, Any]:
    baseline_trades = max(0, _to_int(baseline_trade_count))
    candidate_trades = max(0, _to_int(candidate_trade_count))
    baseline_signals = max(0, _to_int(baseline_signals_detected))
    candidate_signals = max(0, _to_int(candidate_signals_detected))
    trade_share_pct = _share_pct(candidate_trades, baseline_trades)
    signal_share_pct = _share_pct(candidate_signals, baseline_signals)

    reasons: list[str] = []
    if candidate_trades < policy.min_trades:
        reasons.append("trade_count_below_floor")
    if trade_share_pct is not None and trade_share_pct < policy.min_trade_share_pct:
        reasons.append("trade_share_below_floor")
    if signal_share_pct is not None and signal_share_pct < policy.min_signal_share_pct:
        reasons.append("signal_share_below_floor")

    return {
        "passes": not reasons,
        "reasons": reasons,
        "policy": {
            "min_trades": policy.min_trades,
            "min_trade_share_pct": str(policy.min_trade_share_pct),
            "min_signal_share_pct": str(policy.min_signal_share_pct),
        },
        "baseline_trade_count": baseline_trades,
        "candidate_trade_count": candidate_trades,
        "trade_share_pct": _fmt_decimal(trade_share_pct),
        "baseline_signals_detected": baseline_signals,
        "candidate_signals_detected": candidate_signals,
        "signal_share_pct": _fmt_decimal(signal_share_pct),
    }


def _share_pct(candidate: int, baseline: int) -> Decimal | None:
    if baseline <= 0:
        return None
    return (Decimal(candidate) / Decimal(baseline) * Decimal("100")).quantize(Decimal("0.01"))


def _to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def _fmt_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)
