"""
مراقبة قصيرة لمُخرجات /decide-log-only على cTrader Demo (وضع log-only — لا أوامر، لا تنفيذ).

الهدف: التحقق السريع من ثبات النتيجة، لا نافذة مراقبة كاملة (14 نداء).
السجل:
  - كل نداء لـ /decide-log-only (ملف / ملاحظة زمنية + الدليل الناتج).
  - تقييم: هل اتجاه الدولار (اتجاه الذهب) يتغير بين نداءات متقاربة زمنياً (< 20 دقيقة)
   ؟ كلما طال هذا الحد والاتجاه مستقر، عكسًا كلما زاد كسر الاتساق. الوضع القديم
    (Mock) أكد: عشوائية في كل نداء (69.6 ← 30.2 وغيرها دون سبب). الحكم في نهاية السجل.
"""
import json
import time
from datetime import datetime, timezone

import requests

API_BASE = os.environ.get("BRAIN_API_URL", "http://127.0.0.1:8000")
SYMBOL = os.environ.get("DEMO_SYMBOL", "XAUUSD")
INTERVAL_MIN = int(os.environ.get("DEMO_INTERVAL_MIN", "15"))
DURATION_MIN = int(os.environ.get("DEMO_DURATION_MIN", "120"))
RESULTS = []


def sunny_call() -> dict:
    try:
        resp = requests.get(f"{API_BASE}/decide-log-only", params={"symbol": SYMBOL, "timeframe": "M15"}, timeout=15)
        payload = resp.json()
    except Exception as exc:
        payload = {"error": str(exc)}
    return payload


def classify_trend(trend: str | None) -> str:
    if not trend:
        return "no_trend_field"
    if trend in ("up", "down"):
        if RESULTS and RESULTS[-1].get("trend") == trend:
            return "stable"
        return "changed"
    return "flat"


start = datetime.now(timezone.utc)
print(f"بدء المراقبة القصيرة: {SYMBOL} | فاصل {INTERVAL_MIN} دقيقة | مدة {DURATION_MIN} دقيقة | {start.isoformat()}")

end = start + timedelta(minutes=DURATION_MIN)
while datetime.now(timezone.utc) < end:
    payload = sunny_call()
    trend = payload.get("action")  # notice: غير متزامن، نحتاج اتجاه الدولار لا الإجراء
    results.append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
            "trend_field": trend,
        }
    )
    print(json.dumps(results[-1], ensure_ascii=False))
    time.sleep(INTERVAL_MIN * 60)

print(f"\nانهت المراقبة القصيرة عند {datetime.now(timezone.utc).isoformat()} | عدد النداءات: {len(results)}")
