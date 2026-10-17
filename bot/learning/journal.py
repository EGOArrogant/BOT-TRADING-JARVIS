"""Diario (SQLite): cada candidato (operado o rechazado), cada llamada a la IA,
cada operación cerrada y eventos operativos. Es la fuente del aprendizaje."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bot.core.models import Signal, Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    entry REAL, stop REAL, take_profit REAL,
    edge_ratio REAL, score REAL, regime TEXT, max_hold_hours REAL,
    status TEXT NOT NULL,
    reasons TEXT,
    snapshot TEXT,
    ai TEXT,
    size TEXT,
    counterfactual TEXT,
    cf_done INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cand_ts ON candidates(ts);
CREATE TABLE IF NOT EXISTS ai_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, candidate_id TEXT, model TEXT, valid INTEGER, reason TEXT,
    decision TEXT, input_tokens INTEGER, output_tokens INTEGER, latency_s REAL
);
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    signal_id TEXT, symbol TEXT, side TEXT, strategy TEXT, regime TEXT,
    opened_at TEXT, closed_at TEXT, exit_reason TEXT,
    pnl_gross REAL, pnl_net REAL, fees REAL, funding REAL, r REAL,
    slippage_pct REAL, entry_maker INTEGER, ai_confidence REAL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, trade_id TEXT NOT NULL,
    digest TEXT NOT NULL, prev TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT
);
"""

# Estados de un candidato
REJECTED = "rechazado_filtro"
NOT_SELECTED = "no_seleccionado"
AI_REJECTED = "rechazado_ia"
RISK_REJECTED = "rechazado_riesgo"
NO_FILL = "sin_llenado"
TRADED = "operado"


def _j(x: Any) -> str:
    return json.dumps(x, default=str, ensure_ascii=False)


class Journal:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ escritura

    def record_candidate(self, sig: Signal, status: str, reasons: list[str], snapshot: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO candidates (id, ts, symbol, strategy, side, entry, stop, take_profit, edge_ratio,"
            " score, regime, max_hold_hours, status, reasons, snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig.id, sig.created_at.isoformat(), sig.symbol, sig.strategy, sig.side.value, sig.entry, sig.stop,
             sig.take_profit, sig.edge_ratio, sig.score, sig.regime.value, sig.max_hold_hours, status, _j(reasons),
             _j(snapshot)),
        )
        self.db.commit()

    def update_candidate(self, cid: str, status: str | None = None, reasons: list[str] | None = None,
                         ai: dict | None = None, size: dict | None = None) -> None:
        sets, vals = [], []
        for col, val in (("status", status), ("reasons", _j(reasons) if reasons is not None else None),
                         ("ai", _j(ai) if ai is not None else None), ("size", _j(size) if size is not None else None)):
            if val is not None:
                sets.append(f"{col}=?")
                vals.append(val)
        if sets:
            self.db.execute(f"UPDATE candidates SET {', '.join(sets)} WHERE id=?", (*vals, cid))
            self.db.commit()

    def record_ai_call(self, ts: datetime, candidate_id: str, model: str | None, valid: bool, reason: str,
                       decision: dict, tokens_in: int, tokens_out: int, latency_s: float) -> None:
        self.db.execute(
            "INSERT INTO ai_calls (ts, candidate_id, model, valid, reason, decision, input_tokens, output_tokens,"
            " latency_s) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), candidate_id, model, int(valid), reason, _j(decision), tokens_in, tokens_out, latency_s),
        )
        self.db.commit()

    def record_trade(self, t: Trade) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO trades (id, signal_id, symbol, side, strategy, regime, opened_at, closed_at,"
            " exit_reason, pnl_gross, pnl_net, fees, funding, r, slippage_pct, entry_maker, ai_confidence, data)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.id, t.signal_id, t.symbol, t.side.value, t.strategy, t.regime.value, t.opened_at.isoformat(),
             t.closed_at.isoformat(), t.exit_reason.value, float(t.pnl_gross), float(t.pnl_net), float(t.fees),
             float(t.funding), float(t.r_multiple), float(t.slippage_pct), int(t.entry_maker), t.ai_confidence,
             t.model_dump_json()),
        )
        self._seal(t.id)
        self.db.commit()

    # ------------------------------------------------------------ sello del diario (cadena de huellas)
    TRADE_COLS = ("id, signal_id, symbol, side, strategy, regime, opened_at, closed_at, exit_reason, pnl_gross, pnl_net,"
                  " fees, funding, r, slippage_pct, entry_maker, ai_confidence, data")

    def _digest(self, trade_id: str) -> str | None:
        row = self.db.execute(f"SELECT {self.TRADE_COLS} FROM trades WHERE id=?", (trade_id,)).fetchone()
        return hashlib.sha256(json.dumps(list(row), sort_keys=True, default=str).encode()).hexdigest() if row else None

    def _seal(self, trade_id: str) -> None:
        """Cada operación cerrada se encadena con la anterior: cambiar o borrar una rompe la cadena."""
        digest = self._digest(trade_id)
        last = self.db.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last[0] if last else "0" * 64
        h = hashlib.sha256(f"{prev}|{trade_id}|{digest}".encode()).hexdigest()
        self.db.execute("INSERT INTO ledger (ts, trade_id, digest, prev, hash) VALUES (?,?,?,?,?)",
                        (datetime.now(UTC).isoformat(), trade_id, digest, prev, h))

    def verify_ledger(self) -> dict[str, Any]:
        """¿El diario de operaciones sigue igual que cuando se cerró cada operación?"""
        prev, latest, broken = "0" * 64, {}, None
        rows = self.db.execute("SELECT seq, trade_id, digest, prev, hash FROM ledger ORDER BY seq").fetchall()
        for seq, tid, digest, p, h in rows:
            if p != prev or hashlib.sha256(f"{p}|{tid}|{digest}".encode()).hexdigest() != h:
                broken = seq
                break
            latest[tid] = digest
            prev = h
        ids = [r[0] for r in self.db.execute("SELECT id FROM trades")]
        altered = [i for i in ids if i in latest and self._digest(i) != latest[i]]
        missing = [i for i in latest if i not in set(ids)]
        unsealed = [i for i in ids if i not in latest]
        return {"ok": broken is None and not altered and not missing, "entries": len(rows), "broken_at": broken,
                "altered": altered, "missing": missing, "unsealed": len(unsealed)}

    def record_event(self, ts: datetime, kind: str, payload: dict) -> None:
        self.db.execute("INSERT INTO events (ts, kind, payload) VALUES (?,?,?)", (ts.isoformat(), kind, _j(payload)))
        self.db.commit()

    def set_counterfactual(self, cid: str, result: dict) -> None:
        self.db.execute("UPDATE candidates SET counterfactual=?, cf_done=1 WHERE id=?", (_j(result), cid))
        self.db.commit()

    # ------------------------------------------------------------ lectura

    def candidates(self, since: datetime, until: datetime | None = None) -> list[dict]:
        q = "SELECT * FROM candidates WHERE ts >= ?" + (" AND ts < ?" if until else "") + " ORDER BY ts"
        rows = self.db.execute(q, (since.isoformat(), until.isoformat()) if until else (since.isoformat(),))
        return [self._decode(dict(r)) for r in rows]

    def pending_counterfactuals(self, older_than: datetime) -> list[dict]:
        rows = self.db.execute("SELECT * FROM candidates WHERE cf_done=0 AND ts <= ? ORDER BY ts",
                               (older_than.isoformat(),))
        return [self._decode(dict(r)) for r in rows]

    def trades(self, since: datetime, until: datetime | None = None) -> list[dict]:
        q = "SELECT * FROM trades WHERE closed_at >= ?" + (" AND closed_at < ?" if until else "") + " ORDER BY closed_at"
        rows = self.db.execute(q, (since.isoformat(), until.isoformat()) if until else (since.isoformat(),))
        return [dict(r) for r in rows]

    def ai_calls(self, since: datetime) -> list[dict]:
        rows = self.db.execute("SELECT * FROM ai_calls WHERE ts >= ? ORDER BY ts", (since.isoformat(),))
        return [dict(r) for r in rows]

    def events(self, since: datetime) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events WHERE ts >= ? ORDER BY ts", (since.isoformat(),))
        return [dict(r) for r in rows]

    @staticmethod
    def _decode(r: dict) -> dict:
        for k in ("reasons", "snapshot", "ai", "size", "counterfactual"):
            if r.get(k):
                r[k] = json.loads(r[k])
        return r
