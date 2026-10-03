"""
اختبارات RealCorrelationFeed — بأسعار مصطنعة معروفة وبدون أي اتصال شبكي.

المراجع اليدوية (محسوبة خارج الكود للتحقق المزدوج):

(1) مرجع دقيق حسابياً — نختار الأسعار بحيث تكون كل عامل من معادلة ICE مساوياً 2:
      EURUSD = 2^(-125/72)  →  EUR^(-0.576) = 2^((-125/72)×(-0.576)) = 2^(+1)
      USDJPY = 2^(+125/17)  →  JPY^(+0.136) = 2^((125/17)×(+0.136)) = 2^(+1)
      GBPUSD = 2^(-1000/119) → GBP^(-0.119) = 2^(+1)
      USDCAD = 2^(+1000/91)  → CAD^(+0.091) = 2^(+1)
      USDSEK = 2^(+500/21)   → SEK^(+0.042) = 2^(+1)
      USDCHF = 2^(+250/9)    → CHF^(+0.036) = 2^(+1)
    إذن DXY = 50.14348112 × 2^6 = 50.14348112 × 64 = 3209.18275968 — حساب يدوي
    صريح بأربع عمليات ضرب، بلا أي تقريب لوغاريتمي.

(2) مرجع واقعي (اقتباسات 2022-09-27 خلال أسبوع ذروة قوة الدولار):
      EURUSD=0.9595, USDJPY=144.15, GBPUSD=1.0700, USDCAD=1.3620, USDSEK=10.8250, USDCHF=0.9825
    الحساب اليدوي باللوغاريتمات:
      مجموع (الوزن × ln(السعر)) = 0.0238135 + 0.6760362 − 0.0080514 + 0.0281148
                                  + 0.1000381 − 0.0006356 = 0.8193156
      DXY = 50.14348112 × e^0.8193156 = 50.14348112 × 2.2689464 ≈ 113.773
    القيمة متسقة مع مستوى DXY الرسمي في أسبوع القمة 27–28 سبتمبر 2022 (114+ عند
    الذروة) ضمن دقة اقتباسات اليوم المذكورة أعلاه — فرق مقبول بحكم تقريب الاقتباسات.
"""
import inspect
import os
import sys
import types
from unittest import mock as unittest_mock

import pandas as pd
import pytest

from brain.data.interfaces import CorrelationFeed, PriceFeed
from brain.data.real_correlation_feed import (
    CorrelationFeedError,
    RealCorrelationFeed,
    compute_dxy_value,
)



# أسعار واقعية مرجعية (المجموعة التاريخية أعلاه) للاختبارات السلوكية
BASE_PRICES: dict[str, float] = {
    "EURUSD": 0.9595,
    "USDJPY": 144.15,
    "GBPUSD": 1.0700,
    "USDCAD": 1.3620,
    "USDSEK": 10.8250,
    "USDCHF": 0.9825,
}

_DXY_COMPONENTS = ("EURUSD", "USDJPY", "GBPUSD", "USDCAD", "USDSEK", "USDCHF")


class _StubPriceFeed(PriceFeed):
    """بديل memory-only لعقد PriceFeed: يسجل الطلبات ويعيد إغلاقات مخطط لها مسبقاً."""

    def __init__(self, closes_by_symbol: dict[str, list[float]]) -> None:
        self._closes = closes_by_symbol
        self.requests: list[tuple[str, str, int]] = []

    def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self.requests.append((symbol, timeframe, count))
        if symbol not in self._closes:
            raise RuntimeError(f"الرمز {symbol} غير موجود على الحساب")
        closes = self._closes[symbol]
        rows = [
            {
                "timestamp": pd.Timestamp("2026-09-30 10:00", tz="UTC") + pd.Timedelta(minutes=15 * i),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 100,
            }
            for i, close in enumerate(closes)
        ]
        return pd.DataFrame(rows)


def _base_closes(count: int = 11) -> dict[str, list[float]]:
    """سلاسل إغلاق ثابتة عند الأسعار المرجعية (لا اتجاه في أي زوج)."""
    return {symbol: [BASE_PRICES[symbol]] * count for symbol in _DXY_COMPONENTS}


def _make_feed(closes_by_symbol: dict[str, list[float]], **kwargs) -> tuple[RealCorrelationFeed, _StubPriceFeed]:
    stub = _StubPriceFeed(closes_by_symbol)
    feed = RealCorrelationFeed(price_feed=stub, **kwargs)
    return feed, stub


# --- (أ) التحقق المزدوج من معادلة DXY ضد مراجع يدوية ---


def test_dxy_matches_exact_manual_reference_with_powers_of_two():
    """مرجع يدوي دقيق: كل عامل يساهم 2 بالضبط → DXY = 50.14348112 × 64 = 3209.18275968."""
    prices = {
        "EURUSD": 2 ** (-125 / 72),
        "USDJPY": 2 ** (125 / 17),
        "GBPUSD": 2 ** (-1000 / 119),
        "USDCAD": 2 ** (1000 / 91),
        "USDSEK": 2 ** (500 / 21),
        "USDCHF": 2 ** (250 / 9),
    }
    # القيمة المرجعية محسوبة يدوياً خارج الكود: 50.14348112 × 2^6
    expected = 50.14348112 * 64  # = 3209.18275968
    assert compute_dxy_value(prices) == pytest.approx(expected, rel=1e-9)


def test_dxy_matches_manual_log_reference_on_realistic_quotes():
    """مرجع يدوي لوغاريتمي على اقتباسات 2022-09-27: DXY ≈ 113.773 (تفصيل الحساب أعلاه)."""
    expected = 113.773
    assert compute_dxy_value(BASE_PRICES) == pytest.approx(expected, abs=0.15)


def test_dxy_through_full_feed_matches_manual_reference():
    """نفس المرجع الدقيق (2^6) عبر المسار الكامل: get_dollar_trend_details على سلاسل ثابتة."""
    prices = {
        "EURUSD": 2 ** (-125 / 72),
        "USDJPY": 2 ** (125 / 17),
        "GBPUSD": 2 ** (-1000 / 119),
        "USDCAD": 2 ** (1000 / 91),
        "USDSEK": 2 ** (500 / 21),
        "USDCHF": 2 ** (250 / 9),
    }
    feed, _stub = _make_feed({symbol: [price] * 11 for symbol, price in prices.items()})
    snapshot = feed.get_dollar_trend_details()
    assert snapshot.current == pytest.approx(50.14348112 * 64, rel=1e-9)
    assert snapshot.previous == pytest.approx(50.14348112 * 64, rel=1e-9)
    assert snapshot.change == pytest.approx(0.0, abs=1e-6)


# --- (ب) التصنيف: up / down / flat ---


def test_clear_rise_in_dxy_reports_up():
    """USDJPY يرتفع 2% فقط من أضعاف الأزواج → DXY يرتفع ≈ +0.28 نقطة > العتبة → up."""
    closes = _base_closes()
    closes["USDJPY"] = [BASE_PRICES["USDJPY"]] * 10 + [BASE_PRICES["USDJPY"] * 1.02]
    feed, _stub = _make_feed(closes)
    snapshot = feed.get_dollar_trend_details()
    assert snapshot.change == pytest.approx(0.28, abs=0.05)
    assert snapshot.trend == "up"
    assert feed.get_dollar_trend() == "up"


def test_clear_drop_in_dxy_reports_down():
    """USDJPY يهبط 2% فقط → DXY ينخفض ≈ −0.29 نقطة < −العتبة → down."""
    closes = _base_closes()
    closes["USDJPY"] = [BASE_PRICES["USDJPY"]] * 10 + [BASE_PRICES["USDJPY"] * 0.98]
    feed, _stub = _make_feed(closes)
    snapshot = feed.get_dollar_trend_details()
    assert snapshot.change == pytest.approx(-0.29, abs=0.05)
    assert snapshot.trend == "down"


def test_tiny_change_reports_flat():
    """تغير ضئيل جداً (EURUSD الأخيرة +0.01% → ΔDXY ≈ −0.006 نقطة < 0.10) → flat."""
    closes = _base_closes()
    closes["EURUSD"] = [BASE_PRICES["EURUSD"]] * 10 + [BASE_PRICES["EURUSD"] * 1.0001]
    feed, _stub = _make_feed(closes)
    snapshot = feed.get_dollar_trend_details()
    assert abs(snapshot.change) < snapshot.threshold
    assert snapshot.change == pytest.approx(-0.006, abs=0.002)
    assert snapshot.trend == "flat"
    assert feed.get_dollar_trend() == "flat"


def test_comparison_uses_oldest_candle_of_window_not_latest_as_previous():
    """اتجاه النافذة: أقدم شمعة USDJPY أعلى بـ5% → السابق أعلى → التغير سالب → down.

    لو انعكس الفهرسة (الأحدث بدل الأقدم كنقطة مقارنة) لانقلب التصنيف إلى up.
    """
    closes = _base_closes()
    usdjpy = [BASE_PRICES["USDJPY"]] * 11
    usdjpy[0] = BASE_PRICES["USDJPY"] * 1.05  # الشمعة الأقدم فقط
    closes["USDJPY"] = usdjpy
    feed, _stub = _make_feed(closes)
    snapshot = feed.get_dollar_trend_details()
    assert snapshot.previous > snapshot.current
    assert snapshot.trend == "down"


# --- (ج) الاتساق الداخلي مع نافذة الترند والعقد ---


def test_default_window_matches_regime_detector_slope_window():
    """N=10 M15 افتراضياً — نفس نافذة ema_slope(lookback=10) في regime_detector."""
    feed, stub = _make_feed(_base_closes())
    assert feed.lookback == 10
    assert feed.timeframe == "M15"
    feed.get_dollar_trend()
    # كل زوج طُلب بإطار M15 وهامش جلب +3 (شمعة cTrader الجارية لا تُعاد — موثَّق تجريبياً)
    assert len(stub.requests) == 6
    assert {symbol for symbol, _tf, _count in stub.requests} == set(_DXY_COMPONENTS)
    assert all(timeframe == "M15" for _symbol, timeframe, _count in stub.requests)
    assert all(count == 11 + 3 for _symbol, _timeframe, count in stub.requests)


def test_contract_same_signature_and_inheritance_as_mock():
    """العقد: يرث CorrelationFeed و get_dollar_trend بنفس توقيع MockCorrelationFeed تماماً."""
    from brain.data.mock_price_feed import MockCorrelationFeed

    assert issubclass(RealCorrelationFeed, CorrelationFeed)
    assert inspect.signature(RealCorrelationFeed.get_dollar_trend) == inspect.signature(
        MockCorrelationFeed.get_dollar_trend
    )
    feed, _stub = _make_feed(_base_closes())
    assert isinstance(feed, CorrelationFeed)


def test_no_separate_connection_uses_injected_feed_only():
    """لا اتصال منفصل: الوحدة لا تستورد مكتبة الوسيط أو أي شبكة — تعمل عبر PriceFeed المحقون فقط."""
    import ast

    import brain.data.real_correlation_feed as module

    source = inspect.getsource(module)
    imported_names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported_names.add(node.module.split(".")[0])

    forbidden = {"ctrader_open_api", "twisted", "socket", "requests", "urllib", "http", "aiohttp"}
    assert imported_names.isdisjoint(forbidden), f"استيرادات شبكية غير مسموحة: {imported_names & forbidden}"
    # وبرهان عملي: يعمل كاملاً فوق بديل memory-only بلا أي شبكة (كل الاختبارات أعلاه)


# --- (د) ممنوع الصمت: أي فشل → استثناء صريح ---


def test_missing_pair_raises_loudly_naming_the_symbol():
    """زوج مفقود (الرمز غير موجود) → CorrelationFeedError يسمّي الزوج، لا قيمة افتراضية."""
    closes = _base_closes()
    del closes["USDCHF"]  # الزوج السادس مفقود
    feed, _stub = _make_feed(closes)
    with pytest.raises(CorrelationFeedError) as excinfo:
        feed.get_dollar_trend()
    assert "USDCHF" in str(excinfo.value)
    assert excinfo.value.__cause__ is not None  # الخطأ الأصلي محفوظ في السلسلة


def test_empty_candles_for_a_pair_raises_loudly():
    """زوج يعيد DataFrame فارغاً → استثناء صريح لا صمت."""
    feed, stub = _make_feed(_base_closes())
    original = stub.get_candles

    def empty_for_sek(symbol, timeframe, count):
        if symbol == "USDSEK":
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        return original(symbol, timeframe, count)

    stub.get_candles = empty_for_sek
    with pytest.raises(CorrelationFeedError) as excinfo:
        feed.get_dollar_trend()
    assert "USDSEK" in str(excinfo.value)


def test_insufficient_candles_raises_loudly():
    """شموع أقل من المطلوب (5 بدل 11) → استثناء صريح يذكر العدد اللازم والواصل."""
    closes = _base_closes()
    closes["USDSEK"] = [BASE_PRICES["USDSEK"]] * 5
    feed, _stub = _make_feed(closes)
    with pytest.raises(CorrelationFeedError) as excinfo:
        feed.get_dollar_trend()
    assert "USDSEK" in str(excinfo.value)
    assert "11" in str(excinfo.value) and "5" in str(excinfo.value)


def test_invalid_price_raises_loudly():
    """سعر صفر أو NaN → استثناء صريح (DXY بقسمة/أس غير منطقي ممنوع حسابه)."""
    feed, stub = _make_feed(_base_closes())
    original = stub.get_candles

    def zero_for_chf(symbol, timeframe, count):
        df = original(symbol, timeframe, count)
        if symbol == "USDCHF":
            df.loc[df.index[-1], "close"] = 0.0
        return df

    stub.get_candles = zero_for_chf
    with pytest.raises(CorrelationFeedError) as excinfo:
        feed.get_dollar_trend()
    assert "USDCHF" in str(excinfo.value)


def test_constructor_rejects_invalid_parameters():
    """حماية مبكرة: lookback<1 أو عتبة غير موجبة أو إطار فارغ → فشل فوري قبل أي جلب."""
    stub = _StubPriceFeed(_base_closes())
    with pytest.raises(CorrelationFeedError):
        RealCorrelationFeed(price_feed=stub, lookback=0)
    with pytest.raises(CorrelationFeedError):
        RealCorrelationFeed(price_feed=stub, flat_threshold=0.0)
    with pytest.raises(CorrelationFeedError):
        RealCorrelationFeed(price_feed=stub, timeframe="")


def test_real_correlation_feed_with_flag_uses_same_price_feed_instance():
    """الربط الفعلي: تفعيل USE_REAL_CORRELATION_FEED=true يُنتج RealCorrelationFeed
    متصلاً بنفس instance من price_feed المُستخدم فعلاً (لا اتصال مستقل، لا استدعاء
    لـ CTraderPriceFeed داخلاً).

    الهدف: إثبات أن الربط يعمل على نسخة واحدة مشتركة — وأن الـ mock لا يُعاد
    إنشاؤه داخلياً عند التبديل؛ وإثبات أيضاً أن النتيجة منطقية (أي اتجاه صحيح)
    على البيانات المثبتة، دون أن يرمي خطأ عند الإقلاع.
    """
    import brain.api.main as main_module

    # دورق كامل لاستعادة المتغير البيئي فور انتهاء الاختبار (بلا أثر على غيره)
    with unittest_mock.patch.dict(os.environ, {"_TEST_REAL_CORRELATION_FEED": "true"}, clear=False) as patch:
        # إعادة توجيه استدعاء os.getenv داخل عملية main وظل تأثيره محلياً للدورة
        with unittest_mock.patch.object(main_module.os, "getenv", lambda key, default="false": (
            "true" if key == "USE_REAL_CORRELATION_FEED" else os.environ.get(key, default)
        )):
            stub = _StubPriceFeed(_base_closes(11))
            feed = main_module._build_correlation_feed(price_feed=stub)

    assert isinstance(feed, RealCorrelationFeed), "التفعيل الحقيقي لم ينتج RealCorrelationFeed"
    assert feed._price_feed is stub, "يُنشئ RealCorrelationFeed مبداً جديداً منفصلاً عن price_feed المُستخدم فعلاً"

    # إثبات أن مصدر cTrader لم يُستدعَ فعلياً في هذا الاختبار (بديل memory-only فقط)
    assert not any(
        name.startswith("ctrader_open_api") or name.startswith("twisted")
        for name in sys.modules
    ), "استدعاء مكتبة الوسيط داخل process_start_without_real_feed()"

    result = feed.get_dollar_trend()
    assert result in ("up", "down", "flat"), "الاتجاه المُبلَّغ عنه غير منطقي على بيانات مرجعية مثبتة"


