"""Traders destacados: selección del ranking, posiciones, movimientos y agregado por moneda (sin red)."""
import asyncio
from datetime import UTC, datetime

from bot.core.config import TopTradersConfig
from bot.data_engine.store import FeedStore
from bot.data_engine.top_traders import (Trader, TopTradersPoller, aggregate, diff_positions, parse_positions,
                                         select_traders)

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def row(addr, av, pm, roi, pa, vlm, pw=1.0):
    return {"ethAddress": addr, "accountValue": str(av), "windowPerformances": [
        ["week", {"pnl": str(pw), "roi": "0.01", "vlm": "1"}],
        ["month", {"pnl": str(pm), "roi": str(roi), "vlm": str(vlm)}],
        ["allTime", {"pnl": str(pa), "roi": "1", "vlm": "1"}]]}


def ch_state(*positions):
    return {"assetPositions": [{"type": "oneWay", "position": {"coin": c, "szi": str(sz), "entryPx": str(px),
                                                               "positionValue": str(abs(sz) * px)}}
                               for c, sz, px in positions]}


def test_select_keeps_sustained_directional_winners():
    cfg = TopTradersConfig(count=2)
    rows = [row("0xgood1", 500_000, 90_000, 0.2, 300_000, 5_000_000),
            row("0xgood2", 2_000_000, 150_000, 0.1, 900_000, 40_000_000),
            row("0xgood3", 300_000, 10_000, 0.05, 50_000, 2_000_000),
            row("0xvault", 40_000_000, 5_000_000, 0.1, 9_000_000, 0),             # sin volumen: bóveda
            row("0xmm", 1_000_000, 200_000, 0.2, 800_000, 900_000_000),           # creador de mercado
            row("0xloser", 800_000, -50_000, -0.05, 100_000, 5_000_000),          # pierde este mes
            row("0xluck", 150_000, 900_000, 6.0, 900_000, 5_000_000),             # +600 % al mes
            row("0xsmall", 5_000, 3_000, 0.6, 3_000, 50_000),                     # cuenta pequeña
            row("0xweek", 900_000, 100_000, 0.1, 400_000, 9_000_000, pw=-5),      # semana en pérdida
            {"ethAddress": "0xbroken"}]
    sel = select_traders(rows, cfg)
    assert [t.address for t in sel] == ["0xgood2", "0xgood1"]


def test_parse_and_diff_positions():
    t = Trader("0xabcdef0123456789", 1e6, 1e5, 0.1, 1e6)
    a = parse_positions(ch_state(("BTC", 0.5, 60_000), ("ETH", -10, 2_500), ("SOL", 1, 150)))
    assert a["BTC"] == {"size": 0.5, "usd": 30_000, "entry": 60_000} and a["ETH"]["size"] == -10
    b = parse_positions(ch_state(("BTC", 0.55, 60_000), ("ETH", 4, 2_500), ("XRP", 50_000, 0.5)))
    ev = {e["coin"]: e["action"] for e in diff_positions(t, a, b, {"BTC", "ETH", "SOL", "XRP"}, NOW)}
    # BTC +10 % no es movimiento; ETH gira; SOL (150 $) es ruido; XRP abre
    assert ev == {"ETH": "giró a long", "XRP": "abrió long"}
    c = parse_positions(ch_state(("ETH", 1, 2_500)))
    ev = {e["coin"]: e["action"] for e in diff_positions(t, b, c, {"BTC", "ETH", "XRP"}, NOW)}
    assert ev == {"BTC": "cerró long", "ETH": "redujo long", "XRP": "cerró long"}


def test_aggregate_by_notional():
    pos = {"a": parse_positions(ch_state(("BTC", 1, 60_000))),
           "b": parse_positions(ch_state(("BTC", -0.5, 60_000))),
           "c": parse_positions(ch_state(("BTC", 0.001, 60_000)))}  # 60 $: ruido
    agg = aggregate(pos, "BTC")
    assert agg["long_n"] == 1 and agg["short_n"] == 1 and agg["net_usd"] == 30_000
    assert abs(agg["ratio"] - 1 / 3) < 1e-9
    assert aggregate(pos, "XLM") == {"ratio": 0.0, "long_n": 0, "short_n": 0, "net_usd": 0.0}


class FakeHttp:
    def __init__(self, rows, states, fail=()):
        self.rows, self.states, self.fail, self.calls = rows, states, set(fail), 0

    async def get_json(self, url, **kw):
        return {"leaderboardRows": self.rows}

    async def post_json(self, url, json, **kw):
        self.calls += 1
        if json["user"] in self.fail:
            raise RuntimeError("caída")
        return self.states[json["user"]]


def test_poller_publishes_fields_and_events():
    rows = [row("0xaaaa000000000001", 500_000, 90_000, 0.2, 300_000, 5_000_000),
            row("0xbbbb000000000002", 600_000, 80_000, 0.2, 300_000, 5_000_000)]
    states = {"0xaaaa000000000001": ch_state(("BTC", 1, 60_000)),
              "0xbbbb000000000002": ch_state(("BTC", -0.25, 60_000), ("DOGE", -100_000, 0.12))}
    store, seen = FeedStore(), []
    p = TopTradersPoller(TopTradersConfig(), "info", ["BTCUSDT", "DOGEUSDT", "XLMUSDT"], store,
                         FakeHttp(rows, states), on_event=seen.append)
    asyncio.run(p.poll_once())
    assert store.get("dex.smart_ratio.BTCUSDT").value == 0.6 and store.get("dex.smart_short_n.DOGEUSDT").value == 1
    assert store.get("dex.smart_long_n.XLMUSDT").value == 0 and seen == []  # primera lectura: sin movimientos
    states["0xaaaa000000000001"] = ch_state()
    asyncio.run(p.poll_once())
    assert [(e["symbol"], e["action"]) for e in seen] == [("BTCUSDT", "cerró long")]
    assert store.get("dex.smart_ratio.BTCUSDT").value == -1.0
    s = p.summary()
    assert s["n_traders"] == 2 and s["events"][0]["coin"] == "BTC"
    assert s["by_symbol"]["DOGEUSDT"]["holders"][0]["side"] == "SHORT"


def test_poller_marks_unknown_when_most_reads_fail():
    rows = [row(f"0x{i:016x}", 500_000, 90_000, 0.2, 300_000, 5_000_000) for i in range(4)]
    states = {r["ethAddress"]: ch_state(("BTC", 1, 60_000)) for r in rows}
    store = FeedStore()
    p = TopTradersPoller(TopTradersConfig(), "info", ["BTCUSDT"], store,
                         FakeHttp(rows, states, fail=[r["ethAddress"] for r in rows[:3]]))
    asyncio.run(p.poll_once())
    assert not store.get("dex.smart_ratio.BTCUSDT").is_ok


def test_smart_fields_reach_snapshot_and_ai_prompt():
    from bot.data_engine.market_state import MarketState
    from bot.data_engine.normalizer import build_raw_snapshot
    from bot.ai_analyst.prompt_builder import SYSTEM_PROMPT, summarize_fields
    store = FeedStore()
    store.set("dex.smart_ratio.BTCUSDT", 0.4, "hyperliquid_top", NOW)
    snap = build_raw_snapshot(MarketState("BTCUSDT"), store, NOW, {"dex": 300})
    assert snap.get("dex.smart_ratio").is_ok and not snap.get("dex.smart_long_n").is_ok
    assert summarize_fields(snap.fields)["dex.smart_ratio"] == "0.4" and "smart_ratio" in SYSTEM_PROMPT
