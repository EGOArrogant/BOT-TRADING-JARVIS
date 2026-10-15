"""Modelo de costos: comisión ida y vuelta + deslizamiento + funding esperado."""

from __future__ import annotations

from dataclasses import dataclass

from bot.core.config import Fees
from bot.core.models import Side


@dataclass(frozen=True)
class CostBreakdown:
    fees_pct: float
    slippage_pct: float
    funding_pct: float

    @property
    def total_pct(self) -> float:
        return self.fees_pct + self.slippage_pct + self.funding_pct


def fee_rate(fees: Fees, maker: bool) -> float:
    base = fees.maker if maker else fees.taker
    return base * (1 - fees.bnb_discount)


def estimate_costs(fees: Fees, side: Side, entry_maker: bool, spread_bps: float | None,
                   funding_rate_1h: float | None, hold_hours: float) -> CostBreakdown:
    """Conservador: la salida se asume taker (stop a mercado).

    Deslizamiento: medio spread por cada pata taker + 1 bp de colchón. Sin
    spread conocido se asumen 3 bps por pata. Funding: lo que pagaría la
    posición si la tasa actual se mantiene (si cobra, no se descuenta).
    """
    fees_pct = fee_rate(fees, entry_maker) + fee_rate(fees, False)
    half = (spread_bps / 2 if spread_bps is not None else 3.0) / 1e4
    taker_legs = 1 if entry_maker else 2
    slippage_pct = taker_legs * (half + 1e-4)
    funding_pct = 0.0
    if funding_rate_1h is not None:
        paid = funding_rate_1h if side is Side.LONG else -funding_rate_1h
        funding_pct = max(0.0, paid * hold_hours)
    return CostBreakdown(fees_pct, slippage_pct, funding_pct)
