from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from moex_scalper.config import ScalperConfig
from moex_scalper.domain import InstrumentSpec, MarketSnapshot, Position, Side
from moex_scalper.strategy import ModerateScalpingStrategy
from moex_scalper.strategy_overlay import MinuteBar


def build_config(*, mode: str = "paper") -> ScalperConfig:
    return ScalperConfig(
        token="token",
        account_id="account",
        mode=mode,
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
        paper_ticker_guard_cooldown_seconds=2700.0,
        paper_continue_after_daily_loss_limit=True,
        intraday_session_max_guarded_tickers=0,
        cooldown_seconds=12.0,
        time_stop_seconds=8.0,
        impulse_window_seconds=2.5,
        max_spread_bps=Decimal("2.5"),
        min_imbalance=Decimal("0.58"),
        min_impulse_bps=Decimal("0.5"),
        take_profit_bps=Decimal("18"),
        stop_loss_bps=Decimal("10"),
        min_expected_edge_bps=Decimal("4"),
        min_net_take_profit_bps=Decimal("4"),
        target_net_take_profit_buffer_bps=Decimal("2"),
        regime_filter_mode="off",
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
        allow_short=True,
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


def build_instrument() -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id="instrument-sber",
        ticker="SBER",
        class_code="TQBR",
        figi="FIGI",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        name="Sberbank",
    )


def build_snapshot(
    instrument: InstrumentSpec,
    at: datetime,
    *,
    bid_price: str,
    ask_price: str,
    bid_quantity: int = 160,
    ask_quantity: int = 100,
) -> MarketSnapshot:
    return MarketSnapshot(
        instrument=instrument,
        bid_price=Decimal(bid_price),
        ask_price=Decimal(ask_price),
        bid_quantity=bid_quantity,
        ask_quantity=ask_quantity,
        at=at,
    )


class AdaptiveStrategyTests(unittest.TestCase):
    def test_paper_mode_uses_adaptive_profile_for_relaxed_spread_with_cost_headroom(self) -> None:
        strategy = ModerateScalpingStrategy(build_config(mode="paper"))
        instrument = build_instrument()
        start = datetime(2026, 6, 15, 10, 15, tzinfo=timezone.utc)

        strategy.diagnose_entry(
            build_snapshot(
                instrument,
                start,
                bid_price="99.99",
                ask_price="100.01",
            ),
            has_open_position=False,
        )
        signal, reason, metrics = strategy.diagnose_entry(
            build_snapshot(
                instrument,
                start + timedelta(seconds=2),
                bid_price="99.996",
                ask_price="100.024",
            ),
            has_open_position=False,
        )

        self.assertEqual(reason, "ok")
        assert signal is not None
        self.assertEqual(signal.side, Side.BUY)
        self.assertEqual(signal.take_profit_bps, Decimal("16"))
        self.assertEqual(signal.stop_loss_bps, Decimal("8"))
        self.assertEqual(signal.time_stop_seconds, 8.0)
        self.assertIn("profile=adaptive", signal.reason)
        self.assertIn("net_tp_after_costs_bps=", signal.reason)
        self.assertEqual(metrics["entry_profile"], "adaptive")
        self.assertEqual(metrics["strict_spread_pass"], "false")
        self.assertEqual(metrics["adaptive_spread_pass"], "true")
        self.assertGreaterEqual(
            Decimal(str(metrics["net_take_profit_after_costs_bps"])),
            Decimal("2"),
        )

    def test_paper_mode_blocks_adaptive_when_relaxed_spread_has_too_little_cost_headroom(self) -> None:
        config = replace(build_config(mode="paper"), take_profit_bps=Decimal("14"))
        strategy = ModerateScalpingStrategy(config)
        instrument = build_instrument()
        start = datetime(2026, 6, 15, 10, 15, tzinfo=timezone.utc)

        strategy.diagnose_entry(
            build_snapshot(
                instrument,
                start,
                bid_price="99.99",
                ask_price="100.01",
            ),
            has_open_position=False,
        )
        signal, reason, metrics = strategy.diagnose_entry(
            build_snapshot(
                instrument,
                start + timedelta(seconds=2),
                bid_price="100.0085",
                ask_price="100.0435",
            ),
            has_open_position=False,
        )

        self.assertIsNone(signal)
        self.assertEqual(reason, "adaptive_cost_headroom_too_low")
        self.assertEqual(metrics["strict_spread_pass"], "false")
        self.assertEqual(metrics["adaptive_spread_pass"], "true")
        self.assertLess(
            Decimal(str(metrics["net_take_profit_after_costs_bps"])),
            Decimal("2"),
        )

    def test_paper_mode_blocks_adaptive_micro_impulse_noise(self) -> None:
        strategy = ModerateScalpingStrategy(build_config(mode="paper"))
        instrument = build_instrument()
        start = datetime(2026, 6, 15, 10, 15, tzinfo=timezone.utc)

        strategy.diagnose_entry(
            build_snapshot(
                instrument,
                start,
                bid_price="99.99",
                ask_price="100.01",
            ),
            has_open_position=False,
        )
        signal, reason, metrics = strategy.diagnose_entry(
            build_snapshot(
                instrument,
                start + timedelta(seconds=2),
                bid_price="99.993",
                ask_price="100.013",
            ),
            has_open_position=False,
        )

        self.assertIsNone(signal)
        self.assertEqual(reason, "impulse_too_small")
        self.assertEqual(metrics["adaptive_enabled"], "true")
        self.assertEqual(metrics["adaptive_long_impulse_pass"], "false")

    def test_live_mode_keeps_small_impulse_blocked(self) -> None:
        strategy = ModerateScalpingStrategy(build_config(mode="live"))
        instrument = build_instrument()
        start = datetime(2026, 6, 15, 10, 15, tzinfo=timezone.utc)

        strategy.diagnose_entry(
            build_snapshot(
                instrument,
                start,
                bid_price="99.99",
                ask_price="100.01",
            ),
            has_open_position=False,
        )
        signal, reason, metrics = strategy.diagnose_entry(
            build_snapshot(
                instrument,
                start + timedelta(seconds=2),
                bid_price="99.993",
                ask_price="100.013",
            ),
            has_open_position=False,
        )

        self.assertIsNone(signal)
        self.assertEqual(reason, "impulse_too_small")
        self.assertEqual(metrics["adaptive_enabled"], "false")
        self.assertEqual(metrics["adaptive_long_impulse_pass"], "false")

    def test_paper_adaptive_regime_allows_neutral_prev_minute_if_not_opposite(self) -> None:
        config = replace(build_config(mode="paper"), regime_filter_mode="trend_side_aware")
        strategy = ModerateScalpingStrategy(config)
        state = strategy._state_for("instrument-sber")
        start = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
        for index in range(20):
            close = Decimal("100")
            state.completed_minute_bars.append(
                MinuteBar(
                    at=start + timedelta(minutes=index),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                )
            )

        allowed, reason, metrics = strategy._check_regime_filter(
            state,
            signal_side=Side.BUY,
            entry_profile="adaptive",
        )
        strict_allowed, strict_reason, _ = strategy._check_regime_filter(
            state,
            signal_side=Side.BUY,
            entry_profile="strict",
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")
        self.assertEqual(metrics["regime_filter_profile"], "adaptive_not_opposite")
        self.assertFalse(strict_allowed)
        self.assertEqual(strict_reason, "regime_prev_minute_not_bullish")

    def test_time_stop_waits_for_fee_break_even_before_forcing_exit(self) -> None:
        strategy = ModerateScalpingStrategy(build_config(mode="paper"))
        instrument = build_instrument()
        opened_at = datetime(2026, 6, 15, 10, 15, tzinfo=timezone.utc)
        position = Position(
            instrument=instrument,
            side=Side.BUY,
            quantity_lots=1,
            entry_price=Decimal("100"),
            opened_at=opened_at,
            take_profit_bps=Decimal("50"),
            stop_loss_bps=Decimal("50"),
            time_stop_seconds=8.0,
            entry_fee_rub=Decimal("0.4"),
            reason="profile=adaptive",
            metadata={},
        )

        soft_timeout_snapshot = build_snapshot(
            instrument,
            opened_at + timedelta(seconds=8),
            bid_price="100.05",
            ask_price="100.06",
        )
        hard_timeout_snapshot = build_snapshot(
            instrument,
            opened_at + timedelta(seconds=16),
            bid_price="100.05",
            ask_price="100.06",
        )

        self.assertIsNone(strategy.evaluate_exit(position, soft_timeout_snapshot))
        hard_exit = strategy.evaluate_exit(position, hard_timeout_snapshot)
        assert hard_exit is not None
        self.assertEqual(hard_exit.reason, "time_stop")

    def test_time_stop_exits_at_soft_timeout_when_trade_is_fee_positive(self) -> None:
        strategy = ModerateScalpingStrategy(build_config(mode="paper"))
        instrument = build_instrument()
        opened_at = datetime(2026, 6, 15, 10, 15, tzinfo=timezone.utc)
        position = Position(
            instrument=instrument,
            side=Side.BUY,
            quantity_lots=1,
            entry_price=Decimal("100"),
            opened_at=opened_at,
            take_profit_bps=Decimal("50"),
            stop_loss_bps=Decimal("50"),
            time_stop_seconds=8.0,
            entry_fee_rub=Decimal("0.4"),
            reason="profile=adaptive",
            metadata={},
        )

        snapshot = build_snapshot(
            instrument,
            opened_at + timedelta(seconds=8),
            bid_price="100.20",
            ask_price="100.21",
        )

        exit_decision = strategy.evaluate_exit(position, snapshot)
        assert exit_decision is not None
        self.assertEqual(exit_decision.reason, "time_stop")


if __name__ == "__main__":
    unittest.main()
