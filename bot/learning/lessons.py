"""Lecciones activas: entran al prompt del analista con su evidencia (máx. 10)
y caducan si en 30 días dejan de confirmarse."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from bot.core.models import DataPoint, Regime, Side, Signal
from bot.learning.hypotheses import Hypothesis
from bot.learning.journal import Journal

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY, text TEXT, evidence TEXT, hypothesis TEXT,
    created_at TEXT, last_confirmed_at TEXT, active INTEGER DEFAULT 1
);
"""


class LessonBook:
    def __init__(self, journal: Journal, expiry_days: int = 30, max_active: int = 10):
        self.db = journal.db
        self.db.executescript(SCHEMA)
        self.expiry = timedelta(days=expiry_days)
        self.max_active = max_active

    def add_from_hypothesis(self, h: Hypothesis, evidence: dict, now: datetime) -> str:
        text = (f"{h.description} (retador {evidence.get('challenger_exp', 0):+.2f}R vs campeón "
                f"{evidence.get('champion_exp', 0):+.2f}R, n={evidence.get('n_cases')})")
        self.db.execute(
            "INSERT OR REPLACE INTO lessons (id, text, evidence, hypothesis, created_at, last_confirmed_at, active)"
            " VALUES (?,?,?,?,?,?,1)", (h.id, text, json.dumps(evidence), h.model_dump_json(), now.isoformat(),
                                        now.isoformat()))
        self.db.commit()
        return h.id

    def confirm(self, lesson_id: str, now: datetime, evidence: dict | None = None) -> None:
        if evidence is not None:
            self.db.execute("UPDATE lessons SET last_confirmed_at=?, evidence=? WHERE id=?",
                            (now.isoformat(), json.dumps(evidence), lesson_id))
        else:
            self.db.execute("UPDATE lessons SET last_confirmed_at=? WHERE id=?", (now.isoformat(), lesson_id))
        self.db.commit()

    def expire_stale(self, now: datetime) -> list[str]:
        cutoff = (now - self.expiry).isoformat()
        rows = self.db.execute("SELECT id FROM lessons WHERE active=1 AND last_confirmed_at < ?", (cutoff,)).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            self.db.executemany("UPDATE lessons SET active=0 WHERE id=?", [(i,) for i in ids])
            self.db.commit()
        return ids

    def active(self) -> list[dict]:
        rows = self.db.execute("SELECT id, text, evidence, last_confirmed_at FROM lessons WHERE active=1 "
                               "ORDER BY last_confirmed_at DESC LIMIT ?", (self.max_active,)).fetchall()
        return [{"leccion": r[1], "evidencia": json.loads(r[2] or "{}"), "confirmada": r[3][:10]} for r in rows]

    def reconfirm_all(self, journal: Journal, now: datetime) -> None:
        """Una lección de bloqueo sigue vigente si, en los últimos 30 días, los casos que bloquea
        tuvieron peor expectativa que el resto (con al menos 10 casos bloqueados)."""
        since = now - self.expiry
        cands = [c for c in journal.candidates(since) if c.get("counterfactual") and c["status"] != "rechazado_filtro"]
        for row in self.db.execute("SELECT id, hypothesis FROM lessons WHERE active=1").fetchall():
            h = Hypothesis.model_validate_json(row[1])
            if h.kind == "strategy_param":
                continue
            blocked, kept = [], []
            for c in cands:
                # Se reconstruye la señal con la misma ventaja registrada (expected/costo = edge_ratio)
                sig = Signal(id=c["id"], symbol=c["symbol"], strategy=c["strategy"], side=Side(c["side"]),
                             entry=c["entry"], stop=c["stop"], take_profit=c["take_profit"],
                             expected_move_pct=max(c["edge_ratio"], 1e-6) * 1e-3, total_cost_pct=1e-3,
                             data_quality=1, regime=Regime(c["regime"]), created_at=datetime.fromisoformat(c["ts"]))
                fields = {k: DataPoint.ok(v, "journal", sig.created_at) for k, v in (c.get("snapshot") or {}).items()
                          if isinstance(v, int | float)}
                (kept if h.allows(sig, fields) else blocked).append(float(c["counterfactual"]["r_net"]))
            if len(blocked) >= 10 and kept and sum(blocked) / len(blocked) < sum(kept) / len(kept):
                self.confirm(row[0], now, {"bloqueados_n": len(blocked), "exp_bloqueados": sum(blocked) / len(blocked),
                                           "exp_resto": sum(kept) / len(kept)})
