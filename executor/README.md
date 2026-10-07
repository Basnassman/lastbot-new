# executor/ — cBot لـ cTrader Automate (C#): مراقبة + تنفيذ مشروط

يستدعي هذا الـcBot Brain API عند كل إغلاق شمعة M15، ويسجّل القرار، ويمكنه — بعد تفعيل
مفتاح صريح — **تنفيذ صفقة واحدة** على حساب **Demo فقط** بوقف خسارة وهدف ربح إلزاميين.

> **تنبيه صريح:** مفتاح التنفيذ `ExecutionEnabled` قيمته الافتراضية **false دائماً عند أي
> نشر جديد**. ما لم يُفعِّله المستخدم يدوياً من واجهة cTrader ⇒ لا صفقات إطلاقاً، بغض النظر
> عن أي قرار من Brain API. هذا منفصل تماماً عن فحص `Account.IsLive` — كلاهما يجب أن يمرّ.

```
executor/
├── XauBotExecutor/
│   └── XauBotExecutor.cs       # ← الـcBot نفسه (ملف واحد جاهز للاستيراد في cTrader)
├── harness/                     # أدوات تحقق خارج cTrader (ليست جزءاً من الـcBot)
│   ├── CAlgoApiShim/           # شيم cAlgo.API مبني من التوثيق (للتجميع/الاختبار فقط)
│   ├── ExecutorHarness/        # يشغّل ملف الـcBot الحقيقي في سيناريوهات محاكاة
│   └── run_harness.sh
└── out/                         # السجلات الناتجة عن التشغيل الفعلي
```

---

## 1) المبدأ: الدفعة الحالية

| البُعد | السلوك |
|------|---------|
| الزناد | عند كل إغلاق شمعة M15 (`Bars.BarClosed` على `TimeFrame.Minute15`) |
| النداء | `GET {scheme}://{host}:{port}/{base}decide-log-only?symbol=…&timeframe=M15` |
| التسجيل | كل استجابة تُطبع في سجل cTrader مع بادئة `[XAU-BOT]` |
| فحص الحماية | إن كان `Account.IsLive == true` → `Stop()` فوراً (بلا أي Parameter لتخطّيه) |
| التنفيذ | **معطّل افتراضياً** (`ExecutionEnabled=false`). عند تفعيله: طبقات 0..7 أدناه |

### الطبقات المنفَّذة

- **الطبقة 0 — مفتاح إيقاف رئيسي:** `ExecutionEnabled` (bool، افتراضي **false**).
- **الطبقة 1 — شروط الدخول (كلها معاً):**
  1. `ExecutionEnabled == true`
  2. `Account.IsLive == false`
  3. `action` من `/decide-log-only` هو `buy` أو `sell` فقط
  4. `position_size_usd > 0` وليس NaN/لانهائي و`≤ MaxPositionSizeUsd`
  5. لا يوجد مركز مفتوح مسبقاً على الرمز من هذا الـcBot — يُفحَص من `Positions.FindAll(OrderLabel, symbol)`
     الفعلية على الحساب (لا من متغير داخلي)، فيصمد بعد إعادة التشغيل.
- **الطبقة 2 — وقف الخسارة إلزامي:** يُحسب من ATR محلياً من شموع الـcBot بنفس منطق
  `brain/analysis/indicators.py` (Wilder smoothing). تعذّر ATR (بيانات غير كافية/ATR≤0) ⇒
  **لا صفقة** بلا قيمة افتراضية بديلة.
- **الطبقة 3 — هدف الربح:** `TP = SL × RiskRewardRatio` (افتراضي 1.5).
- **الطبقة 4 — Lot Sizing:** `rawVolume = position_size_usd / (SL_pips × Symbol.PipValue)`
  من مواصفات الرمز الحقيقية، ثم تقريب **للأسفل** لأقرب `VolumeInUnitsStep`. إن كان الحجم
  أقل من `VolumeInUnitsMin` ⇒ **لا صفقة** (لا تقريب للأعلى يتجاوز المخاطرة المقصودة).
- **الطبقة 5 — التنفيذ والتحقق:** `ExecuteMarketOrder(..., slPips, tpPips, ...)` مع SL/TP في
  **نفس الاستدعاء**. يُتحقَّق من `TradeResult.IsSuccessful` و`Position`، ويُسجَّل رقم المركز
  والسعر الفعلي والحجم وSL/TP الفعليان. عند الفشل: تسجيل كامل لخطأ المنصة، بلا إعادة محاولة
  تلقائية بنفس الشمعة.
- **الطبقة 6 — تحديث حلقة المخاطر:** الاشتراك في `Positions.Closed`؛ عند إغلاق أي مركز
  (TP/SL/يدوي) يُرسَل `POST /trade-result?pnl_usd=<NetProfit>` لتحديث قاطع الدائرة اليومي.
- **الطبقة 7 — منع التكرار:** وقت شمعة القرار (`Bars.LastBar.OpenTime`) مفتاح منع تكرار للأمر.

### Parameters

| المعامل | النوع | الافتراضي | ملاحظة |
|------|------|--------|------|
| `Execution Enabled (master switch)` | bool | **false** | يجب أن يبقى false عند النشر |
| `Max Position Size (USD risk)` | double | 5.0 | سقف مخاطرة محافظ (≈5% من حساب Demo افتراضي 100$) |
| `Risk/Reward Ratio` | double | 1.5 | TP = SL × RR |
| `Stop-Loss ATR Multiplier` | double | 1.5 | مسافة SL = ATR × المضاعف |
| `ATR Period` | int | 14 | نفس فترة `indicators.py` |
| `ATR Lookback Bars` | int | 100 | يطابق عدد الشموع التي يستخدمها Brain |
| `Brain API Host/Port/Use HTTPS/Base Path` | — | — | لا Host/Port مكتوب يدوياً في الكود |
| `Symbol` | string | XAUUSD | رمز الذهب عند الوسيط |

---

## 2) الاستيراد والتشغيل داخل cTrader Automate

1. cTrader ← **Automate** ← **cBots** ← **New** ← **C#**.
2. الصق **كامل** محتوى `XauBotExecutor/XauBotExecutor.cs` (اسم الكلاس `XauBotExecutor`).
3. **Build** — يجب أن ينجح. الملف يستخدم `Http.Get` و`Http.Send` + `HttpRequest` (بدون
   `AccessRights.FullAccess`؛ مضبوط على `AccessRights.None`).
4. سجّل دخولك على **حساب Demo**، ثم أضف الـcBot على شارت **XAUUSD**.
5. اضبط الـParameters (`Brain API Host`/`Port`، `Symbol`).
6. عند الرغبة في التنفيذ الفعلي: فعّل `ExecutionEnabled=true` يدوياً، واضبط
   `MaxPositionSizeUsd` لأدنى قيمة ممكنة في البداية.
7. راقب تبويب **Log**، وتبويب **Positions/History** للتأكد من SL/TP المسجَّلين على الخادم.

---

## 3) التحقق — ما أُثبِت فعلياً وما لم يُثبَت بعد

> **إفصاح صريح:** هذه البيئة (Linux، بدون cTrader وبدون حساب وسيط) لا تسمح بتشغيل cBot
> داخل منصة cTrader على حساب Demo حقيقي. لذلك **لم يُنفَّذ اختبار صفقة حقيقية على Demo من
> داخل cTrader**، ولا توجد لقطة شاشة من تبويب Positions/History. ما يلي هو أقوى تحقق ممكن
> خارج المنصة، وهو **موسوم بوضوح** ولا يُقدَّم على أنه تشغيل cTrader.

### 3.أ — ما تم إثباته فعلياً ✅

الـHARNESS شيء واحد: ملف الـcBot الحقيقي نفسه يُبنى ويُشغَّل مقابل شيم `cAlgo.API` مبني من
التوثيق الرسمي. أمر التشغيل:

```bash
cd xau-bot-core
.venv/bin/python -m uvicorn brain.api.main:app --host 127.0.0.1 --port 8000 &   # خادم Brain

cd ../executor/harness
DOTNET="$HOME/.dotnet/dotnet" ./run_harness.sh
#   → out/scenarios_summary.txt + out/scenario_*.log
```

النتيجة: **67 تأكيداً ناجحاً، 0 فشل، 0 خطأ بناء، 0 تحذير**. تغطي السيناريوهات:

| السيناريو | الملف | ما يُثبِته |
|------|------|------|
| A | `scenario_A_demo_ok.log` | حساب Demo: 3 نداءات ناجحة على `/decide-log-only` + تسجيل القرار + سطر `ExecutionEnabled=false` |
| B | `scenario_B_live_guard_stop.log` | `IsLive=true` → `Stop()` فوراً، **صفر** نداءات HTTP |
| C | `scenario_C_network_error_then_recover.log` | فشل شبكة لا يُسقط الـcBot، ونجاح عند الشمعة التالية |
| D/E/F | `scenario_D/E/F*.log` | HTTP 500 / JSON غير صالح / استثناء `Http.Get` — كلها تُسجَّل ولا تُسقط |
| G | `scenario_G_symbol_missing.log` | رمز غير موجود → إيقاف برسالة واضحة |
| **H** | `scenario_H_execution_success_buy.log` | **فتح شراء**: أمر واحد، `SL=15 pip`, `TP=22.5 pip` (RR=1.5)، `volume=10`، SL/TP مرفقان في نفس الأمر، وتسجيل التفاصيل |
| H2 | `scenario_H2_execution_success_sell.log` | نفس الشيء لاتجاه البيع |
| I | `scenario_I_execution_disabled.log` | `ExecutionEnabled=false` ⇒ لا أمر |
| J | `scenario_J_action_none.log` | `action=none` ⇒ لا أمر |
| K | `scenario_K_over_max_size.log` | `position_size_usd > MaxPositionSizeUsd` ⇒ لا أمر |
| L | `scenario_L_invalid_size.log` | `position_size_usd ≤ 0` ⇒ لا أمر |
| M | `scenario_M_existing_position.log` | وجود مركز مفتوح مسبقاً ⇒ لا أمر |
| N | `scenario_N_no_atr.log` | تعذّر ATR ⇒ لا SL ⇒ **لا أمر** |
| O | `scenario_O_server_failure.log` | فشل تنفيذ من الخادم: يُسجَّل الخطأ كاملاً، ولا إعادة محاولة بنفس الشمعة |
| P | `scenario_P_volume_below_min.log` | الحجم أقل من الحد الأدنى للوسيط ⇒ لا أمر |
| Q | `scenario_Q_idempotency.log` | نفس شمعة القرار مرتين ⇒ أمر واحد فقط |
| R | `scenario_R_position_closed_report.log` | إغلاق مركز → `POST /trade-result?pnl_usd=12.5` |

> ملاحظة: قيم `PipSize/PipValue/VolumeInUnits*` في سيناريوهات التنفيذ **اصطناعية صريحة**
> (مُعرَّفة في `Program.cs`) كي تكون النتائج قابلة للحساب يدوياً؛ الـcBot نفسه لا يفترض أي
> قيمة ثابتة بل يقرأها من `Symbol` الحقيقي على المنصة.

### 3.ب — ما لم يُثبَت بعد ❌ (مطلوب منك قبل اعتماد طبقة التنفيذ)

1. صفقة حقيقية واحدة (دخول + خروج) على **حساب cTrader Demo فعلي عند وسيط**، من داخل المنصة.
2. لقطة شاشة/نص من تبويب **Positions/History** داخل cTrader تُظهر الصفقة و SL/TP المسجَّلين.
3. إثبات تقارير الإغلاق (`POST /trade-result`) من إغلاق فعلي على المنصة.

خطوات الاختبار الحقيقي المطلوب (على Demo فقط):

1. شغّل Brain API وأضف الـcBot على شارت XAUUSD (Demo).
2. اضبط `MaxPositionSizeUsd` لأدنى قيمة ممكنة (مثلاً 0.5)، ثم فعّل `ExecutionEnabled=true`.
3. لضمان ظهور فرصة دخول، يمكنك مؤقتاً خفض `DECISION_SCORE_THRESHOLD` في Brain (وثّق أي
   تعديل مؤقت وأعِده لقيمته الأصلية بعده).
4. انتظر فتح الصفقة، ثم تحقق من تبويب Positions/History والتقط اللقطة.
5. أعد `ExecutionEnabled=false` و`MaxPositionSizeUsd` لقيمتهما بعد الاختبار.

---

## 4) قيود معروفة

- `Http.Get` نداء متزامن (كما في أمثلة cTrader الرسمية)؛ عند تعطّل الخادم قد ينتظر حتى مهلة
  المنصة داخل معالج الشمعة. البديل المستقبلي: `Http.GetAsync`.
- يجب أن يكون `Symbol` مطابقاً لرمز الذهب عند وسيطك، وإلا يُوقف الـcBot برسالة واضحة.
- ATR يُحسب على `min(Bars.Count, AtrLookbackBars)` شمعة من شموع الـcBot، بينما Brain يحسبه
  على 100 شمعة من مصدر الأسعار؛ ولأن EMA مسار-اعتمادي فقد تختلف القيمة قليلاً.
- الـharness يبني الشيم فقط للتحقق خارج المنصة؛ النسخة النهائية تُبنى دائماً داخل cTrader.
