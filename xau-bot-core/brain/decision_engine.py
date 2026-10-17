"""
محرك القرار: يجمع كل المراحل (بيئة → نظام → تصويت → مخاطر) في نداء واحد.

هذا هو المكوّن الوحيد الذي يستدعيه أي شيء خارجي (API أو cBot لاحقاً).
بقية المكوّنات الداخلية لا يجب أن تُستدعى مباشرة من خارج هذا الملف —
هذا يحافظ على وضوح نقطة الدخول الواحدة ويمنع التعقيد المبعثر.
"""
from dataclasses import asdict, dataclass

from brain.analysis.environment_filter import check_environment
from brain.analysis.indicators import atr
from brain.analysis.market_structure import analyze_market_structure, invalid_result
from brain.analysis.regime_detector import detect_regime
from brain.data.interfaces import CorrelationFeed, NewsFeed, PriceFeed
from brain.logger import log_decision
from brain.risk.risk_manager import RiskManager
from brain.scoring.weighted_voting import compute_score
from config.settings import settings


@dataclass
class FinalDecision:
    action: str  # "buy" | "sell" | "none"
    position_size_usd: float
    confidence: float
    reasons: list[str]
    # المرحلة 1: سياق البنية السوقية الإضافي (دعم/مقاومة/سيولة محتملة).
    # حقل اختياري بقيمة افتراضية None — أي مستدعٍ أو بناء قديم يبقى صالحاً دون
    # تعديل، والقرار (action/position_size_usd/confidence) لا يعتمد عليه إطلاقاً.
    market_structure: dict | None = None


class DecisionEngine:
    def __init__(
        self,
        price_feed: PriceFeed,
        news_feed: NewsFeed,
        correlation_feed: CorrelationFeed,
        risk_manager: RiskManager | None = None,
    ) -> None:
        self.price_feed = price_feed
        self.news_feed = news_feed
        self.correlation_feed = correlation_feed
        self.risk_manager = risk_manager or RiskManager()

    def decide(self, symbol: str = "XAUUSD", timeframe: str = "M15") -> FinalDecision:
        candles = self.price_feed.get_candles(symbol, timeframe, count=100)
        all_reasons: list[str] = []

        # المرحلة 1 (إضافية، لا تغيّر القرار): تحليل البنية السوقية من نفس الشموع
        # التي جلبتها هذه الدورة — بلا جلب ثانٍ ولا مصدر بيانات جديد.
        market_structure = self._analyze_market_structure(candles, timeframe)

        # المرحلة 1: فلتر البيئة
        upcoming_news = self.news_feed.get_upcoming_events(symbol, minutes_ahead=30)
        env_result = check_environment(candles, upcoming_news)
        all_reasons.extend(env_result.reasons)

        if not env_result.allowed:
            decision = FinalDecision(
                action="none", position_size_usd=0.0, confidence=0.0, reasons=all_reasons,
                market_structure=market_structure,
            )
            self._log(decision, stage_stopped="environment_filter")
            return decision

        # المرحلة 2: كشف النظام
        regime = detect_regime(candles)
        all_reasons.append(f"النظام الحالي: {regime.regime}")

        # المرحلة 3: التصويت المرجّح
        dollar_trend = self.correlation_feed.get_dollar_trend()
        scoring = compute_score(candles, regime, dollar_trend)
        all_reasons.extend(scoring.reasons)
        all_reasons.append(f"النتيجة النهائية: {scoring.final_score:.1f} (اتجاه={scoring.direction})")

        if scoring.direction == "none":
            decision = FinalDecision(
                action="none", position_size_usd=0.0, confidence=scoring.confidence, reasons=all_reasons,
                market_structure=market_structure,
            )
            self._log(decision, stage_stopped="weighted_voting")
            return decision

        # المرحلة 4: إدارة المخاطر (لها حق الفيتو)
        current_atr = atr(candles).iloc[-1]
        risk_decision = self.risk_manager.evaluate(scoring.confidence, current_atr)
        all_reasons.append(risk_decision.reason)

        if not risk_decision.approved:
            decision = FinalDecision(
                action="none", position_size_usd=0.0, confidence=scoring.confidence, reasons=all_reasons,
                market_structure=market_structure,
            )
            self._log(decision, stage_stopped="risk_manager")
            return decision

        decision = FinalDecision(
            action=scoring.direction,
            position_size_usd=risk_decision.position_size_usd,
            confidence=scoring.confidence,
            reasons=all_reasons,
            market_structure=market_structure,
        )
        self._log(decision, stage_stopped=None)
        return decision

    @staticmethod
    def _analyze_market_structure(candles, timeframe: str) -> dict | None:
        """يحسب سياق البنية السوقية الإضافي — معزول تماماً عن مسار القرار.

        أي فشل هنا يُسجَّل كسياق غير صالح ولا يمسّ stability أو القرار: القرار
        يبقى كما لو أن هذا التحليل غير موجود. عند تعطيل الميزة من الإعدادات
        تُعاد None (سياق غير متاح) دون أي تأثير آخر.
        """
        if not settings.market_structure_enabled:
            return None
        try:
            return analyze_market_structure(candles, timeframe=timeframe).to_dict()
        except Exception as error:  # noqa: BLE001 — فشل التحليل لا يُسقط الـBrain أبداً
            return invalid_result(
                f"analysis_failed:{type(error).__name__}", timeframe=timeframe
            ).to_dict()

    @staticmethod
    def _log(decision: FinalDecision, stage_stopped: str | None) -> None:
        payload = asdict(decision)
        payload["stage_stopped"] = stage_stopped
        log_decision(payload)
