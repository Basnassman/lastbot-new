"""
المرحلة 3: نظام التصويت المرجّح (مبسّط لـ v1 بـ 3 مكونات فقط).

كل مكوّن يُعيد نقطة بين -100 و +100:
  +100 = دعم قوي جداً للشراء
  -100 = دعم قوي جداً للبيع
     0 = محايد

النتيجة النهائية = متوسط مرجّح لكل المكونات.
القرار (شراء/بيع/لا شيء) يعتمد على تجاوز |النتيجة| لعتبة معينة.
"""
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from brain.analysis.indicators import ema, rsi
from brain.analysis.regime_detector import RegimeResult
from config.settings import settings

Direction = Literal["buy", "sell", "none"]


@dataclass
class ScoringBreakdown:
    trend_score: float
    momentum_score: float
    dollar_score: float
    final_score: float  # من -100 إلى +100
    direction: Direction
    confidence: float  # 0 إلى 100 — |final_score|
    reasons: list[str] = field(default_factory=list)


def _trend_component(candles: pd.DataFrame, regime: RegimeResult) -> tuple[float, str]:
    """يدعم الاتجاه السائد فقط — لا يعطي إشارة قوية في حالة التذبذب."""
    if regime.regime == "trend_up":
        score = min(100.0, abs(regime.slope_value) * 20)
        return score, f"ترند صاعد واضح (ميل EMA={regime.slope_value:.2f})"
    if regime.regime == "trend_down":
        score = -min(100.0, abs(regime.slope_value) * 20)
        return score, f"ترند هابط واضح (ميل EMA={regime.slope_value:.2f})"
    return 0.0, "السوق متذبذب — لا إشارة اتجاه واضحة"


def _momentum_component(candles: pd.DataFrame) -> tuple[float, str]:
    current_rsi = rsi(candles["close"]).iloc[-1]
    if current_rsi >= 70:
        return -60.0, f"RSI متشبع شراء ({current_rsi:.1f}) — احتمال ارتداد هابط"
    if current_rsi <= 30:
        return 60.0, f"RSI متشبع بيع ({current_rsi:.1f}) — احتمال ارتداد صاعد"
    # كلما اقترب من المنتصف (50) كلما قلّ وزن الزخم
    score = (current_rsi - 50) * 1.2
    return score, f"RSI محايد نسبياً ({current_rsi:.1f})"


def _dollar_component(dollar_trend: str) -> tuple[float, str]:
    """الذهب يتحرك عكسياً مع الدولار غالباً."""
    if dollar_trend == "down":
        return 70.0, "الدولار في اتجاه هابط — يدعم صعود الذهب"
    if dollar_trend == "up":
        return -70.0, "الدولار في اتجاه صاعد — يضغط على الذهب"
    return 0.0, "الدولار في حالة تذبذب — لا إشارة واضحة"


def compute_score(
    candles: pd.DataFrame,
    regime: RegimeResult,
    dollar_trend: str,
) -> ScoringBreakdown:
    trend_score, trend_reason = _trend_component(candles, regime)
    momentum_score, momentum_reason = _momentum_component(candles)
    dollar_score, dollar_reason = _dollar_component(dollar_trend)

    weighted_sum = (
        trend_score * settings.weight_trend
        + momentum_score * settings.weight_momentum
        + dollar_score * settings.weight_dollar_correlation
    ) / 100.0

    if weighted_sum >= settings.decision_score_threshold:
        direction: Direction = "buy"
    elif weighted_sum <= -settings.decision_score_threshold:
        direction = "sell"
    else:
        direction = "none"

    return ScoringBreakdown(
        trend_score=trend_score,
        momentum_score=momentum_score,
        dollar_score=dollar_score,
        final_score=weighted_sum,
        direction=direction,
        confidence=abs(weighted_sum),
        reasons=[trend_reason, momentum_reason, dollar_reason],
    )
