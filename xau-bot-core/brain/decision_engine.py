"""
محرك القرار: يجمع كل المراحل (بيئة → نظام → تصويت → مخاطر) في نداء واحد.

هذا هو المكوّن الوحيد الذي يستدعيه أي شيء خارجي (API أو cBot لاحقاً).
بقية المكوّنات الداخلية لا يجب أن تُستدعى مباشرة من خارج هذا الملف —
هذا يحافظ على وضوح نقطة الدخول الواحدة ويمنع التعقيد المبعثر.
"""
from dataclasses import asdict, dataclass

from brain.analysis.environment_filter import check_environment
from brain.analysis.indicators import atr
from brain.analysis.regime_detector import detect_regime
from brain.data.interfaces import CorrelationFeed, NewsFeed, PriceFeed
from brain.logger import log_decision
from brain.risk.risk_manager import RiskManager
from brain.scoring.weighted_voting import compute_score


@dataclass
class FinalDecision:
    action: str  # "buy" | "sell" | "none"
    position_size_usd: float
    confidence: float
    reasons: list[str]


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

        # المرحلة 1: فلتر البيئة
        upcoming_news = self.news_feed.get_upcoming_events(symbol, minutes_ahead=30)
        env_result = check_environment(candles, upcoming_news)
        all_reasons.extend(env_result.reasons)

        if not env_result.allowed:
            decision = FinalDecision(action="none", position_size_usd=0.0, confidence=0.0, reasons=all_reasons)
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
                action="none", position_size_usd=0.0, confidence=scoring.confidence, reasons=all_reasons
            )
            self._log(decision, stage_stopped="weighted_voting")
            return decision

        # المرحلة 4: إدارة المخاطر (لها حق الفيتو)
        current_atr = atr(candles).iloc[-1]
        risk_decision = self.risk_manager.evaluate(scoring.confidence, current_atr)
        all_reasons.append(risk_decision.reason)

        if not risk_decision.approved:
            decision = FinalDecision(
                action="none", position_size_usd=0.0, confidence=scoring.confidence, reasons=all_reasons
            )
            self._log(decision, stage_stopped="risk_manager")
            return decision

        decision = FinalDecision(
            action=scoring.direction,
            position_size_usd=risk_decision.position_size_usd,
            confidence=scoring.confidence,
            reasons=all_reasons,
        )
        self._log(decision, stage_stopped=None)
        return decision

    @staticmethod
    def _log(decision: FinalDecision, stage_stopped: str | None) -> None:
        payload = asdict(decision)
        payload["stage_stopped"] = stage_stopped
        log_decision(payload)
