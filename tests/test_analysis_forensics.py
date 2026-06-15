from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from moex_scalper.analysis import analyze_trades
from moex_scalper.config import ScalperConfig


def build_config(runtime_dir: Path) -> ScalperConfig:
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
        max_spread_bps=Decimal("2.5"),
        min_imbalance=Decimal("0.52"),
        min_impulse_bps=Decimal("1.5"),
        take_profit_bps=Decimal("12"),
        stop_loss_bps=Decimal("10"),
        min_expected_edge_bps=Decimal("6"),
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
        entry_start_time=time(10, 0),
        entry_end_time=time(18, 0),
        allow_short=True,
        max_open_positions=4,
        run_duration_seconds=0.0,
        runtime_dir=runtime_dir,
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


class AnalysisForensicsTests(unittest.TestCase):
    def test_analyze_trades_builds_entry_forensics_and_shadow_missing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            market_dir = runtime_dir / "market"
            market_dir.mkdir(parents=True, exist_ok=True)

            trades = [
                {
                    "instrument_id": "instrument-sber",
                    "ticker": "SBER",
                    "side": "buy",
                    "quantity_lots": 1,
                    "entry_price": "100.00",
                    "exit_price": "99.95",
                    "opened_at": "2026-06-15T10:20:00+00:00",
                    "closed_at": "2026-06-15T10:20:06+00:00",
                    "gross_pnl_rub": "-5",
                    "fees_rub": "45",
                    "net_pnl_rub": "-50",
                    "entry_reason": "profile=adaptive side=buy impulse_bps=2.00 spread_bps=1.80 imbalance=0.53 net_tp_bps=5.00 net_tp_after_costs_bps=1.20",
                    "exit_reason": "time_stop",
                    "hold_seconds": 6,
                },
                {
                    "instrument_id": "instrument-sber",
                    "ticker": "SBER",
                    "side": "sell",
                    "quantity_lots": 1,
                    "entry_price": "101.00",
                    "exit_price": "101.08",
                    "opened_at": "2026-06-15T17:10:00+00:00",
                    "closed_at": "2026-06-15T17:10:08+00:00",
                    "gross_pnl_rub": "-8",
                    "fees_rub": "62",
                    "net_pnl_rub": "-70",
                    "entry_reason": "profile=adaptive side=sell impulse_bps=-2.20 spread_bps=1.90 imbalance=0.46 net_tp_bps=5.00 net_tp_after_costs_bps=1.10",
                    "exit_reason": "time_stop",
                    "hold_seconds": 8,
                },
                {
                    "instrument_id": "instrument-sber",
                    "ticker": "SBER",
                    "side": "buy",
                    "quantity_lots": 1,
                    "entry_price": "102.00",
                    "exit_price": "102.20",
                    "opened_at": "2026-06-15T10:25:00+00:00",
                    "closed_at": "2026-06-15T10:25:04+00:00",
                    "gross_pnl_rub": "20",
                    "fees_rub": "5",
                    "net_pnl_rub": "15",
                    "entry_reason": "profile=strict side=buy impulse_bps=8.00 spread_bps=0.50 imbalance=0.60 net_tp_bps=12.00",
                    "exit_reason": "take_profit",
                    "hold_seconds": 4,
                },
            ]
            (runtime_dir / "paper_trades.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in trades) + "\n",
                encoding="utf-8",
            )

            shadow_records = [
                {
                    "instrument_id": "instrument-sber",
                    "ticker": "SBER",
                    "side": "buy",
                    "quantity_lots": 1,
                    "entry_price": "100.00",
                    "real_exit_price": "99.95",
                    "shadow_exit_price": "100.05",
                    "opened_at": "2026-06-15T10:20:00+00:00",
                    "real_closed_at": "2026-06-15T10:20:06+00:00",
                    "shadow_closed_at": "2026-06-15T10:22:10+00:00",
                    "real_hold_seconds": 6,
                    "shadow_hold_seconds": 130,
                    "shadow_follow_seconds": 124,
                    "real_net_pnl_rub": "-50",
                    "shadow_net_pnl_rub": "-20",
                    "delta_net_pnl_rub": "30",
                    "real_exit_reason": "time_stop",
                    "shadow_result": "better",
                }
            ]
            (runtime_dir / "shadow_trades.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in shadow_records) + "\n",
                encoding="utf-8",
            )

            shadow_missing = [
                {
                    "instrument_id": "instrument-sber",
                    "ticker": "SBER",
                    "side": "sell",
                    "quantity_lots": 1,
                    "opened_at": "2026-06-15T17:10:00+00:00",
                    "real_closed_at": "2026-06-15T17:10:08+00:00",
                    "real_exit_reason": "time_stop",
                    "shadow_due_at": "2026-06-15T17:12:08+00:00",
                    "issue_at": "2026-06-15T18:00:00+00:00",
                    "missing_recorded_at": "2026-06-15T18:00:00+00:00",
                    "missing_reason": "expired_day_roll",
                    "overdue_seconds": 2872.0,
                }
            ]
            (runtime_dir / "shadow_missing.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in shadow_missing) + "\n",
                encoding="utf-8",
            )

            snapshots: list[dict[str, object]] = []
            base = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
            for minute in range(0, 31):
                price = Decimal("99.50") + Decimal(minute) * Decimal("0.10")
                moment = base + timedelta(minutes=minute)
                snapshots.append(
                    {
                        "at": moment.isoformat(),
                        "ticker": "SBER",
                        "instrument_id": "instrument-sber",
                        "class_code": "TQBR",
                        "figi": "FIGI",
                        "lot_size": 10,
                        "min_price_increment": "0.01",
                        "currency": "RUB",
                        "name": "Sberbank",
                        "bid_price": str(price),
                        "ask_price": str(price + Decimal("0.02")),
                        "bid_quantity": 120,
                        "ask_quantity": 100,
                    }
                )
            evening = datetime(2026, 6, 15, 17, 0, tzinfo=timezone.utc)
            for minute in range(0, 12):
                price = Decimal("101.50") - Decimal(minute) * Decimal("0.05")
                moment = evening + timedelta(minutes=minute)
                snapshots.append(
                    {
                        "at": moment.isoformat(),
                        "ticker": "SBER",
                        "instrument_id": "instrument-sber",
                        "class_code": "TQBR",
                        "figi": "FIGI",
                        "lot_size": 10,
                        "min_price_increment": "0.01",
                        "currency": "RUB",
                        "name": "Sberbank",
                        "bid_price": str(price),
                        "ask_price": str(price + Decimal("0.02")),
                        "bid_quantity": 100,
                        "ask_quantity": 140,
                    }
                )
            (market_dir / "2026-06-15.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in snapshots) + "\n",
                encoding="utf-8",
            )

            payload = analyze_trades(
                build_config(runtime_dir),
                date_key="2026-06-15",
                input_path=None,
                top_n=3,
                days=1,
                write_report=False,
            )

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["entry_forensics"]["summary"]["trade_count"], 3)
            self.assertEqual(payload["entry_forensics"]["summary"]["worst_primary_tag"], "fee_churn")
            self.assertGreaterEqual(
                payload["entry_forensics"]["summary"]["tag_presence"]["fee_churn"],
                2,
            )
            self.assertEqual(payload["shadow_followup"]["status"], "partial_missing_followups")
            self.assertEqual(payload["shadow_followup"]["summary"]["missing_followup_count"], 1)
            self.assertEqual(
                payload["shadow_followup"]["missing_reasons"]["expired_day_roll"],
                1,
            )


if __name__ == "__main__":
    unittest.main()
