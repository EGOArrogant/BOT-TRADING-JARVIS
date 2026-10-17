"""Estrategia discount_absorption (método de Chris): solo entra con ubicación + absorción + giro."""
from datetime import UTC, datetime

from bot.core.config import StrategyParams, load_settings
from bot.core.models import DataPoint, Regime, Side
from bot.filters.strategies import absorption_checklist, discount_absorption

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
P = StrategyParams(enabled=True, target_atr_multiple=4.0, stop_atr_multiple=1.0, max_hold_hours=12)
FEES = load_settings().exchange.fees


def fields(**over):
    # impulso alcista 100 -> 120; precio en 104 = retroceso del 80 % (dentro de 70,5–88,6 %)
    base = {"ind.1h.imp_up_high": 120, "ind.1h.imp_up_low": 100, "ind.1h.imp_up_ext": 103.5, "ind.1h.atr": 2.0,
            "ind.15m.close": 104.0, "ind.15m.open": 103.4, "ind.15m.high": 104.2, "ind.15m.low": 103.6,
            "ind.15m.atr": 0.6, "ind.15m.prev_high": 104.0, "ind.15m.prev_low": 103.5, "ind.15m.prev_close": 103.85,
            "ind.15m.prev_delta_ratio": -0.2, "market.spread_bps": 0.5, "funding.rate_1h": 0.00001}
    base.update(over)
    return {k: DataPoint.ok(v, "test", NOW) for k, v in base.items() if v is not None}


def run(**over):
    return discount_absorption("ETHUSDT", fields(**over), P, FEES, NOW, Regime.TREND_UP, 1.0)


def test_enters_long_with_stop_behind_failed_sellers_and_target_at_the_high():
    s = run()
    assert s is not None and s.side is Side.LONG and s.take_profit == 120
    assert s.stop < 103.5 and 104 - s.stop >= 0.6 - 1e-9         # detrás del mínimo absorbido, al menos 1 ATR de 15 min
    assert (120 - 104) / (104 - s.stop) >= 1.5


def test_needs_absorption_and_shift_and_the_zone():
    assert run(**{"ind.15m.prev_delta_ratio": 0.1}) is None          # sin vendedores agresivos: no hay absorción
    assert run(**{"ind.15m.prev_close": 103.55}) is None             # cerró abajo: los vendedores sí ganaron
    assert run(**{"ind.15m.close": 103.3}) is None                   # vela actual no gira (cierra bajo su apertura)
    assert run(**{"ind.15m.low": 103.4}) is None                     # vuelve a hacer mínimo más bajo: no fallaron más arriba
    assert run(**{"ind.15m.close": 110.0, "ind.15m.high": 110.2}) is None  # 50 %: no es descuento
    assert run(**{"ind.1h.imp_up_ext": 101.0}) is None               # ya pasó del 88,6 %: idea anulada
    assert run(**{"ind.1h.imp_up_high": 102.5}) is None              # impulso chico (< 2 ATR de 1 h)
    assert run(**{"ind.1h.imp_up_high": None}) is None               # dato faltante = desconocido: no opera


def test_checklist_matches_the_strategy():
    ck = absorption_checklist(fields(), P)
    assert ck["ready"] and ck["side"] == "LONG"
    ck = absorption_checklist(fields(**{"ind.15m.prev_delta_ratio": 0.1}), P)
    assert not ck["ready"] and [i["ok"] for i in ck["items"]] == [True, True, False, True]
