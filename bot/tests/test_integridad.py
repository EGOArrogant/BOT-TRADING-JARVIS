"""La revisión de la copia detecta un archivo viejo antes de que el bot entre en un ciclo de reinicios."""
import sys
import types

from bot import integridad


def test_complete_copy_passes():
    assert integridad.check() == []
    assert integridad.main() == 0


def test_old_engine_is_detected(monkeypatch):
    old = types.ModuleType("bot.engine")  # como el engine.py de una versión anterior: sin run_live_like
    old.run_record = lambda: None
    monkeypatch.setitem(sys.modules, "bot.engine", old)
    problems = integridad.check()
    assert any("bot.engine" in p and "run_live_like" in p for p in problems)
    assert integridad.main() == integridad.EXIT_BROKEN


def test_missing_data_file_is_detected(monkeypatch, tmp_path):
    monkeypatch.setattr(integridad, "DATA_FILES", ("no_existe.yaml",))
    assert any("no_existe.yaml" in p for p in integridad.check())
