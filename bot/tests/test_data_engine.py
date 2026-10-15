import asyncio
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from bot.core.clock import dt_to_ms
from bot.core.models import DataStatus
from bot.data_engine.derivatives import parse_funding_history, parse_series
from bot.data_engine.dex import hourly_funding, parse_meta_and_ctxs
from bot.data_engine.http import RateLimiter
from bot.data_engine.macro import blocking_event, MacroEvent, parse_forexfactory
from bot.data_engine.market_state import Candle, MarketState
from bot.data_engine.market_ws import MarketDispatcher, combined_url, market_streams, public_streams
from bot.data_engine.news import Headline, parse_cryptopanic, parse_rss, score_headlines
from bot.data_engine.normalizer import build_raw_snapshot, data_quality
from bot.data_engine.onchain import parse_fee_stats, parse_stablecoin_chart
from bot.data_engine.orderbook import OrderBook, OrderBookDesync
from bot.data_engine.recorder import Recorder, completeness
from bot.data_engine.sentiment import parse_fng
from bot.data_engine.store import FeedStore
from bot.data_engine.volatility import parse_dvol, realized_vol

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TTL = {"market": 5, "funding": 60, "open_interest": 900, "dvol": 900, "dex": 300, "sentiment": 129600,
       "onchain": 43200}


# ------------------------------------------------------------ order book


def test_orderbook_sync_and_sequence():
    ob = OrderBook("BTCUSDT")
    ob.on_event({"U": 95, "u": 99, "pu": 94, "b": [["100", "9"]], "a": []})      # anterior al snapshot
    ob.on_event({"U": 100, "u": 105, "pu": 99, "b": [["100", "2"]], "a": [["101", "0"]], "E": 1})
    ob.apply_snapshot({"lastUpdateId": 102, "bids": [["100", "1"], ["99", "3"]], "asks": [["101", "1"], ["102", "2"]]})
    assert ob.synced and ob.last_update_id == 105
    assert ob.bids[100.0] == 2 and 101.0 not in ob.asks
    assert ob.best_bid() == 100 and ob.best_ask() == 102
    ob.on_event({"U": 106, "u": 107, "pu": 105, "b": [], "a": [["101.5", "1"]], "E": 2})
    assert ob.best_ask() == 101.5
    with pytest.raises(OrderBookDesync):
        ob.on_event({"U": 109, "u": 110, "pu": 108, "b": [], "a": []})  # hueco
    assert not ob.synced and ob.last_update_id is None


def test_orderbook_first_event_must_cover_snapshot():
    ob = OrderBook("X")
    ob.on_event({"U": 200, "u": 210, "pu": 199, "b": [], "a": []})
    with pytest.raises(OrderBookDesync):
        ob.apply_snapshot({"lastUpdateId": 150, "bids": [], "asks": []})


def test_orderbook_walk_and_metrics():
    ob = OrderBook("X")
    ob.apply_snapshot({"lastUpdateId": 1, "bids": [["99", "1"], ["98", "1"]], "asks": [["101", "1"], ["102", "1"]]})
    assert ob.walk("BUY", 1.5) == pytest.approx((101 + 0.5 * 102) / 1.5)
    assert ob.walk("SELL", 5) is None
    assert ob.spread_bps() == pytest.approx(200.0)
    assert ob.imbalance() == pytest.approx((197 - 203) / 400)


# ------------------------------------------------------------ dispatcher


def test_stream_urls_split_public_market():
    assert public_streams(["BTCUSDT"]) == ["btcusdt@depth@100ms"]
    ms = market_streams(["BTCUSDT"])
    assert "btcusdt@aggTrade" in ms and "btcusdt@markPrice@1s" in ms and "btcusdt@kline_15m" in ms
    assert combined_url("wss://fstream.binance.com/market/", ["a", "b"]) == \
        "wss://fstream.binance.com/market/stream?streams=a/b"


def test_dispatcher_routes_messages():
    st = MarketState("BTCUSDT")
    closed = []
    d = MarketDispatcher({"BTCUSDT": st}, on_kline_closed=lambda s, i, c: closed.append((s, i, c)))
    msgs = [
        {"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "p": "60000", "q": "2", "m": True, "T": 1}},
        {"stream": "btcusdt@markPrice@1s", "data": {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "60010", "i": "60005",
                                                    "r": "0.0001", "T": 1700000000000, "E": 2}},
        {"stream": "btcusdt@kline_1m", "data": {"e": "kline", "s": "BTCUSDT", "k": {
            "t": 0, "i": "1m", "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "q": "15", "V": "7", "x": True}}},
        {"stream": "ethusdt@aggTrade", "data": {"e": "aggTrade", "s": "ETHUSDT"}},  # no suscrito: se ignora
    ]
    for m in msgs:
        asyncio.run(d.handle(json.dumps(m)))
    assert st.last_price == 60000 and st.cvd_live == -2
    assert st.mark_price == 60010 and st.funding_rate == 0.0001
    assert closed and closed[0][2].delta == pytest.approx(4.0)


def test_candle_series_dedup():
    st = MarketState("X")
    s = st.candles["1m"]
    c = Candle(60_000, 1, 1, 1, 1, 1, 1, 0.5)
    assert s.add_closed(c) and not s.add_closed(c)
    s.add_closed(Candle(240_000, 1, 1, 1, 1, 1, 1, 0.5))
    assert s.gaps(60_000) == 2


# ------------------------------------------------------------ parsers


def test_parse_derivatives():
    rows = [{"symbol": "BTCUSDT", "sumOpenInterestValue": "123.5", "timestamp": 1700000000000},
            {"symbol": "BTCUSDT", "sumOpenInterestValue": "", "timestamp": 1700000300000}]
    assert parse_series(rows, "sumOpenInterestValue")[0][1] == 123.5
    assert len(parse_series(rows, "sumOpenInterestValue")) == 1
    fh = parse_funding_history([{"fundingTime": 1700000000000, "fundingRate": "0.0001"}])
    assert fh[0][1] == 0.0001


def test_parse_dvol_and_realized():
    resp = {"result": {"data": [[1700000000000, 50, 52, 49, 51.5]], "continuation": None}}
    assert parse_dvol(resp)[0][1] == 51.5
    rng = np.random.default_rng(0)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 2000)))
    rv = realized_vol(closes, 1)
    assert rv == pytest.approx(0.001 * np.sqrt(525600), rel=0.1)
    assert realized_vol(np.array([1.0, 2.0]), 1) is None


def test_parse_hyperliquid_and_normalize_funding():
    resp = [{"universe": [{"name": "BTC"}, {"name": "ETH"}]},
            [{"funding": "0.0000125", "openInterest": "100", "markPx": "60000"},
             {"funding": "bad", "openInterest": "1", "markPx": "1"}]]
    p = parse_meta_and_ctxs(resp)
    assert p["BTC"]["funding_1h"] == 0.0000125 and "ETH" not in p
    assert hourly_funding(0.0001, 8) == pytest.approx(0.0000125)


def test_parse_fng_onchain():
    assert parse_fng({"data": [{"value": "25", "timestamp": "1700000000"}]})[0][1] == 25
    rows = [{"date": "1700000000", "totalCirculatingUSD": {"peggedUSD": 1.5e11}}, {"date": "1700086400"}]
    assert parse_stablecoin_chart(rows) == [(datetime.fromtimestamp(1700000000, tz=UTC), 1.5e11)]
    assert parse_fee_stats({"ledger_capacity_usage": "0.42"}) == 0.42
    assert parse_fee_stats({}) is None


# ------------------------------------------------------------ macro


def test_forexfactory_filters_usd_high():
    rows = [{"title": "CPI m/m", "country": "USD", "date": "2026-10-15T08:30:00-04:00", "impact": "High"},
            {"title": "GDP", "country": "EUR", "date": "2026-10-15T05:00:00-04:00", "impact": "High"},
            {"title": "Retail", "country": "USD", "date": "2026-10-15T08:30:00-04:00", "impact": "Medium"}]
    ev = parse_forexfactory(rows)
    assert len(ev) == 1 and ev[0].ts == datetime(2026, 10, 15, 12, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "offset_min,blocked",
    [(-31, False), (-30, True), (0, True), (60, True), (61, False)],
)
def test_macro_blackout_edges(offset_min, blocked):
    ev = MacroEvent(title="CPI", ts=NOW, source="t")
    now = NOW + timedelta(minutes=offset_min)
    assert (blocking_event(now, [ev], 30, 60) is not None) is blocked


# ------------------------------------------------------------ news

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin ETF sees record inflows</title><link>https://x/1</link><pubDate>Thu, 15 Oct 2026 11:50:00 GMT</pubDate></item>
<item><title>Bitcoin ETF sees record inflows today</title><link>https://x/2</link><pubDate>Thu, 15 Oct 2026 11:55:00 GMT</pubDate></item>
<item><title>Solana validator update</title><link>https://x/3</link><pubDate>Thu, 15 Oct 2026 11:40:00 GMT</pubDate></item>
<item><title>Old XRP news</title><link>https://x/4</link><pubDate>Thu, 15 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Celebrity buys a house</title><link>https://x/5</link><pubDate>Thu, 15 Oct 2026 11:59:00 GMT</pubDate></item>
</channel></rss>"""


def test_rss_scoring_dedupe_age():
    items = parse_rss(RSS, "https://www.coindesk.com/arc/outboundfeeds/rss/")
    assert len(items) == 5 and items[0].source == "coindesk"
    top = score_headlines(items, NOW, {"BTCUSDT": ["bitcoin"], "SOLUSDT": ["solana"], "XRPUSDT": ["xrp"]},
                          ["coindesk"], ["etf", "hack"], max_age_min=30, similarity=0.8, top_n=8)
    titles = [h.title for h in top]
    assert len([t for t in titles if "ETF" in t]) == 1           # duplicado eliminado
    assert "Old XRP news" not in titles                          # > 30 min
    assert "Celebrity buys a house" not in titles                # irrelevante
    assert top[0].high_impact and top[0].symbols == ["BTCUSDT"]


def test_news_top_n_limit():
    items = [Headline(title=f"bitcoin hack number {i} {'x' * i}", source="coindesk", published=NOW) for i in range(20)]
    assert len(score_headlines(items, NOW, {"BTCUSDT": ["bitcoin"]}, [], ["hack"], 30, 0.99, top_n=8)) == 8


def test_parse_cryptopanic():
    resp = {"results": [{"title": "ETH upgrade", "published_at": "2026-10-15T11:58:00Z",
                         "source": {"domain": "theblock.co"}, "url": "u"}]}
    h = parse_cryptopanic(resp)[0]
    assert h.source == "theblock" and h.published.tzinfo is not None


# ------------------------------------------------------------ infra


def test_rate_limiter_window():
    rl = RateLimiter({"k": (3, 10.0)})
    assert rl.try_acquire("k", 2, now=0) and rl.try_acquire("k", 1, now=1)
    assert not rl.try_acquire("k", 1, now=5)
    assert rl.try_acquire("k", 1, now=10.5)


def test_recorder_roundtrip_and_parquet(tmp_path):
    rec = Recorder(tmp_path, flush_every_s=999)
    ts = datetime(2026, 10, 15, tzinfo=UTC)
    for i in range(3):
        rec.record("btcusdt@aggTrade", {"p": str(i), "nested": {"a": i}}, ts)
    rec.flush()
    rows = rec.read("btcusdt@aggTrade", "2026-10-15")
    assert [r["data"]["p"] for r in rows] == ["0", "1", "2"]
    out = rec.compact_to_parquet("btcusdt@aggTrade", "2026-10-15")
    assert out and out.exists()
    assert len(rec.read("btcusdt@aggTrade", "2026-10-15")) == 3  # se lee del .gz
    assert completeness(100, 99) == pytest.approx(0.01)


def test_raw_snapshot_unknown_and_stale():
    st = MarketState("SOLUSDT")
    st.on_agg_trade({"p": "150", "q": "1", "m": False, "T": dt_to_ms(NOW - timedelta(seconds=2))})
    st.on_mark_price({"p": "150.1", "i": "150", "r": "0.0008", "T": 0, "E": dt_to_ms(NOW - timedelta(seconds=120))})
    store = FeedStore()
    store.set("deriv.oi.SOLUSDT", 1e9, "binance_rest", NOW - timedelta(minutes=5))
    snap = build_raw_snapshot(st, store, NOW, TTL)
    assert snap.get("market.price").is_ok
    assert snap.get("market.mark").status is DataStatus.STALE        # 120 s > 5 s
    assert snap.get("funding.rate_1h").status is DataStatus.STALE    # 120 s > 60 s
    assert snap.get("market.spread_bps").status is DataStatus.UNKNOWN  # libro sin sincronizar
    assert snap.get("vol.dvol").status is DataStatus.UNKNOWN          # DVOL no existe para SOL
    assert snap.get("deriv.oi").is_ok
    assert data_quality(snap) == 0.0
