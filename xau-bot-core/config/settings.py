"""
كل الإعدادات القابلة للتعديل في مكان واحد فقط.
لا تُكتب أي قيمة إعداد مباشرة في الكود في مكان آخر — تُقرأ من هنا دائماً.

مُبسّط عمداً (dataclass + python-dotenv بدل pydantic-settings)
لتقليل عدد الاعتماديات الخارجية في v1.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_float(key: str, default: float) -> float:
    value = os.getenv(key)
    return float(value) if value else default


def _get_str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class Settings:
    # --- cTrader Open API (تُقرأ من هنا حصراً — لا قيم إعداد مكتوبة في أي ملف آخر) ---
    ctrader_client_id: str = _get_str("CTRADER_CLIENT_ID")
    ctrader_client_secret: str = _get_str("CTRADER_CLIENT_SECRET")
    ctrader_account_id: str = _get_str("CTRADER_ACCOUNT_ID")
    ctrader_environment: str = _get_str("CTRADER_ENVIRONMENT", "demo")
    # رمز الوصول الصادر من openapi.ctrader.com — مطلوب للمصادقة على الحساب
    ctrader_access_token: str = _get_str("CTRADER_ACCESS_TOKEN")
    # رمز التجديد (بديل عن رمز الوصول): يُستخدم لتجديد التوكن تلقائياً عند انتهائه
    ctrader_refresh_token: str = _get_str("CTRADER_REFRESH_TOKEN")
    # مهلة الاتصال/الاستجابة بالثواني — بعدها يُطلق استثناء صريح بدل الانتظار اللانهائي
    ctrader_connection_timeout_seconds: float = _get_float("CTRADER_CONNECTION_TIMEOUT_SECONDS", 10.0)

    # --- الأخبار الاقتصادية ---
    economic_calendar_api_key: str = _get_str("ECONOMIC_CALENDAR_API_KEY")

    # --- إدارة المخاطر ---
    risk_per_trade_percent: float = _get_float("RISK_PER_TRADE_PERCENT", 1.0)
    max_daily_loss_percent: float = _get_float("MAX_DAILY_LOSS_PERCENT", 5.0)
    account_balance_usd: float = _get_float("ACCOUNT_BALANCE_USD", 100.0)

    # --- عتبة القرار (المرحلة 3: التصويت المرجّح) ---
    decision_score_threshold: float = _get_float("DECISION_SCORE_THRESHOLD", 75.0)

    # --- أوزان طبقات التصويت (يجب أن يكون مجموعها 100) ---
    weight_trend: float = 45.0
    weight_momentum: float = 30.0
    weight_dollar_correlation: float = 25.0


settings = Settings()
