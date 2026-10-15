"""Construye el Snapshot crudo por moneda con estados ok/stale/unknown.

Prefijos de campo -> TTL de config.data.ttl_s:
market, funding, deriv(open_interest), vol(dvol), dex, sent(sentiment), onchain.
"""

from __future__ import annotations

from datetime import datetime

from bot.core.clock import ms_to_dt
from bot.core.models import DataPoint, Snapshot
from bot.data_engine.market_state import MarketState
from bot.data_engine.store import FeedStore

PREFIX_TTL_KEY = {
    "market": "market",
    "funding": "funding",
    "deriv": "open_interest",
    "vol": "dvol",
    "dex": "dex",
    "sent": "sentiment",
    "onchain": "onchain",
    "news": "news",
    "macro": "macro",
}

REQUIRED_FIELDS = (
    "market.price", "market.mark", "market.spread_bps", "market.depth_notional",
    "funding.rate_1h", "deriv.oi",
)


def ttl_by_prefix(ttl_s: dict[str, float]) -> dict[str, float]:
    return {p: ttl_s[k] for p, k in PREFIX_TTL_KEY.items() if k in ttl_s}


def _dp(value: float | None, source: str, ts: datetime | None) -> DataPoint:
    if value is None or ts is None:
        return DataPoint.unknown(source)
    return DataPoint.ok(value, source, ts)


def build_raw_snapshot(state: MarketState, store: FeedStore, now: datetime, ttl_s: dict[str, float]) -> Snapshot:
    sym = state.symbol
    f: dict[str, DataPoint] = {}
    trade_ts = ms_to_dt(state.last_trade_ms) if state.last_trade_ms else None
    mark_ts = ms_to_dt(state.mark_ms) if state.mark_ms else None
    book_ts = ms_to_dt(state.book.event_ts_ms) if state.book.synced and state.book.event_ts_ms else None

    f["market.price"] = _dp(state.last_price, "binance_ws", trade_ts)
    f["market.mark"] = _dp(state.mark_price, "binance_ws", mark_ts)
    f["market.spread_bps"] = _dp(state.book.spread_bps() if book_ts else None, "binance_ws", book_ts)
    f["market.book_imbalance"] = _dp(state.book.imbalance(10) if book_ts else None, "binance_ws", book_ts)
    f["market.depth_notional"] = _dp(state.book.depth_notional(10) if book_ts else None, "binance_ws", book_ts)

    f["funding.rate"] = _dp(state.funding_rate, "binance_ws", mark_ts)
    rate_1h = state.funding_rate / state.funding_interval_hours if state.funding_rate is not None else None
    f["funding.rate_1h"] = _dp(rate_1h, "binance_ws", mark_ts)
    f["funding.next_ms"] = _dp(float(state.next_funding_ms) if state.next_funding_ms else None, "binance_ws", mark_ts)

    for base in ("deriv.oi", "deriv.top_ls_ratio", "deriv.global_ls_ratio", "deriv.taker_ratio"):
        f[base] = store.get(f"{base}.{sym}")

    coin = sym.removesuffix("USDT")
    f["vol.dvol"] = store.get(f"vol.dvol.{coin}") if coin in ("BTC", "ETH") else DataPoint.unknown("deribit_n/a")
    f["vol.dvol_btc"] = store.get("vol.dvol.BTC")

    f["dex.funding_1h"] = store.get(f"dex.funding_1h.{sym}")
    # Posicionamiento de traders destacados de Hyperliquid (contexto; ver data_engine/top_traders.py)
    for base in ("dex.smart_ratio", "dex.smart_long_n", "dex.smart_short_n", "dex.smart_net_usd"):
        f[base] = store.get(f"{base}.{sym}")
    f["sent.fng"] = store.get("sent.fng")
    f["onchain.stablecoin_supply"] = store.get("onchain.stablecoin_supply")
    f["onchain.exchange_netflow"] = store.get("onchain.exchange_netflow")
    if sym == "XLMUSDT":
        f["onchain.xlm_ledger_usage"] = store.get("onchain.xlm_ledger_usage")

    snap = Snapshot(symbol=sym, ts=now, fields=f)
    return snap.refreshed(now, ttl_by_prefix(ttl_s))


def data_quality(snap: Snapshot, optional: tuple[str, ...] = ("vol.dvol_btc", "dex.funding_1h", "sent.fng",
                                                               "deriv.top_ls_ratio", "deriv.taker_ratio")) -> float:
    """1.0 si requeridos y opcionales están ok; 0 si falta algún requerido."""
    if snap.missing(list(REQUIRED_FIELDS)):
        return 0.0
    ok = sum(1 for k in optional if snap.get(k).is_ok)
    return round(0.6 + 0.4 * ok / len(optional), 4)
