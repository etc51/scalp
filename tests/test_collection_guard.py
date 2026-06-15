from __future__ import annotations

import unittest
from decimal import Decimal

from moex_scalper.collection_guard import CollectionGuardPolicy, evaluate_collection_guard


class CollectionGuardTests(unittest.TestCase):
    def test_passes_when_candidate_preserves_sample(self) -> None:
        payload = evaluate_collection_guard(
            baseline_trade_count=10,
            candidate_trade_count=5,
            baseline_signals_detected=20,
            candidate_signals_detected=12,
            policy=CollectionGuardPolicy(
                min_trades=3,
                min_trade_share_pct=Decimal("35"),
                min_signal_share_pct=Decimal("35"),
            ),
        )

        self.assertTrue(payload["passes"])
        self.assertEqual(payload["reasons"], [])
        self.assertEqual(payload["trade_share_pct"], "50.00")
        self.assertEqual(payload["signal_share_pct"], "60.00")

    def test_fails_when_candidate_starves_collection(self) -> None:
        payload = evaluate_collection_guard(
            baseline_trade_count=12,
            candidate_trade_count=2,
            baseline_signals_detected=30,
            candidate_signals_detected=5,
            policy=CollectionGuardPolicy(
                min_trades=3,
                min_trade_share_pct=Decimal("35"),
                min_signal_share_pct=Decimal("35"),
            ),
        )

        self.assertFalse(payload["passes"])
        self.assertIn("trade_count_below_floor", payload["reasons"])
        self.assertIn("trade_share_below_floor", payload["reasons"])
        self.assertIn("signal_share_below_floor", payload["reasons"])


if __name__ == "__main__":
    unittest.main()
