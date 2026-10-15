"""Reporte HTML estático (sin librerías externas): KPIs, curva de capital,
drawdown, tablas por dimensión y mejores/peores operaciones.

Se genera cada hora en reports/ y NO se expone a internet.
"""

from __future__ import annotations

import html
import json
import math
from datetime import datetime
from pathlib import Path

from bot.metrics.performance import ai_calibration, by_dimension, equity_curve, rejection_stats, summarize

CSS = """
:root{--surface:#fcfcfb;--bg:#f4f4f2;--ink:#0b0b0b;--ink2:#52514e;--grid:#e1e0d9;--series:#2a78d6;
--good:#006300;--bad:#b42323;--card:#ffffff}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--surface:#1a1a19;--bg:#121211;--ink:#f0efec;
--ink2:#c3c2b7;--grid:#2c2c2a;--series:#3987e5;--good:#0ca30c;--bad:#e25c5c;--card:#1f1f1e}}
:root[data-theme="dark"]{--surface:#1a1a19;--bg:#121211;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;
--series:#3987e5;--good:#0ca30c;--bad:#e25c5c;--card:#1f1f1e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px}.muted{color:var(--ink2)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:16px}
.kpi{background:var(--card);border:1px solid var(--grid);border-radius:8px;padding:10px 12px}
.kpi .v{font-size:20px;font-variant-numeric:tabular-nums}.kpi .l{color:var(--ink2);font-size:12px}
.card{background:var(--card);border:1px solid var(--grid);border-radius:8px;padding:12px;position:relative}
svg{display:block;width:100%;height:auto}.axis{fill:var(--ink2);font-size:11px}
.gl{stroke:var(--grid);stroke-width:1}.line{fill:none;stroke:var(--series);stroke-width:2}
.area{fill:var(--series);opacity:.18}.tip{position:absolute;pointer-events:none;background:var(--card);
border:1px solid var(--grid);border-radius:6px;padding:4px 8px;font-size:12px;display:none;white-space:nowrap}
.xh{stroke:var(--ink2);stroke-width:1;stroke-dasharray:3 3;display:none}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px}
th,td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--grid)}th:first-child,td:first-child{text-align:left}
th{color:var(--ink2);font-weight:500}.pos{color:var(--good)}.neg{color:var(--bad)}
.scroll{overflow-x:auto}
"""

JS = """
document.querySelectorAll('[data-series]').forEach(function(card){
  var pts=JSON.parse(card.dataset.series),svg=card.querySelector('svg'),tip=card.querySelector('.tip'),
      xh=card.querySelector('.xh'),fmt=card.dataset.fmt;
  if(!pts.length)return;
  svg.addEventListener('mousemove',function(e){
    var r=svg.getBoundingClientRect(),vb=svg.viewBox.baseVal,x=(e.clientX-r.left)*vb.width/r.width,best=pts[0];
    pts.forEach(function(p){if(Math.abs(p[0]-x)<Math.abs(best[0]-x))best=p;});
    xh.setAttribute('x1',best[0]);xh.setAttribute('x2',best[0]);xh.style.display='block';
    tip.textContent=best[2]+'  '+(fmt==='pct'?(best[3]*100).toFixed(2)+'%':best[3].toFixed(2));
    tip.style.display='block';tip.style.left=Math.min(r.width-160,(best[0]*r.width/vb.width)+12)+'px';tip.style.top='12px';
  });
  svg.addEventListener('mouseleave',function(){tip.style.display='none';xh.style.display='none';});
});
"""


def _esc(x: object) -> str:
    return html.escape(str(x))


def _num(v: object, pct: bool = False, digits: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
        return "∞" if math.isinf(v) else "—"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int) and not pct:
        return f"{v:,d}"
    if isinstance(v, int | float):
        return f"{v * 100:.{digits}f}%" if pct else f"{v:,.{digits}f}"
    return _esc(v)


def line_chart(points: list[tuple[str, float]], title: str, fmt: str = "num", area: bool = False,
               w: int = 1000, h: int = 260) -> str:
    pad_l, pad_r, pad_t, pad_b = 64, 16, 12, 28
    if len(points) < 2:
        return f'<div class="card"><p class="muted">Sin datos suficientes (n={len(points)}).</p></div>'
    ys = [p[1] for p in points]
    lo, hi = min(ys), max(ys)
    if area:
        hi = max(hi, 0.0)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    span = hi - lo
    lo, hi = lo - span * 0.05, hi + (0 if area else span * 0.05)

    def sx(i: int) -> float:
        return pad_l + i * (w - pad_l - pad_r) / (len(points) - 1)

    def sy(v: float) -> float:
        return pad_t + (hi - v) * (h - pad_t - pad_b) / (hi - lo)

    grid = []
    step = (hi - lo) / 4
    dec = max(0, min(4, -math.floor(math.log10(step * (100 if fmt == "pct" else 1))) + 1)) if step > 0 else 0
    for k in range(5):
        v = lo + k * step
        y = sy(v)
        lab = f"{v * 100:.{dec}f}%" if fmt == "pct" else f"{v:,.{dec}f}"
        grid.append(f'<line class="gl" x1="{pad_l}" x2="{w - pad_r}" y1="{y:.1f}" y2="{y:.1f}"/>'
                    f'<text class="axis" x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end">{lab}</text>')
    for i, anchor in ((0, "start"), (len(points) // 2, "middle"), (len(points) - 1, "end")):
        grid.append(f'<text class="axis" x="{sx(i):.1f}" y="{h - 8}" text-anchor="{anchor}">{_esc(points[i][0][:10])}</text>')
    path = " ".join(f"{'M' if i == 0 else 'L'}{sx(i):.1f},{sy(v):.1f}" for i, (_, v) in enumerate(points))
    area_svg = ""
    if area:
        base = sy(0.0)
        area_svg = f'<path class="area" d="{path} L{sx(len(points) - 1):.1f},{base:.1f} L{sx(0):.1f},{base:.1f} Z"/>'
    series = json.dumps([[round(sx(i), 1), 0, d[:16], v] for i, (d, v) in enumerate(points)])
    return (f'<div class="card" data-series=\'{_esc(series)}\' data-fmt="{fmt}"><h2 style="margin-top:0">{_esc(title)}</h2>'
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{_esc(title)}">{"".join(grid)}{area_svg}'
            f'<path class="line" d="{path}"/><line class="xh" y1="{pad_t}" y2="{h - pad_b}"/></svg>'
            f'<div class="tip"></div></div>')


def _table(rows: dict[str, dict], cols: list[tuple[str, str, bool]]) -> str:
    head = "".join(f"<th>{_esc(label)}</th>" for _, label, _ in cols)
    body = []
    for name, m in rows.items():
        cells = []
        for key, _, pct in cols:
            v = m.get(key)
            cls = ' class="pos"' if key == "pnl_net" and isinstance(v, float) and v > 0 else \
                (' class="neg"' if key == "pnl_net" and isinstance(v, float) and v < 0 else "")
            cells.append(f"<td{cls}>{_num(v, pct)}</td>")
        body.append(f"<tr><td>{_esc(name)}</td>{''.join(cells)}</tr>")
    return f'<div class="scroll"><table><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


DIM_COLS = [("n", "n", False), ("pnl_net", "PnL neto", False), ("expectancy_r", "Expect. R", False),
            ("win_rate", "Acierto", True), ("profit_factor", "F. benef.", False), ("max_drawdown", "Máx DD", True)]


def render(trades: list[dict], candidates: list[dict], capital: float, generated: datetime, mode: str) -> str:
    s = summarize(trades, capital)
    curve = equity_curve(trades, capital)
    eq_vals = [capital] + [e for _, e in curve]
    peak, dd_points = capital, []
    for (d, e) in curve:
        peak = max(peak, e)
        dd_points.append((d, -(1 - e / peak)))
    kpi = [("PnL neto", _num(s.get("pnl_net"))), ("Expectativa (R)", _num(s.get("expectancy_r"), digits=3)),
           ("Factor de beneficio", _num(s.get("profit_factor"))), ("Tasa de acierto", _num(s.get("win_rate"), True, 1)),
           ("Máx. drawdown", _num(s.get("max_drawdown"), True, 2)), ("Operaciones (n)", str(s.get("n", 0)))]
    kpi_html = "".join(f'<div class="kpi"><div class="v">{v}</div><div class="l">{_esc(l)}</div></div>' for l, v in kpi)
    sections = [f"<h1>Bot de perpetuos — reporte</h1><div class='muted'>Modo <b>{_esc(mode)}</b> · generado "
                f"{generated:%Y-%m-%d %H:%M} UTC · capital inicial {capital:,.0f} · equity {eq_vals[-1]:,.2f}</div>",
                f'<div class="kpis">{kpi_html}</div>',
                "<h2>Capital</h2>" + line_chart([(curve[0][0], capital)] + curve if curve else [], "Curva de capital (USDT)"),
                "<h2>Drawdown</h2>" + line_chart(dd_points, "Drawdown desde el pico", fmt="pct", area=True)]
    sections = [x.replace('<h2 style="margin-top:0">', '<h2 hidden>') for x in sections]
    if trades:
        extra = {k: s.get(k) for k in ("fees", "fees_pct_of_gross", "avg_slippage_pct", "maker_entry_rate",
                                       "avg_duration_h", "sharpe_daily", "sortino_daily", "avg_mae_r", "avg_mfe_r")}
        sections.append("<h2>Costos y ejecución</h2>" + _table({"total": {**extra, "n": s["n"]}}, [
            ("n", "n", False), ("fees", "Comisiones", False), ("fees_pct_of_gross", "Com./bruto", True),
            ("avg_slippage_pct", "Desliz. medio", True), ("maker_entry_rate", "Entradas maker", True),
            ("avg_duration_h", "Duración (h)", False), ("sharpe_daily", "Sharpe", False),
            ("sortino_daily", "Sortino", False), ("avg_mae_r", "MAE R", False), ("avg_mfe_r", "MFE R", False)]))
        for key, label in (("symbol", "Por moneda"), ("side", "Por dirección"), ("strategy", "Por estrategia"),
                           ("regime", "Por régimen"), ("hour", "Por hora de entrada (UTC)"),
                           ("exit_reason", "Por motivo de salida")):
            sections.append(f"<h2>{label}</h2>" + _table(by_dimension(trades, key, capital), DIM_COLS))
        cal = ai_calibration(trades)
        if cal:
            sections.append("<h2>Calibración de la IA</h2>" + _table(
                {c["tramo"]: c for c in cal}, [("n", "n", False), ("acierto", "Acierto real", True),
                                               ("expectativa_r", "Expect. R", False)]))
        ranked = sorted(trades, key=lambda t: float(t["r"]))
        for title, rows in (("Mejores operaciones", ranked[::-1][:5]), ("Peores operaciones", ranked[:5])):
            sections.append(f"<h2>{title}</h2>" + _table(
                {f"{t['closed_at'][:16]} {t['symbol']} {t['side']}": {
                    "r": float(t["r"]), "pnl_net": float(t["pnl_net"]), "strategy": t["strategy"],
                    "exit_reason": t["exit_reason"]} for t in rows},
                [("strategy", "Estrategia", False), ("exit_reason", "Salida", False), ("r", "R", False),
                 ("pnl_net", "PnL neto", False)]))
    rej = rejection_stats(candidates)
    if rej.get("n"):
        cf = {k: v for k, v in rej["counterfactual"].items()}
        sections.append(f"<h2>Candidatos (n={rej['n']}, rechazo {rej['rejection_rate'] * 100:.1f}%)</h2>" + _table(
            {k: {"n": v, **cf.get(k, {})} for k, v in rej["by_status"].items()},
            [("n", "n", False), ("expectativa_r", "Contrafactual R", False)]))
    return (f'<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Reporte del bot</title><style>{CSS}</style></head>'
            f'<body><main>{"".join(sections)}</main><script>{JS}</script></body></html>')


def write_report(path: Path, trades: list[dict], candidates: list[dict], capital: float, generated: datetime,
                 mode: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(trades, candidates, capital, generated, mode), encoding="utf-8")
    return path
