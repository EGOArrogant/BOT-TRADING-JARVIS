"""Ver el panel desde el celular (modo celular), protegido con una clave.

Sin este modo el panel solo escucha en 127.0.0.1. Con `--celular` escucha en la red de la casa (y en
Tailscale si está instalado), pero quien no sea la propia PC necesita la clave:

- La clave vive en `reports/<modo>/panel_clave.txt` (aleatoria, 24 caracteres) y solo la PC la ve.
- El panel de la PC muestra un código QR con la dirección y la clave; al abrirlo, el celular guarda
  una cookie (HttpOnly, SameSite=Strict) y ya no la vuelve a pedir.
- Borrar `panel_clave.txt` crea una nueva y desconecta a todos los celulares.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
import socket
from pathlib import Path

from bot.core.fsutil import atomic_write_text, read_text_retry

FILENAME = "panel_clave.txt"
COOKIE = "jarvis_clave"
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def load_or_create_key(reports_dir: Path) -> str:
    path = reports_dir / FILENAME
    try:
        key = read_text_retry(path).strip()
        if len(key) >= 20:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(18)
    reports_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, key)
    return key


def key_ok(given: str | None, key: str) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), key.encode())


def cookie_value(cookie_header: str | None) -> str | None:
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def local_ips() -> list[dict[str, str]]:
    """Direcciones de esta PC a las que puede llegar el celular: la de la red de casa y la de Tailscale."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no envía nada: solo elige la interfaz de salida
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(str(info[4][0]))
    except OSError:
        pass
    out, seen = [], set()
    for ip in found:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if ip in seen or a.is_loopback or a.is_link_local:
            continue
        seen.add(ip)
        if a in TAILSCALE:
            out.append({"ip": ip, "kind": "tailscale"})
        elif a.is_private:
            out.append({"ip": ip, "kind": "casa"})
    return sorted(out, key=lambda x: x["kind"] != "casa")
