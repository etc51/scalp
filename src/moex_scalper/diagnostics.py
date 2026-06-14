from __future__ import annotations

from decimal import Decimal
from typing import Any

from .commission import CommissionModel
from .config import ScalperConfig


def get_roundtrip_commission_bps(config: ScalperConfig) -> Decimal:
    return CommissionModel(config.premium_share_commission_bps).roundtrip_bps


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
    viable_for_entry = net_take_profit_bps >= config.min_net_take_profit_bps
    target_headroom_met = net_take_profit_buffer_bps >= target_net_take_profit_buffer_bps
    warnings: list[str] = []

    if config.take_profit_bps <= roundtrip_commission_bps:
        warnings.append("take_profit_below_roundtrip_commission")
    if not viable_for_entry:
        warnings.append("net_take_profit_below_floor")
    elif net_take_profit_buffer_bps <= Decimal("0"):
        warnings.append("net_take_profit_no_headroom")
    elif not target_headroom_met:
        warnings.append("net_take_profit_below_target_buffer")

    return {
        "premium_roundtrip_commission_bps": str(roundtrip_commission_bps),
        "configured_take_profit_bps": str(config.take_profit_bps),
        "configured_net_take_profit_bps": str(net_take_profit_bps),
        "min_net_take_profit_bps": str(config.min_net_take_profit_bps),
        "net_take_profit_buffer_bps": str(net_take_profit_buffer_bps),
        "target_net_take_profit_buffer_bps": str(target_net_take_profit_buffer_bps),
        "recommended_take_profit_bps": str(recommended_take_profit_bps),
        "viable_for_entry": viable_for_entry,
        "target_headroom_met": target_headroom_met,
        "warnings": warnings,
    }


def is_strategy_config_viable(config: ScalperConfig) -> bool:
    return bool(build_strategy_diagnostics(config)["viable_for_entry"])
