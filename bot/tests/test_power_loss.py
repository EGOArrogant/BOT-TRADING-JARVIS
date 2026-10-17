"""Apagones: un archivo de estado lleno de ceros no debe impedir que el bot (ni el panel) arranquen."""
from datetime import UTC, date, datetime

from bot.core import control
from bot.core.fsutil import atomic_write_text, backup_path
from bot.core.state import BotState
from bot.tests.test_engine_paper import setup  # noqa: F401

ZEROS = "\x00" * 2048  # lo que deja Windows si se va la luz mientras guarda


def test_state_recovers_from_backup_after_power_loss(tmp_path):
    p = tmp_path / "state_paper.json"
    st = BotState.fresh(27.0, date(2026, 10, 17))
    st.equity = 26.5
    st.save(p)
    st.equity = 26.0
    st.save(p)  # la versión anterior (26.5) queda en .bak
    assert backup_path(p).exists()
    p.write_text(ZEROS, encoding="utf-8")
    rec = BotState.load_or_new(p, 27.0, date(2026, 10, 17))
    assert rec.equity == 26.5 and not rec.safe_mode


def test_state_without_backup_restarts_in_safe_mode_and_panel_does_not_touch_files(tmp_path):
    p = tmp_path / "state_paper.json"
    p.write_text(ZEROS, encoding="utf-8")
    panel = BotState.load_or_new(p, 27.0, date(2026, 10, 17), repair=False)
    assert panel.safe_mode and p.exists()  # el panel no mueve nada
    bot = BotState.load_or_new(p, 27.0, date(2026, 10, 17))
    assert bot.safe_mode and "apagón" in bot.safe_mode_reason and bot.equity == 27.0
    assert not p.exists() and list(tmp_path.glob("state_paper.json.danado-*"))


def test_control_recovers_from_backup(tmp_path):
    p = tmp_path / "control.json"
    control.save(p, control.Control(mode="operar", aggressiveness=7))
    control.save(p, control.Control(mode="operar", aggressiveness=6))
    p.write_text(ZEROS, encoding="utf-8")
    c = control.load(p)
    assert c.mode == "operar" and c.aggressiveness == 7


def test_corrupt_file_is_never_copied_over_a_good_backup(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_text(p, '{"a": 1}', backup=True)
    atomic_write_text(p, '{"a": 2}', backup=True)
    p.write_text(ZEROS, encoding="utf-8")
    atomic_write_text(p, '{"a": 3}', backup=True)
    assert backup_path(p).read_text() == '{"a": 1}'  # el respaldo sigue siendo el último bueno


def test_engine_starts_after_power_loss_and_resumes_by_itself(setup):
    import asyncio

    from bot.tests.test_engine_paper import FakeAnalyst, make_engine
    s, states, ex, now = setup
    s.storage.state_path.parent.mkdir(parents=True, exist_ok=True)
    s.storage.state_path.write_text(ZEROS, encoding="utf-8")
    eng = make_engine(s, states, ex, FakeAnalyst(), now)  # antes: ValidationError y el bot no arrancaba
    assert eng.state.safe_mode
    for _ in range(3):
        asyncio.run(eng.reconcile(now))
    assert not eng.state.safe_mode  # todo cuadra con el exchange: vuelve a operar
