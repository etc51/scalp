from __future__ import annotations

import unittest
from datetime import time, timezone
from decimal import Decimal
from pathlib import Path

from moex_scalper.config import ScalperConfig
from moex_scalper.tuning import (
    build_analysis_edge_floor_guard_candidate,
    build_unblocker_step_candidate,
    current_strategy_parameters,
)


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
        intraday_ticker_max_consecutive_time_stop_losses=0,
        paper_ticker_guard_cooldown_seconds=900.0,
        paper_continue_after_daily_loss_limit=True,
        intraday_session_max_guarded_tickers=0,
        cooldown_seconds=12.0,
        time_stop_seconds=20.0,
        impulse_window_seconds=2.5,
        max_spread_bps=Decimal("2.5"),
        min_imbalance=Decimal("0.52"),
        min_impulse_bps=Decimal("1.5"),
        take_profit_bps=Decimal("18"),
        stop_loss_bps=Decimal("10"),
        min_expected_edge_bps=Decimal("6"),
        min_net_take_profit_bps=Decimal("4"),
        target_net_take_profit_buffer_bps=Decimal("2"),
        regime_filter_mode="off",
        strategy_overlay_mode="adaptive_twap_trend",
        premium_share_commission_bps=Decimal("4"),
        paper_initial_cash_rub=Decimal("300000"),
        paper_max_gross_leverage=Decimal("1.2"),
        position_sizing_mode="equal_weight_cash",
        timezone_name="UTC",
        timezone=timezone.utc,
        entry_weekdays=(0, 1, 2, 3, 4),
        entry_start_time=time(10, 15),
        entry_end_time=time(17, 45),
        allow_short=True,
        max_open_positions=4,
        run_duration_seconds=0.0,
        runtime_dir=Path("runtime"),
        state_heartbeat_seconds=30.0,
        stream_idle_reconnect_seconds=45.0,
        stream_reconnect_delay_seconds=1.0,
        stream_poll_fallback_enabled=True,
        stream_poll_fallback_interval_seconds=5.0,
        watchdog_max_state_age_seconds=120,
        watchdog_max_market_data_age_seconds=90,
        watchdog_market_data_warmup_seconds=90,
        watchdog_timeout_seconds=3.0,
        watchdog_check_dashboard_http=True,
    )


class TuningTests(unittest.TestCase):
    def test_current_strategy_parameters_expose_adaptive_thresholds(self) -> None:
        payload = current_strategy_parameters(build_config())

        self.assertEqual(payload["adaptive_min_impulse_bps"], "1.5")
        self.assertEqual(payload["adaptive_expected_edge_after_costs_floor_bps"], "2.0")
        self.assertEqual(payload["adaptive_impulse_spread_ratio_floor"], "1.25")
        self.assertEqual(payload["adaptive_workable_time_stop_seconds"], "14.0")

    def test_coverage_unblocker_relaxes_hidden_adaptive_impulse_floors_together(self) -> None:
        candidate = build_unblocker_step_candidate(
            current_parameters=current_strategy_parameters(build_config()),
            dominant_block_reason="impulse_too_small",
        )

        assert candidate is not None
        self.assertEqual(candidate["min_impulse_bps"], "1.0")
        self.assertEqual(candidate["adaptive_min_impulse_bps"], "1.0")
        self.assertEqual(candidate["adaptive_late_session_min_impulse_bps"], "1.5")

    def test_entry_forensics_edge_guard_raises_adaptive_post_cost_floor(self) -> None:
        candidate, details = build_analysis_edge_floor_guard_candidate(
            analysis_payload={
                "status": "ok",
                "assessment": "negative_expectancy_so_far",
                "entry_forensics": {
                    "summary": {
                        "trade_count": 8,
                        "worst_entry_tier": "workable",
                        "worst_edge_bucket": "workable_2_to_4",
                    },
                    "by_expected_edge_after_costs_bucket": {
                        "worst": [
                            {
                                "key": "workable_2_to_4",
                                "trade_count": 4,
                                "expectancy_rub": "-120.5",
                                "avg_expected_edge_after_costs_bps": "2.7",
                            }
                        ],
                        "best": [],
                    },
                },
            },
            current_parameters=current_strategy_parameters(build_config()),
            enabled=True,
            min_trade_count=5,
            min_bucket_trade_count=3,
            min_bucket_share_pct=Decimal("25"),
        )

        assert candidate is not None
        self.assertTrue(details["eligible"])
        self.assertEqual(details["reason"], "analysis_edge_floor_guard_candidate")
        self.assertEqual(candidate["adaptive_expected_edge_after_costs_floor_bps"], "2.5")


if __name__ == "__main__":
    unittest.main()
