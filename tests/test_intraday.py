from __future__ import annotations

import unittest
from datetime import time, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from moex_scalper.config import ScalperConfig
from moex_scalper.intraday import run_intraday_research


def build_config() -> ScalperConfig:
    return ScalperConfig(
        token="token",
        account_id="",
        mode="paper",
        target="invest-public-api.tbank.ru:443",
        class_code="TQBR",
        watchlist=("SBER",),
        orderbook_depth=1,
        order_quantity_lots=1,
        max_position_notional_rub=Decimal("30000"),
        daily_loss_limit_rub=Decimal("2500"),
        intraday_ticker_loss_limit_rub=Decimal("250"),
        intraday_ticker_max_consecutive_losses=4,
        intraday_ticker_max_consecutive_time_stop_losses=2,
        paper_ticker_guard_cooldown_seconds=1800.0,
        paper_continue_after_daily_loss_limit=True,
        intraday_session_max_guarded_tickers=0,
        cooldown_seconds=12.0,
        time_stop_seconds=8.0,
        impulse_window_seconds=2.5,
        max_spread_bps=Decimal("1.5"),
        min_imbalance=Decimal("0.58"),
        min_impulse_bps=Decimal("6"),
        take_profit_bps=Decimal("14"),
        stop_loss_bps=Decimal("10"),
        min_expected_edge_bps=Decimal("8"),
        min_net_take_profit_bps=Decimal("4"),
        target_net_take_profit_buffer_bps=Decimal("2"),
        regime_filter_mode="trend_bullish",
        strategy_overlay_mode="off",
        premium_share_commission_bps=Decimal("4"),
        paper_initial_cash_rub=Decimal("300000"),
        paper_max_gross_leverage=Decimal("1.2"),
        position_sizing_mode="equal_weight_cash",
        timezone_name="UTC",
        timezone=timezone.utc,
        entry_weekdays=(0, 1, 2, 3, 4),
        entry_start_time=time(10, 15),
        entry_end_time=time(17, 45),
        allow_short=False,
        max_open_positions=4,
        run_duration_seconds=0.0,
        runtime_dir=Path("runtime"),
        state_heartbeat_seconds=30.0,
        stream_idle_reconnect_seconds=45.0,
        stream_reconnect_delay_seconds=1.0,
        watchdog_max_state_age_seconds=120,
        watchdog_max_market_data_age_seconds=90,
        watchdog_market_data_warmup_seconds=90,
        watchdog_timeout_seconds=3.0,
        watchdog_check_dashboard_http=True,
    )


class IntradayResearchTests(unittest.TestCase):
    @patch("moex_scalper.intraday.moment_in_entry_window", return_value=(False, "entry_before_window"))
    def test_skips_outside_entry_window(self, _: object) -> None:
        payload = run_intraday_research(build_config(), write_report=False)

        self.assertEqual(payload["status"], "outside_entry_window")
        self.assertFalse(payload["ran"])
        self.assertEqual(payload["next_action"], "wait_for_entry_window")

    @patch("moex_scalper.intraday.build_daily_summary")
    @patch("moex_scalper.intraday.build_indicator_research")
    @patch("moex_scalper.intraday.analyze_trades")
    @patch("moex_scalper.intraday.moment_in_entry_window", return_value=(True, "ok"))
    def test_runs_analysis_research_and_summary(
        self,
        _: object,
        analyze_mock: object,
        research_mock: object,
        summary_mock: object,
    ) -> None:
        analyze_mock.return_value = {"status": "ok", "assessment": "negative_expectancy_so_far"}
        research_mock.return_value = {
            "status": "ok",
            "summary": {
                "best_strategy_lab_candidate": {
                    "name": "trend_pullback_short",
                    "entry_modes": "short_only",
                    "delta_vs_baseline_rub": "2450.2759",
                    "trade_count": 1,
                },
                "strategy_lab_recommendation": {"reason": "insufficient_trade_sample"},
                "best_regime_candidate": {
                    "name": "long_only_rsi_50_70",
                    "entry_modes": "long_only",
                    "delta_vs_baseline_rub": "-189.5750",
                    "trade_count": 2,
                },
                "regime_recommendation": {"reason": "insufficient_trade_sample"},
            },
        }
        summary_mock.return_value = {"headline": "headline"}

        payload = run_intraday_research(build_config(), write_report=False)

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ran"])
        self.assertEqual(payload["analysis_status"], "ok")
        self.assertEqual(payload["research_status"], "ok")
        self.assertEqual(payload["summary_headline"], "headline")
        self.assertEqual(payload["best_strategy_lab_candidate"]["name"], "trend_pullback_short")
        self.assertEqual(payload["next_action"], "continue_intraday_collection")


if __name__ == "__main__":
    unittest.main()
