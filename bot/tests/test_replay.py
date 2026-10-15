from datetime import UTC, datetime, timedelta

from bot.backtest.engine import Backtester
from bot.core.config import load_settings
from bot.core.models import DataStatus
from bot.data_engine.market_state import MarketState
from bot.data_engine.recorder import Recorder
from bot.data_engine.store import AsOfStore, FeedStore, load_recorded_store
from bot.tests.helpers import fill_state, trend_series

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def test_asof_store_never_sees_the_future():
    s = FeedStore()
    for h in range(10):
        s.set("deriv.oi.ETHUSDT", 100.0 + h, "t", T0 + timedelta(hours=h))
    v = AsOfStore(s, T0 + timedelta(hours=4, minutes=30))
    assert v.get("deriv.oi.ETHUSDT").value == 104.0
    assert v.values("deriv.oi.ETHUSDT").max() == 104.0
    assert v.value_at_or_before("deriv.oi.ETHUSDT", T0 + timedelta(hours=9)) == 104.0
    assert AsOfStore(s, T0 - timedelta(hours=1)).get("deriv.oi.ETHUSDT").status is DataStatus.UNKNOWN
    v.set("calc.x", 1.0, "calc", T0 + timedelta(hours=9))  # escritura del futuro: ignorada
    assert v.get("calc.x").status is DataStatus.UNKNOWN


def test_out_of_order_backfill_kept_in_history():
    s = FeedStore()
    s.set("k", 3.0, "t", T0 + timedelta(hours=3))
    s.set("k", 1.0, "t", T0 + timedelta(hours=1))  # relleno antiguo
    assert s.get("k").value == 3.0
    assert [v for _, v in s.history("k")] == [1.0, 3.0]


def test_recorded_store_roundtrip(tmp_path):
    rec = Recorder(tmp_path, flush_every_s=999)
    live = FeedStore(on_set=lambda k, v, src, ts: rec.record("store", {"k": k, "v": v, "src": src,
                                                                        "ts": ts.isoformat()}, ts))
    for h in range(5):
        live.set("deriv.oi.BTCUSDT", 1e9 + h, "binance_rest", T0 + timedelta(hours=h))
        live.set("deriv.oi.BTCUSDT", 1e9 + h, "binance_rest", T0 + timedelta(hours=h))  # repetido: no se graba
    live.set("calc.fund_diff.BTCUSDT", 0.1, "calc", T0)  # derivado: no se graba
    rec.flush()
    replay = load_recorded_store(tmp_path / "raw", [T0.strftime("%Y-%m-%d")])
    assert [v for _, v in replay.history("deriv.oi.BTCUSDT")] == [1e9 + h for h in range(5)]
    assert replay.get("calc.fund_diff.BTCUSDT").status is DataStatus.UNKNOWN


def test_backtest_features_use_recorded_data_as_of_bar():
    s = FeedStore()
    for h in range(200):
        s.set("deriv.oi.ETHUSDT", 1e9 * (1 + 0.001 * h), "t", T0 + timedelta(hours=h))
    for i in range(-60, 60):
        s.set("deriv.funding_hist.ETHUSDT", 0.0001 + 0.00002 * (i % 3), "t", T0 + timedelta(hours=8 * i))
    bt = Backtester(load_settings(), store=s)
    st = MarketState("ETHUSDT")
    asof = T0 + timedelta(hours=100, minutes=7)
    fill_state(st, {"15m": trend_series(300, 0, 0.002, seed=1, start=2500),
                    "1h": trend_series(300, 0, 0.003, seed=2, start=2500)}, end_ms=int(asof.timestamp() * 1000))
    f = bt._features("ETHUSDT", st, asof)
    assert f["ind.oi_chg_1h"].is_ok
    assert f["ind.oi_chg_1h"].value > 0
    assert f["funding.rate_1h"].source == "replay" and f["funding.rate_1h"].value > 0
    assert f["ind.funding_z"].is_ok


def test_record_gate_needs_7_full_days_and_a_reconnect(tmp_path):
    from datetime import date

    from bot import gates
    from bot.learning.journal import Journal
    syms = ["BTCUSDT", "ETHUSDT"]
    raw = tmp_path / "raw"
    start = date(2026, 10, 1)
    for i in range(9):
        d = start + timedelta(days=i)
        for s in syms:
            p = raw / f"{s.lower()}@kline_1m" / f"{d.isoformat()}.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            n = 1440 if i != 1 else 1300  # el día 2 pierde ~10 %
            p.write_text("{}\n" * n)
    res = gates.record_gate(tmp_path, syms, today=date(2026, 10, 10))
    assert res["mejor_racha_dias"] == 7 and not res["ok"]  # falta reconexión probada
    j = Journal(tmp_path / "bot_record.sqlite")
    j.record_event(datetime(2026, 10, 5, tzinfo=UTC), "reconexion", {"total": 1})
    j.close()
    assert gates.record_gate(tmp_path, syms, today=date(2026, 10, 10))["ok"]
    assert not gates.record_gate(tmp_path, syms, today=date(2026, 10, 9))["ok"]  # solo 6 días completos
