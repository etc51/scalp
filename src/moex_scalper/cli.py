from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .commission import CommissionModel
from .config import ScalperConfig, build_parser, load_config, load_dotenv
from .runtime import ScalperRuntime
from .tbank import open_client, resolve_instruments, validate_account


async def run_doctor(config: ScalperConfig) -> int:
    payload: dict[str, object] = {
        "mode": config.mode,
        "watchlist": list(config.watchlist),
        "premium_share_commission_bps": str(config.premium_share_commission_bps),
        "premium_roundtrip_commission_bps": str(
            CommissionModel(config.premium_share_commission_bps).roundtrip_bps
        ),
        "min_expected_edge_bps": str(config.min_expected_edge_bps),
    }

    async with open_client(config) as services:
        instruments = await resolve_instruments(services, config)
        payload["instruments"] = [
            {
                "ticker": item.ticker,
                "instrument_id": item.instrument_id,
                "lot_size": item.lot_size,
                "min_price_increment": str(item.min_price_increment),
            }
            for item in instruments
        ]
        if config.account_id:
            payload["account"] = await validate_account(services, config.account_id)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    load_dotenv(Path(".env"))
    parser = build_parser()
    args = parser.parse_args()
    config = load_config(args)

    if args.command == "doctor":
        return asyncio.run(run_doctor(config))

    runtime = ScalperRuntime(config)
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        print("Stopped by user.")
        return 130
    return 0
