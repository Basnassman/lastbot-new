"""
تطبيق حقيقي لعقد CorrelationFeed: اتجاه الدولار محسوباً من DXY تركيبي (Synthetic DXY).

القرار المعماري (موثَّق في استطلاع مصادر البيانات 2026-09):
- لا يوجد عقد DXY spot لدى الوسيط (cTrader)، وعقود DXY الآجلة تحتاج إدارة
  rollover وضبط انتهاء صلاحية يضيف طبقة فشل جديدة بلا داعٍ في v1.
- البديل المعتمد: حساب DXY وفق معادلة ICE الرسمية من 6 أزواج FX متاحة كـ spot
  عبر نفس عقد PriceFeed الحالي — أي مصدر بيانات واحد، اتصال واحد، وصفر
  اعتماديات جديدة.

المعادلة الرسمية (US Dollar Index — ICE):
    DXY = 50.14348112 × EURUSD^(-0.576) × USDJPY^(+0.136) × GBPUSD^(-0.119)
                       × USDCAD^(+0.091) × USDSEK^(+0.042) × USDCHF^(+0.036)
الأزواج المقتبسة عكسياً (EUR/USD و GBP/USD — الدولار في المقام) تأخذ أُساً سالباً،
والأزواج المقتبسة مباشرة (الدولار في الأساس) تأخذ أُساً موجباً. مجموع الأوزان المطلقة = 1.000.

الاتساق الداخلي المقصود: نافذة المقارنة N=10 شموع M15 مطابقة تماماً لنافذة
ema_slope(lookback=10) المستخدمة في كشف النظام (regime_detector.py) — فيقاس
اتجاه الدولار على نفس الأفق الزمني الذي يقاس به اتجاه الذهب، ولا يُضاف معامل
زمني جديد مختلف.

عتبة "flat": الفرق المطلق بين النقطتين أقل من 0.10 نقطة مؤشر (~0.1% من مستوى
~100) يُصنَّف flat. المنطق: على نافذة ساعتين ونصف (10×M15) يكون ضجيج تجميع
سبريد 6 أزواج وسوق FX الهادئ من رتبة بضعة مئات من النقاط (0.01–0.05 نقطة
مؤشر)، والحركات ذات المعنى الاتجاهي تتجاوز 0.10 عادةً. العتبة معلَّمة كوسيط
للصنف (flat_threshold) لمن يريد ضبطها لاحقاً دون تعديل الكود.

مبدأ "ممنوع الصمت" (نفس مبدأ CTraderPriceFeed): فشل جلب أي من الأزواج الستة،
أو نقص الشموع، أو سعر غير منطقي (≤ 0 / NaN) → استثناء صريح يسمّي الزوج والسبب.
لا قيمة افتراضية صامتة أبداً — محرك القرار لا يجوز أن يصوّت على اتجاه دولار
غير محسوب فعلياً.
"""
from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from brain.data.interfaces import CorrelationFeed, PriceFeed


class CorrelationFeedError(RuntimeError):
    """يُطلق عند أي فشل في جلب أو حساب بيانات الارتباط (DXY) — لا صمت أبداً."""


# مكونات DXY الرسمية: الزوج ← الأس الموقَّع (سالب للمقتبس عكسياً، موجب للمقتبس مباشرة)
_DXY_COMPONENTS: dict[str, float] = {
    "EURUSD": -0.576,
    "USDJPY": 0.136,
    "GBPUSD": -0.119,
    "USDCAD": 0.091,
    "USDSEK": 0.042,
    "USDCHF": 0.036,
}

# ثابت المعادلة الرسمية ICE (غير قابل للتعديل — قيمة معيارية لا قيمة إعداد)
_DXY_BASE_CONSTANT = 50.14348112

# هامش جلب إضافي: cTrader (مصادر شموع) لا يُعيد الشمعة الجارية قيد التكوين ويحسبها
# ضمن العدد المطلوب، فيصل عدد الشموع المغلقة أقل بواحد (موثَّق تجريبياً 2026-09-30:
# طلب 20 شمعة M15 → 19 مغلقة). نطلب فائضاً صغيراً ثم نأخذ آخر (lookback+1) شمعة
# مغلقة — سلوك صحيح على أي PriceFeed يرجع على الأقل العدد المطلوب.
_FETCH_BUFFER = 3


def compute_dxy_value(prices: Mapping[str, float]) -> float:
    """يحسب DXY من أسعار إغلاق للأزواج الستة وفق معادلة ICE الرسمية حرفياً."""
    value = _DXY_BASE_CONSTANT
    for symbol, exponent in _DXY_COMPONENTS.items():
        value *= prices[symbol] ** exponent
    return value


@dataclass(frozen=True)
class DxySnapshot:
    """نتيجة قياس DXY التركيبي — للتشخيص والتسجيل وللمقارنة اليدوية مع مصادر مرجعية."""

    current: float        # DXY عند آخر شمعة
    previous: float       # DXY عند شمعة قبل N شموع
    change: float         # current - previous
    threshold: float      # عتبة flat المستخدمة
    trend: str            # "up" | "down" | "flat"


class RealCorrelationFeed(CorrelationFeed):
    """اتجاه الدولار من DXY تركيبي محسوب عبر PriceFeed موجود (لا اتصال منفصل)."""

    def __init__(
        self,
        price_feed: PriceFeed,
        timeframe: str = "M15",
        lookback: int = 10,
        flat_threshold: float = 0.10,
    ) -> None:
        if lookback < 1:
            raise CorrelationFeedError(f"lookback يجب أن يكون ≥ 1، حصلنا على: {lookback}")
        if not timeframe:
            raise CorrelationFeedError("timeframe مطلوب ولا يمكن أن يكون فارغاً")
        if flat_threshold <= 0:
            raise CorrelationFeedError(
                f"flat_threshold يجب أن يكون موجباً، حصلنا على: {flat_threshold}"
            )
        self._price_feed = price_feed
        self.timeframe = timeframe
        self.lookback = lookback  # 10 × M15 = نفس نافذة ema_slope(lookback=10) في regime_detector
        self.flat_threshold = flat_threshold

    def get_dollar_trend(self) -> str:
        """نفس توقيع العقد تماماً: 'up' أو 'down' أو 'flat' لاتجاه الدولار."""
        return self.get_dollar_trend_details().trend

    def get_dollar_trend_details(self) -> DxySnapshot:
        """القياس الكامل (القيمتان والتغير والتصنيف) — يُستخدم للتشخيص والتوثيق."""
        current_prices, previous_prices = self._fetch_current_and_previous_prices()
        current_dxy = compute_dxy_value(current_prices)
        previous_dxy = compute_dxy_value(previous_prices)
        change = current_dxy - previous_dxy

        if abs(change) < self.flat_threshold:
            trend = "flat"
        elif change > 0:
            trend = "up"    # DXY ارتفع → الدولار يقوى
        else:
            trend = "down"  # DXY انخفض → الدولار يضعف

        return DxySnapshot(
            current=current_dxy,
            previous=previous_dxy,
            change=change,
            threshold=self.flat_threshold,
            trend=trend,
        )

    # --- الداخلية ---

    def _fetch_current_and_previous_prices(self) -> tuple[dict[str, float], dict[str, float]]:
        """يجلب إغلاقات الأزواج الستة ويعيد (أسعار النقطة الحالية، أسعار نقطة N شمعة مضت)."""
        needed = self.lookback + 1  # النقطة الحالية + نقطة المقارنة قبل N شمعة
        current: dict[str, float] = {}
        previous: dict[str, float] = {}

        for symbol in _DXY_COMPONENTS:
            closes = self._fetch_pair_closes(symbol, needed + _FETCH_BUFFER)
            previous[symbol] = closes[0]        # شمعة قبل N شمعة (أقدم شمعة في النافذة)
            current[symbol] = closes[-1]        # آخر شمعة متاحة

        return current, previous

    def _fetch_pair_closes(self, symbol: str, fetch_count: int) -> list[float]:
        """يجلب إغلاقات زوج واحد (بفائض جلب) ويقصّها إلى آخر lookback+1 شمعة مغلقة."""
        needed = self.lookback + 1
        try:
            candles = self._price_feed.get_candles(symbol, self.timeframe, fetch_count)
        except Exception as error:
            raise CorrelationFeedError(
                f"فشل جلب شموع {symbol} على الإطار {self.timeframe} "
                f"لازم حساب DXY: {error}"
            ) from error

        if candles is None or candles.empty:
            raise CorrelationFeedError(
                f"لم تُعَد أي شموع للزوج {symbol} على الإطار {self.timeframe} — "
                "لا يمكن حساب DXY بدون الأزواج الستة كاملة (ممنوع الصمت)"
            )

        closes = candles["close"].iloc[-needed:]  # آخر النافذة المغلقة المطلوبة
        if len(closes) < needed:
            raise CorrelationFeedError(
                f"الزوج {symbol}: يلزم {needed} شموع ({self.lookback}+1) لحساب DXY "
                f"على الإطار {self.timeframe}، وصلنا {len(closes)} فقط "
                f"(طلبنا {fetch_count} شمعة من المصدر)"
            )

        validated: list[float] = []
        for close in closes:
            value = float(close)
            if value != value or value <= 0:  # NaN أو سعر غير منطقي
                raise CorrelationFeedError(
                    f"الزوج {symbol}: سعر إغلاق غير صالح ({close}) — لا يمكن حساب DXY"
                )
            validated.append(value)
        return validated
