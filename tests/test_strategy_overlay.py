from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from moex_scalper.domain import Side
from moex_scalper.strategy_overlay import MinuteBar, compute_overlay_indicator_state, evaluate_strategy_overlay


def build_bar(
    at: datetime,
    *,
    open_price: str,
    high_price: str,
    low_price: str,
    close_price: str,
) -> MinuteBar:
    return MinuteBar(
        at=at,
        open=Decimal(open_price),
        high=Decimal(high_price),
        low=Decimal(low_price),
        close=Decimal(close_price),
    )


class StrategyOverlayTests(unittest.TestCase):
    def test_indicator_state_exposes_twap_gap_and_atr(self) -> None:
        start = datetime(2026, 6, 15, 10, 15, tzinfo=timezone.utc)
        bars = [
            build_bar(
                start + timedelta(minutes=index),
                open_price=str(Decimal("100") + Decimal(index) * Decimal("0.2")),
                high_price=str(Decimal("100.4") + Decimal(index) * Decimal("0.2")),
                low_price=str(Decimal("99.8") + Decimal(index) * Decimal("0.2")),
                close_price=str(Decimal("100.2") + Decimal(index) * Decimal("0.2")),
            )
            for index in range(25)
        ]

        state = compute_overlay_indicator_state(bars)

        self.assertIsNotNone(state["session_twap"])
        self.assertIsNotNone(state["session_twap_gap_bps"])
        self.assertIsNotNone(state["atr14_bps"])
        self.assertGreater(state["session_twap_gap_bps"], Decimal("0"))
        self.assertGreater(state["atr14_bps"], Decimal("0"))

    def test_trend_pullback_short_allows_bearish_pullback(self) -> None:
        allowed, reason, _ = evaluate_strategy_overlay(
            "trend_pullback_short",
            indicator_state={
                "trend_label": "bearish",
                "rsi14": Decimal("40"),
                "ema_gap_bps": Decimal("-8"),
                "macd_hist": Decimal("-0.6"),
                "bb_pos": Decimal("0.55"),
            },
            signal_side=Side.SELL,
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_adaptive_twap_trend_allows_bullish_twap_alignment(self) -> None:
        allowed, reason, _ = evaluate_strategy_overlay(
            "adaptive_twap_trend",
            indicator_state={
                "trend_label": "bullish",
                "rsi14": Decimal("58"),
                "ema_gap_bps": Decimal("7"),
                "macd_hist": Decimal("0.5"),
                "bb_pos": Decimal("0.55"),
                "session_twap_gap_bps": Decimal("3.2"),
                "atr14_bps": Decimal("6"),
            },
            signal_side=Side.BUY,
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_opening_range_breakdown_short_requires_bearish_breakdown(self) -> None:
        allowed, reason, _ = evaluate_strategy_overlay(
            "opening_range_breakdown_short",
            indicator_state={
                "trend_label": "bearish",
                "rsi14": Decimal("38"),
                "ema_gap_bps": Decimal("-6"),
                "macd_hist": Decimal("-0.4"),
                "session_return_bps": Decimal("-24"),
                "atr14_bps": Decimal("9"),
                "opening_range_ready": True,
                "opening_range_breakdown_bps": Decimal("-3"),
            },
            signal_side=Side.SELL,
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_session_twap_reclaim_long_requires_positive_twap_gap(self) -> None:
        allowed, reason, _ = evaluate_strategy_overlay(
            "session_twap_reclaim_long",
            indicator_state={
                "trend_label": "bullish",
                "rsi14": Decimal("57"),
                "ema_gap_bps": Decimal("7"),
                "macd_hist": Decimal("0.5"),
                "session_twap_gap_bps": Decimal("3.5"),
                "atr14_bps": Decimal("6"),
            },
            signal_side=Side.BUY,
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "ok")

    def test_session_twap_reject_short_blocks_long_signal(self) -> None:
        allowed, reason, _ = evaluate_strategy_overlay(
            "session_twap_reject_short",
            indicator_state={
                "trend_label": "bearish",
                "rsi14": Decimal("44"),
                "ema_gap_bps": Decimal("-4"),
                "macd_hist": Decimal("-0.3"),
                "session_twap_gap_bps": Decimal("-2.5"),
                "atr14_bps": Decimal("6"),
            },
            signal_side=Side.BUY,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "strategy_overlay_short_only")


if __name__ == "__main__":
    unittest.main()
