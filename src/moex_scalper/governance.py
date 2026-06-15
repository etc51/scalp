from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
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
    selected_action, selection_reason, action_scores = choose_governor_action(
        tuning_preview=tuning_preview,
        tuning_ready=tuning_ready,
        restrictions_preview=restrictions_preview,
        restrictions_ready=restrictions_ready,
    )

    tuning_result = tuning_preview
    restrictions_result = restrictions_preview
    tuning_applied = False
    restrictions_applied = False

    if apply and selected_action == "tuning":
        tuning_result = tune_parameters(
            config,
            apply=True,
            write_report=True,
            env_path=env_path,
        )
        tuning_applied = bool(tuning_result.get("applied"))

    if apply and selected_action == "restrictions":
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
    deferred_actions = [
        action
        for action in ready_actions
        if action != selected_action
    ]

    payload = {
        "status": "ok",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": config.mode,
        "apply_requested": apply,
        "applied": applied_any,
        "action_scores": action_scores,
        "selected_action": selected_action,
        "selection_reason": selection_reason,
        "decision": build_decision(
            apply=apply,
            applied_any=applied_any,
            ready_actions=ready_actions,
        ),
        "ready_actions": ready_actions,
        "deferred_actions": deferred_actions,
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


def choose_governor_action(
    *,
    tuning_preview: dict[str, Any],
    tuning_ready: bool,
    restrictions_preview: dict[str, Any],
    restrictions_ready: bool,
) -> tuple[str | None, str, dict[str, Any]]:
    action_scores = build_action_scores(
        tuning_preview=tuning_preview,
        tuning_ready=tuning_ready,
        restrictions_preview=restrictions_preview,
        restrictions_ready=restrictions_ready,
    )
    if not tuning_ready and not restrictions_ready:
        return None, "no_ready_actions", action_scores

    ready_items = [
        (action, details)
        for action, details in action_scores.items()
        if details.get("ready")
    ]
    ranked = sorted(
        ready_items,
        key=lambda item: (
            float(item[1].get("score", 0.0)),
            -int(item[1].get("scope_penalty", 0)),
            int(item[1].get("tie_break_rank", 0)),
        ),
        reverse=True,
    )
    selected_action, selected_details = ranked[0]
    next_best = ranked[1] if len(ranked) > 1 else None
    selection_reason = str(selected_details.get("selection_reason") or "highest_score")
    if next_best is not None:
        selection_reason = (
            f"{selection_reason}; "
            f"{selected_action}={selected_details.get('score')} "
            f"vs {next_best[0]}={next_best[1].get('score')}"
        )
    return selected_action, selection_reason, action_scores


def build_action_scores(
    *,
    tuning_preview: dict[str, Any],
    tuning_ready: bool,
    restrictions_preview: dict[str, Any],
    restrictions_ready: bool,
) -> dict[str, Any]:
    return {
        "tuning": score_tuning_action(tuning_preview, ready=tuning_ready),
        "restrictions": score_restrictions_action(restrictions_preview, ready=restrictions_ready),
    }


def score_tuning_action(payload: dict[str, Any], *, ready: bool) -> dict[str, Any]:
    details: dict[str, Any] = {
        "ready": ready,
        "score": 0.0,
        "selection_reason": None,
        "score_components": [],
        "scope_penalty": 0,
        "tie_break_rank": 1,
    }
    if not ready:
        details["selection_reason"] = "not_ready"
        return details

    score = Decimal("0")
    sources = {
        part.strip()
        for part in str(payload.get("candidate_source") or "").split("+")
        if part.strip()
    }
    diagnostics = dict(payload.get("strategy_diagnostics") or {})
    changed_keys = list(payload.get("changed_keys") or [])
    changed_count = len(changed_keys)

    if not diagnostics.get("viable_for_entry", True):
        score += Decimal("120")
        details["score_components"].append("global_config_block=120")
        details["selection_reason"] = "global_config_unblocker"
    if "headroom_guard" in sources or "optimizer_headroom_guard" in sources:
        score += Decimal("110")
        details["score_components"].append("headroom_guard=110")
        details["selection_reason"] = details["selection_reason"] or "headroom_guard"
    if "coverage_unblocker" in sources:
        score += Decimal("95")
        details["score_components"].append("coverage_unblocker=95")
        details["selection_reason"] = details["selection_reason"] or "coverage_unblocker"
    if "optimizer" in sources:
        score += Decimal("35")
        details["score_components"].append("optimizer_candidate=35")
        delta = _decimal((((payload.get("optimizer") or {}).get("delta_vs_baseline_rub"))), default="0")
        trade_count = int(((payload.get("optimizer") or {}).get("trade_count", 0)) or 0)
        profit_factor = _decimal((((payload.get("optimizer") or {}).get("profit_factor"))), default="0")
        delta_score = min(max(delta, Decimal("0")) / Decimal("20"), Decimal("25"))
        trade_score = min(Decimal(trade_count), Decimal("15"))
        pf_score = min(max(profit_factor - Decimal("1"), Decimal("0")) * Decimal("20"), Decimal("10"))
        score += delta_score + trade_score + pf_score
        details["score_components"].append(f"optimizer_delta={_fmt_decimal(delta_score)}")
        details["score_components"].append(f"optimizer_trades={_fmt_decimal(trade_score)}")
        details["score_components"].append(f"optimizer_pf={_fmt_decimal(pf_score)}")
        details["selection_reason"] = details["selection_reason"] or "optimizer_candidate"
    if "research_regime" in sources:
        score += Decimal("30")
        details["score_components"].append("research_regime=30")
        details["selection_reason"] = details["selection_reason"] or "research_regime"

    scope_penalty = max(0, changed_count - 1) * 6
    combo_penalty = max(0, len(sources) - 1) * 5
    total_penalty = scope_penalty + combo_penalty
    if total_penalty:
        score -= Decimal(total_penalty)
        details["score_components"].append(f"scope_penalty=-{total_penalty}")
    details["scope_penalty"] = total_penalty
    details["score"] = float(round(score, 3))
    if details["selection_reason"] is None:
        details["selection_reason"] = "tuning_ready"
    return details


def score_restrictions_action(payload: dict[str, Any], *, ready: bool) -> dict[str, Any]:
    details: dict[str, Any] = {
        "ready": ready,
        "score": 0.0,
        "selection_reason": None,
        "score_components": [],
        "scope_penalty": 0,
        "tie_break_rank": 2,
    }
    if not ready:
        details["selection_reason"] = "not_ready"
        return details

    score = Decimal("0")
    source = str(payload.get("candidate_source") or "").strip()
    active = dict(payload.get("proposed_restrictions") or {})
    affected_count = len(list(active.get("disabled_tickers") or [])) + len(list(active.get("blocked_entry_hours") or []))
    breakdown = dict(payload.get("candidate_breakdown") or {})

    if source == "analysis":
        score += Decimal("70")
        details["score_components"].append("analysis_source=70")
        details["selection_reason"] = "analysis_restriction"
        trade_count = int(((payload.get("analysis") or {}).get("trade_count", 0)) or 0)
        trade_score = min(Decimal(trade_count), Decimal("20"))
        score += trade_score
        details["score_components"].append(f"analysis_trades={_fmt_decimal(trade_score)}")
        total_loss = sum(
            max(-_decimal(item.get("net_pnl_rub"), default="0"), Decimal("0"))
            for item in list(breakdown.get("tickers") or []) + list(breakdown.get("hours") or [])
        )
        loss_score = min(total_loss / Decimal("100"), Decimal("15"))
        score += loss_score
        details["score_components"].append(f"analysis_loss={_fmt_decimal(loss_score)}")
    elif source == "optimizer_signal_coverage":
        score += Decimal("55")
        details["score_components"].append("coverage_source=55")
        details["selection_reason"] = "coverage_restriction"
        snapshot_count = int(((payload.get("optimizer") or {}).get("snapshot_count", 0)) or 0)
        snapshot_score = min(Decimal(snapshot_count) / Decimal("200"), Decimal("15"))
        score += snapshot_score
        details["score_components"].append(f"coverage_snapshots={_fmt_decimal(snapshot_score)}")
        dominant_share = max(
            (
                _decimal(item.get("dominant_block_share_pct"), default="0")
                for item in list(breakdown.get("tickers") or []) + list(breakdown.get("hours") or [])
            ),
            default=Decimal("0"),
        )
        dominant_score = min(dominant_share / Decimal("10"), Decimal("10"))
        score += dominant_score
        details["score_components"].append(f"coverage_dominant_share={_fmt_decimal(dominant_score)}")
    else:
        details["selection_reason"] = "restrictions_ready"

    scope_penalty = max(0, affected_count - 1) * 8
    if scope_penalty:
        score -= Decimal(scope_penalty)
        details["score_components"].append(f"scope_penalty=-{scope_penalty}")
    details["scope_penalty"] = scope_penalty
    details["score"] = float(round(score, 3))
    return details


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


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _fmt_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001")))
