"""Puertas de fase (CLAUDE.md §14). `live` no arranca sin que paper y testnet las hayan pasado.

Cada modo tiene su propia base de datos (bot_<modo>.sqlite), así que los
resultados de papel, testnet y live nunca se mezclan.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from bot.learning.journal import Journal
from bot.metrics.performance import summarize

PAPER_MIN_DAYS = 42
PAPER_MIN_TRADES = 60
PAPER_MIN_PF = 1.3
PAPER_MAX_DD = 0.15
TESTNET_MIN_DAYS = 7
TESTNET_BAD_EVENTS = ("posicion_sin_stop", "posicion_huerfana", "orden_huerfana")


def _span_days(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    first = datetime.fromisoformat(trades[0]["opened_at"])
    last = datetime.fromisoformat(trades[-1]["closed_at"])
    return (last - first).total_seconds() / 86400


def paper_gate(db: Path, capital: float) -> dict:
    if not db.exists():
        return {"ok": False, "motivo": "sin base de datos de papel"}
    j = Journal(db)
    trades = j.trades(datetime(2000, 1, 1, tzinfo=UTC))
    s = summarize(trades, capital)
    days = _span_days(trades)
    checks = {
        f"≥ {PAPER_MIN_DAYS} días": days >= PAPER_MIN_DAYS,
        f"≥ {PAPER_MIN_TRADES} operaciones": s.get("n", 0) >= PAPER_MIN_TRADES,
        f"factor de beneficio > {PAPER_MIN_PF}": (s.get("profit_factor") or 0) > PAPER_MIN_PF,
        f"drawdown < {PAPER_MAX_DD:.0%}": s.get("n", 0) > 0 and s.get("max_drawdown", 1) < PAPER_MAX_DD,
    }
    j.close()
    return {"ok": all(checks.values()), "checks": checks, "dias": round(days, 1), "resumen": s}


def testnet_gate(db: Path) -> dict:
    if not db.exists():
        return {"ok": False, "motivo": "sin base de datos de testnet"}
    j = Journal(db)
    trades = j.trades(datetime(2000, 1, 1, tzinfo=UTC))
    events = j.events(datetime(2000, 1, 1, tzinfo=UTC))
    bad = [e for e in events if e["kind"] == "modo_seguro" and any(k in (e["payload"] or "") for k in TESTNET_BAD_EVENTS)]
    days = _span_days(trades)
    checks = {f"≥ {TESTNET_MIN_DAYS} días operando": days >= TESTNET_MIN_DAYS,
              "con operaciones": len(trades) > 0,
              "sin órdenes huérfanas ni posiciones sin stop": not bad}
    j.close()
    return {"ok": all(checks.values()), "checks": checks, "dias": round(days, 1), "incidentes": len(bad)}


def gate_file(data_dir: Path) -> Path:
    return data_dir / "gates.json"


def approve(data_dir: Path, capital: float) -> dict:
    """Registra la aprobación solo si las métricas actuales pasan las puertas."""
    res = {"paper": paper_gate(data_dir / "bot_paper.sqlite", capital),
           "testnet": testnet_gate(data_dir / "bot_testnet.sqlite")}
    if res["paper"]["ok"] and res["testnet"]["ok"]:
        gate_file(data_dir).write_text(json.dumps({"approved_at": datetime.now(UTC).isoformat(), **res},
                                                  default=str, indent=1), encoding="utf-8")
    return res


def live_allowed(data_dir: Path) -> bool:
    p = gate_file(data_dir)
    if not p.exists():
        return False
    data = json.loads(p.read_text(encoding="utf-8"))
    return bool(data.get("paper", {}).get("ok") and data.get("testnet", {}).get("ok"))
