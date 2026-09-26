"""
المرحلة 2: كشف نظام السوق (Regime Detection).

منطق بسيط ومفهوم بالكامل (وليس نموذج ML معقد) لـ v1:
- إذا كان ميل EMA واضحاً في اتجاه واحد → ترند
- إذا كان الميل شبه مستوٍ → تذبذب (Range)

قرار 2026-09-20 (إصلاح حادثة 15 سبتمبر 19:39): تأكيد سعري إلزامي للترند.
الميل وحده لا يكفي لأن الزخم متأخر عن السعر الفعلي — EMA تتذكّر الصعود القديم
بينما السعر انقلب. لذلك:
- trend_up لا يُصنَّف إلا إذا: slope > flat_threshold و close > ema(21)
- trend_down لا يُصنَّف إلا إذا: slope < -flat_threshold و close < ema(21)
- ميل واضح دون تأكيد السعر → range (وليس ترند)

قيد معروف (موثَّق 2026-09-21): قد يتأخر التصنيف خلال انزلاق تدريجي بعد قمة/قاع
قريب؛ تعويضه حالياً عبر عتبة القرار في طبقة التصويت، لا داخل هذه الدالة. لا
إصلاح هنا قبل دليل فعلي من بيانات حية على أثرٍ في قرار تداول.
"""
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from brain.analysis.indicators import ema, ema_slope

Regime = Literal["trend_up", "trend_down", "range"]

# دورة مرجع التأكيد السعري — نفس دورة حساب الميل كي لا يُضاف معامل جديد
_CONFIRMATION_PERIOD = 21


@dataclass
class RegimeResult:
    regime: Regime
    slope_value: float
    # مرجع التأكيد السعري المستخدم (EMA(21) على نفس النافذة) — للتشخيص والتسجيل.
    # حقل اختياري بقيمة افتراضية: أي مستدعٍ قديم يعمل كما هو دون تعديل.
    confirmation_reference: float | None = None


def detect_regime(candles: pd.DataFrame, flat_threshold: float = 1.5) -> RegimeResult:
    """صنّف نظام السوق (ترند صاعد/هابط/تذبذب) من إغلاقات الشموع.

    قيد معروف (موثَّق 2026-09-21): قد يتأخر التصنيف خلال انزلاق تدريجي بعد قمة/قاع
    قريب؛ تعويضه حالياً عبر عتبة القرار في طبقة التصويت، لا داخل هذه الدالة — لا
    نعالجه هنا نظرياً إلا بدليل فعلي من بيانات حية على أثرٍ في قرار تداول.
    """
    closes = candles["close"]
    if len(closes) == 0:
        # نفس سلوك النسخة السابقة مع مدخل فارغ: ميل 0 → range
        return RegimeResult(regime="range", slope_value=0.0)

    slope = ema_slope(closes, period=21, lookback=10)
    confirmation_reference = float(ema(closes, _CONFIRMATION_PERIOD).iloc[-1])
    current_close = float(closes.iloc[-1])

    if abs(slope) < flat_threshold:
        regime: Regime = "range"
    elif slope > 0:
        regime = "trend_up" if current_close > confirmation_reference else "range"
    else:
        regime = "trend_down" if current_close < confirmation_reference else "range"

    return RegimeResult(
        regime=regime,
        slope_value=slope,
        confirmation_reference=confirmation_reference,
    )
