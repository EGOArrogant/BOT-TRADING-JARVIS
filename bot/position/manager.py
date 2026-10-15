"""MÓDULO 7 — Gestión de posición en vivo/papel.

Vigila cada posición contra el estado real del exchange: detecta parcial
llenado, cierre por stop/TP, aplica reglas (equilibrio, trailing, tiempo,
invalidación, macro, funding) y produce el registro `Trade` al cerrar.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal

from bot.core.config import PositionConfig
from bot.core.logging_setup import get_logger
from bot.core.models import ExitReason, Position, Side, Trade
from bot.data_engine.market_state import MarketState
from bot.execution.base import Exchange, Fill
from bot.execution.executor import Executor, client_id
from bot.position.rules import ActionKind, PosView, management_actions
from bot.risk.engine import SymbolRules

log = get_logger("bot.position")

D = Decimal

# Pagar más que esto en un solo cobro de funding sin ganancia -> evaluar cierre
FUNDING_ALERT_RATE = 0.0005
FUNDING_LOOKAHEAD = timedelta(minutes=15)

REASON_MAP = {
    "invalidacion": ExitReason.INVALIDATION, "tiempo": ExitReason.TIME, "tiempo_max": ExitReason.TIME,
    "macro": ExitReason.MACRO, "funding": ExitReason.FUNDING, "ia": ExitReason.AI_EARLY_EXIT,
    "manual": ExitReason.MANUAL, "modo_seguro": ExitReason.SAFE_MODE,
}

OnClosed = Callable[[Trade, Position], Awaitable[None] | None]


def view_of(pos: Position) -> PosView:
    return PosView(pos.side, float(pos.entry_price), float(pos.initial_stop_price), float(pos.stop_price),
                   pos.opened_at, pos.partial_taken,
                   float(pos.invalidation_price) if pos.invalidation_price else None, pos.max_hold_hours)


def build_trade(pos: Position, fills: list[Fill], funding: Decimal, reason: ExitReason, closed_at: datetime) -> Trade:
    exit_sell = pos.side is Side.LONG
    exit_fills = [f for f in fills if (f.side.value == "SELL") == exit_sell]
    fees = sum((f.fee for f in fills), D(0))
    gross = sum((f.realized_pnl for f in exit_fills), D(0))
    q = sum((f.qty for f in exit_fills), D(0))
    exit_px = (sum((f.qty * f.price for f in exit_fills), D(0)) / q) if q > 0 else pos.entry_price
    return Trade(
        id=uuid.uuid4().hex[:16], signal_id=pos.signal_id, symbol=pos.symbol, side=pos.side, strategy=pos.strategy,
        regime=pos.regime, qty=pos.initial_qty or pos.qty, entry_price=pos.entry_price, exit_price=exit_px,
        expected_entry_price=pos.expected_entry_price or pos.entry_price, initial_stop_price=pos.initial_stop_price,
        entry_maker=pos.entry_maker, fees=fees, funding=funding, pnl_gross=gross, mae_r=pos.mae_r, mfe_r=pos.mfe_r,
        opened_at=pos.opened_at, closed_at=closed_at, exit_reason=reason, ai_confidence=pos.ai_confidence,
        ai_size_multiplier=pos.ai_size_multiplier,
    )


class PositionManager:
    def __init__(self, exchange: Exchange, executor: Executor, states: dict[str, MarketState], cfg: PositionConfig,
                 on_closed: OnClosed):
        self.ex = exchange
        self.executor = executor
        self.states = states
        self.cfg = cfg
        self.on_closed = on_closed
        self.positions: dict[str, Position] = {}
        self.rules: dict[str, SymbolRules] = {}
        self._pending_reason: dict[str, str] = {}

    def add(self, pos: Position, rules: SymbolRules) -> None:
        self.positions[pos.symbol] = pos
        self.rules[pos.symbol] = rules

    def _exit_reason(self, pos: Position, fills: list[Fill]) -> ExitReason:
        pending = self._pending_reason.pop(pos.symbol, None)
        if pending:
            return REASON_MAP.get(pending, ExitReason.MANUAL)
        last = fills[-1].order_client_id if fills else ""
        if last == client_id("ftp", pos.signal_id):
            return ExitReason.TAKE_PROFIT
        if last.startswith("cbstp"):
            return ExitReason.TRAILING if pos.partial_taken else ExitReason.STOP
        return ExitReason.STOP

    async def _finalize(self, pos: Position, now: datetime) -> None:
        fills = await self.ex.fills(pos.symbol, pos.opened_at - timedelta(seconds=5))
        income = await self.ex.income(pos.opened_at)
        funding = sum((i.amount for i in income if i.symbol == pos.symbol and i.kind == "FUNDING_FEE"), D(0))
        trade = build_trade(pos, fills, funding, self._exit_reason(pos, fills), now)
        await self.executor.cancel_all(pos.symbol)
        self.positions.pop(pos.symbol, None)
        log.info("posición cerrada", extra={"symbol": pos.symbol, "reason": trade.exit_reason.value,
                                            "pnl_net": str(trade.pnl_net), "r": str(round(trade.r_multiple, 3))})
        res = self.on_closed(trade, pos)
        if hasattr(res, "__await__"):
            await res  # type: ignore[misc]

    async def request_close(self, symbol: str, reason: str) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return
        self._pending_reason[symbol] = reason
        await self.executor.flatten(symbol, pos.side, pos.qty, f"{pos.signal_id}-{reason}")

    async def tick(self, now: datetime, atr_15m: dict[str, float | None], closed_15m: dict[str, float],
                   macro_soon: bool) -> None:
        if not self.positions:
            return
        ex_pos = {p.symbol: p for p in await self.ex.positions()}
        for sym, pos in list(self.positions.items()):
            ep = ex_pos.get(sym)
            if ep is None or ep.side is not pos.side:
                await self._finalize(pos, now)
                continue
            if ep.qty < pos.qty:
                pos.qty = ep.qty
                if not pos.partial_taken:
                    pos.partial_taken = True
                    log.info("parcial ejecutado", extra={"symbol": sym, "qty_left": str(ep.qty)})
            st = self.states[sym]
            price = st.mark_price or st.last_price
            if price is None:
                continue
            r = float(pos.r_multiple(D(str(price))))
            pos.mae_r, pos.mfe_r = min(pos.mae_r, r), max(pos.mfe_r, r)

            # Funding: cobro inminente caro y sin ganancia -> cerrar
            if st.next_funding_ms and st.funding_rate is not None and r <= 0:
                until = datetime.fromtimestamp(st.next_funding_ms / 1000, tz=now.tzinfo) - now
                pays = st.funding_rate if pos.side is Side.LONG else -st.funding_rate
                if timedelta(0) <= until <= FUNDING_LOOKAHEAD and pays >= FUNDING_ALERT_RATE:
                    await self.request_close(sym, "funding")
                    continue

            actions = management_actions(view_of(pos), price, now, self.cfg, atr_15m.get(sym),
                                         closed_15m=closed_15m.get(sym), macro_soon=macro_soon)
            for act in actions:
                if act.kind is ActionKind.CLOSE:
                    await self.request_close(sym, act.reason)
                    break
                if act.kind is ActionKind.MOVE_STOP and act.price is not None:
                    await self.executor.replace_stop(pos, D(str(act.price)), self.rules[sym],
                                                     tag=f"{act.reason}-{int(now.timestamp())}")
