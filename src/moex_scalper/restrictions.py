from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import ScalperConfig, parse_bool


@dataclass(slots=True, frozen=True)
class EntryRestrictions:
    disabled_tickers: tuple[str, ...] = ()
    blocked_entry_hours: tuple[int, ...] = ()
    updated_at: str | None = None
    source: str | None = None


DEFAULT_COVERAGE_ALLOWED_BLOCK_REASONS = (
    "spread_too_wide",
    "imbalance_too_low",
    "impulse_too_small",
    "expected_edge_too_low",
)


def build_restrictions(
    config: ScalperConfig,
    *,
    apply: bool,
    write_report: bool,
) -> dict[str, Any]:
    runtime_dir = config.runtime_dir
    analysis_path = runtime_dir / "analysis" / "latest.json"
    optimizer_path = runtime_dir / "optimizer" / "latest.json"
    session_path = runtime_dir / "paper_session.json"

    analysis_payload = _load_json(analysis_path)
    optimizer_payload = _load_json(optimizer_path)
    session_payload = _load_json(session_path)
    current_active = load_active_restrictions(runtime_dir)

    enabled = parse_bool(_env_value("SCALPER_RESTRICTIONS_ENABLED", "1"), default=True)
    min_total_trades = int(_env_value("SCALPER_RESTRICTION_MIN_TRADES", "8"))
    min_bucket_trades = int(_env_value("SCALPER_RESTRICTION_MIN_BUCKET_TRADES", "2"))
    min_loss_rub = Decimal(_env_value("SCALPER_RESTRICTION_MIN_LOSS_RUB", "0"))
    max_tickers = int(_env_value("SCALPER_RESTRICTION_MAX_TICKERS", "1"))
    max_hours = int(_env_value("SCALPER_RESTRICTION_MAX_HOURS", "1"))
    block_hours_enabled = parse_bool(_env_value("SCALPER_RESTRICTION_BLOCK_HOURS", "1"), default=True)
    coverage_fallback_enabled = parse_bool(_env_value("SCALPER_RESTRICTION_USE_COVERAGE_FALLBACK", "1"), default=True)
    coverage_min_snapshot_count = int(_env_value("SCALPER_RESTRICTION_COVERAGE_MIN_SNAPSHOTS", "300"))
    coverage_max_ready_rate_pct = Decimal(_env_value("SCALPER_RESTRICTION_COVERAGE_MAX_READY_RATE_PCT", "0.10"))
    coverage_min_dominant_block_share_pct = Decimal(
        _env_value("SCALPER_RESTRICTION_COVERAGE_MIN_BLOCK_SHARE_PCT", "65")
    )
    coverage_allowed_reasons = tuple(
        _parse_csv(_env_value("SCALPER_RESTRICTION_COVERAGE_ALLOWED_BLOCK_REASONS"))
        or DEFAULT_COVERAGE_ALLOWED_BLOCK_REASONS
    )
    open_positions = len(list((session_payload or {}).get("positions", [])))
    total_trades = int(((analysis_payload or {}).get("summary") or {}).get("trade_count", 0))

    reasons: list[str] = []
    if config.mode != "paper":
        reasons.append("mode_not_paper")
    if not enabled:
        reasons.append("restrictions_disabled")
    if _entry_window_open(config):
        reasons.append("entry_window_open")
    if open_positions > 0:
        reasons.append("open_positions_present")

    analysis_reasons: list[str] = []
    if analysis_payload is None:
        analysis_reasons.append("missing_analysis_report")
    elif analysis_payload.get("status") != "ok":
        analysis_reasons.append(f"analysis_{analysis_payload.get('status', 'unknown')}")
    elif total_trades < min_total_trades:
        analysis_reasons.append("insufficient_trade_sample")

    coverage_reasons: list[str] = []
    if not coverage_fallback_enabled:
        coverage_reasons.append("coverage_fallback_disabled")
    elif optimizer_payload is None:
        coverage_reasons.append("missing_optimizer_report")
    elif optimizer_payload.get("status") != "ok":
        coverage_reasons.append(f"optimizer_{optimizer_payload.get('status', 'unknown')}")

    ticker_candidates: list[dict[str, Any]] = []
    hour_candidates: list[dict[str, Any]] = []
    candidate_source: str | None = None
    if not reasons and not analysis_reasons and analysis_payload is not None:
        candidate_source = "analysis"
        ticker_candidates = select_negative_buckets(
            ((analysis_payload.get("by_ticker") or {}).get("worst") or []),
            min_bucket_trades=min_bucket_trades,
            min_loss_rub=min_loss_rub,
        )[:max(0, max_tickers)]
        if block_hours_enabled:
            hour_candidates = select_negative_buckets(
                ((analysis_payload.get("by_hour") or {}).get("worst") or []),
                min_bucket_trades=min_bucket_trades,
                min_loss_rub=min_loss_rub,
            )[:max(0, max_hours)]
    elif not reasons and not coverage_reasons and optimizer_payload is not None:
        candidate_source = "optimizer_signal_coverage"
        ticker_candidates = select_coverage_buckets(
            (((optimizer_payload.get("signal_coverage") or {}).get("by_ticker") or {}).get("worst") or []),
            min_snapshot_count=coverage_min_snapshot_count,
            max_ready_rate_pct=coverage_max_ready_rate_pct,
            min_dominant_block_share_pct=coverage_min_dominant_block_share_pct,
            allowed_reasons=coverage_allowed_reasons,
        )[:max(0, max_tickers)]
        if block_hours_enabled:
            hour_candidates = select_coverage_buckets(
                (((optimizer_payload.get("signal_coverage") or {}).get("by_hour") or {}).get("worst") or []),
                min_snapshot_count=coverage_min_snapshot_count,
                max_ready_rate_pct=coverage_max_ready_rate_pct,
                min_dominant_block_share_pct=coverage_min_dominant_block_share_pct,
                allowed_reasons=coverage_allowed_reasons,
            )[:max(0, max_hours)]
    elif not reasons:
        reasons.extend(analysis_reasons)
        if coverage_fallback_enabled or not analysis_reasons:
            reasons.extend(coverage_reasons)

    proposed = EntryRestrictions(
        disabled_tickers=tuple(item["key"] for item in ticker_candidates),
        blocked_entry_hours=tuple(_hour_key_to_int(item["key"]) for item in hour_candidates),
        updated_at=datetime.utcnow().isoformat() + "Z",
        source=candidate_source,
    )
    current_signature = restrictions_signature(current_active)
    proposed_signature = restrictions_signature(proposed)
    has_current_restrictions = bool(current_active.disabled_tickers or current_active.blocked_entry_hours)
    has_proposed_restrictions = bool(proposed.disabled_tickers or proposed.blocked_entry_hours)
    if not reasons and not has_proposed_restrictions and not has_current_restrictions:
        reasons.append("no_negative_buckets" if candidate_source == "analysis" else "no_coverage_outliers")
    if not reasons and current_signature == proposed_signature:
        reasons.append("candidate_already_applied")

    applied = apply and not reasons
    active_after = proposed if applied else current_active
    payload = {
        "status": "ok",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": config.mode,
        "enabled": enabled,
        "apply_requested": apply,
        "applied": applied,
        "candidate_source": candidate_source,
        "decision": build_decision(apply=apply, applied=applied, reasons=reasons),
        "reasons": reasons,
        "open_positions": open_positions,
        "current_signature": current_signature,
        "proposed_signature": proposed_signature,
        "analysis": {
            "status": (analysis_payload or {}).get("status"),
            "assessment": (analysis_payload or {}).get("assessment"),
            "trade_count": total_trades,
            "window": (analysis_payload or {}).get("window"),
            "reasons": analysis_reasons,
        },
        "optimizer": {
            "status": (optimizer_payload or {}).get("status"),
            "snapshot_count": (optimizer_payload or {}).get("snapshot_count"),
            "raw_snapshot_count": (optimizer_payload or {}).get("raw_snapshot_count"),
            "recommendation_reason": (((optimizer_payload or {}).get("recommendation") or {}).get("reason")),
            "reasons": coverage_reasons,
        },
        "coverage_fallback": {
            "enabled": coverage_fallback_enabled,
            "min_snapshot_count": coverage_min_snapshot_count,
            "max_ready_rate_pct": str(coverage_max_ready_rate_pct),
            "min_dominant_block_share_pct": str(coverage_min_dominant_block_share_pct),
            "allowed_block_reasons": list(coverage_allowed_reasons),
        },
        "current_active": serialize_restrictions(current_active),
        "proposed_restrictions": serialize_restrictions(proposed),
        "active_restrictions": serialize_restrictions(active_after),
        "clears_existing_restrictions": bool(has_current_restrictions and not has_proposed_restrictions),
        "candidate_breakdown": {
            "tickers": ticker_candidates,
            "hours": hour_candidates,
        },
        "next_action": build_next_action(apply=apply, applied=applied, reasons=reasons),
        "service_restart_required": applied,
    }
    if write_report or applied:
        write_restrictions_report(runtime_dir, payload, applied=applied)
    return payload


def load_active_restrictions(runtime_dir: Path) -> EntryRestrictions:
    active_path = runtime_dir / "restrictions" / "active.json"
    if not active_path.exists():
        return EntryRestrictions()
    try:
        payload = json.loads(active_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return EntryRestrictions()
    return EntryRestrictions(
        disabled_tickers=tuple(str(item).upper() for item in list(payload.get("disabled_tickers", [])) if str(item).strip()),
        blocked_entry_hours=tuple(
            int(item)
            for item in list(payload.get("blocked_entry_hours", []))
            if str(item).strip() and str(item).isdigit()
        ),
        updated_at=str(payload.get("updated_at")) if payload.get("updated_at") else None,
        source=str(payload.get("source")) if payload.get("source") else None,
    )


def serialize_restrictions(restrictions: EntryRestrictions) -> dict[str, Any]:
    return {
        "disabled_tickers": list(restrictions.disabled_tickers),
        "blocked_entry_hours": list(restrictions.blocked_entry_hours),
        "updated_at": restrictions.updated_at,
        "source": restrictions.source,
    }


def restriction_reason(
    restrictions: EntryRestrictions,
    *,
    ticker: str,
    local_hour: int,
) -> str | None:
    if ticker.upper() in restrictions.disabled_tickers:
        return "restricted_ticker"
    if local_hour in restrictions.blocked_entry_hours:
        return "restricted_entry_hour"
    return None


def restrictions_signature(restrictions: EntryRestrictions) -> str:
    return (
        ",".join(sorted(restrictions.disabled_tickers))
        + "|"
        + ",".join(str(hour) for hour in sorted(restrictions.blocked_entry_hours))
    )


def select_negative_buckets(
    items: list[dict[str, Any]],
    *,
    min_bucket_trades: int,
    min_loss_rub: Decimal,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for item in items:
        trade_count = int(item.get("trade_count", 0))
        net_pnl_rub = Decimal(str(item.get("net_pnl_rub", "0")))
        if trade_count < min_bucket_trades:
            continue
        if net_pnl_rub >= -min_loss_rub:
            continue
        selected.append(item)
    return selected


def select_coverage_buckets(
    items: list[dict[str, Any]],
    *,
    min_snapshot_count: int,
    max_ready_rate_pct: Decimal,
    min_dominant_block_share_pct: Decimal,
    allowed_reasons: tuple[str, ...],
) -> list[dict[str, Any]]:
    allowed = {reason.strip().lower() for reason in allowed_reasons if reason.strip()}
    selected: list[dict[str, Any]] = []
    for item in items:
        snapshot_count = int(item.get("snapshot_count", 0))
        if snapshot_count < min_snapshot_count:
            continue
        ready_rate_pct = Decimal(str(item.get("signal_ready_rate_pct", "0")))
        if ready_rate_pct > max_ready_rate_pct:
            continue
        top_blocked = list(item.get("top_blocked_reasons") or [])
        if not top_blocked:
            continue
        dominant = dict(top_blocked[0])
        dominant_reason = str(dominant.get("reason", "")).strip().lower()
        dominant_count = int(dominant.get("count", 0))
        if dominant_reason not in allowed:
            continue
        dominant_share_pct = (
            Decimal(dominant_count) / Decimal(snapshot_count) * Decimal("100")
            if snapshot_count > 0
            else Decimal("0")
        )
        if dominant_share_pct < min_dominant_block_share_pct:
            continue
        candidate = dict(item)
        candidate["dominant_block_reason"] = dominant_reason
        candidate["dominant_block_share_pct"] = str(dominant_share_pct.quantize(Decimal("0.01")))
        selected.append(candidate)
    return selected


def write_restrictions_report(runtime_dir: Path, payload: dict[str, Any], *, applied: bool) -> None:
    restrictions_dir = runtime_dir / "restrictions"
    restrictions_dir.mkdir(parents=True, exist_ok=True)
    latest_path = restrictions_dir / "latest.json"
    history_path = restrictions_dir / "history.jsonl"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    if applied:
        active_path = restrictions_dir / "active.json"
        active_path.write_text(
            json.dumps(payload.get("active_restrictions", {}), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def build_decision(*, apply: bool, applied: bool, reasons: list[str]) -> str:
    if applied:
        return "applied"
    if apply and reasons:
        return "skipped"
    if not apply and not reasons:
        return "ready_to_apply"
    return "preview_skipped"


def build_next_action(*, apply: bool, applied: bool, reasons: list[str]) -> str:
    if applied:
        return "restart_paper_service"
    if "analysis_no_entry_window_data" in reasons:
        return "collect_in_window_market_data"
    if "optimizer_no_entry_window_data" in reasons:
        return "collect_in_window_market_data"
    if "insufficient_trade_sample" in reasons:
        return "collect_more_paper_trades"
    if "open_positions_present" in reasons:
        return "wait_for_positions_to_close"
    if "entry_window_open" in reasons:
        return "retry_outside_entry_window"
    if "no_negative_buckets" in reasons:
        return "no_restrictions_needed"
    if "no_coverage_outliers" in reasons:
        return "no_restrictions_needed"
    if not apply and not reasons:
        return "restriction_candidate_ready"
    return "no_change"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _env_value(key: str, default: str | None = None) -> str | None:
    return __import__("os").getenv(key, default)


def _entry_window_open(config: ScalperConfig) -> bool:
    local_now = datetime.now(config.timezone)
    if local_now.weekday() not in config.entry_weekdays:
        return False
    current_time = local_now.time().replace(tzinfo=None)
    return config.entry_start_time <= current_time <= config.entry_end_time


def _hour_key_to_int(value: str) -> int:
    return int(str(value).split(":", 1)[0])


def _parse_csv(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
