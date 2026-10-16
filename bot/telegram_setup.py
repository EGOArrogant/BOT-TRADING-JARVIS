"""Configurar las notificaciones al teléfono por Telegram (pensado para quien no programa).

    python -m bot.telegram_setup      (o doble clic en configurar_telegram.bat)

1. Pide el token del bot de Telegram (lo da @BotFather).
2. Espera a que le escribas cualquier mensaje a tu bot desde el teléfono y así obtiene tu chat_id.
3. Guarda ambos en .env (nunca se suben a internet) y manda un mensaje de prueba.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
API = "https://api.telegram.org/bot{token}/{method}"


def set_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done: set[str] = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in values:
            out.append(f"{key}={values[key]}")
            done.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def call(token: str, method: str, **params) -> dict:
    r = httpx.post(API.format(token=token, method=method), json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", f"error {r.status_code}"))
    return data["result"]


def wait_chat_id(token: str, timeout_s: float = 180, ask=call) -> str | None:
    deadline = time.monotonic() + timeout_s
    offset = None
    while time.monotonic() < deadline:
        updates = ask(token, "getUpdates", timeout=20, **({"offset": offset} if offset else {}))
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                return str(chat["id"])
    return None


def main() -> int:
    print()
    print(" === Notificaciones de JARVIS en tu teléfono (Telegram) ===")
    print()
    print(" 1. Instala Telegram en el iPhone y busca  @BotFather  (con la palomita azul).")
    print(" 2. Escríbele  /newbot  y sigue sus pasos: un nombre (ej. Mi JARVIS) y un usuario que termine en 'bot'.")
    print(" 3. BotFather te dará un TOKEN parecido a  123456789:AAH...  Cópialo y pégalo aquí abajo.")
    print()
    token = input(" Token: ").strip().strip('"')
    try:
        me = call(token, "getMe")
    except Exception as exc:  # noqa: BLE001
        print(f"\n [X] Ese token no funcionó ({exc}). Revisa que esté completo y vuelve a intentarlo.")
        return 1
    print(f"\n [OK] Bot encontrado: @{me.get('username')}")
    print(f" 4. Ahora, en el iPhone, abre tu bot @{me.get('username')}, toca INICIAR y escríbele 'hola'.")
    print("    Esperando tu mensaje (hasta 3 minutos)...")
    try:
        chat_id = wait_chat_id(token)
    except Exception as exc:  # noqa: BLE001
        print(f"\n [X] No se pudo leer el mensaje ({exc}).")
        return 1
    if not chat_id:
        print("\n [X] No llegó ningún mensaje. Vuelve a abrir configurar_telegram.bat e inténtalo de nuevo.")
        return 1
    set_env({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    try:
        call(token, "sendMessage", chat_id=chat_id,
             text="JARVIS · ✅ Listo: aquí te avisaré de cada entrada, salida y resumen del día.")
    except Exception:  # noqa: BLE001
        pass
    print("\n [OK] Guardado. Revisa tu Telegram: te llegó un mensaje de prueba.")
    print("      Cierra la ventana del bot y vuelve a abrir iniciar_demo.bat para que empiece a avisarte.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
