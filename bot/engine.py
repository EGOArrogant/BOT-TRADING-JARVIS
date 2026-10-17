"""Orquestador del bot (modos record | paper | testnet | live).

Flujo por ventana de 15 min (al cerrar la vela de 15m de todos los símbolos):
datos -> indicadores -> estrategias -> filtros (+ cambios aprendidos) ->
mejor candidato -> IA -> techo diario con confianza -> tamaño -> ejecución.
En paralelo: gestión de posiciones (1 s), conciliación (30 s), tareas
periódicas (1 min): equity, guardias, contrafactuales, reportes, aprendizaje.
"""

from __future__ import annotations

import asyncio
import logging
import os
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.ai_analyst.client import Analyst
from bot.ai_analyst.prompt_builder import build_exit_prompt, build_user_prompt
from bot.core.alerts import Alerter
from bot.core.clock import Clock, ms_to_dt
from bot.core import control
from bot.core.config import Mode, Secrets, Settings
from bot.core.fsutil import atomic_write_text, read_with_backup
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
from bot.data_engine import retention
from bot.data_engine.top_traders import TopTradersPoller
from bot.data_engine.volatility import DvolPoller
from bot.execution.base import Exchange
from bot.execution.executor import Executor
from bot.execution.paper import PaperExchange
from bot.filters.checks import check_data, check_macro, run_filters, select_best
from bot.filters.strategies import generate
from bot.indicators import ta
from bot.indicators.features import compute_features
from bot.learning import counterfactual
from bot.learning.champion_challenger import ChampionChallenger
from bot.dashboard import snapshot as panel_snapshot
from bot import updater
from bot.core import notify
from bot.dashboard.reports import day_report
from bot.learning.daily_report import write_zone
from bot.learning.hypotheses import effective_strategies, load_overrides, overrides_allow
from bot.learning.journal import AI_REJECTED, NO_FILL, NOT_SELECTED, REJECTED, RISK_REJECTED, TRADED, Journal
from bot.learning.lessons import LessonBook
from bot.learning.weekly_reflection import last_week, run_reflection, write_reflection_md
from bot.metrics.exchange_reconcile import adopt_position, reconcile_income, reconcile_state
from bot.metrics.performance import summarize
from bot.metrics.report_html import write_report
from bot.position.manager import PositionManager
from bot.risk.engine import Check, OpenExposure, RiskEngine, SymbolRules

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
    extra_tasks: list[Any] = field(default_factory=list)  # fábricas sin argumentos que devuelven corrutinas
    top_traders: Any = None                                 # TopTradersPoller (para el panel)


class Engine:
    def __init__(self, settings: Settings, secrets: Secrets, comps: Components, alert: Alerter | None = None,
                 clock: Clock | None = None):
        self.s = settings
        self.secrets = secrets
        self.c = comps
        self.clock = clock or Clock()
        self._alerter = alert or Alerter(False, None, None)
        self.unavailable: set[str] = set()   # símbolos en mantenimiento o retirados: sin entradas nuevas
        self.system: dict[str, Any] = {"clock_offset_s": None, "free_gb": None, "low_disk": False,
                                       "unavailable": [], "retention": None}
        self.risk = RiskEngine(settings.risk, settings.limits, settings.exchange)
        # Controles del panel (Operar/Solo cierre, agresividad): `eff` es la config efectiva, nunca más laxa que `s`
        self.eff = settings
        self.control = control.Control()
        self.profile = control.profile(control.MAX_LEVEL, settings)
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
        self._exit_reviews: dict[str, tuple[datetime, set[str]]] = {}
        self.user_stream: Any = None          # UserStream si el exchange es real
        self._wake = asyncio.Event()          # lo dispara un evento de cuenta  # símbolo -> (última, titulares vistos)

    def notify(self, notice: notify.Notice) -> None:
        """Notificación para personas (campana del panel + Telegram): entradas, salidas y reportes."""
        try:
            self.journal.record_event(self.clock.now(), "notif", notice.payload())
        except Exception:  # noqa: BLE001
            pass
        self._alerter(notice.as_text(), "info")

    def alert(self, msg: str, level: str = "error") -> None:
        """Alerta (log/Telegram) que además queda en el diario para la bitácora del panel."""
        before = len(self._alerter.sent)
        self._alerter(msg, level)
        if len(self._alerter.sent) > before:  # no repetida (el Alerter deduplica)
            try:
                self.journal.record_event(self.clock.now(), "alerta", {"level": level, "msg": msg})
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ eventos de mercado

    async def on_kline_closed(self, symbol: str, interval: str, candle: Candle) -> None:
        if interval != "15m":
            return
        close_ts = candle.open_time + 900_000
        self._closed_15m[symbol][close_ts] = candle.close
        self._pending_closed_15m[symbol] = candle.close
        if all(close_ts in v for s, v in self._closed_15m.items() if s not in self.unavailable):
            # En segundo plano: la IA y la ejecución no deben bloquear el flujo del WebSocket
            self._spawn(self.evaluate_window(ms_to_dt(close_ts)))
        for v in self._closed_15m.values():
            for old in [k for k in v if k < close_ts - 4 * 900_000]:
                v.pop(old, None)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def evaluate_late_window(self, now: datetime) -> None:
        """Si una moneda dejó de mandar velas, la ventana no espera para siempre: a los 2 min se evalúa
        igual (esa moneda queda bloqueada por el filtro de datos vencidos)."""
        minutes = self.s.limits.candidate_window_minutes
        close = datetime.fromtimestamp(int(now.timestamp()) // (minutes * 60) * minutes * 60, tz=UTC)
        if (now - close).total_seconds() >= 120 and self._last_window != int(close.timestamp()) // (minutes * 60):
            self._spawn(self.evaluate_window(close))

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
        ctl = self.refresh_control()
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
                out = run_filters(sig, fields, now, self.state, self.risk, self.eff.risk, self.eff.filters, events,
                                  macro_ok, macro_upd, exposures)
                ok_learned, why = overrides_allow(self.overrides, sig, fields)
                reasons = out.reasons + ([why] if not ok_learned else [])
                if ctl.close_only:
                    reasons.append("solo cierre: activado desde el panel")
                if sym in self.unavailable:
                    reasons.append("símbolo no disponible en Binance (mantenimiento o retiro)")
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

    def refresh_control(self) -> control.Control:
        """Relee control.json; si cambió, recalcula la config efectiva y el motor de riesgo."""
        ctl = control.load(control.path_for(self.s))
        if (ctl.mode, ctl.aggressiveness) != (self.control.mode, self.control.aggressiveness) or self.eff is self.s:
            self.eff, self.profile = control.apply(self.s, ctl)
            self.risk = RiskEngine(self.eff.risk, self.eff.limits, self.s.exchange)
            if (ctl.mode, ctl.aggressiveness) != (self.control.mode, self.control.aggressiveness):
                log.info("controles del panel", extra={"mode": ctl.mode, "level": ctl.aggressiveness,
                                                        "risk_per_trade": self.eff.risk.risk_per_trade,
                                                        "min_edge": self.profile.min_edge,
                                                        "max_trades": self.profile.max_trades})
        self.control = ctl
        return ctl

    def _shadow(self, sym, fields, strategies, now, events, macro_ok, macro_upd, exposures) -> None:
        for h in self.cc.active():
            for sig in generate(sym, fields, h.apply_params(strategies), self.s.exchange.fees, now,
                                data_quality_from(fields)):
                out = run_filters(sig, fields, now, self.state, self.risk, self.eff.risk, self.eff.filters, events,
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
            # Re-chequeos tras la IA (puede tardar hasta 30 s): confianza, guardias y datos aún frescos
            exec_now = self.clock.now()
            fresh, _ = self._fields(sig.symbol, exec_now)
            m_events, m_ok, m_upd = self._macro()
            ctl, p = self.refresh_control(), self.profile
            for name, chk in (("solo_cierre", Check(not ctl.close_only, "activado desde el panel")),
                              ("agresividad", Check(d.confidence >= p.min_ai_confidence,
                                                    f"nivel {p.level} exige confianza IA ≥ {p.min_ai_confidence:.2f}")),
                              ("techo_diario", self.risk.slot_check(self.state, exec_now, sig.edge_ratio, d.confidence)),
                              ("guardias", self.risk.global_guard(self.state, exec_now)),
                              ("datos_al_ejecutar", check_data(fresh)),
                              ("macro_al_ejecutar", check_macro(exec_now, m_events, self.s.filters, m_ok, m_upd))):
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
                eo = result.entry_order
                if eo is not None and eo.filled_qty > 0:
                    # Se llenó pero se cerró por falta de stop: consumió riesgo y cupo -> cuenta como entrada
                    self.state.record_entry(EntryRecord(symbol=sig.symbol, side=sig.side, ts=now,
                                                        edge_ratio=sig.edge_ratio, signal_id=sig.id))
                    self.state.save(self.s.storage.state_path)
                    self.journal.record_event(now, "entrada_sin_stop", {"symbol": sig.symbol, "signal": sig.id,
                                                                         "qty": str(eo.filled_qty)})
                return
            self.manager.add(result.position, rules)
            self.save_paper()
            self.state.record_entry(EntryRecord(symbol=sig.symbol, side=sig.side, ts=now, edge_ratio=sig.edge_ratio,
                                                signal_id=sig.id))
            self.state.save(self.s.storage.state_path)
            self.journal.update_candidate(sig.id, status=TRADED)
            p = result.position
            log.info("entrada", extra={"symbol": sig.symbol, "side": sig.side.value, "strategy": sig.strategy,
                                       "qty": str(p.qty), "entry": str(p.entry_price), "stop": str(p.stop_price),
                                       "conf": d.confidence})
            self.notify(notify.entry(sig.symbol, sig.side.value, sig.strategy, float(p.entry_price), float(p.stop_price),
                                     float(p.take_profit_price) if p.take_profit_price else None, float(p.qty),
                                     d.confidence))

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
        self.save_paper()
        log.info("cierre", extra={"symbol": trade.symbol, "side": trade.side.value, "reason": trade.exit_reason.value,
                                  "r": float(trade.r_multiple), "pnl_net": float(trade.pnl_net)})
        self.notify(notify.exit_(trade.symbol, trade.side.value, trade.exit_reason.value, float(trade.pnl_net),
                                 float(trade.r_multiple), self.state.equity))
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
                self.state.save(self.s.storage.state_path)  # que el panel y un reinicio lo vean de inmediato
                self.alert("modo seguro levantado tras 3 conciliaciones limpias", level="warning")

    # ------------------------------------------------------------ papel: sobrevivir apagones

    @property
    def paper_path(self) -> Path:
        st = self.s.storage.state_path
        return st.with_name(f"{st.stem}_exchange.json")

    def save_paper(self) -> None:
        """En papel el "exchange" vive en memoria: se guarda saldo, posiciones, órdenes y su gestión."""
        ex = self.c.exchange
        if not isinstance(ex, PaperExchange):
            return
        try:
            positions = self.manager.positions if self.manager else {}
            atomic_write_text(self.paper_path, json.dumps({
                "saved_at": self.clock.now().isoformat(), "exchange": ex.to_dict(),
                "positions": [p.model_dump(mode="json") for p in positions.values()]}, default=str), backup=True)
        except Exception as exc:  # noqa: BLE001
            log.error("no se pudo guardar el estado de papel", extra={"err": repr(exc)})

    async def restore_paper(self) -> None:
        """Al arrancar en papel: continúa con el saldo y las posiciones (con sus stops) de antes del apagón.
        Si cayó el precio mientras estaba apagado, el stop se ejecuta al precio actual (como un hueco real)."""
        ex = self.c.exchange
        if not isinstance(ex, PaperExchange):
            return
        if not self.paper_path.exists():
            ex.wallet = Decimal(str(self.state.equity))  # versiones anteriores solo guardaban el estado
            return
        try:
            data = read_with_backup(self.paper_path, json.loads, lambda exc: log.error(
                "estado de papel dañado: se recuperó del respaldo", extra={"err": repr(exc)[:200]}))
            ex.load_dict(data["exchange"])
        except Exception as exc:  # noqa: BLE001
            log.error("estado de papel ilegible: se sigue con el saldo del estado", extra={"err": repr(exc)})
            ex.wallet = Decimal(str(self.state.equity))
            return
        if self.manager is None:
            return
        open_syms = {p.symbol for p in await ex.positions()}
        for raw in data.get("positions", []):
            pos = Position.model_validate(raw)
            if pos.symbol in open_syms:
                self.manager.add(pos, await ex.symbol_rules(pos.symbol))
        log.info("estado de papel recuperado", extra={"saved_at": data.get("saved_at"), "wallet": str(ex.wallet),
                                                      "positions": sorted(self.manager.positions)})

    async def adopt_existing(self, now: datetime) -> None:
        """Al arrancar: posiciones abiertas en el exchange se adoptan (con su stop) en vez de ignorarse."""
        ex = self.c.exchange
        if ex is None or self.manager is None:
            return
        orders = await ex.open_orders()
        for ep in await ex.positions():
            if ep.symbol in self.manager.positions:  # ya recuperada con toda su información
                continue
            stop = next((o for o in orders if o.symbol == ep.symbol and o.type.value == "STOP_MARKET"), None)
            pos = adopt_position(ep, stop, now)
            self.manager.add(pos, await ex.symbol_rules(ep.symbol))
            self.alert(f"posición adoptada al arrancar: {ep.symbol} {ep.side.value} {ep.qty} stop="
                       f"{'sí' if stop else 'NO'}", level="warning")

    # ------------------------------------------------------------ salida anticipada por IA

    EXIT_REVIEW_MIN_INTERVAL = timedelta(minutes=30)
    EXIT_NEWS_MIN_SCORE = 2.0

    def _exit_trigger(self, pos: Position, headlines: list, now: datetime) -> tuple[str | None, set[str]]:
        """Motivo para consultar a la IA: titular de alto impacto nuevo sobre la moneda, o
        la operación lleva la mitad de su tiempo máximo en pérdida (una sola vez)."""
        last, seen = self._exit_reviews.get(pos.signal_id, (None, set()))
        if last is not None and now - last < self.EXIT_REVIEW_MIN_INTERVAL:
            return None, seen
        fresh = [h for h in headlines if pos.symbol in h.symbols and h.high_impact and h.score >= self.EXIT_NEWS_MIN_SCORE
                 and h.title not in seen]
        if fresh:
            return f"noticia: {fresh[0].title}", seen | {h.title for h in fresh}
        st = self.c.states[pos.symbol]
        px = st.mark_price or st.last_price
        half = now - pos.opened_at >= timedelta(hours=pos.max_hold_hours / 2)
        if half and last is None and px is not None and float(pos.r_multiple(Decimal(str(px)))) < 0:
            return "mitad del tiempo máximo en pérdida", seen
        return None, seen

    async def review_exits(self, now: datetime) -> None:
        if self.manager is None or self.c.analyst is None or not self.manager.positions:
            return
        headlines = []
        if self.c.news is not None:
            headlines = score_headlines(self.c.news.items, now, self.s.news.symbol_keywords,
                                        self.s.news.trusted_sources, self.s.news.high_impact_keywords,
                                        self.s.data.news_max_age_min, self.s.news.dedupe_similarity, top_n=20)
        events, _, _ = self._macro()
        for sym, pos in list(self.manager.positions.items()):
            trigger, seen = self._exit_trigger(pos, headlines, now)
            if trigger is None:
                continue
            self._exit_reviews[pos.signal_id] = (now, seen)
            st = self.c.states[sym]
            px = st.mark_price or st.last_price
            summary = {"simbolo": sym, "lado": pos.side.value, "estrategia": pos.strategy,
                       "entrada": str(pos.entry_price), "stop": str(pos.stop_price),
                       "r_actual": round(float(pos.r_multiple(Decimal(str(px)))), 3) if px else None,
                       "horas_abierta": round((now - pos.opened_at).total_seconds() / 3600, 2),
                       "tesis_invalidacion": pos.invalidation}
            fields, _ = self._fields(sym, now)
            prompt = build_exit_prompt(summary, fields, [h for h in headlines if sym in h.symbols],
                                       upcoming(now, events, 12), trigger, now)
            close, reason = await self.c.analyst.review_exit(prompt)
            self.journal.record_event(now, "revision_salida_ia", {"symbol": sym, "trigger": trigger,
                                                                   "close": close, "reason": reason})
            if close and sym in self.manager.positions:
                self.alert(f"IA recomienda cerrar {sym} antes: {reason}", level="warning")
                await self.manager.request_close(sym, "ia")

    async def housekeeping(self, now: datetime) -> None:
        self.state.roll_day(now.date())
        self.save_paper()
        if self.c.analyst is not None:
            self.evaluate_late_window(now)
        await self._refresh_equity()
        self.risk.update_guards(self.state, now)
        self.state.save(self.s.storage.state_path)
        self._resolve_counterfactuals(now)
        try:
            await self.review_exits(now)
        except Exception as exc:  # noqa: BLE001
            log.warning("revisión de salidas falló", extra={"err": repr(exc)})
        down, up = self.health.check(now)
        for src in down:
            self.alert(f"fuente caída: {src}", level="warning")
        for src in up:
            self.alert(f"fuente recuperada: {src}", level="info")
        self.health.write(now, {"mode": self.s.mode.value, "equity": self.state.equity, "halted": self.state.halted,
                                "safe_mode": self.state.safe_mode,
                                "positions": list(self.manager.positions) if self.manager else []})
        self.write_panel(now)
        hour = int(now.timestamp()) // 3600
        if self._last_hourly != hour:
            self._last_hourly = hour
            self.write_report(now)
            await self.check_disk(now)
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

    SOURCES = (  # (nombre para el panel, clave en el almacén, clave de TTL en config.data.ttl_s)
        ("Open interest y ratios (Binance)", "deriv.oi.BTCUSDT", "open_interest"),
        ("Volatilidad implícita (Deribit)", "vol.dvol.BTC", "dvol"),
        ("Funding en Hyperliquid", "dex.funding_1h.BTCUSDT", "dex"),
        ("Traders destacados (Hyperliquid)", "dex.smart_ratio.BTCUSDT", "dex"),
        ("Sentimiento (Fear & Greed)", "sent.fng", "sentiment"),
        ("On-chain (stablecoins)", "onchain.stablecoin_supply", "onchain"),
    )

    def system_status(self, now: datetime) -> dict[str, Any]:
        def entry(name: str, ts: datetime | None, ttl: float | None) -> dict:
            age = (now - ts).total_seconds() if ts else None
            return {"name": name, "age_s": round(age) if age is not None else None,
                    "ok": age is not None and (ttl is None or age <= ttl)}
        ttl = self.s.data.ttl_s
        src = [entry("Precios en vivo (Binance)", self.health.last_seen.get("ws_market"), 30),
               entry("Libro de órdenes (Binance)", self.health.last_seen.get("ws_public"), 30)]
        for name, key, tkey in self.SOURCES:
            dp = self.c.store.get(key)
            src.append(entry(name, dp.ts if dp.is_ok else None, ttl.get(tkey)))
        if self.c.news is not None:
            src.append(entry("Noticias (RSS)", self.c.news.updated_at, ttl.get("news")))
        if self.c.macro is not None:
            src.append(entry("Calendario macro", self.c.macro.updated_at if self.c.macro.ok else None,
                             ttl.get("macro", 86400) * 1.5))
        return {**self.system, "sources": src, "safe_mode": self.state.safe_mode,
                "safe_mode_reason": self.state.safe_mode_reason}

    def write_panel(self, now: datetime) -> None:
        try:
            positions = self.manager.positions if self.manager else {}
            snap = panel_snapshot.build(now, self.s.mode.value, self.c.states, positions,
                                        self.c.top_traders.summary() if self.c.top_traders else None)
            snap["system"] = self.system_status(now)
            panel_snapshot.write(self.s.storage.reports_dir / panel_snapshot.FILENAME, snap)
        except Exception as exc:  # noqa: BLE001
            log.error("foto del panel falló", extra={"err": repr(exc)})

    async def check_disk(self, now: datetime, force: bool = False) -> None:
        """Cada hora mira el espacio libre; a diario (o si falta espacio) comprime y depura los crudos."""
        raw = self.s.data.dir / "raw"
        try:
            free = retention.free_gb(self.s.data.dir) if self.s.data.dir.exists() else None
            self.system["free_gb"] = round(free, 1) if free is not None else None
            if force or (free is not None and free < self.s.data.min_free_gb):
                rep = await asyncio.to_thread(retention.run, raw, now.date(), self.s.data.raw_retention_days,
                                              self.s.data.min_free_gb)
                self.system.update(free_gb=round(rep.free_gb, 1) if rep.free_gb is not None else None,
                                   low_disk=rep.low_disk,
                                   retention={"ts": now.isoformat(), "compressed": rep.compressed,
                                              "deleted": len(rep.deleted), "freed_mb": round(rep.freed_mb)})
                if rep.low_disk:
                    self.alert(f"poco espacio en disco: {rep.free_gb:.1f} GB libres incluso tras depurar", "error")
        except Exception as exc:  # noqa: BLE001
            log.warning("revisión de disco falló", extra={"err": repr(exc)})

    async def daily_tasks(self, now: datetime) -> None:
        await self.check_disk(now, force=True)
        yesterday = (now - timedelta(days=1)).date()
        write_zone(self.journal, yesterday, self.state.equity, self.s.storage.reports_dir)
        try:
            start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=UTC)
            rep = day_report(yesterday.isoformat(), self.journal.trades(start, start + timedelta(days=1)),
                             self.journal.candidates(start, start + timedelta(days=1)), self.s.risk.risk_per_trade,
                             self.s.paper.initial_capital)
            self.notify(notify.day(rep))
        except Exception as exc:  # noqa: BLE001
            log.warning("resumen del día para notificación falló", extra={"err": repr(exc)})
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

    # ------------------------------------------------------------ stream de usuario

    def on_account_event(self, event: str, data: dict) -> None:
        """Evento del stream privado (orden, algo orden, posición): revisar posiciones ya."""
        self._wake.set()

    def tick_interval(self) -> float:
        """Papel: 1 s (simula llenados). Real con stream sano: 5 s + despertar por eventos. Sin stream: 1 s."""
        if isinstance(self.c.exchange, PaperExchange):
            return 1.0
        if self.user_stream is not None and self.user_stream.connected:
            return 5.0
        return 1.0

    async def _tick_loop(self) -> None:
        while True:
            try:
                await self.tick(self.clock.now())
            except Exception as exc:  # noqa: BLE001
                log.error("bucle falló", extra={"fn": "tick", "err": repr(exc)})
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_interval())
            except TimeoutError:
                pass

    async def run(self) -> None:
        now = self.clock.now()
        if self.state.halted:
            self.alert(f"arranque con bot DETENIDO ({self.state.halt_reason}): sin entradas hasta reinicio manual")
        await self.restore_paper()
        await self.adopt_existing(now)
        await self.reconcile(now)
        self._spawn(self.check_disk(now, force=True))
        await asyncio.gather(
            self._tick_loop(),
            self._loop(self.s.execution.reconcile_interval_s, self.reconcile),
            self._loop(60.0, self.housekeeping),
            *(make() for make in self.c.extra_tasks),
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
    recorder = Recorder(settings.data.dir)
    # Todo valor de las fuentes REST se graba: así el backtest puede reproducir OI/funding/DEX/sentimiento
    store = FeedStore(on_set=lambda k, v, src, ts: recorder.record(
        "store", {"k": k, "v": v, "src": src, "ts": ts.isoformat()}, ts))
    http = HttpClient(RateLimiter({"binance": (1500, 60.0), "binance_data": (800, 300.0), "deribit": (20, 1.0),
                                   "hyperliquid": (600, 60.0), "alternative_me": (30, 60.0), "defillama": (30, 60.0),
                                   "horizon": (60, 60.0), "forexfactory": (5, 60.0), "rss": (60, 60.0),
                                   "cryptopanic": (5, 60.0)}))
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
    poll = settings.data.poll_s
    pollers = [
        lambda: DerivativesPoller(ep.rest, symbols, store, http).run(poll["open_interest"]),
        lambda: DvolPoller(settings.endpoints.deribit, store, http).run(poll["dvol"]),
        lambda: HyperliquidPoller(settings.endpoints.hyperliquid, symbols, store, http).run(poll["dex"]),
        lambda: FearGreedPoller(settings.endpoints.alternative_me, store, http).run(poll["sentiment"]),
        lambda: OnchainPoller(settings.endpoints.defillama_stablecoins, settings.endpoints.stellar_horizon, store,
                              http).run(poll["onchain"]),
        lambda: macro.run(poll["macro"]),
        lambda: news.run(poll["news"]),
    ]
    if settings.top_traders.enabled:
        top = TopTradersPoller(settings.top_traders, settings.endpoints.hyperliquid, symbols, store, http,
                               on_event=lambda ev: recorder.record("smart_events", ev))
        comps.top_traders = top
        pollers.append(lambda: top.run(settings.top_traders.poll_s))
    comps.extra_tasks = pollers
    return comps, market, recorder


async def run_live_like(settings: Settings, secrets: Secrets, alert: Alerter) -> None:
    comps, market, recorder = build_components(settings, secrets)
    info = await market.verify_symbols(allow_missing=True)  # nunca se opera un símbolo que no exista
    await market.load_funding_intervals()
    await market.bootstrap_klines()
    engine = Engine(settings, secrets, comps, alert=alert)
    engine.unavailable = set(comps.states) - set(info)
    engine.system["unavailable"] = sorted(engine.unavailable)
    if engine.unavailable:
        engine.alert(f"monedas no disponibles en Binance, sin entradas: {sorted(engine.unavailable)}", "warning")

    async def sync_clock(now: datetime) -> None:
        """El reloj de la PC puede desfasarse: con unos segundos los precios parecerían vencidos (TTL 5 s)."""
        try:
            off = await market.server_offset()
        except Exception as exc:  # noqa: BLE001
            log.warning("no se pudo medir la hora de Binance", extra={"err": repr(exc)})
            return
        engine.clock._offset = timedelta(seconds=off)
        engine.system["clock_offset_s"] = round(off, 2)
        if abs(off) > 2:
            engine.alert(f"el reloj de esta PC difiere {off:+.1f} s de Binance; el bot lo compensa, pero conviene "
                         "sincronizar la hora de Windows", "warning")

    async def check_symbols(now: datetime) -> None:
        try:
            found = await market.verify_symbols(allow_missing=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("no se pudo revisar el estado de las monedas", extra={"err": repr(exc)})
            return
        missing = set(comps.states) - set(found)
        for sym in missing - engine.unavailable:
            engine.alert(f"{sym} dejó de estar disponible en Binance: sin entradas nuevas (las abiertas se cuidan)",
                         "warning")
        for sym in engine.unavailable - missing:
            engine.alert(f"{sym} vuelve a estar disponible", "info")
        engine.unavailable = missing
        engine.system["unavailable"] = sorted(missing)

    await sync_clock(engine.clock.now())
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
        from bot.execution.user_stream import UserStream
        eps = settings.endpoints.binance_testnet if settings.mode is Mode.TESTNET else settings.endpoints.binance
        engine.user_stream = UserStream(ex, eps.ws_private, engine.on_account_event)
        comps.extra_tasks.append(engine.user_stream.run)
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

    started = datetime.now(UTC)

    async def check_restart(now: datetime) -> None:
        """Botón "Actualizar" del panel: guardar todo y salir; el .bat o systemd arrancan el código nuevo."""
        if updater.restart_requested(started):
            engine.save_paper()
            recorder.flush()
            engine.alert("reiniciando para aplicar la actualización", "info")
            print("\n  Actualización lista: reiniciando JARVIS con la versión nueva...", flush=True)
            logging.shutdown()
            os._exit(updater.RESTART_CODE)  # salida limpia: estado y diario ya guardados

    comps.extra_tasks += [market.run, lambda: engine._loop(5.0, heartbeat), lambda: engine._loop(1.0, book_sampler),
                          lambda: engine._loop(3600.0, sync_clock), lambda: engine._loop(3600.0, check_symbols),
                          lambda: engine._loop(5.0, check_restart)]
    if settings.alerts.telegram_enabled and secrets.telegram_bot_token and secrets.telegram_chat_id:
        from bot.core.telegram_commands import TelegramCommands  # /estado, /hoy, /solo_cierre... desde el teléfono
        comps.extra_tasks.append(TelegramCommands(secrets.telegram_bot_token, secrets.telegram_chat_id, engine).run)
    await engine.run()


async def run_record(settings: Settings, secrets: Secrets, alert: Alerter) -> None:
    """Modo `record`: solo graba datos y mide completitud para la puerta de fase."""
    comps, market, recorder = build_components(settings, secrets)
    await market.verify_symbols(allow_missing=True)
    await market.load_funding_intervals()
    engine = Engine(settings, secrets, comps, alert=alert)

    seen_reconnects = {"n": 0}

    async def stats(now: datetime) -> None:
        recorder.flush()
        if market.reconnects > seen_reconnects["n"]:
            engine.journal.record_event(now, "reconexion", {"total": market.reconnects})
            seen_reconnects["n"] = market.reconnects
        gaps = {s: st.candles["1m"].gaps(60_000) for s, st in comps.states.items()}
        engine.health.write(now, {"mode": "record", "counts": dict(recorder.counts), "kline_1m_gaps": gaps,
                                  "reconnects": market.reconnects, "connected": market.connected})

    async def book_sampler(now: datetime) -> None:
        for sym, st in comps.states.items():
            if st.book.synced:
                bids, asks = st.book.top(20)
                recorder.record(f"{sym.lower()}@book20", {"bids": bids, "asks": asks, "E": st.book.event_ts_ms}, now)

    await asyncio.gather(market.run(), engine._loop(30.0, stats), engine._loop(1.0, book_sampler),
                         *(make() for make in comps.extra_tasks))
