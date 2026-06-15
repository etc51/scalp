from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .analysis import analyze_trades
from .config import ScalperConfig, build_parser, load_config, load_project_env
from .dashboard import serve_dashboard
from .doctor import build_doctor_payload, write_doctor_report
from .optimizer import optimize_parameters
from .research import build_indicator_research
from .restrictions import build_restrictions
from .runtime import ScalperRuntime
from .summary import build_daily_summary
from .tuning import tune_parameters
from .watchdog import run_watchdog


async def run_doctor(config: ScalperConfig) -> int:
    payload, exit_code = await build_doctor_payload(config)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def main() -> int:
    load_project_env()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "dashboard":
        runtime_dir = Path(os.getenv("SCALPER_RUNTIME_DIR", "runtime"))
        serve_dashboard(host=args.host, port=args.port, runtime_dir=runtime_dir)
        return 0

    config = load_config(args, require_auth=args.command in {"doctor", "run"})

    if args.command == "doctor":
        payload, exit_code = asyncio.run(build_doctor_payload(config))
        if args.write_report:
            write_doctor_report(config.runtime_dir, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return exit_code
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
