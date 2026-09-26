# XAU Bot — النواة (v1)

هذه هي **النواة الأولى** فقط — الهدف إثبات أن منطق القرار سليم، وليس بناء كل الطبقات دفعة واحدة.

## الفلسفة

- استراتيجية واحدة فقط (لا Ensemble معقد)
- نظام مخاطر واحد بسيط وواضح
- كل قرار يُسجَّل مع سببه الكامل
- بيانات وهمية (Mock) الآن للاختبار — تُستبدل لاحقاً ببيانات حية من cTrader Open API

## هيكل المشروع

```
xau-bot-core/
├── config/
│   └── settings.py          # كل الإعدادات القابلة للتعديل في مكان واحد
├── brain/
│   ├── data/
│   │   ├── interfaces.py    # العقد (Interface) الذي يجب أن تلتزم به أي مصدر بيانات
│   │   └── mock_price_feed.py  # بيانات تجريبية لاختبار المنطق بدون اتصال حي
│   ├── analysis/
│   │   ├── indicators.py         # EMA, RSI, ATR — بدون مكتبات خارجية ثقيلة
│   │   ├── environment_filter.py # المرحلة 1: هل السوق صالح للتداول الآن؟
│   │   └── regime_detector.py    # المرحلة 2: ترند أم تذبذب؟
│   ├── scoring/
│   │   └── weighted_voting.py    # المرحلة 3: نظام التصويت المرجّح المبسّط
│   ├── risk/
│   │   └── risk_manager.py       # المرحلة 4: حجم الصفقة + قاطع الدائرة اليومي
│   ├── decision_engine.py        # يجمع كل المراحل في قرار نهائي واحد
│   ├── logger.py                 # يسجل كل قرار + سببه بصيغة JSON Lines
│   └── api/
│       └── main.py               # FastAPI — الواجهة التي سيستدعيها cBot لاحقاً
├── tests/
│   └── test_scoring.py
├── run_demo.py               # تشغيل دورة قرار كاملة ببيانات تجريبية (بدون API)
├── requirements.txt
├── .env.example
└── docker-compose.yml        # Redis + PostgreSQL (للمستقبل، غير مُفعّل الآن)
```

## التشغيل السريع

```bash
python -m venv venv
source venv/bin/activate          # على ويندوز: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

# اختبار المنطق ببيانات تجريبية (بدون أي اتصال خارجي)
python run_demo.py

# تشغيل الخدمة كـ API (لاستقبال طلبات من cBot لاحقاً)
uvicorn brain.api.main:app --reload --port 8000
```

## ما هو موجود الآن (v1)

- ✅ فلتر البيئة (سيولة تقريبية عبر وقت اليوم + فحص تقلب ATR)
- ✅ كشف النظام (ترند/تذبذب) بمنطق EMA slope بسيط
- ✅ تصويت مرجّح بـ 3 مكونات: الاتجاه الفني، الزخم (RSI)، الارتباط مع الدولار (مُدخل يدوياً/محاكى الآن)
- ✅ نظام مخاطر: حجم مخاطرة ديناميكي حسب الثقة + قاطع دائرة يومي
- ✅ تسجيل كل قرار مع الأسباب الكاملة (`logs/decisions.jsonl`)
- ✅ واجهة FastAPI جاهزة ليستدعيها cBot عبر HTTP

## ما هو غير موجود بعد (عمداً)

- ❌ اتصال حي بـ cTrader Open API (المرحلة التالية)
- ❌ مصدر أخبار اقتصادية حقيقي (الآن Mock)
- ❌ نماذج تعلم آلي
- ❌ Kafka / Kubernetes / مراقبة متقدمة
- ❌ استراتيجيات متعددة (Ensemble)

## الخطوة التالية بعد هذه النواة

1. تشغيل `run_demo.py` والتأكد أن المنطق منطقي ومفهوم لك بالكامل
2. استبدال `mock_price_feed.py` باتصال حقيقي بـ cTrader Open API
3. بناء الـ cBot بلغة C# الذي يستدعي `/decide` من هذه الخدمة وينفذ القرار
4. تشغيل Forward Testing على حساب Demo لعدة أسابيع قبل أي تطوير إضافي
