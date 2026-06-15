from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .analysis import analyze_trades
from .config import ScalperConfig, build_parser, load_config, load_project_env
from .dashboard import serve_dashboard
from .doctor import build_doctor_payload, write_doctor_report
from .governance import run_governor
from .intraday import run_intraday_research
from .optimizer import optimize_parameters
from .research import build_indicator_research
from .restrictions import build_restrictions
from .runtime import ScalperRuntime
from .summary import build_daily_summary
from .tuning import tune_parameters
from .watchdog import run_watchdog


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


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
        host = args.host or os.getenv("SCALPER_DASHBOARD_HOST", "0.0.0.0")
        port = args.port if args.port is not None else _int_env("SCALPER_DASHBOARD_PORT", 8080)
        serve_dashboard(host=host, port=port, runtime_dir=runtime_dir)
        return 0

    config = load_config(args, require_auth=args.command in {"doctor", "run"})

    if args.command == "doctor":
        payload, exit_code = asyncio.run(build_doctor_payload(config))
        if args.write_report:
            write_doctor_report(config.runtime_dir, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return exit_code
    if args.command == "optimize":
        top = args.top if args.top is not None else 10
        days = args.days if args.days is not None else _int_env("SCALPER_OPTIMIZER_DAYS", 5)
        min_trades = (
            args.min_trades if args.min_trades is not None else _int_env("SCALPER_OPTIMIZER_MIN_TRADES", 5)
        )
        payload = optimize_parameters(
            config,
            date_key=args.date,
            input_path=args.input,
            top_n=top,
            days=days,
            min_trades=min_trades,
            write_report=args.write_report,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "analyze":
        days = args.days if args.days is not None else _int_env("SCALPER_ANALYSIS_DAYS", 5)
        top = args.top if args.top is not None else _int_env("SCALPER_ANALYSIS_TOP", 5)
        payload = analyze_trades(
            config,
            date_key=args.date,
            input_path=args.input,
            top_n=top,
            days=days,
            write_report=args.write_report,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "research":
        days = args.days if args.days is not None else _int_env("SCALPER_RESEARCH_DAYS", 5)
        top = args.top if args.top is not None else _int_env("SCALPER_RESEARCH_TOP", 5)
        payload = build_indicator_research(
            config,
            date_key=args.date,
            input_path=args.input,
            top_n=top,
            days=days,
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

    if args.command == "intraday":
        payload = run_intraday_research(
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
    if args.command == "govern":
        payload = run_governor(
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
