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


def _get_int(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        # قيمة غير رقمية → نرجع للافتراضي بدل إسقاط الخدمة عند الإقلاع
        return default


def _get_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


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

    # --- المرحلة 1: تحليل البنية السوقية (دعم/مقاومة/سيولة محتملة) ---
    # هذا التحليل إضافي فقط: لا يغيّر أي قرار أو حجم أو وقف خسارة. عند تعطيله
    # (MARKET_STRUCTURE_ENABLED=false) يبقى كل السلوك التنفيذي كما هو تماماً.
    market_structure_enabled: bool = _get_bool("MARKET_STRUCTURE_ENABLED", True)
    # عدد شموع التأكيد على كل جانب لتثبيت نقطة انعطاف (Swing). نقطة تتطلب شموعاً
    # مستقبلية لا تُعتبر مؤكدة ولا تُستخدم (منع النظر إلى المستقبل).
    market_structure_swing_window: int = _get_int("MARKET_STRUCTURE_SWING_WINDOW", 2)
    # تسامح دمج المستويات المتقاربة في منطقة واحدة = المضاعف × ATR (ATR الحالي نفسه،
    # غير معدَّل). بلا ATR صالح يُستخدم وسيط المدى كبديل موثَّق.
    market_structure_merge_tolerance_atr: float = _get_float("MARKET_STRUCTURE_MERGE_TOLERANCE_ATR", 0.5)
    # الحد الأدنى لعدد التفاعلات المؤهلة لاعتبار المنطقة صالحة.
    market_structure_min_reactions: int = _get_int("MARKET_STRUCTURE_MIN_REACTIONS", 2)
    # الحد الأقصى لعدد المناطق المُعادة لكل جانب (دعم/مقاومة) — سقف حجم الاستجابة.
    market_structure_max_zones: int = _get_int("MARKET_STRUCTURE_MAX_ZONES", 5)
    # نافذة الحداثة (بالشموع): تفاعل أقدم منها يُعطى حداثة صفرية في قوة المنطقة.
    market_structure_recency_bars: int = _get_int("MARKET_STRUCTURE_RECENCY_BARS", 50)
    # الحد الأقصى لعدد مناطق السيولة المحتملة المُعادة (سقف حجم الاستجابة).
    market_structure_max_liquidity_levels: int = _get_int("MARKET_STRUCTURE_MAX_LIQUIDITY_LEVELS", 6)


settings = Settings()
