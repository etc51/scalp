from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


FOUR_DECIMAL_PLACES = Decimal("0.0001")
DEFAULT_PREMIUM_SHARE_COMMISSION_BPS = Decimal("4.0")


@dataclass(slots=True, frozen=True)
class CommissionModel:
    shares_bps: Decimal = DEFAULT_PREMIUM_SHARE_COMMISSION_BPS

    def fee_rub(self, notional_rub: Decimal) -> Decimal:
        fee = notional_rub * self.shares_bps / Decimal("10000")
        return fee.quantize(FOUR_DECIMAL_PLACES, rounding=ROUND_HALF_UP)

    @property
    def roundtrip_bps(self) -> Decimal:
        return self.shares_bps * Decimal("2")
