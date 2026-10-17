"""Escenarios de la vida real con el bot desatendido: disco, monedas retiradas, ventanas trabadas, IA caída."""
import asyncio
import gzip
from datetime import UTC, date, datetime, timedelta

from bot.data_engine import retention
from bot.dashboard.data import ai_health
from bot.learning.journal import REJECTED
from bot.tests.test_engine_paper import FakeAnalyst, make_engine, setup  # noqa: F401

TODAY = date(2026, 10, 20)


def _make(raw, source, day, size=1000):
    d = raw / source
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{day.isoformat()}.jsonl"
    f.write_text('{"x":1}\n' * (size // 8), encoding="utf-8")
    return f


def test_retention_compresses_and_deletes_only_high_volume(tmp_path):
    raw = tmp_path / "raw"
    old, recent = TODAY - timedelta(days=20), TODAY - timedelta(days=1)
    for src in ("btcusdt@aggTrade", "btcusdt@book20", "btcusdt@kline_1m", "store"):
        _make(raw, src, old)
        _make(raw, src, recent)
        _make(raw, src, TODAY)
    rep = retention.run(raw, TODAY, retention_days=14, min_free_gb=1, free_fn=lambda p: 100.0, min_age_s=0)
    # hoy no se toca; lo demás se comprime
    assert (raw / "store" / f"{TODAY}.jsonl").exists() and rep.compressed == 8
    with gzip.open(raw / "btcusdt@kline_1m" / f"{recent}.jsonl.gz", "rt") as fh:
        assert fh.readline().strip() == '{"x":1}'
    # lo viejo de alto volumen se borra; velas y series REST se conservan siempre
    assert not (raw / "btcusdt@aggTrade" / f"{old}.jsonl.gz").exists()
    assert not (raw / "btcusdt@book20" / f"{old}.jsonl.gz").exists()
    assert (raw / "btcusdt@kline_1m" / f"{old}.jsonl.gz").exists() and (raw / "store" / f"{old}.jsonl.gz").exists()
    assert (raw / "btcusdt@aggTrade" / f"{recent}.jsonl.gz").exists() and not rep.low_disk


def test_low_disk_deletes_oldest_high_volume_first(tmp_path):
    raw = tmp_path / "raw"
    days = [TODAY - timedelta(days=i) for i in (5, 4, 3)]
    for d in days:
        _make(raw, "ethusdt@aggTrade", d)
        _make(raw, "store", d)
    free = iter([1.0, 1.5, 3.0, 3.0])          # GB libres tras cada borrado
    rep = retention.run(raw, TODAY, retention_days=14, min_free_gb=2.0, free_fn=lambda p: next(free), min_age_s=0)
    assert rep.deleted == [f"ethusdt@aggTrade/{days[0]}.jsonl.gz", f"ethusdt@aggTrade/{days[1]}.jsonl.gz"]
    assert not rep.low_disk and len(list((raw / "store").iterdir())) == 3


def test_low_disk_alert_when_nothing_left_to_delete(tmp_path):
    raw = tmp_path / "raw"
    _make(raw, "store", TODAY - timedelta(days=3))
    rep = retention.run(raw, TODAY, retention_days=14, min_free_gb=10.0, free_fn=lambda p: 1.0, min_age_s=0)
    assert rep.low_disk and rep.deleted == []


def test_verify_symbols_tolerates_maintenance():
    from bot.data_engine.market_ws import BinanceMarketData

    class Http:
        def __init__(self, live):
            self.live = live

        async def get_json(self, url, **kw):
            return {"symbols": [{"symbol": s, "contractType": "PERPETUAL", "status": "TRADING"} for s in self.live]
                    + [{"symbol": "XLMUSDT", "contractType": "PERPETUAL", "status": "SETTLING"}]}
    m = BinanceMarketData.__new__(BinanceMarketData)
    m.rest, m.states = "x", {"BTCUSDT": None, "XLMUSDT": None}
    m.http = Http(["BTCUSDT"])
    assert set(asyncio.run(m.verify_symbols(allow_missing=True))) == {"BTCUSDT"}
    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(m.verify_symbols())
    m.http = Http([])
    with pytest.raises(RuntimeError):  # sin ninguna moneda no tiene sentido arrancar
        asyncio.run(m.verify_symbols(allow_missing=True))


def test_unavailable_symbol_blocks_entries_but_not_others(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eng.unavailable = {"ETHUSDT"}
    asyncio.run(eng.evaluate_window(now))
    eth = [c for c in eng.journal.candidates(now.replace(year=2000)) if c["symbol"] == "ETHUSDT"]
    assert eth and all(c["status"] == REJECTED for c in eth)
    assert any("no disponible" in r for r in eth[0]["reasons"]) and ex.orders == {}


def test_window_not_stuck_when_one_coin_stops(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    calls = []

    async def fake_eval(t):
        calls.append(t)
    eng.evaluate_window = fake_eval
    close = datetime(2026, 10, 16, 12, 15, tzinfo=UTC)

    async def run(t):
        eng.evaluate_late_window(t)
        await asyncio.sleep(0)
    asyncio.run(run(close + timedelta(seconds=30)))       # aún puede llegar la vela de la moneda lenta
    asyncio.run(run(close + timedelta(minutes=3)))        # ya no: se evalúa igual
    assert calls == [close]


def test_alerts_go_to_the_log_shown_in_panel(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eng.alert("fuente caída: news", "warning")
    eng.alert("fuente caída: news", "warning")  # repetida: el Alerter la deduplica
    ev = [e for e in eng.journal.events(now - timedelta(hours=1)) if e["kind"] == "alerta"]
    assert len(ev) == 1 and "news" in ev[0]["payload"]


def test_ai_health_explains_failures():
    assert ai_health([])["code"] == "sin_consultas"
    no_credit = [{"ts": "2026-10-16T10:00", "valid": 0,
                  "reason": "api 400: Your credit balance is too low to access the Anthropic API"}] * 3
    h = ai_health(no_credit)
    assert h["code"] == "falla" and "saldo" in h["label"]
    bad_key = [{"ts": "x", "valid": 0, "reason": "api 401: invalid x-api-key"}] * 3
    assert "llave" in ai_health(bad_key)["label"]
    mixed = [{"ts": "2026-10-16T10:00", "valid": 0, "reason": "timeout"},
             {"ts": "2026-10-16T09:00", "valid": 1, "reason": "ok"}]
    assert ai_health(mixed)["code"] == "ok"


def test_system_status_reaches_panel(setup):
    from bot.dashboard import snapshot
    from bot.dashboard.data import collect
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eng.system.update(clock_offset_s=0.4, free_gb=120.0)
    eng.write_panel(now)
    d = collect(s, now)
    names = [x["name"] for x in d["system"]["sources"]]
    assert "Precios en vivo (Binance)" in names and d["system"]["clock_offset_s"] == 0.4
    assert d["system"]["ai"]["code"] == "sin_consultas" and "performance" in d and d["bitacora"] == []
    assert snapshot.read(s.storage.reports_dir / snapshot.FILENAME)["system"]["free_gb"] == 120.0
