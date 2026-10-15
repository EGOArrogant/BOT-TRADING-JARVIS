"""Backtest de la parte determinista (estrategias + filtros reproducibles + riesgo).

La IA NO se backtestea (sesgo de memoria del modelo, CLAUDE.md §2). Supuestos
conservadores: entrada maker solo si el precio atraviesa el límite en la barra
siguiente; si stop y objetivo caen en la misma barra, gana el stop; salidas por
stop a mercado (taker + deslizamiento); funding prorrateado por hora.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from bot.core.config import Settings
from bot.core.models import DataPoint, Side, Signal
from bot.core.state import BotState, CloseRecord, EntryRecord
from bot.data_engine.market_state import INTERVAL_MS, Candle, MarketState
from bot.data_engine.store import FeedStore
from bot.filters.checks import check_dangerous_leverage, check_edge, check_extreme_vol, select_best
from bot.filters.strategies import generate
from bot.indicators.features import compute_features
from bot.indicators.ta import atr as atr_fn
from bot.position.rules import ActionKind, PosView, management_actions, partial_price
from bot.risk.costs import fee_rate
from bot.risk.engine import OpenExposure, RiskEngine, SymbolRules


@dataclass
class BtTrade:
    symbol: str
    strategy: str
    side: Side
    entry_time: datetime
    exit_time: datetime
    entry: float
    exit: float
    qty: float
    risk_amount: float
    fees: float
    funding: float          # positivo = pagado (costo)
    pnl_gross: float
    exit_reason: str
    maker_entry: bool

    @property
    def pnl_net(self) -> float:
        return self.pnl_gross - self.fees - self.funding

    @property
    def r(self) -> float:
        return self.pnl_net / self.risk_amount if self.risk_amount > 0 else 0.0


@dataclass
class _OpenPos:
    sig: Signal
    view: PosView
    qty: float
    initial_qty: float
    entry: float
    maker: bool
    fees: float
    funding: float = 0.0
    realized: float = 0.0
    partial_level: float = 0.0
    risk_amount: float = 0.0


@dataclass
class BtResult:
    trades: list[BtTrade]
    equity_curve: list[tuple[datetime, float]]
    candidates: int
    rejected: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, float]:
        n = len(self.trades)
        pnl = sum(t.pnl_net for t in self.trades)
        gross_win = sum(t.pnl_net for t in self.trades if t.pnl_net > 0)
        gross_loss = -sum(t.pnl_net for t in self.trades if t.pnl_net < 0)
        eq = np.array([e for _, e in self.equity_curve]) if self.equity_curve else np.array([0.0])
        peak = np.maximum.accumulate(eq)
        dd = float(np.max(1 - eq / peak)) if len(eq) else 0.0
        return {
            "trades": n,
            "pnl_net": pnl,
            "fees": sum(t.fees for t in self.trades),
            "funding": sum(t.funding for t in self.trades),
            "expectancy_r": float(np.mean([t.r for t in self.trades])) if n else 0.0,
            "win_rate": sum(1 for t in self.trades if t.pnl_net > 0) / n if n else 0.0,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0,
            "max_drawdown": dd,
            "candidates": self.candidates,
        }


def aggregate(candles: list[Candle], factor: int, interval_ms: int) -> list[Candle]:
    """Agrega velas (p. ej. 15m -> 1h) alineadas al intervalo destino."""
    out: list[Candle] = []
    bucket: list[Candle] = []
    for c in candles:
        key = c.open_time // interval_ms
        if bucket and bucket[0].open_time // interval_ms != key:
            if len(bucket) == factor:
                out.append(_merge(bucket))
            bucket = []
        bucket.append(c)
    if len(bucket) == factor:
        out.append(_merge(bucket))
    return out


def _merge(b: list[Candle]) -> Candle:
    return Candle(b[0].open_time, b[0].open, max(c.high for c in b), min(c.low for c in b), b[-1].close,
                  sum(c.volume for c in b), sum(c.quote_volume for c in b), sum(c.taker_buy_volume for c in b))


class Backtester:
    def __init__(self, settings: Settings, assumed_spread_bps: float = 2.0, funding_rate_1h: float = 0.0000125,
                 store: FeedStore | None = None, warmup_bars_15m: int = 800):
        self.s = settings
        self.risk = RiskEngine(settings.risk, settings.limits, settings.exchange)
        self.spread_bps = assumed_spread_bps
        self.funding_1h = funding_rate_1h
        self.store = store or FeedStore()
        self.warmup = warmup_bars_15m

    def _fees(self, maker: bool) -> float:
        return fee_rate(self.s.exchange.fees, maker)

    def run(self, data_15m: dict[str, list[Candle]], strategies=None) -> BtResult:
        strategies = strategies or self.s.strategies
        symbols = list(data_15m)
        states = {s: MarketState(s) for s in symbols}
        hourly = {s: aggregate(data_15m[s], 4, INTERVAL_MS["1h"]) for s in symbols}
        h_idx = {s: 0 for s in symbols}
        times = sorted({c.open_time for s in symbols for c in data_15m[s]})
        by_time = {s: {c.open_time: c for c in data_15m[s]} for s in symbols}
        bt_state = BotState.fresh(self.s.paper.initial_capital, datetime.fromtimestamp(times[0] / 1000, UTC).date())
        open_pos: dict[str, _OpenPos] = {}
        pending: list[Signal] = []
        trades: list[BtTrade] = []
        curve: list[tuple[datetime, float]] = []
        rejected: dict[str, int] = {}
        candidates = 0
        rules = SymbolRules(tick_size=1e-8, step_size=1e-8, min_qty=0.0, min_notional=5.0)

        for i, t in enumerate(times):
            bar_close = datetime.fromtimestamp((t + INTERVAL_MS["15m"]) / 1000, UTC)
            # 1) Ejecutar entradas pendientes con la barra actual
            for sig in pending:
                bar = by_time[sig.symbol].get(t)
                if bar is None or sig.symbol in open_pos:
                    continue
                self._try_enter(sig, bar, bt_state, open_pos, rules, rejected, bar_close)
            pending = []

            # 2) Actualizar posiciones abiertas con la barra
            for sym in list(open_pos):
                bar = by_time[sym].get(t)
                if bar is None:
                    continue
                tr = self._manage(open_pos[sym], bar, bar_close, states[sym])
                if tr is not None:
                    trades.append(tr)
                    del open_pos[sym]
                    bt_state.record_close(CloseRecord(symbol=sym, side=tr.side, closed_at=bar_close, pnl_net=tr.pnl_net))
                    self.risk.update_guards(bt_state, bar_close)

            # 3) Alimentar estado y generar señales al cierre
            for sym in symbols:
                bar = by_time[sym].get(t)
                if bar is None:
                    continue
                states[sym].candles["15m"].add_closed(bar)
                while h_idx[sym] < len(hourly[sym]) and \
                        hourly[sym][h_idx[sym]].open_time + INTERVAL_MS["1h"] <= t + INTERVAL_MS["15m"]:
                    states[sym].candles["1h"].add_closed(hourly[sym][h_idx[sym]])
                    h_idx[sym] += 1
            unreal = sum(self._unrealized(p, by_time[s].get(t)) for s, p in open_pos.items())
            curve.append((bar_close, bt_state.equity + unreal))
            if i < self.warmup:
                continue

            bt_state.roll_day(bar_close.date())
            passed: list[Signal] = []
            exposures = [OpenExposure(s, p.sig.side, p.qty * p.entry) for s, p in open_pos.items()]
            for sym in symbols:
                if by_time[sym].get(t) is None:
                    continue
                f = compute_features(states[sym], self.store, bar_close)
                f["market.spread_bps"] = DataPoint.ok(self.spread_bps, "assumed", bar_close)
                f["funding.rate_1h"] = DataPoint.ok(self.funding_1h, "assumed", bar_close)
                for sig in generate(sym, f, strategies, self.s.exchange.fees, bar_close, quality=1.0):
                    candidates += 1
                    checks = [check_edge(sig, self.s.filters), check_extreme_vol(f, self.s.filters),
                              self.risk.slot_check(bt_state, bar_close, sig.edge_ratio),
                              self.risk.symbol_limits(bt_state, sym, sig.side, bar_close, exposures),
                              self.risk.global_guard(bt_state, bar_close)]
                    if f.get("ind.funding_z", DataPoint.unknown("x")).is_ok:
                        checks.append(check_dangerous_leverage(sig, f, self.s.filters))
                    bad = [c.reason.split(":")[0].split(" ")[0] for c in checks if not c.ok]
                    if bad:
                        for b in bad:
                            rejected[b] = rejected.get(b, 0) + 1
                        continue
                    passed.append(sig)
            best, _ = select_best(passed)
            if best is not None:
                pending.append(best)

        # cerrar lo abierto al final
        for sym, p in open_pos.items():
            last = data_15m[sym][-1]
            trades.append(self._close(p, last.close, datetime.fromtimestamp((last.open_time + INTERVAL_MS["15m"]) / 1000, UTC),
                                      "fin_backtest", taker=True))
        return BtResult(trades, curve, candidates, rejected)

    # ------------------------------------------------------------ simulación

    def _try_enter(self, sig: Signal, bar: Candle, st: BotState, open_pos: dict[str, _OpenPos], rules: SymbolRules,
                   rejected: dict[str, int], ts: datetime) -> None:
        if sig.urgent and sig.edge_ratio >= self.s.execution.market_entry_min_edge:
            slip = self.spread_bps / 2 / 1e4 + 1e-4
            price = bar.open * (1 + slip) if sig.side is Side.LONG else bar.open * (1 - slip)
            maker = False
        else:
            touched = bar.low < sig.entry if sig.side is Side.LONG else bar.high > sig.entry
            if not touched:
                rejected["sin_llenado"] = rejected.get("sin_llenado", 0) + 1
                return
            price, maker = sig.entry, True
        exposures = [OpenExposure(s, p.sig.side, p.qty * p.entry) for s, p in open_pos.items()]
        size = self.risk.size(sig, st.equity, 1.0, vol_percentile=0.0, open_positions=exposures, correlations={},
                              rules=rules, depth_notional=1e12, entry_maker=maker, min_depth_multiple=1.0)
        if not size.ok:
            rejected["tamano"] = rejected.get("tamano", 0) + 1
            return
        # El stop mantiene su distancia en precio aunque la entrada real difiera
        view = PosView(sig.side, price, sig.stop, sig.stop, ts - timedelta(minutes=15), False,
                       sig.invalidation_price, sig.max_hold_hours)
        fee = size.qty * price * self._fees(maker)
        open_pos[sig.symbol] = _OpenPos(sig, view, size.qty, size.qty, price, maker, fee,
                                        partial_level=partial_price(sig.side, price, sig.stop, self.s.position.partial_take_r),
                                        risk_amount=size.qty * abs(price - sig.stop))
        st.record_entry(EntryRecord(symbol=sig.symbol, side=sig.side, ts=ts, edge_ratio=sig.edge_ratio, signal_id=sig.id))

    def _manage(self, p: _OpenPos, bar: Candle, ts: datetime, state: MarketState) -> BtTrade | None:
        side, v = p.sig.side, p.view
        long = side is Side.LONG
        p.funding += p.qty * bar.close * self.funding_1h * 0.25 * (1 if long else -1)
        # Stop primero (conservador)
        stop_hit = bar.low <= v.stop if long else bar.high >= v.stop
        if stop_hit:
            fill = min(v.stop, bar.open) if long else max(v.stop, bar.open)  # gap atraviesa el stop
            reason = "stop" if not p.view.partial_taken else "trailing"
            return self._close(p, fill, ts, reason, taker=True)
        # Parcial (orden límite reduce-only, maker)
        if not v.partial_taken:
            hit = bar.high >= p.partial_level if long else bar.low <= p.partial_level
            if hit:
                part = p.qty * self.s.position.partial_take_fraction
                p.realized += part * (p.partial_level - p.entry) * (1 if long else -1)
                p.fees += part * p.partial_level * self._fees(True)
                p.qty -= part
                v.partial_taken = True
        # Objetivo final (límite, maker)
        tp = p.sig.take_profit
        if tp is not None and (bar.high >= tp if long else bar.low <= tp):
            return self._close(p, tp, ts, "take_profit", taker=False)
        atr_arr = state.candles["15m"].arrays()
        atr_val = None
        if len(atr_arr["c"]) >= 15:
            a = atr_fn(atr_arr["h"], atr_arr["l"], atr_arr["c"], 14)
            atr_val = float(a[-1])
        for act in management_actions(v, bar.close, ts, self.s.position, atr_val, closed_15m=bar.close):
            if act.kind is ActionKind.CLOSE:
                return self._close(p, bar.close, ts, act.reason, taker=True)
            if act.kind is ActionKind.MOVE_STOP and act.price is not None and v.improves(act.price):
                v.stop = act.price
        return None

    def _close(self, p: _OpenPos, price: float, ts: datetime, reason: str, taker: bool) -> BtTrade:
        long = p.sig.side is Side.LONG
        if taker:
            slip = self.spread_bps / 2 / 1e4 + 1e-4
            price = price * (1 - slip) if long else price * (1 + slip)
        pnl = p.realized + p.qty * (price - p.entry) * (1 if long else -1)
        fees = p.fees + p.qty * price * self._fees(not taker)
        return BtTrade(p.sig.symbol, p.sig.strategy, p.sig.side, p.view.opened_at, ts, p.entry, price, p.initial_qty,
                       p.risk_amount, fees, p.funding, pnl, reason, p.maker)

    @staticmethod
    def _unrealized(p: _OpenPos, bar: Candle | None) -> float:
        if bar is None:
            return 0.0
        return p.realized + p.qty * (bar.close - p.entry) * (1 if p.sig.side is Side.LONG else -1)


def walk_forward(bt: Backtester, data_15m: dict[str, list[Candle]], folds: int = 4) -> list[dict[str, float]]:
    """Divide el periodo en `folds` tramos consecutivos y evalúa cada uno por separado.

    Con parámetros fijos de config no hay optimización: cada tramo es fuera de
    muestra. Si se optimizan parámetros, hacerlo solo con los tramos previos.
    """
    times = sorted({c.open_time for cs in data_15m.values() for c in cs})
    if len(times) < (folds + 1) * bt.warmup // folds:
        raise ValueError("datos insuficientes para walk-forward")
    usable = len(times) - bt.warmup
    size = usable // folds
    results = []
    for k in range(folds):
        start_t = times[k * size]
        end_t = times[bt.warmup + (k + 1) * size - 1]
        chunk = {s: [c for c in cs if start_t <= c.open_time <= end_t] for s, cs in data_15m.items()}
        results.append(bt.run(chunk).summary())
    return results
