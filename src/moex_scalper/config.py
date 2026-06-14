from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .commission import DEFAULT_PREMIUM_SHARE_COMMISSION_BPS


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(value: str | None, default: list[str]) -> list[str]:
    if value is None or not value.strip():
        return default
    return [item.strip().upper() for item in value.split(",") if item.strip()]


@dataclass(slots=True, frozen=True)
class ScalperConfig:
    token: str
    account_id: str
    mode: str
    target: str
    class_code: str
    watchlist: tuple[str, ...]
    orderbook_depth: int
    order_quantity_lots: int
    max_position_notional_rub: Decimal
    daily_loss_limit_rub: Decimal
    cooldown_seconds: float
    time_stop_seconds: float
    impulse_window_seconds: float
    max_spread_bps: Decimal
    min_imbalance: Decimal
    min_impulse_bps: Decimal
    take_profit_bps: Decimal
    stop_loss_bps: Decimal
    min_expected_edge_bps: Decimal
    premium_share_commission_bps: Decimal
    allow_short: bool
    max_open_positions: int
    run_duration_seconds: float
    runtime_dir: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moex_scalper",
        description="Moderate scalper for high-liquidity MOEX stocks via T-Bank Invest API.",
    )
    parser.add_argument("command", choices=("doctor", "run"))
    parser.add_argument("--mode", choices=("paper", "live"), default=os.getenv("SCALPER_MODE", "paper"))
    parser.add_argument("--watchlist", default=os.getenv("SCALPER_WATCHLIST", "SBER,GAZP,LKOH,VTBR"))
    parser.add_argument("--run-seconds", type=float, default=float(os.getenv("SCALPER_RUN_DURATION_SECONDS", "0")))
    return parser


def load_config(args: argparse.Namespace) -> ScalperConfig:
    token = os.getenv("TBANK_TOKEN", "").strip()
    account_id = os.getenv("TBANK_ACCOUNT_ID", "").strip()
    if not token:
        raise SystemExit("TBANK_TOKEN is required.")
    if args.mode == "live" and not account_id:
        raise SystemExit("TBANK_ACCOUNT_ID is required for live mode.")

    return ScalperConfig(
        token=token,
        account_id=account_id,
        mode=args.mode,
        target=os.getenv("TBANK_TARGET", "invest-public-api.tbank.ru:443"),
        class_code=os.getenv("SCALPER_CLASS_CODE", "TQBR").strip().upper(),
        watchlist=tuple(parse_csv(args.watchlist, ["SBER", "GAZP", "LKOH", "VTBR"])),
        orderbook_depth=max(1, int(os.getenv("SCALPER_ORDERBOOK_DEPTH", "1"))),
        order_quantity_lots=max(1, int(os.getenv("SCALPER_ORDER_QUANTITY_LOTS", "1"))),
        max_position_notional_rub=Decimal(os.getenv("SCALPER_MAX_POSITION_NOTIONAL_RUB", "30000")),
        daily_loss_limit_rub=Decimal(os.getenv("SCALPER_DAILY_LOSS_LIMIT_RUB", "2500")),
        cooldown_seconds=float(os.getenv("SCALPER_COOLDOWN_SECONDS", "12")),
        time_stop_seconds=float(os.getenv("SCALPER_TIME_STOP_SECONDS", "8")),
        impulse_window_seconds=float(os.getenv("SCALPER_IMPULSE_WINDOW_SECONDS", "2.5")),
        max_spread_bps=Decimal(os.getenv("SCALPER_MAX_SPREAD_BPS", "2.5")),
        min_imbalance=Decimal(os.getenv("SCALPER_MIN_IMBALANCE", "0.58")),
        min_impulse_bps=Decimal(os.getenv("SCALPER_MIN_IMPULSE_BPS", "6")),
        take_profit_bps=Decimal(os.getenv("SCALPER_TAKE_PROFIT_BPS", "18")),
        stop_loss_bps=Decimal(os.getenv("SCALPER_STOP_LOSS_BPS", "10")),
        min_expected_edge_bps=Decimal(os.getenv("SCALPER_MIN_EXPECTED_EDGE_BPS", "14")),
        premium_share_commission_bps=Decimal(
            os.getenv("SCALPER_PREMIUM_SHARE_COMMISSION_BPS", str(DEFAULT_PREMIUM_SHARE_COMMISSION_BPS))
        ),
        allow_short=parse_bool(os.getenv("SCALPER_ALLOW_SHORT"), default=False),
        max_open_positions=max(1, int(os.getenv("SCALPER_MAX_OPEN_POSITIONS", "1"))),
        run_duration_seconds=max(0.0, float(args.run_seconds)),
        runtime_dir=Path(os.getenv("SCALPER_RUNTIME_DIR", "runtime")),
    )
