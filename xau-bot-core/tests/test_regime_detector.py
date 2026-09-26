"""
اختبار انحدار لإصلاح حادثة 15 سبتمبر (قرار 2026-09-20): شرط التأكيد السعري.

الإصلاح: لا يُصنَّف trend_up إلا إذا slope > flat_threshold و close > ema(21)
(والمعكوس لـ trend_down). ميل واضح دون تأكيد السعر → range.

البيانات هنا **حقيقية**: آخر 100 شمعة M15 مغلقة قبل كل نقطة قرار، مأخوذة من
cTrader عبر scripts/diagnose_regime.py (المحاكاة A — طابقت أرقام المحرك في
logs/decisions.jsonl). القيم الفيزيائية عند 19:39:
  close=4301.19 | slope(21,10)=+8.156 | ema(21)=4295.712 → close > ema(21)

هذا يعني أن اللحظة المستهدفة **لا تستوفي** شرط `close < ema(21)` المحدد في
قرار الإصلاح — السعر عند 19:39 كان على بُعد 7.5 نقطة من الذروة (إغلاق 19:00
بـ 4308.70) وقبل بدء النزف بصرياً، فالسعر كان فوق EMA21 فعلاً رغم بدء تباطؤ
الزخم (slope هبط من 8.88 عند الذروة إلى 8.16). لهذا:
- test_regression_2026_09_15_1939_post_peak_context يثبت **السلوك الفعلي
  الجديد** على تلك اللحظة بالضبط (trend_up مع توثيق أن close > ema21)، فينعكس
  فوراً لو غُيّرت المنطق أو معاملات الميل مستقبلاً — وهذا الغرض التصنيفي للاختبار.
- test_regression_slope_only_without_price_confirmation_is_range يثبت جوهر
  الإصلاح: ميل واضح دون تأكيد سعر → range.
- test_control_sept19_closed_market_satisfies_rule يثبت أن القاعدة تُبطل
  ترنداً كاملاً عند انقلاب السعر تحت EMA21 (نقطة ضبط سوق مغلق 19 سبتمبر:
  slope=+2.277 لكن close=4378.11 < ema21=4379.109 → range).

إعادة توليد البيانات إن احتجت:
  .venv/bin/python scripts/diagnose_regime.py
"""
import numpy as np
import pandas as pd

from brain.analysis.indicators import ema, ema_slope
from brain.analysis.regime_detector import RegimeResult, detect_regime

FLAT_THRESHOLD = 1.5


def _candles_from_closes(closes: list[float]) -> pd.DataFrame:
    """يبني DataFrame بنفس أعمدة العقد — detect_regime يستهلك close حصراً."""
    closes_arr = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": closes_arr - 0.5,
            "high": closes_arr + 1.0,
            "low": closes_arr - 1.0,
            "close": closes_arr,
            "volume": [100] * len(closes_arr),
        }
    )


# آخر 100 إغلاق M15 مغلقة قبل 2026-09-15T19:39:00Z (من diagnose_regime.py)
CLOSES_1939 = [
    4309.99, 4311.1, 4312.69, 4309.71, 4309.46, 4307.42, 4306.08, 4296.43, 4291.97, 4283.96,
    4287.82, 4296.72, 4294.77, 4298.6, 4297.09, 4295.8, 4295.66, 4295.39, 4292.24, 4289.93,
    4289.59, 4288.28, 4286.85, 4288.13, 4292.7, 4293.03, 4299.79, 4301.8, 4298.67, 4304.45,
    4307.36, 4309.96, 4311.51, 4314.07, 4310.86, 4310.87, 4308.69, 4303.63, 4307.37, 4303.64,
    4305.03, 4304.84, 4301.56, 4297.44, 4296.29, 4291.07, 4289.79, 4289.57, 4290.8, 4293.49,
    4288.13, 4292.01, 4283.89, 4280.6, 4267.9, 4273.27, 4267.59, 4262.38, 4265.66, 4266.99,
    4269.62, 4268.38, 4273.2, 4274.54, 4274.95, 4281.63, 4283.68, 4283.41, 4282.86, 4284.71,
    4278.64, 4277.84, 4281.25, 4290.44, 4286.84, 4287.39, 4289.73, 4294.88, 4294.7, 4284.14,
    4278.25, 4277.23, 4285.45, 4283.89, 4287.79, 4289.34, 4286.34, 4295.73, 4296.64, 4295.55,
    4295.18, 4293.1, 4293.33, 4294.97, 4302.29, 4304.95, 4304.83, 4308.7, 4300.35, 4301.19,
]

# آخر 100 إغلاق M15 مغلقة قبل 2026-09-19T19:52:14Z — نقطة الضبط (سوق مغلق)
CLOSES_SEPT19_CONTROL = [
    4354.71, 4343.35, 4345.32, 4346.26, 4343.15, 4342.27, 4341.29, 4341.59, 4344.57, 4344.91,
    4344.57, 4344.51, 4345.06, 4344.25, 4344.32, 4346.37, 4354.36, 4353.4, 4358.78, 4356.43,
    4358.22, 4359.84, 4347.26, 4348.36, 4350.42, 4352.97, 4351.71, 4346.85, 4344.8, 4340.85,
    4357.58, 4359.29, 4357.06, 4357.88, 4364.68, 4366.9, 4365.46, 4364.69, 4377.11, 4377.35,
    4389.04, 4392.56, 4396.22, 4393.51, 4390.51, 4395.72, 4393.77, 4393.42, 4390.45, 4387.95,
    4386.89, 4392.29, 4394.54, 4387.86, 4376.8, 4376.57, 4379.13, 4381.51, 4381.41, 4381.77,
    4388.25, 4377.8, 4375.91, 4381.35, 4373.45, 4358.55, 4369.2, 4368.98, 4371.43, 4367.18,
    4353.5, 4349.37, 4351.57, 4343.04, 4353.66, 4353.05, 4352.24, 4356.32, 4354.88, 4356.77,
    4361.85, 4377.71, 4378.08, 4382.85, 4385.46, 4386.9, 4393.17, 4393.2, 4392.2, 4390.73,
    4381.15, 4382.23, 4384.36, 4380.61, 4381.48, 4376.52, 4381.61, 4381.14, 4380.79, 4378.11,
]


def test_regression_2026_09_15_1939_post_peak_context():
    """سيناريو 19:39 بالبيانات الحقيقية: يثبت السلوك الفعلي بعد الإصلاح.

    أرقام اللحظة (متحقق منها عبر diagnose_regime.py): slope=+8.156 و
    ema(21)=4295.712 و close=4301.19 → السعر **فوق** EMA21، فالتصنيف بعد
    الإصلاح trend_up. انقلاب هذا الاختبار مستقبلاً يعني أن أرقام الميل أو
    مرجع التأكيد تغيّرت — إشارة مبكرة قبل أي اعتماد على التصنيف.
    """
    candles = _candles_from_closes(CLOSES_1939)
    result = detect_regime(candles, flat_threshold=FLAT_THRESHOLD)

    # أرقام النافذة الحقيقية نفسها (حماية إضافية لصحة البيانات المضمّنة)
    closes = candles["close"]
    assert float(closes.iloc[-1]) == 4301.19
    assert abs(ema_slope(closes, period=21, lookback=10) - 8.156) < 0.01
    assert abs(float(ema(closes, 21).iloc[-1]) - 4295.712) < 0.01
    assert float(closes.iloc[-1]) > float(ema(closes, 21).iloc[-1])  # السعر فوق EMA21 عند 19:39

    # السلوك بعد الإصلاح على هذه اللحظة تحديداً
    assert result.slope_value > FLAT_THRESHOLD
    assert result.regime == "trend_up"
    assert abs(result.confirmation_reference - 4295.712) < 0.01


def test_regression_slope_only_without_price_confirmation_is_range():
    """جوهر الإصلاح: ميل واضح دون تأكيد السعر → range (وليس ترند).

    نفس نافذة 19:39 بعد خفض آخر إغلاق تحت EMA21 (سيناريو "الزخم متأخر عن
    السعر"): الميل يبقى قوياً لكن السعر انقلب تحت المرجع — ممنوع trend_up.
    """
    closes = list(CLOSES_1939)
    closes[-1] = 4280.0  # تحت ema21 (~4295.4 بعد التعديل) بميل يبقى فوق العتبة
    candles = _candles_from_closes(closes)

    result = detect_regime(candles, flat_threshold=FLAT_THRESHOLD)

    assert result.slope_value > FLAT_THRESHOLD  # الميل وحده كان يكفي سابقاً
    assert result.regime == "range"             # الإصلاح: لا ترند بلا تأكيد سعر


def test_regression_trend_down_requires_price_confirmation():
    """الشرط المعكوس: ميل سالب واضح مع سعر فوق EMA21 → range وليس trend_down."""
    # نافذة هبوط خطية نظيفة: EMA21 هابطة بوضوح (الميل وحده كان يكفي سابقاً لـ trend_down)
    closes = list(np.linspace(4330.0, 4260.0, 100))
    reference = float(ema(_candles_from_closes(closes)["close"], 21).iloc[-1])
    # نرفع آخر إغلاق فوق مرجع EMA21 — الميل يبقى سالباً بوضوح لكن السعر انقلب فوق المرجع
    closes[-1] = reference + 5.0
    candles = _candles_from_closes(closes)

    result = detect_regime(candles, flat_threshold=FLAT_THRESHOLD)

    assert result.slope_value < -FLAT_THRESHOLD
    assert float(candles["close"].iloc[-1]) > result.confirmation_reference  # السعر فوق المرجع
    assert result.regime == "range"  # الإصلاح: لا ترند هابط بلا تأكيد سعر


def test_control_sept19_closed_market_satisfies_rule():
    """نقطة ضبط 19 سبتمبر (سوق مغلق): slope=+2.277 لكن close < ema21 → range.

    تثبت أن القاعدة الجديدة تُبطل ترنداً كاملاً عند انقلاب السعر تحت المرجع —
    وهذه هي الحالة التي أتت القاعدة لحلّها أصلاً.
    """
    candles = _candles_from_closes(CLOSES_SEPT19_CONTROL)
    result = detect_regime(candles, flat_threshold=FLAT_THRESHOLD)

    closes = candles["close"]
    assert float(closes.iloc[-1]) == 4378.11
    assert abs(result.slope_value - 2.277) < 0.01
    assert abs(result.confirmation_reference - 4379.109) < 0.01
    assert float(closes.iloc[-1]) < result.confirmation_reference  # الشرط محقق هنا

    assert result.regime == "range"


def test_public_signature_unchanged():
    """العقد العام: نفس المدخلات/المخرجات — القيمة القديمة RegimeResult(regime, slope) تبقى صحيحة."""
    candles = _candles_from_closes(CLOSES_1939)
    result = detect_regime(candles)

    assert isinstance(result.slope_value, float)
    assert result.regime in ("trend_up", "trend_down", "range")

    # البناء الموضعي القديم (بدون الحقل الجديد) يظل صالحاً — الحقل اختياري بقيمة افتراضية
    legacy = RegimeResult(regime="trend_up", slope_value=10.0)
    assert legacy.confirmation_reference is None

    # الاستدعاء بالعتبة الافتراضية يعطي نفس النتيجة فوق (لم تُغيَّر عتبة النطاق)
    assert detect_regime(candles, flat_threshold=FLAT_THRESHOLD).regime == result.regime
