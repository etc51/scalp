from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .analysis import analyze_trades
from .commission import CommissionModel
from .config import ScalperConfig, build_parser, load_config, load_dotenv
from .dashboard import serve_dashboard
from .diagnostics import build_strategy_diagnostics
from .optimizer import optimize_parameters
from .research import build_indicator_research
from .restrictions import build_restrictions
from .runtime import ScalperRuntime
from .summary import build_daily_summary
from .tbank import open_client, resolve_instruments, validate_account
from .tuning import tune_parameters
from .watchdog import run_watchdog


async def run_doctor(config: ScalperConfig) -> int:
    payload: dict[str, object] = {
        "mode": config.mode,
        "watchlist": list(config.watchlist),
        "premium_share_commission_bps": str(config.premium_share_commission_bps),
        "premium_roundtrip_commission_bps": str(
            CommissionModel(config.premium_share_commission_bps).roundtrip_bps
        ),
        "min_expected_edge_bps": str(config.min_expected_edge_bps),
        "min_net_take_profit_bps": str(config.min_net_take_profit_bps),
        "strategy_diagnostics": build_strategy_diagnostics(config),
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

    if args.command == "dashboard":
        runtime_dir = Path(os.getenv("SCALPER_RUNTIME_DIR", "runtime"))
        serve_dashboard(host=args.host, port=args.port, runtime_dir=runtime_dir)
        return 0

    config = load_config(args, require_auth=args.command in {"doctor", "run"})

    if args.command == "doctor":
        return asyncio.run(run_doctor(config))
    if args.command == "optimize":
        payload = optimize_parameters(
            config,
            date_key=args.date,
            input_path=args.input,
            top_n=args.top,
            days=args.days,
            min_trades=args.min_trades,
            write_report=args.write_report,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "analyze":
        payload = analyze_trades(
            config,
            date_key=args.date,
            input_path=args.input,
            top_n=args.top,
            days=args.days,
            write_report=args.write_report,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "research":
        payload = build_indicator_research(
            config,
            date_key=args.date,
            input_path=args.input,
            top_n=args.top,
            days=args.days,
            write_report=args.write_report,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "summarize":
        payload = build_daily_summary(
            config,
            write_report=args.write_report,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "tune":
        payload = tune_parameters(
            config,
            apply=args.apply,
            write_report=args.write_report,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "watchdog":
        payload = run_watchdog(
            config,
            write_report=args.write_report,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "restrict":
        payload = build_restrictions(
            config,
            apply=args.apply,
            write_report=args.write_report,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    runtime = ScalperRuntime(config)
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        print("Stopped by user.")
        return 130
    return 0
