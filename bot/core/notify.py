"""Notificaciones para personas: entradas, salidas y reportes en palabras simples.

El motor las guarda en el diario (evento `notif`, que el panel muestra en su campana) y las manda por
Telegram al teléfono si está configurado. Solo informan: no cambian nada del bot.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.dashboard.reports import COIN, EXIT, STRATEGY, amount, money, num, price


@dataclass(frozen=True)
class Notice:
    kind: str     # entrada | salida | reporte | alerta
    title: str
    text: str
    level: str = "info"   # info | good | bad | warning | error

    def as_text(self) -> str:
        return f"{self.title}\n{self.text}"

    def payload(self) -> dict[str, str]:
        return {"kind": self.kind, "title": self.title, "text": self.text, "level": self.level}


def entry(symbol: str, side: str, strategy: str, entry_price: float, stop: float, take_profit: float | None,
          qty: float, confidence: float | None) -> Notice:
    coin = COIN.get(symbol, symbol)
    long = side == "LONG"
    risk = abs(entry_price - stop) * qty
    title = f"🟢 Entrada LONG en {coin}" if long else f"🔴 Entrada SHORT en {coin}"
    what = "Compró (apuesta a que sube)" if long else "Vendió en corto (apuesta a que baja)"
    text = f"{what} a {price(entry_price)}. Stop-loss {price(stop)}"
    if take_profit:
        text += f" · objetivo {price(take_profit)}"
    text += f". Arriesga como máximo {amount(risk)}."
    if confidence is not None:
        text += f" Confianza de la IA: {confidence * 100:.0f} %."
    text += f" Oportunidad: {STRATEGY.get(strategy, strategy)}."
    return Notice("entrada", title, text, "info")


def exit_(symbol: str, side: str, reason: str, pnl_net: float, r: float, equity: float | None) -> Notice:
    coin = COIN.get(symbol, symbol)
    lado = "long" if side == "LONG" else "short"
    if pnl_net > 0:
        title, level = f"✅ Ganó {amount(pnl_net)} en {coin}", "good"
    elif pnl_net < 0:
        title, level = f"🔻 Perdió {amount(pnl_net)} en {coin}", "bad"
    else:
        title, level = f"⚪ Cerró en cero en {coin}", "info"
    text = (f"Cerró la operación {lado} porque {EXIT.get(reason, reason)} ({num(r, sign=True)} veces lo arriesgado, "
            "ya con comisiones).")
    if equity is not None:
        text += f" Capital ahora: {amount(equity)}."
    return Notice("salida", title, text, level)


def day(report: dict) -> Notice:
    pnl = report.get("pnl", 0.0)
    level = "good" if pnl > 0 else "bad" if pnl < 0 else "info"
    text = " ".join(report.get("paragraphs", [])[:2]) + f" 💡 {report.get('lesson', '')}"
    return Notice("reporte", f"📊 Resumen del {report['day']}: {report['headline']}", text.strip(), level)


def alert(msg: str, level: str) -> Notice:
    icon = {"error": "⛔", "warning": "⚠️"}.get(level, "ℹ️")
    return Notice("alerta", f"{icon} Aviso del bot", msg, level)


__all__ = ["Notice", "alert", "day", "entry", "exit_", "money"]
