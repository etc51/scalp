from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from moex_scalper.analysis import load_trade_records
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


if __name__ == "__main__":
    unittest.main()
