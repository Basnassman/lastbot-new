"""
حلقة مراقبة يدوية (مراقبة فقط — لا تنفيذ).

ترفع واجهة API داخل نفس العملية (uvicorn في خيط) بمصدر الأسعار الحقيقي
(USE_REAL_CTRADER_FEED=true) ثم تستدعي /decide-log-only عبر HTTP حقيقي
كل 15 دقيقة (بما يماثل M15) لمدة 14 نداءً (المحصلة 3 ساعات و15 دقيقة ≥ 3 ساعات).

كل نداء يُطبع مع عدد سطور logs/decisions.jsonl كمرجع لاستخراج قرارات
النافذة لاحقاً — الملف نفسه لا يُلمس من هنا (الكتابة مصدرها المحرك فقط).

بوابة اختيارية (WAIT_FOR_LIVE_MARKET=true): قبل عدّ النداءات تنتظر ظهور أول
شمعة M15 أحدث من آخر شمعة متاحة الآن — دليل فعلي أن السوق ينتج شموعاً. بدونها
قد تجري النافذة كلها على شموع مغلقة قبل الافتتاح (ما حدث في نافذة 21 سبتمبر).
"""
import os
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

# عند تشغيل السكربت مباشرة يكون مجلد scripts/ على sys.path — نضيف جذر المشروع لنستورد brain
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import uvicorn

os.environ.setdefault("USE_REAL_CTRADER_FEED", "true")

from brain.api.main import app  # noqa: E402 — بعد ضبط متغير البيئة

LOG_PATH = Path("logs/decisions.jsonl")
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8123  # منفذ جانبي: لا يتعارض مع أي تشغيل آخر
INTERVAL_SECONDS = 15 * 60
TOTAL_CALLS = 14  # من أول نداء لآخر نداء: 13 فاصل × 15 دقيقة = 3 ساعات و15 دقيقة

# بوابة السوق الحي (اختيارية، افتراضياً معطّلة): تنتظر أول شمعة M15 جديدة قبل العدّ
WAIT_FOR_LIVE_MARKET = os.environ.get("WAIT_FOR_LIVE_MARKET", "false").strip().lower() == "true"
# أقصى انتظار للافتتاح قبل إلغاء النافذة — قابل للتجاوز بالبيئة (انتظار نهاية الأسبوع يحتاج ~40 ساعة)
MARKET_OPEN_TIMEOUT_MINUTES = int(os.environ.get("MARKET_OPEN_TIMEOUT_MINUTES", "150"))
MARKET_POLL_SECONDS = 60


def log_line(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{stamp}] {message}", flush=True)


def lines_count() -> int:
    if not LOG_PATH.exists():
        return 0
    with LOG_PATH.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def last_candle_open_utc(symbol: str = "XAUUSD", timeframe: str = "M15") -> datetime:
    """آخر شمعة متاحة من الفيد (وقت فتحها UTC) — مرجع كشف الحيّة."""
    from brain.data.ctrader_price_feed import CTraderPriceFeed  # استيراد محلي بعد ضبط متغيرات البيئة

    feed = CTraderPriceFeed()
    try:
        df = feed.get_candles(symbol, timeframe, 1)
    finally:
        feed.close()
    stamp = df.iloc[-1]["timestamp"]
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(timezone.utc)
    return stamp.to_pydatetime().astimezone(timezone.utc)


def wait_for_live_market(symbol: str = "XAUUSD", timeframe: str = "M15") -> bool:
    """تنتظر حتى يُثبت الفيد إنتاج شموع جديدة الآن قبل السماح ببدء النداءات.

    المرجع = آخر شمعة متاحة عند أول جلب ناجح (قبل الافتتاح غالباً شمعة الجمعة)؛
    ثم استطلاع كل MARKET_POLL_SECONDS حتى تظهر شمعة أحدث منه. جلب فاشل أو فارغ
    في البداية (إعادة تعيين ما قبل الافتتاح عند الوسيط) لا يُفشل البوابة — يُعاد
    المحاولة حتى مهلة MARKET_OPEN_TIMEOUT_MINUTES. عدم ظهور أي شمعة جديدة خلال
    المهلة = السوق لم يفتح → إلغاء النافذة قبل أول نداء (بدون تلويث السجل).
    """
    log_line("بوابة السوق الحي مفعّلة — انتظار أول شمعة M15 جديدة قبل بدء العدّ")
    reference: datetime | None = None

    deadline = time.monotonic() + MARKET_OPEN_TIMEOUT_MINUTES * 60
    while time.monotonic() < deadline:
        try:
            latest = last_candle_open_utc(symbol, timeframe)
        except Exception as error:  # noqa: BLE001 — فشل استطلاع عابر لا يُلغي الانتظار
            log_line(f"استطلاع فاشل (يُعاد): {error}")
            time.sleep(MARKET_POLL_SECONDS)
            continue
        if reference is None:
            reference = latest
            log_line(f"مرجع الانتظار: آخر شمعة متاحة حالياً فتحت {reference.isoformat()}")
        elif latest > reference:
            log_line(f"السوق حي: ظهرت شمعة تفتح {latest.isoformat()} — بدء عدّ النداءات")
            return True
        else:
            log_line(f"لا شموع جديدة بعد (آخر فتح: {latest.isoformat()}) — استمرار الانتظار")
        time.sleep(MARKET_POLL_SECONDS)
    log_line("انتهت مهلة انتظار فتح السوق — إلغاء النافذة دون أي نداء")
    return False


def main() -> int:
    log_line("بدء حلقة المراقبة (log-only) — مصدر الأسعار: cTrader Demo الحقيقي")

    config = uvicorn.Config(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    deadline = time.monotonic() + 60
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.2)
    if not server.started:
        log_line("خطأ: فشل رفع السيرفر خلال 60 ثانية")
        return 1

    log_line(f"السيرفر يعمل على {SERVER_HOST}:{SERVER_PORT} — أول نداء بعد 60 ثانية من الإقلاع")

    if WAIT_FOR_LIVE_MARKET:
        try:
            live = wait_for_live_market()
        except Exception as error:  # noqa: BLE001 — فشل البوابة لا يجب أن يترك العملية معلّقة
            log_line(f"فشل بوابة السوق الحي: {error}")
            live = False
        if not live:
            log_line("إيقاف السيرفر وإنهاء الحلقة قبل بدء النداءات")
            server.should_exit = True
            server_thread.join(timeout=15)
            return 1

    start_marker = lines_count()
    log_line(f"سطور السجل قبل الحلقة: {start_marker}")

    try:
        with httpx.Client(timeout=120.0) as client:
            for call_index in range(1, TOTAL_CALLS + 1):
                if call_index > 1:
                    log_line("انتظار 15 دقيقة…")
                    time.sleep(INTERVAL_SECONDS)
                started = datetime.now(timezone.utc).isoformat(timespec="seconds")
                try:
                    response = client.get(
                        f"http://{SERVER_HOST}:{SERVER_PORT}/decide-log-only",
                        params={"symbol": "XAUUSD", "timeframe": "M15"},
                    )
                    after = lines_count()
                    body = response.json()
                    log_line(
                        f"نداء {call_index}/{TOTAL_CALLS} @ {started} → HTTP {response.status_code} | "
                        f"action={body.get('action')} | confidence={round(float(body.get('confidence', 0.0)), 2)} | "
                        f"size_usd={body.get('position_size_usd')} | سطور السجل بعد النداء: {after}"
                    )
                except Exception as error:  # noqa: BLE001 — نداء فاشل يُسجّل وتُكمل الحلقة
                    log_line(f"نداء {call_index}/{TOTAL_CALLS} @ {started} → فشل: {error}")
    finally:
        log_line("إيقاف السيرفر وإنهاء الحلقة")
        server.should_exit = True
        server_thread.join(timeout=15)

    final_count = lines_count()
    log_line(f"تم. سطور السجل قبل: {start_marker}، بعد: {final_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
