"""Puertas de fase (CLAUDE.md §14): record, backtest, paper y testnet. `live` exige las cuatro.

Cada modo tiene su propia base de datos (bot_<modo>.sqlite), así que los
resultados de papel, testnet y live nunca se mezclan.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from bot.learning.journal import Journal
from bot.metrics import robustness
from bot.metrics.performance import summarize

PAPER_MIN_DAYS = 42
PAPER_MIN_TRADES = 60
PAPER_MIN_PF = 1.3
PAPER_MAX_DD = 0.08   # < 10 % de la detención: la spec decía 15 %, que nunca podía fallar
PAPER_MAX_HALT_PROB = 0.05   # Monte Carlo: probabilidad de tocar la detención del 10 % en 100 operaciones
TESTNET_MIN_DAYS = 7
TESTNET_BAD_EVENTS = ("posicion_sin_stop", "posicion_huerfana", "orden_huerfana")
RECORD_DAYS = 7
RECORD_MAX_LOSS = 0.01
KLINES_PER_DAY = 1440


def _count_lines(path: Path) -> int:
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    gz = path.with_suffix(".jsonl.gz")
    if gz.exists():
        with gzip.open(gz, "rt", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    return 0


def record_day_loss(raw_dir: Path, symbols: list[str], day: date) -> float:
    """Peor fracción de velas de 1m perdidas entre los símbolos ese día (0 = completo)."""
    worst = 0.0
    for sym in symbols:
        n = _count_lines(raw_dir / f"{sym.lower()}@kline_1m" / f"{day.isoformat()}.jsonl")
        worst = max(worst, 1 - min(n, KLINES_PER_DAY) / KLINES_PER_DAY)
    return worst


def record_gate(data_dir: Path, symbols: list[str], today: date | None = None) -> dict:
    """7 días seguidos con < 1 % de datos perdidos y al menos una reconexión registrada (probada)."""
    raw = data_dir / "raw"
    today = today or datetime.now(UTC).date()
    days_dir = raw / f"{symbols[0].lower()}@kline_1m"
    if not days_dir.exists():
        return {"ok": False, "motivo": "sin datos grabados"}
    days = sorted({date.fromisoformat(p.name[:10]) for p in days_dir.iterdir()
                   if p.name[:10] < today.isoformat()})
    losses = {d: record_day_loss(raw, symbols, d) for d in days}
    best_run, run, run_end = 0, 0, None
    prev = None
    for d in days:
        good = losses[d] < RECORD_MAX_LOSS
        run = run + 1 if good and prev is not None and d - prev == timedelta(days=1) and run > 0 else (1 if good else 0)
        if run > best_run:
            best_run, run_end = run, d
        prev = d
    reconnects = 0
    db = data_dir / "bot_record.sqlite"
    if db.exists():
        j = Journal(db)
        reconnects = sum(1 for e in j.events(datetime(2000, 1, 1, tzinfo=UTC)) if e["kind"] == "reconexion")
        j.close()
    checks = {f"{RECORD_DAYS} días seguidos con < {RECORD_MAX_LOSS:.0%} perdido": best_run >= RECORD_DAYS,
              "reconexión probada": reconnects > 0}
    return {"ok": all(checks.values()), "checks": checks, "mejor_racha_dias": best_run,
            "fin_racha": run_end.isoformat() if run_end else None, "reconexiones": reconnects,
            "perdida_por_dia": {d.isoformat(): round(v, 4) for d, v in list(losses.items())[-14:]}}


def backtest_gate(data_dir: Path) -> dict:
    p = data_dir / "backtest_gate.json"
    if not p.exists():
        return {"ok": False, "motivo": "sin backtest registrado"}
    return json.loads(p.read_text(encoding="utf-8"))


def _span_days(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    first = datetime.fromisoformat(trades[0]["opened_at"])
    last = datetime.fromisoformat(trades[-1]["closed_at"])
    return (last - first).total_seconds() / 86400


def paper_gate(db: Path, capital: float, risk_per_trade: float = 0.005, halt_drawdown: float = 0.10) -> dict:
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
    rob = robustness.analyze(robustness.from_trades(trades), risk_per_trade, halt_drawdown)
    # No basta con haber ganado: la ventaja tiene que distinguirse de la suerte y sobrevivir a otro orden
    checks["ventaja estadística (IC 95 % de la esperanza > 0)"] = bool(rob.get("enough") and rob["edge"] == "confirmada")
    checks[f"Monte Carlo: prob. de tocar la detención < {PAPER_MAX_HALT_PROB:.0%}"] = bool(
        rob.get("enough") and rob["mc_prob_halt"] < PAPER_MAX_HALT_PROB)
    j.close()
    return {"ok": all(checks.values()), "checks": checks, "dias": round(days, 1), "resumen": s,
            "robustez": {k: rob.get(k) for k in ("n", "expectancy_ci", "mc_dd_p95", "mc_prob_halt", "fragile")}}


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


def all_gates(data_dir: Path, capital: float, symbols: list[str]) -> dict:
    return {"record": record_gate(data_dir, symbols), "backtest": backtest_gate(data_dir),
            "paper": paper_gate(data_dir / "bot_paper.sqlite", capital),
            "testnet": testnet_gate(data_dir / "bot_testnet.sqlite")}


def approve(data_dir: Path, capital: float, symbols: list[str] | None = None) -> dict:
    """Registra la aprobación solo si las métricas actuales pasan TODAS las puertas."""
    res = all_gates(data_dir, capital, symbols or ["BTCUSDT"])
    if all(r["ok"] for r in res.values()):
        gate_file(data_dir).write_text(json.dumps({"approved_at": datetime.now(UTC).isoformat(), **res},
                                                  default=str, indent=1), encoding="utf-8")
    return res


def live_allowed(data_dir: Path) -> bool:
    p = gate_file(data_dir)
    if not p.exists():
        return False
    data = json.loads(p.read_text(encoding="utf-8"))
    return all(bool(data.get(k, {}).get("ok")) for k in ("record", "backtest", "paper", "testnet"))
