from __future__ import annotations

import unittest
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from moex_scalper.config import ScalperConfig
from moex_scalper.domain import ClosedTrade, InstrumentSpec, MarketSnapshot, Side
from moex_scalper.risk import RiskManager, trading_day_key


def build_config(
    *,
    mode: str = "paper",
    paper_guard_cooldown_seconds: float = 1800.0,
    paper_continue_after_daily_loss_limit: bool = True,
) -> ScalperConfig:
    return ScalperConfig(
        token="token",
        account_id="",
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
        paper_ticker_guard_cooldown_seconds=paper_guard_cooldown_seconds,
        paper_continue_after_daily_loss_limit=paper_continue_after_daily_loss_limit,
        intraday_session_max_guarded_tickers=0,
        cooldown_seconds=12.0,
        time_stop_seconds=8.0,
        impulse_window_seconds=2.5,
        max_spread_bps=Decimal("1.5"),
        min_imbalance=Decimal("0.58"),
        min_impulse_bps=Decimal("6"),
        take_profit_bps=Decimal("18"),
        stop_loss_bps=Decimal("10"),
        min_expected_edge_bps=Decimal("14"),
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
        allow_short=False,
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


def build_instrument(
    *,
    ticker: str = "SBER",
    instrument_id: str = "instrument-sber",
    name: str = "Sberbank",
) -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id=instrument_id,
        ticker=ticker,
        class_code="TQBR",
        figi="FIGI",
        lot_size=10,
        min_price_increment=Decimal("0.01"),
        currency="RUB",
        name=name,
    )


def build_trade(instrument: InstrumentSpec, closed_at: datetime, *, pnl: str = "-30") -> ClosedTrade:
    opened_at = closed_at - timedelta(seconds=8)
    return ClosedTrade(
        instrument=instrument,
        side=Side.BUY,
        quantity_lots=1,
        entry_price=Decimal("100"),
        exit_price=Decimal("99.70"),
        opened_at=opened_at,
        closed_at=closed_at,
        gross_pnl_rub=Decimal(pnl),
        fees_rub=Decimal("0"),
        net_pnl_rub=Decimal(pnl),
        entry_reason="signal",
        exit_reason="time_stop",
    )


def build_snapshot(instrument: InstrumentSpec, at: datetime) -> MarketSnapshot:
    return MarketSnapshot(
        instrument=instrument,
        bid_price=Decimal("100"),
        ask_price=Decimal("100.01"),
        bid_quantity=100,
        ask_quantity=90,
        at=at,
    )


class RiskManagerPaperGuardTests(unittest.TestCase):
    def test_paper_mode_continues_collecting_after_daily_loss_limit(self) -> None:
        config = build_config(mode="paper", paper_continue_after_daily_loss_limit=True)
        risk = RiskManager(config)
        instrument = build_instrument()
        second_instrument = build_instrument(
            ticker="GAZP",
            instrument_id="instrument-gazp",
            name="Gazprom",
        )
        closed_at = datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc)

        risk.note_closed_trade(build_trade(instrument, closed_at, pnl="-2600"))

        allowed, reason = risk.can_open(
            build_snapshot(second_instrument, closed_at + timedelta(minutes=1)),
            open_positions=0,
            planned_notional_rub=Decimal("1000"),
        )
        self.assertTrue(risk.daily_loss_limit_hit())
        self.assertFalse(risk.daily_loss_limit_enforced())
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_paper_ticker_guard_rearms_after_cooldown(self) -> None:
        config = build_config(mode="paper", paper_guard_cooldown_seconds=1800.0)
        risk = RiskManager(config)
        instrument = build_instrument()
        first_close = datetime(2026, 6, 15, 10, 20, tzinfo=timezone.utc)
        second_close = first_close + timedelta(minutes=5)

        risk.note_closed_trade(build_trade(instrument, first_close))
        risk.note_closed_trade(build_trade(instrument, second_close))

        guard_snapshot = build_snapshot(instrument, second_close + timedelta(minutes=10))
        allowed, reason = risk.can_open(
            guard_snapshot,
            open_positions=0,
            planned_notional_rub=Decimal("1000"),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "ticker_consecutive_time_stop_losses_limit")
        self.assertEqual(len(risk.active_ticker_guards(now=guard_snapshot.at)), 1)

        reopened_snapshot = build_snapshot(instrument, second_close + timedelta(minutes=31))
        allowed, reason = risk.can_open(
            reopened_snapshot,
            open_positions=0,
            planned_notional_rub=Decimal("1000"),
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")
        self.assertEqual(risk.active_ticker_guards(now=reopened_snapshot.at), [])
        self.assertEqual(risk.ticker_consecutive_time_stop_losses.get("SBER", 0), 0)
        self.assertEqual(risk.realized_pnl_rub, Decimal("-60"))
        self.assertEqual(risk.ticker_realized_pnl_rub["SBER"], Decimal("-60"))

    def test_restore_state_converts_legacy_day_guard_into_temporary_cooldown(self) -> None:
        config = build_config(mode="paper", paper_guard_cooldown_seconds=1800.0)
        risk = RiskManager(config)
        instrument = build_instrument()
        first_close = datetime(2026, 6, 15, 10, 20, tzinfo=timezone.utc)
        second_close = first_close + timedelta(minutes=5)
        trades = [
            build_trade(instrument, first_close),
            build_trade(instrument, second_close),
        ]

        risk.restore_state(
            realized_pnl_rub=Decimal("-60"),
            current_day=trading_day_key(second_close, config.timezone),
            cooldown_until={},
            trades_today=trades,
            now=second_close + timedelta(minutes=10),
        )

        guarded_snapshot = build_snapshot(instrument, second_close + timedelta(minutes=10))
        allowed, reason = risk.can_open(
            guarded_snapshot,
            open_positions=0,
            planned_notional_rub=Decimal("1000"),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "ticker_consecutive_time_stop_losses_limit")

        reopened_snapshot = build_snapshot(instrument, second_close + timedelta(minutes=31))
        allowed, reason = risk.can_open(
            reopened_snapshot,
            open_positions=0,
            planned_notional_rub=Decimal("1000"),
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_restore_state_recomputes_existing_guard_using_current_cooldown(self) -> None:
        config = build_config(mode="paper", paper_guard_cooldown_seconds=900.0)
        risk = RiskManager(config)
        instrument = build_instrument()
        first_close = datetime(2026, 6, 15, 10, 20, tzinfo=timezone.utc)
        second_close = first_close + timedelta(minutes=5)
        trades = [
            build_trade(instrument, first_close),
            build_trade(instrument, second_close),
        ]

        risk.restore_state(
            realized_pnl_rub=Decimal("-60"),
            current_day=trading_day_key(second_close, config.timezone),
            cooldown_until={},
            ticker_guard_cooldown_until={
                "SBER": second_close + timedelta(minutes=45),
            },
            trades_today=trades,
            now=second_close + timedelta(minutes=20),
        )

        reopened_snapshot = build_snapshot(instrument, second_close + timedelta(minutes=20))
        allowed, reason = risk.can_open(
            reopened_snapshot,
            open_positions=0,
            planned_notional_rub=Decimal("1000"),
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")
        self.assertEqual(risk.active_ticker_guards(now=reopened_snapshot.at), [])
        self.assertNotIn("SBER", risk.ticker_guard_cooldown_until)

    def test_live_mode_keeps_ticker_guard_for_entire_day(self) -> None:
        config = build_config(
            mode="live",
            paper_guard_cooldown_seconds=1800.0,
            paper_continue_after_daily_loss_limit=False,
        )
        risk = RiskManager(config)
        instrument = build_instrument()
        first_close = datetime(2026, 6, 15, 10, 20, tzinfo=timezone.utc)
        second_close = first_close + timedelta(minutes=5)

        risk.note_closed_trade(build_trade(instrument, first_close))
        risk.note_closed_trade(build_trade(instrument, second_close))

        late_snapshot = build_snapshot(instrument, second_close + timedelta(hours=2))
        allowed, reason = risk.can_open(
            late_snapshot,
            open_positions=0,
            planned_notional_rub=Decimal("1000"),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "ticker_consecutive_time_stop_losses_limit")

    def test_live_mode_enforces_daily_loss_limit(self) -> None:
        config = build_config(
            mode="live",
            paper_continue_after_daily_loss_limit=False,
        )
        risk = RiskManager(config)
        instrument = build_instrument()
        second_instrument = build_instrument(
            ticker="GAZP",
            instrument_id="instrument-gazp",
            name="Gazprom",
        )
        closed_at = datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc)

        risk.note_closed_trade(build_trade(instrument, closed_at, pnl="-2600"))

        allowed, reason = risk.can_open(
            build_snapshot(second_instrument, closed_at + timedelta(minutes=1)),
            open_positions=0,
            planned_notional_rub=Decimal("1000"),
        )
        self.assertTrue(risk.daily_loss_limit_hit())
        self.assertTrue(risk.daily_loss_limit_enforced())
        self.assertFalse(allowed)
        self.assertEqual(reason, "daily_loss_limit")


if __name__ == "__main__":
    unittest.main()
