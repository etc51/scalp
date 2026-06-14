from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ScalperConfig, parse_bool


def run_watchdog(
    config: ScalperConfig,
    *,
    write_report: bool,
) -> dict[str, Any]:
    runtime_dir = config.runtime_dir
    state_path = runtime_dir / "dashboard_state.json"
    session_path = runtime_dir / "paper_session.json"
    watchdog_url = f"http://127.0.0.1:{_dashboard_port()}/health"
    max_state_age_seconds = int(config.watchdog_max_state_age_seconds)
    max_market_data_age_seconds = int(config.watchdog_max_market_data_age_seconds)
    market_data_warmup_seconds = int(config.watchdog_market_data_warmup_seconds)
    dashboard_timeout_seconds = float(config.watchdog_timeout_seconds)
    check_dashboard_http = bool(config.watchdog_check_dashboard_http)

    now = datetime.now(timezone.utc)
    restart_reasons: list[str] = []
    warning_reasons: list[str] = []

    state_payload, state_error = _load_json(state_path)
    session_payload, session_error = _load_json(session_path)

    state_updated_at = _parse_dt((state_payload or {}).get("updated_at"))
    state_started_at = _parse_dt((state_payload or {}).get("started_at"))
    state_age_seconds = (
        round((now - state_updated_at).total_seconds(), 3)
        if state_updated_at is not None
        else None
    )
    state_uptime_seconds = (
        round((now - state_started_at).total_seconds(), 3)
        if state_started_at is not None
        else None
    )
    market_data_required_now = _market_data_required_now(now, config)
    market_data_payload = (state_payload or {}).get("market_data") or {}
    market_data_updated_at = _parse_dt(market_data_payload.get("last_received_at"))
    market_data_age_seconds = (
        round((now - market_data_updated_at).total_seconds(), 3)
        if market_data_updated_at is not None
        else None
    )
    market_data_error = None
    if state_error == "missing":
        restart_reasons.append("missing_dashboard_state")
    elif state_error is not None:
        restart_reasons.append("dashboard_state_parse_error")
    elif state_age_seconds is None:
        restart_reasons.append("dashboard_state_missing_updated_at")
    elif state_age_seconds > max_state_age_seconds:
        restart_reasons.append("dashboard_state_stale")
    elif market_data_required_now and market_data_updated_at is None:
        if state_uptime_seconds is not None and state_uptime_seconds > market_data_warmup_seconds:
            restart_reasons.append("market_data_missing")
            market_data_error = "missing"
    elif market_data_required_now and market_data_age_seconds > max_market_data_age_seconds:
        restart_reasons.append("market_data_stale")
        market_data_error = "stale"

    if session_error == "missing":
        warning_reasons.append("missing_paper_session")
    elif session_error is not None:
        warning_reasons.append("paper_session_parse_error")

    dashboard_http_ok = None
    dashboard_http_error = None
    if check_dashboard_http:
        dashboard_http_ok, dashboard_http_error = _check_http_health(
            watchdog_url,
            timeout_seconds=dashboard_timeout_seconds,
        )
        if not dashboard_http_ok:
            restart_reasons.append("dashboard_http_unreachable")

    service_mode = str((state_payload or {}).get("mode", config.mode or ""))
    if service_mode != "paper":
        warning_reasons.append("mode_not_paper")
    if parse_bool(_env_value("SCALPER_ALLOW_LIVE_TRADING", "0"), default=False):
        warning_reasons.append("live_guard_disabled")

    open_positions = len(list((session_payload or {}).get("positions", [])))
    status = "healthy"
    if restart_reasons:
        status = "restart_required"
    elif warning_reasons:
        status = "warning"

    payload = {
        "status": status,
        "generated_at": now.isoformat(),
        "mode": service_mode or config.mode,
        "restart_required": bool(restart_reasons),
        "restart_reasons": restart_reasons,
        "warning_reasons": warning_reasons,
        "checks": {
            "dashboard_state": {
                "path": str(state_path),
                "exists": state_path.exists(),
                "started_at": state_started_at.isoformat() if state_started_at else None,
                "uptime_seconds": state_uptime_seconds,
                "updated_at": state_updated_at.isoformat() if state_updated_at else None,
                "age_seconds": state_age_seconds,
                "max_age_seconds": max_state_age_seconds,
                "error": state_error,
            },
            "market_data": {
                "required_now": market_data_required_now,
                "last_received_at": market_data_updated_at.isoformat() if market_data_updated_at else None,
                "age_seconds": market_data_age_seconds,
                "max_age_seconds": max_market_data_age_seconds,
                "warmup_seconds": market_data_warmup_seconds,
                "error": market_data_error,
            },
            "paper_session": {
                "path": str(session_path),
                "exists": session_path.exists(),
                "open_positions": open_positions,
                "error": session_error,
            },
            "dashboard_http": {
                "checked": check_dashboard_http,
                "url": watchdog_url,
                "ok": dashboard_http_ok,
                "timeout_seconds": dashboard_timeout_seconds,
                "error": dashboard_http_error,
            },
            "paper_guard": {
                "state_mode": service_mode,
                "allow_live_trading": parse_bool(_env_value("SCALPER_ALLOW_LIVE_TRADING", "0"), default=False),
            },
        },
        "next_action": "restart_services" if restart_reasons else "none",
    }
    if write_report:
        write_watchdog_report(runtime_dir, payload)
    return payload


def write_watchdog_report(runtime_dir: Path, payload: dict[str, Any]) -> None:
    watchdog_dir = runtime_dir / "watchdog"
    watchdog_dir.mkdir(parents=True, exist_ok=True)
    latest_path = watchdog_dir / "latest.json"
    history_path = watchdog_dir / "history.jsonl"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    latest_path.write_text(body, encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError:
        return None, "json_decode_error"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _check_http_health(url: str, *, timeout_seconds: float) -> tuple[bool, str | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return 200 <= getattr(response, "status", 500) < 300, None
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _dashboard_port() -> int:
    raw = _env_value("SCALPER_DASHBOARD_PORT", "8080") or "8080"
    return int(raw)


def _market_data_required_now(now: datetime, config: ScalperConfig) -> bool:
    local_now = now.astimezone(config.timezone)
    if local_now.weekday() not in config.entry_weekdays:
        return False
    local_time = local_now.time().replace(tzinfo=None)
    return config.entry_start_time <= local_time <= config.entry_end_time


def _env_value(key: str, default: str | None = None) -> str | None:
    return __import__("os").getenv(key, default)
