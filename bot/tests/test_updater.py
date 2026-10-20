"""Botón Actualizar: baja el código nuevo sin tocar llaves ni datos y pide un reinicio ordenado."""
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from bot import updater

SHA_NEW, SHA_OLD = "b" * 40, "a" * 40


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(f"BOT-TRADING-JARVIS-main/{name}", text)
    return buf.getvalue()


def _client(zip_bytes: bytes, head=SHA_NEW) -> httpx.Client:
    commits = [{"sha": head, "commit": {"committer": {"date": "2026-10-17T08:00:00Z"}, "message": "Mejora nueva\n\ndetalle"}},
               {"sha": SHA_OLD, "commit": {"committer": {"date": "2026-10-16T08:00:00Z"}, "message": "Versión anterior"}}]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=commits)
        if req.url.host == "codeload.github.com":
            assert req.url.path.endswith(f"/zip/{head}")  # por SHA exacto, nunca "lo último de la rama"
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def install(tmp_path):
    root = tmp_path / "jarvis"
    (root / "bot" / "data").mkdir(parents=True)
    (root / "bot" / "reports").mkdir(parents=True)
    (root / "bot" / "engine.py").write_text("viejo")
    (root / "pyproject.toml").write_text("[project]\nname='bot'\n")
    (root / ".env").write_text("ANTHROPIC_API_KEY=mi-llave")
    (root / "bot" / "data" / "bot_paper.sqlite").write_text("mis datos")
    (root / "bot" / "reports" / "zona.md").write_text("mi reporte")
    (root / "bot" / "data" / "version.json").write_text(json.dumps({"sha": SHA_OLD}))
    return root


NEW = {"bot/engine.py": "nuevo", "bot/main.py": "m", "bot/config.yaml": "c", "bot/dashboard/page.html": "p",
       "pyproject.toml": "[project]\nname='bot'\n", ".env": "ANTHROPIC_API_KEY=", "bot/data/.gitkeep": "",
       "bot/reports/zona.md": "pisado", "bot/nuevo.py": "x"}


def test_check_lists_changes_since_installed_version(install):
    info = updater.check(install, _client(_zip(NEW)))
    assert info["available"] and [c["title"] for c in info["changes"]] == ["Mejora nueva"]
    assert not updater.check(install, _client(_zip(NEW), head=SHA_OLD))["available"]


def test_apply_replaces_code_but_never_keys_or_data(install):
    t0 = datetime.now(UTC) - timedelta(seconds=5)
    out = updater.apply(install, _client(_zip(NEW)), install=False)
    assert out["updated"] and (install / "bot" / "engine.py").read_text() == "nuevo" and (install / "bot" / "nuevo.py").exists()
    assert (install / ".env").read_text() == "ANTHROPIC_API_KEY=mi-llave"
    assert (install / "bot" / "data" / "bot_paper.sqlite").read_text() == "mis datos"
    assert (install / "bot" / "reports" / "zona.md").read_text() == "mi reporte"
    assert json.loads(updater.version_file(install).read_text())["sha"] == SHA_NEW
    assert updater.restart_requested(t0, install)
    assert not updater.apply(install, _client(_zip(NEW)), install=False)["updated"]  # ya al día


def test_incomplete_download_is_rejected(install):
    with pytest.raises(RuntimeError, match="incompleto"):
        updater.apply(install, _client(_zip({"bot/engine.py": "x"})), install=False)
    assert (install / "bot" / "engine.py").read_text() == "viejo"


def test_apply_can_leave_the_restart_to_the_caller(install):
    out = updater.apply(install, _client(_zip(NEW)), install=False, restart=False)
    assert out["updated"] and not updater.restart_flag(install).exists()  # Telegram contesta primero y luego reinicia
