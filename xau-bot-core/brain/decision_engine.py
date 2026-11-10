"""
محرك القرار: يجمع كل المراحل (بيئة → نظام → تصويت → مخاطر) في نداء واحد.

هذا هو المكوّن الوحيد الذي يستدعيه أي شيء خارجي (API أو cBot لاحقاً).
بقية المكوّنات الداخلية لا يجب أن تُستدعى مباشرة من خارج هذا الملف —
هذا يحافظ على وضوح نقطة الدخول الواحدة ويمنع التعقيد المبعثر.
"""
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from uuid import uuid4

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
    # معرّف واحد يربط القرار وبياناته التشخيصية في دورة واحدة.
    data_snapshot_id: str | None = None
    price_data_as_of_utc: str | None = None
    dollar_trend_debug: dict | None = None


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
        data_snapshot_id = uuid4().hex
        price_data_as_of_utc = self._latest_candle_timestamp(candles)
        dollar_trend_debug: dict | None = None
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
                data_snapshot_id=data_snapshot_id,
                price_data_as_of_utc=price_data_as_of_utc,
            )
            self._log(decision, stage_stopped="environment_filter")
            return decision

        # المرحلة 2: كشف النظام
        regime = detect_regime(candles)
        all_reasons.append(f"النظام الحالي: {regime.regime}")

        # المرحلة 3: التصويت المرجّح
        # إن كان المصدر يوفر التفاصيل، نجلبها مرة واحدة ونستخرج منها الاتجاه
        # نفسه الذي سيُستخدم في التصويت؛ لا نعيد جلب DXY لأغراض التشخيص.
        get_details = getattr(self.correlation_feed, "get_dollar_trend_details", None)
        if callable(get_details):
            dxy_snapshot = get_details()
            dollar_trend = dxy_snapshot.trend
            if is_dataclass(dxy_snapshot):
                dollar_trend_debug = asdict(dxy_snapshot)
            elif isinstance(dxy_snapshot, dict):
                dollar_trend_debug = dict(dxy_snapshot)
            else:
                raise TypeError("get_dollar_trend_details يجب أن يعيد dataclass أو dict")
            dollar_trend_debug["data_snapshot_id"] = data_snapshot_id
            self._validate_aligned_data_timestamps(
                price_data_as_of_utc, dollar_trend_debug.get("as_of_utc")
            )
        else:
            dollar_trend = self.correlation_feed.get_dollar_trend()
        scoring = compute_score(candles, regime, dollar_trend)
        all_reasons.extend(scoring.reasons)
        all_reasons.append(f"النتيجة النهائية: {scoring.final_score:.1f} (اتجاه={scoring.direction})")

        if scoring.direction == "none":
            decision = FinalDecision(
                action="none", position_size_usd=0.0, confidence=scoring.confidence, reasons=all_reasons,
                market_structure=market_structure,
                data_snapshot_id=data_snapshot_id,
                price_data_as_of_utc=price_data_as_of_utc,
                dollar_trend_debug=dollar_trend_debug,
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
                data_snapshot_id=data_snapshot_id,
                price_data_as_of_utc=price_data_as_of_utc,
                dollar_trend_debug=dollar_trend_debug,
            )
            self._log(decision, stage_stopped="risk_manager")
            return decision

        decision = FinalDecision(
            action=scoring.direction,
            position_size_usd=risk_decision.position_size_usd,
            confidence=scoring.confidence,
            reasons=all_reasons,
            market_structure=market_structure,
            data_snapshot_id=data_snapshot_id,
            price_data_as_of_utc=price_data_as_of_utc,
            dollar_trend_debug=dollar_trend_debug,
        )
        self._log(decision, stage_stopped=None)
        return decision

    @staticmethod
    def _validate_aligned_data_timestamps(
        price_timestamp: str | None, dollar_timestamp: str | None
    ) -> None:
        """يرفض لقطة DXY إذا لم تتطابق شمعتها الأخيرة مع شمعة الذهب المستخدمة."""
        if not price_timestamp or not dollar_timestamp:
            raise ValueError("توقيت شمعة الذهب أو DXY مفقود؛ رُفضت لقطة البيانات")
        try:
            price_dt = datetime.fromisoformat(price_timestamp.replace("Z", "+00:00"))
            dollar_dt = datetime.fromisoformat(dollar_timestamp.replace("Z", "+00:00"))
            if price_dt.tzinfo is None or dollar_dt.tzinfo is None:
                raise ValueError("التوقيت يجب أن يحتوي على منطقة زمنية")
            price_dt = price_dt.astimezone(timezone.utc)
            dollar_dt = dollar_dt.astimezone(timezone.utc)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"توقيت لقطة البيانات غير صالح: {error}") from error
        if price_dt != dollar_dt:
            raise ValueError(
                "توقيت آخر شمعة للذهب لا يطابق توقيت DXY: "
                f"XAU={price_dt.isoformat()}, DXY={dollar_dt.isoformat()}"
            )

    @staticmethod
    def _latest_candle_timestamp(candles) -> str | None:
        """يعيد توقيت آخر شمعة مستخدمة دون التأثير على مسار القرار."""
        try:
            value = candles.iloc[-1]["timestamp"]
            timestamp = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
            if getattr(timestamp, "tzinfo", None) is None:
                return None
            return timestamp.astimezone(timezone.utc).isoformat()
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return None

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
