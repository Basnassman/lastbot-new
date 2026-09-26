"""
تطبيق تجريبي (Mock) لمصادر البيانات — يُستخدم فقط لاختبار منطق القرار
قبل ربطه بأي اتصال حقيقي. يلتزم بنفس العقود في interfaces.py تماماً.
"""
import random
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from brain.data.interfaces import CorrelationFeed, NewsFeed, PriceFeed


class MockPriceFeed(PriceFeed):
    """يولّد شموع أسعار واقعية الشكل عشوائياً (مشي عشوائي بسيط) لأغراض الاختبار فقط."""

    def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        rng = np.random.default_rng(seed=None)
        base_price = 2650.0  # سعر تقريبي للذهب كنقطة بداية
        returns = rng.normal(loc=0.0, scale=1.2, size=count)
        closes = base_price + np.cumsum(returns)

        rows = []
        now = datetime.now(timezone.utc)
        for i, close in enumerate(closes):
            open_ = close - rng.normal(0, 0.5)
            high = max(open_, close) + abs(rng.normal(0, 0.6))
            low = min(open_, close) - abs(rng.normal(0, 0.6))
            rows.append(
                {
                    "timestamp": now - timedelta(minutes=(count - i) * 5),
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": rng.integers(50, 500),
                }
            )
        return pd.DataFrame(rows)


class MockNewsFeed(NewsFeed):
    def get_upcoming_events(self, symbol: str, minutes_ahead: int) -> list[dict]:
        # افتراضياً: لا أخبار قريبة (لاختبار الحالة العادية)
        # غيّر هذا يدوياً لاختبار سلوك فلتر البيئة عند وجود خبر مهم
        return []


class MockCorrelationFeed(CorrelationFeed):
    def get_dollar_trend(self) -> str:
        return random.choice(["up", "down", "flat"])
