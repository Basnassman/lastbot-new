"""
سكربت تشخيصي (قراءة فقط — لا يعدّل أي كود إنتاج ولا يكتب في أي سجل).

السؤال: لماذا صُنّف النظام السوق "trend_up" في نافذة المراقبة (15 سبتمبر)
بينما السعر كان ينزف فعلياً من 4308 إلى 4294؟ وهل في حساب الميل خطأ؟

الفرضيات المفصولة بالأرقام لكل نقطة قرار:
  A) نافذة المحرك = آخر 100 شمعة **المغلقة** قبل لحظة النداء (المرجع النظري).
  B) الخادم يقتطع **أول** 100 شمعة من نافذة fromTimestamp (أقدم أولاً) —
     أي أن آخر شمعة في نافذة المحرك ليست الأحدث (خطأ اقتطاع محتمل).
  C) cTrader يُرجع الشمعة **الحية قيد التكوّن** كسطر أخير — يقفز الميل قليلاً
     حسب السعر اللحظي. نستنتج السعر اللحظي الضمني x من فرق الميل:
     Δslope = α·(x − close_الأخير_المغلق) حيث α = 2/(21+1)، ثم نتحقق أن x
     يقع داخل نطاق الشمعة النهائية [low, high] وأن RSI المحسوب به x يطابق
     المُسجّل — تطابق مزدوج = تأكيد الفرضية.

نقطة ضبط: نداء 19 سبتمبر 19:52 على سوق **مغلق** (لا شمعة حية ولا اقتطاع
محتمل) — إن طابقت المحاكاة A أرقامه (ميل 2.28، RSI 49.9) فالمنهجية سليمة.

المقارنة مع أرقام محرك القرار الحقيقية المسجلة في logs/decisions.jsonl.
التشغيل:  .venv/bin/python scripts/diagnose_regime.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from brain.analysis.indicators import ema, ema_slope, rsi  # دوال الإنتاج نفسها — استيراد لا نسخ
from brain.data.ctrader_price_feed import CTraderPriceFeed

SYMBOL = "XAUUSD"
TIMEFRAME_MIN = 15
ALPHA = 2.0 / (21 + 1)  # معامل تنعيم EMA(21)

# نقاط القرار مع أرقام المحرك المسجلة (الميل، RSI) وحالتها:
#   real_monitor = من حلقة monitor_log_only (feed حقيقي مؤكد من logs/monitor.log)
#   uncertain    = سطران قبل بدء الحلقة (قد يكونان Mock — يُطابَقان مع تحفظ)
#   control      = الاختبار الدخاني 19 سبتمبر — سوق مغلق (لا شمعة حية)
DECISION_POINTS = [
    {"t": "2026-09-15T19:12:56", "slope": None, "rsi": 50.7, "kind": "uncertain"},
    {"t": "2026-09-15T19:13:22", "slope": 8.88, "rsi": 69.7, "kind": "uncertain"},
    {"t": "2026-09-15T19:17:09", "slope": 8.41, "rsi": 57.6, "kind": "uncertain"},
    {"t": "2026-09-15T19:39:00", "slope": 8.16, "rsi": 58.4, "kind": "real_monitor"},
    {"t": "2026-09-15T19:54:00", "slope": 7.57, "rsi": 53.0, "kind": "real_monitor"},
    {"t": "2026-09-15T20:09:01", "slope": 7.32, "rsi": 54.0, "kind": "real_monitor"},
    {"t": "2026-09-19T19:52:14", "slope": 2.28, "rsi": 49.9, "kind": "control"},
]

KIND_LABEL = {
    "real_monitor": "حقيقي (حلقة المراقبة)",
    "uncertain": "غير مؤكد (قبل بدء الحلقة — قد يكون Mock)",
    "control": "نقطة ضبط — سوق مغلق",
}


def main() -> int:
    feed = CTraderPriceFeed()
    try:
        # 800 شمعة لتغطية يومي 15 و19 سبتمبر مع تاريخ كافٍ قبل كليهما
        df = feed.get_candles(SYMBOL, "M15", 800)
    finally:
        feed.close()
    df = df.reset_index(drop=True)
    print(f"شموع M15 الحقيقية المجلوبة: {len(df)} | من {df.iloc[0]['timestamp']} إلى {df.iloc[-1]['timestamp']}\n")

    # --- سياق يوم 15 سبتمبر: ملخص H1 ليظهر الصعود النهاري الذي يتذكره EMA ---
    sep15 = df[(df["timestamp"] >= "2026-09-15") & (df["timestamp"] < "2026-09-16")]
    hourly = sep15.set_index("timestamp").resample("1h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    print("=== سياق السوق: ملخص H1 ليوم 15 سبتمبر (UTC) ===")
    print(hourly[["open", "high", "low", "close", "volume"]].round(2).to_string())
    print(f"عتبة التصنيف: |slope| < 1.5 → range | إشارة trend_up إذا slope > 0\n")

    matches = {"A": [], "B": [], "C": []}

    for point in DECISION_POINTS:
        t = pd.Timestamp(datetime.fromisoformat(point["t"]).replace(tzinfo=timezone.utc))
        t = t.tz_localize(None) if df["timestamp"].dt.tz is None else t

        # --- المحاكاة A: آخر 100 شمعة مغلقة بصرامة قبل لحظة النداء ---
        closed = df[df["timestamp"] + pd.Timedelta(minutes=TIMEFRAME_MIN) <= t]
        window_a = closed.tail(100)
        e_a = ema(window_a["close"], 21)
        slope_a = float(ema_slope(window_a["close"], period=21, lookback=10))
        rsi_a = float(rsi(window_a["close"]).iloc[-1])
        ema_a_last = float(e_a.iloc[-1])
        regime_a = "range" if abs(slope_a) < 1.5 else ("trend_up" if slope_a > 0 else "trend_down")

        # اختبار الانعكاس: لو كانت فهرسة الميل معكوسة لكانت النتيجة بهذا الشكل
        slope_reversed = float(ema_slope(window_a["close"].iloc[::-1].reset_index(drop=True), period=21, lookback=10))

        def ctx(bars: int) -> str:
            return "N/A" if len(e_a) < bars else f"{float(e_a.iloc[-1] - e_a.iloc[-bars]):+.2f}"

        # --- المحاكاة B: أول 100 شمعة من نافذة 50 ساعة (فرضية الاقتطاع الأقدم-أولاً) ---
        span = df[(df["timestamp"] >= t - pd.Timedelta(hours=50)) & (df["timestamp"] < t)]
        window_b = span.head(100)
        slope_b = float(ema_slope(window_b["close"], period=21, lookback=10))
        rsi_b = float(rsi(window_b["close"]).iloc[-1])

        # --- المحاكاة C: فرضية الشمعة الحية — استنتاج السعر اللحظي الضمني ---
        last_closed = window_a.iloc[-1]
        forming = df[df["timestamp"] == last_closed["timestamp"] + pd.Timedelta(minutes=TIMEFRAME_MIN)]
        has_forming = len(forming) == 1 and (last_closed["timestamp"] + pd.Timedelta(minutes=TIMEFRAME_MIN)) < t

        # الترتيب الصحيح: نفحص مطابقة A أولاً — إن طابقت فلا حاجة لأي فرضية بديلة،
        # واختبار C بعدها يكون محاولة تفسير بقايا لا تُذكر (مطابقة فارغة).
        a_match = (
            point["slope"] is not None
            and abs(slope_a - point["slope"]) < 0.05
            and abs(rsi_a - point["rsi"]) <= 0.5
        )
        b_match = (
            point["slope"] is not None
            and abs(slope_b - point["slope"]) < 0.05
            and abs(rsi_b - point["rsi"]) <= 0.5
        )
        verdict = ""
        if a_match:
            matches["A"].append(point["t"])
            verdict = "المحاكاة A (مغلقة صارمة) تطابق أرقام المحرك ✓ — لا شمعة حية ولا اقتطاع"
        elif b_match:
            matches["B"].append(point["t"])
            verdict = "المحاكاة B (اقتطاع أول-100) تطابق أرقام المحرك ⚠ — مؤشر خطأ اقتطاع من الخادم"
        elif point["slope"] is not None and has_forming:
            implied_x = float(last_closed["close"]) + (point["slope"] - slope_a) / ALPHA
            bar_low = float(forming.iloc[0]["low"])
            bar_high = float(forming.iloc[0]["high"])
            plausible = (bar_low - 2.0) <= implied_x <= (bar_high + 2.0)
            if plausible:
                close_c = list(window_a["close"]) + [implied_x]
                slope_c = float(ema(pd.Series(close_c), 21).iloc[-1] - ema(pd.Series(close_c), 21).iloc[-11])
                rsi_c = float(rsi(pd.Series(close_c)).iloc[-1])
                rsi_ok = abs(rsi_c - point["rsi"]) <= 1.5
                verdict = (
                    f"الفرضية C (شمعة حية): السعر اللحظي الضمني x={implied_x:.2f} "
                    f"داخل نطاق الشمعة النهائية [{bar_low:.2f}, {bar_high:.2f}] → {'صالح' if plausible else 'مرفوض'} | "
                    f"RSI به x = {rsi_c:.1f} مقابل المُسجّل {point['rsi']} → {'طابق ✓' if rsi_ok else 'لا يطابق ✗'}"
                )
                if rsi_ok:
                    matches["C"].append(point["t"])
            else:
                verdict = f"الفرضية C مرفوضة: السعر الضمني x={implied_x:.2f} خارج نطاق الشمعة [{bar_low:.2f}, {bar_high:.2f}]"
        elif point["slope"] is not None:
            verdict = "لا A ولا B ولا C تطابق — انظر فرق الأرقام أدناه"

        print(f"=== نقطة القرار {point['t']} — {KIND_LABEL[point['kind']]} ===")
        print(f"  أرقام المحرك المسجلة: slope={point['slope']} | RSI={point['rsi']}")
        print(f"  آخر شمعة مغلقة: {last_closed['timestamp']} | close={last_closed['close']:.2f}")
        print(f"  [A] ema(21)={ema_a_last:.3f} | slope={slope_a:+.3f} → {regime_a} | RSI={rsi_a:.1f}")
        print(f"      مكونا الميل: EMA(-11)={float(e_a.iloc[-11]):.3f} → EMA(-1)={ema_a_last:.3f}")
        print(f"      [اختبار الانعكاس] slope على السلسلة المعكوسة = {slope_reversed:+.3f} (لو أضاف الكود اتجاهاً معكوساً لتطابق هذا)")
        print(f"      سياق أوسع: slope-30={ctx(31)} | slope-60={ctx(61)} (وحدة: نقاط سعر)")
        print(f"  [B] أول-100 من نافذة 50 ساعة: slope={slope_b:+.3f} | RSI={rsi_b:.1f} | آخر شمعة فيها: {window_b.iloc[-1]['timestamp']}")
        if verdict:
            print(f"  → {verdict}")
        print()

    print("=== ملخص المطابقة ===")
    for key, label in (("A", "مغلقة صارمة"), ("B", "اقتطاع أول-100"), ("C", "شمعة حية (السعر الضمني)")):
        print(f"  {label}: {len(matches[key])} نقاط {matches[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
