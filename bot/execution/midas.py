"""Cuenta MIDAS: opera con dinero ficticio y apalancamiento alto (x5–x25), aparte de la demo normal.

Autorizada por el usuario el 2026-10-20 ("Autorizo MIDAS con dinero ficticio"). Reglas propias:
- Solo existe en modo demo (paper). Construirla en cualquier otro modo es un error: con dinero real no se carga.
- Usa las señales de las estrategias del bot. Con alcance "todas" deja de lado la disciplina (filtros, IA, techo
  del día); con "aprobadas" solo entra cuando la IA aprobó la señal.
- Cada entrada pone `margin` con `leverage` (margen aislado). Mantiene el stop y el objetivo del bot; si el precio llega
  antes a la liquidación (≈ 1/apalancamiento − margen de mantenimiento) se pierde todo el margen de esa entrada.
- Tiene su propio saldo y su propio archivo; nunca toca la cuenta, el riesgo ni las posiciones de la demo normal.
- Si el saldo libre no alcanza para un margen, deja de abrir (la cuenta "se vació").

Los resultados son información para observar y aprender: la reflexión semanal los recibe, pero el aprendizaje nunca
cambia por ello el riesgo, el apalancamiento ni los stops del bot (CLAUDE.md §1.7).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from bot.core.config import HARD_MIDAS_LEVERAGES, Fees, MidasConfig, Mode
from bot.core.fsutil import atomic_write_text, read_with_backup
from bot.core.logging_setup import get_logger
from bot.core.models import Signal

log = get_logger("bot.midas")
FILENAME = "midas_cuenta.json"
CONTROL_FILENAME = "midas_control.json"
MAX_CLOSED = 500


class MidasControl(BaseModel):
    """Lo que el usuario cambia desde el panel (reemplaza a config.yaml mientras exista el archivo)."""

    enabled: bool
    leverage: int
    margin: float = Field(gt=0, le=100_000)
    scope: str = "todas"

    @field_validator("leverage")
    @classmethod
    def _lev(cls, v: int) -> int:
        if v not in HARD_MIDAS_LEVERAGES:
            raise ValueError(f"apalancamiento debe ser uno de {HARD_MIDAS_LEVERAGES}")
        return v

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        if v not in ("todas", "aprobadas"):
            raise ValueError("alcance: todas | aprobadas")
        return v


def liq_price(entry: float, side: str, leverage: int, mmr: float) -> float:
    dist = max(1.0 / leverage - mmr, 0.0)
    return entry * (1 - dist) if side == "LONG" else entry * (1 + dist)


def load_control(path: Path, cfg: MidasConfig) -> MidasControl:
    default = MidasControl(enabled=cfg.enabled, leverage=cfg.leverage, margin=cfg.margin_per_trade, scope=cfg.scope)
    if not path.exists():
        return default
    try:
        return read_with_backup(path, MidasControl.model_validate_json)
    except Exception as exc:  # noqa: BLE001  (ilegible = apagada: ante la duda, no abrir)
        log.error("midas_control.json ilegible: MIDAS no abre nada", extra={"err": repr(exc)})
        return default.model_copy(update={"enabled": False})


def save_control(path: Path, ctl: MidasControl) -> None:
    atomic_write_text(path, ctl.model_dump_json(indent=1), backup=True)


class MidasAccount:
    def __init__(self, reports_dir: Path, cfg: MidasConfig, fees: Fees, mode: Mode):
        if mode is not Mode.PAPER:
            raise RuntimeError("MIDAS solo existe en modo demo (paper): con dinero real no se carga")
        self.path, self.control_path = reports_dir / FILENAME, reports_dir / CONTROL_FILENAME
        self.cfg, self.fees = cfg, fees
        self._ctl: tuple[float, MidasControl] | None = None
        self.balance = cfg.initial_capital
        self.positions: dict[str, dict[str, Any]] = {}
        self.closed: list[dict[str, Any]] = []
        self.curve: list[list[Any]] = []
        self.started_at: str | None = None
        self._load()

    # ------------------------------------------------------------ estado en disco
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = read_with_backup(self.path, json.loads)
            self.balance = float(data["balance"])
            self.positions = {p["symbol"]: p for p in data.get("positions", [])}
            self.closed = data.get("closed", [])[-MAX_CLOSED:]
            self.curve = data.get("curve", [])[-2000:]
            self.started_at = data.get("started_at")
        except Exception as exc:  # noqa: BLE001
            log.error("cuenta MIDAS ilegible: se empieza de nuevo", extra={"err": repr(exc)})

    def save(self) -> None:
        try:
            atomic_write_text(self.path, json.dumps({
                "balance": self.balance, "initial_capital": self.cfg.initial_capital, "started_at": self.started_at,
                "positions": list(self.positions.values()), "closed": self.closed[-MAX_CLOSED:],
                "curve": self.curve[-2000:]}, default=str), backup=True)
        except Exception as exc:  # noqa: BLE001
            log.error("no se pudo guardar la cuenta MIDAS", extra={"err": repr(exc)})

    def control(self) -> MidasControl:
        try:
            mtime = self.control_path.stat().st_mtime if self.control_path.exists() else 0.0
        except OSError:
            mtime = 0.0
        if self._ctl is None or self._ctl[0] != mtime:
            self._ctl = (mtime, load_control(self.control_path, self.cfg))
        return self._ctl[1]

    # ------------------------------------------------------------ operar
    def used_margin(self) -> float:
        return sum(p["margin"] for p in self.positions.values())

    def consider(self, sig: Signal, price: float | None, now: datetime, approved: bool) -> dict | None:
        """Abre una posición MIDAS con esta señal si toca. Devuelve la posición abierta o None."""
        ctl = self.control()
        if not ctl.enabled or price is None or price <= 0:
            return None
        if ctl.scope == "aprobadas" and not approved:
            return None
        if sig.symbol in self.positions or len(self.positions) >= self.cfg.max_positions:
            return None
        margin, lev = ctl.margin, ctl.leverage
        if self.balance - self.used_margin() < margin:
            return None  # sin saldo libre: la cuenta se vació
        notional = margin * lev
        fee = notional * self.fees.taker  # entra a mercado: es una cuenta de observación, no busca maker
        pos = {"symbol": sig.symbol, "side": sig.side.value, "strategy": sig.strategy, "signal_id": sig.id,
               "approved": approved, "entry": price, "qty": notional / price, "margin": margin, "leverage": lev,
               "notional": notional, "stop": sig.stop, "take_profit": sig.take_profit,
               "liq": liq_price(price, sig.side.value, lev, self.cfg.maint_margin), "opened_at": now.isoformat(),
               "max_hold_hours": sig.max_hold_hours, "fee_in": fee}
        self.balance -= fee
        self.started_at = self.started_at or now.isoformat()
        self.positions[sig.symbol] = pos
        self.save()
        return pos

    def update(self, prices: dict[str, float | None], now: datetime) -> list[dict]:
        """Revisa stop, objetivo, liquidación y tiempo máximo con el precio actual. Devuelve las cerradas."""
        out = []
        for sym, p in list(self.positions.items()):
            px = prices.get(sym)
            if not px:
                continue
            long = p["side"] == "LONG"
            reason, exit_px = None, px
            if (px <= p["liq"]) if long else (px >= p["liq"]):
                reason, exit_px = "liquidada", p["liq"]
            elif (px <= p["stop"]) if long else (px >= p["stop"]):
                reason, exit_px = "stop", p["stop"]
            elif p.get("take_profit") and ((px >= p["take_profit"]) if long else (px <= p["take_profit"])):
                reason, exit_px = "take_profit", p["take_profit"]
            elif now >= datetime.fromisoformat(p["opened_at"]) + timedelta(hours=p["max_hold_hours"]):
                reason = "tiempo"
            if reason is None:
                continue
            if reason == "liquidada":
                pnl, fee_out = -p["margin"], 0.0
            else:
                fee_out = abs(exit_px * p["qty"]) * self.fees.taker
                pnl = max((1 if long else -1) * (exit_px - p["entry"]) * p["qty"] - fee_out, -p["margin"])
            self.balance += pnl
            trade = {**p, "exit": exit_px, "closed_at": now.isoformat(), "reason": reason, "pnl": round(pnl, 4),
                     "pnl_total": round(pnl - p["fee_in"], 4), "ret": round((pnl - p["fee_in"]) / p["margin"], 4),
                     "fee_out": fee_out}
            del self.positions[sym]
            self.closed.append(trade)
            self.curve.append([now.isoformat(), round(self.balance, 4)])
            out.append(trade)
        if out:
            self.save()
        return out

    # ------------------------------------------------------------ para el panel y el aprendizaje
    def summary(self, prices: dict[str, float | None] | None = None) -> dict[str, Any]:
        prices = prices or {}
        open_rows, unreal = [], 0.0
        for p in self.positions.values():
            px = prices.get(p["symbol"])
            u = None
            if px:
                u = max((1 if p["side"] == "LONG" else -1) * (px - p["entry"]) * p["qty"], -p["margin"])
                unreal += u
            open_rows.append({**p, "price": px, "pnl_unrealized": round(u, 4) if u is not None else None})
        rows = self.closed
        wins = [t for t in rows if t["pnl_total"] > 0]
        return {"enabled": self.control().enabled, "control": self.control().model_dump(),
                "initial_capital": self.cfg.initial_capital, "balance": round(self.balance, 4),
                "equity": round(self.balance + unreal, 4), "unrealized": round(unreal, 4),
                "free": round(self.balance - self.used_margin(), 4), "started_at": self.started_at,
                "positions": open_rows, "closed": list(reversed(rows[-100:])), "curve": self.curve[-500:],
                "stats": {"n": len(rows), "wins": len(wins), "liquidated": sum(t["reason"] == "liquidada" for t in rows),
                          "total": round(sum(t["pnl_total"] for t in rows), 4),
                          "best": max((t["pnl_total"] for t in rows), default=None),
                          "worst": min((t["pnl_total"] for t in rows), default=None),
                          "by_leverage": _by(rows, "leverage"), "by_scope": _by(rows, "approved")},
                "max_positions": self.cfg.max_positions, "updated_at": datetime.now(UTC).isoformat()}


def _by(rows: list[dict], key: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for t in rows:
        g = out.setdefault(str(t.get(key)), {"n": 0, "wins": 0, "total": 0.0, "liquidated": 0})
        g["n"] += 1
        g["wins"] += t["pnl_total"] > 0
        g["total"] = round(g["total"] + t["pnl_total"], 4)
        g["liquidated"] += t["reason"] == "liquidada"
    return out
