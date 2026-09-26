"""
هذا الملف هو العقد (Contract) الذي يجب أن يلتزم به أي مصدر بيانات.

الفكرة: بدل أن يعتمد منطق التحليل على "cTrader" أو "Mock" مباشرة،
يعتمد على هذا العقد فقط. لاحقاً عند استبدال Mock بـ cTrader الحقيقي،
لن يتغير أي سطر في طبقة التحليل — هذا بالضبط ما كان مفقوداً في
المشروع القديم (MT5) الذي دمج مصادر بيانات متعددة بدون عقد موحد.
"""
from abc import ABC, abstractmethod

import pandas as pd


class PriceFeed(ABC):
    """أي مصدر أسعار (Mock، cTrader، أي وسيط آخر مستقبلاً) يجب أن يوفر هذه الدالة."""

    @abstractmethod
    def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """
        يُعيد DataFrame بالأعمدة: timestamp, open, high, low, close, volume
        مرتبة من الأقدم إلى الأحدث.
        """
        raise NotImplementedError


class NewsFeed(ABC):
    """أي مصدر أخبار اقتصادية يجب أن يوفر هذه الدالة."""

    @abstractmethod
    def get_upcoming_events(self, symbol: str, minutes_ahead: int) -> list[dict]:
        """
        يُعيد قائمة أحداث اقتصادية قادمة، كل حدث:
        {"name": str, "impact": "high"|"medium"|"low", "minutes_until": int}
        """
        raise NotImplementedError


class CorrelationFeed(ABC):
    """مصدر بيانات الأدوات المرتبطة (الدولار DXY، العوائد...)."""

    @abstractmethod
    def get_dollar_trend(self) -> str:
        """يُعيد 'up' أو 'down' أو 'flat' لاتجاه الدولار الحالي."""
        raise NotImplementedError
