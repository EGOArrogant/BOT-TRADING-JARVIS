"""Conciliación con el exchange.

1) Estado (cada 30 s): posiciones y órdenes del exchange vs estado local.
   Discrepancia -> alerta + modo seguro (no abrir, proteger lo abierto).
2) Ingresos: PnL realizado + comisiones + funding del exchange vs el diario.
   Diferencia > 0.5 % -> alerta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bot.core.models import Order, OrderType, Position
from bot.execution.base import ExchangePosition, IncomeRecord


@dataclass(frozen=True)
class Discrepancy:
    kind: str       # posicion_huerfana | posicion_sin_stop | cantidad_distinta | lado_distinto | orden_huerfana
    symbol: str
    detail: str
    critical: bool = True
    ref: str = ""     # client id de la orden implicada, si aplica


def reconcile_state(local: dict[str, Position], ex_positions: list[ExchangePosition], ex_orders: list[Order],
                    entries_in_flight: set[str] | None = None) -> list[Discrepancy]:
    out: list[Discrepancy] = []
    in_flight = entries_in_flight or set()
    ex_by_sym = {p.symbol: p for p in ex_positions}
    stops_by_sym: dict[str, list[Order]] = {}
    for o in ex_orders:
        if o.type is OrderType.STOP_MARKET and o.is_open:
            stops_by_sym.setdefault(o.symbol, []).append(o)

    for sym, ep in ex_by_sym.items():
        lp = local.get(sym)
        if lp is None:
            if sym not in in_flight:
                out.append(Discrepancy("posicion_huerfana", sym, f"{ep.side.value} {ep.qty} sin registro local"))
        else:
            if ep.side is not lp.side:
                out.append(Discrepancy("lado_distinto", sym, f"exchange {ep.side.value} vs local {lp.side.value}"))
            elif ep.qty > lp.qty:
                out.append(Discrepancy("cantidad_distinta", sym, f"exchange {ep.qty} > local {lp.qty}"))
        if sym not in stops_by_sym and sym not in in_flight:
            out.append(Discrepancy("posicion_sin_stop", sym, "posición abierta sin stop en el exchange"))

    for o in ex_orders:
        if not o.is_open or o.symbol in ex_by_sym or o.symbol in in_flight:
            continue
        if o.reduce_only or o.close_position:
            out.append(Discrepancy("orden_huerfana", o.symbol, f"{o.type.value} {o.client_order_id} sin posición",
                                   critical=False, ref=o.client_order_id))
    return out


@dataclass(frozen=True)
class IncomeCheck:
    exchange_total: Decimal
    journal_total: Decimal
    diff_pct: float
    ok: bool


def reconcile_income(income: list[IncomeRecord], journal_pnl_net: float, capital: float,
                     tolerance: float = 0.005) -> IncomeCheck:
    """Compara el PnL neto del exchange (realizado + comisiones + funding) con el del diario.

    La diferencia se mide respecto al mayor entre |PnL| y 1 % del capital, para
    no disparar alertas por ruido cuando el PnL del periodo es casi cero.
    """
    kinds = {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}
    ex_total = sum((r.amount for r in income if r.kind in kinds), Decimal(0))
    j_total = Decimal(str(round(journal_pnl_net, 8)))
    base = max(abs(float(ex_total)), abs(float(j_total)), 0.01 * capital)
    diff = abs(float(ex_total - j_total)) / base if base else 0.0
    return IncomeCheck(ex_total, j_total, diff, diff <= tolerance)


def adopt_position(ep: ExchangePosition, stop: Order | None, now: datetime) -> Position:
    """Reconstruye una posición del exchange tras un reinicio (no se pierde el control)."""
    stop_px = stop.stop_price if stop and stop.stop_price else None
    fallback = ep.entry_price * (Decimal("0.98") if ep.side.value == "LONG" else Decimal("1.02"))
    return Position(symbol=ep.symbol, side=ep.side, qty=ep.qty, initial_qty=ep.qty, entry_price=ep.entry_price,
                    stop_price=stop_px or fallback, initial_stop_price=stop_px or fallback,
                    stop_order_id=stop.client_order_id if stop else None, strategy="adoptada",
                    signal_id=f"adopt-{ep.symbol}-{int(now.timestamp())}", opened_at=now)
