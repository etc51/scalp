from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .collection_guard import CollectionGuardPolicy, evaluate_collection_guard
from .config import ScalperConfig, parse_bool
from .diagnostics import build_strategy_diagnostics, get_recommended_take_profit_bps


PARAMETER_ENV_MAP: dict[str, str] = {
    "max_spread_bps": "SCALPER_MAX_SPREAD_BPS",
    "min_imbalance": "SCALPER_MIN_IMBALANCE",
    "min_impulse_bps": "SCALPER_MIN_IMPULSE_BPS",
    "take_profit_bps": "SCALPER_TAKE_PROFIT_BPS",
    "stop_loss_bps": "SCALPER_STOP_LOSS_BPS",
    "time_stop_seconds": "SCALPER_TIME_STOP_SECONDS",
    "min_expected_edge_bps": "SCALPER_MIN_EXPECTED_EDGE_BPS",
    "min_net_take_profit_bps": "SCALPER_MIN_NET_TAKE_PROFIT_BPS",
    "cooldown_seconds": "SCALPER_COOLDOWN_SECONDS",
    "paper_ticker_guard_cooldown_seconds": "SCALPER_PAPER_TICKER_GUARD_COOLDOWN_SECONDS",
}
REGIME_FILTER_ENV_KEY = "SCALPER_REGIME_FILTER_MODE"
STRATEGY_OVERLAY_ENV_KEY = "SCALPER_STRATEGY_OVERLAY_MODE"
ALLOW_SHORT_ENV_KEY = "SCALPER_ALLOW_SHORT"
EXPECTED_EDGE_STEPS = (Decimal("4"), Decimal("6"), Decimal("8"), Decimal("10"), Decimal("12"), Decimal("14"))
IMPULSE_STEPS = (Decimal("1.0"), Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))
IMBALANCE_STEPS = (Decimal("0.45"), Decimal("0.50"), Decimal("0.55"), Decimal("0.60"), Decimal("0.66"))
DEFAULT_COVERAGE_ALLOWED_BLOCK_REASONS = (
    "expected_edge_too_low",
    "impulse_too_small",
    "imbalance_too_low",
)


def tune_parameters(
    config: ScalperConfig,
    *,
    apply: bool,
    write_report: bool,
    env_path: str = ".env",
) -> dict[str, Any]:
    env_file = Path(env_path)
    analysis_path = config.runtime_dir / "analysis" / "latest.json"
    optimizer_path = config.runtime_dir / "optimizer" / "latest.json"
    research_path = config.runtime_dir / "research" / "latest.json"
    session_path = config.runtime_dir / "paper_session.json"

    analysis_payload = _load_json(analysis_path)
    optimizer_payload = _load_json(optimizer_path)
    research_payload = _load_json(research_path)
    session_payload = _load_json(session_path)
    strategy_diagnostics = build_strategy_diagnostics(config)

    current_parameters = current_strategy_parameters(config)
    current_signature = parameter_signature(current_parameters)
    recommendation = dict((optimizer_payload or {}).get("recommendation") or {})
    candidate = dict(recommendation.get("candidate") or {})
    optimizer_candidate_parameters = normalize_parameters(dict(candidate.get("parameters") or {}))
    optimizer_candidate_source = "optimizer"
    optimizer_headroom_adjusted = False
    if optimizer_candidate_parameters:
        optimizer_candidate_parameters, optimizer_headroom_adjusted = enforce_take_profit_headroom(
            optimizer_candidate_parameters,
            config,
        )
        if optimizer_headroom_adjusted:
            optimizer_candidate_source = "optimizer_headroom_guard"
    optimizer_candidate_signature = (
        parameter_signature(optimizer_candidate_parameters)
        if optimizer_candidate_parameters
        else None
    )
    headroom_candidate_parameters = build_headroom_guard_candidate(
        config,
        current_parameters=current_parameters,
        strategy_diagnostics=strategy_diagnostics,
    )
    headroom_candidate_signature = (
        parameter_signature(headroom_candidate_parameters)
        if headroom_candidate_parameters
        else None
    )
    research_recommendation = dict((((research_payload or {}).get("regime_replay") or {}).get("recommendation") or {}))
    research_candidate = dict(research_recommendation.get("candidate") or {})
    research_candidate_allow_short = research_candidate.get("allow_short")
    regime_baseline = dict((((research_payload or {}).get("regime_replay") or {}).get("baseline")) or {})
    strategy_lab_recommendation = dict((((research_payload or {}).get("strategy_lab") or {}).get("recommendation") or {}))
    strategy_lab_candidate = dict(strategy_lab_recommendation.get("candidate") or {})
    strategy_lab_candidate_allow_short = strategy_lab_candidate.get("allow_short")
    strategy_lab_baseline = dict((((research_payload or {}).get("strategy_lab") or {}).get("baseline")) or {})
    collection_guard_policy = load_collection_guard_policy()

    enabled = parse_bool(_env_value("SCALPER_AUTO_TUNE_ENABLED"), default=True)
    regime_apply_enabled = parse_bool(_env_value("SCALPER_AUTO_APPLY_REGIME_FILTER", "1"), default=True)
    strategy_overlay_apply_enabled = parse_bool(
        _env_value("SCALPER_AUTO_APPLY_STRATEGY_OVERLAY", "1"),
        default=True,
    )
    coverage_fallback_enabled = parse_bool(_env_value("SCALPER_AUTO_TUNE_USE_COVERAGE_FALLBACK", "1"), default=True)
    min_trades = int(_env_value("SCALPER_AUTO_TUNE_MIN_TRADES", "8"))
    min_delta_rub = Decimal(_env_value("SCALPER_AUTO_TUNE_MIN_DELTA_RUB", "0"))
    min_regime_delta_rub = Decimal(_env_value("SCALPER_AUTO_TUNE_MIN_REGIME_DELTA_RUB", "0"))
    min_strategy_overlay_trades = int(
        _env_value("SCALPER_AUTO_TUNE_MIN_STRATEGY_OVERLAY_TRADES", str(min_trades))
    )
    min_strategy_overlay_delta_rub = Decimal(
        _env_value("SCALPER_AUTO_TUNE_MIN_STRATEGY_OVERLAY_DELTA_RUB", "0")
    )
    coverage_min_snapshots = int(_env_value("SCALPER_AUTO_TUNE_COVERAGE_MIN_SNAPSHOTS", "500"))
    coverage_max_ready_rate_pct = Decimal(_env_value("SCALPER_AUTO_TUNE_COVERAGE_MAX_READY_RATE_PCT", "0.10"))
    coverage_min_block_share_pct = Decimal(
        _env_value("SCALPER_AUTO_TUNE_COVERAGE_MIN_BLOCK_SHARE_PCT", "60")
    )
    coverage_allowed_reasons = tuple(
        _parse_csv(_env_value("SCALPER_AUTO_TUNE_COVERAGE_ALLOWED_BLOCK_REASONS"))
        or DEFAULT_COVERAGE_ALLOWED_BLOCK_REASONS
    )
    open_positions = len(list((session_payload or {}).get("positions", [])))
    analysis_trade_count = int(((analysis_payload or {}).get("summary") or {}).get("trade_count", 0))
    delta_vs_baseline_rub = Decimal(str(recommendation.get("delta_vs_baseline_rub", "0")))
    regime_delta_vs_baseline_rub = Decimal(str(research_candidate.get("delta_vs_baseline_rub", "0")))
    strategy_overlay_delta_vs_baseline_rub = Decimal(
        str(strategy_lab_candidate.get("delta_vs_baseline_rub", "0"))
    )
    analysis_status = str((analysis_payload or {}).get("status") or "missing")

    coverage_candidate_parameters, coverage_fallback = build_coverage_unblocker_candidate(
        config,
        current_parameters=current_parameters,
        analysis_status=analysis_status,
        analysis_trade_count=analysis_trade_count,
        min_trades=min_trades,
        optimizer_payload=optimizer_payload,
        enabled=coverage_fallback_enabled,
        min_snapshot_count=coverage_min_snapshots,
        max_ready_rate_pct=coverage_max_ready_rate_pct,
        min_dominant_block_share_pct=coverage_min_block_share_pct,
        allowed_block_reasons=coverage_allowed_reasons,
    )
    coverage_candidate_signature = (
        parameter_signature(coverage_candidate_parameters)
        if coverage_candidate_parameters
        else None
    )

    common_reasons: list[str] = []
    if config.mode != "paper":
        common_reasons.append("mode_not_paper")
    if not enabled:
        common_reasons.append("autotune_disabled")
    if _entry_window_open(config):
        common_reasons.append("entry_window_open")
    if open_positions > 0:
        common_reasons.append("open_positions_present")
    if apply and not env_file.exists():
        common_reasons.append("missing_env_file")

    optimizer_reasons: list[str] = []
    if analysis_payload is None:
        optimizer_reasons.append("missing_analysis_report")
    elif analysis_payload.get("status") != "ok":
        optimizer_reasons.append(f"analysis_{analysis_payload.get('status', 'unknown')}")
    elif analysis_trade_count < min_trades:
        optimizer_reasons.append("insufficient_trade_sample")
    if optimizer_payload is None:
        optimizer_reasons.append("missing_optimizer_report")
    elif optimizer_payload.get("status") != "ok":
        optimizer_reasons.append(f"optimizer_{optimizer_payload.get('status', 'unknown')}")
    elif not recommendation.get("eligible", False):
        optimizer_reasons.append(f"optimizer_{recommendation.get('reason', 'not_eligible')}")
    if not optimizer_candidate_parameters:
        optimizer_reasons.append("missing_candidate_parameters")
    elif optimizer_candidate_signature == current_signature:
        optimizer_reasons.append("candidate_already_applied")
    if delta_vs_baseline_rub < min_delta_rub:
        optimizer_reasons.append("delta_below_threshold")
    optimizer_collection_guard = None
    if not optimizer_reasons and optimizer_candidate_parameters:
        optimizer_baseline = dict((optimizer_payload or {}).get("baseline") or {})
        optimizer_collection_guard = evaluate_collection_guard(
            baseline_trade_count=optimizer_baseline.get("trade_count"),
            candidate_trade_count=candidate.get("trade_count"),
            baseline_signals_detected=optimizer_baseline.get("signals_detected"),
            candidate_signals_detected=candidate.get("signals_detected"),
            policy=collection_guard_policy,
        )
        if not optimizer_collection_guard.get("passes", False):
            optimizer_reasons.append("optimizer_collection_guard_blocked")

    selected_candidate_parameters: dict[str, str] | None = None
    candidate_source: str | None = None
    if not optimizer_reasons and optimizer_candidate_parameters:
        selected_candidate_parameters = optimizer_candidate_parameters
        candidate_source = optimizer_candidate_source
    elif headroom_candidate_parameters:
        selected_candidate_parameters = headroom_candidate_parameters
        candidate_source = "headroom_guard"
    elif coverage_candidate_parameters:
        selected_candidate_parameters = coverage_candidate_parameters
        candidate_source = "coverage_unblocker"

    regime_reasons: list[str] = []
    selected_regime_filter_mode: str | None = None
    selected_regime_source: str | None = None
    selected_strategy_overlay_mode: str | None = None
    selected_allow_short: bool | None = None
    regime_collection_guard = None
    if not regime_apply_enabled:
        regime_reasons.append("regime_autotune_disabled")
    if research_payload is None:
        regime_reasons.append("missing_research_report")
    elif research_payload.get("status") != "ok":
        regime_reasons.append(f"research_{research_payload.get('status', 'unknown')}")
    elif not research_recommendation.get("eligible", False):
        regime_reasons.append(f"research_{research_recommendation.get('reason', 'not_eligible')}")
    regime_candidate_mode = str(research_candidate.get("mode") or "").strip()
    regime_candidate_allow_short = (
        None
        if research_candidate_allow_short is None
        else bool(research_candidate_allow_short)
    )
    if not regime_reasons:
        if not regime_candidate_mode:
            regime_reasons.append("missing_regime_candidate_mode")
        elif regime_delta_vs_baseline_rub < min_regime_delta_rub:
            regime_reasons.append("regime_delta_below_threshold")
        else:
            regime_collection_guard = evaluate_collection_guard(
                baseline_trade_count=regime_baseline.get("trade_count"),
                candidate_trade_count=research_candidate.get("trade_count"),
                baseline_signals_detected=regime_baseline.get("signals_detected"),
                candidate_signals_detected=research_candidate.get("signals_detected"),
                policy=collection_guard_policy,
            )
            if not regime_collection_guard.get("passes", False):
                regime_reasons.append("regime_collection_guard_blocked")
            else:
                mode_would_change = regime_candidate_mode != config.regime_filter_mode
                allow_short_would_change = (
                    regime_candidate_allow_short is not None
                    and regime_candidate_allow_short != config.allow_short
                )
                if not mode_would_change and not allow_short_would_change:
                    regime_reasons.append("research_candidate_already_applied")
                else:
                    if mode_would_change:
                        selected_regime_filter_mode = regime_candidate_mode
                        selected_regime_source = "research_regime"
                    if allow_short_would_change:
                        selected_allow_short = regime_candidate_allow_short

    strategy_overlay_reasons: list[str] = []
    strategy_overlay_collection_guard = None
    overlay_candidate_mode = str(
        strategy_lab_candidate.get("strategy_overlay_mode")
        or strategy_lab_candidate.get("overlay_mode")
        or ""
    ).strip()
    overlay_candidate_allow_short = (
        None
        if strategy_lab_candidate_allow_short is None
        else bool(strategy_lab_candidate_allow_short)
    )
    overlay_candidate_trade_count = int(strategy_lab_candidate.get("trade_count", 0) or 0)
    if not strategy_overlay_apply_enabled:
        strategy_overlay_reasons.append("strategy_overlay_autotune_disabled")
    if research_payload is None:
        strategy_overlay_reasons.append("missing_research_report")
    elif research_payload.get("status") != "ok":
        strategy_overlay_reasons.append(f"research_{research_payload.get('status', 'unknown')}")
    elif not strategy_lab_recommendation.get("eligible", False):
        strategy_overlay_reasons.append(
            f"strategy_lab_{strategy_lab_recommendation.get('reason', 'not_eligible')}"
        )
    if not strategy_overlay_reasons:
        if not overlay_candidate_mode or overlay_candidate_mode == "baseline":
            strategy_overlay_reasons.append("missing_strategy_overlay_candidate_mode")
        elif overlay_candidate_trade_count < min_strategy_overlay_trades:
            strategy_overlay_reasons.append("strategy_overlay_insufficient_trade_sample")
        elif strategy_overlay_delta_vs_baseline_rub < min_strategy_overlay_delta_rub:
            strategy_overlay_reasons.append("strategy_overlay_delta_below_threshold")
        else:
            strategy_overlay_collection_guard = evaluate_collection_guard(
                baseline_trade_count=strategy_lab_baseline.get("trade_count"),
                candidate_trade_count=strategy_lab_candidate.get("trade_count"),
                baseline_signals_detected=strategy_lab_baseline.get("signals_detected"),
                candidate_signals_detected=strategy_lab_candidate.get("signals_detected"),
                policy=collection_guard_policy,
            )
            if not strategy_overlay_collection_guard.get("passes", False):
                strategy_overlay_reasons.append("strategy_overlay_collection_guard_blocked")
            else:
                overlay_would_change = overlay_candidate_mode != config.strategy_overlay_mode
                allow_short_would_change = (
                    overlay_candidate_allow_short is not None
                    and overlay_candidate_allow_short != config.allow_short
                )
                regime_reset_would_change = config.regime_filter_mode != "off"
                if not overlay_would_change and not allow_short_would_change and not regime_reset_would_change:
                    strategy_overlay_reasons.append("strategy_overlay_candidate_already_applied")
                else:
                    if overlay_would_change:
                        selected_strategy_overlay_mode = overlay_candidate_mode
                    if allow_short_would_change:
                        selected_allow_short = overlay_candidate_allow_short
                    if regime_reset_would_change:
                        selected_regime_filter_mode = "off"
                        selected_regime_source = "research_strategy_overlay"
                    regime_reasons.append("strategy_overlay_preferred")

    env_updates: dict[str, str] = {}
    candidate_sources: list[str] = []
    candidate_signature = None
    if selected_candidate_parameters is not None:
        env_updates.update(parameter_env_updates(selected_candidate_parameters))
        candidate_sources.append(candidate_source or "parameter_candidate")
        candidate_signature = parameter_signature(selected_candidate_parameters)
    if selected_regime_filter_mode is not None:
        env_updates[REGIME_FILTER_ENV_KEY] = selected_regime_filter_mode
        candidate_sources.append(selected_regime_source or "research_regime")
    if selected_strategy_overlay_mode is not None:
        env_updates[STRATEGY_OVERLAY_ENV_KEY] = selected_strategy_overlay_mode
        candidate_sources.append("research_strategy_overlay")
    if selected_allow_short is not None:
        env_updates[ALLOW_SHORT_ENV_KEY] = "1" if selected_allow_short else "0"
        candidate_sources.append("research_entry_mode")

    reasons = list(common_reasons)
    if not env_updates:
        reasons.extend(optimizer_reasons)
        reasons.extend(regime_reasons)
        reasons.extend(strategy_overlay_reasons)

    changed_keys = changed_env_keys(config, env_updates)
    if env_updates and not changed_keys:
        reasons.append("candidate_already_applied")

    applied = apply and not reasons
    updated_parameters = dict(current_parameters)
    updated_regime_filter_mode = config.regime_filter_mode
    updated_strategy_overlay_mode = config.strategy_overlay_mode
    updated_allow_short = config.allow_short
    if applied:
        if selected_candidate_parameters is not None:
            updated_parameters = normalize_parameters(selected_candidate_parameters)
        if selected_regime_filter_mode is not None:
            updated_regime_filter_mode = selected_regime_filter_mode
        if selected_strategy_overlay_mode is not None:
            updated_strategy_overlay_mode = selected_strategy_overlay_mode
        if selected_allow_short is not None:
            updated_allow_short = selected_allow_short
        update_env_file(env_file, env_updates)

    payload = {
        "status": "ok",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": config.mode,
        "enabled": enabled,
        "apply_requested": apply,
        "applied": applied,
        "decision": build_decision(apply=apply, applied=applied, reasons=reasons),
        "reasons": reasons,
        "env_file": str(env_file),
        "open_positions": open_positions,
        "current_signature": current_signature,
        "candidate_signature": candidate_signature,
        "candidate_source": "+".join(dict.fromkeys(candidate_sources)) if candidate_sources else None,
        "candidate_env_updates": env_updates or None,
        "current_parameters": current_parameters,
        "candidate_parameters": normalize_parameters(selected_candidate_parameters) if selected_candidate_parameters else None,
        "parameters_after": updated_parameters,
        "changed_keys": changed_keys,
        "current_regime_filter_mode": config.regime_filter_mode,
        "candidate_regime_filter_mode": selected_regime_filter_mode,
        "regime_filter_mode_after": updated_regime_filter_mode,
        "current_strategy_overlay_mode": config.strategy_overlay_mode,
        "candidate_strategy_overlay_mode": selected_strategy_overlay_mode,
        "strategy_overlay_mode_after": updated_strategy_overlay_mode,
        "current_allow_short": config.allow_short,
        "candidate_allow_short": selected_allow_short,
        "allow_short_after": updated_allow_short,
        "strategy_diagnostics": strategy_diagnostics,
        "headroom_guard": {
            "needed": not bool(strategy_diagnostics.get("target_headroom_met", True)),
            "target_net_take_profit_buffer_bps": str(config.target_net_take_profit_buffer_bps),
            "recommended_take_profit_bps": str(get_recommended_take_profit_bps(config)),
            "candidate_signature": headroom_candidate_signature,
            "candidate_parameters": headroom_candidate_parameters,
        },
        "collection_guard": {
            "policy": {
                "min_trades": collection_guard_policy.min_trades,
                "min_trade_share_pct": str(collection_guard_policy.min_trade_share_pct),
                "min_signal_share_pct": str(collection_guard_policy.min_signal_share_pct),
            },
            "optimizer": optimizer_collection_guard,
            "regime": regime_collection_guard,
            "strategy_overlay": strategy_overlay_collection_guard,
        },
        "coverage_fallback": {
            **coverage_fallback,
            "candidate_signature": coverage_candidate_signature,
            "candidate_parameters": coverage_candidate_parameters,
        },
        "analysis": {
            "status": (analysis_payload or {}).get("status"),
            "assessment": (analysis_payload or {}).get("assessment"),
            "trade_count": analysis_trade_count,
            "net_pnl_rub": ((analysis_payload or {}).get("summary") or {}).get("net_pnl_rub"),
            "profit_factor": ((analysis_payload or {}).get("summary") or {}).get("profit_factor"),
            "window": (analysis_payload or {}).get("window"),
        },
        "optimizer": {
            "status": (optimizer_payload or {}).get("status"),
            "reason": recommendation.get("reason"),
            "eligible": recommendation.get("eligible", False),
            "trade_count": candidate.get("trade_count"),
            "equity_delta_rub": candidate.get("equity_delta_rub"),
            "profit_factor": candidate.get("profit_factor"),
            "delta_vs_baseline_rub": str(delta_vs_baseline_rub),
            "candidate_source": optimizer_candidate_source if optimizer_candidate_parameters else None,
            "headroom_adjusted": optimizer_headroom_adjusted,
            "candidate_signature": optimizer_candidate_signature,
        },
        "research": {
            "status": (research_payload or {}).get("status"),
            "recommendation_reason": research_recommendation.get("reason"),
            "eligible": research_recommendation.get("eligible", False),
            "candidate_name": research_candidate.get("name"),
            "candidate_mode": regime_candidate_mode or None,
            "candidate_allow_short": regime_candidate_allow_short,
            "candidate_entry_modes": research_candidate.get("entry_modes"),
            "candidate_trade_count": research_candidate.get("trade_count"),
            "delta_vs_baseline_rub": str(regime_delta_vs_baseline_rub),
            "apply_enabled": regime_apply_enabled,
            "strategy_overlay_recommendation_reason": strategy_lab_recommendation.get("reason"),
            "strategy_overlay_eligible": strategy_lab_recommendation.get("eligible", False),
            "strategy_overlay_candidate_name": strategy_lab_candidate.get("name"),
            "strategy_overlay_candidate_mode": overlay_candidate_mode or None,
            "strategy_overlay_candidate_allow_short": overlay_candidate_allow_short,
            "strategy_overlay_candidate_entry_modes": strategy_lab_candidate.get("entry_modes"),
            "strategy_overlay_candidate_trade_count": overlay_candidate_trade_count,
            "strategy_overlay_delta_vs_baseline_rub": str(strategy_overlay_delta_vs_baseline_rub),
            "strategy_overlay_apply_enabled": strategy_overlay_apply_enabled,
        },
        "next_action": build_next_action(apply=apply, applied=applied, reasons=reasons),
        "service_restart_required": applied,
    }
    if write_report or applied:
        write_tuning_report(config.runtime_dir, payload)
    return payload


def current_strategy_parameters(config: ScalperConfig) -> dict[str, str]:
    return normalize_parameters(
        {
            "max_spread_bps": config.max_spread_bps,
            "min_imbalance": config.min_imbalance,
            "min_impulse_bps": config.min_impulse_bps,
            "take_profit_bps": config.take_profit_bps,
            "stop_loss_bps": config.stop_loss_bps,
            "time_stop_seconds": config.time_stop_seconds,
            "min_expected_edge_bps": config.min_expected_edge_bps,
            "min_net_take_profit_bps": config.min_net_take_profit_bps,
            "cooldown_seconds": config.cooldown_seconds,
            "paper_ticker_guard_cooldown_seconds": config.paper_ticker_guard_cooldown_seconds,
        }
    )


def normalize_parameters(parameters: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key in PARAMETER_ENV_MAP:
        if key in parameters:
            normalized[key] = str(parameters[key])
    return normalized


def parameter_signature(parameters: dict[str, Any]) -> str:
    normalized = normalize_parameters(parameters)
    return "|".join(normalized.get(key, "") for key in PARAMETER_ENV_MAP)


def changed_parameter_keys(current_parameters: dict[str, Any], candidate_parameters: dict[str, Any]) -> list[str]:
    current = normalize_parameters(current_parameters)
    candidate = normalize_parameters(candidate_parameters)
    return [
        key
        for key in PARAMETER_ENV_MAP
        if key in candidate and current.get(key) != candidate.get(key)
    ]


def parameter_env_updates(parameters: dict[str, Any]) -> dict[str, str]:
    normalized = normalize_parameters(parameters)
    return {
        env_key: normalized[param_key]
        for param_key, env_key in PARAMETER_ENV_MAP.items()
        if param_key in normalized
    }


def changed_env_keys(config: ScalperConfig, env_updates: dict[str, str]) -> list[str]:
    current_values = current_env_values(config)
    reverse_map = {env_key: key for key, env_key in PARAMETER_ENV_MAP.items()}
    changed: list[str] = []
    for env_key, value in env_updates.items():
        if current_values.get(env_key) == value:
            continue
        changed.append(reverse_map.get(env_key, env_key.lower().removeprefix("scalper_")))
    return changed


def current_env_values(config: ScalperConfig) -> dict[str, str]:
    current = parameter_env_updates(current_strategy_parameters(config))
    current[REGIME_FILTER_ENV_KEY] = config.regime_filter_mode
    current[STRATEGY_OVERLAY_ENV_KEY] = config.strategy_overlay_mode
    current[ALLOW_SHORT_ENV_KEY] = "1" if config.allow_short else "0"
    return current


def build_headroom_guard_candidate(
    config: ScalperConfig,
    *,
    current_parameters: dict[str, Any],
    strategy_diagnostics: dict[str, Any],
) -> dict[str, str] | None:
    if bool(strategy_diagnostics.get("target_headroom_met", True)):
        return None
    candidate = normalize_parameters(current_parameters)
    candidate["take_profit_bps"] = str(get_recommended_take_profit_bps(config))
    if parameter_signature(candidate) == parameter_signature(current_parameters):
        return None
    return candidate


def enforce_take_profit_headroom(
    parameters: dict[str, Any],
    config: ScalperConfig,
) -> tuple[dict[str, str], bool]:
    normalized = normalize_parameters(parameters)
    if not normalized:
        return normalized, False

    current_take_profit_bps = Decimal(normalized.get("take_profit_bps", str(config.take_profit_bps)))
    min_net_take_profit_bps = Decimal(
        normalized.get("min_net_take_profit_bps", str(config.min_net_take_profit_bps))
    )
    recommended_take_profit_bps = get_recommended_take_profit_bps(
        config,
        min_net_take_profit_bps=min_net_take_profit_bps,
    )
    if current_take_profit_bps >= recommended_take_profit_bps:
        return normalized, False

    normalized["take_profit_bps"] = str(recommended_take_profit_bps)
    return normalized, True


def build_coverage_unblocker_candidate(
    config: ScalperConfig,
    *,
    current_parameters: dict[str, Any],
    analysis_status: str,
    analysis_trade_count: int,
    min_trades: int,
    optimizer_payload: dict[str, Any] | None,
    enabled: bool,
    min_snapshot_count: int,
    max_ready_rate_pct: Decimal,
    min_dominant_block_share_pct: Decimal,
    allowed_block_reasons: tuple[str, ...],
) -> tuple[dict[str, str] | None, dict[str, Any]]:
    details: dict[str, Any] = {
        "enabled": enabled,
        "eligible": False,
        "reason": None,
        "analysis_status": analysis_status,
        "analysis_trade_count": analysis_trade_count,
        "min_trades": min_trades,
        "min_snapshot_count": min_snapshot_count,
        "max_ready_rate_pct": str(max_ready_rate_pct),
        "min_dominant_block_share_pct": str(min_dominant_block_share_pct),
        "allowed_block_reasons": list(allowed_block_reasons),
        "snapshot_count": None,
        "signal_ready_rate_pct": None,
        "dominant_block_reason": None,
        "dominant_block_share_pct": None,
    }
    if not enabled:
        details["reason"] = "coverage_fallback_disabled"
        return None, details
    if analysis_status == "ok" and analysis_trade_count >= min_trades:
        details["reason"] = "trade_sample_already_sufficient"
        return None, details
    if optimizer_payload is None:
        details["reason"] = "missing_optimizer_report"
        return None, details
    if optimizer_payload.get("status") != "ok":
        details["reason"] = f"optimizer_{optimizer_payload.get('status', 'unknown')}"
        return None, details

    coverage_summary = dict((((optimizer_payload.get("signal_coverage") or {}).get("summary")) or {}))
    snapshot_count = int(coverage_summary.get("snapshot_count", 0))
    signal_ready_rate_pct = Decimal(str(coverage_summary.get("signal_ready_rate_pct", "0")))
    details["snapshot_count"] = snapshot_count
    details["signal_ready_rate_pct"] = str(signal_ready_rate_pct)
    if snapshot_count < min_snapshot_count:
        details["reason"] = "not_enough_snapshots"
        return None, details
    if signal_ready_rate_pct > max_ready_rate_pct:
        details["reason"] = "ready_rate_not_low_enough"
        return None, details

    top_blocked = list(coverage_summary.get("top_blocked_reasons") or [])
    if not top_blocked:
        details["reason"] = "no_blocked_reasons"
        return None, details
    dominant = dict(top_blocked[0])
    dominant_reason = str(dominant.get("reason", "")).strip().lower()
    dominant_count = int(dominant.get("count", 0))
    dominant_share_pct = (
        (Decimal(dominant_count) / Decimal(snapshot_count) * Decimal("100"))
        if snapshot_count > 0
        else Decimal("0")
    )
    details["dominant_block_reason"] = dominant_reason or None
    details["dominant_block_share_pct"] = str(dominant_share_pct.quantize(Decimal("0.01")))
    allowed = {reason.strip().lower() for reason in allowed_block_reasons if reason.strip()}
    if dominant_reason not in allowed:
        details["reason"] = "dominant_block_reason_not_allowed"
        return None, details
    if dominant_share_pct < min_dominant_block_share_pct:
        details["reason"] = "dominant_block_reason_not_strong_enough"
        return None, details

    candidate = build_unblocker_step_candidate(
        current_parameters=current_parameters,
        dominant_block_reason=dominant_reason,
    )
    if candidate is None:
        details["reason"] = "no_safe_relaxation_available"
        return None, details

    details["eligible"] = True
    details["reason"] = "coverage_unblocker_candidate"
    return candidate, details


def load_collection_guard_policy() -> CollectionGuardPolicy:
    return CollectionGuardPolicy(
        min_trades=max(0, int(_env_value("SCALPER_COLLECTION_GUARD_MIN_TRADES", "3") or 3)),
        min_trade_share_pct=Decimal(_env_value("SCALPER_COLLECTION_GUARD_MIN_TRADE_SHARE_PCT", "35") or "35"),
        min_signal_share_pct=Decimal(_env_value("SCALPER_COLLECTION_GUARD_MIN_SIGNAL_SHARE_PCT", "35") or "35"),
    )


def build_unblocker_step_candidate(
    *,
    current_parameters: dict[str, Any],
    dominant_block_reason: str,
) -> dict[str, str] | None:
    candidate = normalize_parameters(current_parameters)
    if dominant_block_reason == "expected_edge_too_low":
        next_value = _step_down_decimal(
            Decimal(candidate.get("min_expected_edge_bps", "0")),
            EXPECTED_EDGE_STEPS,
        )
        if next_value is None:
            return None
        candidate["min_expected_edge_bps"] = str(next_value)
        return candidate
    if dominant_block_reason == "impulse_too_small":
        next_value = _step_down_decimal(
            Decimal(candidate.get("min_impulse_bps", "0")),
            IMPULSE_STEPS,
        )
        if next_value is None:
            return None
        candidate["min_impulse_bps"] = str(next_value)
        return candidate
    if dominant_block_reason == "imbalance_too_low":
        next_value = _step_down_decimal(
            Decimal(candidate.get("min_imbalance", "0")),
            IMBALANCE_STEPS,
        )
        if next_value is None:
            return None
        candidate["min_imbalance"] = str(next_value)
        return candidate
    return None


def _step_down_decimal(current: Decimal, steps: tuple[Decimal, ...]) -> Decimal | None:
    ordered = sorted(steps)
    previous: Decimal | None = None
    for value in ordered:
        if current <= value:
            return previous
        previous = value
    return previous if previous is not None and previous < current else None


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in updates:
            output.append(f"{normalized_key}={updates[normalized_key]}")
            seen.add(normalized_key)
        else:
            output.append(line)

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")

    path.write_text("\n".join(output) + "\n", encoding="utf-8")


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
    if any(
        reason in {"analysis_no_entry_window_data", "optimizer_no_entry_window_data", "research_no_entry_window_data"}
        for reason in reasons
    ):
        return "collect_in_window_market_data"
    if "insufficient_trade_sample" in reasons:
        return "collect_more_paper_trades"
    if "open_positions_present" in reasons:
        return "wait_for_positions_to_close"
    if "entry_window_open" in reasons:
        return "retry_outside_entry_window"
    if any(reason.startswith("strategy_lab_") or reason.startswith("strategy_overlay_") for reason in reasons):
        return "wait_for_better_strategy_overlay_candidate"
    if any(reason.startswith("research_") for reason in reasons):
        return "wait_for_better_regime_candidate"
    if any(reason.startswith("optimizer_") for reason in reasons):
        return "wait_for_better_optimizer_candidate"
    if not apply and not reasons:
        return "candidate_ready_for_apply"
    return "no_change"


def write_tuning_report(runtime_dir: Path, payload: dict[str, Any]) -> None:
    tuning_dir = runtime_dir / "tuning"
    tuning_dir.mkdir(parents=True, exist_ok=True)
    latest_path = tuning_dir / "latest.json"
    history_path = tuning_dir / "history.jsonl"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    latest_path.write_text(body, encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _env_value(key: str, default: str | None = None) -> str | None:
    return __import__("os").getenv(key, default)


def _entry_window_open(config: ScalperConfig) -> bool:
    local_now = datetime.now(config.timezone)
    if local_now.weekday() not in config.entry_weekdays:
        return False
    current_time = local_now.time().replace(tzinfo=None)
    return config.entry_start_time <= current_time <= config.entry_end_time


def _parse_csv(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
