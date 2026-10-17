"""
اختبارات المرحلة 1: تحليل البنية السوقية (دعم/مقاومة/سيولة محتملة).

البيانات هنا **مصنوعة يدوياً** بقيم صريحة قابلة للتحقق الحسابي (لا عشوائية)،
كي تكون كل نتيجة متوقعة قابلة للإثبات. نمرّر ``atr_value`` صريحاً لعزل
منطق التحليل عن تقلب البيانات (scale = 1.0 دائماً ما لم يُذكر غير ذلك).
"""
import numpy as np
import pandas as pd
import pytest

from brain.analysis.market_structure import (
    MarketStructure,
    _resolve_config,
    _swing_points,
    _zone_from_cluster,
    analyze_market_structure,
)


def _frame(highs, lows=None, closes=None, timestamps=None):
    """يبني DataFrame بنفس أعمدة عقد PriceFeed من قمم/قيعان صريحة."""
    highs = np.asarray(highs, dtype=float)
    n = len(highs)
    if lows is None:
        lows = highs - 2.0
    lows = np.asarray(lows, dtype=float)
    if closes is None:
        closes = (highs + lows) / 2.0
    closes = np.asarray(closes, dtype=float)
    if timestamps is None:
        timestamps = pd.date_range("2026-10-14 00:00", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame(
        {"timestamp": timestamps, "open": closes, "high": highs, "low": lows, "close": closes, "volume": 100}
    )


# قمم ذات قمم محلية مؤكدة عند الفهارس 2 و7 و12 و17 (القيم 12,13,14,13)
SWING_HIGHS = np.array(
    [10, 11, 12, 11, 10, 11, 12, 13, 12, 11, 12, 13, 14, 13, 12, 11, 12, 13, 12, 11], dtype=float
)


# ---------------------------------------------------------------------------
# 1) كشف قمم انعطاف مؤكدة
# ---------------------------------------------------------------------------
def test_confirmed_swing_high_detection():
    points = _swing_points(SWING_HIGHS, window=2, find_high=True)
    assert [index for index, _price in points] == [2, 7, 12, 17]
    assert [price for _index, price in points] == [12.0, 13.0, 14.0, 13.0]


# ---------------------------------------------------------------------------
# 2) كشف قيعان انعطاف مؤكدة
# ---------------------------------------------------------------------------
def test_confirmed_swing_low_detection():
    lows = 30.0 - SWING_HIGHS  # القيعان المحلية عند نفس الفهارس
    points = _swing_points(lows, window=2, find_high=False)
    assert [index for index, _price in points] == [2, 7, 12, 17]
    assert [price for _index, price in points] == [18.0, 17.0, 16.0, 17.0]


def test_developing_pivot_requires_future_candles_and_is_excluded():
    """قمة في الشمعة الأخيرة لا تُؤكَّد أبداً (لا شموع مستقبلية) ⇒ تُستبعد."""
    with_spike = np.append(SWING_HIGHS, 100.0)
    indices = [index for index, _price in _swing_points(with_spike, window=2, find_high=True)]

    assert 20 not in indices  # رأس الشمعة الأخيرة لم يُصنَّف
    assert indices == [2, 7, 12, 17]  # القمم السابقة لم تتغيّر
    # كل نقطة مؤكدة يجب أن تملك نافذة تأكيد كاملة قبل نهاية السلسلة
    assert all(index + 2 <= len(with_spike) - 1 for index in indices)


# ---------------------------------------------------------------------------
# 11) منع النظر إلى المستقبل (No look-ahead)
# ---------------------------------------------------------------------------
def test_no_look_ahead_earlier_swings_unchanged_by_future_bar():
    """إضافة شمعة مستقبلية لا يجوز أن تغيّر تصنيف أي نقطة سابقة."""
    extended = np.append(SWING_HIGHS, 100.0)
    base_highs = set(_swing_points(SWING_HIGHS, window=2, find_high=True))
    extended_highs = set(_swing_points(extended, window=2, find_high=True))
    assert base_highs.issubset(extended_highs)

    base_lows = set(_swing_points(30.0 - SWING_HIGHS, window=2, find_high=False))
    extended_lows = set(_swing_points(30.0 - extended, window=2, find_high=False))
    assert base_lows.issubset(extended_lows)


def test_no_look_ahead_public_zones_identical_on_overlap():
    """نفس المنطقة (بنفس formed_index) تبقى بنفس الحدود بعد إضافة شمعة لاحقة."""
    extended = np.append(SWING_HIGHS, 100.0)
    base = analyze_market_structure(
        _frame(SWING_HIGHS), atr_value=1.0, merge_tolerance_atr=0.05, min_reactions=0, analyzed_at="T"
    )
    with_future = analyze_market_structure(
        _frame(extended), atr_value=1.0, merge_tolerance_atr=0.05, min_reactions=0, analyzed_at="T"
    )
    future_by_index = {zone["formed_index"]: zone for zone in with_future.resistance_zones}
    for zone in base.resistance_zones:
        counterpart = future_by_index.get(zone["formed_index"])
        assert counterpart is not None
        assert (counterpart["low"], counterpart["high"]) == (zone["low"], zone["high"])


# ---------------------------------------------------------------------------
# 3 و4) تجميع المناطق المتقاربة وفصل المناطق المتباعدة
# ---------------------------------------------------------------------------
# قمم انعطاف: 13@2، 13.3@7، 20@12، 13@17
CLUSTER_HIGHS = np.array(
    [10, 11, 13, 11, 10, 11, 12, 13.3, 12, 11, 12, 13, 20, 13, 12, 11, 12, 13, 12, 11], dtype=float
)


def test_nearby_levels_merge_into_single_zone():
    result = analyze_market_structure(
        _frame(CLUSTER_HIGHS), atr_value=1.0, merge_tolerance_atr=0.5, min_reactions=0, analyzed_at="T"
    )
    assert result.valid
    assert len(result.resistance_zones) == 2  # عنقود ~13 وعنقود 20
    merged = [zone for zone in result.resistance_zones if zone["low"] <= 13.0 and zone["high"] >= 13.3]
    assert len(merged) == 1, "المستويات المتقاربة لم تُدمج في منطقة واحدة"


def test_distinct_levels_stay_separate():
    result = analyze_market_structure(
        _frame(CLUSTER_HIGHS), atr_value=1.0, merge_tolerance_atr=0.05, min_reactions=0, analyzed_at="T"
    )
    assert len(result.resistance_zones) == 3  # 13، 13.3، 20 منفصلة تماماً


# ---------------------------------------------------------------------------
# 5) عدّ التفاعلات وحساب القوة
# ---------------------------------------------------------------------------
def test_reaction_counts_and_strength_scoring():
    config = _resolve_config(2, 0.5, 0, 5, 50, 6)

    # منطقة دعم مرجعية عند ~100 (تُوسَّع تلقائياً إلى شريط بعرض 0.25 حول 100)
    cluster = {"min": 100.0, "max": 100.0, "members": [(4, 100.0)]}

    # ثلاث لمسات ناجحة (قاع يدخل المنطقة والإغلاق يعود فوقها)
    strong_close = np.array(
        [106, 105, 104, 103, 101, 105, 104, 103, 101, 105, 104, 103, 101, 105, 106, 105, 106, 105, 106, 105],
        dtype=float,
    )
    strong_low = np.array(
        [105, 104, 103, 102, 99, 104, 103, 102, 99, 104, 103, 102, 99, 104, 105, 104, 105, 104, 105, 104],
        dtype=float,
    )
    strong_high = strong_close + 1.0

    strong = _zone_from_cluster(cluster, "support", strong_high, strong_low, strong_close, 1.0, config)
    assert strong["reactions"] == 3
    assert strong["touches"] == 3
    assert strong["last_reaction_bars_ago"] == 7  # آخر تفاعل عند 12، والسلسلة 20 شمعة
    # قوة = 100 × (0.5×3/5 + 0.3×(1−7/50) + 0.2×3/5) = 67.8 (حساب يدوي صريح)
    assert strong["strength"] == pytest.approx(67.8, abs=0.01)

    # لمسة واحدة فقط (لا شيء يدخل المنطقة إلا عند الفهرس 4) ⇒ قوة أقل حتماً
    weak_close = strong_close.copy()
    weak_low = strong_close.copy()  # بلا أي غوص داخل المنطقة إلا ما نضبطه صراحةً
    weak_low[4] = 99.0
    weak_high = weak_close + 1.0
    weak = _zone_from_cluster(cluster, "support", weak_high, weak_low, weak_close, 1.0, config)

    assert weak["reactions"] == 1
    assert weak["touches"] == 1
    assert strong["strength"] > weak["strength"]


def test_min_reactions_filter_applies():
    # نفس بيانات القمة المزدوجة، لكن نطلب تفاعلات أكثر من المتاح ⇒ لا مناطق
    result = analyze_market_structure(
        _frame(CLUSTER_HIGHS), atr_value=1.0, merge_tolerance_atr=0.5, min_reactions=999, analyzed_at="T"
    )
    assert result.valid
    assert result.resistance_zones == []


# ---------------------------------------------------------------------------
# 6) تصنيف المنطقة المكسورة
# ---------------------------------------------------------------------------
def test_broken_zone_classification():
    # قاع انعطاف عند ~99.5 ثم إغلاقات تنزل تحته بوضوح في النهاية
    closes = np.array(
        [105, 104, 103, 102, 101, 100, 101, 102, 103, 104, 105, 104, 103, 102, 101, 100, 99, 98, 97, 96],
        dtype=float,
    )
    result = analyze_market_structure(
        _frame(closes + 0.5, lows=closes - 0.5, closes=closes),
        atr_value=1.0,
        merge_tolerance_atr=0.5,
        min_reactions=0,
        analyzed_at="T",
    )
    assert result.valid
    assert len(result.support_zones) >= 1
    assert result.support_zones[0]["status"] == "broken"


def test_active_zone_stays_active_when_price_holds():
    result = analyze_market_structure(
        _frame(CLUSTER_HIGHS), atr_value=1.0, merge_tolerance_atr=0.05, min_reactions=0, analyzed_at="T"
    )
    assert any(zone["status"] == "active" for zone in result.resistance_zones)


# ---------------------------------------------------------------------------
# 7) مناطق السيولة المحتملة (منطق منفصل)
# ---------------------------------------------------------------------------
# أربع قمم انعطاف متطابقة عند 13 منفصلة بمسافات كافية (لا هضبة)
EQUAL_HIGHS = np.array([10, 11, 13, 11, 10, 11, 12, 13, 12, 11, 11, 12, 13, 12, 11, 11, 12, 13, 12, 11], dtype=float)


def test_liquidity_equal_highs_cluster():
    result = analyze_market_structure(
        _frame(EQUAL_HIGHS), atr_value=1.0, merge_tolerance_atr=0.5, min_reactions=0, analyzed_at="T"
    )
    equal_highs = [area for area in result.liquidity_levels if area["kind"] == "equal_highs"]
    assert len(equal_highs) == 1
    assert equal_highs[0]["count"] == 4
    assert equal_highs[0]["price"] == pytest.approx(13.0, abs=0.01)
    # ومرجع آخر قمة سابقة موجود أيضاً
    assert any(area["kind"] == "prior_swing_high" for area in result.liquidity_levels)


def test_liquidity_levels_are_bounded_and_labelled_as_potential():
    result = analyze_market_structure(
        _frame(EQUAL_HIGHS), atr_value=1.0, min_reactions=0, max_liquidity_levels=3, analyzed_at="T"
    )
    assert len(result.liquidity_levels) <= 3
    # كل المراجع توصَف كـ potential / price reference — لا ادّعاء بسيولة فعلية
    for area in result.liquidity_levels:
        assert "potential" in area["note"] or "price reference" in area["note"]


# ---------------------------------------------------------------------------
# 8) بيانات غير كافية أو فارغة
# ---------------------------------------------------------------------------
def test_empty_candles_returns_invalid():
    result = analyze_market_structure(pd.DataFrame(), analyzed_at="T")
    assert isinstance(result, MarketStructure)
    assert result.valid is False
    assert result.reason == "empty_candles"
    assert result.support_zones == [] and result.resistance_zones == []


def test_insufficient_candles_returns_invalid():
    result = analyze_market_structure(_frame([10, 11, 12]), analyzed_at="T")
    assert result.valid is False
    assert result.reason.startswith("insufficient_data")


def test_missing_columns_returns_invalid():
    result = analyze_market_structure(pd.DataFrame({"high": [1.0, 2.0, 3.0]}), analyzed_at="T")
    assert result.valid is False
    assert result.reason.startswith("missing_columns")


def test_none_candles_returns_invalid():
    result = analyze_market_structure(None, analyzed_at="T")
    assert result.valid is False
    assert result.reason == "missing_candles"


# ---------------------------------------------------------------------------
# 9) قيم غير صالحة وغير منتهية
# ---------------------------------------------------------------------------
def test_non_finite_values_are_rejected():
    frame = _frame(SWING_HIGHS)
    frame.loc[3, "high"] = np.nan
    result = analyze_market_structure(frame, analyzed_at="T")
    assert result.valid is False
    assert result.reason == "non_finite_values:high"


def test_infinite_values_are_rejected():
    frame = _frame(SWING_HIGHS)
    frame.loc[4, "close"] = np.inf
    result = analyze_market_structure(frame, analyzed_at="T")
    assert result.valid is False
    assert result.reason == "non_finite_values:close"


def test_inconsistent_ohlc_is_rejected():
    frame = _frame(SWING_HIGHS)
    frame.loc[2, "low"] = frame.loc[2, "high"] + 5.0  # low > high
    result = analyze_market_structure(frame, analyzed_at="T")
    assert result.valid is False
    assert result.reason.startswith("inconsistent_ohlc")


# ---------------------------------------------------------------------------
# 10) ترتيب الطوابع الزمنية
# ---------------------------------------------------------------------------
def test_out_of_order_timestamps_are_rejected():
    frame = _frame(SWING_HIGHS)
    reversed_ts = frame["timestamp"].iloc[::-1].reset_index(drop=True)
    frame["timestamp"] = reversed_ts
    result = analyze_market_structure(frame, analyzed_at="T")
    assert result.valid is False
    assert result.reason == "out_of_order_timestamps"


def test_sorted_timestamps_are_accepted():
    result = analyze_market_structure(_frame(SWING_HIGHS), atr_value=1.0, min_reactions=0, analyzed_at="T")
    assert result.valid is True
    assert result.reason == "ok"


# ---------------------------------------------------------------------------
# 12) حتمية النتيجة
# ---------------------------------------------------------------------------
def test_deterministic_output_for_identical_input():
    kwargs = dict(atr_value=1.0, merge_tolerance_atr=0.5, min_reactions=0, analyzed_at="FIXED")
    first = analyze_market_structure(_frame(CLUSTER_HIGHS), **kwargs).to_dict()
    second = analyze_market_structure(_frame(CLUSTER_HIGHS), **kwargs).to_dict()
    assert first == second


# ---------------------------------------------------------------------------
# 13) سقف حجم الحمولة (Bounded payload)
# ---------------------------------------------------------------------------
def test_payload_is_bounded_by_configuration():
    result = analyze_market_structure(
        _frame(CLUSTER_HIGHS),
        atr_value=1.0,
        merge_tolerance_atr=0.05,
        min_reactions=0,
        max_zones=1,
        max_liquidity_levels=2,
        analyzed_at="T",
    )
    assert len(result.resistance_zones) <= 1
    assert len(result.support_zones) <= 1
    assert len(result.liquidity_levels) <= 2
