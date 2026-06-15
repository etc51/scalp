from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .analysis import analyze_trades
from .config import ScalperConfig
from .optimizer import optimize_parameters
from .research import build_indicator_research
from .restrictions import build_restrictions
from .tuning import tune_parameters


def run_governor(
    config: ScalperConfig,
    *,
    apply: bool,
    write_report: bool,
    env_path: str = ".env",
) -> dict[str, Any]:
    analysis_days = int(_env_value("SCALPER_ANALYSIS_DAYS", "5"))
    analysis_top = int(_env_value("SCALPER_ANALYSIS_TOP", "5"))
    optimizer_days = int(_env_value("SCALPER_OPTIMIZER_DAYS", "5"))
    optimizer_min_trades = int(_env_value("SCALPER_OPTIMIZER_MIN_TRADES", "5"))
    research_days = int(_env_value("SCALPER_RESEARCH_DAYS", "5"))
    research_top = int(_env_value("SCALPER_RESEARCH_TOP", "5"))

    analysis_payload = analyze_trades(
        config,
        date_key=None,
        input_path=None,
        top_n=analysis_top,
        days=analysis_days,
        write_report=True,
    )
    optimizer_payload = optimize_parameters(
        config,
        date_key=None,
        input_path=None,
        top_n=10,
        days=optimizer_days,
        min_trades=optimizer_min_trades,
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

    tuning_preview = tune_parameters(
        config,
        apply=False,
        write_report=False,
        env_path=env_path,
    )
    restrictions_preview = build_restrictions(
        config,
        apply=False,
        write_report=False,
    )

    tuning_ready = tuning_preview.get("decision") == "ready_to_apply"
    restrictions_ready = restrictions_preview.get("decision") == "ready_to_apply"

    tuning_result = tuning_preview
    restrictions_result = restrictions_preview
    tuning_applied = False
    restrictions_applied = False

    if apply and tuning_ready:
        tuning_result = tune_parameters(
            config,
            apply=True,
            write_report=True,
            env_path=env_path,
        )
        tuning_applied = bool(tuning_result.get("applied"))

    if apply and restrictions_ready:
        restrictions_result = build_restrictions(
            config,
            apply=True,
            write_report=True,
        )
        restrictions_applied = bool(restrictions_result.get("applied"))

    applied_any = tuning_applied or restrictions_applied
    ready_actions = []
    if tuning_ready:
        ready_actions.append("tuning")
    if restrictions_ready:
        ready_actions.append("restrictions")

    payload = {
        "status": "ok",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": config.mode,
        "apply_requested": apply,
        "applied": applied_any,
        "decision": build_decision(
            apply=apply,
            applied_any=applied_any,
            ready_actions=ready_actions,
        ),
        "ready_actions": ready_actions,
        "applied_actions": [
            action
            for action, applied_flag in (
                ("tuning", tuning_applied),
                ("restrictions", restrictions_applied),
            )
            if applied_flag
        ],
        "service_restart_required": applied_any,
        "pipeline": {
            "analysis_status": analysis_payload.get("status"),
            "optimizer_status": optimizer_payload.get("status"),
            "research_status": research_payload.get("status"),
        },
        "tuning": {
            "preview": tuning_preview,
            "result": tuning_result,
            "ready": tuning_ready,
            "applied": tuning_applied,
        },
        "restrictions": {
            "preview": restrictions_preview,
            "result": restrictions_result,
            "ready": restrictions_ready,
            "applied": restrictions_applied,
        },
        "next_action": build_next_action(
            apply=apply,
            applied_any=applied_any,
            tuning=tuning_result,
            tuning_ready=tuning_ready,
            restrictions=restrictions_result,
            restrictions_ready=restrictions_ready,
        ),
    }
    if write_report or applied_any:
        write_governance_report(config.runtime_dir, payload)
    return payload


def build_decision(*, apply: bool, applied_any: bool, ready_actions: list[str]) -> str:
    if applied_any:
        return "applied"
    if apply and not ready_actions:
        return "skipped"
    if not apply and ready_actions:
        return "ready_to_apply"
    return "preview_skipped"


def build_next_action(
    *,
    apply: bool,
    applied_any: bool,
    tuning: dict[str, Any],
    tuning_ready: bool,
    restrictions: dict[str, Any],
    restrictions_ready: bool,
) -> str:
    if applied_any:
        return "restart_paper_service"
    if not apply and (tuning_ready or restrictions_ready):
        return "governance_candidate_ready"
    tuning_next = str(tuning.get("next_action") or "no_change")
    if tuning_next not in {"no_change", "wait_for_better_optimizer_candidate"}:
        return tuning_next
    restrictions_next = str(restrictions.get("next_action") or "no_change")
    if restrictions_next not in {"no_change", "no_restrictions_needed"}:
        return restrictions_next
    return "no_change"


def write_governance_report(runtime_dir: Path, payload: dict[str, Any]) -> None:
    governance_dir = runtime_dir / "governance"
    governance_dir.mkdir(parents=True, exist_ok=True)
    latest_path = governance_dir / "latest.json"
    history_path = governance_dir / "history.jsonl"
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    latest_path.write_text(body, encoding="utf-8")
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _env_value(key: str, default: str | None = None) -> str | None:
    return __import__("os").getenv(key, default)
