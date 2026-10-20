"""MIDAS: laboratorio de apalancamiento (solo simulación)."""
import pytest

from bot.learning import midas

FEE = 0.0007


def cand(r_gross, mae_r, status="operado", entry=100.0, stop=98.0, ts="2026-10-19T10:00:00+00:00", side="LONG"):
    return {"id": ts, "ts": ts, "symbol": "ETHUSDT", "side": side, "strategy": "trend_breakout", "status": status,
            "entry": entry, "stop": stop, "take_profit": 106.0, "max_hold_hours": 12,
            "counterfactual": {"r_gross": r_gross, "r_net": r_gross, "exit": "take_profit", "mae_r": mae_r, "mfe_r": 3}}


def test_pnl_scales_with_leverage_and_pays_fees():
    c = cand(r_gross=2.0, mae_r=-0.3)                 # stop a 2 %: +2R = +4 %
    o10 = midas.outcome(c, 10, 20, FEE)
    assert o10["notional"] == 200 and o10["pnl"] == pytest.approx(200 * 0.04 - 200 * FEE)
    assert midas.outcome(c, 20, 20, FEE)["pnl"] == pytest.approx(2 * 200 * 0.04 - 400 * FEE)


def test_liquidation_before_the_stop_loses_the_whole_margin():
    c = cand(r_gross=2.0, mae_r=-1.0, stop=95.0)      # stop a 5 %; llegó 5 % en contra y luego ganó
    assert midas.liq_distance(25) == pytest.approx(0.035)
    o = midas.outcome(c, 25, 20, FEE)
    assert o["liquidated"] and o["pnl"] == -20        # a x25 se liquida al 3,5 %: nunca llega a ganar
    assert not midas.outcome(c, 5, 20, FEE)["liquidated"]  # a x5 aguanta (liquida al 19,5 %)
    loser = midas.outcome(cand(r_gross=-1.0, mae_r=-1.0, stop=99.0), 10, 20, FEE)
    assert not loser["liquidated"] and loser["pnl"] == pytest.approx(-200 * 0.01 - 200 * FEE)
    assert midas.outcome({"entry": 100, "stop": 99, "counterfactual": None}, 10, 20, FEE) is None


def test_simulate_compares_discipline_and_explains():
    cands = [cand(2.0, -0.2, ts="2026-10-19T01:00:00+00:00"),
             cand(-1.0, -1.0, status="rechazado_ia", ts="2026-10-19T02:00:00+00:00"),
             cand(-1.0, -1.0, status="rechazado_filtro", ts="2026-10-19T03:00:00+00:00"),
             {**cand(0, 0, ts="2026-10-19T04:00:00+00:00"), "counterfactual": None}]
    out = midas.simulate(cands, 20, FEE, last={"ETHUSDT": 101.0})
    a = {s["leverage"]: s for s in out["aprobadas"]["by_leverage"]}
    t = {s["leverage"]: s for s in out["todas"]["by_leverage"]}
    assert a[10]["n"] == 1 and t[10]["n"] == 3 and a[10]["total"] > t[10]["total"]
    assert t[10]["max_drawdown"] > 0 and len(t[10]["curve"]) == 3
    assert out["live"][0]["pnl"][10] == pytest.approx(2.0)   # +1 % × 200 de posición
    assert any("disciplina salió mejor" in x for x in out["lessons"])
