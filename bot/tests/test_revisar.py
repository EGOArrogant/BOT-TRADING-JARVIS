"""Revisión previa: guardar la llave en .env sin tocar el resto del archivo."""
from bot.revisar import read_key, save_key


def test_save_key_replaces_only_that_line(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BINANCE_API_KEY=abc\nANTHROPIC_API_KEY=\nCONFIRM_LIVE_TRADING=false\n", encoding="utf-8")
    save_key("sk-ant-nueva", env)
    assert env.read_text(encoding="utf-8") == ("BINANCE_API_KEY=abc\nANTHROPIC_API_KEY=sk-ant-nueva\n"
                                               "CONFIRM_LIVE_TRADING=false\n")
    assert read_key(env) == "sk-ant-nueva"


def test_save_key_creates_file(tmp_path):
    env = tmp_path / ".env"
    save_key("sk-ant-x", env)
    assert read_key(env) == "sk-ant-x" and read_key(tmp_path / "no.env") == ""
