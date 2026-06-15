from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis import analyze_trades
from .config import ScalperConfig
from .entry_window import moment_in_entry_window
from .research import build_indicator_research
from .summary import build_daily_summary


def run_intraday_research(
    config: ScalperConfig,
    *,
    write_report: bool,
) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    in_window, entry_window_state = moment_in_entry_window(config, now_utc)
    payload: dict[str, Any] = {
        "status": "skipped",
        "generated_at": now_utc.isoformat(),
        "mode": config.mode,
        "entry_window_state": entry_window_state,
        "ran": False,
        "analysis_days": None,
        "analysis_top": None,
        "research_days": None,
        "research_top": None,
        "analysis_status": None,
        "analysis_assessment": None,
        "research_status": None,
        "summary_headline": None,
        "best_strategy_lab_candidate": None,
        "strategy_lab_recommendation": None,
        "best_regime_candidate": None,
        "regime_recommendation": None,
        "next_action": "wait_for_entry_window",
    }

    if config.mode != "paper":
        payload["status"] = "skipped_mode_not_paper"
        payload["next_action"] = "mode_not_paper"
        if write_report:
            write_intraday_report(config.runtime_dir, payload)
        return payload

    if not in_window:
        payload["status"] = "outside_entry_window"
        if write_report:
            write_intraday_report(config.runtime_dir, payload)
        return payload

    analysis_days = _int_env("SCALPER_INTRADAY_ANALYSIS_DAYS", 1)
    analysis_top = _int_env("SCALPER_INTRADAY_ANALYSIS_TOP", 5)
    research_days = _int_env("SCALPER_INTRADAY_RESEARCH_DAYS", 1)
    research_top = _int_env("SCALPER_INTRADAY_RESEARCH_TOP", 5)

    analysis_payload = analyze_trades(
        config,
        date_key=None,
        input_path=None,
        top_n=analysis_top,
        days=analysis_days,
        write_report=True,
    )
    research_payload = build_indicator_research(
        config,
        date_key=None,
        input_path=None,
        top_n=research_top,
        days=research_days,
        write_report=True,
    )
    summary_payload = build_daily_summary(
        config,
        write_report=True,
    )

    research_summary = dict(research_payload.get("summary") or {})
    payload.update(
        {
            "status": "ok",
            "ran": True,
            "analysis_days": analysis_days,
            "analysis_top": analysis_top,
            "research_days": research_days,
            "research_top": research_top,
            "analysis_status": analysis_payload.get("status"),
            "analysis_assessment": analysis_payload.get("assessment"),
            "research_status": research_payload.get("status"),
            "summary_headline": summary_payload.get("headline"),
            "best_strategy_lab_candidate": research_summary.get("best_strategy_lab_candidate"),
            "strategy_lab_recommendation": research_summary.get("strategy_lab_recommendation"),
            "best_regime_candidate": research_summary.get("best_regime_candidate"),
            "regime_recommendation": research_summary.get("regime_recommendation"),
            "next_action": "continue_intraday_collection",
        }
    )
    if write_report:
        write_intraday_report(config.runtime_dir, payload)
    return payload


def write_intraday_report(runtime_dir: Path, payload: dict[str, Any]) -> None:
    intraday_dir = runtime_dir / "intraday"
    intraday_dir.mkdir(parents=True, exist_ok=True)
    latest_path = intraday_dir / "latest.json"
    history_path = intraday_dir / "history.jsonl"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    latest_path.write_text(body, encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)
