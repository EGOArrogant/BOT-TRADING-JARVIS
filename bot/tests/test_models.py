from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bot.core.models import (
    AIAction,
    DataPoint,
    DataStatus,
    Decision,
    ExitReason,
    Order,
    OrderSide,
    OrderType,
    Regime,
    Side,
    Signal,
    Snapshot,
    Trade,
    parse_decision,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------ DataPoint


def test_unknown_has_no_value_and_never_becomes_zero():
    dp = DataPoint.unknown("binance")
    assert dp.status is DataStatus.UNKNOWN
    assert dp.value is None
    assert dp.as_float() is None


def test_unknown_with_value_rejected():
    with pytest.raises(ValidationError):
        DataPoint(value=0.0, source="x", status=DataStatus.UNKNOWN)


def test_ok_requires_value_and_ts():
    with pytest.raises(ValidationError):
        DataPoint(source="x", status=DataStatus.OK)
    with pytest.raises(ValidationError):
        DataPoint(value=1.0, source="x", status=DataStatus.OK)


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        DataPoint.ok(1.0, "x", datetime(2026, 1, 1))


def test_ttl_marks_stale_and_stale_is_not_usable():
    dp = DataPoint.ok(100.0, "binance", NOW)
    assert dp.with_ttl(NOW + timedelta(seconds=5), 5).is_ok
    stale = dp.with_ttl(NOW + timedelta(seconds=6), 5)
    assert stale.status is DataStatus.STALE
    assert stale.as_float() is None


def test_snapshot_missing_and_refresh():
    snap = Snapshot(
        symbol="BTCUSDT",
        ts=NOW,
        fields={
            "market.price": DataPoint.ok(60000.0, "binance", NOW),
            "funding.rate": DataPoint.ok(0.0001, "binance", NOW - timedelta(seconds=120)),
            "onchain.exchange_flow": DataPoint.unknown("cryptoquant"),
        },
    )
    refreshed = snap.refreshed(NOW, {"market": 5, "funding": 60})
    assert refreshed.get("market.price").is_ok
    assert refreshed.get("funding.rate").status is DataStatus.STALE
    assert refreshed.missing(["market.price", "funding.rate", "derivs.oi"]) == ["funding.rate", "derivs.oi"]
    assert refreshed.get("does.not.exist").status is DataStatus.UNKNOWN


# ------------------------------------------------------------ Signal


def _signal(**kw) -> Signal:
    base = dict(
        id="s1", symbol="ETHUSDT", strategy="pullback_ema", side=Side.LONG,
        entry=2500.0, stop=2475.0, expected_move_pct=0.012, total_cost_pct=0.003,
        data_quality=0.9, regime=Regime.TREND_UP, created_at=NOW,
    )
    base.update(kw)
    return Signal(**base)


def test_signal_score_and_edge():
    s = _signal()
    assert s.edge_ratio == pytest.approx(4.0)
    assert s.score == pytest.approx(3.6)
    assert s.stop_distance_pct == pytest.approx(0.01)


@pytest.mark.parametrize("side,stop", [(Side.LONG, 2510.0), (Side.SHORT, 2490.0)])
def test_signal_stop_on_wrong_side_rejected(side, stop):
    with pytest.raises(ValidationError):
        _signal(side=side, stop=stop)


# ------------------------------------------------------------ Decision (IA)

VALID = {
    "action": "LONG",
    "confidence": 0.7,
    "size_multiplier": 0.8,
    "thesis": "Retroceso a EMA20 en tendencia con CVD a favor.",
    "key_evidence": ["CVD positivo", "OI subiendo"],
    "invalidation": "cierre 15m bajo 2475",
    "news_impact": "NONE",
    "risk_flags": [],
}


def test_valid_decision_parses():
    d = parse_decision(VALID)
    assert d is not None and d.action is AIAction.LONG
    assert d.agrees_with(Side.LONG) and not d.agrees_with(Side.SHORT)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "{",
        "[]",
        "{}",
        '{"action": "LONG"}',
        {**VALID, "size_multiplier": 1.5},      # IA intenta aumentar tamaño
        {**VALID, "size_multiplier": 0.1},
        {**VALID, "confidence": 1.2},
        {**VALID, "confidence": True},
        {**VALID, "action": "BUY"},
        {**VALID, "news_impact": "MAYBE"},
        {**VALID, "leverage": 10},               # campo extra
        {**VALID, "invalidation": ""},           # operar sin invalidación
        {**VALID, "thesis": ""},
        {**VALID, "thesis": "x" * 401},
    ],
)
def test_broken_decisions_are_rejected(raw):
    assert parse_decision(raw) is None


def test_no_trade_without_invalidation_is_ok():
    d = parse_decision({**VALID, "action": "NO_TRADE", "invalidation": ""})
    assert d is not None and d.action is AIAction.NO_TRADE


def test_no_trade_factory():
    d = Decision.no_trade("timeout")
    assert d.action is AIAction.NO_TRADE and d.confidence == 0.0


# ------------------------------------------------------------ Orders / Trades


def test_stop_order_must_be_reduce_only():
    with pytest.raises(ValidationError):
        Order(client_order_id="c1", symbol="BTCUSDT", side=OrderSide.SELL, type=OrderType.STOP_MARKET,
              qty=Decimal("0.01"), stop_price=Decimal("59000"), created_at=NOW)
    o = Order(client_order_id="c1", symbol="BTCUSDT", side=OrderSide.SELL, type=OrderType.STOP_MARKET,
              qty=Decimal("0.01"), stop_price=Decimal("59000"), reduce_only=True, created_at=NOW)
    assert o.reduce_only


def test_limit_requires_price_and_post_only_only_limit():
    with pytest.raises(ValidationError):
        Order(client_order_id="c", symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT,
              qty=Decimal("1"), created_at=NOW)
    with pytest.raises(ValidationError):
        Order(client_order_id="c", symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET,
              qty=Decimal("1"), post_only=True, created_at=NOW)


def test_trade_net_pnl_and_r():
    t = Trade(
        id="t1", signal_id="s1", symbol="ETHUSDT", side=Side.SHORT, strategy="x", regime=Regime.RANGE,
        qty=Decimal("1"), entry_price=Decimal("2500"), exit_price=Decimal("2450"),
        expected_entry_price=Decimal("2501"), initial_stop_price=Decimal("2525"), entry_maker=True,
        fees=Decimal("1.5"), funding=Decimal("-0.5"), pnl_gross=Decimal("50"),
        mae_r=-0.3, mfe_r=2.1, opened_at=NOW, closed_at=NOW + timedelta(hours=2),
        exit_reason=ExitReason.TAKE_PROFIT,
    )
    assert t.pnl_net == Decimal("48")
    assert t.r_multiple == Decimal("48") / Decimal("25")
    assert t.slippage_pct > 0  # short vendido más bajo de lo esperado = costo
    assert t.duration == timedelta(hours=2)
