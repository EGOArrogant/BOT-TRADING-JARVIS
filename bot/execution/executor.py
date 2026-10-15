"""MÓDULO 6 — Ejecución de entradas y protección inmediata.

Flujo: límite post-only en el mejor precio (o un tick dentro) -> espera X s ->
si la señal sigue válida, 1 reintento -> cancelar. Mercado solo para señales
urgentes con ventaja ≥ 4×. Tras el llenado: stop STOP_MARKET (close-position)
en el exchange; si no se confirma en N s, cierre a mercado. Luego take-profits
reduce-only (parcial + final). IDs de cliente deterministas = idempotencia.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from bot.core.clock import utc_now
from bot.core.config import ExecutionConfig, PositionConfig
from bot.core.logging_setup import get_logger
from bot.core.models import Order, OrderSide, OrderStatus, Position, Side, Signal
from bot.data_engine.market_state import MarketState
from bot.execution.base import Exchange, ExchangeError, UncertainOrderState
from bot.position.rules import partial_price, target_qty_split
from bot.risk.engine import SymbolRules, round_step, round_tick

log = get_logger("bot.executor")

D = Decimal


def client_id(kind: str, signal_id: str, attempt: int = 0) -> str:
    """ID ≤ 36 chars, determinista: el mismo intento siempre produce el mismo ID."""
    h = hashlib.sha1(f"{kind}|{signal_id}|{attempt}".encode()).hexdigest()[:24]
    return f"cb{kind[:3]}{attempt}-{h}"


def entry_side(side: Side) -> OrderSide:
    return OrderSide.BUY if side is Side.LONG else OrderSide.SELL


def exit_side(side: Side) -> OrderSide:
    return OrderSide.SELL if side is Side.LONG else OrderSide.BUY


@dataclass
class EntryResult:
    position: Position | None
    reason: str
    entry_order: Order | None = None
    slippage_pct: float | None = None


class Executor:
    def __init__(self, exchange: Exchange, states: dict[str, MarketState], exec_cfg: ExecutionConfig,
                 pos_cfg: PositionConfig, alert: Callable[[str], None] | None = None, poll_s: float = 0.25):
        self.ex = exchange
        self.states = states
        self.cfg = exec_cfg
        self.pos_cfg = pos_cfg
        self.alert = alert or (lambda msg: log.error("ALERTA", extra={"msg": msg}))
        self.poll_s = poll_s

    # ------------------------------------------------------------ utilidades

    async def _place_idempotent(self, symbol: str, cid: str, placer) -> Order | None:
        """Si ya existe una orden con ese ID (p. ej. tras reconexión), se reutiliza."""
        existing = await self.ex.get_order(symbol, cid)
        if existing is not None:
            return existing
        try:
            return await placer()
        except UncertainOrderState:
            await asyncio.sleep(1.0)
            return await self.ex.get_order(symbol, cid)

    async def _wait_fill(self, symbol: str, cid: str, timeout_s: float) -> Order | None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        order = None
        while asyncio.get_running_loop().time() < deadline:
            order = await self.ex.get_order(symbol, cid)
            if order is None or order.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED,
                                                 OrderStatus.REJECTED):
                return order
            await asyncio.sleep(self.poll_s)
        return order

    def _maker_price(self, symbol: str, side: Side, rules: SymbolRules) -> Decimal | None:
        book = self.states[symbol].book
        bid, ask = book.best_bid(), book.best_ask()
        if bid is None or ask is None:
            return None
        tick = rules.tick_size
        if side is Side.LONG:
            px = bid + tick if ask - bid > tick * 1.5 else bid  # un tick dentro si hay espacio
        else:
            px = ask - tick if ask - bid > tick * 1.5 else ask
        return D(str(round_tick(px, tick)))

    # ------------------------------------------------------------ entrada

    async def open_position(self, sig: Signal, qty: float, rules: SymbolRules, edge_ratio: float,
                            still_valid: Callable[[], bool], ai_confidence: float | None = None,
                            ai_size_multiplier: float | None = None) -> EntryResult:
        qty_d = D(str(round_step(qty, rules.step_size)))
        side = entry_side(sig.side)
        filled: Order | None = None
        use_market = sig.urgent and edge_ratio >= self.cfg.market_entry_min_edge

        if use_market:
            cid = client_id("ent", sig.id, 9)
            try:
                filled = await self._place_idempotent(
                    sig.symbol, cid, lambda: self.ex.place_market(sig.symbol, side, qty_d, cid))
            except ExchangeError as exc:
                return EntryResult(None, f"entrada a mercado rechazada: {exc}")
        else:
            for attempt in range(1 + self.cfg.post_only_retries):
                if attempt > 0 and not still_valid():
                    return EntryResult(None, "señal ya no válida para reintentar")
                price = self._maker_price(sig.symbol, sig.side, rules)
                if price is None:
                    return EntryResult(None, "libro no disponible")
                cid = client_id("ent", sig.id, attempt)
                try:
                    order = await self._place_idempotent(
                        sig.symbol, cid, lambda: self.ex.place_limit(sig.symbol, side, qty_d, price, cid, post_only=True))
                except ExchangeError as exc:
                    log.warning("entrada rechazada", extra={"symbol": sig.symbol, "err": str(exc)})
                    continue
                if order is None:
                    continue
                if order.is_open:
                    order = await self._wait_fill(sig.symbol, cid, self.cfg.post_only_timeout_s) or order
                if order.is_open:
                    try:
                        await self.ex.cancel(sig.symbol, cid)
                    except ExchangeError:
                        pass  # pudo llenarse justo antes de cancelar
                    order = await self.ex.get_order(sig.symbol, cid) or order
                if order.filled_qty > 0:
                    filled = order
                    break

        if filled is None or filled.filled_qty <= 0 or filled.avg_fill_price is None:
            return EntryResult(None, "sin llenado", filled)

        pos = await self._protect(sig, filled, rules, ai_confidence, ai_size_multiplier)
        slip = None
        if pos is not None:
            sign = 1 if sig.side is Side.LONG else -1
            slip = sign * (float(filled.avg_fill_price) - sig.entry) / sig.entry
        return EntryResult(pos, "abierta" if pos else "cerrada por falta de stop", filled, slip)

    async def _protect(self, sig: Signal, filled: Order, rules: SymbolRules, ai_conf: float | None,
                       ai_mult: float | None) -> Position | None:
        qty = filled.filled_qty
        fill_px = filled.avg_fill_price
        assert fill_px is not None
        # El stop conserva la distancia en precio de la señal respecto al llenado real
        dist = abs(sig.entry - sig.stop)
        raw_stop = float(fill_px) - dist if sig.side is Side.LONG else float(fill_px) + dist
        stop_px = D(str(round_tick(raw_stop, rules.tick_size, side_up=sig.side is Side.SHORT)))
        stop_cid = client_id("stp", sig.id, 0)
        stop_ok = False
        try:
            stop = await asyncio.wait_for(
                self._place_idempotent(sig.symbol, stop_cid,
                                       lambda: self.ex.place_stop(sig.symbol, exit_side(sig.side), stop_px, stop_cid)),
                timeout=self.cfg.stop_confirm_timeout_s)
            stop_ok = stop is not None and stop.is_open
        except (TimeoutError, ExchangeError) as exc:
            log.error("stop no confirmado", extra={"symbol": sig.symbol, "err": repr(exc)})
        if not stop_ok:
            self.alert(f"{sig.symbol}: stop no confirmado en {self.cfg.stop_confirm_timeout_s}s -> cierre a mercado")
            await self.flatten(sig.symbol, sig.side, qty, f"{sig.id}-nostop")
            return None

        pos = Position(
            symbol=sig.symbol, side=sig.side, qty=qty, initial_qty=qty, entry_price=fill_px, stop_price=stop_px,
            initial_stop_price=stop_px, take_profit_price=D(str(sig.take_profit)) if sig.take_profit else None,
            stop_order_id=stop_cid, strategy=sig.strategy, signal_id=sig.id, opened_at=filled.updated_at or utc_now(),
            invalidation=sig.invalidation,
            invalidation_price=D(str(sig.invalidation_price)) if sig.invalidation_price else None,
            max_hold_hours=sig.max_hold_hours, regime=sig.regime, expected_entry_price=D(str(sig.entry)),
            entry_maker=bool(filled.maker) if filled.maker is not None else not sig.urgent, entry_fee=filled.fee,
            ai_confidence=ai_conf, ai_size_multiplier=ai_mult,
        )
        await self._place_take_profits(pos, rules)
        return pos

    async def _place_take_profits(self, pos: Position, rules: SymbolRules) -> None:
        side = exit_side(pos.side)
        part_qty, rest_qty = target_qty_split(float(pos.qty), self.pos_cfg.partial_take_fraction, rules.step_size)
        try:
            if part_qty >= rules.min_qty and part_qty * float(pos.entry_price) >= rules.min_notional:
                lvl = partial_price(pos.side, float(pos.entry_price), float(pos.initial_stop_price),
                                    self.pos_cfg.partial_take_r)
                px = D(str(round_tick(lvl, rules.tick_size)))
                cid = client_id("ptp", pos.signal_id)
                await self._place_idempotent(pos.symbol, cid, lambda: self.ex.place_limit(
                    pos.symbol, side, D(str(part_qty)), px, cid, post_only=False, reduce_only=True))
            else:
                rest_qty = float(pos.qty)
            if pos.take_profit_price is not None:
                cid = client_id("ftp", pos.signal_id)
                px = D(str(round_tick(float(pos.take_profit_price), rules.tick_size)))
                await self._place_idempotent(pos.symbol, cid, lambda: self.ex.place_limit(
                    pos.symbol, side, D(str(rest_qty)), px, cid, post_only=False, reduce_only=True))
                pos.tp_order_id = cid
        except ExchangeError as exc:
            # Sin TP la posición sigue protegida por el stop y el gestor la cierra por reglas
            log.warning("take-profit no colocado", extra={"symbol": pos.symbol, "err": str(exc)})

    # ------------------------------------------------------------ salidas

    async def flatten(self, symbol: str, side: Side, qty: Decimal, tag: str) -> Order | None:
        """Cierre a mercado reduce-only + cancelación de órdenes de la moneda."""
        cid = client_id("cls", tag)
        order = None
        try:
            order = await self._place_idempotent(symbol, cid, lambda: self.ex.place_market(
                symbol, exit_side(side), qty, cid, reduce_only=True))
        except ExchangeError as exc:
            self.alert(f"{symbol}: cierre a mercado falló: {exc}")
        await self.cancel_all(symbol)
        return order

    async def cancel_all(self, symbol: str) -> None:
        for o in await self.ex.open_orders(symbol):
            try:
                await self.ex.cancel(symbol, o.client_order_id)
            except ExchangeError:
                pass

    async def ensure_stop(self, pos: Position, rules: SymbolRules, tag: str) -> bool:
        """Repone el stop de una posición que lo perdió (al nivel actual registrado)."""
        cid = client_id("stp", f"{pos.signal_id}-{tag}")
        px = D(str(round_tick(float(pos.stop_price), rules.tick_size, side_up=pos.side is Side.SHORT)))
        try:
            o = await self._place_idempotent(pos.symbol, cid, lambda: self.ex.place_stop(
                pos.symbol, exit_side(pos.side), px, cid))
        except ExchangeError as exc:
            self.alert(f"{pos.symbol}: no se pudo reponer el stop: {exc}")
            return False
        if o is None or not o.is_open:
            return False
        pos.stop_order_id = cid
        return True

    async def replace_stop(self, pos: Position, new_stop: Decimal, rules: SymbolRules, tag: str) -> bool:
        """Mueve el stop solo a favor. Primero coloca el nuevo y luego cancela el viejo."""
        better = new_stop > pos.stop_price if pos.side is Side.LONG else new_stop < pos.stop_price
        if not better:
            return False
        px = D(str(round_tick(float(new_stop), rules.tick_size, side_up=pos.side is Side.SHORT)))
        cid = client_id("stp", f"{pos.signal_id}-{tag}")
        try:
            o = await self._place_idempotent(pos.symbol, cid, lambda: self.ex.place_stop(
                pos.symbol, exit_side(pos.side), px, cid))
        except ExchangeError as exc:
            log.warning("no se pudo mover el stop", extra={"symbol": pos.symbol, "err": str(exc)})
            return False
        if o is None or not o.is_open:
            return False
        old = pos.stop_order_id
        pos.stop_order_id, pos.stop_price = cid, px
        if old and old != cid:
            try:
                await self.ex.cancel(pos.symbol, old)
            except ExchangeError:
                pass
        return True

