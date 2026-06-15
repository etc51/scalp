from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from moex_scalper.analysis import load_shadow_missing_records, load_shadow_records, load_trade_records
from moex_scalper.market_history import load_snapshots
from moex_scalper.persistence import PaperRuntimeStore


class RuntimeIoResilienceTests(unittest.TestCase):
    def test_load_trade_records_skips_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_trades.jsonl"
            good = {
                "instrument_id": "instrument-sber",
                "ticker": "SBER",
                "side": "buy",
                "quantity_lots": 1,
                "entry_price": "100",
                "exit_price": "101",
                "opened_at": "2026-06-15T10:15:00+00:00",
                "closed_at": "2026-06-15T10:15:06+00:00",
                "gross_pnl_rub": "10",
                "fees_rub": "1",
                "net_pnl_rub": "9",
                "entry_reason": "signal",
                "exit_reason": "time_stop",
                "hold_seconds": 6,
            }
            path.write_text(
                json.dumps(good, ensure_ascii=False) + "\n" + "{broken json}\n",
                encoding="utf-8",
            )

            records = load_trade_records(path)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].ticker, "SBER")
            self.assertEqual(records[0].net_pnl_rub, Decimal("9"))

    def test_load_snapshots_skips_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "2026-06-15.jsonl"
            good = {
                "at": "2026-06-15T10:15:00+00:00",
                "ticker": "SBER",
                "instrument_id": "instrument-sber",
                "class_code": "TQBR",
                "figi": "FIGI",
                "lot_size": 10,
                "min_price_increment": "0.01",
                "currency": "RUB",
                "name": "Sberbank",
                "bid_price": "100",
                "ask_price": "100.01",
                "bid_quantity": 100,
                "ask_quantity": 90,
            }
            path.write_text(
                "{broken json}\n" + json.dumps(good, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            snapshots = load_snapshots(path)

            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0].instrument.ticker, "SBER")
            self.assertEqual(snapshots[0].bid_price, Decimal("100"))

    def test_load_shadow_records_skips_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "shadow_trades.jsonl"
            good = {
                "instrument_id": "instrument-sber",
                "ticker": "SBER",
                "side": "buy",
                "quantity_lots": 1,
                "entry_price": "100",
                "real_exit_price": "100.1",
                "shadow_exit_price": "100.2",
                "opened_at": "2026-06-15T10:15:00+00:00",
                "real_closed_at": "2026-06-15T10:15:06+00:00",
                "shadow_closed_at": "2026-06-15T10:17:06+00:00",
                "real_hold_seconds": 6,
                "shadow_hold_seconds": 126,
                "shadow_follow_seconds": 120,
                "real_net_pnl_rub": "9",
                "shadow_net_pnl_rub": "11",
                "delta_net_pnl_rub": "2",
                "real_exit_reason": "time_stop",
                "shadow_result": "better",
            }
            path.write_text(
                json.dumps(good, ensure_ascii=False) + "\n" + "{broken json}\n",
                encoding="utf-8",
            )

            records = load_shadow_records(path)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].ticker, "SBER")
            self.assertEqual(records[0].delta_net_pnl_rub, Decimal("2"))

    def test_load_shadow_missing_records_skips_corrupt_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "shadow_missing.jsonl"
            good = {
                "instrument_id": "instrument-sber",
                "ticker": "SBER",
                "side": "buy",
                "quantity_lots": 1,
                "opened_at": "2026-06-15T10:15:00+00:00",
                "real_closed_at": "2026-06-15T10:15:06+00:00",
                "real_exit_reason": "time_stop",
                "issue_at": "2026-06-15T10:17:30+00:00",
                "missing_recorded_at": "2026-06-15T10:17:30+00:00",
                "missing_reason": "expired_day_roll",
                "overdue_seconds": 24.0,
            }
            path.write_text(
                json.dumps(good, ensure_ascii=False) + "\n" + "{broken json}\n",
                encoding="utf-8",
            )

            records = load_shadow_missing_records(path)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].ticker, "SBER")
            self.assertEqual(records[0].missing_reason, "expired_day_roll")

    def test_corrupt_summary_and_session_fallback_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            store = PaperRuntimeStore(runtime_dir, timezone.utc)
            store.session_path.write_text("{broken json}", encoding="utf-8")
            (store.daily_dir / "2026-06-15.json").parent.mkdir(parents=True, exist_ok=True)
            (store.daily_dir / "2026-06-15.json").write_text("{broken json}", encoding="utf-8")
            store.overview_path.write_text("{broken json}", encoding="utf-8")

            session = store.load_session()
            stats = store.load_stats("2026-06-15")

            self.assertIsNone(session)
            self.assertEqual(stats["today"]["trade_count"], 0)
            self.assertEqual(stats["overall"]["trade_count"], 0)
            self.assertEqual(stats["today"]["scope"], "2026-06-15")
            self.assertEqual(stats["overall"]["scope"], "all_time")

    def test_shadow_stats_append_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            store = PaperRuntimeStore(runtime_dir, timezone.utc)

            store.append_shadow_result(
                {
                    "instrument_id": "instrument-sber",
                    "ticker": "SBER",
                    "side": "buy",
                    "quantity_lots": 1,
                    "entry_price": "100",
                    "real_exit_price": "100.1",
                    "shadow_exit_price": "100.2",
                    "opened_at": "2026-06-15T10:15:00+00:00",
                    "real_closed_at": "2026-06-15T10:15:06+00:00",
                    "shadow_closed_at": "2026-06-15T10:17:06+00:00",
                    "real_hold_seconds": 6,
                    "shadow_hold_seconds": 126,
                    "shadow_follow_seconds": 120,
                    "real_net_pnl_rub": "9",
                    "shadow_net_pnl_rub": "11",
                    "delta_net_pnl_rub": "2",
                    "real_exit_reason": "time_stop",
                    "shadow_result": "better",
                }
            )

            stats = store.load_shadow_stats("2026-06-15")

            self.assertEqual(stats["today"]["observation_count"], 1)
            self.assertEqual(stats["today"]["improved_count"], 1)
            self.assertEqual(stats["today"]["delta_net_pnl_rub"], "2")
            self.assertEqual(stats["overall"]["observation_count"], 1)
            self.assertEqual(stats["overall"]["last_ticker"], "SBER")

    def test_shadow_issue_stats_append_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            store = PaperRuntimeStore(runtime_dir, timezone.utc)

            store.append_shadow_issue(
                {
                    "instrument_id": "instrument-sber",
                    "ticker": "SBER",
                    "side": "buy",
                    "quantity_lots": 1,
                    "opened_at": "2026-06-15T10:15:00+00:00",
                    "real_closed_at": "2026-06-15T10:15:06+00:00",
                    "real_exit_reason": "time_stop",
                    "shadow_due_at": "2026-06-15T10:17:06+00:00",
                    "issue_at": "2026-06-15T10:18:00+00:00",
                    "missing_recorded_at": "2026-06-15T10:18:00+00:00",
                    "missing_reason": "expired_day_roll",
                    "overdue_seconds": 54.0,
                }
            )

            stats = store.load_shadow_issue_stats("2026-06-15")

            self.assertEqual(stats["today"]["issue_count"], 1)
            self.assertEqual(stats["today"]["reasons"]["expired_day_roll"], 1)
            self.assertEqual(stats["overall"]["issue_count"], 1)
            self.assertEqual(stats["overall"]["last_ticker"], "SBER")


if __name__ == "__main__":
    unittest.main()
