from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .commission import CommissionModel
from .config import ScalperConfig
from .diagnostics import build_strategy_diagnostics, resolve_strategy_config_next_action
from .tbank import open_client, resolve_instruments, validate_account


async def build_doctor_payload(config: ScalperConfig) -> tuple[dict[str, Any], int]:
    now = datetime.now(config.timezone)
    strategy_diagnostics = build_strategy_diagnostics(config)
    warnings: list[str] = []
    errors: list[str] = []
    exit_code = 0

    if not strategy_diagnostics["viable_for_entry"]:
        warnings.append("strategy_config_not_viable")
        exit_code = 1
    elif strategy_diagnostics["warnings"]:
        warnings.extend(str(item) for item in strategy_diagnostics["warnings"])

    payload: dict[str, Any] = {
        "status": "ready",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": config.mode,
        "target": config.target,
        "watchlist": list(config.watchlist),
        "entry_schedule": _build_entry_schedule_snapshot(config, now=now),
        "premium_share_commission_bps": str(config.premium_share_commission_bps),
        "premium_roundtrip_commission_bps": str(
            CommissionModel(config.premium_share_commission_bps).roundtrip_bps
        ),
        "paper_max_gross_leverage": str(config.paper_max_gross_leverage),
        "strategy_diagnostics": strategy_diagnostics,
        "warnings": warnings,
        "errors": errors,
        "api": {
            "reachable": False,
            "resolved_instruments": [],
            "account": None,
        },
        "next_action": "none",
    }

    try:
        async with open_client(config) as services:
            instruments = await resolve_instruments(services, config)
            payload["api"] = {
                "reachable": True,
                "resolved_instruments": [
                    {
                        "ticker": item.ticker,
                        "instrument_id": item.instrument_id,
                        "lot_size": item.lot_size,
                        "min_price_increment": str(item.min_price_increment),
                    }
                    for item in instruments
                ],
                "account": None,
            }
            if config.account_id:
                payload["api"]["account"] = await validate_account(services, config.account_id)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")
        exit_code = 1

    if errors:
        payload["status"] = "error"
        payload["next_action"] = "inspect_api_access"
    elif not strategy_diagnostics["viable_for_entry"]:
        payload["status"] = "warning"
        payload["next_action"] = resolve_strategy_config_next_action(strategy_diagnostics)
    elif not strategy_diagnostics.get("target_headroom_met", True):
        payload["status"] = "warning"
        payload["next_action"] = resolve_strategy_config_next_action(strategy_diagnostics)
    elif warnings:
        payload["status"] = "warning"
        payload["next_action"] = "review_strategy_headroom"

    return payload, exit_code


def write_doctor_report(runtime_dir: Path, payload: dict[str, Any]) -> None:
    doctor_dir = runtime_dir / "doctor"
    doctor_dir.mkdir(parents=True, exist_ok=True)
    latest_path = doctor_dir / "latest.json"
    history_path = doctor_dir / "history.jsonl"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    latest_path.write_text(body, encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _build_entry_schedule_snapshot(config: ScalperConfig, *, now: datetime) -> dict[str, Any]:
    local_time = now.time().replace(tzinfo=None)
    weekday_open = now.weekday() in config.entry_weekdays

    if not weekday_open:
        state = "weekday_closed"
    elif local_time < config.entry_start_time:
        state = "before_window"
    elif local_time > config.entry_end_time:
        state = "after_window"
    else:
        state = "in_window"

    next_start, next_end = _next_entry_window(config, now=now)
    return {
        "timezone": config.timezone_name,
        "weekday": now.weekday(),
        "local_now": now.isoformat(),
        "state": state,
        "weekdays": list(config.entry_weekdays),
        "start": config.entry_start_time.isoformat(timespec="minutes"),
        "end": config.entry_end_time.isoformat(timespec="minutes"),
        "next_start_at": next_start.isoformat() if next_start else None,
        "next_end_at": next_end.isoformat() if next_end else None,
    }


def _next_entry_window(config: ScalperConfig, *, now: datetime) -> tuple[datetime | None, datetime | None]:
    for day_offset in range(0, 8):
        candidate_day = (now + timedelta(days=day_offset)).date()
        candidate_dt = datetime.combine(candidate_day, config.entry_start_time, tzinfo=config.timezone)
        if candidate_dt.weekday() not in config.entry_weekdays:
            continue
        candidate_end = datetime.combine(candidate_day, config.entry_end_time, tzinfo=config.timezone)
        if day_offset == 0:
            local_time = now.time().replace(tzinfo=None)
            if local_time <= config.entry_end_time:
                if local_time <= config.entry_start_time:
                    return candidate_dt, candidate_end
                return now, candidate_end
        else:
            return candidate_dt, candidate_end
    return None, None
