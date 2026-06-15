from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ScalperConfig
from .diagnostics import resolve_strategy_config_next_action


def build_daily_summary(
    config: ScalperConfig,
    *,
    write_report: bool,
) -> dict[str, Any]:
    runtime_dir = config.runtime_dir
    state = _load_json(runtime_dir / "dashboard_state.json")
    analysis = _load_json(runtime_dir / "analysis" / "latest.json")
    optimizer = _load_json(runtime_dir / "optimizer" / "latest.json")
    research = _load_json(runtime_dir / "research" / "latest.json")
    doctor = _load_json(runtime_dir / "doctor" / "latest.json")
    tuning = _load_json(runtime_dir / "tuning" / "latest.json")
    restrictions = _load_json(runtime_dir / "restrictions" / "latest.json")
    governance = _load_json(runtime_dir / "governance" / "latest.json")
    watchdog = _load_json(runtime_dir / "watchdog" / "latest.json")

    today_stats = ((state or {}).get("stats") or {}).get("today") or {}
    overall_stats = ((state or {}).get("stats") or {}).get("overall") or {}
    market_history = (state or {}).get("market_history") or {}
    blocked_reasons = dict((state or {}).get("blocked_reasons") or {})
    sorted_blocked = sorted(blocked_reasons.items(), key=lambda item: item[1], reverse=True)
    top_blocked = [{"reason": reason, "count": count} for reason, count in sorted_blocked[:5]]

    payload = {
        "status": "ok",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": (state or {}).get("mode", config.mode),
        "updated_at": (state or {}).get("updated_at"),
        "watchlist": list((state or {}).get("watchlist") or []),
        "strategy_diagnostics": (state or {}).get("strategy_diagnostics"),
        "risk_controls": (state or {}).get("risk_controls"),
        "active_restrictions": (state or {}).get("active_restrictions"),
        "today": {
            "trade_count": int(today_stats.get("trade_count", 0) or 0),
            "net_pnl_rub": today_stats.get("net_pnl_rub"),
            "win_rate_pct": today_stats.get("win_rate_pct"),
            "signals_detected": int((state or {}).get("signals_detected", 0) or 0),
            "snapshots_processed": int((state or {}).get("snapshots_processed", 0) or 0),
            "recorded_snapshots": int(market_history.get("recorded_snapshots_today", 0) or 0),
            "open_positions": len(list((state or {}).get("positions") or [])),
            "top_blocked_reasons": top_blocked,
        },
        "overall": {
            "trade_count": int(overall_stats.get("trade_count", 0) or 0),
            "net_pnl_rub": overall_stats.get("net_pnl_rub"),
            "win_rate_pct": overall_stats.get("win_rate_pct"),
        },
        "market_history": {
            "recording_mode": market_history.get("recording_mode"),
            "recorded_snapshots_total": int(market_history.get("recorded_snapshots_total", 0) or 0),
            "recorded_snapshots_today": int(market_history.get("recorded_snapshots_today", 0) or 0),
            "skipped_snapshots_total": int(market_history.get("skipped_snapshots_total", 0) or 0),
            "current_day": market_history.get("current_day"),
            "last_recorded_at": market_history.get("last_recorded_at"),
        },
        "analysis": {
            "status": (analysis or {}).get("status"),
            "assessment": (analysis or {}).get("assessment"),
            "trade_count": (((analysis or {}).get("summary") or {}).get("trade_count", (analysis or {}).get("trade_count"))),
            "net_pnl_rub": (((analysis or {}).get("summary") or {}).get("net_pnl_rub", (analysis or {}).get("net_pnl_rub"))),
            "focus": list((analysis or {}).get("focus") or []),
        },
        "optimizer": {
            "status": (optimizer or {}).get("status"),
            "snapshot_count": (optimizer or {}).get("snapshot_count"),
            "raw_snapshot_count": (optimizer or {}).get("raw_snapshot_count"),
            "recommendation_reason": (((optimizer or {}).get("recommendation") or {}).get("reason")),
            "signal_coverage": (((optimizer or {}).get("signal_coverage") or {}).get("summary")),
        },
        "research": {
            "status": (research or {}).get("status"),
            "indicator_backend": (research or {}).get("indicator_backend"),
            "snapshot_count": (research or {}).get("snapshot_count"),
            "strategy_lab_recommendation": ((((research or {}).get("summary") or {}).get("strategy_lab_recommendation")) or {}),
            "best_strategy_lab_candidate": ((((research or {}).get("summary") or {}).get("best_strategy_lab_candidate")) or {}),
            "focus": list((research or {}).get("focus") or []),
        },
        "doctor": {
            "status": (doctor or {}).get("status"),
            "next_action": (doctor or {}).get("next_action"),
            "warnings": list((doctor or {}).get("warnings") or []),
            "errors": list((doctor or {}).get("errors") or []),
            "entry_schedule": (doctor or {}).get("entry_schedule"),
            "strategy_diagnostics": (doctor or {}).get("strategy_diagnostics"),
        },
        "tuning": {
            "decision": (tuning or {}).get("decision"),
            "next_action": (tuning or {}).get("next_action"),
            "reasons": list((tuning or {}).get("reasons") or []),
        },
        "restrictions": {
            "decision": (restrictions or {}).get("decision"),
            "next_action": (restrictions or {}).get("next_action"),
            "reasons": list((restrictions or {}).get("reasons") or []),
        },
        "governance": {
            "decision": (governance or {}).get("decision"),
            "next_action": (governance or {}).get("next_action"),
            "applied": (governance or {}).get("applied"),
            "applied_actions": list((governance or {}).get("applied_actions") or []),
            "ready_actions": list((governance or {}).get("ready_actions") or []),
            "blocked_ready_actions": list((governance or {}).get("blocked_ready_actions") or []),
            "post_change_guard": (governance or {}).get("post_change_guard"),
            "active_experiment": (governance or {}).get("active_experiment"),
            "service_restart_required": (governance or {}).get("service_restart_required"),
        },
        "watchdog": {
            "status": (watchdog or {}).get("status"),
            "restart_required": (watchdog or {}).get("restart_required"),
            "warning_reasons": list((watchdog or {}).get("warning_reasons") or []),
            "next_action": (watchdog or {}).get("next_action"),
            "strategy_config": (((watchdog or {}).get("checks") or {}).get("strategy_config")),
        },
    }
    payload["focus"] = build_focus(payload)
    payload["headline"] = build_headline(payload)
    payload["next_action"] = build_next_action(payload)
    if write_report:
        write_summary_report(runtime_dir, payload)
    return payload


def build_focus(payload: dict[str, Any]) -> list[str]:
    focus: list[str] = []
    today = payload.get("today") or {}
    analysis = payload.get("analysis") or {}
    optimizer = payload.get("optimizer") or {}
    research = payload.get("research") or {}
    doctor = payload.get("doctor") or {}
    governance = payload.get("governance") or {}
    active_experiment = governance.get("active_experiment") or {}
    watchdog = payload.get("watchdog") or {}
    strategy_diagnostics = payload.get("strategy_diagnostics") or {}
    risk_controls = payload.get("risk_controls") or {}
    active_restrictions = payload.get("active_restrictions") or {}
    active_guards = list(risk_controls.get("active_ticker_guards") or [])
    active_session_guards = list(risk_controls.get("active_session_guards") or [])

    if watchdog.get("status") not in {None, "healthy"}:
        focus.append(f"Watchdog status: {watchdog.get('status')}.")
    if doctor.get("status") == "error":
        focus.append("Doctor не смог подтвердить готовность API или watchlist перед сессией.")
    if governance.get("applied"):
        focus.append(
            "Nightly governor применил: "
            + ", ".join(str(item) for item in governance.get("applied_actions") or [])
            + "."
        )
    elif (governance.get("post_change_guard") or {}).get("active"):
        focus.append(
            "Nightly governor ждет новый sample после последнего apply и пока не наслаивает следующую автоправку."
        )
    if active_experiment.get("status") == "negative_expectancy_so_far":
        focus.append(
            "Последний auto-change пока выглядит отрицательно по expectancy на новом sample."
        )
    elif active_experiment.get("status") == "positive_expectancy_so_far":
        focus.append(
            "Последний auto-change пока выглядит полезным: post-change sample держится в плюсе."
        )
    elif active_experiment.get("status") == "collecting_sample":
        focus.append(
            f"После последнего auto-change пока только {active_experiment.get('trade_count', 0)} сделок: рано судить."
        )
    if not strategy_diagnostics.get("viable_for_entry", True):
        warnings = set(str(item) for item in strategy_diagnostics.get("warnings") or [])
        if "min_expected_edge_above_take_profit" in warnings:
            focus.append(
                "Минимальный expected-edge выше take-profit; новые входы будут блокироваться до правки порога или цели."
            )
        else:
            focus.append(
                "Текущий take-profit после комиссии ниже минимального net-floor; новые входы будут блокироваться."
            )
    else:
        warnings = list(strategy_diagnostics.get("warnings") or [])
        if "net_take_profit_no_headroom" in warnings or "net_take_profit_below_target_buffer" in warnings:
            focus.append(
                "У стратегии слишком маленький запас по чистой цели после комиссии; "
                f"рекомендуемый take-profit сейчас {strategy_diagnostics.get('recommended_take_profit_bps', '—')} bps."
            )
    if int(today.get("trade_count", 0) or 0) <= 0:
        focus.append("Сегодня нет закрытых paper-сделок.")
    if active_guards:
        guard_summary = ", ".join(
            f"{item.get('ticker')}({','.join(str(reason_name) for reason_name in list(item.get('reasons') or []))})"
            for item in active_guards[:3]
        )
        focus.append(f"Intraday ticker-guard сейчас удерживает новые входы: {guard_summary}.")
        if any(
            "ticker_consecutive_time_stop_losses_limit" in list(item.get("reasons") or [])
            for item in active_guards
        ):
            focus.append("Отдельный intraday-guard уже режет тикеры с серией убыточных time-stop выходов.")
    if active_session_guards:
        session_guard_summary = ", ".join(
            f"{item.get('reason')}({item.get('guarded_tickers')}/{item.get('max_guarded_tickers')})"
            for item in active_session_guards
        )
        focus.append(f"Сессия поставлена на паузу по session-guard: {session_guard_summary}.")
    active_ticker_hours = list(active_restrictions.get("blocked_ticker_hours") or [])
    if active_ticker_hours:
        focus.append(
            "Активные точечные restrictions по ticker+hour: "
            + ", ".join(str(item) for item in active_ticker_hours[:3])
            + "."
        )
    if analysis.get("assessment") == "insufficient_sample":
        focus.append("Trade sample пока недостаточен для устойчивых выводов.")

    coverage = optimizer.get("signal_coverage") or {}
    if coverage:
        blocked = coverage.get("top_blocked_reasons") or []
        if blocked:
            focus.append(
                "Главный блокер сигналов: "
                + ", ".join(f"{item['reason']}={item['count']}" for item in blocked[:2])
                + "."
            )
        if coverage.get("signal_ready_rate_pct") is not None:
            focus.append(f"Signal-ready rate внутри окна: {coverage['signal_ready_rate_pct']}%.")

    for item in list(analysis.get("focus") or [])[:2]:
        message = item.get("message")
        if message:
            focus.append(str(message))
    for item in list(research.get("focus") or [])[:2]:
        message = item.get("message")
        if message:
            focus.append(str(message))
    return focus[:6]


def build_headline(payload: dict[str, Any]) -> str:
    today = payload.get("today") or {}
    analysis = payload.get("analysis") or {}
    optimizer = payload.get("optimizer") or {}
    research = payload.get("research") or {}
    doctor = payload.get("doctor") or {}
    governance = payload.get("governance") or {}
    active_experiment = governance.get("active_experiment") or {}
    watchdog = payload.get("watchdog") or {}
    strategy_diagnostics = payload.get("strategy_diagnostics") or {}
    risk_controls = payload.get("risk_controls") or {}
    active_guards = list(risk_controls.get("active_ticker_guards") or [])

    if watchdog.get("status") not in {None, "healthy"}:
        return f"Watchdog status {watchdog.get('status')}: контур требует внимания."
    if doctor.get("status") == "error":
        return "Doctor не подтвердил готовность API или watchlist; перед сессией нужен ручной разбор."
    if governance.get("applied"):
        return "Nightly governor применил изменения и ожидает один рестарт paper-сервиса."
    if (governance.get("post_change_guard") or {}).get("active"):
        return "Nightly governor ждет новый post-change sample перед следующей авто-правкой."
    if active_experiment.get("status") == "negative_expectancy_so_far":
        return "Последний auto-change пока не подтверждает улучшение: post-change sample остается отрицательным."
    if active_experiment.get("status") == "positive_expectancy_so_far":
        return "Последний auto-change пока выглядит полезным: post-change sample положительный."
    if not strategy_diagnostics.get("viable_for_entry", True):
        warnings = set(str(item) for item in strategy_diagnostics.get("warnings") or [])
        if "min_expected_edge_above_take_profit" in warnings:
            return "Конфиг стратегии блокирует новые входы: expected-edge floor выше take-profit."
        return "Конфиг стратегии блокирует новые входы: нужно поднять take-profit или ослабить net-floor."
    if not strategy_diagnostics.get("target_headroom_met", True):
        return (
            "У стратегии слишком маленький запас по чистой цели после комиссии; "
            f"минимально безопасный take-profit сейчас {strategy_diagnostics.get('recommended_take_profit_bps', '—')} bps."
        )
    if optimizer.get("status") == "no_entry_window_data" and research.get("status") == "no_entry_window_data":
        return "В market-history пока нет валидного in-window sample для optimizer/research."
    if int(today.get("trade_count", 0) or 0) <= 0:
        return "Сегодня закрытых paper-сделок нет; продолжаем сбор сигнала и market-data."
    if active_guards:
        return (
            "Intraday ticker-guard уже сработал по части тикеров; "
            "остальные инструменты продолжают paper-торговлю."
        )
    return (
        f"Сегодня {today.get('trade_count', 0)} сделок, net PnL {today.get('net_pnl_rub', '0')} RUB, "
        f"analysis={analysis.get('assessment', 'n/a')}."
    )


def build_next_action(payload: dict[str, Any]) -> str:
    today = payload.get("today") or {}
    analysis = payload.get("analysis") or {}
    optimizer = payload.get("optimizer") or {}
    research = payload.get("research") or {}
    doctor = payload.get("doctor") or {}
    tuning = payload.get("tuning") or {}
    restrictions = payload.get("restrictions") or {}
    governance = payload.get("governance") or {}
    active_experiment = governance.get("active_experiment") or {}
    watchdog = payload.get("watchdog") or {}
    strategy_diagnostics = payload.get("strategy_diagnostics") or {}

    if watchdog.get("restart_required"):
        return "inspect_watchdog_and_runtime"
    if doctor.get("status") == "error":
        return "inspect_api_access"
    if governance.get("next_action") not in {None, "no_change"}:
        return str(governance.get("next_action"))
    if active_experiment.get("next_action") not in {None, "wait_for_next_apply", "collect_post_change_sample"}:
        return str(active_experiment.get("next_action"))
    if not strategy_diagnostics.get("viable_for_entry", True):
        return resolve_strategy_config_next_action(strategy_diagnostics)
    if not strategy_diagnostics.get("target_headroom_met", True):
        return resolve_strategy_config_next_action(strategy_diagnostics)
    if doctor.get("next_action") not in {None, "none", "review_strategy_headroom"}:
        return str(doctor.get("next_action"))
    if watchdog.get("next_action") not in {None, "none"}:
        return str(watchdog.get("next_action"))
    if optimizer.get("status") == "no_entry_window_data" or research.get("status") == "no_entry_window_data":
        return "collect_in_window_market_data"
    if analysis.get("assessment") == "insufficient_sample":
        return "collect_more_paper_trades"
    if tuning.get("next_action") not in {None, "no_change", "wait_for_better_optimizer_candidate"}:
        return str(tuning.get("next_action"))
    if restrictions.get("next_action") not in {None, "no_change", "no_restrictions_needed"}:
        return str(restrictions.get("next_action"))
    if int(today.get("trade_count", 0) or 0) <= 0:
        return "continue_paper_collection"
    if analysis.get("assessment") == "negative_expectancy_so_far":
        return "review_weak_tickers_and_hours"
    return "continue_collecting_and_review_daily"


def write_summary_report(runtime_dir: Path, payload: dict[str, Any]) -> None:
    summary_dir = runtime_dir / "summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    latest_path = summary_dir / "latest.json"
    history_path = summary_dir / "history.jsonl"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    latest_path.write_text(body, encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
