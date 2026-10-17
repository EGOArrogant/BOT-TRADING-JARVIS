"""Reportes del panel en palabras simples: un resumen por día y un análisis general.

Se arman directamente del diario (SQLite, solo lectura), así existen para todos los días con datos, no
solo para los que tienen `zona_YYYY-MM-DD.md`. Las reflexiones semanales y los cambios aprobados se
muestran tal cual (ya están escritos para leerse). Nada de esto decide ni cambia el comportamiento del bot.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from bot.core.config import Settings
from bot.dashboard.data import _connect, _rows
from bot.metrics import robustness

DAYS = 60

COIN = {"BTCUSDT": "Bitcoin", "ETHUSDT": "Ethereum", "SOLUSDT": "Solana", "XRPUSDT": "XRP", "DOGEUSDT": "Dogecoin",
        "XLMUSDT": "Stellar"}
STRATEGY = {"trend_pullback": "retroceso dentro de una tendencia", "range_breakout": "ruptura de un rango",
            "funding_reversion": "giro por exceso de apuestas en un sentido",
            "trend_breakout": "ruptura a favor de la tendencia"}
EXIT = {"stop": "tocó su stop-loss (la pérdida máxima prevista)", "take_profit": "llegó a su objetivo",
        "trailing": "el stop que la iba siguiendo protegió la ganancia", "time": "pasó el tiempo sin avanzar",
        "time_max": "llegó al tiempo máximo", "invalidation": "la idea dejó de cumplirse",
        "macro": "venía una noticia económica importante", "funding": "iba a pagar un costo de funding alto",
        "ai_early_exit": "la IA pidió cerrar antes", "manual": "se cerró a mano", "safe_mode": "modo seguro",
        "fin_backtest": "fin de la prueba"}
# Motivos de descarte (prefijo del texto técnico -> explicación simple)
REASONS = [
    ("datos no ok", "faltaban datos para decidir con seguridad"),
    ("calendario macro", "no pudo confirmar el calendario de noticias económicas"),
    ("bloqueo macro", "había una noticia económica importante cerca (inflación, empleo o la Fed)"),
    ("ventaja", "la ganancia esperada no alcanzaba para cubrir comisiones con margen"),
    ("spread", "había poca liquidez en ese momento"), ("profundidad", "había poca liquidez en ese momento"),
    ("funding z", "demasiada gente apostando en el mismo sentido (riesgo de barrida)"),
    ("percentil de volatilidad", "el mercado estaba demasiado agitado"),
    ("volatilidad", "el mercado estaba demasiado agitado"),
    ("techo diario", "ya había hecho el máximo de operaciones del día"),
    ("entrada #", "la 4.ª y 5.ª operación del día exigen una oportunidad todavía mejor"),
    ("ya hay posición", "ya tenía una operación abierta en esa moneda"),
    ("máximo de posiciones", "ya tenía el máximo de operaciones abiertas a la vez"),
    ("máximo de operaciones por moneda", "ya había operado esa moneda 2 veces ese día"),
    ("enfriamiento", "acababa de cerrar en esa moneda y espera un rato antes de volver"),
    ("detenido", "el bot estaba detenido por seguridad"), ("modo seguro", "el bot estaba en modo seguro"),
    ("pausa", "el bot estaba en pausa tras varias pérdidas seguidas"),
    ("pérdida diaria", "ya había perdido el máximo permitido ese día"),
    ("exposición", "ya había demasiado dinero apostado en la misma dirección"),
    ("bajo rendimiento", "esa moneda venía perdiendo en esa dirección y está en pausa un día"),
]


def money(x: float) -> str:
    s = f"{abs(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("+" if x > 0 else "−" if x < 0 else "") + s + " $"


def amount(x: float) -> str:
    """Cantidad sin signo (cuando la frase ya dice ganó/perdió)."""
    return money(abs(x)).lstrip("+")


def num(x: float, d: int = 2, sign: bool = False) -> str:
    s = f"{x:+.{d}f}" if sign else f"{x:.{d}f}"
    return s.replace("-", "−").replace(".", ",")


def price(x: float) -> str:
    d = 2 if x >= 100 else 4 if x >= 1 else 5
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def plain_reason(r: str) -> str:
    low = r.lower()
    for prefix, text in REASONS:
        if low.startswith(prefix) or prefix in low:
            return text
    return r


def _times(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def trade_sentence(t: dict) -> str:
    d = json.loads(t.get("data") or "{}") if isinstance(t.get("data"), str) else (t.get("data") or {})
    coin = COIN.get(t["symbol"], t["symbol"])
    verb = f"Compró {coin} (apuesta a que sube)" if t["side"] == "LONG" else f"Vendió {coin} en corto (apuesta a que baja)"
    entry, exit_ = float(d.get("entry_price") or 0), float(d.get("exit_price") or 0)
    where = f" a {price(entry)} y cerró a {price(exit_)}" if entry and exit_ else ""
    pnl = float(t["pnl_net"])
    result = f"ganó {amount(pnl)}" if pnl > 0 else f"perdió {amount(pnl)}" if pnl < 0 else "quedó en cero"
    why = EXIT.get(t["exit_reason"], t["exit_reason"])
    strat = STRATEGY.get(t["strategy"], t["strategy"])
    return f"{verb}{where}: {result}. Cerró porque {why}. Tipo de oportunidad: {strat}."


def _cf(c: dict) -> float | None:
    cf = c.get("counterfactual")
    return float(cf["r_net"]) if cf and cf.get("r_net") is not None else None


def day_report(day: str, trades: list[dict], cands: list[dict], risk_per_trade: float, capital: float) -> dict:
    pnl = sum(float(t["pnl_net"]) for t in trades)
    fees = sum(float(t["fees"]) for t in trades)
    wins = [t for t in trades if float(t["pnl_net"]) > 0]
    paras: list[str] = []
    if trades:
        tone = "pos" if pnl > 0 else "neg" if pnl < 0 else "neutral"
        headline = (f"Ganó {amount(pnl)}" if pnl > 0 else f"Perdió {amount(pnl)}" if pnl < 0 else "Quedó en cero") + \
            f" en {_times(len(trades), 'operación', 'operaciones')}"
        paras.append(f"Hizo {_times(len(trades), 'operación', 'operaciones')}: {len(wins)} con ganancia y "
                     f"{len(trades) - len(wins)} con pérdida. El resultado ya descuenta {amount(fees)} de comisiones"
                     f" ({num(pnl / capital * 100, sign=True)} % del capital inicial).")
    else:
        tone, headline = "neutral", "No operó"
        paras.append("No abrió ninguna operación. Es un resultado válido: el bot prefiere esperar a que aparezca una "
                     "oportunidad clara antes que arriesgar el dinero en una dudosa.")

    status = Counter(c["status"] for c in cands)
    if cands:
        reasons = Counter()
        for c in cands:
            if c["status"] in ("rechazado_filtro", "rechazado_riesgo"):
                for r in c.get("reasons") or []:
                    reasons[plain_reason(r)] += 1
        filt = status.get("rechazado_filtro", 0) + status.get("rechazado_riesgo", 0)
        ai_seen = sum(1 for c in cands if c.get("ai"))
        ai_yes = status.get("operado", 0) + status.get("sin_llenado", 0)
        txt = f"Encontró {_times(len(cands), 'posible oportunidad', 'posibles oportunidades')}."
        if filt:
            top = "; ".join(f"{k} ({v})" for k, v in reasons.most_common(3))
            txt += f" Descartó {filt} por sus reglas de seguridad" + (f" — sobre todo porque {top}." if top else ".")
        if status.get("no_seleccionado"):
            txt += (f" En {_times(status['no_seleccionado'], 'caso', 'casos')} había otra mejor al mismo tiempo y "
                    "eligió esa.")
        if ai_seen:
            txt += f" La IA revisó {ai_seen}: aprobó {ai_yes} y dijo que no a {status.get('rechazado_ia', 0)}."
        if status.get("sin_llenado"):
            txt += f" {_times(status['sin_llenado'], 'orden no se llenó', 'órdenes no se llenaron')} (el precio no llegó)."
        paras.append(txt)
    else:
        paras.append("No apareció ninguna oportunidad que cumpliera las condiciones de sus estrategias.")

    ai_no = [x for x in (_cf(c) for c in cands if c["status"] == "rechazado_ia") if x is not None]
    if ai_no:
        right = sum(1 for x in ai_no if x <= 0)
        paras.append(f"Cuando la IA dijo que no, acertó {right} de {len(ai_no)} veces (se comprobó después con los "
                     "precios reales qué habría pasado).")
    practice = [x for x in (_cf(c) for c in cands if c["status"] != "operado") if x is not None]
    if practice:
        avg = mean(practice)
        paras.append(f"Práctica: de las oportunidades que no operó, {sum(1 for x in practice if x > 0)} de {len(practice)} "
                     f"habrían ganado. En promedio cada una habría dado {num(avg, sign=True)} veces lo arriesgado"
                     f" (≈ {money(avg * risk_per_trade * capital)} por operación).")
    missing = sum(1 for c in cands for r in c.get("reasons") or [] if r.startswith("datos"))

    if trades and pnl < 0:
        lesson = ("Día con pérdida: es normal y forma parte del plan. Lo importante es que cada pérdida se quedó en lo "
                  "previsto por su stop-loss.")
    elif trades and pnl > 0:
        lesson = "Buen día, pero un solo día no prueba nada: lo que cuenta es el resultado de muchas semanas."
    elif ai_no and sum(1 for x in ai_no if x > 0) > len(ai_no) / 2:
        lesson = "La IA rechazó varias que habrían ganado; si se repite muchos días, el bot lo detectará en Disciplina."
    elif missing:
        lesson = f"Faltaron datos {_times(missing, 'vez', 'veces')}: en esos momentos el bot no opera por seguridad."
    else:
        lesson = "Día tranquilo: esperó con paciencia, que también es parte de operar bien."
    return {"day": day, "headline": headline, "tone": tone, "paragraphs": paras,
            "trades": [{"text": trade_sentence(t), "pnl": float(t["pnl_net"])} for t in sorted(trades, key=lambda t: t["closed_at"])],
            "lesson": lesson, "n_trades": len(trades), "pnl": pnl, "n_candidates": len(cands)}


def analysis(trades: list[dict], cands: list[dict], capital: float, risk_per_trade: float,
             halt_drawdown: float) -> list[dict[str, str]]:
    """Frases del análisis general, de lo más importante a lo menos."""
    out: list[dict[str, str]] = []
    n = len(trades)
    if not n:
        out.append({"title": "Todavía sin operaciones", "text": "Aún no hay operaciones cerradas. Cuando haya, aquí verás "
                    "cuánto gana o pierde, en qué monedas le va mejor y si su ventaja es real o suerte.", "tone": "neutral"})
    else:
        pnl = sum(float(t["pnl_net"]) for t in trades)
        rs = [float(t["r"]) for t in trades]
        wr = sum(1 for r in rs if r > 0) / n
        out.append({"title": "Resultado total", "tone": "pos" if pnl > 0 else "neg",
                    "text": f"En {_times(n, 'operación', 'operaciones')} {'ganó' if pnl >= 0 else 'perdió'} {amount(pnl)}"
                            f" ({num(pnl / capital * 100, sign=True)} % del capital). Acertó {wr * 100:.0f} % de las veces. En "
                            f"promedio, cada operación {'ganó' if mean(rs) >= 0 else 'perdió'} {num(abs(mean(rs)))} veces lo "
                            "que arriesga."})
        rb = robustness.analyze(robustness.from_trades(trades), risk_per_trade, halt_drawdown)
        if rb.get("enough"):
            lo, hi = rb["expectancy_ci"]
            verdict = {"confirmada": ("¿Ventaja real o suerte? Real", "pos",
                                      "Incluso en el peor caso razonable el resultado promedio es positivo."),
                       "negativa": ("¿Ventaja real o suerte? Por ahora pierde", "neg",
                                    "Incluso en el mejor caso razonable el promedio es negativo: así no debe pasar a dinero real."),
                       "incierta": ("¿Ventaja real o suerte? Todavía no se sabe", "neutral",
                                    f"Con {n} operaciones el promedio real podría estar entre {num(lo, sign=True)} y "
                                    f"{num(hi, sign=True)} veces lo "
                                    "arriesgado. Hacen falta más operaciones para saberlo.")}[rb["edge"]]
            out.append({"title": verdict[0], "tone": verdict[1], "text": verdict[2]})
        by: dict[str, dict[str, list[float]]] = {"coin": defaultdict(list), "strategy": defaultdict(list),
                                                 "side": defaultdict(list)}
        for t in trades:
            by["coin"][COIN.get(t["symbol"], t["symbol"])].append(float(t["pnl_net"]))
            by["strategy"][STRATEGY.get(t["strategy"], t["strategy"])].append(float(t["pnl_net"]))
            by["side"]["las compras" if t["side"] == "LONG" else "las ventas en corto"].append(
                float(t["pnl_net"]))
        for key, title in (("coin", "Monedas"), ("strategy", "Tipos de oportunidad"), ("side", "Subidas o bajadas")):
            g = sorted(((k, sum(v), len(v)) for k, v in by[key].items()), key=lambda x: -x[1])
            if len(g) >= 2:
                best, worst = g[0], g[-1]
                txt = (f"Le va mejor con {best[0]} ({money(best[1])}, {_times(best[2], 'operación', 'operaciones')}) y peor con "
                       f"{worst[0]} ({money(worst[1])}, {_times(worst[2], 'operación', 'operaciones')}).")
                if min(best[2], worst[2]) < 10:
                    txt += " Con tan pocas operaciones todavía puede ser casualidad."
                out.append({"title": title, "text": txt, "tone": "neutral"})
        fees = sum(float(t["fees"]) for t in trades)
        gross = sum(float(t["pnl_gross"]) for t in trades if "pnl_gross" in t)
        out.append({"title": "Comisiones", "tone": "neutral",
                    "text": f"Pagó {amount(fees)} de comisiones"
                            + (f", que equivalen al {fees / abs(gross) * 100:.0f} % de lo ganado antes de comisiones." if gross > 0
                               else ".") + " Por eso opera poco y solo cuando lo esperado supera varias veces el costo."})
    practice = [x for x in (_cf(c) for c in cands if c["status"] != "operado") if x is not None]
    if practice:
        out.append({"title": "Práctica con precios reales", "tone": "neutral",
                    "text": f"Además simuló {len(practice)} oportunidades que no operó: {sum(1 for x in practice if x > 0) / len(practice) * 100:.0f} % "
                            f"habrían ganado y en promedio darían {num(mean(practice), sign=True)} veces lo arriesgado. Esto le sirve para "
                            "aprender más rápido sin arriesgar."})
    # Camino a dinero real (puertas de fase del papel)
    if n:
        first = min(datetime.fromisoformat(t["opened_at"]) for t in trades)
        last = max(datetime.fromisoformat(t["closed_at"]) for t in trades)
        weeks = (last - first).days / 7
        gw = sum(float(t["pnl_net"]) for t in trades if float(t["pnl_net"]) > 0)
        gl = -sum(float(t["pnl_net"]) for t in trades if float(t["pnl_net"]) < 0)
        pf = gw / gl if gl else None
        out.append({"title": "¿Cuánto falta para dinero real?", "tone": "neutral",
                    "text": f"Necesita al menos 6 semanas y 60 operaciones en demo con buen resultado. Lleva {num(weeks, 1)} semanas y "
                            f"{n} operaciones" + (f"; por cada 1 $ perdido gana {num(pf)} $ (se pide más de 1,30)." if pf else ".")})
    return out


def collect(settings: Settings, now: datetime | None = None, days: int = DAYS) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    st = settings.storage
    since = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    db = _connect(st.db_path)
    try:
        trades = _rows(db, "SELECT * FROM trades WHERE closed_at >= ? ORDER BY closed_at", (since,))
        all_trades = _rows(db, "SELECT * FROM trades ORDER BY closed_at")
        cands = [{**c, "reasons": json.loads(c["reasons"] or "[]"), "ai": json.loads(c["ai"] or "null"),
                  "counterfactual": json.loads(c["counterfactual"] or "null")}
                 for c in _rows(db, "SELECT ts, symbol, strategy, side, status, reasons, ai, counterfactual FROM candidates "
                                    "WHERE ts >= ? ORDER BY ts", (since,))]
    finally:
        if db is not None:
            db.close()
    by_day_t: dict[str, list[dict]] = defaultdict(list)
    by_day_c: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_day_t[t["closed_at"][:10]].append(t)
    for c in cands:
        by_day_c[c["ts"][:10]].append(c)
    capital = settings.paper.initial_capital
    rpt = settings.risk.risk_per_trade
    days_list = sorted(set(by_day_t) | set(by_day_c) | {now.date().isoformat()}, reverse=True)
    return {
        "generated_at": now.isoformat(),
        "analysis": analysis(all_trades, cands, capital, rpt, settings.risk.max_drawdown),
        "days": [day_report(d, by_day_t.get(d, []), by_day_c.get(d, []), rpt, capital) for d in days_list],
        "weekly": _files(st.reports_dir, "reflexion_*.md", 12),
        "changes": _read(st.reports_dir / "cambios.md"),
    }


def _read(path: Path, limit: int = 20000) -> str | None:
    try:
        return path.read_text(encoding="utf-8")[-limit:]
    except OSError:
        return None


def _files(folder: Path, pattern: str, n: int) -> list[dict[str, str]]:
    return [{"name": p.stem.replace("reflexion_", "Semana "), "text": _read(p) or ""}
            for p in sorted(folder.glob(pattern), reverse=True)[:n]]
