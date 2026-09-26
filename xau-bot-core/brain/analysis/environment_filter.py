"""
المرحلة 1: فلتر البيئة.

قبل التفكير بأي صفقة، نتحقق: هل الظروف الحالية صالحة للتداول أصلاً؟
هذا الفلتر له حق "الفيتو" الكامل — لو رفض، لا تُستدعى بقية المراحل إطلاقاً.
"""
from dataclasses import dataclass

import pandas as pd

from brain.analysis.indicators import atr


@dataclass
class EnvironmentResult:
    allowed: bool
    reasons: list[str]


def check_environment(
    candles: pd.DataFrame,
    upcoming_news: list[dict],
    news_blackout_minutes: int = 15,
) -> EnvironmentResult:
    reasons: list[str] = []
    allowed = True

    # 1. فحص الأخبار عالية التأثير القريبة
    for event in upcoming_news:
        if event.get("impact") == "high" and event.get("minutes_until", 999) <= news_blackout_minutes:
            allowed = False
            reasons.append(
                f"خبر عالي التأثير قريب: {event.get('name')} خلال {event.get('minutes_until')} دقيقة"
            )

    # 2. فحص التقلب — يجب ألا يكون راكداً جداً ولا منفجراً جداً
    current_atr = atr(candles).iloc[-1]
    avg_atr = atr(candles).mean()

    if avg_atr > 0:
        ratio = current_atr / avg_atr
        if ratio < 0.4:
            allowed = False
            reasons.append(f"السوق راكد جداً (ATR الحالي أقل من 40% من المتوسط، النسبة={ratio:.2f})")
        elif ratio > 3.0:
            allowed = False
            reasons.append(f"تقلب مفاجئ شديد (ATR الحالي أعلى بـ{ratio:.1f}x من المتوسط) — احذر السلبج")

    if allowed:
        reasons.append("البيئة مناسبة للتداول")

    return EnvironmentResult(allowed=allowed, reasons=reasons)
