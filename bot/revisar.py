"""Revisión antes de arrancar (pensada para quien no programa):

    python -m bot.revisar

1. ¿Llegan los datos de Binance desde este país? (Binance bloquea algunas regiones: error 451/403)
2. ¿Llegan los datos de Hyperliquid (traders destacados)?
3. ¿Hay una llave de Anthropic válida? Si falta, la pide y la guarda en .env.
   Se valida listando modelos: no gasta tokens.

Sale con código 0 si el modo demo (paper) puede operar.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anthropic
import httpx

from bot.core.config import load_settings

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
OK, BAD, WARN = "[OK]", "[X]", "[!]"


def check_binance(rest: str) -> tuple[bool, str]:
    try:
        r = httpx.get(f"{rest}/fapi/v1/exchangeInfo", timeout=15)
    except httpx.HTTPError as exc:
        return False, f"no hay conexión con Binance ({exc.__class__.__name__}). Revisa tu internet."
    if r.status_code in (451, 403):
        return False, ("Binance bloquea los futuros desde tu país o red (error "
                       f"{r.status_code}). El bot no puede funcionar desde aquí: se necesita un servidor (VPS) "
                       "en un país permitido.")
    if r.status_code != 200:
        return False, f"Binance respondió {r.status_code}. Intenta de nuevo en unos minutos."
    return True, "Binance responde: hay precios en vivo."


def check_hyperliquid(url: str) -> tuple[bool, str]:
    try:
        r = httpx.post(url, json={"type": "meta"}, timeout=15)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        return False, f"Hyperliquid no responde ({exc.__class__.__name__}). El bot funciona igual, sin esos datos."
    return True, "Hyperliquid responde: se podrá observar a los traders destacados."


def save_key(key: str, path: Path = ENV_PATH) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            out.append(f"ANTHROPIC_API_KEY={key}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"ANTHROPIC_API_KEY={key}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def read_key(path: Path = ENV_PATH) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def check_anthropic(key: str) -> tuple[bool, str]:
    """Valida la llave sin gastar tokens (lista de modelos)."""
    try:
        anthropic.Anthropic(api_key=key, timeout=20, max_retries=1).models.list(limit=1)
    except anthropic.AuthenticationError:
        return False, "la llave de Anthropic no es válida (¿se copió completa?)."
    except anthropic.PermissionDeniedError:
        return False, "la llave existe pero no tiene permiso. Revisa en console.anthropic.com."
    except anthropic.RateLimitError:
        return True, "la llave es válida (Anthropic pidió esperar un momento, no es un problema)."
    except anthropic.APIStatusError as exc:
        return False, f"Anthropic respondió {exc.status_code}. Intenta de nuevo en unos minutos."
    except anthropic.APIConnectionError:
        return False, "no hay conexión con Anthropic. Revisa tu internet."
    return True, "la llave de Anthropic es válida: la IA podrá decidir."


def main() -> int:
    s = load_settings()
    print("\n=== Revisión de JARVIS ===\n")
    ok_b, msg = check_binance(s.endpoints.binance.rest)
    print(OK if ok_b else BAD, msg)
    ok_h, msg = check_hyperliquid(s.endpoints.hyperliquid)
    print(OK if ok_h else WARN, msg)

    key = read_key() or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key and sys.stdin.isatty():
        print("\nFalta la llave de Anthropic (la IA del bot). Se consigue en https://console.anthropic.com"
              " -> API Keys -> Create Key. Empieza con 'sk-ant-'.")
        key = input("Pégala aquí y presiona Enter (o deja vacío para hacerlo después): ").strip()
        if key:
            save_key(key)
            print("Guardada en .env (ese archivo nunca se sube a GitHub).")
    if key:
        ok_a, msg = check_anthropic(key)
        print(OK if ok_a else BAD, msg)
    else:
        ok_a = False
        print(BAD, "sin llave de Anthropic: el bot correrá, pero la IA dirá 'no operar' a todo (cero operaciones).")

    print()
    if ok_b and ok_a:
        print("Todo listo para el modo demo. Ejecuta iniciar_demo.bat")
        return 0
    print("Corrige lo marcado con [X] y vuelve a ejecutar esta revisión.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
