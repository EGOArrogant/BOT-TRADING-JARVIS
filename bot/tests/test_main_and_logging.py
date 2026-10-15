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
