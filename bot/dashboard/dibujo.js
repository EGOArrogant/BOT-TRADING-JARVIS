/* JARVIS · herramientas de dibujo y capa del bot sobre el gráfico (estilo TradingView).
 *
 * - Dibujos del usuario: línea de tendencia, rayo, línea extendida, horizontal, rayo horizontal, vertical, canal
 *   paralelo, tridente (Andrews), retroceso de Fibonacci, pincel, texto, emoji, posición larga/corta, regla y zoom.
 *   Imán (pega al OHLC), bloquear, ocultar y borrar. Se guardan por moneda en este navegador (localStorage), con
 *   coordenadas de tiempo y precio: sirven en cualquier temporalidad.
 * - Capa JARVIS: cada operación del bot como caja (entrada, stop, objetivo, salida), las posiciones abiertas, las
 *   señales que descartó y los niveles que mira (rango de ruptura, soportes/resistencias, Fibonacci). Al tocar una
 *   caja o señal se ve por qué entró o por qué no, y qué dijo la IA.
 *
 * Solo dibuja: nada de aquí cambia lo que hace el bot.
 */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
  var COLORS = ["#0a84ff", "#ff9f0a", "#30d158", "#ff453a", "#bf5af2", "#8e8e93"];
  var EMOJIS = ["🚀", "🎯", "⚠️", "✅", "❌", "👀", "🔥", "💰", "📌", "⭐", "🐂", "🐻"];
  var ICON = {  // trazos de 24×24, sin relleno
    cursor: "M12 3v6M12 15v6M3 12h6M15 12h6",
    trend: "M4 20L20 4M4 20m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0M20 4m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0",
    ray: "M5 19L21 3M5 19m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0",
    xline: "M2 22L22 2",
    hline: "M3 12h18M7 12m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0",
    hray: "M7 12h14M7 12m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0",
    vline: "M12 3v18",
    channel: "M3 16L15 4M9 20L21 8M3 16m-1 0a1 1 0 1 0 2 0a1 1 0 1 0-2 0",
    pitchfork: "M3 12h6M9 12L21 5M9 12l12 7M9 12h12M3 12m-1 0a1 1 0 1 0 2 0a1 1 0 1 0-2 0",
    fib: "M3 5h18M3 9h18M3 13h18M3 19h18M5 19L19 5",
    brush: "M4 18c3-1 3-6 6-6s2 5 5 4 3-7 5-9",
    text: "M5 5h14M12 5v15M9 20h6",
    emoji: "M12 21a9 9 0 1 0 0-18a9 9 0 1 0 0 18M8.5 14.5c1.8 2 5.2 2 7 0M9 9.5h.01M15 9.5h.01",
    long: "M4 4h16v8H4zM4 12h16v8H4zM4 12h16",
    short: "M4 4h16v8H4zM4 12h16v8H4z",
    ruler: "M3 17L17 3l4 4L7 21zM7 13l2 2M10 10l2 2M13 7l2 2",
    zoom: "M10.5 17a6.5 6.5 0 1 0 0-13a6.5 6.5 0 1 0 0 13M15.5 15.5L21 21M10.5 8v5M8 10.5h5",
    magnet: "M6 4v7a6 6 0 0 0 12 0V4M6 4h4v7a2 2 0 0 0 4 0V4h4M6 8h4M14 8h4",
    lock: "M6 11h12v10H6zM8.5 11V7.5a3.5 3.5 0 0 1 7 0V11",
    unlock: "M6 11h12v10H6zM8.5 11V7.5a3.5 3.5 0 0 1 6.8-1.2",
    eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6a3 3 0 1 0 0 6",
    eyeoff: "M2 12s3.6-7 10-7c2 0 3.8.7 5.3 1.7M22 12s-3.6 7-10 7c-2 0-3.8-.7-5.3-1.7M3 3l18 18",
    trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
    jarvis: "M4 9a8 8 0 0 1 16 0v4a8 8 0 0 1-16 0zM7.5 11h3M13.5 11h3M9 16h6"
  };
  var GROUPS = [
    ["cursor", [["cursor", "Cursor"]]],
    ["lines", [["trend", "Línea de tendencia"], ["ray", "Rayo"], ["xline", "Línea extendida"]]],
    ["horiz", [["hline", "Línea horizontal"], ["hray", "Rayo horizontal"], ["vline", "Línea vertical"]]],
    ["channel", [["channel", "Canal paralelo"], ["pitchfork", "Tridente (Andrews)"]]],
    ["fib", [["fib", "Retroceso de Fibonacci"]]],
    ["brush", [["brush", "Pincel"]]],
    ["text", [["text", "Texto"]]],
    ["emoji", [["emoji", "Emoji"]]],
    ["pos", [["long", "Posición larga"], ["short", "Posición corta"]]],
    ["ruler", [["ruler", "Regla (medir)"]]],
    ["zoom", [["zoom", "Zoom: arrastra sobre la zona"]]]
  ];
  // En el celular, deslizar hacia arriba/abajo sobre el gráfico mueve la página (no el gráfico)
  var SCROLL_ON = {mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false};
  var POINTS = {trend: 2, ray: 2, xline: 2, fib: 2, ruler: 2, zoom: 2, channel: 3, pitchfork: 3};
  var LAYERS = [["trades", "Operación abierta: entrada, stop y objetivo"], ["past", "Operaciones pasadas"],
    ["signals", "Señales que descartó (y por qué)"],
    ["range", "Rango de ruptura que vigila"], ["sr", "Soportes y resistencias (1 h)"], ["fib", "Fibonacci del último tramo (1 h)"]];

  var H = null;  // ganchos de la página
  var S = {tool: "cursor", groupPick: {}, emoji: "🚀", color: COLORS[0], magnet: false, locked: false, hidden: false,
    layers: {trades: true, past: false, signals: false, range: true, sr: false, fib: false}, all: {}, sel: null, pending: null,
    temp: null, drag: null, bot: null, botSym: null, info: null, raf: 0, lastUp: 0};

  function load() {
    try {
      S.all = JSON.parse(localStorage.getItem("jarvis-dibujos") || "{}") || {};
      var c = JSON.parse(localStorage.getItem("jarvis-dibujo-cfg") || "{}");
      ["magnet", "locked", "hidden", "color", "emoji"].forEach(function (k) { if (c[k] != null) S[k] = c[k]; });
      if (c.layers) Object.keys(S.layers).forEach(function (k) { if (c.layers[k] != null) S.layers[k] = !!c.layers[k]; });
      if (c.groupPick) S.groupPick = c.groupPick;
    } catch (e) { S.all = {}; }
  }
  function save() {
    try {
      localStorage.setItem("jarvis-dibujos", JSON.stringify(S.all));
      localStorage.setItem("jarvis-dibujo-cfg", JSON.stringify({magnet: S.magnet, locked: S.locked, hidden: S.hidden,
        color: S.color, emoji: S.emoji, layers: S.layers, groupPick: S.groupPick}));
    } catch (e) {}
  }
  function list() { var s = H.sym(); return (S.all[s] = S.all[s] || []); }
  function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }

  // ------------------------------------------------------------ coordenadas (tiempo en ms UTC, precio)
  function bars() { return H.bars() || []; }
  function scale() {  // x = a + lógico × b (lineal; sirve también fuera de las velas cargadas)
    var ts = H.chart().timeScale(), a = ts.logicalToCoordinate(0), b = ts.logicalToCoordinate(1);
    if (a == null || b == null || b === a) return null;
    return {a: a, b: b - a};
  }
  function msToLogical(ms) {
    var b = bars(), n = b.length, tf = H.tfMs();
    if (!n) return null;
    if (ms <= b[0][0]) return (ms - b[0][0]) / tf;
    if (ms >= b[n - 1][0]) return n - 1 + (ms - b[n - 1][0]) / tf;
    var lo = 0, hi = n - 1;
    while (hi - lo > 1) { var m = (lo + hi) >> 1; if (b[m][0] <= ms) lo = m; else hi = m; }
    return lo + (ms - b[lo][0]) / (b[hi][0] - b[lo][0]);
  }
  function logicalToMs(l) {
    var b = bars(), n = b.length, tf = H.tfMs();
    if (!n) return null;
    if (l <= 0) return b[0][0] + l * tf;
    if (l >= n - 1) return b[n - 1][0] + (l - (n - 1)) * tf;
    var i = Math.floor(l);
    return b[i][0] + (l - i) * (b[i + 1][0] - b[i][0]);
  }
  function X(ms) { var sc = scale(), l = msToLogical(ms); return sc && l != null ? sc.a + l * sc.b : null; }
  function Y(p) { var v = H.main().priceToCoordinate(p); return v == null ? null : v; }
  function toPoint(x, y) {
    var sc = scale(); if (!sc) return null;
    var l = (x - sc.a) / sc.b, p = H.main().coordinateToPrice(y);
    if (p == null) return null;
    if (S.magnet) {  // imán: pega al apertura/máximo/mínimo/cierre de la vela más cercana
      var b = bars(), i = Math.round(l);
      if (i >= 0 && i < b.length) {
        l = i;
        var best = null;
        [1, 2, 3, 4].forEach(function (k) { if (best == null || Math.abs(b[i][k] - p) < Math.abs(best - p)) best = b[i][k]; });
        p = best;
      }
    }
    return [logicalToMs(l), p];
  }
  function pane() {
    var c = H.chart(), el = H.el();
    return {w: el.clientWidth - c.priceScale("right").width(), h: el.clientHeight - c.timeScale().height()};
  }
  function barMs() { return H.tfMs(); }

  // ------------------------------------------------------------ SVG
  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function txt(parent, x, y, s, attrs) {
    var t = el("text", Object.assign({x: x, y: y, "font-size": 11, "font-family": "Inter,-apple-system,sans-serif"}, attrs || {}), parent);
    t.textContent = s;
    return t;
  }
  function edge(x1, y1, x2, y2, w, h, both) {  // prolonga el segmento hasta el borde (rayo o línea extendida)
    var dx = x2 - x1, dy = y2 - y1, L = (w + h) * 4 / (Math.hypot(dx, dy) || 1);
    return both ? [x1 - dx * L, y1 - dy * L, x2 + dx * L, y2 + dy * L] : [x1, y1, x2 + dx * L, y2 + dy * L];
  }
  function pct(a, b) { return (b - a) / a * 100; }
  function fmtPct(v) { return (v >= 0 ? "+" : "") + v.toFixed(2) + " %"; }
  function dur(ms) {
    var m = Math.abs(ms) / 6e4;
    if (m < 60) return Math.round(m) + " min";
    if (m < 2880) return (m / 60).toFixed(m < 600 ? 1 : 0) + " h";
    return (m / 1440).toFixed(1) + " días";
  }

  // ------------------------------------------------------------ dibujar una figura del usuario
  function shape(g, d, live) {
    var P = pane(), pts = d.pts.map(function (p) { return [X(p[0]), Y(p[1])]; });
    if (pts.some(function (p) { return p[0] == null || p[1] == null; })) return;
    var col = d.color || S.color, sw = d.id === S.sel ? 2.4 : 1.6;
    var hitA = {"stroke": "transparent", "stroke-width": 14, "fill": "none", "class": "jd-hit", "data-id": d.id};
    function line(x1, y1, x2, y2, extra) {
      el("line", Object.assign({x1: x1, y1: y1, x2: x2, y2: y2, stroke: col, "stroke-width": sw}, extra || {}), g);
      if (!live) el("line", Object.assign({x1: x1, y1: y1, x2: x2, y2: y2}, hitA), g);
    }
    var a = pts[0], b = pts[1], c = pts[2], e;
    switch (d.type) {
      case "trend": line(a[0], a[1], b[0], b[1]); break;
      case "ray": e = edge(a[0], a[1], b[0], b[1], P.w, P.h, false); line(e[0], e[1], e[2], e[3]); break;
      case "xline": e = edge(a[0], a[1], b[0], b[1], P.w, P.h, true); line(e[0], e[1], e[2], e[3]); break;
      case "hline":
        line(-10, a[1], P.w + 10, a[1]);
        txt(g, P.w - 4, a[1] - 4, H.price(d.pts[0][1]), {fill: col, "text-anchor": "end"});
        break;
      case "hray": line(a[0], a[1], P.w + 10, a[1]); txt(g, P.w - 4, a[1] - 4, H.price(d.pts[0][1]), {fill: col, "text-anchor": "end"}); break;
      case "vline": line(a[0], -10, a[0], P.h + 10); break;
      case "channel":
        if (!c) { line(a[0], a[1], b[0], b[1]); break; }
        var dy = c[1] - (a[1] + (b[1] - a[1]) * ((c[0] - a[0]) / ((b[0] - a[0]) || 1)));
        el("polygon", {points: [a[0], a[1], b[0], b[1], b[0], b[1] + dy, a[0], a[1] + dy].join(" "), fill: col, "fill-opacity": 0.08}, g);
        line(a[0], a[1], b[0], b[1]); line(a[0], a[1] + dy, b[0], b[1] + dy);
        line(a[0], a[1] + dy / 2, b[0], b[1] + dy / 2, {"stroke-dasharray": "4 4", "stroke-width": 1});
        break;
      case "pitchfork":
        if (!c) { line(a[0], a[1], b[0], b[1]); break; }
        var mx = (b[0] + c[0]) / 2, my = (b[1] + c[1]) / 2, vx = mx - a[0], vy = my - a[1], k = (P.w + P.h) * 4 / (Math.hypot(vx, vy) || 1);
        line(a[0], a[1], mx + vx * k, my + vy * k);
        line(b[0], b[1], b[0] + vx * k, b[1] + vy * k); line(c[0], c[1], c[0] + vx * k, c[1] + vy * k);
        line(b[0], b[1], c[0], c[1], {"stroke-dasharray": "4 4", "stroke-width": 1});
        el("polygon", {points: [b[0], b[1], b[0] + vx * k, b[1] + vy * k, c[0] + vx * k, c[1] + vy * k, c[0], c[1]].join(" "), fill: col, "fill-opacity": 0.06}, g);
        break;
      case "fib":
        var p1 = d.pts[0][1], p2 = d.pts[1][1], x1 = Math.min(a[0], b[0]), x2 = Math.max(a[0], b[0]) + 60;
        FIB.forEach(function (r, i) {
          var pr = p2 - (p2 - p1) * r, y = Y(pr), clr = r === 0.618 || r === 0.5 ? "#ff9f0a" : col;
          if (y == null) return;
          if (i < FIB.length - 1) {
            var yn = Y(p2 - (p2 - p1) * FIB[i + 1]);
            if (yn != null) el("rect", {x: x1, y: Math.min(y, yn), width: x2 - x1, height: Math.abs(yn - y), fill: clr, "fill-opacity": 0.05}, g);
          }
          line(x1, y, x2, y, {stroke: clr, "stroke-width": 1});
          txt(g, x1 + 3, y - 3, r.toFixed(3).replace(/0+$/, "").replace(/\.$/, "") + "  " + H.price(pr), {fill: clr, "font-size": 10.5});
        });
        line(a[0], a[1], b[0], b[1], {"stroke-dasharray": "3 4", "stroke-width": 1});
        break;
      case "brush":
        var path = pts.map(function (p, i) { return (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); }).join("");
        el("path", {d: path, stroke: col, "stroke-width": sw + 0.6, fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round"}, g);
        if (!live) el("path", Object.assign({d: path}, hitA), g);
        break;
      case "text":
        var t = txt(g, a[0], a[1], d.text || "", {fill: col, "font-size": 14, "font-weight": 600});
        if (!live) { t.setAttribute("class", "jd-hit"); t.setAttribute("data-id", d.id); }
        break;
      case "emoji":
        var em = txt(g, a[0], a[1], d.text || "📌", {"font-size": 22, "text-anchor": "middle", "dominant-baseline": "central"});
        if (!live) { em.setAttribute("class", "jd-hit"); em.setAttribute("data-id", d.id); }
        break;
      case "long": case "short": posBox(g, d, pts, live, hitA); break;
      case "ruler": ruler(g, d, a, b); break;
      case "zoom":
        el("rect", {x: Math.min(a[0], b[0]), y: 0, width: Math.abs(b[0] - a[0]), height: P.h, fill: "#0a84ff", "fill-opacity": 0.12,
          stroke: "#0a84ff", "stroke-dasharray": "4 3"}, g);
        break;
    }
    if (d.id === S.sel && !live) handles(g, d, pts);
  }

  function posBox(g, d, pts, live, hitA) {
    var a = pts[0], xe = pts[1][0], ys = pts[1][1], yt = pts[2][1], entry = d.pts[0][1], stop = d.pts[1][1], tgt = d.pts[2][1];
    var good = H.cssv("--good"), bad = H.cssv("--bad"), x = Math.min(a[0], xe), w = Math.abs(xe - a[0]);
    el("rect", {x: x, y: Math.min(a[1], yt), width: w, height: Math.abs(yt - a[1]), fill: good, "fill-opacity": 0.16}, g);
    el("rect", {x: x, y: Math.min(a[1], ys), width: w, height: Math.abs(ys - a[1]), fill: bad, "fill-opacity": 0.16}, g);
    el("line", {x1: x, y1: a[1], x2: x + w, y2: a[1], stroke: H.cssv("--ink2"), "stroke-width": 1.2}, g);
    if (!live) el("rect", Object.assign({x: x, y: Math.min(yt, ys), width: w, height: Math.abs(yt - ys)}, hitA, {"stroke-width": 1}), g);
    var risk = Math.abs(entry - stop), rr = risk ? Math.abs(tgt - entry) / risk : 0;
    txt(g, x + 4, yt < a[1] ? yt + 13 : yt - 4, "Objetivo " + H.price(tgt) + " (" + fmtPct(pct(entry, tgt)) + ") · R:R " + rr.toFixed(2),
      {fill: good, "font-size": 10.5, "font-weight": 600});
    txt(g, x + 4, ys > a[1] ? ys - 4 : ys + 13, "Stop " + H.price(stop) + " (" + fmtPct(pct(entry, stop)) + ")", {fill: bad, "font-size": 10.5, "font-weight": 600});
    txt(g, x + 4, a[1] - 4, (d.type === "long" ? "Largo" : "Corto") + " · entrada " + H.price(entry), {fill: H.cssv("--ink2"), "font-size": 10.5});
  }

  function ruler(g, d, a, b) {
    var p1 = d.pts[0][1], p2 = d.pts[1][1], up = p2 >= p1, col = up ? H.cssv("--good") : H.cssv("--bad");
    var nb = Math.round(msToLogical(d.pts[1][0]) - msToLogical(d.pts[0][0]));
    el("rect", {x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), width: Math.abs(b[0] - a[0]), height: Math.abs(b[1] - a[1]),
      fill: col, "fill-opacity": 0.14, stroke: col, "stroke-width": 1}, g);
    el("line", {x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: col, "stroke-width": 1, "stroke-dasharray": "3 3"}, g);
    var lbl = H.price(p2 - p1).replace(/^(?!-)/, "+") + "  (" + fmtPct(pct(p1, p2)) + ")  ·  " + Math.abs(nb) + " velas, " + dur(d.pts[1][0] - d.pts[0][0]);
    var tx = (a[0] + b[0]) / 2, ty = up ? Math.min(a[1], b[1]) - 10 : Math.max(a[1], b[1]) + 18;
    var w = lbl.length * 6.1 + 14;
    el("rect", {x: tx - w / 2, y: ty - 13, width: w, height: 19, rx: 6, fill: col}, g);
    txt(g, tx, ty, lbl, {fill: "#fff", "text-anchor": "middle", "font-size": 11, "font-weight": 600});
  }

  function handles(g, d, pts) {
    pts.forEach(function (p, i) {
      if ((d.type === "long" || d.type === "short") && i > 0) p = [i === 1 ? pts[1][0] : pts[0][0] + (pts[1][0] - pts[0][0]) / 2, p[1]];
      el("circle", {cx: p[0], cy: p[1], r: 6, fill: H.cssv("--card"), stroke: d.color || S.color, "stroke-width": 2, "class": "jd-handle",
        "data-id": d.id, "data-i": i}, g);
    });
  }

  // ------------------------------------------------------------ capa JARVIS (lo que hace el bot)
  function botLayer(g) {
    var P = pane(), good = H.cssv("--good"), bad = H.cssv("--bad"), blue = H.cssv("--series"), warn = H.cssv("--warn") || "#ff9f0a";
    var ink3 = H.cssv("--ink3"), sym = H.sym(), lv = (H.levels() || {})[sym], hits = [];
    var now = Date.now();
    var hasOpen = (H.positions() || []).some(function (p) { return p.symbol === sym; });
    if (lv && S.layers.range && lv.range_15m && !hasOpen) {  // con posición abierta no busca otra entrada aquí
      var r = lv.range_15m, xs = X(r.from), xe = P.w;
      [[r.high, "LONG si una vela de 15 min cierra arriba de " + H.price(r.high), good], [r.low, "SHORT si cierra abajo de " + H.price(r.low), bad]].forEach(function (l) {
        var y = Y(l[0]);
        if (y == null || xs == null) return;
        el("line", {x1: Math.max(xs, -10), y1: y, x2: xe, y2: y, stroke: l[2], "stroke-width": 1.2, "stroke-dasharray": "6 4", "stroke-opacity": 0.9}, g);
        txt(g, Math.max(xs, 4) + 2, y + (l[0] === r.high ? -4 : 12), l[1], {fill: l[2], "font-size": 10.5});
      });
    }
    if (lv && S.layers.sr) {
      (lv.res || []).forEach(function (p) { var y = Y(p); if (y == null) return;
        el("line", {x1: 0, y1: y, x2: P.w, y2: y, stroke: bad, "stroke-width": 1, "stroke-dasharray": "2 5", "stroke-opacity": 0.8}, g);
        txt(g, 6, y - 3, "Resistencia " + H.price(p), {fill: bad, "font-size": 10}); });
      (lv.sup || []).forEach(function (p) { var y = Y(p); if (y == null) return;
        el("line", {x1: 0, y1: y, x2: P.w, y2: y, stroke: good, "stroke-width": 1, "stroke-dasharray": "2 5", "stroke-opacity": 0.8}, g);
        txt(g, 6, y + 11, "Soporte " + H.price(p), {fill: good, "font-size": 10}); });
    }
    if (lv && S.layers.fib && lv.swing_high && lv.swing_low) {
      var hi = lv.swing_high, lo = lv.swing_low, first = hi[0] < lo[0] ? hi : lo, last = hi[0] < lo[0] ? lo : hi;
      var x1 = X(first[0]);
      if (x1 != null) FIB.forEach(function (rr) {
        var pr = last[1] - (last[1] - first[1]) * rr, y = Y(pr);
        if (y == null) return;
        el("line", {x1: x1, y1: y, x2: P.w, y2: y, stroke: warn, "stroke-width": rr === 0.618 || rr === 0.5 ? 1.2 : 0.8, "stroke-opacity": 0.75}, g);
        txt(g, P.w - 6, y - 3, "Fib " + rr + " · " + H.price(pr), {fill: warn, "font-size": 10, "text-anchor": "end"});
      });
    }
    var data = S.bot && S.botSym === sym ? S.bot : null;
    if (data && S.layers.signals) data.signals.forEach(function (c) {
      if (c.traded) return;
      var x = X(Date.parse(c.ts)), y = Y(c.entry);
      if (x == null || y == null || x < -10 || x > P.w + 10) return;
      var col = c.status === "La IA dijo que no" ? warn : ink3;
      el("path", {d: "M" + x + " " + (y - 6) + "l6 6-6 6-6-6z", fill: col, "fill-opacity": 0.85, stroke: H.cssv("--card"), "stroke-width": 1}, g);
      hits.push({kind: "signal", x1: x - 8, x2: x + 8, y1: y - 9, y2: y + 9, item: c});
    });
    if (S.layers.trades) {
      var boxes = [];
      if (data && S.layers.past) data.trades.forEach(function (t) { boxes.push({t: t, open: false}); });
      (H.positions() || []).filter(function (p) { return p.symbol === sym; }).forEach(function (p) { boxes.push({t: p, open: true}); });
      boxes.forEach(function (bx) {
        var t = bx.t, t0 = Date.parse(t.opened_at), t1 = bx.open ? Math.max(now, t0 + (t.max_hold_hours || 6) * 36e5) : Date.parse(t.closed_at);
        var x0 = X(t0), x1 = X(t1), ye = Y(t.entry), stop0 = bx.open ? (t.initial_stop || t.stop) : t.initial_stop;
        if (x0 == null || x1 == null || ye == null || x1 < -20 || x0 > P.w + 20) return;
        x1 = Math.max(x1, x0 + 6);
        var w = x1 - x0, ys = stop0 ? Y(stop0) : null, yt = t.take_profit ? Y(t.take_profit) : null;
        if (yt != null) el("rect", {x: x0, y: Math.min(ye, yt), width: w, height: Math.abs(yt - ye), fill: good, "fill-opacity": bx.open ? 0.18 : 0.11}, g);
        if (ys != null) el("rect", {x: x0, y: Math.min(ye, ys), width: w, height: Math.abs(ys - ye), fill: bad, "fill-opacity": bx.open ? 0.18 : 0.11}, g);
        el("line", {x1: x0, y1: ye, x2: x1, y2: ye, stroke: blue, "stroke-width": 1.4}, g);
        if (bx.open && t.stop && t.stop !== stop0) {  // stop actual (movido por parcial o trailing)
          var yc = Y(t.stop);
          if (yc != null) { el("line", {x1: x0, y1: yc, x2: x1, y2: yc, stroke: bad, "stroke-width": 1.4, "stroke-dasharray": "5 3"}, g);
            txt(g, x0 + 4, yc - 3, "stop ahora " + H.price(t.stop), {fill: bad, "font-size": 10}); }
        }
        if (bx.open && t.invalidation_price) {
          var yi = Y(t.invalidation_price);
          if (yi != null) { el("line", {x1: x0, y1: yi, x2: x1, y2: yi, stroke: warn, "stroke-width": 1, "stroke-dasharray": "2 3"}, g);
            txt(g, x0 + 4, yi - 3, "invalidación " + H.price(t.invalidation_price), {fill: warn, "font-size": 10}); }
        }
        if (!bx.open && t.exit) {
          var yx = Y(t.exit), cl = t.pnl_net >= 0 ? good : bad;
          if (yx != null) {
            el("line", {x1: x0, y1: ye, x2: x1, y2: yx, stroke: cl, "stroke-width": 1.2, "stroke-dasharray": "3 3"}, g);
            el("circle", {cx: x1, cy: yx, r: 4, fill: cl, stroke: H.cssv("--card"), "stroke-width": 1.5}, g);
          }
        }
        var lbl = (t.side === "LONG" ? "▲ LONG" : "▼ SHORT") + (bx.open ? " abierta" : " " + H.money(t.pnl_net, true) + (t.r != null ? " (" + (t.r >= 0 ? "+" : "") + t.r.toFixed(2) + "R)" : ""));
        var yl = Math.max(12, Math.min(P.h - 4, Math.min(ye, ys == null ? ye : ys, yt == null ? ye : yt) - 5));
        txt(g, x0 + 3, yl, lbl, {fill: bx.open ? blue : (t.pnl_net >= 0 ? good : bad), "font-size": 11, "font-weight": 700});
        var ys2 = [ye, ys, yt].filter(function (v) { return v != null; });
        hits.push({kind: bx.open ? "open" : "trade", x1: x0, x2: x1, y1: Math.min.apply(null, ys2) - 16, y2: Math.max.apply(null, ys2) + 4, item: t});
      });
    }
    S.hits = hits;
  }

  // ------------------------------------------------------------ pintar todo
  function redraw() {
    S.raf = 0;
    if (!H || !H.chart() || !S.svg) return;
    var P = pane(), svg = S.svg;
    svg.setAttribute("width", P.w); svg.setAttribute("height", P.h);
    svg.innerHTML = "";
    var clip = el("clipPath", {id: "jdclip"}, el("defs", {}, svg));
    el("rect", {x: 0, y: 0, width: P.w, height: P.h}, clip);
    var root = el("g", {"clip-path": "url(#jdclip)"}, svg);
    if (scale() && bars().length) {
      botLayer(el("g", {"class": "jd-bot"}, root));
      if (!S.hidden) {
        var ug = el("g", {"class": "jd-user" + (S.locked ? " locked" : "")}, root);
        list().forEach(function (d) { shape(ug, d, false); });
      }
      if (S.temp) shape(root, S.temp, true);
      if (S.pending && S.pending.preview) shape(root, S.pending.preview, true);
    }
    // capa que captura el puntero mientras hay una herramienta elegida
    if (S.tool !== "cursor") el("rect", {x: 0, y: 0, width: P.w, height: P.h, fill: "transparent", "class": "jd-cap"}, svg);
    renderBar();
  }
  function schedule() { if (!S.raf) S.raf = requestAnimationFrame(redraw); }

  // ------------------------------------------------------------ barra de herramientas
  function btn(name, title, pressed, extra) {
    return '<button type="button" class="jd-b' + (extra || "") + '" data-a="' + name + '" title="' + title + '" aria-label="' + title + '" aria-pressed="' + pressed +
      '"><svg viewBox="0 0 24 24" width="20" height="20"><path d="' + ICON[name] + '"/></svg></button>';
  }
  function renderBar() {
    var bar = S.bar; if (!bar) return;
    var key = [S.tool, S.magnet, S.locked, S.hidden, JSON.stringify(S.groupPick), S.sel, S.color].join("|");
    if (bar.dataset.k === key) return;
    bar.dataset.k = key;
    var html = GROUPS.map(function (gr) {
      var pick = S.groupPick[gr[0]] || gr[1][0][0], t = gr[1].filter(function (x) { return x[0] === pick; })[0] || gr[1][0];
      var on = gr[1].some(function (x) { return x[0] === S.tool; });
      return btn(on ? S.tool : t[0], t[1], on, gr[1].length > 1 ? " jd-more" : "").replace('data-a="', 'data-g="' + gr[0] + '" data-a="');
    }).join("");
    html += '<span class="jd-sep"></span>' + btn("magnet", "Imán: pegar a apertura, máximo, mínimo o cierre", S.magnet) +
      btn(S.locked ? "lock" : "unlock", S.locked ? "Dibujos bloqueados" : "Bloquear dibujos", S.locked) +
      btn(S.hidden ? "eyeoff" : "eye", S.hidden ? "Mostrar dibujos" : "Ocultar dibujos", S.hidden) +
      btn("trash", S.sel ? "Borrar el dibujo elegido" : "Borrar todos los dibujos de esta moneda", false) +
      '<span class="jd-sep"></span>' + btn("jarvis", "Lo que hace JARVIS en el gráfico", false, " jd-jarvis");
    bar.innerHTML = html;
    var sel = S.sel && list().filter(function (d) { return d.id === S.sel; })[0];
    S.colors.hidden = !sel;
    if (sel) S.colors.innerHTML = COLORS.map(function (c) {
      return '<button type="button" data-c="' + c + '" style="background:' + c + '" aria-label="Color" aria-pressed="' + ((sel.color || S.color) === c) + '"></button>';
    }).join("") + '<button type="button" data-del="1" class="jd-del" aria-label="Borrar">🗑</button>';
  }
  function menu(anchor, html, onPick) {
    var m = S.menu;
    m.innerHTML = html; m.hidden = false;
    var r = anchor.getBoundingClientRect(), w = S.wrap.getBoundingClientRect();
    m.style.left = (r.right - w.left + 6) + "px";
    m.style.top = Math.max(0, Math.min(r.top - w.top, w.height - m.offsetHeight - 4)) + "px";
    m.onclick = function (e) { var b = e.target.closest("[data-v]"); if (b) onPick(b.dataset.v, b); };
  }
  function closeMenu() { if (S.menu) S.menu.hidden = true; }

  function setTool(t) {
    S.tool = t; S.pending = null;
    if (t !== "cursor") S.sel = null;
    var c = H.chart();
    c.applyOptions({handleScroll: t === "cursor" ? SCROLL_ON : false, handleScale: t === "cursor"});
    S.wrap.classList.toggle("jd-drawing", t !== "cursor");
    S.svg.classList.toggle("jd-active", t !== "cursor");
    schedule();
  }

  function onBar(e) {
    var b = e.target.closest("button"); if (!b) return;
    var a = b.dataset.a, gname = b.dataset.g;
    closeMenu();
    if (gname) {
      var gr = GROUPS.filter(function (x) { return x[0] === gname; })[0];
      if (gname === "emoji") {
        menu(b, '<div class="jd-emojis">' + EMOJIS.map(function (x) { return '<button type="button" data-v="' + x + '">' + x + "</button>"; }).join("") + "</div>",
          function (v) { S.emoji = v; save(); closeMenu(); setTool("emoji"); });
        return;
      }
      if (gr[1].length > 1) {  // grupo con variantes: siempre se elige de la lista
        menu(b, gr[1].map(function (x) { return '<button type="button" data-v="' + x[0] + '"><svg viewBox="0 0 24 24" width="18" height="18"><path d="' + ICON[x[0]] + '"/></svg>' + x[1] + "</button>"; }).join(""),
          function (v) { S.groupPick[gname] = v; save(); closeMenu(); setTool(v); });
        return;
      }
      setTool(S.tool === a && a !== "cursor" ? "cursor" : a);
      return;
    }
    if (a === "magnet") S.magnet = !S.magnet;
    else if (a === "lock" || a === "unlock") { S.locked = !S.locked; S.sel = null; }
    else if (a === "eye" || a === "eyeoff") { S.hidden = !S.hidden; S.sel = null; }
    else if (a === "trash") {
      if (S.sel) remove(S.sel);
      else if (list().length && confirm("¿Borrar todos tus dibujos de " + H.coin(H.sym()) + "?")) S.all[H.sym()] = [];
    } else if (a === "jarvis") {
      menu(b, "<h4>Lo que hace JARVIS</h4>" + LAYERS.map(function (l) {
        return '<label data-v="' + l[0] + '"><input type="checkbox"' + (S.layers[l[0]] ? " checked" : "") + "> " + l[1] + "</label>";
      }).join("") + '<p class="jd-tip">Toca una caja o un rombo del gráfico para ver por qué entró o por qué no.</p>', function (v, el2) {
        var cb = el2.querySelector("input"); S.layers[v] = cb.checked; save(); schedule(); if (H.onLayers) H.onLayers();
      });
      return;
    }
    save(); schedule();
  }
  function remove(id) { S.all[H.sym()] = list().filter(function (d) { return d.id !== id; }); S.sel = null; save(); schedule(); }

  // ------------------------------------------------------------ puntero: crear, elegir y mover dibujos
  function local(e) { var r = S.svg.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }
  function finish(d) {
    if (d.type === "zoom") {
      var l1 = msToLogical(d.pts[0][0]), l2 = msToLogical(d.pts[1][0]);
      if (Math.abs(l2 - l1) >= 2) H.chart().timeScale().setVisibleLogicalRange({from: Math.min(l1, l2), to: Math.max(l1, l2)});
      setTool("cursor"); return;
    }
    if (d.type === "ruler") { S.temp = d; setTool("cursor"); return; }
    d.id = uid(); d.color = S.color;
    list().push(d); S.sel = d.id; save(); setTool("cursor");
  }
  function defaultsFor(type, p) {
    if (type === "long" || type === "short") {
      var b = bars().slice(-14), rng = b.length ? b.reduce(function (s, x) { return s + (x[2] - x[3]); }, 0) / b.length : p[1] * 0.01;
      var risk = Math.max(rng * 1.5, p[1] * 0.002), sg = type === "long" ? 1 : -1, end = p[0] + 20 * barMs();
      return {type: type, pts: [p, [end, p[1] - sg * risk], [end, p[1] + sg * risk * 2]]};
    }
    return null;
  }
  function onDown(e) {
    if (e.button > 0) return;
    var t = e.target, pt = local(e);
    if (S.tool !== "cursor") {
      e.preventDefault(); S.svg.setPointerCapture(e.pointerId);
      var p = toPoint(pt[0], pt[1]); if (!p) return;
      var tool = S.tool;
      if (tool === "brush") { S.pending = {type: "brush", pts: [p], preview: {type: "brush", pts: [p]}, down: pt}; return; }
      if (POINTS[tool]) {
        if (!S.pending) S.pending = {type: tool, pts: [p], down: pt, preview: {type: tool, pts: [p, p]}};
        S.pending.down = pt;
        return;
      }
      S.pending = {type: tool, single: true};
      return;
    }
    // cursor: ¿tocó un dibujo o un punto de control?
    if (S.locked || S.hidden) return;
    var id = t.getAttribute && t.getAttribute("data-id");
    if (!id) return;
    e.preventDefault(); e.stopPropagation();
    S.svg.classList.add("jd-active"); S.svg.setPointerCapture(e.pointerId);
    var d = list().filter(function (x) { return x.id === id; })[0]; if (!d) return;
    S.sel = id;
    S.drag = {d: d, i: t.hasAttribute("data-i") ? +t.getAttribute("data-i") : -1, start: toPoint(pt[0], pt[1]),
      orig: JSON.parse(JSON.stringify(d.pts)), moved: false};
    H.chart().applyOptions({handleScroll: false, handleScale: false});  // mientras se arrastra un dibujo
    schedule();
  }
  function onMove(e) {
    var pt = local(e);
    if (S.drag) {
      var p = toPoint(pt[0], pt[1]); if (!p || !S.drag.start) return;
      var dr = S.drag, d = dr.d, dt = p[0] - dr.start[0], dp = p[1] - dr.start[1];
      dr.moved = true;
      if (dr.i < 0) d.pts = dr.orig.map(function (q) { return [q[0] + dt, q[1] + dp]; });
      else if (d.type === "long" || d.type === "short") {
        if (dr.i === 0) d.pts = dr.orig.map(function (q) { return [q[0] + dt, q[1] + dp]; });
        else if (dr.i === 1) { d.pts[1] = [Math.max(p[0], d.pts[0][0] + barMs()), p[1]]; d.pts[2][0] = d.pts[1][0]; }
        else d.pts[2] = [d.pts[1][0], p[1]];
      } else d.pts[dr.i] = p;
      schedule(); return;
    }
    var pe = S.pending;
    if (!pe || S.tool === "cursor") return;
    var q = toPoint(pt[0], pt[1]); if (!q) return;
    if (pe.type === "brush") {
      if (e.buttons && Math.hypot(pt[0] - pe.last0, pt[1] - pe.last1) > 2 || pe.last0 == null) { pe.pts.push(q); pe.last0 = pt[0]; pe.last1 = pt[1]; }
      pe.preview = {type: "brush", pts: pe.pts.slice()};
    } else if (pe.preview) pe.preview = {type: pe.type, pts: pe.pts.concat([q])};
    schedule();
  }
  function onUp(e) {
    var pt = local(e);
    if (S.drag) {
      var moved = S.drag.moved; S.drag = null; S.svg.classList.remove("jd-active");
      H.chart().applyOptions({handleScroll: SCROLL_ON, handleScale: true});
      if (moved) save();
      schedule(); return;
    }
    var pe = S.pending;
    if (!pe || S.tool === "cursor") return;
    var p = toPoint(pt[0], pt[1]); if (!p) return;
    if (pe.single) {
      S.pending = null;
      if (S.tool === "text") {
        var s = prompt("Texto para el gráfico:", "");
        if (s && s.trim()) finish({type: "text", pts: [p], text: s.trim().slice(0, 80)}); else setTool("cursor");
      } else if (S.tool === "emoji") finish({type: "emoji", pts: [p], text: S.emoji});
      else if (S.tool === "long" || S.tool === "short") finish(defaultsFor(S.tool, p));
      else finish({type: S.tool, pts: [p]});
      return;
    }
    if (pe.type === "brush") { S.pending = null; if (pe.pts.length > 1) finish({type: "brush", pts: pe.pts}); else setTool("cursor"); return; }
    var dragged = pe.down && Math.hypot(pt[0] - pe.down[0], pt[1] - pe.down[1]) > 6;
    if (dragged || pe.clicked) pe.pts.push(p);
    pe.clicked = true;
    if (pe.pts.length >= POINTS[pe.type]) { S.pending = null; finish({type: pe.type, pts: pe.pts.slice(0, POINTS[pe.type])}); }
    else pe.preview = {type: pe.type, pts: pe.pts.concat([p])};
    schedule();
  }

  // ------------------------------------------------------------ información al tocar la capa JARVIS
  function onChartClick(param) {
    if (S.tool !== "cursor" || !param.point) return;
    if (S.temp) { S.temp = null; schedule(); }
    if (S.sel && !S.drag) { S.sel = null; schedule(); }
    var x = param.point.x, y = param.point.y, hit = null;
    (S.hits || []).forEach(function (h) { if (x >= h.x1 && x <= h.x2 && y >= h.y1 && y <= h.y2 && (!hit || h.kind === "signal")) hit = h; });
    if (!hit) { S.info.hidden = true; return; }
    S.info.innerHTML = '<button type="button" class="jd-x" aria-label="Cerrar">✕</button>' + describe(hit);
    S.info.hidden = false;
    S.info.querySelector(".jd-x").onclick = function () { S.info.hidden = true; };
  }
  function aiBlock(ai) {
    if (!ai || !ai.thesis) return "";
    var verdict = ai.action === "NO_TRADE" ? "dijo que NO" : "aprobó " + ai.action;
    return '<div class="jd-ai"><b>La IA ' + H.esc(verdict) + "</b>" + (ai.confidence != null ? " · confianza " + Math.round(ai.confidence * 100) + " %" : "") +
      (ai.size_multiplier != null && ai.action !== "NO_TRADE" ? " · tamaño ×" + ai.size_multiplier : "") +
      "<p>" + H.esc(ai.thesis) + "</p>" +
      ((ai.key_evidence || []).length ? "<ul>" + ai.key_evidence.slice(0, 4).map(function (k) { return "<li>" + H.esc(k) + "</li>"; }).join("") + "</ul>" : "") +
      (ai.invalidation ? "<p><b>Se anula si:</b> " + H.esc(ai.invalidation) + "</p>" : "") +
      ((ai.risk_flags || []).length ? '<p class="muted">Riesgos que vio: ' + H.esc(ai.risk_flags.slice(0, 3).join("; ")) + "</p>" : "") + "</div>";
  }
  function row(k, v) { return "<tr><td>" + k + "</td><td><b>" + v + "</b></td></tr>"; }
  function describe(h) {
    var t = h.item, side = t.side === "LONG" ? "LONG (a la subida)" : "SHORT (a la baja)", strat = H.strategy(t.strategy);
    var when = new Date(Date.parse(h.kind === "signal" ? t.ts : t.opened_at)).toLocaleString("es-MX", {dateStyle: "short", timeStyle: "short"});
    if (h.kind === "signal") {
      return "<h4>Señal " + H.esc(side) + " · " + H.esc(t.status) + "</h4><p class=\"muted\">" + H.esc(when) + " · " + H.esc(strat) + "</p><table class=\"keep-table\">" +
        row("Entrada propuesta", H.price(t.entry)) + row("Stop", H.price(t.stop)) + (t.take_profit ? row("Objetivo", H.price(t.take_profit)) : "") +
        (t.level ? row(t.side === "LONG" ? "Rompió el máximo de 6 h" : "Rompió el mínimo de 6 h", H.price(t.level)) : "") + "</table>" +
        ((t.reasons || []).length ? "<p><b>Por qué no entró:</b> " + H.esc(t.reasons.join("; ")) + "</p>" : "") + aiBlock(t.ai);
    }
    var risk = t.entry && (t.initial_stop || t.stop) ? Math.abs(t.entry - (t.initial_stop || t.stop)) : null;
    var rr = risk && t.take_profit ? Math.abs(t.take_profit - t.entry) / risk : null;
    return "<h4>" + (h.kind === "open" ? "Operación abierta " : "Operación ") + H.esc(side) + "</h4><p class=\"muted\">" + H.esc(when) + " · " + H.esc(strat) + "</p><table class=\"keep-table\">" +
      row("Entrada", H.price(t.entry)) + row("Stop inicial", H.price(t.initial_stop || t.stop)) +
      (h.kind === "open" && t.stop !== t.initial_stop ? row("Stop ahora", H.price(t.stop)) : "") +
      (t.take_profit ? row("Objetivo", H.price(t.take_profit) + (rr ? " (" + rr.toFixed(1) + " a 1)" : "")) : "") +
      (t.level ? row(t.side === "LONG" ? "Rompió el máximo de 6 h" : "Rompió el mínimo de 6 h", H.price(t.level)) : "") +
      (h.kind === "open" ? (t.pnl_unrealized != null ? row("Va", H.money(t.pnl_unrealized, true)) : "") :
        row("Salida", H.price(t.exit) + " · " + H.esc(t.exit_reason)) + row("Resultado", H.money(t.pnl_net, true) + " (" + (t.r >= 0 ? "+" : "") + t.r.toFixed(2) + "R)") +
        (t.mfe_r != null ? row("Lo mejor / peor que llegó", "+" + (+t.mfe_r).toFixed(2) + "R / " + (+t.mae_r).toFixed(2) + "R") : "")) +
      (t.ai_confidence != null && !(t.ai && t.ai.thesis) ? row("Confianza de la IA", Math.round(t.ai_confidence * 100) + " %") : "") +
      "</table>" + (h.kind === "open" && t.invalidation ? "<p><b>Se cierra antes si:</b> " + H.esc(t.invalidation) + "</p>" : "") + aiBlock(t.ai);
  }

  function fetchBot(force) {
    var sym = H.sym();
    if (!force && S.botSym === sym && S.botAt && Date.now() - S.botAt < 6e4) return;
    S.botAt = Date.now();
    fetch("/api/senales?symbol=" + encodeURIComponent(sym), {cache: "no-store"})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) { if (j.symbol === H.sym()) { S.bot = j; S.botSym = j.symbol; schedule(); if (H.onBot) H.onBot(j); } })
      .catch(function () {});
  }

  // ------------------------------------------------------------ arranque
  function init(hooks) {
    H = hooks; load();
    var wrap = S.wrap = H.wrap(), chartEl = H.el();
    var bar = S.bar = document.createElement("div");
    bar.className = "jd-bar"; bar.setAttribute("role", "toolbar"); bar.setAttribute("aria-label", "Herramientas de dibujo");
    wrap.insertBefore(bar, wrap.firstChild);
    var svg = S.svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "jd-svg");
    chartEl.appendChild(svg);
    S.menu = document.createElement("div"); S.menu.className = "jd-menu"; S.menu.hidden = true; wrap.appendChild(S.menu);
    S.info = document.createElement("div"); S.info.className = "jd-info"; S.info.hidden = true; wrap.appendChild(S.info);
    S.colors = document.createElement("div"); S.colors.className = "jd-colors"; S.colors.hidden = true; wrap.appendChild(S.colors);
    bar.addEventListener("click", onBar);
    S.colors.addEventListener("click", function (e) {
      var b = e.target.closest("button"); if (!b) return;
      if (b.dataset.del) { remove(S.sel); return; }
      var d = list().filter(function (x) { return x.id === S.sel; })[0];
      if (d) { d.color = b.dataset.c; S.color = b.dataset.c; save(); bar.dataset.k = ""; schedule(); }
    });
    svg.addEventListener("pointerdown", onDown);
    svg.addEventListener("pointermove", onMove);
    svg.addEventListener("pointerup", onUp);
    document.addEventListener("pointerdown", function (e) {  // el gráfico no deja pasar el clic: se cierra al tocar
      if (!S.menu.hidden && !S.menu.contains(e.target) && !bar.contains(e.target)) closeMenu();
    }, true);
    document.addEventListener("keydown", function (e) {
      if (e.target && /INPUT|TEXTAREA/.test(e.target.tagName)) return;
      if (e.key === "Escape") { S.pending = null; S.temp = null; S.sel = null; closeMenu(); S.info.hidden = true; setTool("cursor"); }
      if ((e.key === "Delete" || e.key === "Backspace") && S.sel) { e.preventDefault(); remove(S.sel); }
    });
    var c = H.chart();
    c.timeScale().subscribeVisibleLogicalRangeChange(schedule);
    c.timeScale().subscribeSizeChange(schedule);
    c.subscribeCrosshairMove(schedule);
    c.subscribeClick(onChartClick);
    ["wheel", "pointermove", "pointerup"].forEach(function (ev) { chartEl.addEventListener(ev, schedule, {passive: true}); });
    // líneas del dibujo viejo ("― Línea"): pasan a ser líneas horizontales
    var old = H.oldLines && H.oldLines();
    if (old) Object.keys(old).forEach(function (sym) {
      (old[sym] || []).forEach(function (p) { (S.all[sym] = S.all[sym] || []).push({id: uid(), type: "hline", pts: [[Date.now(), p]], color: "#8e8e93"}); });
    });
    if (old) { save(); H.clearOldLines(); }
  }

  window.JDraw = {
    init: init,
    refresh: function () { if (H) { fetchBot(false); schedule(); } },
    symbolChanged: function () { if (H) { S.sel = null; S.pending = null; S.temp = null; S.info.hidden = true; S.bot = null; fetchBot(true); schedule(); } },
    layer: function (k) { return !!S.layers[k]; },
    themeChanged: function () { if (H) { S.bar.dataset.k = ""; schedule(); } },
    focus: function (ms) {  // centra el gráfico en ese momento (desde las tablas de abajo)
      var l = H && msToLogical(ms);
      if (l == null) return;
      var r = H.chart().timeScale().getVisibleLogicalRange(), half = r ? Math.max(20, (r.to - r.from) / 2) : 60;
      H.chart().timeScale().setVisibleLogicalRange({from: l - half, to: l + half});
    },
    _state: S
  };
})();
