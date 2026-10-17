from datetime import UTC, datetime, timedelta

from bot.core.health import HealthMonitor

T0 = datetime(2026, 10, 17, 21, 24, tzinfo=UTC)


def test_no_false_alarm_while_connecting(tmp_path):
    h = HealthMonitor(tmp_path / "h.json", {"ws_market": 30, "macro": 36 * 3600})
    assert h.check(T0) == ([], [])
    assert h.check(T0 + timedelta(seconds=120)) == ([], [])     # aún arrancando: no es caída
    h.beat("ws_market", T0 + timedelta(seconds=190))
    down, _ = h.check(T0 + timedelta(seconds=200))               # macro nunca llegó tras la gracia
    assert down == ["macro"]


def test_source_that_stops_after_connecting_is_down(tmp_path):
    h = HealthMonitor(tmp_path / "h.json", {"ws_market": 30}, grace_s=180)
    h.check(T0)
    h.beat("ws_market", T0 + timedelta(seconds=5))
    down, _ = h.check(T0 + timedelta(seconds=60))                # vista y luego callada: caída aunque esté en gracia
    assert down == ["ws_market"]
    h.beat("ws_market", T0 + timedelta(seconds=61))
    assert h.check(T0 + timedelta(seconds=62)) == ([], ["ws_market"])
