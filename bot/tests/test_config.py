import copy

import pytest
import yaml
from pydantic import ValidationError

from bot.core.config import (
    DEFAULT_CONFIG_PATH,
    HARD_MAX_LEVERAGE,
    HARD_MAX_TRADES_PER_DAY,
    Mode,
    Secrets,
    Settings,
    load_settings,
)


@pytest.fixture
def raw() -> dict:
    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_default_config_loads_in_paper_mode():
    s = load_settings()
    assert s.mode is Mode.PAPER
    assert s.limits.max_trades_per_day == 5
    assert s.limits.base_trades_per_day == 3
    assert 0 < s.risk.risk_per_trade <= 0.01  # 1 % en la demo de 3.000 US$; nunca más que el tope duro
    assert s.risk.max_leverage == 3
    assert s.exchange.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "XLMUSDT"]
    assert s.logging.dir.is_absolute()


def test_mode_override():
    assert load_settings(mode="record").mode is Mode.RECORD


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("limits", "max_trades_per_day", HARD_MAX_TRADES_PER_DAY + 1),
        ("risk", "max_leverage", HARD_MAX_LEVERAGE + 1),
        ("risk", "risk_per_trade", 0.05),
        ("risk", "max_positions_per_symbol", 2),
        ("filters", "min_edge_cost_multiple", 2.0),
        ("limits", "extra_trade_min_edge", 2.5),
        ("ai", "max_news_headlines", 20),
        ("learning", "challenger_min_cases", 10),
    ],
)
def test_hard_caps_cannot_be_exceeded_from_yaml(raw, section, key, value):
    bad = copy.deepcopy(raw)
    bad[section][key] = value
    with pytest.raises(ValidationError):
        Settings.model_validate(bad)


def test_unknown_keys_rejected(raw):
    bad = copy.deepcopy(raw)
    bad["risk"]["risk_per_trade_typo"] = 0.01
    with pytest.raises(ValidationError):
        Settings.model_validate(bad)


def test_base_cap_not_above_hard_cap(raw):
    bad = copy.deepcopy(raw)
    bad["limits"]["max_trades_per_day"] = 2
    with pytest.raises(ValidationError):
        Settings.model_validate(bad)


def test_settings_are_immutable():
    s = load_settings()
    with pytest.raises(ValidationError):
        s.risk.risk_per_trade = 0.02  # type: ignore[misc]


def test_secrets_not_in_repr(monkeypatch):
    monkeypatch.setenv("BINANCE_API_SECRET", "supersecret")
    monkeypatch.setenv("CONFIRM_LIVE_TRADING", "TRUE")
    s = Secrets.from_env()
    assert "supersecret" not in repr(s)
    assert s.confirm_live_trading is True


def test_confirm_live_defaults_false(monkeypatch):
    monkeypatch.delenv("CONFIRM_LIVE_TRADING", raising=False)
    assert Secrets.from_env().confirm_live_trading is False
