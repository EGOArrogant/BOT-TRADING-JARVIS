"""El chequeo de mirar al futuro: el backtest real pasa; uno con fuga a propósito se detecta."""
from bot.backtest.engine import Backtester
from bot.backtest.lookahead import check
from bot.core.config import load_settings
from bot.core.models import DataPoint
from bot.tests.helpers import make_candles, trend_series

S = load_settings()


def _data():
    return {"ETHUSDT": make_candles(trend_series(1300, 0.0004, seed=3, start=2500), 900_000),
            "SOLUSDT": make_candles(trend_series(1300, -0.0003, seed=4, start=150), 900_000)}


def test_backtest_does_not_look_ahead():
    res = check(lambda: Backtester(S, warmup_bars_15m=800), _data(), samples=3)
    assert res["checked"] == 3 and res["bars"] >= 6 and res["ok"], res["leaks"]


def test_a_leaky_indicator_is_caught():
    class Leaky(Backtester):
        """Usa por error una vela que todavía no ha ocurrido (como un indicador mal calculado)."""

        def run(self, data_15m, strategies=None):
            self._fut = {(s, c.open_time): c.close for s, cs in data_15m.items() for c in cs}
            return super().run(data_15m, strategies)

        def _features(self, sym, state, asof):
            f = super()._features(sym, state, asof)
            nxt = self._fut.get((sym, int(asof.timestamp() * 1000) + 900_000 * 3))  # ¡el cierre de dentro de 1 h!
            f["ind.trampa"] = DataPoint.ok(nxt, "x", asof) if nxt else DataPoint.unknown("x")
            return f
    res = check(lambda: Leaky(S, warmup_bars_15m=800), _data(), samples=3)
    assert not res["ok"] and "ind.trampa" in res["leaks"]
