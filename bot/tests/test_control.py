"""Controles del panel: Operar / Solo cierre y agresividad 1–10 (nunca más laxa que config.yaml)."""
import asyncio
import http.client
import json
import threading

import pytest

from bot.core import control
from bot.core.config import load_settings
from bot.dashboard.server import serve
from bot.learning.journal import REJECTED, RISK_REJECTED
from bot.tests.test_engine_paper import FakeAnalyst, _fill, make_engine, setup  # noqa: F401
from bot.core.models import AIAction


def test_levels_never_looser_than_config():
    s = load_settings()
    top, _ = control.apply(s, control.Control(aggressiveness=10))
    assert top.risk.risk_per_trade == s.risk.risk_per_trade
    assert top.filters.min_edge_cost_multiple == s.filters.min_edge_cost_multiple
    assert top.limits.max_trades_per_day == s.limits.max_trades_per_day
    assert control.profile(10, s).min_ai_confidence == 0
    prev = None
    for lv in range(1, 11):
        eff, p = control.apply(s, control.Control(aggressiveness=lv))
        assert eff.risk.risk_per_trade <= s.risk.risk_per_trade
        assert eff.filters.min_edge_cost_multiple >= s.filters.min_edge_cost_multiple >= 3.0
        assert 1 <= eff.limits.max_trades_per_day <= s.limits.max_trades_per_day
        assert eff.limits.base_trades_per_day <= eff.limits.max_trades_per_day
        assert eff.risk.max_leverage == s.risk.max_leverage and eff.risk.max_drawdown == s.risk.max_drawdown
        if prev:  # más nivel = igual o más permisivo, nunca al revés
            assert p.risk_factor >= prev.risk_factor and p.min_edge <= prev.min_edge
            assert p.max_trades >= prev.max_trades and p.min_ai_confidence <= prev.min_ai_confidence
        prev = p
    low = control.profile(1, s)
    assert (low.risk_factor, low.min_edge, low.max_trades, low.min_ai_confidence) == (0.4, 5.0, 2, 0.7)
    with pytest.raises(ValueError):
        control.Control(aggressiveness=11)


def test_missing_file_is_default_and_corrupt_is_close_only(tmp_path):
    p = tmp_path / "control.json"
    assert control.load(p) == control.Control()
    p.write_text("{no es json", encoding="utf-8")
    assert control.load(p).close_only
    p.write_text(json.dumps({"mode": "operar", "aggressiveness": 99}), encoding="utf-8")
    assert control.load(p).close_only
    saved = control.save(p, control.Control(aggressiveness=4))
    assert control.load(p).aggressiveness == 4 and saved.updated_at is not None


def test_close_only_blocks_entries_but_journals_candidates(setup):
    s, states, ex, now = setup
    control.save(control.path_for(s), control.Control(mode="solo_cierre"))
    analyst = FakeAnalyst()
    eng = make_engine(s, states, ex, analyst, now)
    assert asyncio.run(eng.evaluate_window(now)) is None
    cands = eng.journal.candidates(now.replace(year=2000))
    assert cands and all(c["status"] == REJECTED for c in cands)
    assert any("solo cierre" in r for r in cands[0]["reasons"])
    assert analyst.calls == 0 and ex.orders == {}


def test_close_only_pressed_while_ai_thinks(setup):
    s, states, ex, now = setup

    class SlowAnalyst(FakeAnalyst):
        async def decide(self, sig, prompt):
            control.save(control.path_for(s), control.Control(mode="solo_cierre"))  # el usuario pulsa el botón
            return await super().decide(sig, prompt)
    eng = make_engine(s, states, ex, SlowAnalyst(), now)
    sig = asyncio.run(eng.evaluate_window(now))
    cand = [c for c in eng.journal.candidates(now.replace(year=2000)) if c["id"] == sig.id][0]
    assert cand["status"] == RISK_REJECTED and "solo_cierre" in cand["reasons"][0] and ex.orders == {}


def test_low_aggressiveness_requires_more_ai_confidence_and_less_risk(setup):
    s, states, ex, now = setup
    control.save(control.path_for(s), control.Control(aggressiveness=1))
    eng = make_engine(s, states, ex, FakeAnalyst(AIAction.LONG, 0.6), now)
    sig = asyncio.run(eng.evaluate_window(now))
    assert sig is not None  # la señal de prueba tiene ventaja 15×: pasa el mínimo de 5× del nivel 1
    cand = [c for c in eng.journal.candidates(now.replace(year=2000)) if c["id"] == sig.id][0]
    assert cand["status"] == RISK_REJECTED and cand["reasons"][0].startswith("agresividad") and ex.orders == {}
    assert eng.eff.risk.risk_per_trade == pytest.approx(s.risk.risk_per_trade * 0.4)
    assert eng.risk.limits.max_trades_per_day == 2


def test_level_change_applies_without_restart(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eng.refresh_control()
    assert eng.eff.risk.risk_per_trade == s.risk.risk_per_trade
    control.save(control.path_for(s), control.Control(aggressiveness=4))
    eng.refresh_control()
    assert eng.profile.level == 4 and eng.risk.risk is eng.eff.risk


def _post(port, body, headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/control", body=json.dumps(body), headers={"Content-Type": "application/json", **headers})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def test_panel_post_control_is_local_only(setup):
    s = setup[0]
    srv = serve(s, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    ok = {"X-Jarvis-Panel": "1"}
    try:
        assert _post(port, {"mode": "solo_cierre"}, {})[0] == 403                       # sin la cabecera
        assert _post(port, {"mode": "solo_cierre"}, {**ok, "Origin": "https://malo.example"})[0] == 403
        assert _post(port, {"mode": "solo_cierre"}, {**ok, "Host": "malo.example"})[0] == 403
        assert not control.path_for(s).exists()
        assert _post(port, {"aggressiveness": 11}, ok)[0] == 400
        st, j = _post(port, {"mode": "solo_cierre"}, ok)
        assert st == 200 and j["mode"] == "solo_cierre" and j["aggressiveness"] == 10
        st, j = _post(port, {"aggressiveness": 3}, {**ok, "Origin": f"http://127.0.0.1:{port}"})
        assert st == 200 and j == {**j, "mode": "solo_cierre", "aggressiveness": 3}
        assert control.load(control.path_for(s)).aggressiveness == 3
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/estado")
        d = json.loads(c.getresponse().read())
        assert d["control"]["mode"] == "solo_cierre" and len(d["control"]["levels"]) == 10
        assert d["kpis"]["techo_hoy"] == control.profile(3, s).max_trades
    finally:
        srv.shutdown()
        srv.server_close()
