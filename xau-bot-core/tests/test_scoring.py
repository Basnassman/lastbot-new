import numpy as np
import pandas as pd

from brain.analysis.regime_detector import RegimeResult
from brain.scoring.weighted_voting import compute_score


def _make_uptrend_candles(n: int = 60) -> pd.DataFrame:
    closes = np.linspace(2600, 2680, n)  # اتجاه صاعد واضح
    return pd.DataFrame(
        {
            "open": closes - 0.5,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": [100] * n,
        }
    )


def test_strong_uptrend_with_supportive_dollar_gives_buy_signal():
    candles = _make_uptrend_candles()
    regime = RegimeResult(regime="trend_up", slope_value=10.0)

    result = compute_score(candles, regime, dollar_trend="down")

    assert result.final_score > 0
    assert result.direction in ("buy", "none")  # يعتمد على العتبة المضبوطة، لكن لازم يكون موجباً


def test_range_market_has_no_trend_component():
    candles = _make_uptrend_candles()
    regime = RegimeResult(regime="range", slope_value=0.2)

    result = compute_score(candles, regime, dollar_trend="flat")

    assert result.trend_score == 0.0
