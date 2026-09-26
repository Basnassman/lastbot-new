"""
المرحلة 2: كشف نظام السوق (Regime Detection).

منطق بسيط ومفهوم بالكامل (وليس نموذج ML معقد) لـ v1:
- إذا كان ميل EMA واضحاً في اتجاه واحد → ترند
- إذا كان الميل شبه مستوٍ → تذبذب (Range)
"""
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from brain.analysis.indicators import ema_slope

Regime = Literal["trend_up", "trend_down", "range"]


@dataclass
class RegimeResult:
    regime: Regime
    slope_value: float


def detect_regime(candles: pd.DataFrame, flat_threshold: float = 1.5) -> RegimeResult:
    slope = ema_slope(candles["close"], period=21, lookback=10)

    if abs(slope) < flat_threshold:
        regime: Regime = "range"
    elif slope > 0:
        regime = "trend_up"
    else:
        regime = "trend_down"

    return RegimeResult(regime=regime, slope_value=slope)
