"""
مؤشرات فنية أساسية محسوبة يدوياً (بدون TA-Lib) لتبسيط التثبيت في v1.
لاحقاً يمكن استبدالها بمكتبة متخصصة دون تغيير أي طبقة أخرى في النظام.
"""
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.fillna(50)  # قيمة محايدة إذا لم تتوفر بيانات كافية


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
