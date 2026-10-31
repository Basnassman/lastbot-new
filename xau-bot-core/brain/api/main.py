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
import math
import os
import threading
from datetime import datetime, timezone
from logging import getLogger
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from brain.data.mock_price_feed import MockCorrelationFeed, MockNewsFeed, MockPriceFeed
from brain.data.real_correlation_feed import RealCorrelationFeed
from brain.decision_engine import DecisionEngine, FinalDecision
from brain.risk.risk_manager import RiskManager
from brain.risk.trade_result_store import TradeResultStore
from config.settings import settings

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

# --- سجل تدقيق نتائج الصفقات + منع التكرار (الخطوتان 1–2) ---
_audit_log = getLogger("brain.api.trade_result")
_trade_result_store: TradeResultStore | None = None
_store_lock = threading.Lock()
_risk_update_lock = threading.Lock()


def _get_trade_result_store() -> TradeResultStore:
    """متجر التدقيق يُنشأ بأول نداء فعلي (لا أثر جانبي عند الاستيراد وحده)."""
    global _trade_result_store
    if _trade_result_store is None:
        with _store_lock:
            if _trade_result_store is None:
                _trade_result_store = TradeResultStore(settings.trade_result_db_path)
    return _trade_result_store


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
        # سياق إضافي فقط (المرحلة 1): لا يغيّر أي حقل أعلاه ولا أي سلوك تنفيذي.
        # None عند تعطيل الميزة؛ يبقى ظاهراً دائماً بمفتاح ثابت لتوافق المستدعين.
        "market_structure": decision.market_structure,
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
        # سياق إضافي فقط (المرحلة 1): الـcBot يتجاهل أي حقل لا يعرفه (System.Text.Json)،
        # فإضافته هنا لا تكسر أي مستدعٍ قائم — ويُثبَت ذلك فعلياً عبر الـharness.
        "market_structure": decision.market_structure,
    }


@app.post("/trade-result")
def trade_result(pnl_usd: str, deal_id: str | None = None) -> dict:
    """يُستدعى من cBot بعد إغلاق كل صفقة لتحديث صافي نتيجة اليوم (PnL موقّع).

    PnL موجب يزيد صافي اليوم، وسالب يُنقصه؛ قاطع الخسارة اليومية يعمل على الصافي
    المحقق فقط. العقد السابق محفوظ بالكامل: إرسال pnl_usd فقط يكفي، واستجابة النجاح
    تبقى حرفيًا {"status": "recorded"} (محميّة باختبار موجود). الحقل الاختياري
    deal_id (ClosingDealId/ClosingDeal.Id من cTrader عبر cBot) يفعّل منع التكرار
    المدعوم بقاعدة البيانات؛ بدونه تُسجَّل النتيجة وتُعالَج مع dedup_applied=0
    (توافق عكسي بلا ضمان dedup — يُسجَّل تحذير تشغيلي عند كل طلب بلا معرّف).

    الاستجابات الموثّقة:
      200 {"status": "recorded"}       — عولجت وسُجِّلت؛ صافي اليوم حُدِّث بالقيمة
                                          الموقّعة (موجب أو سالب)
      200 {"status": "duplicate", ...} — نفس deal_id سبقت معالجتها؛ العداد لم يُحدَّث
                                          مرة ثانية ويُسجَّل محاولة التكرار للتدقيق
      400 {"status": "rejected", ...}  — pnl غير رقمي/غير محدود (NaN/Infinity) أو
                                          deal_id فارغ المساحة
      500 {"status": "error", ...}     — استثناء أثناء التطبيق؛ مآل العداد ملتبس،
                                          ولا ينبغي إعادة التطبيق تلقائيًا
      503 {"status": "error", ...}     — سجل التدقيق غير متاح/تعذّر تثبيته أو السجل
                                          unresolved؛ يلزم تقرير/مراجعة إدارية
    """
    received_at = datetime.now(timezone.utc).isoformat()
    event_id = uuid4().hex
    account_scope = (settings.ctrader_account_id or "").strip()

    # تحقق رقمي صارم: النص يُحلَّل يدويًا لنرفض NaN/Infinity وكل ما لا يُحلل،
    # مع تسجيل محاولة الرفض في سجل التدقيق.
    try:
        value: float | None = float(pnl_usd)
    except (TypeError, ValueError):
        value = None
    if value is None or not math.isfinite(value):
        _safe_record_rejected(event_id, deal_id, account_scope, pnl_usd, received_at,
                              "pnl_usd غير رقمي أو غير محدود (NaN/Infinity)")
        return JSONResponse(status_code=400, content={"status": "rejected", "error": "invalid_pnl_usd"})

    if deal_id is not None:
        deal_id = deal_id.strip()
        if not deal_id:
            _safe_record_rejected(event_id, deal_id, account_scope, pnl_usd, received_at,
                                  "deal_id فارغ بعد التنظيف")
            return JSONResponse(status_code=400, content={"status": "rejected", "error": "empty_deal_id"})

    environment = (settings.ctrader_environment or "").strip().lower()
    if environment not in ("demo", "live"):
        _audit_log.error("trade-result rejected: cTrader environment is not explicitly demo or live")
        return JSONResponse(status_code=503, content={"status": "error", "error": "invalid_ctrader_environment"})
    valid_account_scope = account_scope.isdigit() and int(account_scope) > 0
    if (environment == "live" or deal_id is not None) and not valid_account_scope:
        _audit_log.error("trade-result rejected: account scope is missing or invalid")
        return JSONResponse(status_code=503, content={"status": "error", "error": "invalid_account_scope"})
    if valid_account_scope:
        account_scope = str(int(account_scope))  # canonical account key; avoid 00123 vs 123 scopes

    # الخطوة 1+2: ادّعاء ذري بمفتاح فريد على مستوى القاعدة قبل أي تحديث للعداد
    try:
        store = _get_trade_result_store()
        outcome = store.claim(
            event_id=event_id, deal_id=deal_id, account_scope=account_scope,
            pnl_usd=value, received_at_utc=received_at,
        )
    except Exception as error:  # noqa: BLE001 — أي فشل تخزين = لا اعتراف بالنجاح
        _audit_log.warning("trade-result audit store unavailable: %s", type(error).__name__)
        return JSONResponse(status_code=503, content={"status": "error", "error": "audit_store_unavailable"})

    if not outcome.claimed:
        if outcome.status == "applied":
            return {"status": "duplicate", "deal_id": deal_id, "first_seen_status": outcome.status}
        # سجل قائم غير applied: لا نعد الطلب ناجحاً ولا نعيد تطبيق PnL.
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "trade_result_unresolved",
                     "deal_id": deal_id, "first_seen_status": outcome.status},
        )

    if deal_id is None:
        # لا يوجد معرّف إغلاق من العميل: لا يمكن اشتقاق هوية ثابتة داخل Brain،
        # فكل إعادة إرسال لنفس صفقة الإغلاق ستُطبَّق مرة أخرى على صافي اليوم.
        # التحذير تشغيلي فقط — لا نرفض الطلب ولا نخترع معرّفًا (قد يضيّع PnL حقيقيًا).
        _audit_log.warning(
            "trade-result accepted without deal_id (event %s): dedup_applied=0, "
            "a retry of the same closing deal cannot be detected and will be applied again",
            outcome.event_id,
        )

    # حالة applying دائمة قبل استدعاء المدير لتمييز الحجز السابق عن النافذة الملتبسة.
    try:
        store.mark_applying(outcome.event_id)
    except Exception as error:  # noqa: BLE001 — لا يمكن أن نثبت أن التطبيق لم يبدأ بعد عطل/تعطل
        _audit_log.warning("could not persist applying state for event %s: %s", outcome.event_id, type(error).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "trade_result_unresolved", "deal_id": deal_id},
        )

    counter_updated = False
    try:
        with _risk_update_lock:
            _risk_manager.register_trade_result(value)
            counter_updated = True
    except Exception as error:  # noqa: BLE001 — قد يكون العداد تغير قبل الاستثناء
        try:
            store.mark_error(outcome.event_id, f"register_trade_result failed: {type(error).__name__}")
        except Exception:  # noqa: BLE001 — السجل يبقى claimed/applying؛ قد يكون العداد تغير
            _audit_log.warning("could not mark error for event %s", outcome.event_id)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "risk_update_unresolved", "deal_id": deal_id},
        )

    try:
        store.mark_applied(outcome.event_id, datetime.now(timezone.utc).isoformat())
    except Exception as error:  # noqa: BLE001 — نتيجة التثبيت/العداد لا يمكن حسمها
        if counter_updated:
            try:
                store.mark_error(
                    outcome.event_id,
                    "finalize failed after risk manager returned",
                    risk_manager_returned=True,
                )
            except Exception:  # noqa: BLE001 — لا نخفي احتمال تحديث العداد
                _audit_log.warning("could not persist unresolved finalize for event %s", outcome.event_id)
        _audit_log.warning("finalize unresolved for event %s: %s", outcome.event_id, type(error).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "trade_result_unresolved", "deal_id": deal_id},
        )

    return {"status": "recorded"}


def _safe_record_rejected(
    event_id: str, deal_id: str | None, account_scope: str,
    raw_pnl: str, received_at: str, detail: str,
) -> None:
    """تسجيل أفضل-ما-يُمكن لطلب مرفوض؛ فشل التسجيل لا يُغيّر رفض الطلب الآمن."""
    try:
        _get_trade_result_store().record_rejected(
            event_id=event_id, deal_id=deal_id, account_scope=account_scope,
            raw_pnl=raw_pnl, received_at_utc=received_at, detail=detail,
        )
    except Exception as error:  # noqa: BLE001
        _audit_log.warning("could not record rejected trade-result: %s", type(error).__name__)
