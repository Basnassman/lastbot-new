"""
واجهة HTTP بسيطة — هذه هي النقطة التي سيستدعيها الـ cBot (C#) لاحقاً
عبر طلب HTTP عادي كل دورة (مثلاً كل إغلاق شمعة جديدة).

في v1: بيانات تجريبية (Mock). لاحقاً تُستبدل بمزوّد بيانات حقيقي
دون تغيير أي شيء في هذا الملف — لأن DecisionEngine يعتمد على
العقود (interfaces.py) وليس على تطبيق معيّن.
"""
from fastapi import FastAPI

from brain.data.mock_price_feed import MockCorrelationFeed, MockNewsFeed, MockPriceFeed
from brain.decision_engine import DecisionEngine, FinalDecision
from brain.risk.risk_manager import RiskManager

app = FastAPI(title="XAU Bot Brain — v1")

# نسخة واحدة مشتركة من محرك القرار وإدارة المخاطر (تحافظ على حالة قاطع الدائرة اليومي)
_risk_manager = RiskManager()
_engine = DecisionEngine(
    price_feed=MockPriceFeed(),
    news_feed=MockNewsFeed(),
    correlation_feed=MockCorrelationFeed(),
    risk_manager=_risk_manager,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/decide", response_model=None)
def decide(symbol: str = "XAUUSD", timeframe: str = "M15") -> dict:
    decision: FinalDecision = _engine.decide(symbol=symbol, timeframe=timeframe)
    return {
        "action": decision.action,
        "position_size_usd": decision.position_size_usd,
        "confidence": decision.confidence,
        "reasons": decision.reasons,
    }


@app.post("/trade-result")
def trade_result(pnl_usd: float) -> dict:
    """يُستدعى من cBot بعد إغلاق كل صفقة لتحديث حساب الخسارة اليومية."""
    _risk_manager.register_trade_result(pnl_usd)
    return {"status": "recorded"}
