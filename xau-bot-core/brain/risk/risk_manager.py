"""
المرحلة 4: إدارة المخاطر — مستقلة تماماً عن منطق الإشارات.
لها حق "الفيتو" على أي صفقة، بغض النظر عن قوة الإشارة.

هذا نظام واحد فقط وواضح (بعكس المشروع القديم الذي كان فيه
3 ملفات متداخلة لإدارة المخاطر بلا تنسيق بينها).
"""
from dataclasses import dataclass
from datetime import date

from config.settings import settings


@dataclass
class RiskDecision:
    approved: bool
    position_size_usd: float
    reason: str


class RiskManager:
    def __init__(self) -> None:
        self._daily_loss_usd: float = 0.0
        self._current_day: date = date.today()

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if today != self._current_day:
            self._current_day = today
            self._daily_loss_usd = 0.0

    def register_trade_result(self, pnl_usd: float) -> None:
        """يُستدعى بعد إغلاق كل صفقة لتحديث حساب الخسارة اليومية."""
        self._reset_if_new_day()
        if pnl_usd < 0:
            self._daily_loss_usd += abs(pnl_usd)

    def evaluate(self, confidence: float, atr_value: float) -> RiskDecision:
        """
        confidence: 0-100 من نظام التصويت المرجّح
        atr_value: لحساب وقف الخسارة وحجم الصفقة تناسباً مع التقلب
        """
        self._reset_if_new_day()

        max_daily_loss_usd = settings.account_balance_usd * (settings.max_daily_loss_percent / 100.0)
        if self._daily_loss_usd >= max_daily_loss_usd:
            return RiskDecision(
                approved=False,
                position_size_usd=0.0,
                reason=(
                    f"قاطع الدائرة فعّال: الخسارة اليومية ${self._daily_loss_usd:.2f} "
                    f"وصلت/تجاوزت الحد الأقصى ${max_daily_loss_usd:.2f}"
                ),
            )

        if atr_value <= 0:
            return RiskDecision(approved=False, position_size_usd=0.0, reason="بيانات تقلب غير صالحة (ATR=0)")

        # حجم المخاطرة يتناسب مع الثقة: ثقة عالية = مخاطرة كاملة، ثقة متوسطة = نصفها
        base_risk_percent = settings.risk_per_trade_percent
        if confidence >= 90:
            risk_multiplier = 1.0
        elif confidence >= settings.decision_score_threshold:
            risk_multiplier = 0.5
        else:
            return RiskDecision(approved=False, position_size_usd=0.0, reason=f"ثقة غير كافية ({confidence:.1f})")

        risk_amount_usd = settings.account_balance_usd * (base_risk_percent / 100.0) * risk_multiplier

        return RiskDecision(
            approved=True,
            position_size_usd=round(risk_amount_usd, 2),
            reason=f"معتمد: مخاطرة ${risk_amount_usd:.2f} (ثقة={confidence:.1f}, مضاعف={risk_multiplier})",
        )
