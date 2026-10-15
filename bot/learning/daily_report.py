"""Zona de aprendizaje diaria: reports/zona_YYYY-MM-DD.md (máximo una página)."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

from bot.learning.journal import AI_REJECTED, TRADED, Journal
from bot.metrics.performance import summarize


def _missing_data(candidates: list[dict]) -> Counter:
    c: Counter = Counter()
    for cand in candidates:
        for r in cand.get("reasons") or []:
            if r.startswith("datos") or "desconocid" in r:
                c[r.split(":", 1)[0] + ": " + r.split(":", 1)[-1].strip()[:60]] += 1
    return c


def build_zone(journal: Journal, day: date, capital: float) -> str:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    trades = journal.trades(start, end)
    cands = journal.candidates(start, end)
    s = summarize(trades, capital)
    lines = [f"# Zona de aprendizaje — {day.isoformat()}", ""]
    if s.get("n"):
        lines.append(f"**Resumen:** {s['n']} operaciones · PnL neto {s['pnl_net']:+.2f} · expectativa "
                     f"{s['expectancy_r']:+.2f}R · acierto {s['win_rate'] * 100:.0f}% · comisiones {s['fees']:.2f} "
                     f"(n={s['n']}, muestra pequeña: no sacar conclusiones de un solo día)")
    else:
        lines.append("**Resumen:** 0 operaciones (resultado válido).")
    status = Counter(c["status"] for c in cands)
    lines.append(f"**Candidatos:** {len(cands)} · " + ", ".join(f"{k}: {v}" for k, v in status.most_common()))
    lines.append("")

    ranked = sorted(trades, key=lambda t: float(t["r"]))
    if ranked:
        lines.append("## Mejores 3")
        for t in ranked[::-1][:3]:
            lines.append(f"- {t['symbol']} {t['side']} {t['strategy']}: {float(t['r']):+.2f}R — salida por {t['exit_reason']}")
        lines.append("## Peores 3")
        for t in ranked[:3]:
            lines.append(f"- {t['symbol']} {t['side']} {t['strategy']}: {float(t['r']):+.2f}R — salida por {t['exit_reason']}")
        lines.append("")

    miss = _missing_data(cands)
    lines.append("## Datos que faltaron")
    lines += [f"- {k} (×{v})" for k, v in miss.most_common(5)] or ["- ninguno"]
    lines.append("")

    lines.append("## Decisiones de la IA")
    ai_ok = [c for c in cands if c["status"] == TRADED]
    ai_no = [c for c in cands if c["status"] == AI_REJECTED and c.get("counterfactual")]
    right_rej = sum(1 for c in ai_no if float(c["counterfactual"]["r_net"]) <= 0)
    wins = sum(1 for t in trades if float(t["r"]) > 0)
    lines.append(f"- Aprobadas: {len(ai_ok)} (ganadoras {wins}/{len(trades)})")
    lines.append(f"- Rechazadas con contrafactual: {len(ai_no)} (acertó al rechazar {right_rej}/{len(ai_no)})")
    lines.append("")

    lesson = "Sin evidencia suficiente para una lección nueva."
    if ranked and float(ranked[0]["r"]) < -1.2:
        lesson = f"Revisar {ranked[0]['symbol']} {ranked[0]['strategy']}: pérdida mayor a 1.2R (posible deslizamiento o hueco)."
    elif ai_no and right_rej < len(ai_no) / 2:
        lesson = "La IA rechazó más candidatos ganadores que perdedores hoy; vigilar si se repite (muestra pequeña)."
    elif miss:
        lesson = f"El dato más faltante fue «{miss.most_common(1)[0][0]}»: revisar la fuente."
    lines.append(f"**Lección:** {lesson}")
    return "\n".join(lines) + "\n"


def write_zone(journal: Journal, day: date, capital: float, reports_dir: Path) -> Path:
    path = reports_dir / f"zona_{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_zone(journal, day, capital), encoding="utf-8")
    return path
