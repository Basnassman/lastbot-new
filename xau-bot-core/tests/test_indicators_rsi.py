"""اختبارات مستقلة لدالة RSI ولتعامل weighted_voting مع غياب RSI.

القيم المرجعية محسوبة بحلقة EWM صريحة داخل هذا الملف (لا تستدعي rsi)،
فتصلح لكشف أي تغيير في طريقة الحساب أو التهيئة.
"""
import math

import numpy as np
import pandas as pd
import pytest

from brain.analysis.indicators import rsi
from brain.analysis.regime_detector import RegimeResult
from brain.scoring.weighted_voting import compute_score
from config.settings import settings

# سلسلة Wilder الكلاسيكية (15 سعراً = 14 تغيراً، أي تاريخ كافٍ تماماً).
CLASSIC = [
    44.34, 44.09, 44.15, 43.61, 44.33,
    44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28,
]

# سلسلة ثابتة البذرة (25 سعراً) لاختبار الانحدار.
SEED7 = [
    100.00123015335748, 100.29997569086595, 100.02583783550374,
    99.13524599674646, 98.68057521157473, 97.68892865657827,
    97.74907225917572, 99.08928750473025, 98.59708098617892,
    97.97660608635898, 98.46644813654417, 98.82333514470423,
    98.92874939370213, 97.99828134899393, 97.96902952653066,
    98.66433272098895, 97.32011817370386, 96.86250241266364,
    94.9612796728628, 93.67174193307783, 91.83000689528609,
    91.59491576421141, 90.32746928276771, 90.5987336415894,
    90.75548472821363,
]

# نواتج مرجعية لـ SEED7 بالفترة 14 (محسوبة بحلقة EWM مستقلة).
SEED7_RSI_14 = [
    52.469787677650, 57.714253743223, 46.932603854408, 43.924284729594,
    34.134738798847, 29.355742824722, 24.154413478697, 23.580084906213,
    20.719694330404, 22.876033734834, 24.159706467007,
]


def _ewm_reference(values, period):
    """EWM بلا بذرة SMA، تماماً كالصيغة الحالية: alpha=1/period, adjust=False."""
    alpha = 1.0 / period
    out = np.full(len(values), np.nan, dtype=float)
    previous = None
    for i, value in enumerate(values):
        if not np.isfinite(value):
            continue
        previous = value if previous is None else (1 - alpha) * previous + alpha * value
        out[i] = previous
    return out


def _rsi_reference(close, period=14):
    """تنفيذ مرجعي مستقل لقواعد RSI (EWM + معالجة المقام الصفري)."""
    close = np.asarray(close, dtype=float)
    deltas = np.diff(close, prepend=np.nan)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = _ewm_reference(gains, period)
    avg_loss = _ewm_reference(losses, period)
    out = np.full(len(close), np.nan, dtype=float)
    for i in range(len(close)):
        # أول قيمة صالحة عند index=period (period تغيّراً مكتملاً)، كما في التنفيذ.
        if i < period:
            continue
        if not (np.isfinite(avg_gain[i]) and np.isfinite(avg_loss[i])):
            continue
        if avg_loss[i] == 0 and avg_gain[i] > 0:
            out[i] = 100.0
        elif avg_gain[i] == 0 and avg_loss[i] > 0:
            out[i] = 0.0
        elif avg_gain[i] == 0 and avg_loss[i] == 0:
            out[i] = 50.0
        else:
            out[i] = 100 - 100 / (1 + avg_gain[i] / avg_loss[i])
    return out


def _candles(closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": 100.0,
        }
    )


# --- سلوك RSI الأساسي -------------------------------------------------------


def test_monotonically_increasing_prices_produce_100():
    result = rsi(pd.Series(np.arange(2400.0, 2420.0)))
    assert result.iloc[:14].isna().all()
    assert (result.iloc[14:] == 100.0).all()


def test_monotonically_decreasing_prices_produce_0():
    result = rsi(pd.Series(np.arange(2420.0, 2400.0, -1.0)))
    assert result.iloc[:14].isna().all()
    assert (result.iloc[14:] == 0.0).all()


def test_completely_flat_prices_produce_50():
    result = rsi(pd.Series([100.0] * 20))
    assert result.iloc[:14].isna().all()
    assert (result.iloc[14:] == 50.0).all()


@pytest.mark.parametrize("n", [0, 1, 5, 14])
def test_insufficient_history_returns_nan_only(n):
    result = rsi(pd.Series(np.linspace(2400.0, 2413.0, n)))
    assert result.isna().all()


def test_exactly_sufficient_history_produces_first_value():
    result = rsi(pd.Series(CLASSIC))
    assert result.iloc[:14].isna().all()
    assert result.iloc[14] == pytest.approx(50.65741494172488, abs=1e-12)
    assert len(result) == 15


# --- المدخلات غير الصالحة ---------------------------------------------------


@pytest.mark.parametrize("position", [0, 7, 14, -1])
def test_nan_input_is_rejected(position):
    close = list(SEED7)
    close[position] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        rsi(pd.Series(close))


@pytest.mark.parametrize("bad", [float("inf"), float("-inf")])
@pytest.mark.parametrize("position", [0, 12, -1])
def test_infinite_input_is_rejected(bad, position):
    close = list(SEED7)
    close[position] = bad
    with pytest.raises(ValueError, match="non-finite"):
        rsi(pd.Series(close))


@pytest.mark.parametrize("period", [True, False, 14.0, np.float64(14.0), "14", None])
def test_non_integer_period_is_rejected(period):
    with pytest.raises(TypeError, match="period must be an integer"):
        rsi(pd.Series(SEED7), period)


@pytest.mark.parametrize("period", [0, -1, -14])
def test_non_positive_period_is_rejected(period):
    with pytest.raises(ValueError, match="period must be >= 1"):
        rsi(pd.Series(SEED7), period)


def test_numpy_integer_period_is_accepted():
    expected = rsi(pd.Series(SEED7), 14)
    actual = rsi(pd.Series(SEED7), np.int64(14))
    pd.testing.assert_series_equal(actual, expected)


# --- قيم مرجعية وحماية من الانحدار -----------------------------------------


def test_valid_ewm_reference_outputs_are_matched():
    result = rsi(pd.Series(SEED7))
    assert result.iloc[:14].isna().all()
    for index, expected in zip(range(14, 25), SEED7_RSI_14):
        assert result.iloc[index] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("period", [2, 5, 7, 14, 21])
def test_existing_valid_ewm_outputs_unchanged_against_independent_loop(period):
    """لو تغيّرت طريقة EWM أو تهيئتها (مثل بذرة Wilder) سيفشل هذا الاختبار."""
    expected = _rsi_reference(SEED7, period)
    actual = rsi(pd.Series(SEED7), period).to_numpy()
    valid = np.isfinite(expected)
    assert valid.any()
    np.testing.assert_allclose(actual[valid], expected[valid], rtol=0, atol=1e-12)
    assert np.isnan(actual[~valid]).all()


def test_default_period_is_still_14():
    pd.testing.assert_series_equal(rsi(pd.Series(SEED7)), rsi(pd.Series(SEED7), 14))


# --- weighted_voting: RSI غير متاح -----------------------------------------


def _expected_direction(score):
    if score >= settings.decision_score_threshold:
        return "buy"
    if score <= -settings.decision_score_threshold:
        return "sell"
    return "none"


def test_short_history_excludes_momentum_without_fabricating_rsi():
    result = compute_score(
        _candles([100.0, 101.0, 102.0, 103.0, 104.0]),
        RegimeResult(regime="trend_up", slope_value=10.0),
        dollar_trend="down",
    )
    assert result.momentum_score == 0.0
    assert "غير متاح" in result.reasons[1]
    assert "تاريخ غير كافٍ" in result.reasons[1]


def test_other_components_and_weights_are_preserved_when_rsi_unavailable():
    result = compute_score(
        _candles([100.0, 101.0, 102.0, 103.0, 104.0]),
        RegimeResult(regime="trend_up", slope_value=10.0),
        dollar_trend="down",
    )
    assert result.trend_score == 100.0
    assert result.dollar_score == 70.0
    # (100*45 + 0*30 + 70*25)/100 — الأوزان الأصلية دون تغيير.
    assert result.final_score == pytest.approx(62.5, abs=1e-12)
    assert result.confidence == pytest.approx(62.5, abs=1e-12)
    assert result.direction == _expected_direction(62.5)


def test_non_finite_price_excludes_momentum_without_crashing():
    close = list(SEED7)
    close[10] = float("nan")
    result = compute_score(
        _candles(close),
        RegimeResult(regime="range", slope_value=0.0),
        dollar_trend="flat",
    )
    assert result.momentum_score == 0.0
    assert "بيانات غير صالحة" in result.reasons[1]
    assert result.trend_score == 0.0
    assert result.dollar_score == 0.0
    assert result.final_score == 0.0


def test_missing_or_empty_close_excludes_momentum_without_crashing():
    empty = compute_score(
        pd.DataFrame({"close": []}),
        RegimeResult(regime="range", slope_value=0.0),
        dollar_trend="flat",
    )
    assert empty.momentum_score == 0.0
    assert "لا توجد أسعار إغلاق" in empty.reasons[1]

    no_close = compute_score(
        pd.DataFrame({"open": [100.0, 101.0]}),
        RegimeResult(regime="range", slope_value=0.0),
        dollar_trend="flat",
    )
    assert no_close.momentum_score == 0.0
    assert "لا توجد أسعار إغلاق" in no_close.reasons[1]


# --- weighted_voting: RSI صالح ---------------------------------------------


def test_valid_uptrend_rsi_still_produces_expected_momentum():
    result = compute_score(
        _candles(np.linspace(2600.0, 2680.0, 60)),
        RegimeResult(regime="trend_up", slope_value=10.0),
        dollar_trend="down",
    )
    assert result.momentum_score == -60.0
    assert "متشبع شراء" in result.reasons[1]
    # (100*45 + (-60)*30 + 70*25)/100
    assert result.final_score == pytest.approx(44.5, abs=1e-12)
    assert result.direction == _expected_direction(44.5)


def test_valid_downtrend_rsi_still_produces_expected_momentum():
    result = compute_score(
        _candles(np.linspace(2680.0, 2600.0, 60)),
        RegimeResult(regime="trend_down", slope_value=10.0),
        dollar_trend="up",
    )
    assert result.momentum_score == 60.0
    assert "تشبع بيع" in result.reasons[1]
    # (-100*45 + 60*30 - 70*25)/100
    assert result.final_score == pytest.approx(-44.5, abs=1e-12)


def test_valid_neutral_rsi_uses_linear_momentum():
    result = compute_score(
        _candles(CLASSIC),
        RegimeResult(regime="range", slope_value=0.0),
        dollar_trend="flat",
    )
    expected_rsi = 50.65741494172488
    expected_momentum = (expected_rsi - 50.0) * 1.2
    assert result.momentum_score == pytest.approx(expected_momentum, abs=1e-9)
    assert "محايد" in result.reasons[1]
    assert math.isfinite(result.final_score)
