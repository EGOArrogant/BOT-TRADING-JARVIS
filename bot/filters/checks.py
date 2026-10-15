"""MÓDULO 3 — Filtros. Un candidato llega a la IA solo si pasa todos.

Cada filtro devuelve un Check con motivo; todo rechazo se registra.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bot.core.config import FiltersConfig, RiskConfig
from bot.core.models import DataPoint, Side, Signal
from bot.core.state import BotState
from bot.data_engine.macro import MacroEvent, blocking_event
from bot.data_engine.normalizer import REQUIRED_FIELDS
from bot.risk.engine import Check, OpenExposure, RiskEngine

Fields = dict[str, DataPoint]


@dataclass
class FilterOutcome:
    signal: Signal
    passed: bool
    checks: list[tuple[str, Check]] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [f"{name}: {c.reason}" for name, c in self.checks if not c.ok]


def _v(f: Fields, k: str) -> float | None:
    dp = f.get(k)
    return dp.as_float() if dp is not None else None


def check_data(f: Fields) -> Check:
    missing = [k for k in REQUIRED_FIELDS if not (f.get(k) and f[k].is_ok)]
    return Check(not missing, f"datos no ok: {missing}" if missing else "")


def check_macro(now: datetime, events: list[MacroEvent], cfg: FiltersConfig, calendar_ok: bool,
                calendar_updated: datetime | None) -> Check:
    if not calendar_ok and (calendar_updated is None or now - calendar_updated > timedelta(hours=36)):
        return Check(False, "calendario macro desconocido")
    ev = blocking_event(now, events, cfg.macro_block_before_min, cfg.macro_block_after_min)
    return Check(ev is None, f"bloqueo macro: {ev.title} {ev.ts.isoformat()}" if ev else "")


def check_edge(sig: Signal, cfg: FiltersConfig) -> Check:
    ok = sig.edge_ratio >= cfg.min_edge_cost_multiple
    return Check(ok, "" if ok else f"ventaja {sig.edge_ratio:.2f}× < {cfg.min_edge_cost_multiple}×")


def check_liquidity(f: Fields, est_notional: float, cfg: FiltersConfig) -> Check:
    spread = _v(f, "market.spread_bps")
    depth = _v(f, "market.depth_notional")
    if spread is None or depth is None:
        return Check(False, "spread/profundidad desconocidos")
    if spread > cfg.max_spread_bps:
        return Check(False, f"spread {spread:.1f} bps > {cfg.max_spread_bps}")
    if depth < cfg.min_depth_multiple * est_notional:
        return Check(False, f"profundidad {depth:.0f} < {cfg.min_depth_multiple}× nocional {est_notional:.0f}")
    return Check(True)


def check_dangerous_leverage(sig: Signal, f: Fields, cfg: FiltersConfig) -> Check:
    fz = _v(f, "ind.funding_z")
    oi_high = _v(f, "ind.oi_at_30d_high")
    if fz is None or oi_high is None:
        return Check(False, "funding z / OI 30d desconocidos")
    if sig.side is Side.LONG and fz > cfg.dangerous_funding_z and oi_high >= 1:
        return Check(False, f"funding z {fz:.2f} con OI en máximos: riesgo de cascada de longs")
    if sig.side is Side.SHORT and fz < -cfg.dangerous_funding_z and oi_high >= 1:
        return Check(False, f"funding z {fz:.2f} con OI en máximos: riesgo de squeeze de shorts")
    return Check(True)


def check_extreme_vol(f: Fields, cfg: FiltersConfig) -> Check:
    pct = _v(f, "ind.vol_pct_30d")
    if pct is None:
        return Check(False, "percentil de volatilidad desconocido")
    return Check(pct <= cfg.extreme_vol_percentile,
                 f"volatilidad en percentil {pct:.1f} > {cfg.extreme_vol_percentile}" if pct > cfg.extreme_vol_percentile else "")


def run_filters(
    sig: Signal,
    f: Fields,
    now: datetime,
    state: BotState,
    risk: RiskEngine,
    risk_cfg: RiskConfig,
    cfg: FiltersConfig,
    macro_events: list[MacroEvent],
    macro_ok: bool,
    macro_updated: datetime | None,
    open_positions: list[OpenExposure],
) -> FilterOutcome:
    est_notional = state.equity * risk_cfg.risk_per_trade / max(sig.stop_distance_pct, 1e-9)
    checks = [
        ("datos", check_data(f)),
        ("macro", check_macro(now, macro_events, cfg, macro_ok, macro_updated)),
        ("ventaja", check_edge(sig, cfg)),
        ("liquidez", check_liquidity(f, est_notional, cfg)),
        ("apalancamiento", check_dangerous_leverage(sig, f, cfg)),
        ("volatilidad", check_extreme_vol(f, cfg)),
        ("techo_diario", risk.slot_check(state, now, sig.edge_ratio)),
        ("limites_moneda", risk.symbol_limits(state, sig.symbol, sig.side, now, open_positions)),
        ("guardias", risk.global_guard(state, now)),
    ]
    return FilterOutcome(sig, all(c.ok for _, c in checks), checks)


def window_key(ts: datetime, minutes: int) -> int:
    return int(ts.timestamp()) // (minutes * 60)


def select_best(passed: list[Signal]) -> tuple[Signal | None, list[Signal]]:
    """El de mayor puntuación pasa a la IA; el resto queda 'no seleccionado'."""
    if not passed:
        return None, []
    ranked = sorted(passed, key=lambda s: -s.score)
    return ranked[0], ranked[1:]
