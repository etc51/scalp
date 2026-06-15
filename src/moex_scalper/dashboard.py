from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MOEX Scalper Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #08131a;
      --panel: #0f202a;
      --panel-2: #142d3a;
      --line: #264556;
      --text: #e8f0f4;
      --muted: #8ba5b3;
      --good: #51d88a;
      --bad: #ff6b6b;
      --accent: #47c7ff;
      --warn: #ffc857;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background:
        radial-gradient(circle at top right, rgba(71,199,255,.16), transparent 28%),
        linear-gradient(180deg, #071017 0%, #0a1821 100%);
      color: var(--text);
    }
    .wrap {
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px;
    }
    .topbar {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-end;
      margin-bottom: 20px;
    }
    h1 {
      margin: 0;
      font-size: 30px;
      letter-spacing: 0.02em;
    }
    .sub {
      color: var(--muted);
      margin-top: 6px;
    }
    .status {
      padding: 10px 14px;
      border: 1px solid var(--line);
      background: rgba(15,32,42,.72);
      border-radius: 14px;
      min-width: 250px;
      text-align: right;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(8, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }
    .card, .panel {
      border: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(15,32,42,.88), rgba(20,45,58,.86));
      border-radius: 18px;
      box-shadow: 0 18px 50px rgba(0,0,0,.22);
    }
    .card {
      padding: 16px;
      min-height: 108px;
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
    }
    .value {
      font-size: 28px;
      margin-top: 12px;
      font-weight: 700;
    }
    .good { color: var(--good); }
    .bad { color: var(--bad); }
    .warn { color: var(--warn); }
    .layout {
      display: grid;
      grid-template-columns: 1.3fr 1fr;
      gap: 18px;
      margin-bottom: 18px;
    }
    .panel {
      padding: 16px;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 18px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    th, td {
      padding: 10px 8px;
      border-bottom: 1px solid rgba(38,69,86,.7);
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .05em;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .chip {
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(8,19,26,.7);
      font-size: 13px;
    }
    .empty {
      color: var(--muted);
      padding: 10px 0 4px;
    }
    .subhead {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .06em;
      margin: 14px 0 8px;
    }
    .stack-gap {
      margin-top: 14px;
    }
    @media (max-width: 1180px) {
      .grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; }
    }
    @media (max-width: 760px) {
      .wrap { padding: 14px; }
      .topbar { display: block; }
      .status { text-align: left; margin-top: 12px; }
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .value { font-size: 22px; }
      table { display: block; overflow-x: auto; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>MOEX Scalper Dashboard</h1>
        <div class="sub" id="subline">Ожидание первого состояния...</div>
      </div>
      <div class="status">
        <div class="label">Состояние</div>
        <div id="statusText">connecting</div>
      </div>
    </div>

    <div class="grid">
      <div class="card"><div class="label">Mode</div><div class="value" id="mode">-</div></div>
      <div class="card"><div class="label">Cash</div><div class="value" id="cash">-</div></div>
      <div class="card"><div class="label">Market Value</div><div class="value" id="marketValue">-</div></div>
      <div class="card"><div class="label">Equity</div><div class="value" id="equity">-</div></div>
      <div class="card"><div class="label">Realized PnL</div><div class="value" id="realized">-</div></div>
      <div class="card"><div class="label">Unrealized PnL</div><div class="value" id="unrealized">-</div></div>
      <div class="card"><div class="label">Today Trades</div><div class="value" id="todayTrades">-</div></div>
      <div class="card"><div class="label">All-Time Trades</div><div class="value" id="allTrades">-</div></div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Open Positions</h2>
        <div id="positionsWrap"></div>
      </div>
      <div class="panel">
        <h2>Market Watch</h2>
        <div id="marketWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Recent Trades</h2>
        <div id="tradesWrap"></div>
      </div>
      <div class="panel">
        <h2>Blocked Reasons</h2>
        <div class="chips" id="blockedWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Today Summary</h2>
        <div id="todaySummaryWrap"></div>
      </div>
      <div class="panel">
        <h2>All-Time Summary</h2>
        <div id="overallSummaryWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Optimizer Top Candidate</h2>
        <div id="optimizerTopWrap"></div>
      </div>
      <div class="panel">
        <h2>Optimizer Baseline</h2>
        <div id="optimizerBaselineWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Signal Coverage</h2>
        <div id="coverageSummaryWrap"></div>
      </div>
      <div class="panel">
        <h2>Coverage Breakdown</h2>
        <div id="coverageBreakdownWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Readiness & Watchdog</h2>
        <div id="watchdogWrap"></div>
      </div>
      <div class="panel">
        <h2>Current Strategy</h2>
        <div id="strategyWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Entry Restrictions</h2>
        <div id="restrictionsWrap"></div>
      </div>
      <div class="panel">
        <h2>Auto Tune</h2>
        <div id="tuningWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Trade Analysis</h2>
        <div id="analysisSummaryWrap"></div>
      </div>
      <div class="panel">
        <h2>Analysis Focus</h2>
        <div id="analysisFocusWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Nightly Governor</h2>
        <div id="governanceWrap"></div>
      </div>
      <div class="panel">
        <h2>Daily Summary</h2>
        <div id="summaryWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Indicator Research</h2>
        <div id="researchSummaryWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Summary Focus</h2>
        <div id="summaryFocusWrap"></div>
      </div>
      <div class="panel">
        <h2>Research Tickers & Replay</h2>
        <div id="researchTickerWrap"></div>
      </div>
    </div>

    <div class="layout">
      <div class="panel">
        <h2>Ticker Breakdown</h2>
        <div id="analysisTickerWrap"></div>
      </div>
      <div class="panel">
        <h2>Hour Breakdown</h2>
        <div id="analysisHourWrap"></div>
      </div>
    </div>
  </div>

  <script>
    const fmtRub = (value) => {
      if (value === null || value === undefined || value === "") return "—";
      const num = Number(value);
      return num.toLocaleString("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + " RUB";
    };

    const fmtNum = (value, digits = 2) => {
      if (value === null || value === undefined || value === "") return "—";
      return Number(value).toLocaleString("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: digits });
    };

    const pnlClass = (value) => {
      const num = Number(value || 0);
      if (num > 0) return "good";
      if (num < 0) return "bad";
      return "";
    };

    const renderTable = (headers, rows) => {
      if (!rows.length) return '<div class="empty">Пока пусто</div>';
      const thead = headers.map((h) => `<th>${h}</th>`).join("");
      const tbody = rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("");
      return `<table><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`;
    };

    const renderSummary = (summary) => {
      if (!summary) return '<div class="empty">Нет статистики</div>';
      return renderTable(
        ["Metric", "Value"],
        [
          ["Trades", fmtNum(summary.trade_count, 0)],
          ["Win Rate", fmtNum(summary.win_rate_pct, 2) + " %"],
          ["Net PnL", `<span class="${pnlClass(summary.net_pnl_rub)}">${fmtRub(summary.net_pnl_rub)}</span>`],
          ["Gross PnL", `<span class="${pnlClass(summary.gross_pnl_rub)}">${fmtRub(summary.gross_pnl_rub)}</span>`],
          ["Fees", fmtRub(summary.fees_rub)],
          ["Turnover", fmtRub(summary.turnover_rub)],
          ["Avg Hold", fmtNum(summary.average_hold_seconds, 2) + " s"],
          ["Best Trade", `<span class="${pnlClass(summary.best_trade_rub)}">${fmtRub(summary.best_trade_rub)}</span>`],
          ["Worst Trade", `<span class="${pnlClass(summary.worst_trade_rub)}">${fmtRub(summary.worst_trade_rub)}</span>`],
          ["Last Ticker", summary.last_ticker || "—"],
          ["Last Trade", summary.last_trade_at || "—"],
        ],
      );
    };

    const renderOptimizer = (report) => {
      if (!report) return '<div class="empty">Пока нет optimizer-report</div>';
      return renderTable(
        ["Metric", "Value"],
        [
          ["Equity Delta", `<span class="${pnlClass(report.equity_delta_rub)}">${fmtRub(report.equity_delta_rub)}</span>`],
          ["Net PnL", `<span class="${pnlClass(report.net_pnl_rub)}">${fmtRub(report.net_pnl_rub)}</span>`],
          ["Trades", fmtNum(report.trade_count, 0)],
          ["Win Rate", fmtNum(report.win_rate_pct, 2) + " %"],
          ["Signals", fmtNum(report.signals_detected, 0)],
          ["Open Positions", fmtNum(report.open_positions, 0)],
          ["Spread", report.parameters?.max_spread_bps ?? "—"],
          ["Imbalance", report.parameters?.min_imbalance ?? "—"],
          ["Impulse", report.parameters?.min_impulse_bps ?? "—"],
          ["TP / SL", `${report.parameters?.take_profit_bps ?? "—"} / ${report.parameters?.stop_loss_bps ?? "—"}`],
          ["Time Stop", report.parameters?.time_stop_seconds ?? "—"],
          ["Expected Edge", report.parameters?.min_expected_edge_bps ?? "—"],
          ["Net TP Floor", report.parameters?.min_net_take_profit_bps ?? "—"],
          ["Cooldown", report.parameters?.cooldown_seconds ?? "—"],
          ["Profit Factor", fmtNum(report.profit_factor, 2)],
          ["Max Drawdown", fmtRub(report.max_drawdown_rub)],
          ["Score", fmtNum(report.score, 2)],
        ],
      );
    };

    const renderCoverageSummary = (coverage) => {
      const summary = coverage?.summary || null;
      if (!summary) return '<div class="empty">Пока нет signal-coverage</div>';
      return renderTable(
        ["Metric", "Value"],
        [
          ["In-Window Snapshots", fmtNum(summary.snapshot_count, 0)],
          ["Signal Ready", fmtNum(summary.signal_ready_count, 0)],
          ["Ready Rate", fmtNum(summary.signal_ready_rate_pct, 2) + " %"],
          ["Spread Pass", fmtNum(summary.spread_pass_rate_pct, 2) + " %"],
          ["Imbalance Pass", fmtNum(summary.imbalance_pass_rate_pct, 2) + " %"],
          ["Impulse Pass", fmtNum(summary.impulse_pass_rate_pct, 2) + " %"],
          ["Expected Edge Pass", fmtNum(summary.expected_edge_pass_rate_pct, 2) + " %"],
          ["Net TP Pass", fmtNum(summary.net_take_profit_pass_rate_pct, 2) + " %"],
          ["Avg Spread", fmtNum(summary.average_spread_bps, 2) + " bps"],
          ["Avg Imbalance", fmtNum(summary.average_imbalance, 3)],
          ["Avg Impulse", fmtNum(summary.average_impulse_bps, 2) + " bps"],
          ["Top Blocked", (summary.top_blocked_reasons || []).map((item) => `${item.reason}=${item.count}`).join(", ") || "—"],
        ],
      );
    };

    const renderCoverageRanked = (section, label) => {
      if (!section) return '<div class="empty">Нет coverage-data</div>';
      const renderOne = (title, rows) => {
        if (!rows.length) return `<div class="subhead">${title}</div><div class="empty">Пока пусто</div>`;
        return `<div class="subhead">${title}</div>${renderTable(
          [label, "Snapshots", "Ready", "Ready Rate", "Spread", "Imbalance", "Impulse", "Net TP", "Top Blocked"],
          rows.map((item) => [
            item.key,
            fmtNum(item.snapshot_count, 0),
            fmtNum(item.signal_ready_count, 0),
            fmtNum(item.signal_ready_rate_pct, 2) + " %",
            fmtNum(item.spread_pass_rate_pct, 1) + " %",
            fmtNum(item.imbalance_pass_rate_pct, 1) + " %",
            fmtNum(item.impulse_pass_rate_pct, 1) + " %",
            fmtNum(item.net_take_profit_pass_rate_pct, 1) + " %",
            (item.top_blocked_reasons || []).map((reason) => `${reason.reason}=${reason.count}`).join(", ") || "—",
          ]),
        )}`;
      };
      return renderOne("Worst", section.worst || []) + `<div class="stack-gap"></div>` + renderOne("Best", section.best || []);
    };

    const renderCoverageBreakdown = (coverage) => {
      if (!coverage) return '<div class="empty">Пока нет signal-coverage</div>';
      const ticker = renderCoverageRanked(coverage.by_ticker, "Ticker");
      const hour = renderCoverageRanked(coverage.by_hour, "Hour");
      return ticker + `<div class="stack-gap"></div>` + hour;
    };

    const renderWatchdog = (watchdog, doctor) => {
      if (!watchdog && !doctor) return '<div class="empty">Пока нет readiness-report</div>';
      const stateCheck = watchdog.checks?.dashboard_state || {};
      const marketCheck = watchdog.checks?.market_data || {};
      const sessionCheck = watchdog.checks?.paper_session || {};
      const httpCheck = watchdog.checks?.dashboard_http || {};
      const strategyCheck = watchdog.checks?.strategy_config || {};
      const doctorSchedule = doctor?.entry_schedule || {};
      const doctorApi = doctor?.api || {};
      return renderTable(
        ["Metric", "Value"],
        [
          ["Doctor Status", doctor?.status || "—"],
          ["Doctor Action", doctor?.next_action || "—"],
          ["API Reachable", doctorApi.reachable === undefined ? "—" : String(doctorApi.reachable)],
          ["Resolved Instruments", fmtNum((doctorApi.resolved_instruments || []).length, 0)],
          ["Window State", doctorSchedule.state || "—"],
          ["Next Window", doctorSchedule.next_start_at ? `${doctorSchedule.next_start_at} .. ${doctorSchedule.next_end_at || "—"}` : "—"],
          ["Doctor Warnings", (doctor?.warnings || []).join(", ") || "—"],
          ["Doctor Errors", (doctor?.errors || []).join(", ") || "—"],
          ["Status", watchdog?.status || "—"],
          ["Restart Required", watchdog?.restart_required === undefined ? "—" : String(watchdog.restart_required)],
          ["Restart Reasons", (watchdog?.restart_reasons || []).join(", ") || "—"],
          ["Warnings", (watchdog?.warning_reasons || []).join(", ") || "—"],
          ["Uptime", stateCheck.uptime_seconds === null || stateCheck.uptime_seconds === undefined ? "—" : fmtNum(stateCheck.uptime_seconds, 1) + " s"],
          ["State Age", stateCheck.age_seconds === null || stateCheck.age_seconds === undefined ? "—" : fmtNum(stateCheck.age_seconds, 1) + " s"],
          ["Max State Age", stateCheck.max_age_seconds === null || stateCheck.max_age_seconds === undefined ? "—" : fmtNum(stateCheck.max_age_seconds, 0) + " s"],
          ["Market Data Required", marketCheck.required_now === undefined ? "—" : String(marketCheck.required_now)],
          ["Last Market Data", marketCheck.last_received_at || "—"],
          ["Market Data Age", marketCheck.age_seconds === null || marketCheck.age_seconds === undefined ? "—" : fmtNum(marketCheck.age_seconds, 1) + " s"],
          ["Max Market Age", marketCheck.max_age_seconds === null || marketCheck.max_age_seconds === undefined ? "—" : fmtNum(marketCheck.max_age_seconds, 0) + " s"],
          ["Dashboard HTTP", httpCheck.checked ? String(httpCheck.ok) : "skipped"],
          ["Strategy Viable", strategyCheck.viable_for_entry === undefined ? "—" : String(strategyCheck.viable_for_entry)],
          ["Regime Filter", strategyCheck.regime_filter_mode ?? "—"],
          ["Expected Edge Ceiling", strategyCheck.expected_edge_ceiling_bps ?? "—"],
          ["Min Expected Edge", strategyCheck.min_expected_edge_bps ?? "—"],
          ["Net TP After Fees", strategyCheck.configured_net_take_profit_bps ?? "—"],
          ["Net TP Buffer", strategyCheck.net_take_profit_buffer_bps ?? "—"],
          ["Target Net Buffer", strategyCheck.target_net_take_profit_buffer_bps ?? "—"],
          ["Recommended TP", strategyCheck.recommended_take_profit_bps ?? "—"],
          ["Strategy Warnings", (strategyCheck.warnings || []).join(", ") || "—"],
          ["Open Positions", fmtNum(sessionCheck.open_positions || 0, 0)],
          ["Next Action", watchdog?.next_action || doctor?.next_action || "—"],
          ["Updated", watchdog?.generated_at || doctor?.generated_at || "—"],
        ],
      );
    };

    const renderStrategy = (parameters, diagnostics) => {
      if (!parameters) return '<div class="empty">Нет strategy-params</div>';
      const risk = diagnostics?.paper_risk_profile || null;
      return renderTable(
        ["Param", "Value"],
        [
          ["Risk Stage", risk?.stage ?? "—"],
          ["Max Gross Leverage", risk?.max_gross_leverage ? `${risk.max_gross_leverage}x` : "—"],
          ["Margin Policy", risk?.margin_policy ?? "—"],
          ["Leverage Decision", risk?.decision ?? "—"],
          ["Promotion Rule", risk?.promotion_rule ?? "—"],
          ["Rollback Rule", risk?.rollback_rule ?? "—"],
          ["Max Spread", parameters.max_spread_bps ?? "—"],
          ["Min Imbalance", parameters.min_imbalance ?? "—"],
          ["Min Impulse", parameters.min_impulse_bps ?? "—"],
          ["Take Profit", parameters.take_profit_bps ?? "—"],
          ["Stop Loss", parameters.stop_loss_bps ?? "—"],
          ["Time Stop", parameters.time_stop_seconds ?? "—"],
          ["Expected Edge", parameters.min_expected_edge_bps ?? "—"],
          ["Net TP Floor", parameters.min_net_take_profit_bps ?? "—"],
          ["Regime Filter", diagnostics?.regime_filter_mode ?? "—"],
          ["Expected Edge Ceiling", diagnostics?.expected_edge_ceiling_bps ?? "—"],
          ["Roundtrip Fee", diagnostics?.premium_roundtrip_commission_bps ?? "—"],
          ["Net TP After Fees", diagnostics?.configured_net_take_profit_bps ?? "—"],
          ["Net TP Buffer", diagnostics?.net_take_profit_buffer_bps ?? "—"],
          ["Target Net Buffer", diagnostics?.target_net_take_profit_buffer_bps ?? "—"],
          ["Recommended TP", diagnostics?.recommended_take_profit_bps ?? "—"],
          ["Viable For Entry", diagnostics?.viable_for_entry === undefined ? "—" : String(diagnostics.viable_for_entry)],
          ["Warnings", (diagnostics?.warnings || []).join(", ") || "—"],
          ["Cooldown", parameters.cooldown_seconds ?? "—"],
        ],
      );
    };

    const renderTuning = (tuning) => {
      if (!tuning) return '<div class="empty">Пока нет tuning-report</div>';
      return renderTable(
        ["Metric", "Value"],
        [
          ["Enabled", String(tuning.enabled)],
          ["Apply Requested", String(tuning.apply_requested)],
          ["Applied", String(tuning.applied)],
          ["Decision", tuning.decision || "—"],
          ["Next Action", tuning.next_action || "—"],
          ["Reasons", (tuning.reasons || []).join(", ") || "—"],
          ["Candidate Source", tuning.candidate_source || "—"],
          ["Regime Before", tuning.current_regime_filter_mode || "—"],
          ["Regime Candidate", tuning.candidate_regime_filter_mode || "—"],
          ["Regime After", tuning.regime_filter_mode_after || "—"],
          ["Open Positions", fmtNum(tuning.open_positions, 0)],
          ["Analysis Trades", fmtNum(tuning.analysis?.trade_count || 0, 0)],
          ["Analysis Assessment", tuning.analysis?.assessment || "—"],
          ["Optimizer Reason", tuning.optimizer?.reason || "—"],
          ["Delta vs Baseline", fmtRub(tuning.optimizer?.delta_vs_baseline_rub)],
          ["Coverage Reason", tuning.coverage_fallback?.reason || "—"],
          ["Coverage Blocker", tuning.coverage_fallback?.dominant_block_reason || "—"],
          ["Coverage Ready Rate", tuning.coverage_fallback?.signal_ready_rate_pct || "—"],
          ["Research Reason", tuning.research?.recommendation_reason || tuning.research?.status || "—"],
          ["Research Delta", fmtRub(tuning.research?.delta_vs_baseline_rub)],
          ["Recommended TP", tuning.headroom_guard?.recommended_take_profit_bps || "—"],
          ["Changed Keys", (tuning.changed_keys || []).join(", ") || "—"],
          ["Updated", tuning.generated_at || "—"],
        ],
      );
    };

    const renderRestrictions = (restrictions, activeRestrictions) => {
      const active = restrictions?.active_restrictions || activeRestrictions || {};
      const proposed = restrictions?.proposed_restrictions || {};
      const activeTickers = active.disabled_tickers || [];
      const activeHours = active.blocked_entry_hours || [];
      const proposedTickers = proposed.disabled_tickers || [];
      const proposedHours = proposed.blocked_entry_hours || [];
      if (!restrictions && !activeTickers.length && !activeHours.length) {
        return '<div class="empty">Пока нет restrictions-report</div>';
      }
      return renderTable(
        ["Metric", "Value"],
        [
          ["Enabled", restrictions ? String(restrictions.enabled) : "—"],
          ["Apply Requested", restrictions ? String(restrictions.apply_requested) : "—"],
          ["Applied", restrictions ? String(restrictions.applied) : "—"],
          ["Decision", restrictions?.decision || "—"],
          ["Next Action", restrictions?.next_action || "—"],
          ["Candidate Source", restrictions?.candidate_source || "—"],
          ["Reasons", (restrictions?.reasons || []).join(", ") || "—"],
          ["Active Tickers", activeTickers.join(", ") || "—"],
          ["Active Hours", activeHours.map((hour) => `${hour}:00`).join(", ") || "—"],
          ["Proposed Tickers", proposedTickers.join(", ") || "—"],
          ["Proposed Hours", proposedHours.map((hour) => `${hour}:00`).join(", ") || "—"],
          ["Clears Existing", restrictions ? String(restrictions.clears_existing_restrictions) : "—"],
          ["Analysis Trades", fmtNum(restrictions?.analysis?.trade_count || 0, 0)],
          ["Analysis Assessment", restrictions?.analysis?.assessment || "—"],
          ["Optimizer Status", restrictions?.optimizer?.status || "—"],
          ["Coverage Max Ready", restrictions?.coverage_fallback?.max_ready_rate_pct || "—"],
          ["Updated", restrictions?.generated_at || active.updated_at || "—"],
        ],
      );
    };

    const renderGovernance = (governance) => {
      if (!governance) return '<div class="empty">Пока нет governance-report</div>';
      return renderTable(
        ["Metric", "Value"],
        [
          ["Decision", governance.decision || "—"],
          ["Next Action", governance.next_action || "—"],
          ["Apply Requested", String(governance.apply_requested)],
          ["Applied", String(governance.applied)],
          ["Selected Action", governance.selected_action || "—"],
          ["Selection Reason", governance.selection_reason || "—"],
          ["Candidate Actions", (governance.candidate_actions || []).join(", ") || "—"],
          ["Tuning Score", governance.action_scores?.tuning?.score ?? "—"],
          ["Restrictions Score", governance.action_scores?.restrictions?.score ?? "—"],
          ["Ready Actions", (governance.ready_actions || []).join(", ") || "—"],
          ["Blocked Ready Actions", (governance.blocked_ready_actions || []).join(", ") || "—"],
          ["Deferred Actions", (governance.deferred_actions || []).join(", ") || "—"],
          ["Applied Actions", (governance.applied_actions || []).join(", ") || "—"],
          ["Post-Change Guard", governance.post_change_guard?.active === undefined ? "—" : String(governance.post_change_guard.active)],
          ["Guard Reason", governance.post_change_guard?.reason || "—"],
          ["Guard Last Applied", governance.post_change_guard?.last_applied_at || "—"],
          ["Guard Trade Delta", governance.post_change_guard?.trade_delta ?? "—"],
          ["Guard Snapshot Delta", governance.post_change_guard?.snapshot_delta ?? "—"],
          ["Restart Required", String(governance.service_restart_required)],
          ["Analysis Status", governance.pipeline?.analysis_status || "—"],
          ["Optimizer Status", governance.pipeline?.optimizer_status || "—"],
          ["Research Status", governance.pipeline?.research_status || "—"],
          ["Tuning Decision", governance.tuning?.result?.decision || governance.tuning?.preview?.decision || "—"],
          ["Restrictions Decision", governance.restrictions?.result?.decision || governance.restrictions?.preview?.decision || "—"],
          ["Updated", governance.generated_at || "—"],
        ],
      );
    };

    const renderAnalysisSummary = (analysis) => {
      if (!analysis || !analysis.summary) return '<div class="empty">Пока нет analysis-report</div>';
      const summary = analysis.summary;
      return renderTable(
        ["Metric", "Value"],
        [
          ["Window", `${analysis.window?.start_date || "—"} .. ${analysis.window?.end_date || "—"}`],
          ["Days With Trades", fmtNum(analysis.window?.days_with_trades || 0, 0)],
          ["Trades", fmtNum(summary.trade_count, 0)],
          ["Win Rate", fmtNum(summary.win_rate_pct, 2) + " %"],
          ["Net PnL", `<span class="${pnlClass(summary.net_pnl_rub)}">${fmtRub(summary.net_pnl_rub)}</span>`],
          ["Expectancy", `<span class="${pnlClass(summary.expectancy_rub)}">${fmtRub(summary.expectancy_rub)}</span>`],
          ["Profit Factor", fmtNum(summary.profit_factor, 2)],
          ["Avg Win", `<span class="${pnlClass(summary.average_win_rub)}">${fmtRub(summary.average_win_rub)}</span>`],
          ["Avg Loss", `<span class="${pnlClass(summary.average_loss_rub)}">${fmtRub(summary.average_loss_rub)}</span>`],
          ["Median Trade", `<span class="${pnlClass(summary.median_trade_rub)}">${fmtRub(summary.median_trade_rub)}</span>`],
          ["Avg Hold", fmtNum(summary.average_hold_seconds, 2) + " s"],
          ["Assessment", analysis.assessment || "—"],
        ],
      );
    };

    const renderResearchSummary = (research) => {
      const summary = research?.summary || null;
      if (!summary) return '<div class="empty">Пока нет research-report</div>';
      const regimeCandidate = summary?.best_regime_candidate || null;
      const regimeRecommendation = summary?.regime_recommendation || null;
      return renderTable(
        ["Metric", "Value"],
        [
          ["Backend", research?.indicator_backend || "—"],
          ["Timeframe", research?.timeframe || "—"],
          ["In-Window Snapshots", fmtNum(summary.snapshot_count, 0)],
          ["Minute Bars", fmtNum(summary.minute_bars, 0)],
          ["Tickers", fmtNum(summary.ticker_count, 0)],
          ["Bullish", fmtNum(summary.bullish_tickers, 0)],
          ["Bearish", fmtNum(summary.bearish_tickers, 0)],
          ["Neutral", fmtNum(summary.neutral_tickers, 0)],
          ["Strongest Return", summary.strongest_return_ticker ? `${summary.strongest_return_ticker.ticker} (${fmtNum(summary.strongest_return_ticker.session_return_bps, 2)} bps)` : "—"],
          ["Weakest Return", summary.weakest_return_ticker ? `${summary.weakest_return_ticker.ticker} (${fmtNum(summary.weakest_return_ticker.session_return_bps, 2)} bps)` : "—"],
          ["Highest RSI", summary.highest_rsi_ticker ? `${summary.highest_rsi_ticker.ticker} (${fmtNum(summary.highest_rsi_ticker.rsi14, 1)})` : "—"],
          ["Highest Volatility", summary.highest_volatility_ticker ? `${summary.highest_volatility_ticker.ticker} (${fmtNum(summary.highest_volatility_ticker.realized_volatility_bps, 2)} bps)` : "—"],
          ["Best Regime Preview", regimeCandidate ? `${regimeCandidate.name} (${fmtRub(regimeCandidate.delta_vs_baseline_rub)} vs baseline)` : "—"],
          ["Regime Recommendation", regimeRecommendation?.reason || "—"],
          ["Focus", (research?.focus || []).map((item) => item.message).join(" | ") || "—"],
        ],
      );
    };

    const renderDailySummary = (summary) => {
      if (!summary) return '<div class="empty">Пока нет daily-summary</div>';
      return renderTable(
        ["Metric", "Value"],
        [
          ["Headline", summary.headline || "—"],
          ["Next Action", summary.next_action || "—"],
          ["Mode", summary.mode || "—"],
          ["Today Trades", fmtNum(summary.today?.trade_count || 0, 0)],
          ["Today Net PnL", `<span class="${pnlClass(summary.today?.net_pnl_rub)}">${fmtRub(summary.today?.net_pnl_rub)}</span>`],
          ["Signals", fmtNum(summary.today?.signals_detected || 0, 0)],
          ["Snapshots", fmtNum(summary.today?.snapshots_processed || 0, 0)],
          ["In-Window Snapshots", fmtNum(summary.today?.recorded_snapshots || 0, 0)],
          ["Overall Trades", fmtNum(summary.overall?.trade_count || 0, 0)],
          ["Overall Net PnL", `<span class="${pnlClass(summary.overall?.net_pnl_rub)}">${fmtRub(summary.overall?.net_pnl_rub)}</span>`],
          ["Analysis", summary.analysis?.assessment || summary.analysis?.status || "—"],
          ["Optimizer", summary.optimizer?.status || "—"],
          ["Research", summary.research?.status || "—"],
          ["Watchdog", summary.watchdog?.status || "—"],
        ],
      );
    };

    const renderSummaryFocus = (summary) => {
      const focus = summary?.focus || [];
      if (!focus.length) return '<div class="empty">Нет summary-focus</div>';
      return `<div class="chips">${focus.map((item) => `<div class="chip">${item}</div>`).join("")}</div>`;
    };

    const renderResearchTickers = (research) => {
      const tickers = research?.tickers || [];
      const tickerTable = tickers.length ? renderTable(
        ["Ticker", "Trend", "Return", "Range", "Vol", "RSI14", "EMA Gap", "MACD Hist", "Spread"],
        tickers.map((item) => [
          item.ticker,
          item.trend_label,
          fmtNum(item.session_return_bps, 2) + " bps",
          fmtNum(item.session_range_bps, 2) + " bps",
          fmtNum(item.realized_volatility_bps, 2) + " bps",
          fmtNum(item.rsi14, 1),
          fmtNum(item.ema_gap_bps, 2) + " bps",
          fmtNum(item.macd_hist, 4),
          fmtNum(item.average_spread_bps, 2) + " bps",
        ]),
      ) : '<div class="empty">Пока нет ticker-research</div>';
      return tickerTable + `<div class="stack-gap"></div>` + renderResearchRegime(research);
    };

    const renderResearchRegime = (research) => {
      const replay = research?.regime_replay || null;
      const rows = replay?.top || [];
      if (!replay || !rows.length) return '<div class="empty">Пока нет regime-replay</div>';
      const recommendation = replay?.recommendation || null;
      const lead = recommendation?.candidate
        ? `<div class="empty">recommendation: ${recommendation.reason || "—"} | lead: ${recommendation.candidate.name} | delta vs baseline: ${fmtRub(recommendation.candidate.delta_vs_baseline_rub)}</div>`
        : `<div class="empty">recommendation: ${recommendation?.reason || "—"}</div>`;
      return (
        `<div class="subhead">Regime Replay</div>` +
        lead +
        renderTable(
          ["Filter", "Trades", "Signals", "Filtered", "Win Rate", "Net PnL", "Delta", "PF"],
          rows.map((item) => [
            item.name,
            fmtNum(item.trade_count, 0),
            fmtNum(item.signals_detected, 0),
            fmtNum(item.filtered_signal_count, 0),
            fmtNum(item.win_rate_pct, 2) + " %",
            `<span class="${pnlClass(item.net_pnl_rub)}">${fmtRub(item.net_pnl_rub)}</span>`,
            `<span class="${pnlClass(item.delta_vs_baseline_rub)}">${fmtRub(item.delta_vs_baseline_rub)}</span>`,
            fmtNum(item.profit_factor, 2),
          ]),
        )
      );
    };

    const renderFocus = (analysis) => {
      const focus = analysis?.focus || [];
      if (!focus.length) return '<div class="empty">Нет focus-items</div>';
      return `<div class="chips">${focus.map((item) => `<div class="chip">${item.message}</div>`).join("")}</div>`;
    };

    const renderBreakdown = (payload, label) => {
      if (!payload) return '<div class="empty">Нет breakdown</div>';
      const renderOne = (title, rows) => {
        if (!rows.length) return `<div class="subhead">${title}</div><div class="empty">Пока пусто</div>`;
        return `<div class="subhead">${title}</div>${renderTable(
          [label, "Trades", "Win Rate", "Net PnL", "Expectancy", "Profit Factor"],
          rows.map((item) => [
            item.key,
            fmtNum(item.trade_count, 0),
            fmtNum(item.win_rate_pct, 2) + " %",
            `<span class="${pnlClass(item.net_pnl_rub)}">${fmtRub(item.net_pnl_rub)}</span>`,
            `<span class="${pnlClass(item.expectancy_rub)}">${fmtRub(item.expectancy_rub)}</span>`,
            fmtNum(item.profit_factor, 2),
          ]),
        )}`;
      };
      return renderOne("Worst", payload.worst || []) + `<div class="stack-gap"></div>` + renderOne("Best", payload.best || []);
    };

    async function refresh() {
      try {
        const response = await fetch("/api/state?ts=" + Date.now(), { cache: "no-store" });
        const state = await response.json();
        const todayStats = state.stats?.today || null;
        const overallStats = state.stats?.overall || null;
        const optimizer = state.optimizer || null;
        const optimizerTop = optimizer?.top?.[0] || null;
        const optimizerBaseline = optimizer?.baseline || null;
        const optimizerRecommendation = optimizer?.recommendation || null;
        const coverage = optimizer?.signal_coverage || null;
        const watchdog = state.watchdog || null;
        const doctor = state.doctor || null;
        const tuning = state.tuning || null;
        const restrictions = state.restrictions || null;
        const governance = state.governance || null;
        const activeRestrictions = state.active_restrictions || null;
        const analysis = state.analysis || null;
        const research = state.research || null;
        const summary = state.summary || null;

        document.getElementById("statusText").textContent = "online";
        document.getElementById("mode").textContent = `${state.mode || "-"} / ${state.position_sizing_mode || "-"} / ${fmtNum(state.portfolio?.max_gross_leverage, 2)}x`;
        document.getElementById("cash").textContent = fmtRub(state.portfolio?.cash_rub);
        document.getElementById("marketValue").textContent = fmtRub(state.portfolio?.market_value_rub);
        document.getElementById("equity").textContent = fmtRub(state.portfolio?.equity_rub);

        const realizedEl = document.getElementById("realized");
        realizedEl.textContent = fmtRub(state.realized_pnl_rub);
        realizedEl.className = "value " + pnlClass(state.realized_pnl_rub);

        const unrealizedEl = document.getElementById("unrealized");
        unrealizedEl.textContent = fmtRub(state.portfolio?.unrealized_pnl_rub);
        unrealizedEl.className = "value " + pnlClass(state.portfolio?.unrealized_pnl_rub);
        document.getElementById("todayTrades").textContent = fmtNum(todayStats?.trade_count || 0, 0);
        document.getElementById("allTrades").textContent = fmtNum(overallStats?.trade_count || 0, 0);

        const marketHistory = state.market_history || {};
        document.getElementById("subline").textContent =
          `watchlist: ${(state.watchlist || []).join(", ")} | schedule: ${state.entry_schedule?.start || "—"}-${state.entry_schedule?.end || "—"} ${state.entry_schedule?.timezone || ""} | updated: ${state.updated_at || "—"} | processed: ${state.snapshots_processed || 0} | in-window today: ${marketHistory.recorded_snapshots_today || 0} | recorded total: ${marketHistory.recorded_snapshots_total || 0} | signals: ${state.signals_detected || 0}`;

        document.getElementById("positionsWrap").innerHTML = renderTable(
          ["Ticker", "Qty", "Entry", "Current Bid", "Entry Fee", "Opened"],
          (state.positions || []).map((item) => [
            item.ticker,
            fmtNum(item.quantity_lots, 0),
            fmtNum(item.entry_price, 4),
            fmtNum(item.current_bid, 4),
            fmtRub(item.entry_fee_rub),
            item.opened_at,
          ]),
        );

        document.getElementById("tradesWrap").innerHTML = renderTable(
          ["Ticker", "Qty", "Entry", "Exit", "Fees", "Net PnL", "Exit Reason"],
          (state.trades_today || []).slice().reverse().map((item) => [
            item.ticker,
            fmtNum(item.quantity_lots, 0),
            fmtNum(item.entry_price, 4),
            fmtNum(item.exit_price, 4),
            fmtRub(item.fees_rub),
            `<span class="${pnlClass(item.net_pnl_rub)}">${fmtRub(item.net_pnl_rub)}</span>`,
            item.exit_reason,
          ]),
        );

        document.getElementById("marketWrap").innerHTML = renderTable(
          ["Ticker", "Bid", "Ask", "Spread bps", "Imbalance", "Time"],
          (state.market || []).map((item) => [
            item.ticker,
            fmtNum(item.bid_price, 4),
            fmtNum(item.ask_price, 4),
            fmtNum(item.spread_bps, 2),
            fmtNum(item.imbalance, 3),
            item.at,
          ]),
        );

        const blocked = state.blocked_reasons || {};
        const blockedEntries = Object.entries(blocked).sort((a, b) => b[1] - a[1]);
        document.getElementById("blockedWrap").innerHTML = blockedEntries.length
          ? blockedEntries.map(([reason, count]) => `<div class="chip">${reason}: ${count}</div>`).join("")
          : '<div class="empty">Нет блокировок</div>';

        document.getElementById("todaySummaryWrap").innerHTML = renderSummary(todayStats);
        document.getElementById("overallSummaryWrap").innerHTML = renderSummary(overallStats);
        const optimizerInfoParts = [];
        if (optimizer?.status) optimizerInfoParts.push(`status: ${optimizer.status}`);
        if (optimizer?.snapshot_count !== undefined) optimizerInfoParts.push(`in-window snapshots: ${fmtNum(optimizer.snapshot_count, 0)}`);
        if (optimizer?.raw_snapshot_count !== undefined) optimizerInfoParts.push(`raw snapshots: ${fmtNum(optimizer.raw_snapshot_count, 0)}`);
        if (optimizerRecommendation) optimizerInfoParts.push(`eligible: ${optimizerRecommendation.eligible}`);
        if (optimizerRecommendation?.reason) optimizerInfoParts.push(`reason: ${optimizerRecommendation.reason}`);
        if (optimizerRecommendation?.delta_vs_baseline_rub) optimizerInfoParts.push(`delta vs baseline: ${fmtRub(optimizerRecommendation.delta_vs_baseline_rub)}`);
        if (optimizer?.entry_window_summary?.excluded_reasons) {
          const excluded = Object.entries(optimizer.entry_window_summary.excluded_reasons)
            .map(([reason, count]) => `${reason}=${count}`)
            .join(", ");
          if (excluded) optimizerInfoParts.push(`excluded: ${excluded}`);
        }
        const recommendationLine = optimizerInfoParts.length
          ? `<div class="empty">${optimizerInfoParts.join(" | ")}</div>`
          : "";
        document.getElementById("optimizerTopWrap").innerHTML = recommendationLine + renderOptimizer(optimizerTop);
        document.getElementById("optimizerBaselineWrap").innerHTML = renderOptimizer(optimizerBaseline);
        document.getElementById("coverageSummaryWrap").innerHTML = renderCoverageSummary(coverage);
        document.getElementById("coverageBreakdownWrap").innerHTML = renderCoverageBreakdown(coverage);
        document.getElementById("watchdogWrap").innerHTML = renderWatchdog(watchdog, doctor);
        document.getElementById("strategyWrap").innerHTML = renderStrategy(
          state.strategy_parameters || null,
          state.strategy_diagnostics || null,
        );
        document.getElementById("restrictionsWrap").innerHTML = renderRestrictions(restrictions, activeRestrictions);
        document.getElementById("tuningWrap").innerHTML = renderTuning(tuning);
        document.getElementById("governanceWrap").innerHTML = renderGovernance(governance);
        document.getElementById("analysisSummaryWrap").innerHTML = renderAnalysisSummary(analysis);
        document.getElementById("analysisFocusWrap").innerHTML = renderFocus(analysis);
        document.getElementById("summaryWrap").innerHTML = renderDailySummary(summary);
        document.getElementById("summaryFocusWrap").innerHTML = renderSummaryFocus(summary);
        document.getElementById("researchSummaryWrap").innerHTML = renderResearchSummary(research);
        document.getElementById("researchTickerWrap").innerHTML = renderResearchTickers(research);
        document.getElementById("analysisTickerWrap").innerHTML = renderBreakdown(analysis?.by_ticker, "Ticker");
        document.getElementById("analysisHourWrap").innerHTML = renderBreakdown(analysis?.by_hour, "Hour");
      } catch (error) {
        document.getElementById("statusText").textContent = "waiting for state";
      }
    }

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


def _default_payload() -> dict[str, object]:
    return {
        "updated_at": None,
        "mode": "paper",
        "watchlist": [],
        "position_sizing_mode": None,
        "strategy_parameters": None,
        "strategy_diagnostics": None,
        "active_restrictions": {
            "disabled_tickers": [],
            "blocked_entry_hours": [],
            "updated_at": None,
            "source": None,
        },
        "entry_schedule": {
            "timezone": None,
            "weekdays": [],
            "start": None,
            "end": None,
        },
        "snapshots_processed": 0,
        "signals_detected": 0,
        "market_history": {
            "recording_mode": "entry_window_only",
            "entry_window_only": True,
            "recorded_snapshots_total": 0,
            "recorded_snapshots_today": 0,
            "skipped_snapshots_total": 0,
            "current_day": None,
            "last_recorded_at": None,
        },
        "realized_pnl_rub": "0",
        "blocked_reasons": {},
        "stats": {
            "today": None,
            "overall": None,
        },
        "optimizer": {
          "top": [],
          "baseline": None,
        },
        "watchdog": None,
        "doctor": None,
        "tuning": None,
        "restrictions": None,
        "governance": None,
        "analysis": None,
        "research": None,
        "summary": None,
        "portfolio": {
            "initial_cash_rub": None,
            "cash_rub": None,
            "borrowed_cash_rub": None,
            "market_value_rub": None,
            "unrealized_pnl_rub": None,
            "equity_rub": None,
            "gross_exposure_rub": None,
            "max_gross_exposure_rub": None,
            "remaining_buying_power_rub": None,
            "max_gross_leverage": None,
            "gross_leverage_used": None,
            "deployment_pct": None,
        },
        "positions": [],
        "trades_today": [],
        "market": [],
    }


def serve_dashboard(*, host: str, port: int, runtime_dir: Path) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    state_path = runtime_dir / "dashboard_state.json"
    optimizer_path = runtime_dir / "optimizer" / "latest.json"
    watchdog_path = runtime_dir / "watchdog" / "latest.json"
    doctor_path = runtime_dir / "doctor" / "latest.json"
    tuning_path = runtime_dir / "tuning" / "latest.json"
    restrictions_path = runtime_dir / "restrictions" / "latest.json"
    governance_path = runtime_dir / "governance" / "latest.json"
    analysis_path = runtime_dir / "analysis" / "latest.json"
    research_path = runtime_dir / "research" / "latest.json"
    summary_path = runtime_dir / "summary" / "latest.json"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/state":
                payload = _default_payload()
                if state_path.exists():
                    try:
                        payload = json.loads(state_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload = _default_payload()
                if optimizer_path.exists():
                    try:
                        payload["optimizer"] = json.loads(optimizer_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload["optimizer"] = {"top": [], "baseline": None}
                if watchdog_path.exists():
                    try:
                        payload["watchdog"] = json.loads(watchdog_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload["watchdog"] = None
                if doctor_path.exists():
                    try:
                        payload["doctor"] = json.loads(doctor_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload["doctor"] = None
                if tuning_path.exists():
                    try:
                        payload["tuning"] = json.loads(tuning_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload["tuning"] = None
                if restrictions_path.exists():
                    try:
                        payload["restrictions"] = json.loads(restrictions_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload["restrictions"] = None
                if governance_path.exists():
                    try:
                        payload["governance"] = json.loads(governance_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload["governance"] = None
                if analysis_path.exists():
                    try:
                        payload["analysis"] = json.loads(analysis_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload["analysis"] = None
                if research_path.exists():
                    try:
                        payload["research"] = json.loads(research_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload["research"] = None
                if summary_path.exists():
                    try:
                        payload["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        payload["summary"] = None
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/health":
                body = b'{"ok":true}'
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
