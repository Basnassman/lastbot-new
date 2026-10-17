"""
اختبارات تكامل/انحدار للمرحلة 1 — تتحقق من عدم كسر أي سلوك قائم.

الهدف: إثبات أن سياق البنية السوقية إضافي بالكامل — القرار، حجم المخاطرة،
السجلات، عقد الـAPI، وقيد Demo/live كلها كما كانت. لا يُغيّر أي اختبار هنا
سلوكاً قائماً؛ بل يثبّت أن الإضافة لا تمسّه.
"""
import dataclasses
import inspect
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from brain.api import main as api_main
from brain.data.interfaces import CorrelationFeed, NewsFeed, PriceFeed
from brain.decision_engine import DecisionEngine, FinalDecision
from brain.risk.risk_manager import RiskManager
from config.settings import settings


# ---------------------------------------------------------------------------
# بدائل memory-only لعقود البيانات (بلا شبكة، بلا عشوائية)
# ---------------------------------------------------------------------------
def _engine_candles(n: int = 60) -> pd.DataFrame:
    closes = np.array([2000.0 + 6.0 * np.sin(index / 4.0) for index in range(n)], dtype=float)
    highs = closes + 1.0
    lows = closes - 1.0
    timestamps = pd.date_range("2026-10-14 00:00", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame(
        {"timestamp": timestamps, "open": closes, "high": highs, "low": lows, "close": closes, "volume": 100}
    )


class _StubPriceFeed(PriceFeed):
    def __init__(self, candles: pd.DataFrame) -> None:
        self._candles = candles

    def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        return self._candles.copy()


class _StubNewsFeed(NewsFeed):
    def get_upcoming_events(self, symbol: str, minutes_ahead: int) -> list[dict]:
        return []


class _StubCorrelationFeed(CorrelationFeed):
    def get_dollar_trend(self) -> str:
        return "flat"


def _make_engine() -> DecisionEngine:
    return DecisionEngine(
        price_feed=_StubPriceFeed(_engine_candles()),
        news_feed=_StubNewsFeed(),
        correlation_feed=_StubCorrelationFeed(),
    )


# ---------------------------------------------------------------------------
# 14) توافق خلفي مع المستدعين القدامى
# ---------------------------------------------------------------------------
def test_final_decision_legacy_construction_still_valid():
    legacy = FinalDecision(action="none", position_size_usd=0.0, confidence=0.0, reasons=["سبب"])
    assert legacy.market_structure is None

    positional = FinalDecision("buy", 5.0, 90.0, ["r"])
    assert (positional.action, positional.position_size_usd, positional.confidence) == ("buy", 5.0, 90.0)
    assert positional.market_structure is None


# ---------------------------------------------------------------------------
# القرار لا يتأثر بسياق البنية السوقية (Decision preservation)
# ---------------------------------------------------------------------------
def test_decide_attaches_context_without_changing_decision(monkeypatch):
    logged: list[dict] = []
    monkeypatch.setattr("brain.decision_engine.log_decision", logged.append)

    enabled_engine = _make_engine()
    with_context = enabled_engine.decide()

    assert with_context.market_structure is not None
    assert with_context.market_structure["valid"] is True
    assert with_context.market_structure["support_zones"] is not None
    assert json.dumps(with_context.market_structure)  # قابل للتحويل إلى JSON (أنواع أصلية)

    # سجّل القرار قبل الإضافة للمقارنة
    control_reasons = list(with_context.reasons)

    # عطّل الميزة تماماً — يجب أن يبقى القرار مطابقاً حرفياً
    disabled = dataclasses.replace(settings, market_structure_enabled=False)
    monkeypatch.setattr("brain.decision_engine.settings", disabled)
    disabled_engine = _make_engine()
    without_context = disabled_engine.decide()

    assert without_context.market_structure is None
    assert without_context.action == with_context.action
    assert without_context.position_size_usd == with_context.position_size_usd
    assert without_context.confidence == with_context.confidence
    assert without_context.reasons == control_reasons


def test_decision_unchanged_when_analysis_raises(monkeypatch):
    logged: list[dict] = []
    monkeypatch.setattr("brain.decision_engine.log_decision", logged.append)

    baseline = _make_engine().decide()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated analysis failure")

    monkeypatch.setattr("brain.decision_engine.analyze_market_structure", _boom)
    failing = _make_engine().decide()

    # القرار لم يتغيّر إطلاقاً
    assert failing.action == baseline.action
    assert failing.position_size_usd == baseline.position_size_usd
    assert failing.confidence == baseline.confidence
    assert failing.reasons == baseline.reasons

    # والسياق صار غير صالح بسبب موثَّق بدل الانهيار
    assert failing.market_structure is not None
    assert failing.market_structure["valid"] is False
    assert failing.market_structure["reason"].startswith("analysis_failed")
    assert failing.market_structure["support_zones"] == []


def test_analysis_failure_does_not_propagate_to_logging(monkeypatch):
    logged: list[dict] = []
    monkeypatch.setattr("brain.decision_engine.log_decision", logged.append)
    monkeypatch.setattr(
        "brain.decision_engine.analyze_market_structure",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")),
    )
    _make_engine().decide()
    assert len(logged) == 1  # السجل كُتب كما في أي دورة عادية


# ---------------------------------------------------------------------------
# 15 و16) عقد الـAPI وشكل الاستجابة الذي يستهلكه الـcBot
# ---------------------------------------------------------------------------
def test_api_log_only_response_keeps_contract_and_adds_context(monkeypatch):
    fixed = FinalDecision(
        action="buy",
        position_size_usd=5.0,
        confidence=88.0,
        reasons=["reason-a"],
        market_structure={"valid": True, "reason": "ok"},
    )
    monkeypatch.setattr(api_main._engine, "decide", lambda symbol="XAUUSD", timeframe="M15": fixed)

    client = TestClient(api_main.app)
    response = client.get("/decide-log-only", params={"symbol": "XAUUSD", "timeframe": "M15"})
    assert response.status_code == 200
    body = response.json()

    # الحقول القديمة كلها كما هي (نفس الأسماء والأنواع)
    assert body["mode"] == "log-only"
    assert body["action"] == "buy"
    assert body["position_size_usd"] == 5.0
    assert body["confidence"] == 88.0
    assert body["reasons"] == ["reason-a"]
    assert "dollar_trend_debug" in body
    # الحقل الإضافي الوحيد
    assert body["market_structure"] == {"valid": True, "reason": "ok"}


def test_api_decide_response_keeps_contract_and_adds_context(monkeypatch):
    fixed = FinalDecision(
        action="none",
        position_size_usd=0.0,
        confidence=0.0,
        reasons=[],
        market_structure={"valid": False, "reason": "insufficient_data"},
    )
    monkeypatch.setattr(api_main._engine, "decide", lambda symbol="XAUUSD", timeframe="M15": fixed)

    client = TestClient(api_main.app)
    body = client.get("/decide", params={"symbol": "XAUUSD", "timeframe": "M15"}).json()
    assert body["action"] == "none"
    assert body["position_size_usd"] == 0.0
    assert body["confidence"] == 0.0
    assert body["reasons"] == []
    assert body["market_structure"]["reason"] == "insufficient_data"


def test_api_health_unchanged():
    client = TestClient(api_main.app)
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 21) تقرير نتيجة الصفقة محفوظ
# ---------------------------------------------------------------------------
def test_trade_result_endpoint_still_records(monkeypatch):
    fresh = RiskManager()
    monkeypatch.setattr(api_main, "_risk_manager", fresh)

    client = TestClient(api_main.app)
    response = client.post("/trade-result", params={"pnl_usd": -12.5})
    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}
    assert fresh._daily_loss_usd == pytest.approx(12.5)  # الخسارة اليومية تحدّثت فعلاً


# ---------------------------------------------------------------------------
# 17) حجم المخاطرة لم يتغيّر
# ---------------------------------------------------------------------------
def test_position_sizing_behaviour_unchanged():
    manager = RiskManager()

    approved_high = manager.evaluate(confidence=95.0, atr_value=1.5)
    expected_high = settings.account_balance_usd * (settings.risk_per_trade_percent / 100.0) * 1.0
    assert approved_high.approved is True
    assert approved_high.position_size_usd == pytest.approx(round(expected_high, 2))

    approved_mid = manager.evaluate(confidence=settings.decision_score_threshold, atr_value=1.5)
    expected_mid = settings.account_balance_usd * (settings.risk_per_trade_percent / 100.0) * 0.5
    assert approved_mid.approved is True
    assert approved_mid.position_size_usd == pytest.approx(round(expected_mid, 2))

    rejected = manager.evaluate(confidence=95.0, atr_value=0.0)
    assert rejected.approved is False and rejected.position_size_usd == 0.0


def test_risk_manager_signature_unchanged():
    """لا حقول جديدة ولا معاملات جديدة على واجهة إدارة المخاطر."""
    assert list(inspect.signature(RiskManager.evaluate).parameters) == ["self", "confidence", "atr_value"]
    assert list(inspect.signature(RiskManager.register_trade_result).parameters) == ["self", "pnl_usd"]


# ---------------------------------------------------------------------------
# وحدة التحليل لا تُنفّذ صفقات ولا تتصل بأي شبكة
# ---------------------------------------------------------------------------
def test_market_structure_module_has_no_execution_or_network_paths():
    import ast

    import brain.analysis.market_structure as module

    source = inspect.getsource(module)
    for forbidden_call in ("ExecuteMarketOrder", "ClosePosition", "ModifyPosition", "PlaceLimitOrder", "PlaceStopOrder"):
        assert forbidden_call not in source

    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"ctrader_open_api", "twisted", "socket", "requests", "urllib", "http", "aiohttp"})


# ---------------------------------------------------------------------------
# 18 و20 و22) مسار التنفيذ في الـcBot سليم ولم يُعدَّل (تحقق مصدري)
# ---------------------------------------------------------------------------
CBOT_SOURCE = Path(__file__).resolve().parents[2] / "executor" / "XauBotExecutor" / "XauBotExecutor.cs"


def test_cbot_execution_path_is_intact_and_unmodified():
    if not CBOT_SOURCE.exists():
        pytest.skip(f"cBot source not present in this checkout: {CBOT_SOURCE}")

    source = CBOT_SOURCE.read_text(encoding="utf-8")

    # SL/TP القائم على ATR ونسبة RR ما زالا موجودين بلا تغيير
    assert "StopLossAtrMultiplier" in source
    assert "RiskRewardRatio" in source
    assert "ComputeAtr()" in source
    assert "ExecuteMarketOrder" in source

    # قيد Demo/live محفوظ
    assert "Account.IsLive" in source
    assert "Stop();" in source

    # قناة الاتصال نفسها محفوظة
    assert "decide-log-only" in source
    assert "/trade-result" in source

    # الـcBot لم يُعدَّل لاستهلاك السياق الجديد — الإضافة لا تحتاجه
    assert "market_structure" not in source


# ---------------------------------------------------------------------------
# قيم إعدادات المرحلة 1 موجودة وصالحة النوع
# ---------------------------------------------------------------------------
def test_phase1_settings_have_safe_types_and_defaults():
    assert isinstance(settings.market_structure_enabled, bool)
    assert isinstance(settings.market_structure_swing_window, int)
    assert settings.market_structure_swing_window >= 1
    assert settings.market_structure_merge_tolerance_atr >= 0.0
    assert settings.market_structure_min_reactions >= 0
    assert settings.market_structure_max_zones >= 1
    assert settings.market_structure_recency_bars >= 1
    assert settings.market_structure_max_liquidity_levels >= 1
