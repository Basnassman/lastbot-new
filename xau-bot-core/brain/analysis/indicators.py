"""
مؤشرات فنية أساسية محسوبة يدوياً (بدون TA-Lib) لتبسيط التثبيت في v1.
لاحقاً يمكن استبدالها بمكتبة متخصصة دون تغيير أي طبقة أخرى في النظام.
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI بطريقة EWM الحالية (alpha=1/period, adjust=False).

    - قبل اكتمال ``period`` من التغيرات تُعاد NaN (لا قيم مُصنّعة).
    - متوسط خسارة صفري مع مكاسب موجبة يعطي 100، والعكس يعطي 0،
      وصفر في الاثنين يعطي 50.
    - أي NaN أو Infinity في المدخلات يُرفض بخطأ صريح.
    """
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)):
        raise TypeError(f"period must be an integer, got {type(period).__name__}")
    period = int(period)
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")

    values = pd.Series(series, dtype="float64")
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError("RSI input contains non-finite values (NaN or Infinity)")

    delta = values.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    ag = avg_gain.to_numpy(dtype="float64")
    al = avg_loss.to_numpy(dtype="float64")
    result = np.full(len(values), np.nan, dtype="float64")

    available = np.isfinite(ag) & np.isfinite(al)
    both_zero = available & (ag == 0) & (al == 0)
    zero_loss = available & (ag > 0) & (al == 0)
    zero_gain = available & (ag == 0) & (al > 0)
    normal = available & ~(both_zero | zero_loss | zero_gain)

    result[zero_loss] = 100.0
    result[zero_gain] = 0.0
    result[both_zero] = 50.0
    result[normal] = 100 - (100 / (1 + ag[normal] / al[normal]))
    return pd.Series(result, index=values.index, name=values.name)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def ema_slope(series: pd.Series, period: int = 21, lookback: int = 5) -> float:
    """يقيس ميل EMA على آخر عدد شموع — مؤشر بسيط لقوة واتجاه الترند."""
    e = ema(series, period)
    if len(e) < lookback + 1:
        return 0.0
    return float(e.iloc[-1] - e.iloc[-1 - lookback])
