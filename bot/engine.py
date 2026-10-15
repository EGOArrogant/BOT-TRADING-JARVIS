"""Orquestador del bot (modos record | paper | testnet | live).

Flujo por ventana de 15 min (al cerrar la vela de 15m de todos los símbolos):
datos -> indicadores -> estrategias -> filtros (+ cambios aprendidos) ->
mejor candidato -> IA -> techo diario con confianza -> tamaño -> ejecución.
En paralelo: gestión de posiciones (1 s), conciliación (30 s), tareas
periódicas (1 min): equity, guardias, contrafactuales, reportes, aprendizaje.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from bot.ai_analyst.client import Analyst
from bot.ai_analyst.prompt_builder import build_user_prompt
from bot.core.alerts import Alerter
from bot.core.clock import Clock, ms_to_dt
from bot.core.config import Mode, Secrets, Settings
from bot.core.health import HealthMonitor
from bot.core.logging_setup import get_logger
from bot.core.models import AIAction, DataPoint, Position, Side, Signal, Snapshot, Trade
from bot.core.state import BotState, CloseRecord, EntryRecord
from bot.data_engine.dex import HyperliquidPoller
from bot.data_engine.derivatives import DerivativesPoller
from bot.data_engine.http import HttpClient, RateLimiter
from bot.data_engine.macro import MacroCalendar, upcoming
from bot.data_engine.market_state import Candle, MarketState
from bot.data_engine.market_ws import BinanceMarketData, MarketDispatcher
from bot.data_engine.news import NewsFeed, score_headlines
from bot.data_engine.normalizer import build_raw_snapshot, data_quality
from bot.data_engine.onchain import OnchainPoller
from bot.data_engine.recorder import Recorder
from bot.data_engine.sentiment import FearGreedPoller
from bot.data_engine.store import FeedStore
from bot.data_engine.volatility import DvolPoller
from bot.execution.base import Exchange
from bot.execution.executor import Executor
from bot.execution.paper import PaperExchange
from bot.filters.checks import run_filters, select_best
from bot.filters.strategies import generate
from bot.indicators import ta
from bot.indicators.features import compute_features
from bot.learning import counterfactual
from bot.learning.champion_challenger import ChampionChallenger
from bot.learning.daily_report import write_zone
from bot.learning.hypotheses import effective_strategies, load_overrides, overrides_allow
from bot.learning.journal import AI_REJECTED, NO_FILL, NOT_SELECTED, REJECTED, RISK_REJECTED, TRADED, Journal
from bot.learning.lessons import LessonBook
from bot.learning.weekly_reflection import last_week, run_reflection, write_reflection_md
from bot.metrics.exchange_reconcile import adopt_position, reconcile_income, reconcile_state
from bot.metrics.performance import summarize
from bot.metrics.report_html import write_report
from bot.position.manager import PositionManager
from bot.risk.engine import OpenExposure, RiskEngine, SymbolRules

log = get_logger("bot.engine")

RECORD_RAW_EVENTS = {"aggTrade", "kline", "markPriceUpdate"}  # el depth crudo se muestrea aparte


@dataclass
class Components:
    """Dependencias inyectables (los tests reemplazan exchange/analista/datos)."""

    states: dict[str, MarketState]
    store: FeedStore
    exchange: Exchange | None
    analyst: Analyst | None
    journal: Journal
    macro: MacroCalendar | None = None
    news: NewsFeed | None = None
    extra_tasks: list[Any] = field(default_factory=list)


class Engine:
    def __init__(self, settings: Settings, secrets: Secrets, comps: Components, alert: Alerter | None = None,
                 clock: Clock | None = None):
        self.s = settings
        self.secrets = secrets
        self.c = comps
        self.clock = clock or Clock()
        self.alert = alert or Alerter(False, None, None)
        self.risk = RiskEngine(settings.risk, settings.limits, settings.exchange)
        self.state = BotState.load_or_new(settings.storage.state_path, settings.paper.initial_capital,
                                          self.clock.trading_day())
        self.journal = comps.journal
        self.cc = ChampionChallenger(self.journal, settings.learning.challenger_min_cases,
                                     settings.learning.challenger_min_trades, settings.learning.require_human_approval,
                                     settings.storage.reports_dir)
        self.lessons = LessonBook(self.journal, settings.learning.lesson_expiry_days, settings.ai.max_active_lessons)
        self.overrides = load_overrides()
        self.executor: Executor | None = None
        self.manager: PositionManager | None = None
        if comps.exchange is not None:
            self.executor = Executor(comps.exchange, comps.states, settings.execution, settings.position, alert=self.alert)
            self.manager = PositionManager(comps.exchange, self.executor, comps.states, settings.position,
                                           on_closed=self._on_closed)
        self.health = HealthMonitor(settings.data.dir / "health.json",
                                    {"ws_market": 30, "ws_public": 30, "macro": 36 * 3600})
        self._closed_15m: dict[str, dict[int, float]] = {s: {} for s in comps.states}
        self._last_window: int | None = None
        self._entry_lock = asyncio.Lock()
        self._in_flight: set[str] = set()
        self._clean_reconciles = 0
        self._last_hourly: int | None = None
        self._last_daily: str | None = None
        self._last_weekly: str | None = None
        self._pending_closed_15m: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ eventos de mercado

    async def on_kline_closed(self, symbol: str, interval: str, candle: Candle) -> None:
        if interval != "15m":
            return
        close_ts = candle.open_time + 900_000
        self._closed_15m[symbol][close_ts] = candle.close
        self._pending_closed_15m[symbol] = candle.close
        if all(close_ts in v for v in self._closed_15m.values()):
            # En segundo plano: la IA y la ejecución no deben bloquear el flujo del WebSocket
            task = asyncio.create_task(self.evaluate_window(ms_to_dt(close_ts)))
            self._tasks.add(task)
            task.add_done_callback(self._task_done)
        for v in self._closed_15m.values():
            for old in [k for k in v if k < close_ts - 4 * 900_000]:
                v.pop(old, None)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("evaluación falló", extra={"err": repr(task.exception())})

    # ------------------------------------------------------------ evaluación

    def _fields(self, symbol: str, now: datetime) -> tuple[dict[str, DataPoint], float]:
        st = self.c.states[symbol]
        raw = build_raw_snapshot(st, self.c.store, now, self.s.data.ttl_s)
        feats = compute_features(st, self.c.store, now, btc_state=self.c.states.get("BTCUSDT"))
        fields = {**raw.fields, **feats}
        return fields, data_quality(raw)

    def _open_exposures(self) -> list[OpenExposure]:
        if self.manager is None:
            return []
        return [OpenExposure(p.symbol, p.side, float(p.qty * p.entry_price)) for p in self.manager.positions.values()]

    def _macro(self) -> tuple[list, bool, datetime | None]:
        m = self.c.macro
        return (m.events, m.ok, m.updated_at) if m else ([], False, None)

    async def evaluate_window(self, now: datetime) -> Signal | None:
        window = int(now.timestamp()) // (self.s.limits.candidate_window_minutes * 60)
        if self._last_window == window:
            return None
        self._last_window = window
        self.risk.update_guards(self.state, now)
        events, macro_ok, macro_upd = self._macro()
        strategies = effective_strategies(self.s.strategies, self.overrides)
        exposures = self._open_exposures()
        passed: list[tuple[Signal, dict]] = []
        snapshots: dict[str, dict[str, DataPoint]] = {}
        for sym in self.c.states:
            fields, quality = self._fields(sym, now)
            snapshots[sym] = fields
            for sig in generate(sym, fields, strategies, self.s.exchange.fees, now, quality):
                out = run_filters(sig, fields, now, self.state, self.risk, self.s.risk, self.s.filters, events,
                                  macro_ok, macro_upd, exposures)
                ok_learned, why = overrides_allow(self.overrides, sig, fields)
                reasons = out.reasons + ([why] if not ok_learned else [])
                self.journal.record_candidate(sig, REJECTED if reasons else NOT_SELECTED, reasons,
                                              self._flat(fields))
                if not reasons:
                    passed.append((sig, fields))
            self._shadow(sym, fields, strategies, now, events, macro_ok, macro_upd, exposures)
        best, rest = select_best([s for s, _ in passed])
        for s in rest:
            self.journal.update_candidate(s.id, status=NOT_SELECTED, reasons=["no seleccionado (menor puntuación)"])
        if best is None:
            return None
        if self.c.exchange is None or self.c.analyst is None:
            return best
        fields = next(f for s, f in passed if s.id == best.id)
        await self._decide_and_execute(best, fields, now)
        return best

    def _shadow(self, sym, fields, strategies, now, events, macro_ok, macro_upd, exposures) -> None:
        for h in self.cc.active():
            for sig in generate(sym, fields, h.apply_params(strategies), self.s.exchange.fees, now,
                                data_quality_from(fields)):
                out = run_filters(sig, fields, now, self.state, self.risk, self.s.risk, self.s.filters, events,
                                  macro_ok, macro_upd, exposures)
                if out.passed and overrides_allow(self.overrides, sig, fields)[0]:
                    self.cc.record_shadow(h, sig, fields)

    @staticmethod
    def _flat(fields: dict[str, DataPoint]) -> dict[str, Any]:
        return {k: (dp.value if dp.is_ok else None) for k, dp in sorted(fields.items())}

    async def _headlines(self, now: datetime) -> list:
        if self.c.news is None:
            return []
        cand = score_headlines(self.c.news.items, now, self.s.news.symbol_keywords, self.s.news.trusted_sources,
                               self.s.news.high_impact_keywords, self.s.data.news_max_age_min,
                               self.s.news.dedupe_similarity, top_n=20)
        if self.c.analyst is not None and cand:
            cand = await self.c.analyst.score_news(cand)
        return cand[: self.s.ai.max_news_headlines]

    def _recent_stats(self, sig: Signal) -> dict:
        rows = [t for t in self.journal.trades(datetime(2000, 1, 1, tzinfo=UTC))
                if t["strategy"] == sig.strategy and t["symbol"] == sig.symbol][-30:]
        s = summarize(rows, self.state.equity)
        return {k: s.get(k) for k in ("n", "expectancy_r", "win_rate", "profit_factor")}

    def _correlations(self, symbol: str) -> dict[str, float]:
        out = {}
        a = self.c.states[symbol].candles["5m"].arrays()["c"][-289:]
        for p in self._open_exposures():
            b = self.c.states[p.symbol].candles["5m"].arrays()["c"][-289:]
            corr = ta.correlation(a, b)
            if corr is not None:
                out[p.symbol] = corr
        return out

    async def _decide_and_execute(self, sig: Signal, fields: dict[str, DataPoint], now: datetime) -> None:
        assert self.c.analyst is not None and self.c.exchange is not None and self.executor and self.manager
        if self._entry_lock.locked():
            self.journal.update_candidate(sig.id, status=RISK_REJECTED, reasons=["otra entrada en curso"])
            return
        async with self._entry_lock:
            headlines = await self._headlines(now)
            events, _, _ = self._macro()
            open_pos = [{"simbolo": p.symbol, "lado": p.side.value, "nocional": round(p.notional, 2)}
                        for p in self._open_exposures()]
            prompt = build_user_prompt(sig, fields, [h for h in headlines if not h.symbols or sig.symbol in h.symbols]
                                       or headlines, upcoming(now, events, 24), open_pos, self.lessons.active(),
                                       self._recent_stats(sig), now)
            res = await self.c.analyst.decide(sig, prompt)
            d = res.decision
            self.journal.record_ai_call(now, sig.id, res.model, res.valid, res.reason, d.model_dump(mode="json"),
                                        res.input_tokens, res.output_tokens, res.latency_s)
            self.journal.update_candidate(sig.id, ai=d.model_dump(mode="json"))
            if d.action is AIAction.NO_TRADE:
                self.journal.update_candidate(sig.id, status=AI_REJECTED, reasons=[f"IA: {res.reason}: {d.thesis}"])
                return
            # Re-chequeos con la confianza de la IA y el estado actual
            for name, chk in (("techo_diario", self.risk.slot_check(self.state, now, sig.edge_ratio, d.confidence)),
                              ("guardias", self.risk.global_guard(self.state, now))):
                if not chk.ok:
                    self.journal.update_candidate(sig.id, status=RISK_REJECTED, reasons=[f"{name}: {chk.reason}"])
                    return
            rules = await self.c.exchange.symbol_rules(sig.symbol)
            depth = fields.get("market.depth_notional")
            size = self.risk.size(sig, self.state.equity, d.size_multiplier,
                                  fields.get("ind.vol_pct_30d").as_float() if fields.get("ind.vol_pct_30d") else None,
                                  self._open_exposures(), self._correlations(sig.symbol), rules,
                                  depth.as_float() if depth else None, entry_maker=not sig.urgent,
                                  min_depth_multiple=self.s.filters.min_depth_multiple)
            self.journal.update_candidate(sig.id, size={"qty": size.qty, "notional": size.notional, **size.factors})
            if not size.ok:
                self.journal.update_candidate(sig.id, status=RISK_REJECTED, reasons=[f"tamaño: {size.reason}"])
                return
            self._in_flight.add(sig.symbol)
            try:
                result = await self.executor.open_position(
                    sig, size.qty, rules, sig.edge_ratio, still_valid=lambda: self._still_valid(sig),
                    ai_confidence=d.confidence, ai_size_multiplier=d.size_multiplier)
            finally:
                self._in_flight.discard(sig.symbol)
            if result.position is None:
                self.journal.update_candidate(sig.id, status=NO_FILL, reasons=[result.reason])
                return
            self.manager.add(result.position, rules)
            self.state.record_entry(EntryRecord(symbol=sig.symbol, side=sig.side, ts=now, edge_ratio=sig.edge_ratio,
                                                signal_id=sig.id))
            self.state.save(self.s.storage.state_path)
            self.journal.update_candidate(sig.id, status=TRADED)
            self.alert(f"ENTRADA {sig.symbol} {sig.side.value} {sig.strategy} qty={result.position.qty} "
                       f"@{result.position.entry_price} stop={result.position.stop_price} conf={d.confidence:.2f}",
                       level="info")

    def _still_valid(self, sig: Signal) -> bool:
        st = self.c.states[sig.symbol]
        px = st.last_price
        if px is None:
            return False
        dist = abs(sig.entry - sig.stop)
        moved = (px - sig.entry) if sig.side is Side.LONG else (sig.entry - px)
        return -0.5 * dist < moved < 0.3 * dist  # no perseguir ni entrar cerca del stop

    # ------------------------------------------------------------ cierres

    async def _on_closed(self, trade: Trade, pos: Position) -> None:
        self.journal.record_trade(trade)
        self.state.record_close(CloseRecord(symbol=trade.symbol, side=trade.side, closed_at=trade.closed_at,
                                            pnl_net=float(trade.pnl_net)), update_equity=False)
        await self._refresh_equity()
        self.risk.update_guards(self.state, trade.closed_at)
        self.state.save(self.s.storage.state_path)
        self.alert(f"CIERRE {trade.symbol} {trade.side.value} {trade.exit_reason.value} "
                   f"R={float(trade.r_multiple):+.2f} neto={float(trade.pnl_net):+.2f}", level="info")
        if self.state.halted:
            self.alert(f"BOT DETENIDO: {self.state.halt_reason}. Reinicio manual requerido.")

    async def _refresh_equity(self) -> None:
        ex = self.c.exchange
        if ex is None:
            return
        try:
            eq = ex.equity() if isinstance(ex, PaperExchange) else await ex.balance()
            self.state.update_equity(float(eq))
        except Exception as exc:  # noqa: BLE001
            log.warning("equity no disponible", extra={"err": repr(exc)})

    # ------------------------------------------------------------ bucles

    def _atr_15m(self) -> dict[str, float | None]:
        out = {}
        for sym, st in self.c.states.items():
            a = st.candles["15m"].arrays()
            out[sym] = float(ta.atr(a["h"], a["l"], a["c"], 14)[-1]) if len(a["c"]) >= 15 else None
        return out

    def _macro_soon(self, now: datetime) -> bool:
        events, _, _ = self._macro()
        return any(timedelta(0) <= e.ts - now <= timedelta(minutes=self.s.filters.macro_block_before_min)
                   for e in events)

    async def tick(self, now: datetime) -> None:
        ex = self.c.exchange
        if isinstance(ex, PaperExchange):
            for sym in self.c.states:
                ex.on_market(sym)
        if self.manager is not None:
            closed, self._pending_closed_15m = self._pending_closed_15m, {}
            try:
                await self.manager.tick(now, self._atr_15m(), closed, self._macro_soon(now))
            except Exception as exc:  # noqa: BLE001
                log.error("gestor de posición falló", extra={"err": repr(exc)})
                self._safe_mode(f"gestor de posición: {exc!r}")

    def _safe_mode(self, reason: str) -> None:
        if not self.state.safe_mode:
            self.alert(f"MODO SEGURO: {reason}")
            self.journal.record_event(self.clock.now(), "modo_seguro", {"reason": reason})
        self.state.safe_mode, self.state.safe_mode_reason = True, reason
        self._clean_reconciles = 0
        self.state.save(self.s.storage.state_path)

    async def reconcile(self, now: datetime) -> None:
        ex = self.c.exchange
        if ex is None or self.manager is None or self.executor is None:
            return
        try:
            positions = await ex.positions()
            orders = await ex.open_orders()
        except Exception as exc:  # noqa: BLE001
            self._safe_mode(f"conciliación imposible: {exc!r}")
            return
        issues = reconcile_state(self.manager.positions, positions, orders, self._in_flight)
        critical = [i for i in issues if i.critical]
        for i in issues:
            if i.kind == "orden_huerfana":
                try:
                    await ex.cancel(i.symbol, i.ref)
                except Exception:  # noqa: BLE001
                    pass
            elif i.kind == "posicion_sin_stop":
                # Proteger lo abierto: reponer el stop; si no se puede, cerrar a mercado
                pos = self.manager.positions.get(i.symbol)
                ep = next((p for p in positions if p.symbol == i.symbol), None)
                ok = False
                if pos is not None:
                    ok = await self.executor.ensure_stop(pos, await ex.symbol_rules(i.symbol),
                                                         tag=f"repair-{int(now.timestamp())}")
                if not ok and ep is not None:
                    await self.executor.flatten(i.symbol, ep.side, ep.qty, f"nostop-{int(now.timestamp())}")
        if critical:
            self._safe_mode("; ".join(f"{i.kind} {i.symbol}: {i.detail}" for i in critical))
        elif self.state.safe_mode and not self.state.halted:
            self._clean_reconciles += 1
            if self._clean_reconciles >= 3 and self.state.safe_mode_reason.startswith(("conciliación", "posicion",
                                                                                       "cantidad", "lado", "gestor")):
                self.state.safe_mode, self.state.safe_mode_reason = False, ""
                self.alert("modo seguro levantado tras 3 conciliaciones limpias", level="warning")

    async def adopt_existing(self, now: datetime) -> None:
        """Al arrancar: posiciones abiertas en el exchange se adoptan (con su stop) en vez de ignorarse."""
        ex = self.c.exchange
        if ex is None or self.manager is None:
            return
        orders = await ex.open_orders()
        for ep in await ex.positions():
            stop = next((o for o in orders if o.symbol == ep.symbol and o.type.value == "STOP_MARKET"), None)
            pos = adopt_position(ep, stop, now)
            self.manager.add(pos, await ex.symbol_rules(ep.symbol))
            self.alert(f"posición adoptada al arrancar: {ep.symbol} {ep.side.value} {ep.qty} stop="
                       f"{'sí' if stop else 'NO'}", level="warning")

    async def housekeeping(self, now: datetime) -> None:
        self.state.roll_day(now.date())
        await self._refresh_equity()
        self.risk.update_guards(self.state, now)
        self.state.save(self.s.storage.state_path)
        self._resolve_counterfactuals(now)
        down, up = self.health.check(now)
        for src in down:
            self.alert(f"fuente caída: {src}", level="warning")
        for src in up:
            self.alert(f"fuente recuperada: {src}", level="info")
        self.health.write(now, {"mode": self.s.mode.value, "equity": self.state.equity, "halted": self.state.halted,
                                "safe_mode": self.state.safe_mode,
                                "positions": list(self.manager.positions) if self.manager else []})
        hour = int(now.timestamp()) // 3600
        if self._last_hourly != hour:
            self._last_hourly = hour
            self.write_report(now)
        day = now.date().isoformat()
        if self._last_daily != day and now.hour == 0 and now.minute >= 5:
            self._last_daily = day
            await self.daily_tasks(now)
        week = f"{now.isocalendar().year}-{now.isocalendar().week}"
        if self._last_weekly != week and now.weekday() == 0 and now.hour == 0 and now.minute >= 30:
            self._last_weekly = week
            await self.weekly_tasks(now)

    def _resolve_counterfactuals(self, now: datetime) -> None:
        for c in self.journal.pending_counterfactuals(now - timedelta(minutes=15)):
            if not counterfactual.due(datetime.fromisoformat(c["ts"]), c["max_hold_hours"] or 6, now):
                continue
            st = self.c.states.get(c["symbol"])
            if st is None:
                continue
            # costo total estimado = movimiento esperado / ventaja registrada
            move = abs((c["take_profit"] or c["entry"]) - c["entry"]) / c["entry"]
            cost = move / c["edge_ratio"] if c["edge_ratio"] else 0.001
            r = counterfactual.simulate(c["side"], c["entry"], c["stop"], c["take_profit"],
                                        datetime.fromisoformat(c["ts"]), c["max_hold_hours"] or 6,
                                        st.candles["15m"].closed(), cost_pct=cost)
            if r is not None:
                self.journal.set_counterfactual(c["id"], r)
        for case in self.cc.pending_shadow():
            st = self.c.states.get(case["symbol"])
            ts = datetime.fromisoformat(case["ts"])
            if st is None or not counterfactual.due(ts, case["max_hold_hours"], now):
                continue
            r = counterfactual.simulate(case["side"], case["entry"], case["stop"], case["take_profit"], ts,
                                        case["max_hold_hours"], st.candles["15m"].closed(), cost_pct=case["cost_pct"])
            if r is not None:
                self.cc.resolve_shadow(case["hyp_id"], case["signal_id"], r["r_net"])

    def write_report(self, now: datetime) -> None:
        since = datetime(2000, 1, 1, tzinfo=UTC)
        try:
            write_report(self.s.storage.reports_dir / "reporte.html", self.journal.trades(since),
                         self.journal.candidates(now - timedelta(days=30)), self.s.paper.initial_capital, now,
                         self.s.mode.value)
        except Exception as exc:  # noqa: BLE001
            log.error("reporte falló", extra={"err": repr(exc)})

    async def daily_tasks(self, now: datetime) -> None:
        yesterday = (now - timedelta(days=1)).date()
        write_zone(self.journal, yesterday, self.state.equity, self.s.storage.reports_dir)
        for h, ev in self.cc.review(now):
            self.alert(f"retador {h.id} supera al campeón ({ev.challenger_exp:+.2f}R vs {ev.champion_exp:+.2f}R, "
                       f"n={ev.n_cases}). " + ("Requiere aprobación." if self.s.learning.require_human_approval
                                               else "Promovido."), level="warning")
        self.lessons.reconfirm_all(self.journal, now)
        self.lessons.expire_stale(now)
        if self.c.exchange is not None and not isinstance(self.c.exchange, PaperExchange):
            start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=UTC)
            try:
                income = [i for i in await self.c.exchange.income(start) if i.ts < start + timedelta(days=1)]
                jpnl = sum(float(t["pnl_net"]) for t in self.journal.trades(start, start + timedelta(days=1)))
                chk = reconcile_income(income, jpnl, self.state.equity)
                self.journal.record_event(now, "conciliacion_ingresos", chk.__dict__)
                if not chk.ok:
                    self.alert(f"conciliación de ingresos: exchange {chk.exchange_total} vs diario "
                               f"{chk.journal_total} ({chk.diff_pct:.2%})")
            except Exception as exc:  # noqa: BLE001
                self.alert(f"conciliación de ingresos falló: {exc!r}", level="warning")

    async def weekly_tasks(self, now: datetime) -> None:
        if self.c.analyst is None:
            return
        start, end = last_week(now)
        try:
            res = await run_reflection(self.journal, self.s.ai, start, end, self.s.paper.initial_capital,
                                       client=self.c.analyst.client,
                                       max_hypotheses=self.s.learning.max_hypotheses_per_week)
        except Exception as exc:  # noqa: BLE001
            self.alert(f"reflexión semanal falló: {exc!r}", level="warning")
            return
        for h in res["hypotheses"]:
            self.cc.add(h, now)
        write_reflection_md(res, self.s.storage.reports_dir, end)

    async def _loop(self, every_s: float, fn) -> None:
        while True:
            try:
                await fn(self.clock.now())
            except Exception as exc:  # noqa: BLE001
                log.error("bucle falló", extra={"fn": fn.__name__, "err": repr(exc)})
            await asyncio.sleep(every_s)

    async def run(self) -> None:
        now = self.clock.now()
        if self.state.halted:
            self.alert(f"arranque con bot DETENIDO ({self.state.halt_reason}): sin entradas hasta reinicio manual")
        await self.adopt_existing(now)
        await self.reconcile(now)
        await asyncio.gather(
            self._loop(1.0, self.tick),
            self._loop(self.s.execution.reconcile_interval_s, self.reconcile),
            self._loop(60.0, self.housekeeping),
            *self.c.extra_tasks,
        )


def data_quality_from(fields: dict[str, DataPoint]) -> float:
    snap = Snapshot(symbol="x", ts=datetime.now(UTC), fields={k: v for k, v in fields.items()
                                                              if not k.startswith("ind.")})
    return data_quality(snap)


# ------------------------------------------------------------ construcción por modo


def build_components(settings: Settings, secrets: Secrets) -> tuple[Components, BinanceMarketData, Recorder]:
    from bot.execution.binance import BinanceFutures

    symbols = settings.exchange.symbols
    states = {s: MarketState(s) for s in symbols}
    store = FeedStore()
    http = HttpClient(RateLimiter({"binance": (1500, 60.0), "binance_data": (800, 300.0), "deribit": (20, 1.0),
                                   "hyperliquid": (100, 60.0), "alternative_me": (30, 60.0), "defillama": (30, 60.0),
                                   "horizon": (60, 60.0), "forexfactory": (5, 60.0), "rss": (60, 60.0),
                                   "cryptopanic": (5, 60.0)}))
    recorder = Recorder(settings.data.dir)
    ep = settings.endpoints.binance  # datos de mercado siempre de producción (públicos)
    journal = Journal(settings.storage.db_path)

    exchange: Exchange | None = None
    if settings.mode is Mode.PAPER:
        exchange = PaperExchange(states, settings.exchange.fees, settings.paper.initial_capital,
                                 latency_ms=settings.paper.latency_ms)
    elif settings.mode in (Mode.TESTNET, Mode.LIVE):
        if settings.mode is Mode.TESTNET:
            exchange = BinanceFutures(settings.endpoints.binance_testnet.rest, secrets.binance_testnet_api_key or "",
                                      secrets.binance_testnet_api_secret or "", settings.exchange.recv_window_ms,
                                      name="binance_testnet")
        else:
            exchange = BinanceFutures(ep.rest, secrets.binance_api_key or "", secrets.binance_api_secret or "",
                                      settings.exchange.recv_window_ms, name="binance")

    analyst = Analyst(settings.ai) if settings.mode in (Mode.PAPER, Mode.TESTNET, Mode.LIVE) else None
    macro = MacroCalendar(settings.endpoints.forexfactory_week, http)
    news = NewsFeed(settings.news.rss_feeds, http, settings.endpoints.cryptopanic, secrets.cryptopanic_api_key)
    comps = Components(states, store, exchange, analyst, journal, macro, news)
    dispatcher = MarketDispatcher(states, on_raw=lambda stream, d: (
        recorder.record(stream, d) if d.get("e") in RECORD_RAW_EVENTS and (d.get("e") != "kline" or d["k"]["x"])
        else None))
    market = BinanceMarketData(ep.rest, ep.ws_public, ep.ws_market, states, http, dispatcher)
    pollers = [
        DerivativesPoller(ep.rest, symbols, store, http).run(settings.data.poll_s["open_interest"]),
        DvolPoller(settings.endpoints.deribit, store, http).run(settings.data.poll_s["dvol"]),
        HyperliquidPoller(settings.endpoints.hyperliquid, symbols, store, http).run(settings.data.poll_s["dex"]),
        FearGreedPoller(settings.endpoints.alternative_me, store, http).run(settings.data.poll_s["sentiment"]),
        OnchainPoller(settings.endpoints.defillama_stablecoins, settings.endpoints.stellar_horizon, store,
                      http).run(settings.data.poll_s["onchain"]),
        macro.run(settings.data.poll_s["macro"]),
        news.run(settings.data.poll_s["news"]),
    ]
    comps.extra_tasks = pollers
    return comps, market, recorder


async def run_live_like(settings: Settings, secrets: Secrets, alert: Alerter) -> None:
    comps, market, recorder = build_components(settings, secrets)
    info = await market.verify_symbols()  # siempre: no se opera un símbolo que no exista
    await market.load_funding_intervals()
    await market.bootstrap_klines()
    engine = Engine(settings, secrets, comps, alert=alert)
    market.dispatcher.on_kline_closed = engine.on_kline_closed

    ex = comps.exchange
    if ex is not None and not isinstance(ex, PaperExchange):
        from bot.execution.binance import BinanceFutures
        assert isinstance(ex, BinanceFutures)
        offset = await ex.sync_time()
        log.info("desfase con el exchange", extra={"offset_ms": offset})
        await ex.load_rules()
        for s in settings.exchange.symbols:
            await ex.set_leverage(s, int(settings.risk.max_leverage))
    elif isinstance(ex, PaperExchange):
        for sym, meta in info.items():
            f = {x["filterType"]: x for x in meta.get("filters", [])}
            ex.rules[sym] = SymbolRules(float(f["PRICE_FILTER"]["tickSize"]), float(f["LOT_SIZE"]["stepSize"]),
                                        float(f["LOT_SIZE"]["minQty"]),
                                        float(f.get("MIN_NOTIONAL", {}).get("notional", 5)))

    async def heartbeat(now: datetime) -> None:
        if market.connected["market"]:
            engine.health.beat("ws_market", now)
        if market.connected["public"]:
            engine.health.beat("ws_public", now)
        if comps.macro and comps.macro.ok and comps.macro.updated_at:
            engine.health.beat("macro", comps.macro.updated_at)
        recorder.flush()

    async def book_sampler(now: datetime) -> None:
        for sym, st in comps.states.items():
            if st.book.synced:
                bids, asks = st.book.top(20)
                recorder.record(f"{sym.lower()}@book20", {"bids": bids, "asks": asks, "E": st.book.event_ts_ms}, now)

    comps.extra_tasks += [market.run(), engine._loop(5.0, heartbeat), engine._loop(1.0, book_sampler)]
    await engine.run()


async def run_record(settings: Settings, secrets: Secrets, alert: Alerter) -> None:
    """Modo `record`: solo graba datos y mide completitud para la puerta de fase."""
    comps, market, recorder = build_components(settings, secrets)
    await market.verify_symbols()
    await market.load_funding_intervals()
    engine = Engine(settings, secrets, comps, alert=alert)

    async def stats(now: datetime) -> None:
        recorder.flush()
        gaps = {s: st.candles["1m"].gaps(60_000) for s, st in comps.states.items()}
        engine.health.write(now, {"mode": "record", "counts": dict(recorder.counts), "kline_1m_gaps": gaps,
                                  "reconnects": market.reconnects, "connected": market.connected})

    async def book_sampler(now: datetime) -> None:
        for sym, st in comps.states.items():
            if st.book.synced:
                bids, asks = st.book.top(20)
                recorder.record(f"{sym.lower()}@book20", {"bids": bids, "asks": asks, "E": st.book.event_ts_ms}, now)

    await asyncio.gather(market.run(), engine._loop(30.0, stats), engine._loop(1.0, book_sampler), *comps.extra_tasks)
