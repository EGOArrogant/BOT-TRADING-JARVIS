import json
import logging
from datetime import UTC, datetime

from bot.core.clock import Clock
from bot.core.config import Secrets, load_settings
from bot.core.logging_setup import setup_logging
from bot.main import LIVE_CONFIRM_PHRASE, check_live_allowed, run


def _live_secrets(**kw) -> Secrets:
    base = dict(binance_api_key="k", binance_api_secret="s", confirm_live_trading=True)
    base.update(kw)
    return Secrets(**base)


def test_live_blocked_without_env_flag():
    assert not check_live_allowed(_live_secrets(confirm_live_trading=False), ask=lambda _: LIVE_CONFIRM_PHRASE)


def test_live_blocked_without_keys():
    assert not check_live_allowed(_live_secrets(binance_api_key=None), ask=lambda _: LIVE_CONFIRM_PHRASE)


def test_live_blocked_with_wrong_console_answer():
    assert not check_live_allowed(_live_secrets(), ask=lambda _: "si")


def test_live_allowed_only_with_both():
    assert check_live_allowed(_live_secrets(), ask=lambda _: LIVE_CONFIRM_PHRASE)


def test_run_live_refuses_without_confirmation():
    assert run(load_settings(mode="live"), Secrets()) == 2


def test_json_logs_and_redaction(tmp_path):
    setup_logging(tmp_path, console=False)
    logging.getLogger("t").info("hola", extra={"symbol": "BTCUSDT", "api_key": "LEAK"})
    for h in logging.getLogger().handlers:
        h.flush()
    line = (tmp_path / "bot.jsonl").read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["msg"] == "hola" and rec["symbol"] == "BTCUSDT"
    assert rec["api_key"] == "***"
    assert "LEAK" not in line


def test_trading_day_is_utc():
    c = Clock()
    late = datetime(2026, 10, 15, 23, 59, 59, tzinfo=UTC)
    assert c.trading_day(late).isoformat() == "2026-10-15"
    assert c.next_day_start(late) == datetime(2026, 10, 16, tzinfo=UTC)


def test_live_blocked_without_phase_gates():
    assert not check_live_allowed(_live_secrets(), ask=lambda _: LIVE_CONFIRM_PHRASE, gates_ok=False)


def test_per_mode_storage_isolated():
    from bot.main import per_mode_storage
    p = per_mode_storage(load_settings(mode="paper")).storage
    l = per_mode_storage(load_settings(mode="live")).storage
    assert p.db_path.name == "bot_paper.sqlite" and l.db_path.name == "bot_live.sqlite"
    assert p.state_path != l.state_path


def test_gates_require_metrics(tmp_path):
    from datetime import timedelta
    from decimal import Decimal

    from bot import gates
    from bot.core.models import ExitReason, Regime, Side, Trade
    from bot.learning.journal import Journal
    assert not gates.live_allowed(tmp_path)
    j = Journal(tmp_path / "bot_paper.sqlite")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(120):  # con 60 variantes probadas, el Sharpe deflactado pide una muestra grande
        win = i % 2 == 0
        j.record_trade(Trade(id=f"t{i}", signal_id=f"s{i}", symbol="ETHUSDT", side=Side.LONG, strategy="x",
                             regime=Regime.RANGE, qty=Decimal(1), entry_price=Decimal(100),
                             exit_price=Decimal(103 if win else 99), expected_entry_price=Decimal(100),
                             initial_stop_price=Decimal(99), entry_maker=True, fees=Decimal("0.1"),
                             pnl_gross=Decimal(3 if win else -1), mae_r=0, mfe_r=0,
                             opened_at=t0 + timedelta(days=i * 0.4), closed_at=t0 + timedelta(days=i * 0.4, hours=2),
                             exit_reason=ExitReason.TAKE_PROFIT))
    res = gates.approve(tmp_path, 10_000)
    assert res["paper"]["ok"] and not res["testnet"]["ok"] and not gates.live_allowed(tmp_path)


def test_reset_halt_requires_typed_confirmation(tmp_path):
    from bot.core.config import StorageConfig
    from bot.core.state import BotState
    from bot.main import cmd_reset_halt, per_mode_storage
    s = load_settings(mode="paper")
    s = s.model_copy(update={"storage": StorageConfig(db_path=tmp_path / "bot.sqlite", reports_dir=tmp_path,
                                                      state_path=tmp_path / "state.json")})
    path = per_mode_storage(s).storage.state_path
    st = BotState.fresh(10_000, datetime(2026, 1, 1).date())
    st.halted, st.halt_reason = True, "dd"
    st.save(path)
    assert cmd_reset_halt(s, ask=lambda _: "si") == 1
    assert BotState.model_validate_json(path.read_text()).halted
    assert cmd_reset_halt(s, ask=lambda _: "REINICIAR") == 0
    assert not BotState.model_validate_json(path.read_text()).halted
