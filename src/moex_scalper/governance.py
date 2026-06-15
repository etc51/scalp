from __future__ import annotations

import json
from datetime import datetime, timezone
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
    state_payload = _load_json(config.runtime_dir / "dashboard_state.json")

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
    evidence = build_evidence_snapshot(
        state_payload=state_payload,
        analysis_payload=analysis_payload,
        optimizer_payload=optimizer_payload,
        research_payload=research_payload,
    )
    last_applied_change = load_last_applied_governance_change(config.runtime_dir)
    post_change_guard = build_post_change_guard(
        evidence=evidence,
        last_applied=last_applied_change,
    )

    tuning_ready = tuning_preview.get("decision") == "ready_to_apply"
    restrictions_ready = restrictions_preview.get("decision") == "ready_to_apply"
    selected_action, selection_reason, action_scores = choose_governor_action(
        tuning_preview=tuning_preview,
        tuning_ready=tuning_ready,
        restrictions_preview=restrictions_preview,
        restrictions_ready=restrictions_ready,
        post_change_guard=post_change_guard,
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
    candidate_actions = []
    if tuning_ready:
        candidate_actions.append("tuning")
    if restrictions_ready:
        candidate_actions.append("restrictions")
    ready_actions = [
        action
        for action, details in action_scores.items()
        if details.get("effective_ready", False)
    ]
    blocked_ready_actions = [
        action
        for action, details in action_scores.items()
        if details.get("ready") and not details.get("effective_ready", False)
    ]
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
        "evidence": evidence,
        "post_change_guard": post_change_guard,
        "action_scores": action_scores,
        "selected_action": selected_action,
        "selection_reason": selection_reason,
        "decision": build_decision(
            apply=apply,
            applied_any=applied_any,
            ready_actions=ready_actions,
            blocked_ready_actions=blocked_ready_actions,
        ),
        "candidate_actions": candidate_actions,
        "ready_actions": ready_actions,
        "blocked_ready_actions": blocked_ready_actions,
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
            post_change_guard=post_change_guard,
        ),
    }
    if write_report or applied_any:
        write_governance_report(config.runtime_dir, payload)
    return payload


def build_decision(
    *,
    apply: bool,
    applied_any: bool,
    ready_actions: list[str],
    blocked_ready_actions: list[str],
) -> str:
    if applied_any:
        return "applied"
    if blocked_ready_actions:
        return "guard_wait"
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
    post_change_guard: dict[str, Any],
) -> str:
    if applied_any:
        return "restart_paper_service"
    if post_change_guard.get("active"):
        return "collect_post_change_sample"
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
    post_change_guard: dict[str, Any],
) -> tuple[str | None, str, dict[str, Any]]:
    action_scores = build_action_scores(
        tuning_preview=tuning_preview,
        tuning_ready=tuning_ready,
        restrictions_preview=restrictions_preview,
        restrictions_ready=restrictions_ready,
        post_change_guard=post_change_guard,
    )
    if not tuning_ready and not restrictions_ready:
        return None, "no_ready_actions", action_scores

    ready_items = [
        (action, details)
        for action, details in action_scores.items()
        if details.get("effective_ready")
    ]
    if not ready_items and post_change_guard.get("active"):
        return None, str(post_change_guard.get("reason") or "post_change_guard_active"), action_scores
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
    post_change_guard: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tuning": score_tuning_action(
            tuning_preview,
            ready=tuning_ready,
            post_change_guard=post_change_guard,
        ),
        "restrictions": score_restrictions_action(
            restrictions_preview,
            ready=restrictions_ready,
            post_change_guard=post_change_guard,
        ),
    }


def score_tuning_action(
    payload: dict[str, Any],
    *,
    ready: bool,
    post_change_guard: dict[str, Any],
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "ready": ready,
        "effective_ready": ready,
        "guard_blocked": False,
        "guard_reason": None,
        "score": 0.0,
        "selection_reason": None,
        "score_components": [],
        "scope_penalty": 0,
        "tie_break_rank": 1,
    }
    if not ready:
        details["selection_reason"] = "not_ready"
        return details
    if post_change_guard.get("active"):
        details["effective_ready"] = False
        details["guard_blocked"] = True
        details["guard_reason"] = str(post_change_guard.get("reason") or "await_post_change_sample")
        details["selection_reason"] = "await_post_change_sample"
        details["score_components"].append(
            "post_change_guard="
            + str(post_change_guard.get("reason") or "await_post_change_sample")
        )
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


def score_restrictions_action(
    payload: dict[str, Any],
    *,
    ready: bool,
    post_change_guard: dict[str, Any],
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "ready": ready,
        "effective_ready": ready,
        "guard_blocked": False,
        "guard_reason": None,
        "score": 0.0,
        "selection_reason": None,
        "score_components": [],
        "scope_penalty": 0,
        "tie_break_rank": 2,
    }
    if not ready:
        details["selection_reason"] = "not_ready"
        return details
    if post_change_guard.get("active"):
        details["effective_ready"] = False
        details["guard_blocked"] = True
        details["guard_reason"] = str(post_change_guard.get("reason") or "await_post_change_sample")
        details["selection_reason"] = "await_post_change_sample"
        details["score_components"].append(
            "post_change_guard="
            + str(post_change_guard.get("reason") or "await_post_change_sample")
        )
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


def build_evidence_snapshot(
    *,
    state_payload: dict[str, Any] | None,
    analysis_payload: dict[str, Any] | None,
    optimizer_payload: dict[str, Any] | None,
    research_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    stats = ((state_payload or {}).get("stats") or {})
    today_stats = stats.get("today") or {}
    overall_stats = stats.get("overall") or {}
    market_history = (state_payload or {}).get("market_history") or {}
    analysis_summary = ((analysis_payload or {}).get("summary") or {})
    return {
        "overall_trade_count": _int(overall_stats.get("trade_count")),
        "today_trade_count": _int(today_stats.get("trade_count")),
        "recorded_snapshots_total": _int(market_history.get("recorded_snapshots_total")),
        "recorded_snapshots_today": _int(market_history.get("recorded_snapshots_today")),
        "analysis_trade_count_window": _int(analysis_summary.get("trade_count")),
        "optimizer_snapshot_count_window": _int((optimizer_payload or {}).get("snapshot_count")),
        "optimizer_raw_snapshot_count_window": _int((optimizer_payload or {}).get("raw_snapshot_count")),
        "research_snapshot_count_window": _int((research_payload or {}).get("snapshot_count")),
    }


def load_last_applied_governance_change(runtime_dir: Path) -> dict[str, Any] | None:
    history_path = runtime_dir / "governance" / "history.jsonl"
    if not history_path.exists():
        return None
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("applied") and list(payload.get("applied_actions") or []):
            return payload
    return None


def build_post_change_guard(
    *,
    evidence: dict[str, Any],
    last_applied: dict[str, Any] | None,
) -> dict[str, Any]:
    min_new_trades = int(_env_value("SCALPER_GOVERNOR_MIN_NEW_TRADES_AFTER_CHANGE", "6") or 6)
    min_new_snapshots = int(_env_value("SCALPER_GOVERNOR_MIN_NEW_SNAPSHOTS_AFTER_CHANGE", "500") or 500)
    payload: dict[str, Any] = {
        "active": False,
        "reason": None,
        "last_applied_at": None,
        "last_applied_actions": [],
        "age_hours": None,
        "min_new_trades": min_new_trades,
        "min_new_snapshots": min_new_snapshots,
        "current_trade_count": None,
        "last_trade_count": None,
        "trade_delta": None,
        "enough_trade_growth": False,
        "current_snapshot_count": None,
        "last_snapshot_count": None,
        "snapshot_delta": None,
        "enough_snapshot_growth": False,
        "comparable": False,
    }
    if last_applied is None:
        payload["reason"] = "no_prior_applied_change"
        return payload

    payload["last_applied_at"] = last_applied.get("generated_at")
    payload["last_applied_actions"] = list(last_applied.get("applied_actions") or [])
    payload["age_hours"] = _age_hours(last_applied.get("generated_at"))

    last_evidence = extract_evidence_snapshot(last_applied)
    current_trade_count = _coalesce_int(
        evidence.get("overall_trade_count"),
        evidence.get("analysis_trade_count_window"),
    )
    last_trade_count = _coalesce_int(
        last_evidence.get("overall_trade_count"),
        last_evidence.get("analysis_trade_count_window"),
    )
    current_snapshot_count = _coalesce_int(
        evidence.get("recorded_snapshots_total"),
        evidence.get("optimizer_snapshot_count_window"),
        evidence.get("research_snapshot_count_window"),
    )
    last_snapshot_count = _coalesce_int(
        last_evidence.get("recorded_snapshots_total"),
        last_evidence.get("optimizer_snapshot_count_window"),
        last_evidence.get("research_snapshot_count_window"),
    )

    payload["current_trade_count"] = current_trade_count
    payload["last_trade_count"] = last_trade_count
    payload["current_snapshot_count"] = current_snapshot_count
    payload["last_snapshot_count"] = last_snapshot_count

    if current_trade_count is not None and last_trade_count is not None:
        trade_delta = current_trade_count - last_trade_count
        payload["trade_delta"] = trade_delta
        payload["enough_trade_growth"] = trade_delta >= min_new_trades
    if current_snapshot_count is not None and last_snapshot_count is not None:
        snapshot_delta = current_snapshot_count - last_snapshot_count
        payload["snapshot_delta"] = snapshot_delta
        payload["enough_snapshot_growth"] = snapshot_delta >= min_new_snapshots

    payload["comparable"] = (
        payload["trade_delta"] is not None
        or payload["snapshot_delta"] is not None
    )
    if not payload["comparable"]:
        payload["reason"] = "no_prior_evidence_available"
        return payload
    if payload["enough_trade_growth"] or payload["enough_snapshot_growth"]:
        payload["reason"] = "fresh_sample_available"
        return payload

    payload["active"] = True
    payload["reason"] = "await_post_change_sample"
    return payload


def extract_evidence_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    tuning = ((payload.get("tuning") or {}).get("result") or {})
    restrictions = ((payload.get("restrictions") or {}).get("result") or {})
    evidence = dict(payload.get("evidence") or {})
    tuning_analysis = dict(tuning.get("analysis") or {})
    restrictions_analysis = dict(restrictions.get("analysis") or {})
    tuning_coverage = dict(tuning.get("coverage_fallback") or {})
    restrictions_optimizer = dict(restrictions.get("optimizer") or {})
    research = dict(tuning.get("research") or {})
    return {
        "overall_trade_count": _coalesce_int(
            evidence.get("overall_trade_count"),
            tuning_analysis.get("trade_count"),
            restrictions_analysis.get("trade_count"),
        ),
        "analysis_trade_count_window": _coalesce_int(
            evidence.get("analysis_trade_count_window"),
            tuning_analysis.get("trade_count"),
            restrictions_analysis.get("trade_count"),
        ),
        "recorded_snapshots_total": _coalesce_int(
            evidence.get("recorded_snapshots_total"),
            evidence.get("optimizer_snapshot_count_window"),
            restrictions_optimizer.get("snapshot_count"),
            tuning_coverage.get("snapshot_count"),
            research.get("snapshot_count"),
        ),
        "optimizer_snapshot_count_window": _coalesce_int(
            evidence.get("optimizer_snapshot_count_window"),
            restrictions_optimizer.get("snapshot_count"),
            tuning_coverage.get("snapshot_count"),
        ),
        "research_snapshot_count_window": _coalesce_int(
            evidence.get("research_snapshot_count_window"),
            research.get("snapshot_count"),
        ),
    }


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


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _fmt_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001")))


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _coalesce_int(*values: Any) -> int | None:
    for value in values:
        parsed = _int(value)
        if parsed is not None:
            return parsed
    return None


def _age_hours(value: Any) -> float | None:
    if not value:
        return None
    timestamp = str(value).strip()
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return round(delta.total_seconds() / 3600, 3)
