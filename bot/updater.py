"""Actualizar el bot desde GitHub con un botón del panel (o `python -m bot.updater`).

- Con una copia de git (servidor): `git pull --ff-only`.
- Con una copia del ZIP (PC con Windows): baja el ZIP oficial del repositorio, lo revisa y reemplaza solo el
  código. Nunca toca `.env`, `bot/data`, `bot/reports`, `.venv` ni `celular_activado.txt`.
- Después pide un reinicio ordenado: el bot guarda su estado y sale con `RESTART_CODE`; `iniciar_demo.bat`
  y systemd lo vuelven a arrancar con el código nuevo. Las posiciones abiertas siguen protegidas por su stop.

La actualización solo cambia código del repositorio oficial. Nunca cambia límites de riesgo por su cuenta: si
una versión nueva los cambiara, sería un cambio revisado y aprobado en el chat (CLAUDE.md §1 y §7).
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO = "EGOArrogant/BOT-TRADING-JARVIS"
BRANCH = "main"
ROOT = Path(__file__).resolve().parent.parent
RESTART_CODE = 10
KEEP_DIRS = {".git", ".venv", "venv", "__pycache__"}
KEEP_UNDER_BOT = {"data", "reports"}
KEEP_FILES = {".env", "celular_activado.txt"}
REQUIRED = ("bot/engine.py", "bot/main.py", "bot/config.yaml", "bot/dashboard/page.html", "pyproject.toml")


def version_file(root: Path = ROOT) -> Path:
    return root / "bot" / "data" / "version.json"


def restart_flag(root: Path = ROOT) -> Path:
    return root / "bot" / "data" / "reiniciar.flag"


def is_git(root: Path = ROOT) -> bool:
    return (root / ".git").exists() and shutil.which("git") is not None


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, timeout=120).stdout.strip()


def local_version(root: Path = ROOT) -> dict[str, Any]:
    if is_git(root):
        try:
            return {"sha": _git(root, "rev-parse", "HEAD"), "source": "git",
                    "date": _git(root, "log", "-1", "--format=%cI")}
        except (subprocess.SubprocessError, OSError):
            pass
    try:
        return {**json.loads(version_file(root).read_text(encoding="utf-8")), "source": "zip"}
    except (OSError, ValueError):
        return {"sha": None, "source": "zip", "date": None}


def remote_commits(client: httpx.Client, since_sha: str | None) -> dict[str, Any]:
    """Último commit de GitHub y los cambios (mensajes) desde la versión instalada."""
    r = client.get(f"https://api.github.com/repos/{REPO}/commits", params={"sha": BRANCH, "per_page": 30},
                   headers={"Accept": "application/vnd.github+json"}, timeout=20)
    r.raise_for_status()
    commits = r.json()
    changes = []
    for c in commits:
        if since_sha and c["sha"] == since_sha:
            break
        changes.append({"sha": c["sha"][:7], "date": c["commit"]["committer"]["date"],
                        "title": c["commit"]["message"].splitlines()[0][:160]})
    head = commits[0]
    return {"sha": head["sha"], "date": head["commit"]["committer"]["date"], "changes": changes[:15]}


@contextlib.contextmanager
def _session(client: httpx.Client | None):
    """El cliente que se pasa lo cierra quien lo creó; si no hay, se abre y se cierra aquí."""
    if client is not None:
        yield client
        return
    with httpx.Client(follow_redirects=True) as c:
        yield c


def check(root: Path = ROOT, client: httpx.Client | None = None) -> dict[str, Any]:
    local = local_version(root)
    with _session(client) as c:
        remote = remote_commits(c, local.get("sha"))
    return {"local": local, "remote": {k: remote[k] for k in ("sha", "date")}, "changes": remote["changes"],
            "available": local.get("sha") != remote["sha"]}


def _copy_tree(src: Path, dst: Path) -> int:
    """Copia el código nuevo encima del actual sin tocar datos, llaves ni el entorno de Python."""
    n = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        parts = rel.parts
        if parts[0] in KEEP_DIRS or (len(parts) >= 2 and parts[0] == "bot" and parts[1] in KEEP_UNDER_BOT):
            continue
        if any(p == "__pycache__" for p in parts) or (len(parts) == 1 and parts[0] in KEEP_FILES):
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            n += 1
    return n


def _install_deps(root: Path) -> str:
    """Instala dependencias nuevas si las hay (uv si existe; si no, pip del entorno)."""
    py = Path(sys.executable)
    cmds = []
    if shutil.which("uv"):
        cmds.append(["uv", "pip", "install", "--python", str(py), "-e", str(root)])
    cmds.append([str(py), "-m", "pip", "install", "-q", "-e", str(root)])
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
            return "ok"
        except (subprocess.SubprocessError, OSError):
            continue
    return "no se pudieron instalar dependencias (si el bot no arranca, ejecuta instalar.bat)"


def apply(root: Path = ROOT, client: httpx.Client | None = None, install: bool = True,
          restart: bool = True) -> dict[str, Any]:
    """restart=False: no pide el reinicio (quien llama lo pide después, p. ej. tras contestar por Telegram)."""
    with _session(client) as c:
        return _apply(root, c, install, restart)


def _apply(root: Path, client: httpx.Client, install: bool, restart: bool = True) -> dict[str, Any]:
    info = check(root, client)
    if not info["available"]:
        return {"ok": True, "updated": False, "message": "Ya tienes la versión más reciente."}
    old_pyproject = (root / "pyproject.toml").read_text(encoding="utf-8") if (root / "pyproject.toml").exists() else ""
    if info["local"]["source"] == "git":
        _git(root, "pull", "--ff-only", "origin", BRANCH)
        copied = None
    else:
        # Por SHA exacto (no "lo último de main"): GitHub puede servir una copia vieja de la rama unos minutos
        r = client.get(f"https://codeload.github.com/{REPO}/zip/{info['remote']['sha']}", timeout=120)
        r.raise_for_status()
        data = r.content
        with tempfile.TemporaryDirectory() as tmp:
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
            tops = [p for p in Path(tmp).iterdir() if p.is_dir()]
            if len(tops) != 1:
                raise RuntimeError("el ZIP descargado no tiene la forma esperada")
            src = tops[0]
            missing = [f for f in REQUIRED if not (src / f).is_file()]
            if missing:
                raise RuntimeError(f"el ZIP descargado está incompleto: falta {', '.join(missing)}")
            copied = _copy_tree(src, root)
    vf = version_file(root)
    vf.parent.mkdir(parents=True, exist_ok=True)
    vf.write_text(json.dumps({"sha": info["remote"]["sha"], "date": info["remote"]["date"],
                              "installed_at": datetime.now(UTC).isoformat()}), encoding="utf-8")
    deps = "sin cambios"
    if install and (root / "pyproject.toml").read_text(encoding="utf-8") != old_pyproject:
        deps = _install_deps(root)
    if restart:
        request_restart(root)
    return {"ok": True, "updated": True, "files": copied, "deps": deps, "changes": info["changes"],
            "message": "Actualizado. El bot y el panel se reinician solos en unos segundos."}


def request_restart(root: Path = ROOT) -> None:
    f = restart_flag(root)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")


def restart_requested(since: datetime, root: Path = ROOT) -> bool:
    try:
        return datetime.fromtimestamp(restart_flag(root).stat().st_mtime, UTC) > since
    except OSError:
        return False


def main() -> int:
    try:
        info = check()
    except Exception as exc:  # noqa: BLE001
        print(f"No se pudo consultar GitHub ({exc}). Revisa tu internet.")
        return 1
    if not info["available"]:
        print("Ya tienes la versión más reciente.")
        return 0
    print("Hay una versión nueva. Cambios:")
    for c in info["changes"]:
        print(f"  - {c['date'][:10]} {c['title']}")
    out = apply()
    print(out["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
