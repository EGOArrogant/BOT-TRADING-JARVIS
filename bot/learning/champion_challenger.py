"""Campeón vs retador.

Cada hipótesis activa se evalúa en paralelo (en papel) sobre TODOS los
candidatos que pasan los filtros, no solo sobre los operados. El resultado de
cada caso es el R real si se operó, o el contrafactual si no.

Promoción solo si: ≥ 50 casos evaluados del retador, de los cuales ≥ 20
operaciones (reales o en papel); mejora la expectativa neta; y no empeora el
drawdown (en R acumulado). Por defecto requiere aprobación humana.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from bot.core.models import DataPoint, Signal
from bot.learning.hypotheses import OVERRIDES_PATH, AppliedOverride, Hypothesis, save_override
from bot.learning.journal import Journal
from bot.metrics.multiple_testing import bh_fdr, welch_p

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY, created_at TEXT, status TEXT, data TEXT, evaluation TEXT
);
CREATE TABLE IF NOT EXISTS shadow_cases (
    hyp_id TEXT, signal_id TEXT, ts TEXT, symbol TEXT, strategy TEXT, side TEXT,
    entry REAL, stop REAL, take_profit REAL, max_hold_hours REAL, cost_pct REAL,
    r_net REAL, traded INTEGER DEFAULT 0, done INTEGER DEFAULT 0,
    PRIMARY KEY (hyp_id, signal_id)
);
"""

ACTIVE, PENDING_APPROVAL, PROMOTED, REJECTED_H, RETIRED = "activa", "pendiente_aprobacion", "promovida", "descartada", "retirada"
PASSED_STATUSES = ("operado", "no_seleccionado", "rechazado_ia", "rechazado_riesgo", "sin_llenado")


FDR_Q = 0.10  # proporción tolerada de falsos descubrimientos entre retadores


@dataclass
class Evaluation:
    n_cases: int
    n_trades: int
    champion_exp: float
    challenger_exp: float
    champion_dd: float
    challenger_dd: float
    eligible: bool
    better: bool
    p_value: float = 1.0      # p de que el retador sea mejor por suerte (Welch, unilateral)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def r_drawdown(rs: list[float]) -> float:
    if not rs:
        return 0.0
    cum = np.cumsum(rs)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    return float(np.max(peak - cum))


class ChampionChallenger:
    def __init__(self, journal: Journal, min_cases: int = 50, min_trades: int = 20, require_approval: bool = True,
                 reports_dir: Path | None = None, overrides_path: Path = OVERRIDES_PATH):
        self.j = journal
        self.db = journal.db
        self.db.executescript(SCHEMA)
        self.min_cases = min_cases
        self.min_trades = min_trades
        self.require_approval = require_approval
        self.reports_dir = reports_dir
        self.overrides_path = overrides_path

    # ------------------------------------------------------------ hipótesis

    def add(self, h: Hypothesis, now: datetime) -> str:
        self.db.execute("INSERT OR IGNORE INTO hypotheses (id, created_at, status, data) VALUES (?,?,?,?)",
                        (h.id, now.isoformat(), ACTIVE, h.model_dump_json()))
        self.db.commit()
        return h.id

    def active(self) -> list[Hypothesis]:
        rows = self.db.execute("SELECT data FROM hypotheses WHERE status=?", (ACTIVE,))
        return [Hypothesis.model_validate_json(r[0]) for r in rows]

    def status(self, hid: str) -> str | None:
        r = self.db.execute("SELECT status FROM hypotheses WHERE id=?", (hid,)).fetchone()
        return r[0] if r else None

    # ------------------------------------------------------------ casos sombra

    def record_shadow(self, h: Hypothesis, sig: Signal, fields: dict[str, DataPoint]) -> None:
        """Registra un candidato que el retador habría dejado pasar."""
        if not h.allows(sig, fields):
            return
        self.db.execute(
            "INSERT OR IGNORE INTO shadow_cases (hyp_id, signal_id, ts, symbol, strategy, side, entry, stop,"
            " take_profit, max_hold_hours, cost_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (h.id, sig.id, sig.created_at.isoformat(), sig.symbol, sig.strategy, sig.side.value, sig.entry, sig.stop,
             sig.take_profit, sig.max_hold_hours, sig.total_cost_pct))
        self.db.commit()

    def pending_shadow(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM shadow_cases WHERE done=0")]

    def resolve_shadow(self, hyp_id: str, signal_id: str, r_net: float) -> None:
        traded = self.db.execute("SELECT 1 FROM trades WHERE signal_id=?", (signal_id,)).fetchone() is not None
        if traded:
            row = self.db.execute("SELECT r FROM trades WHERE signal_id=?", (signal_id,)).fetchone()
            r_net = float(row[0])
        self.db.execute("UPDATE shadow_cases SET r_net=?, done=1, traded=? WHERE hyp_id=? AND signal_id=?",
                        (r_net, int(traded), hyp_id, signal_id))
        self.db.commit()

    # ------------------------------------------------------------ evaluación

    def _champion_rs(self, since: str) -> list[float]:
        out = []
        for c in self.j.candidates(datetime.fromisoformat(since)):
            if c["status"] not in PASSED_STATUSES:
                continue
            t = self.db.execute("SELECT r FROM trades WHERE signal_id=?", (c["id"],)).fetchone()
            if t is not None:
                out.append(float(t[0]))
            elif c.get("counterfactual"):
                out.append(float(c["counterfactual"]["r_net"]))
        return out

    def evaluate(self, hid: str) -> Evaluation | None:
        row = self.db.execute("SELECT created_at FROM hypotheses WHERE id=?", (hid,)).fetchone()
        if row is None:
            return None
        cases = self.db.execute("SELECT r_net, traded FROM shadow_cases WHERE hyp_id=? AND done=1 ORDER BY ts",
                                (hid,)).fetchall()
        ch = [float(r[0]) for r in cases]
        n_tr = sum(int(r[1]) for r in cases)
        champ = self._champion_rs(row[0])
        ce = float(np.mean(champ)) if champ else 0.0
        xe = float(np.mean(ch)) if ch else 0.0
        cdd, xdd = r_drawdown(champ), r_drawdown(ch)
        eligible = len(ch) >= self.min_cases and n_tr >= self.min_trades
        return Evaluation(len(ch), n_tr, ce, xe, cdd, xdd, eligible, eligible and xe > ce and xdd <= cdd,
                          welch_p(ch, champ))

    def review(self, now: datetime) -> list[tuple[Hypothesis, Evaluation]]:
        """Revisa hipótesis activas; las que ganan pasan a promoción (o a aprobación humana)."""
        out = []
        evals = [(h, ev) for h in self.active() if (ev := self.evaluate(h.id)) is not None]
        # Varias ideas a la vez: solo cuentan las mejoras que sobreviven al control de falsos positivos (BH-FDR)
        cand = [(h, ev) for h, ev in evals if ev.better]
        keep = bh_fdr([ev.p_value for _, ev in cand], FDR_Q)
        for (h, ev), ok in zip(cand, keep, strict=True):
            if not ok:
                ev.better = False
        for h, ev in evals:
            self.db.execute("UPDATE hypotheses SET evaluation=? WHERE id=?", (json.dumps(ev.as_dict()), h.id))
            if ev.better:
                if self.require_approval:
                    self._set_status(h.id, PENDING_APPROVAL)
                    self._log_change(h, ev, now, "PENDIENTE DE APROBACIÓN HUMANA")
                else:
                    self.promote(h.id, now)
                out.append((h, ev))
            elif ev.eligible and ev.n_cases >= 2 * self.min_cases:
                self._set_status(h.id, REJECTED_H)  # con muestra amplia y sin mejora: descartar
        self.db.commit()
        return out

    def promote(self, hid: str, now: datetime) -> Hypothesis:
        row = self.db.execute("SELECT data, evaluation FROM hypotheses WHERE id=?", (hid,)).fetchone()
        if row is None:
            raise KeyError(hid)
        h = Hypothesis.model_validate_json(row[0])
        ev = json.loads(row[1]) if row[1] else {}
        save_override(AppliedOverride(hypothesis=h, approved_at=now, evidence=ev), self.overrides_path)
        self._set_status(hid, PROMOTED)
        self._log_change(h, None, now, "PROMOVIDA", ev)
        return h

    def _set_status(self, hid: str, status: str) -> None:
        self.db.execute("UPDATE hypotheses SET status=? WHERE id=?", (status, hid))
        self.db.commit()

    def _log_change(self, h: Hypothesis, ev: Evaluation | None, now: datetime, title: str,
                    ev_dict: dict | None = None) -> None:
        if self.reports_dir is None:
            return
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        e = ev.as_dict() if ev else (ev_dict or {})
        with open(self.reports_dir / "cambios.md", "a", encoding="utf-8") as fh:
            fh.write(f"\n## {now:%Y-%m-%d %H:%M} UTC — {title}: `{h.id}`\n\n- {h.description}\n"
                     f"- Cambio: `{h.model_dump_json(exclude_none=True)}`\n"
                     f"- Evidencia: casos={e.get('n_cases')}, operaciones={e.get('n_trades')}, "
                     f"expectativa campeón={e.get('champion_exp', 0):+.3f}R vs retador={e.get('challenger_exp', 0):+.3f}R, "
                     f"DD campeón={e.get('champion_dd', 0):.2f}R vs retador={e.get('challenger_dd', 0):.2f}R\n")
            if title.startswith("PENDIENTE"):
                fh.write(f"- Aprobar con: `python -m bot.main approve {h.id}`\n")

