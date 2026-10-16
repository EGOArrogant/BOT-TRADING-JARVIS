"""Panel: la foto en vivo del motor, la lectura de datos y el servidor local."""
import asyncio
import json
import threading
import urllib.request
from datetime import timedelta

from bot.core.clock import utc_now
from bot.dashboard import snapshot
from bot.dashboard.data import collect
from bot.dashboard.server import _clean, serve
from bot.learning.champion_challenger import ChampionChallenger
from bot.learning.hypotheses import Hypothesis
from bot.tests.test_engine_paper import FakeAnalyst, _fill, make_engine, setup  # noqa: F401


def _open_and_stop_out(eng, states, ex, now):
    eth = states["ETHUSDT"]

    async def run():
        t = asyncio.create_task(_fill(eth, ex, eth.book.best_bid() - 0.5))
        await eng.evaluate_window(now)
        await t
    asyncio.run(run())


def test_panel_shows_open_position_then_closed_trade(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    _open_and_stop_out(eng, states, ex, now)
    eng.write_panel(utc_now())
    snap = snapshot.read(s.storage.reports_dir / snapshot.FILENAME)
    assert snap and len(snap["prices"]["ETHUSDT"]) == snapshot.PRICE_BARS
    c15 = snap["candles"]["ETHUSDT"]["15m"]
    assert len(c15) == snapshot.CANDLE_BARS["15m"] and all(c[3] <= min(c[1], c[4]) <= max(c[1], c[4]) <= c[2] for c in c15)
    assert [c[0] for c in c15] == sorted({c[0] for c in c15})  # sin velas repetidas
    pos = snap["positions"][0]
    assert pos["symbol"] == "ETHUSDT" and pos["side"] == "LONG" and pos["stop"] < pos["entry"]

    eng.state.save(s.storage.state_path)
    d = collect(s, utc_now())
    assert d["status"]["code"] == "operando"
    assert d["kpis"]["entradas_hoy"] == 1 and d["positions"][0]["symbol"] == "ETHUSDT"
    assert d["decisions"][0]["status"] == "Operó"

    # Toca el stop: la operación aparece cerrada con su motivo y sin posiciones abiertas
    p = eng.manager.positions["ETHUSDT"]
    stop = float(p.stop_price)
    states["ETHUSDT"].book.apply_snapshot({"lastUpdateId": 10, "bids": [[str(stop - 0.2), "1000"]],
                                           "asks": [[str(stop - 0.18), "1000"]]})
    states["ETHUSDT"].mark_price = stop - 0.1
    asyncio.run(eng.tick(utc_now()))
    eng.write_panel(utc_now())
    d = collect(s, utc_now())
    assert d["positions"] == [] and len(d["trades"]) == 1
    t = d["trades"][0]
    assert t["exit_reason"] == "Stop-loss" and t["pnl_net"] < 0 and t["exit"] > 0
    assert d["kpis"]["cerradas_hoy"] == 1 and len(d["equity_curve"]) == 1


def test_panel_status_off_halted_and_learning(setup):
    s, states, ex, now = setup
    d = collect(s, now)  # sin bot, sin base de datos
    assert d["status"]["code"] == "apagado" and d["trades"] == [] and d["learning"] == []

    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eng.write_panel(now)
    assert collect(s, now + timedelta(minutes=10))["status"]["code"] == "apagado"  # foto vieja

    eng.state.halted, eng.state.halt_reason = True, "drawdown 10.5% ≥ 10%"
    eng.state.save(s.storage.state_path)
    st = collect(s, now)["status"]
    assert st["code"] == "detenido" and "drawdown" in st["detail"]

    cc = ChampionChallenger(eng.journal)
    cc.add(Hypothesis(kind="min_edge", description="Exigir ventaja 3.5x en DOGE", symbol="DOGEUSDT",
                      min_edge=3.5), now)
    d = collect(s, now)
    assert d["learning"][0]["text"] == "Exigir ventaja 3.5x en DOGE" and d["learning"][0]["cases_needed"] == 50


def test_server_serves_page_and_valid_json(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eng.write_panel(now)
    srv = serve(s, port=0)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "Panel del bot" in page and "/api/estado" in page
        data = json.loads(urllib.request.urlopen(base + "/api/estado").read())
        assert data["mode"] == s.mode.value and set(data["prices"]) == set(states)
    finally:
        srv.shutdown()
        srv.server_close()
    assert srv.server_address[0] == "127.0.0.1"
    assert _clean({"pf": float("inf"), "x": [float("nan"), 1.0]}) == {"pf": None, "x": [None, 1.0]}


def test_chart_candles_any_timeframe_with_cache_and_validation(setup):
    import urllib.error

    from bot.dashboard.klines import CACHE_S, INTERVALS, KlineSource
    s, *_ = setup
    calls, t = [], [0.0]

    def fetch(url, params):
        calls.append(params)
        if params["symbol"] == "XLMUSDT":
            raise OSError("sin internet")
        return [[1_000, "1", "2", "0.5", "1.5", "10", 0, "0"]]

    src = KlineSource("https://x", s.exchange.symbols, fetch=fetch, clock=lambda: t[0])
    assert {"1m", "1h", "4h", "1d", "1w", "1M"} <= set(INTERVALS)
    assert src.get("BTCUSDT", "4h") == [[1000, 1.0, 2.0, 0.5, 1.5, 10.0]]
    src.get("BTCUSDT", "4h")
    assert len(calls) == 1  # dentro de la caché no se vuelve a pedir
    t[0] += CACHE_S + 1
    src.get("BTCUSDT", "4h")
    assert len(calls) == 2 and calls[-1] == {"symbol": "BTCUSDT", "interval": "4h", "limit": 1000}

    srv = serve(s, port=0, klines=src)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert len(urllib.request.urlopen(base + "/grafico.js").read()) > 100_000
        assert json.loads(urllib.request.urlopen(base + "/api/velas?symbol=ETHUSDT&interval=1d").read())[0][4] == 1.5
        for q, code in (("symbol=FAKEUSDT&interval=1h", 400), ("symbol=ETHUSDT&interval=7m", 400),
                        ("symbol=XLMUSDT&interval=1h", 502)):
            try:
                urllib.request.urlopen(base + "/api/velas?" + q)
                raise AssertionError(q)
            except urllib.error.HTTPError as exc:
                assert exc.code == code
    finally:
        srv.shutdown()
        srv.server_close()
