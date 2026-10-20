"""Cuenta MIDAS: opera con dinero ficticio a x5–x25, aparte de la demo, y nunca existe con dinero real."""
import http.client
import json
import threading
from datetime import timedelta

import pytest

from bot.core.clock import utc_now
from bot.core.config import Mode, load_settings
from bot.core.models import Regime, Side, Signal
from bot.dashboard.server import serve
from bot.execution import midas as M
from bot.tests.test_engine_paper import FakeAnalyst, make_engine, setup  # noqa: F401

S = load_settings()
NOW = utc_now()


def sig(symbol="ETHUSDT", side=Side.LONG, entry=100.0, stop=98.0, tp=106.0, hold=12):
    return Signal(id=f"{symbol}{side.value}", symbol=symbol, strategy="trend_breakout", side=side, entry=entry, stop=stop,
                  take_profit=tp, expected_move_pct=0.06, total_cost_pct=0.001, data_quality=1, regime=Regime.TREND_UP,
                  max_hold_hours=hold, created_at=NOW)


def acct(tmp_path, **ctl):
    a = M.MidasAccount(tmp_path, S.midas, S.exchange.fees, Mode.PAPER)
    if ctl:
        M.save_control(tmp_path / M.CONTROL_FILENAME, M.MidasControl(**{"enabled": True, "leverage": 10, "margin": 20,
                                                                       "scope": "todas", **ctl}))
    return a


def test_never_exists_with_real_money(tmp_path):
    for mode in (Mode.LIVE, Mode.TESTNET, Mode.BACKTEST, Mode.RECORD):
        with pytest.raises(RuntimeError):
            M.MidasAccount(tmp_path, S.midas, S.exchange.fees, mode)


def test_take_profit_stop_and_liquidation(tmp_path):
    a = acct(tmp_path, leverage=10, margin=20)
    taker = S.exchange.fees.taker
    p = a.consider(sig(), 100.0, NOW, approved=False)
    assert p["notional"] == 200 and p["liq"] == pytest.approx(100 * (1 - 0.095)) and a.balance == pytest.approx(500 - 200 * taker)
    assert a.consider(sig(), 100.0, NOW, approved=False) is None          # una por moneda
    (t,) = a.update({"ETHUSDT": 106.5}, NOW + timedelta(minutes=5))
    assert t["reason"] == "take_profit" and t["pnl"] == pytest.approx(2 * 6 - 2 * 106 * taker)
    # a x25 el precio llega a la liquidación (−3,5 %) antes que al stop (−5 %): se pierde el margen
    M.save_control(a.control_path, M.MidasControl(enabled=True, leverage=25, margin=20, scope="todas"))
    a.consider(sig(stop=95.0), 100.0, NOW, approved=False)
    before = a.balance
    (t,) = a.update({"ETHUSDT": 96.4}, NOW + timedelta(minutes=10))
    assert t["reason"] == "liquidada" and a.balance == pytest.approx(before - 20) and t["ret"] < -1
    a.consider(sig(side=Side.SHORT, entry=100, stop=101, tp=95), 100.0, NOW, approved=False)
    (t,) = a.update({"ETHUSDT": 101.2}, NOW + timedelta(minutes=15))
    assert t["reason"] == "stop" and t["pnl"] < 0
    s = a.summary({})
    assert s["stats"]["n"] == 3 and s["stats"]["liquidated"] == 1 and s["stats"]["by_leverage"]["25"]["n"] == 2
    b = M.MidasAccount(tmp_path, S.midas, S.exchange.fees, Mode.PAPER)   # sobrevive reinicios
    assert b.balance == pytest.approx(a.balance) and len(b.closed) == 3


def test_time_exit_scope_and_empty_account(tmp_path):
    a = acct(tmp_path, scope="aprobadas")
    assert a.consider(sig(), 100.0, NOW, approved=False) is None          # sin la IA no entra
    assert a.consider(sig(), 100.0, NOW, approved=True) is not None
    assert a.update({"ETHUSDT": 100.5}, NOW + timedelta(hours=1)) == []
    (t,) = a.update({"ETHUSDT": 100.5}, NOW + timedelta(hours=12, minutes=1))
    assert t["reason"] == "tiempo"
    M.save_control(a.control_path, M.MidasControl(enabled=True, leverage=10, margin=600, scope="todas"))
    assert a.consider(sig(), 100.0, NOW, approved=True) is None           # margen mayor que el saldo: no abre
    M.save_control(a.control_path, M.MidasControl(enabled=False, leverage=10, margin=20, scope="todas"))
    assert a.consider(sig(), 100.0, NOW, approved=True) is None           # apagado
    with pytest.raises(ValueError):
        M.MidasControl(enabled=True, leverage=50, margin=20)
    a.control_path.write_text("{roto", encoding="utf-8")
    a.control_path.with_name(a.control_path.name + ".bak").unlink(missing_ok=True)
    assert not a.control().enabled                                         # ilegible = no abre nada


def test_engine_runs_midas_only_in_demo_and_panel_controls_it(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    assert eng.midas is not None
    sg = sig(entry=states["ETHUSDT"].last_price or 2500, stop=(states["ETHUSDT"].last_price or 2500) * 0.98, tp=None)
    eng._midas_consider(sg, now, approved=False)
    assert "ETHUSDT" in eng.midas.positions
    eng.write_panel(now)
    live = json.loads((s.storage.reports_dir / "panel_vivo.json").read_text())
    assert live["midas"]["positions"][0]["symbol"] == "ETHUSDT"
    live_mode = s.model_copy(update={"mode": Mode.LIVE})
    assert make_engine(live_mode, states, ex, FakeAnalyst(), now).midas is None
    srv = serve(s, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def post(body, headers):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request("POST", "/api/midas/control", body=json.dumps(body), headers={"Content-Type": "application/json", **headers})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        assert post({"leverage": 20}, {})[0] == 403                         # sin la cabecera del panel
        assert post({"leverage": 30}, {"X-Jarvis-Panel": "1"})[0] == 400
        st, j = post({"leverage": 20, "margin": 25}, {"X-Jarvis-Panel": "1"})
        assert st == 200 and j["control"]["leverage"] == 20 and eng.midas.control().margin == 25
    finally:
        srv.shutdown()
        srv.server_close()
