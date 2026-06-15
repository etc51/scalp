from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .commission import DEFAULT_PREMIUM_SHARE_COMMISSION_BPS


TRACKED_PAPER_PROFILE_PATH = Path("config") / "paper_profile.env"
TRACKED_PAPER_PROFILE_KEYS = frozenset(
    {
        "SCALPER_PAPER_INITIAL_CASH_RUB",
        "SCALPER_PAPER_MAX_GROSS_LEVERAGE",
    }
)
TRACKED_STRATEGY_PROFILE_PATH = Path("config") / "strategy_profile.env"
TRACKED_STRATEGY_PROFILE_KEYS = frozenset(
    {
        "SCALPER_MAX_SPREAD_BPS",
        "SCALPER_MIN_IMBALANCE",
        "SCALPER_MIN_IMPULSE_BPS",
        "SCALPER_TAKE_PROFIT_BPS",
        "SCALPER_STOP_LOSS_BPS",
        "SCALPER_TIME_STOP_SECONDS",
        "SCALPER_MIN_EXPECTED_EDGE_BPS",
        "SCALPER_MIN_NET_TAKE_PROFIT_BPS",
        "SCALPER_TARGET_NET_TAKE_PROFIT_BUFFER_BPS",
        "SCALPER_COOLDOWN_SECONDS",
        "SCALPER_REGIME_FILTER_MODE",
        "SCALPER_INTRADAY_TICKER_LOSS_LIMIT_RUB",
        "SCALPER_INTRADAY_TICKER_MAX_CONSECUTIVE_LOSSES",
        "SCALPER_MAX_OPEN_POSITIONS",
        "SCALPER_MAX_POSITION_NOTIONAL_RUB",
        "SCALPER_POSITION_SIZING_MODE",
        "SCALPER_ORDER_QUANTITY_LOTS",
        "SCALPER_ALLOW_SHORT",
        "SCALPER_WATCHLIST",
    }
)


def load_dotenv(
    path: Path,
    *,
    override: bool = False,
    protected_keys: set[str] | None = None,
    allowed_keys: set[str] | frozenset[str] | None = None,
) -> None:
    if not path.exists():
        return
    protected = protected_keys or set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if allowed_keys is not None and normalized_key not in allowed_keys:
            continue
        if override:
            if normalized_key in protected:
                continue
            os.environ[normalized_key] = value.strip().strip("'").strip('"')
            continue
        os.environ.setdefault(normalized_key, value.strip().strip("'").strip('"'))


def load_project_env(
    dotenv_path: Path = Path(".env"),
    tracked_paper_profile_path: Path = TRACKED_PAPER_PROFILE_PATH,
    tracked_strategy_profile_path: Path = TRACKED_STRATEGY_PROFILE_PATH,
) -> None:
    protected_keys = set(os.environ)
    load_dotenv(dotenv_path)
    load_dotenv(
        tracked_paper_profile_path,
        override=True,
        protected_keys=protected_keys,
        allowed_keys=TRACKED_PAPER_PROFILE_KEYS,
    )
    load_dotenv(
        tracked_strategy_profile_path,
        override=True,
        protected_keys=protected_keys,
        allowed_keys=TRACKED_STRATEGY_PROFILE_KEYS,
    )


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(value: str | None, default: list[str]) -> list[str]:
    if value is None or not value.strip():
        return default
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def parse_time_value(value: str | None, default: str) -> time:
    candidate = (value or default).strip()
    try:
        return time.fromisoformat(candidate)
    except ValueError as exc:
        raise SystemExit(f"Invalid time value: {candidate}") from exc


def parse_weekdays(value: str | None, default: str = "mon,tue,wed,thu,fri") -> tuple[int, ...]:
    raw = (value or default).strip().lower()
    mapping = {
        "mon": 0,
        "monday": 0,
        "tue": 1,
        "tues": 1,
        "tuesday": 1,
        "wed": 2,
        "wednesday": 2,
        "thu": 3,
        "thur": 3,
        "thurs": 3,
        "thursday": 3,
        "fri": 4,
        "friday": 4,
        "sat": 5,
        "saturday": 5,
        "sun": 6,
        "sunday": 6,
    }
    result: list[int] = []
    for item in raw.split(","):
        token = item.strip()
        if not token:
            continue
        if token.isdigit():
            weekday = int(token)
        else:
            weekday = mapping.get(token, -1)
        if weekday not in range(7):
            raise SystemExit(f"Invalid weekday value: {token}")
        result.append(weekday)
    if not result:
        raise SystemExit("SCALPER_ENTRY_WEEKDAYS must not be empty.")
    return tuple(dict.fromkeys(result))


def parse_timezone(value: str | None, default: str = "Europe/Moscow") -> tuple[str, ZoneInfo | timezone]:
    name = (value or default).strip() or default
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Europe/Moscow":
            return name, timezone(timedelta(hours=3), name="MSK")
        raise SystemExit(f"Unknown timezone: {name}")


def parse_regime_filter_mode(value: str | None, default: str = "off") -> str:
    candidate = (value or default).strip().lower() or default
    allowed = {
        "off",
        "trend_not_bearish",
        "trend_side_aware",
        "trend_bullish",
        "macd_positive",
        "rsi_50_70",
    }
    if candidate not in allowed:
        raise SystemExit(
            "Invalid SCALPER_REGIME_FILTER_MODE. Expected one of: "
            + ", ".join(sorted(allowed))
        )
    return candidate


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
    intraday_ticker_loss_limit_rub: Decimal
    intraday_ticker_max_consecutive_losses: int
    cooldown_seconds: float
    time_stop_seconds: float
    impulse_window_seconds: float
    max_spread_bps: Decimal
    min_imbalance: Decimal
    min_impulse_bps: Decimal
    take_profit_bps: Decimal
    stop_loss_bps: Decimal
    min_expected_edge_bps: Decimal
    min_net_take_profit_bps: Decimal
    target_net_take_profit_buffer_bps: Decimal
    regime_filter_mode: str
    premium_share_commission_bps: Decimal
    paper_initial_cash_rub: Decimal
    paper_max_gross_leverage: Decimal
    position_sizing_mode: str
    timezone_name: str
    timezone: ZoneInfo | timezone
    entry_weekdays: tuple[int, ...]
    entry_start_time: time
    entry_end_time: time
    allow_short: bool
    max_open_positions: int
    run_duration_seconds: float
    runtime_dir: Path
    state_heartbeat_seconds: float
    stream_idle_reconnect_seconds: float
    stream_reconnect_delay_seconds: float
    watchdog_max_state_age_seconds: int
    watchdog_max_market_data_age_seconds: int
    watchdog_market_data_warmup_seconds: int
    watchdog_timeout_seconds: float
    watchdog_check_dashboard_http: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moex_scalper",
        description="Moderate scalper for high-liquidity MOEX stocks via T-Bank Invest API.",
    )
    parser.add_argument("command", choices=("doctor", "run", "dashboard", "optimize", "analyze", "research", "summarize", "tune", "watchdog", "restrict", "govern"))
    parser.add_argument("--mode", choices=("paper", "live"), default=os.getenv("SCALPER_MODE", "paper"))
    parser.add_argument("--watchlist", default=os.getenv("SCALPER_WATCHLIST", "SBER,GAZP,LKOH,VTBR"))
    parser.add_argument("--run-seconds", type=float, default=float(os.getenv("SCALPER_RUN_DURATION_SECONDS", "0")))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--date", default=None)
    parser.add_argument("--input", default=None)
    parser.add_argument("--top", type=int, default=None)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--min-trades", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    return parser


def load_config(args: argparse.Namespace, *, require_auth: bool = True) -> ScalperConfig:
    token = os.getenv("TBANK_TOKEN", "").strip()
    account_id = os.getenv("TBANK_ACCOUNT_ID", "").strip()
    if require_auth and not token:
        raise SystemExit("TBANK_TOKEN is required.")
    if require_auth and args.mode == "live" and not account_id:
        raise SystemExit("TBANK_ACCOUNT_ID is required for live mode.")
    if require_auth and args.mode == "live" and not parse_bool(os.getenv("SCALPER_ALLOW_LIVE_TRADING"), default=False):
        raise SystemExit(
            "Live trading is disabled. Keep SCALPER_MODE=paper unless the user explicitly approves live mode "
            "and SCALPER_ALLOW_LIVE_TRADING=1 is set."
        )

    default_max_open_positions = "4" if args.mode == "paper" else "1"
    timezone_name, parsed_timezone = parse_timezone(os.getenv("SCALPER_TIMEZONE"))

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
        intraday_ticker_loss_limit_rub=Decimal(
            os.getenv("SCALPER_INTRADAY_TICKER_LOSS_LIMIT_RUB", "250")
        ),
        intraday_ticker_max_consecutive_losses=max(
            0,
            int(os.getenv("SCALPER_INTRADAY_TICKER_MAX_CONSECUTIVE_LOSSES", "4")),
        ),
        cooldown_seconds=float(os.getenv("SCALPER_COOLDOWN_SECONDS", "12")),
        time_stop_seconds=float(os.getenv("SCALPER_TIME_STOP_SECONDS", "8")),
        impulse_window_seconds=float(os.getenv("SCALPER_IMPULSE_WINDOW_SECONDS", "2.5")),
        max_spread_bps=Decimal(os.getenv("SCALPER_MAX_SPREAD_BPS", "2.5")),
        min_imbalance=Decimal(os.getenv("SCALPER_MIN_IMBALANCE", "0.58")),
        min_impulse_bps=Decimal(os.getenv("SCALPER_MIN_IMPULSE_BPS", "6")),
        take_profit_bps=Decimal(os.getenv("SCALPER_TAKE_PROFIT_BPS", "18")),
        stop_loss_bps=Decimal(os.getenv("SCALPER_STOP_LOSS_BPS", "10")),
        min_expected_edge_bps=Decimal(os.getenv("SCALPER_MIN_EXPECTED_EDGE_BPS", "14")),
        min_net_take_profit_bps=Decimal(os.getenv("SCALPER_MIN_NET_TAKE_PROFIT_BPS", "4")),
        target_net_take_profit_buffer_bps=Decimal(
            os.getenv("SCALPER_TARGET_NET_TAKE_PROFIT_BUFFER_BPS", "2")
        ),
        regime_filter_mode=parse_regime_filter_mode(os.getenv("SCALPER_REGIME_FILTER_MODE", "off")),
        premium_share_commission_bps=Decimal(
            os.getenv("SCALPER_PREMIUM_SHARE_COMMISSION_BPS", str(DEFAULT_PREMIUM_SHARE_COMMISSION_BPS))
        ),
        paper_initial_cash_rub=Decimal(os.getenv("SCALPER_PAPER_INITIAL_CASH_RUB", "300000")),
        paper_max_gross_leverage=max(Decimal("1.0"), Decimal(os.getenv("SCALPER_PAPER_MAX_GROSS_LEVERAGE", "1.0"))),
        position_sizing_mode=os.getenv("SCALPER_POSITION_SIZING_MODE", "equal_weight_cash").strip().lower(),
        timezone_name=timezone_name,
        timezone=parsed_timezone,
        entry_weekdays=parse_weekdays(os.getenv("SCALPER_ENTRY_WEEKDAYS")),
        entry_start_time=parse_time_value(os.getenv("SCALPER_ENTRY_START"), "10:15"),
        entry_end_time=parse_time_value(os.getenv("SCALPER_ENTRY_END"), "17:45"),
        allow_short=parse_bool(os.getenv("SCALPER_ALLOW_SHORT"), default=False),
        max_open_positions=max(1, int(os.getenv("SCALPER_MAX_OPEN_POSITIONS", default_max_open_positions))),
        run_duration_seconds=max(0.0, float(args.run_seconds)),
        runtime_dir=Path(os.getenv("SCALPER_RUNTIME_DIR", "runtime")),
        state_heartbeat_seconds=max(1.0, float(os.getenv("SCALPER_STATE_HEARTBEAT_SECONDS", "30"))),
        stream_idle_reconnect_seconds=max(5.0, float(os.getenv("SCALPER_STREAM_IDLE_RECONNECT_SECONDS", "45"))),
        stream_reconnect_delay_seconds=max(0.5, float(os.getenv("SCALPER_STREAM_RECONNECT_DELAY_SECONDS", "1"))),
        watchdog_max_state_age_seconds=max(5, int(os.getenv("SCALPER_WATCHDOG_MAX_STATE_AGE_SECONDS", "120"))),
        watchdog_max_market_data_age_seconds=max(
            5,
            int(os.getenv("SCALPER_WATCHDOG_MAX_MARKET_DATA_AGE_SECONDS", "90")),
        ),
        watchdog_market_data_warmup_seconds=max(
            5,
            int(os.getenv("SCALPER_WATCHDOG_MARKET_DATA_WARMUP_SECONDS", "90")),
        ),
        watchdog_timeout_seconds=max(0.5, float(os.getenv("SCALPER_WATCHDOG_TIMEOUT_SECONDS", "3"))),
        watchdog_check_dashboard_http=parse_bool(
            os.getenv("SCALPER_WATCHDOG_CHECK_DASHBOARD_HTTP", "1"),
            default=True,
        ),
    )
