"""MÓDULO 5 — Motor de riesgo: guardias globales, techo diario y tamaño.

Nada de aquí puede ser modificado por la IA ni por el aprendizaje. La IA solo
aporta `size_multiplier` en [0.5, 1.0], que únicamente reduce el tamaño.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bot.core.config import ExchangeConfig, LimitsConfig, RiskConfig
from bot.core.logging_setup import get_logger
from bot.core.models import Side, Signal
from bot.core.state import BotState
from bot.risk.costs import fee_rate

log = get_logger("bot.risk")


@dataclass(frozen=True)
class Check:
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class OpenExposure:
    symbol: str
    side: Side
    notional: float


@dataclass(frozen=True)
class SymbolRules:
    """Reglas del exchange por símbolo (exchangeInfo)."""

    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float


@dataclass
class SizeResult:
    ok: bool
    reason: str = ""
    qty: float = 0.0
    notional: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)


def round_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return float(int(x / step + 1e-9) * step)


def round_tick(price: float, tick: float, side_up: bool | None = None) -> float:
    if tick <= 0:
        return price
    n = price / tick
    if side_up is True:
        n = math.ceil(n - 1e-9)
    elif side_up is False:
        n = math.floor(n + 1e-9)
    else:
        n = round(n)
    return round(n * tick, 12)


class RiskEngine:
    def __init__(self, risk: RiskConfig, limits: LimitsConfig, exchange: ExchangeConfig):
        self.risk = risk
        self.limits = limits
        self.exchange = exchange

    # ------------------------------------------------------------ guardias globales

    def update_guards(self, state: BotState, now: datetime) -> None:
        """Activa detenciones/pausas según el estado. Llamar tras cada cierre y periódicamente."""
        state.roll_day(now.date())
        if state.drawdown >= self.risk.max_drawdown and not state.halted:
            state.halted = True
            state.halt_reason = f"drawdown {state.drawdown:.2%} ≥ {self.risk.max_drawdown:.0%}"
            log.error("BOT DETENIDO", extra={"reason": state.halt_reason})
        if state.consecutive_losses() >= self.risk.max_consecutive_losses:
            last = state.closes[-1].closed_at
            until = last + timedelta(hours=self.risk.consecutive_loss_pause_hours)
            # Solo una pausa por racha: se activa si el último cierre es nuevo
            if until > now and (state.pause_until is None or state.pause_until < until):
                state.pause_until = until
                log.warning("pausa por pérdidas seguidas", extra={"until": until.isoformat()})

    def global_guard(self, state: BotState, now: datetime) -> Check:
        if state.halted:
            return Check(False, f"detenido: {state.halt_reason} (requiere reinicio manual)")
        if state.safe_mode:
            return Check(False, f"modo seguro: {state.safe_mode_reason}")
        if state.pause_until and now < state.pause_until:
            return Check(False, f"pausa hasta {state.pause_until.isoformat()}")
        if state.day_pnl_pct <= -self.risk.max_daily_loss:
            return Check(False, f"pérdida diaria {state.day_pnl_pct:.2%} ≥ {self.risk.max_daily_loss:.0%}")
        return Check(True)

    # ------------------------------------------------------------ techo diario

    def daily_cap(self, state: BotState, now: datetime) -> int:
        today = now.date()
        losses = sum(1 for c in state.closes_on(today) if c.pnl_net < 0)
        cap = self.limits.max_trades_per_day
        if losses >= self.limits.losing_day_loss_count or state.day_pnl_pct <= -self.limits.losing_day_loss_pct:
            cap = min(cap, self.limits.losing_day_trade_cap)
        return cap

    def slot_check(self, state: BotState, now: datetime, edge_ratio: float,
                   ai_confidence: float | None = None) -> Check:
        """Entrada n-ésima del día. Llamar antes de la IA (sin confianza) y después (con ella)."""
        used = len(state.entries_on(now.date()))
        cap = self.daily_cap(state, now)
        n = used + 1
        if n > cap:
            return Check(False, f"techo diario alcanzado ({used}/{cap})")
        if n > self.limits.base_trades_per_day:
            if edge_ratio < self.limits.extra_trade_min_edge:
                return Check(False, f"entrada #{n} exige ventaja ≥ {self.limits.extra_trade_min_edge}× "
                                    f"(tiene {edge_ratio:.2f}×)")
            if ai_confidence is not None and ai_confidence < self.limits.extra_trade_min_confidence:
                return Check(False, f"entrada #{n} exige confianza IA ≥ {self.limits.extra_trade_min_confidence}")
        return Check(True)

    # ------------------------------------------------------------ límites por moneda

    def symbol_limits(self, state: BotState, symbol: str, side: Side, now: datetime,
                      open_positions: list[OpenExposure]) -> Check:
        if sum(1 for p in open_positions if p.symbol == symbol) >= self.risk.max_positions_per_symbol:
            return Check(False, "ya hay posición en la moneda")
        if len(open_positions) >= self.risk.max_concurrent_positions:
            return Check(False, "máximo de posiciones simultáneas")
        today_sym = [e for e in state.entries_on(now.date()) if e.symbol == symbol]
        if len(today_sym) >= self.limits.max_trades_per_symbol_per_day:
            return Check(False, "máximo de operaciones por moneda hoy")
        closes = [c for c in state.closes if c.symbol == symbol]
        same = [c for c in closes if c.side is side][-self.limits.underperf_lookback_trades:]
        if self.limits.underperf_enabled and len(same) >= self.limits.underperf_lookback_trades and state.equity > 0:
            lost = -sum(c.pnl_net for c in same) / state.equity
            until = same[-1].closed_at + timedelta(hours=self.limits.underperf_lock_hours)
            if lost > self.limits.underperf_max_loss_pct and now < until:
                return Check(False, f"bajo rendimiento: {symbol} {side.value} perdió {lost:.1%} en sus últimas "
                                    f"{len(same)} operaciones; en pausa hasta {until.isoformat()}")
        if closes:
            last = closes[-1]
            cooldown = timedelta(minutes=self.limits.symbol_cooldown_minutes)
            if side is not last.side:
                cooldown = timedelta(minutes=self.limits.reversal_cooldown_minutes)
            if now < last.closed_at + cooldown:
                return Check(False, f"enfriamiento hasta {(last.closed_at + cooldown).isoformat()}")
        return Check(True)

    # ------------------------------------------------------------ tamaño

    def size(
        self,
        signal: Signal,
        equity: float,
        ai_size_multiplier: float,
        vol_percentile: float | None,
        open_positions: list[OpenExposure],
        correlations: dict[str, float],
        rules: SymbolRules,
        depth_notional: float | None,
        entry_maker: bool,
        min_depth_multiple: float,
    ) -> SizeResult:
        if not 0.5 <= ai_size_multiplier <= 1.0:
            return SizeResult(False, "size_multiplier fuera de [0.5, 1.0]")
        if equity <= 0:
            return SizeResult(False, "equity no positiva")
        stop_pct = signal.stop_distance_pct
        if stop_pct <= 0:
            return SizeResult(False, "distancia de stop nula")

        base = equity * self.risk.risk_per_trade / stop_pct
        f_vol = 1.0
        if vol_percentile is None:
            f_vol = self.risk.high_vol_size_factor  # desconocido: tratar como alto
        elif vol_percentile >= self.risk.high_vol_percentile:
            f_vol = self.risk.high_vol_size_factor
        f_corr = 1.0
        for p in open_positions:
            corr = correlations.get(p.symbol)
            if p.side is signal.side and (corr is None or corr > self.risk.correlation_threshold):
                f_corr = self.risk.correlation_size_factor
                break
        notional = base * ai_size_multiplier * f_vol * f_corr
        factors = {"base": base, "ai": ai_size_multiplier, "vol": f_vol, "corr": f_corr}

        # Topes: apalancamiento total, exposición neta direccional, liquidez
        gross = sum(p.notional for p in open_positions)
        cap_lev = self.risk.max_leverage * equity - gross
        sign = 1 if signal.side is Side.LONG else -1
        net = sum(p.notional * (1 if p.side is Side.LONG else -1) for p in open_positions)
        cap_net = self.risk.max_net_exposure * equity - sign * net
        caps = {"leverage": cap_lev, "net_exposure": cap_net}
        if depth_notional is not None:
            caps["liquidity"] = depth_notional / min_depth_multiple
        else:
            return SizeResult(False, "profundidad del libro desconocida", factors=factors)
        for name, cap in caps.items():
            if notional > cap:
                factors[f"cap_{name}"] = cap
                notional = cap
        if notional <= 0:
            return SizeResult(False, "sin margen de exposición", factors=factors)

        qty = round_step(notional / signal.entry, rules.step_size)
        notional = qty * signal.entry
        if qty < rules.min_qty or notional < rules.min_notional:
            return SizeResult(False, f"nocional {notional:.2f} bajo el mínimo del exchange", factors=factors)

        fee_cost = notional * (fee_rate(self.exchange.fees, entry_maker) + fee_rate(self.exchange.fees, False))
        target_profit = notional * signal.expected_move_pct
        if fee_cost > self.risk.max_fee_share_of_target * target_profit:
            return SizeResult(False, "la comisión supera 25% del objetivo", factors=factors)
        return SizeResult(True, qty=qty, notional=notional, factors=factors)
