"""Traders destacados de Hyperliquid: qué posiciones tienen y qué abren o cierran.

En Hyperliquid todas las cuentas son públicas. Cada `refresh_hours` se baja el
ranking (stats-data.hyperliquid.xyz/Mainnet/leaderboard, el que usa la web
oficial) y se eligen las cuentas que ganan de forma sostenida y operan de forma
direccional. Cada `poll_s` se lee su estado con el endpoint oficial
POST /info {"type": "clearinghouseState", "user": ...}.

Con eso se publican, por moneda, campos `dex.smart_*` (contexto para la IA y
para el aprendizaje) y una lista de movimientos (abrió, cerró, aumentó, redujo,
giró) para el panel. NO se copia ninguna operación: un buen historial pasado
no garantiza nada y una posición puede ser cobertura de otra cuenta.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bot.core.clock import utc_now
from bot.core.config import TopTradersConfig
from bot.core.logging_setup import get_logger
from bot.data_engine.dex import HL_COINS
from bot.data_engine.http import HttpClient
from bot.data_engine.store import FeedStore

log = get_logger("bot.top_traders")

SRC = "hyperliquid_top"
MIN_POSITION_USD = 1_000.0     # por debajo es ruido
CHANGE_FRACTION = 0.25         # aumentar/reducir solo cuenta si cambia ≥ 25 % del tamaño


@dataclass(frozen=True)
class Trader:
    address: str
    account_value: float
    pnl_month: float
    roi_month: float
    pnl_all_time: float

    @property
    def short(self) -> str:
        return f"{self.address[:6]}…{self.address[-4:]}"


def _window(row: dict, name: str) -> dict:
    return next((w[1] for w in row.get("windowPerformances", []) if w and w[0] == name), {})


def select_traders(rows: list[dict], cfg: TopTradersConfig) -> list[Trader]:
    """Ganadores sostenidos y direccionales: excluye bóvedas/cuentas del protocolo (sin volumen)
    y creadores de mercado (volumen mensual desproporcionado frente a su capital)."""
    out: list[Trader] = []
    for r in rows:
        try:
            av = float(r["accountValue"])
            m, a, w = _window(r, "month"), _window(r, "allTime"), _window(r, "week")
            pm, roi_m, pa = float(m["pnl"]), float(m["roi"]), float(a["pnl"])
            turnover = float(m["vlm"]) / av if av > 0 else 0.0
            pw = float(w.get("pnl", 0))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if not cfg.min_account_usd <= av <= cfg.max_account_usd:
            continue
        if pm <= 0 or pa <= 0 or pw < 0 or not 0 < roi_m <= cfg.max_monthly_roi:
            continue
        if not cfg.min_monthly_turnover <= turnover <= cfg.max_monthly_turnover:
            continue
        out.append(Trader(r["ethAddress"], av, pm, roi_m, pa))
    out.sort(key=lambda t: t.pnl_month, reverse=True)
    return out[: cfg.count]


def parse_positions(state: dict) -> dict[str, dict[str, float]]:
    """{coin: {"size": con signo, "usd": nocional, "entry": precio}} de clearinghouseState."""
    out: dict[str, dict[str, float]] = {}
    for ap in state.get("assetPositions", []) or []:
        p = ap.get("position", ap)
        try:
            size = float(p["szi"])
            usd = abs(float(p["positionValue"]))
            entry = float(p.get("entryPx") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if size != 0:
            out[p["coin"]] = {"size": size, "usd": usd, "entry": entry}
    return out


def diff_positions(trader: Trader, before: dict[str, dict], after: dict[str, dict], coins: set[str],
                   now: datetime, marks: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Movimientos entre dos lecturas de la misma cuenta."""
    events = []
    for coin in coins:
        b, a = before.get(coin), after.get(coin)
        bs, as_ = (b or {}).get("size", 0.0), (a or {}).get("size", 0.0)
        if bs == as_:
            continue
        usd = (a or b or {}).get("usd", 0.0)
        if max(abs((a or {}).get("usd", 0.0)), abs((b or {}).get("usd", 0.0))) < MIN_POSITION_USD:
            continue
        side = "LONG" if as_ > 0 else "SHORT"
        if bs == 0:
            action = f"abrió {side.lower()}"
        elif as_ == 0:
            action = f"cerró {'long' if bs > 0 else 'short'}"
            usd = b["usd"]  # type: ignore[index]
        elif (bs > 0) != (as_ > 0):
            action = f"giró a {side.lower()}"
        elif abs(as_ - bs) < CHANGE_FRACTION * abs(bs):
            continue
        elif abs(as_) > abs(bs):
            action = f"aumentó {side.lower()}"
        else:
            action = f"redujo {side.lower()}"
        px = (marks or {}).get(coin) or (a or b or {}).get("entry")
        events.append({"ts": now.isoformat(), "trader": trader.short, "address": trader.address, "coin": coin,
                       "action": action, "usd": round(usd, 2), "price": px,
                       "trader_pnl_month": round(trader.pnl_month, 2)})
    return events


def aggregate(positions: dict[str, dict[str, dict]], coin: str) -> dict[str, float]:
    long_usd = short_usd = 0.0
    long_n = short_n = 0
    for pos in positions.values():
        p = pos.get(coin)
        if not p or p["usd"] < MIN_POSITION_USD:
            continue
        if p["size"] > 0:
            long_usd += p["usd"]
            long_n += 1
        else:
            short_usd += p["usd"]
            short_n += 1
    total = long_usd + short_usd
    return {"ratio": (long_usd - short_usd) / total if total else 0.0, "long_n": long_n, "short_n": short_n,
            "net_usd": long_usd - short_usd}


class TopTradersPoller:
    def __init__(self, cfg: TopTradersConfig, info_url: str, symbols: list[str], store: FeedStore,
                 http: HttpClient, on_event: Callable[[dict], None] | None = None):
        self.cfg = cfg
        self.info_url = info_url
        self.symbols = symbols
        self.coins = {HL_COINS[s]: s for s in symbols if s in HL_COINS}
        self.store = store
        self.http = http
        self.on_event = on_event
        self.traders: list[Trader] = []
        self.positions: dict[str, dict[str, dict]] = {}
        self.events: deque[dict] = deque(maxlen=200)
        self.leaderboard_at: datetime | None = None

    async def refresh_leaderboard(self) -> None:
        try:
            data = await self.http.get_json(self.cfg.leaderboard_url, limit_key="hyperliquid", timeout=120.0)
            traders = select_traders(data.get("leaderboardRows", []), self.cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("ranking de hyperliquid: fallo", extra={"err": repr(exc)})
            return
        if traders:
            keep = {t.address for t in traders}
            self.positions = {a: p for a, p in self.positions.items() if a in keep}
            self.traders = traders
            self.leaderboard_at = utc_now()
            log.info("traders destacados elegidos", extra={"n": len(traders)})

    async def poll_once(self) -> None:
        now = utc_now()
        if self.leaderboard_at is None or (now - self.leaderboard_at).total_seconds() > self.cfg.refresh_hours * 3600:
            await self.refresh_leaderboard()
        if not self.traders:
            for sym in self.symbols:
                self.store.mark_unknown(f"dex.smart_ratio.{sym}", SRC)
            return
        read = 0
        for t in self.traders:
            try:
                state = await self.http.post_json(self.info_url, limit_key="hyperliquid", weight=2,
                                                  json={"type": "clearinghouseState", "user": t.address})
                pos = parse_positions(state)
            except Exception as exc:  # noqa: BLE001
                log.debug("estado de trader: fallo", extra={"err": repr(exc), "trader": t.short})
                continue
            read += 1
            if t.address in self.positions:  # en la primera lectura no hay "antes": no son movimientos
                for ev in diff_positions(t, self.positions[t.address], pos, set(self.coins), now):
                    ev["symbol"] = self.coins[ev["coin"]]
                    self.events.appendleft(ev)
                    if self.on_event:
                        self.on_event(ev)
            self.positions[t.address] = pos
        # Con menos de la mitad de las cuentas leídas el agregado no es representativo: desconocido
        if read < max(1, len(self.traders) // 2):
            for sym in self.symbols:
                self.store.mark_unknown(f"dex.smart_ratio.{sym}", SRC)
            return
        for coin, sym in self.coins.items():
            agg = aggregate(self.positions, coin)
            self.store.set(f"dex.smart_ratio.{sym}", agg["ratio"], SRC, now)
            self.store.set(f"dex.smart_long_n.{sym}", float(agg["long_n"]), SRC, now)
            self.store.set(f"dex.smart_short_n.{sym}", float(agg["short_n"]), SRC, now)
            self.store.set(f"dex.smart_net_usd.{sym}", agg["net_usd"], SRC, now)

    def summary(self) -> dict[str, Any]:
        """Para el panel."""
        by_symbol = {}
        for coin, sym in self.coins.items():
            agg = aggregate(self.positions, coin)
            holders = [{"trader": t.short, "side": "LONG" if p["size"] > 0 else "SHORT", "usd": round(p["usd"], 2),
                        "entry": p["entry"], "trader_pnl_month": round(t.pnl_month, 2)}
                       for t in self.traders for c, p in self.positions.get(t.address, {}).items()
                       if c == coin and p["usd"] >= MIN_POSITION_USD]
            holders.sort(key=lambda h: -h["usd"])
            by_symbol[sym] = {**agg, "holders": holders[:8]}
        return {"n_traders": len(self.traders), "n_read": len(self.positions),
                "leaderboard_at": self.leaderboard_at.isoformat() if self.leaderboard_at else None,
                "by_symbol": by_symbol, "events": list(self.events)[:40]}

    async def run(self, every_s: float) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("traders destacados: fallo", extra={"err": repr(exc)})
            await asyncio.sleep(every_s)
