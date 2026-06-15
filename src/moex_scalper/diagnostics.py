from __future__ import annotations

from decimal import Decimal
from typing import Any

from .commission import CommissionModel
from .config import ScalperConfig


def get_roundtrip_commission_bps(config: ScalperConfig) -> Decimal:
    return CommissionModel(config.premium_share_commission_bps).roundtrip_bps


def build_paper_risk_profile(config: ScalperConfig) -> dict[str, str]:
    leverage = config.paper_max_gross_leverage
    if leverage <= Decimal("1.2"):
        return {
            "stage": "Conservative validation",
            "max_gross_leverage": str(leverage),
            "margin_policy": "Margin enabled, but capped",
            "decision": "Hold at 1.2x until expectancy is proven",
            "promotion_rule": "Move to 1.5x only after 100+ closed paper trades, profit factor >= 1.15, positive expectancy, and no repeated daily loss-limit breaches",
            "rollback_rule": "Drop back to 1.0x if the recent sample turns negative or daily loss-limit triggers start repeating",
        }
    if leverage <= Decimal("1.5"):
        return {
            "stage": "Moderate scale-up",
            "max_gross_leverage": str(leverage),
            "margin_policy": "Measured margin use",
            "decision": "Use 1.5x only after a positive sample is confirmed",
            "promotion_rule": "Hold here only while profit factor and expectancy stay positive through new samples",
            "rollback_rule": "Drop back to 1.2x if drawdown or daily loss-limit pressure increases",
        }
    return {
        "stage": "Aggressive for paper scalping",
        "max_gross_leverage": str(leverage),
        "margin_policy": "High margin usage",
        "decision": "Not recommended for the current validation phase",
        "promotion_rule": "Do not raise leverage further without a clear statistical edge",
        "rollback_rule": "Reduce leverage if stability is not already proven",
    }


def get_configured_net_take_profit_bps(config: ScalperConfig) -> Decimal:
    return config.take_profit_bps - get_roundtrip_commission_bps(config)


def get_recommended_take_profit_bps(
    config: ScalperConfig,
    *,
    min_net_take_profit_bps: Decimal | None = None,
    target_buffer_bps: Decimal | None = None,
) -> Decimal:
    net_floor = (
        config.min_net_take_profit_bps
        if min_net_take_profit_bps is None
        else Decimal(min_net_take_profit_bps)
    )
    target_buffer = (
        config.target_net_take_profit_buffer_bps
        if target_buffer_bps is None
        else Decimal(target_buffer_bps)
    )
    return get_roundtrip_commission_bps(config) + net_floor + target_buffer


def build_strategy_diagnostics(config: ScalperConfig) -> dict[str, Any]:
    roundtrip_commission_bps = get_roundtrip_commission_bps(config)
    net_take_profit_bps = get_configured_net_take_profit_bps(config)
    net_take_profit_buffer_bps = net_take_profit_bps - config.min_net_take_profit_bps
    target_net_take_profit_buffer_bps = config.target_net_take_profit_buffer_bps
    recommended_take_profit_bps = get_recommended_take_profit_bps(config)
    paper_risk_profile = build_paper_risk_profile(config)
    expected_edge_ceiling_bps = config.take_profit_bps
    expected_edge_constraint_met = config.min_expected_edge_bps <= expected_edge_ceiling_bps
    viable_for_entry = (
        net_take_profit_bps >= config.min_net_take_profit_bps
        and expected_edge_constraint_met
    )
    target_headroom_met = net_take_profit_buffer_bps >= target_net_take_profit_buffer_bps
    warnings: list[str] = []

    if config.take_profit_bps <= roundtrip_commission_bps:
        warnings.append("take_profit_below_roundtrip_commission")
    if config.min_expected_edge_bps > expected_edge_ceiling_bps:
        warnings.append("min_expected_edge_above_take_profit")
    if not viable_for_entry:
        warnings.append("net_take_profit_below_floor")
    elif net_take_profit_buffer_bps <= Decimal("0"):
        warnings.append("net_take_profit_no_headroom")
    elif not target_headroom_met:
        warnings.append("net_take_profit_below_target_buffer")

    return {
        "allow_short": config.allow_short,
        "entry_modes": "long+short" if config.allow_short else "long_only",
        "regime_filter_mode": config.regime_filter_mode,
        "premium_roundtrip_commission_bps": str(roundtrip_commission_bps),
        "configured_take_profit_bps": str(config.take_profit_bps),
        "configured_net_take_profit_bps": str(net_take_profit_bps),
        "expected_edge_ceiling_bps": str(expected_edge_ceiling_bps),
        "expected_edge_constraint_met": expected_edge_constraint_met,
        "min_expected_edge_bps": str(config.min_expected_edge_bps),
        "min_net_take_profit_bps": str(config.min_net_take_profit_bps),
        "paper_risk_profile": paper_risk_profile,
        "net_take_profit_buffer_bps": str(net_take_profit_buffer_bps),
        "target_net_take_profit_buffer_bps": str(target_net_take_profit_buffer_bps),
        "recommended_take_profit_bps": str(recommended_take_profit_bps),
        "viable_for_entry": viable_for_entry,
        "target_headroom_met": target_headroom_met,
        "warnings": warnings,
    }


def is_strategy_config_viable(config: ScalperConfig) -> bool:
    return bool(build_strategy_diagnostics(config)["viable_for_entry"])


def resolve_strategy_config_next_action(diagnostics: dict[str, Any]) -> str:
    warnings = set(str(item) for item in diagnostics.get("warnings") or [])
    if not diagnostics.get("viable_for_entry", True):
        if "min_expected_edge_above_take_profit" in warnings:
            return "lower_expected_edge_or_raise_take_profit"
        return "raise_take_profit_or_lower_net_floor"
    if not diagnostics.get("target_headroom_met", True):
        return "raise_take_profit_for_headroom"
    return "review_strategy_config"
