from dataclasses import dataclass
from types import SimpleNamespace

import pandas as pd

from brain.api import main as api_main
from brain.data.interfaces import CorrelationFeed, NewsFeed, PriceFeed
from brain.decision_engine import DecisionEngine, FinalDecision


class _PriceFeed(PriceFeed):
    def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        timestamps = pd.date_range("2026-11-10T00:00:00Z", periods=60, freq="15min")
        closes = [2000.0 + index * 0.1 for index in range(60)]
        return pd.DataFrame({
            "timestamp": timestamps,
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [100] * 60,
        })


class _NewsFeed(NewsFeed):
    def get_upcoming_events(self, symbol: str, minutes_ahead: int) -> list[dict]:
        return []


@dataclass(frozen=True)
class _DxySnapshot:
    current: float = 101.2
    previous: float = 101.0
    change: float = 0.2
    threshold: float = 0.1
    trend: str = "up"
    as_of_utc: str = "2026-11-10T14:45:00+00:00"


class _DetailsCorrelationFeed(CorrelationFeed):
    def __init__(self):
        self.details_calls = 0
        self.legacy_calls = 0

    def get_dollar_trend_details(self):
        self.details_calls += 1
        return _DxySnapshot()

    def get_dollar_trend(self) -> str:
        self.legacy_calls += 1
        raise AssertionError("يجب ألا يُستدعَى الاتجاه منفصلاً بعد جلب التفاصيل")


def test_decision_uses_one_dxy_snapshot_for_scoring_and_debug(monkeypatch):
    correlation = _DetailsCorrelationFeed()
    seen_trends = []
    monkeypatch.setattr("brain.decision_engine.detect_regime", lambda candles: SimpleNamespace(regime="range"))
    monkeypatch.setattr(
        "brain.decision_engine.compute_score",
        lambda candles, regime, trend: (
            seen_trends.append(trend) or SimpleNamespace(
                direction="none", confidence=55.0, reasons=["score"] , final_score=0.0
            )
        ),
    )
    engine = DecisionEngine(_PriceFeed(), _NewsFeed(), correlation)

    decision = engine.decide()

    assert correlation.details_calls == 1
    assert correlation.legacy_calls == 0
    assert seen_trends == ["up"]
    assert decision.dollar_trend_debug["trend"] == "up"
    assert decision.dollar_trend_debug["as_of_utc"] == _DxySnapshot.as_of_utc
    assert decision.dollar_trend_debug["data_snapshot_id"] == decision.data_snapshot_id
    assert decision.price_data_as_of_utc == "2026-11-10T14:45:00+00:00"
    assert decision.data_snapshot_id


def test_log_only_api_returns_decision_snapshot_without_refetch(monkeypatch):
    debug = {
        "trend": "down",
        "as_of_utc": "2026-11-10T14:45:00+00:00",
        "data_snapshot_id": "same-snapshot",
    }
    fixed = FinalDecision(
        action="none",
        position_size_usd=0.0,
        confidence=42.0,
        reasons=["test"],
        data_snapshot_id="same-snapshot",
        price_data_as_of_utc="2026-11-10T14:45:00+00:00",
        dollar_trend_debug=debug,
    )
    monkeypatch.setattr(api_main._engine, "decide", lambda **kwargs: fixed)

    from fastapi.testclient import TestClient
    response = TestClient(api_main.app).get("/decide-log-only")

    assert response.status_code == 200
    body = response.json()
    assert body["dollar_trend_debug"] == debug
    assert body["data_snapshot_id"] == "same-snapshot"
    assert body["price_data_as_of_utc"] == "2026-11-10T14:45:00+00:00"


def test_engine_rejects_gold_and_dxy_candles_with_different_timestamps(monkeypatch):
    correlation = _DetailsCorrelationFeed()
    correlation.get_dollar_trend_details = lambda: _DxySnapshot(
        as_of_utc="2026-11-10T14:30:00+00:00"
    )
    monkeypatch.setattr("brain.decision_engine.detect_regime", lambda candles: SimpleNamespace(regime="range"))
    engine = DecisionEngine(_PriceFeed(), _NewsFeed(), correlation)

    import pytest
    with pytest.raises(ValueError, match="لا يطابق توقيت DXY"):
        engine.decide()
