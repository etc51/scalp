from __future__ import annotations

from decimal import Decimal
from typing import Any

from .commission import CommissionModel
from .config import ScalperConfig


def build_strategy_diagnostics(config: ScalperConfig) -> dict[str, Any]:
    roundtrip_commission_bps = CommissionModel(config.premium_share_commission_bps).roundtrip_bps
    net_take_profit_bps = config.take_profit_bps - roundtrip_commission_bps
    net_take_profit_buffer_bps = net_take_profit_bps - config.min_net_take_profit_bps
    viable_for_entry = net_take_profit_bps >= config.min_net_take_profit_bps
    warnings: list[str] = []

    if config.take_profit_bps <= roundtrip_commission_bps:
        warnings.append("take_profit_below_roundtrip_commission")
    if not viable_for_entry:
        warnings.append("net_take_profit_below_floor")
    elif net_take_profit_buffer_bps <= Decimal("0"):
        warnings.append("net_take_profit_no_headroom")

    return {
        "premium_roundtrip_commission_bps": str(roundtrip_commission_bps),
        "configured_take_profit_bps": str(config.take_profit_bps),
        "configured_net_take_profit_bps": str(net_take_profit_bps),
        "min_net_take_profit_bps": str(config.min_net_take_profit_bps),
        "net_take_profit_buffer_bps": str(net_take_profit_buffer_bps),
        "viable_for_entry": viable_for_entry,
        "warnings": warnings,
    }


def is_strategy_config_viable(config: ScalperConfig) -> bool:
    return bool(build_strategy_diagnostics(config)["viable_for_entry"])
