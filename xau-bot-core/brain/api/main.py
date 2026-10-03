"""
واجهة HTTP بسيطة — هذه هي النقطة التي سيستدعيها الـ cBot (C#) لاحقاً
عبر طلب HTTP عادي كل دورة (مثلاً كل إغلاق شمعة جديدة).

في v1: بيانات تجريبية (Mock) افتراضياً. يمكن اختيار مصدر الأسعار الحقيقي
(CTraderPriceFeed) عبر متغير البيئة USE_REAL_CTRADER_FEED=true — التبديل
يحدث هنا فقط (التركيب/التجميع) لأن DecisionEngine يعتمد على العقود
(interfaces.py) وليس على تطبيق معيّن. NewsFeed و CorrelationFeed يبقيان
Mock حالياً حتى إشعار آخر.

وضع المراقبة فقط (log-only): endpoint /decide-log-only يستدعي محرك القرار
ويُرجع النتيجة لكن لا ينفّذ أي شيء تنفيذي — النظام ككل لا يرسل أوامر
لأي وسيط ولا يتصل بأي Order/Execution API حتى إشعار آخر.
"""
import os

from fastapi import FastAPI

from brain.data.mock_price_feed import MockCorrelationFeed, MockNewsFeed, MockPriceFeed
from brain.data.real_correlation_feed import RealCorrelationFeed
from brain.decision_engine import DecisionEngine, FinalDecision
from brain.risk.risk_manager import RiskManager

app = FastAPI(title="XAU Bot Brain — v1")


def _build_price_feed():
    """اختيار مصدر الأسعار من البيئة فقط — نقطة التبديل الوحيدة بين Mock والحقيقي."""
    use_real = os.getenv("USE_REAL_CTRADER_FEED", "false").strip().lower() in ("1", "true", "yes")
    if use_real:
        # يستورد ويُنشأ هنا كي لا يتأثر تشغيل Mock الافتراضي بغياب اعتماد cTrader
        from brain.data.ctrader_price_feed import CTraderPriceFeed

        return CTraderPriceFeed()
    return MockPriceFeed()


def _build_correlation_feed(price_feed):
    """اختيار مصدر الاتجاه بالدولار من البيئة فقط.

    الوضع الافتراضي: MockCorrelationFeed (ثابت، لا اتصال شبكة أبداً).
    عند USE_REAL_CORRELATION_FEED=true: RealCorrelationFeed يُنشأ بنفس instance
    من price_feed الذي بُني لـ decide() — لا اتصال مستقل، والدولار يقيس
    ما يقاس بالسعر الفعلي الذي يُرمز به لاتجاه الذهب.
    إذا توقفت المكتبة المزيفة لكنه تم استخدام cTrader فعلاً (تحقق صريح),
    تُرفض هذه القيمة فوراً كي لا يصوّت القرار على بيانات غير محسوبة.
    """
    if os.getenv("USE_REAL_CORRELATION_FEED", "false").strip().lower() not in ("1", "true", "yes"):
        return MockCorrelationFeed()
    # فقط إذا كان مصدر الأسعار الحقيقي مستخدماً فعلاً في هذا العملية (تحقق صريح
    # — لا تخمين عن نوع الوسيط من صفة خارجية، وهذا اكتشاف في مكان واحد).
    from brain.data.real_correlation_feed import RealCorrelationFeed

    if not hasattr(price_feed, "get_candles"):
        return MockCorrelationFeed()
    return RealCorrelationFeed(price_feed=price_feed)


# نسخة واحدة مشتركة من محرك القرار وإدارة المخاطر (تحافظ على حالة قاطع الدائرة اليومي)
# price_feed يُبنى مرة واحدة فقط ويتشاركه engine و CorrelationFeed: نسختان من
# CTraderPriceFeed في نفس العملية تفتحان جلستَي cTrader متزامنتين فيتعطلّان
# معاً (موثَّق تجريبياً 2026-10-02: الجلسة الثانية تتصل TCP ثم لا تستجيب أبداً
# → TimeoutError(11s) ← 500 على كل نداء).
_price_feed = _build_price_feed()
_risk_manager = RiskManager()
_engine = DecisionEngine(
    price_feed=_price_feed,
    news_feed=MockNewsFeed(),
    correlation_feed=_build_correlation_feed(_price_feed),
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


@app.get("/decide-log-only", response_model=None)
def decide_log_only(symbol: str = "XAUUSD", timeframe: str = "M15") -> dict:
    """وضع المراقبة فقط: قرار + تسجيل في logs/decisions.jsonl — بلا أي تنفيذ.

    هذا endpoint مخصص للمراقبة حصراً: يستدعي DecisionEngine.decide() (الذي
    لا ينفّذ صفقات أصلاً في النظام الحالي) ويعيد النتيجة للقراءة فقط.
    لا اتصال بأي Order/Execution API من هنا أو من أي مسار آخر.
    """
    decision: FinalDecision = _engine.decide(symbol=symbol, timeframe=timeframe)
    dollar_debug: dict | None = None
    correlation = getattr(_engine, "correlation_feed", None)
    # حقل تشخيصي إضافي فقط للمحقق: لا يغيّر action أو position_size_usd أو أي
    # سلوك تنفيذي، ويبقى null عندما CorrelationFeed المعطل هو Mock (المهمة القديم).
    if correlation is not None and isinstance(correlation, RealCorrelationFeed):
        dollar_debug = correlation.get_dollar_trend_details()
    return {
        "mode": "log-only",
        "action": decision.action,
        "position_size_usd": decision.position_size_usd,
        "confidence": decision.confidence,
        "reasons": decision.reasons,
        "dollar_trend_debug": dollar_debug,
    }


@app.post("/trade-result")
def trade_result(pnl_usd: float) -> dict:
    """يُستدعى من cBot بعد إغلاق كل صفقة لتحديث حساب الخسارة اليومية."""
    _risk_manager.register_trade_result(pnl_usd)
    return {"status": "recorded"}
