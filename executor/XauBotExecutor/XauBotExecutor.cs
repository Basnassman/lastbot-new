// =====================================================================================
//  XauBotExecutor  —  cBot لـ cTrader Automate  (C# / cAlgo.API)
// -------------------------------------------------------------------------------------
//  عند كل إغلاق شمعة M15 على رمز العملة (XAUUSD افتراضياً) يستدعي HTTP GET على
//  الـ endpoint  /decide-log-only  في Brain API، ويسجّل كل استجابة (action /
//  confidence / position_size_usd / reasons / dollar_trend_debug) في سجل cTrader.
//
//  وضعان، يفصلهما مفتاح إيقاف رئيسي (Parameter: ExecutionEnabled، القيمة الافتراضية
//  false دائماً عند أي نشر جديد):
//    * ExecutionEnabled=false  →  وضع مراقبة فقط: لا يُفتح أي أمر إطلاقاً.
//    * ExecutionEnabled=true   →  تنفيذ فعلي مشروط بكل شروط الطبقة 1 (وفي Demo فقط).
//
//  تنفيذ الصفقات (الطبقات 0..7):
//   0) مفتاح إيقاف ExecutionEnabled (منفصل تماماً عن فحص IsLive — كلاهما يجب أن يمرّ).
//   1) شروط الدخول: ExecutionEnabled=true، وIsLive=false (الفحص الموجود مسبقاً،
//      لم يُعدَّل)، وaction="buy"|"sell" فقط، وposition_size_usd>0 و≤MaxPositionSizeUsd،
//      ولا يوجد مركز مفتوح مسبقاً على الرمز من هذا الـcBot (فحص Positions الفعلية).
//   2) وقف خسارة إلزامي مرفق في نفس الأمر (ExecuteMarketOrder) — محسوب من ATR محلياً
//      من شموع الـcBot (بنفس منطق indicators.py). تعذّر حساب ATR ⇒ لا صفقة، بلا قيمة بديلة.
//   3) هدف ربح بنسبة RiskRewardRatio (افتراضي 1.5).
//   4) Lot Sizing من مواصفات الرمز الحقيقية (PipValue / PipSize / VolumeInUnitsStep /
//      Min / Max) ومسافة SL — ولا يُقرَّب الحجم للأعلى فوق المخاطرة المقصودة.
//   5) تحقق فعلي من TradeResult (ليست مجرد عدم رمي استثناء) وتسجيل كل التفاصيل.
//   6) عند إغلاق أي مركز: POST /trade-result بالربح/الخسارة الفعلية (NetProfit).
//   7) منع التكرار: وقت الشمعة نفسه مفتاح منع تكرار للأمر.
//
//  ⚠️ حساب Demo فقط. الفحص Account.IsLive=true يوقف الـcBot فوراً ولا يمكن تجاوزه.
//  أخطاء الشبكة تُسجَّل بوضوح ولا تُسقط الـcBot.
//
//  طريقة الاستخدام: انظر executor/README.md
// =====================================================================================

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Serialization;
using cAlgo.API;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    [Robot(TimeZone = TimeZones.UTC, AccessRights = AccessRights.None)]
    public class XauBotExecutor : Robot
    {
        // بادئة موحّدة لكل سطور السجل — تسهّل الفلترة في لسان/Log tab داخل cTrader.
        private const string LogPrefix = "[XAU-BOT]";

        // المدة الزمنية المستهدفة للشمعة (M15).
        private const string BarTimeframe = "M15";

        // وسم (Label) أوامر هذا الـcBot — يُستخدم لتحديد "مراكز هذا الـcBot" على الحساب
        // الفعلي (يبقى محفوظاً على الخادم فيصمد بعد إعادة التشغيل، بخلاف متغير داخلي).
        private const string OrderLabel = "XauBotExecutor";

        // ---------------------------------------------------------------------------
        // Parameters: كل ما هو قابل للتعديل يُعرَّف هنا. لا يوجد Host/Port مكتوب يدوياً
        // داخل الكود — كل نداء HTTP يُبنى من هذه القيم.
        // ---------------------------------------------------------------------------

        [Parameter("Brain API Host", DefaultValue = "127.0.0.1", Group = "Brain API")]
        public string BrainApiHost { get; set; }

        [Parameter("Brain API Port", DefaultValue = 8000, Group = "Brain API")]
        public int BrainApiPort { get; set; }

        [Parameter("Use HTTPS", DefaultValue = false, Group = "Brain API")]
        public bool UseHttps { get; set; }

        [Parameter("Base Path (optional)", DefaultValue = "", Group = "Brain API")]
        public string BrainApiBasePath { get; set; }

        [Parameter("Symbol", DefaultValue = "XAUUSD", Group = "Market")]
        public string TargetSymbol { get; set; }

        // --- الطبقة 0: مفتاح الإيقاف الرئيسي ---------------------------------------
        // يجب أن يكون false عند أي نشر جديد. لا صفقات إطلاقاً ما لم يُفعَّل يدوياً.
        [Parameter("Execution Enabled (master switch)", DefaultValue = false, Group = "Execution")]
        public bool ExecutionEnabled { get; set; }

        // --- الطبقة 1.4: سقف المخاطرة لكل صفقة (بالدولار) ---------------------------
        // القيمة الافتراضية محافظة عمداً (5$): مع حساب Demo افتراضي 100$ تساوي ~5% كحد
        // أقصى للمخاطرة لكل صفقة — ويمكن تصغيرها لأدنى حدّ للاختبارات الحيّة على Demo.
        [Parameter("Max Position Size (USD risk)", DefaultValue = 5.0, MinValue = 0.01, Group = "Execution")]
        public double MaxPositionSizeUsd { get; set; }

        // --- الطبقة 3: نسبة المخاطرة/العائد ----------------------------------------
        [Parameter("Risk/Reward Ratio", DefaultValue = 1.5, MinValue = 0.1, Group = "Execution")]
        public double RiskRewardRatio { get; set; }

        // --- الطبقة 2: مضاعف ATR لمسافة وقف الخسارة --------------------------------
        [Parameter("Stop-Loss ATR Multiplier", DefaultValue = 1.5, MinValue = 0.1, Group = "Execution")]
        public double StopLossAtrMultiplier { get; set; }

        // --- الطبقة 2: إعدادات حساب ATR محلياً -------------------------------------
        [Parameter("ATR Period", DefaultValue = 14, MinValue = 2, Group = "Execution")]
        public int AtrPeriod { get; set; }

        // عدد الشموع التي يُحسب عليها ATR — 100 يطابق عدد الشموع التي يستخدمها Brain.
        [Parameter("ATR Lookback Bars", DefaultValue = 100, MinValue = 5, Group = "Execution")]
        public int AtrLookbackBars { get; set; }

        // ---------------------------------------------------------------------------
        // حالة وقت التشغيل
        // ---------------------------------------------------------------------------
        private static readonly JsonSerializerOptions JsonOptions = new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
        };

        private Bars _m15Bars;
        private Symbol _targetSymbol;
        private string _resolvedSymbolName;

        // علامة داخلية: لا يُسمح بأي نداء HTTP قبل اجتياز فحص الحماية.
        private bool _guardPassed;

        private long _successCount;
        private long _failureCount;

        // الطبقة 7: وقت شمعة آخر أمر أُرسل (نجح أو فشل) — مفتاح منع التكرار.
        private DateTime? _lastExecutionBarTimeUtc;

        // ===========================================================================
        //  دورة الحياة
        // ===========================================================================
        protected override void OnStart()
        {
            _guardPassed = false;
            _successCount = 0;
            _failureCount = 0;
            _lastExecutionBarTimeUtc = null;

            Print($"{LogPrefix} =================================================================");
            Print($"{LogPrefix} بدء تشغيل XauBotExecutor.");
            Print($"{LogPrefix} RunningMode={RunningMode} | IsBacktesting={IsBacktesting}");
            Print($"{LogPrefix} الحساب: النوع={Account.AccountType} | الرقم={Account.Number} | " +
                  $"الوسيط={Account.BrokerName} | IsLive={Account.IsLive}");

            // سطر صريح في كل تشغيل لحالة مفتاح التنفيذ (الطبقة 0).
            PrintExecutionMode();

            // =======================================================================
            //  فحص إلزامي غير قابل للتجاوز: حساب Demo فقط.
            //  Account.IsLive == true  =>  الحساب حقيقي  =>  أوقف فوراً ولا تكمل إطلاقاً.
            //  لا يوجد Parameter لتخطّي هذا الفحص، ولا يُقرأ من أي إعداد خارجي.
            // =======================================================================
            if (Account.IsLive)
            {
                Print($"{LogPrefix} ❌ رفض التشغيل: الحساب الحالي حساب حقيقي (IsLive=true)، " +
                      $"رقم الحساب={Account.Number}، الوسيط={Account.BrokerName}.");
                Print($"{LogPrefix} ❌ هذا الـcBot يعمل على حساب Demo فقط. يتم إيقافه الآن ولن ينفّذ أي شيء.");
                Stop();
                return;
            }

            Print($"{LogPrefix} ✅ فحص الحماية: الحساب Demo (IsLive=false) — تم اجتيازه. متابعة التشغيل.");

            // -----------------------------------------------------------------------
            // حلّ رمز العملة (يعطي رسالة واضحة إن لم يكن الرمز موجوداً عند الوسيط).
            // -----------------------------------------------------------------------
            try
            {
                var symbol = string.IsNullOrWhiteSpace(TargetSymbol) ? Symbol : Symbols.GetSymbol(TargetSymbol);
                _targetSymbol = symbol;
                _resolvedSymbolName = symbol.Name;
            }
            catch (Exception ex)
            {
                Print($"{LogPrefix} ❌ تعذّر إيجاد الرمز '{TargetSymbol}' عند هذا الوسيط: {ex.Message}");
                Print($"{LogPrefix} ❌ يتم إيقاف الـcBot. تحقّق من إعداد Parameter «Symbol».");
                Stop();
                return;
            }

            // -----------------------------------------------------------------------
            // الاشتراك في إغلاق شمعة M15 للرمز المحدد — بغض النظر عن الفريم المعروض
            // على الشارت. الحدث BarClosed يُستدعى للشمعة المغلقة (السابقة للجديدة).
            // -----------------------------------------------------------------------
            try
            {
                _m15Bars = MarketData.GetBars(TimeFrame.Minute15, _resolvedSymbolName);
                _m15Bars.BarClosed += OnM15BarClosed;
            }
            catch (Exception ex)
            {
                Print($"{LogPrefix} ❌ تعذّر الاشتراك في بيانات M15 للرمز '{_resolvedSymbolName}': {ex.Message}");
                Stop();
                return;
            }

            // -----------------------------------------------------------------------
            // الطبقة 6: الاشتراك في إغلاق المراكز على الحساب (سواء أغلقه TP/SL أو يدوياً)
            // لإبلاغ Brain بالربح/الخسارة الفعلية (تفعيل قاطع الدائرة اليومي).
            // -----------------------------------------------------------------------
            try
            {
                if (Positions != null)
                {
                    Positions.Closed += OnPositionClosed;
                }
                else
                {
                    Print($"{LogPrefix} ⚠️ Positions غير متاحة — لن تُبلَّغ نتائج الإغلاق إلى Brain.");
                }
            }
            catch (Exception ex)
            {
                Print($"{LogPrefix} ⚠️ تعذّر الاشتراك في حدث إغلاق المراكز: {ex.Message}");
            }

            _guardPassed = true;

            Print($"{LogPrefix} تم الاشتراك في إغلاق شمعة M15 للرمز: {_resolvedSymbolName}");
            Print($"{LogPrefix} Endpoint: {BuildRequestUrlUnsigned()}");
            Print($"{LogPrefix} جاهز. سيُستدعى Brain API عند كل إغلاق شمعة M15.");
            Print($"{LogPrefix} =================================================================");
        }

        protected override void OnStop()
        {
            if (_m15Bars != null)
            {
                try
                {
                    _m15Bars.BarClosed -= OnM15BarClosed;
                }
                catch
                {
                    // لا شيء — إلغاء الاشتراك لا يجب أن يسبّب استثناءً عند الإيقاف.
                }
            }

            try
            {
                if (Positions != null)
                {
                    Positions.Closed -= OnPositionClosed;
                }
            }
            catch
            {
                // لا شيء.
            }

            Print($"{LogPrefix} توقّف الـcBot. إجمالي استدعاءات Brain API الناجحة={_successCount} | الفاشلة={_failureCount}.");
            Print($"{LogPrefix} =================================================================");
        }

        // استثناء غير متوقّع: نسجّله ولا نسمح له بإسقاط الـcBot.
        protected override void OnException(Exception exception)
        {
            Print($"{LogPrefix} ⚠️ استثناء غير متوقّع (تم التقاطه، الـcBot يستمر): " +
                  $"{exception.GetType().Name}: {exception.Message}");
        }

        // ===========================================================================
        //  معالج إغلاق شمعة M15
        // ===========================================================================
        private void OnM15BarClosed(BarClosedEventArgs args)
        {
            // إعادة تحقّق وقائية: لو تغيّر الحساب إلى حقيقي أثناء التشغيل، أوقف فوراً.
            if (Account.IsLive)
            {
                Print($"{LogPrefix} ❌ تم رصد حساب حقيقي (IsLive=true) أثناء التشغيل. إيقاف فوري.");
                Stop();
                return;
            }

            if (!_guardPassed)
            {
                return;
            }

            var url = BuildRequestUrl();
            Print($"{LogPrefix} ────────────────────────────────────────────────────────────────");
            Print($"{LogPrefix} 🔔 إغلاق شمعة M15 على {_resolvedSymbolName} @ {TimeInUtc:yyyy-MM-dd HH:mm:ss} UTC");
            Print($"{LogPrefix}    GET {url}");

            // -----------------------------------------------------------------------
            // نداء HTTP — أي فشل يُسجَّل بوضوح ولا يُسقط الـcBot. إعادة المحاولة تلقائية
            // عند إغلاق الشمعة التالية (لأننا لا نستدعي إلا عند BarClosed).
            // -----------------------------------------------------------------------
            HttpResponse response;
            try
            {
                response = Http.Get(url);
            }
            catch (Exception ex)
            {
                _failureCount++;
                Print($"{LogPrefix} ⚠️ فشل شبكة أثناء استدعاء Brain API: {ex.GetType().Name}: {ex.Message}");
                Print($"{LogPrefix}    الـcBot مستمر. ستُعاد المحاولة عند إغلاق شمعة M15 التالية.");
                return;
            }

            if (response == null)
            {
                _failureCount++;
                Print($"{LogPrefix} ⚠️ استجابة فارغة (null) من Brain API. ستُعاد المحاولة عند الشمعة التالية.");
                return;
            }

            if (response.Exception != null)
            {
                _failureCount++;
                Print($"{LogPrefix} ⚠️ خطأ HTTP من Brain API: {response.Exception.Message}");
                Print($"{LogPrefix}    ستُعاد المحاولة عند إغلاق شمعة M15 التالية.");
                return;
            }

            if (!response.IsSuccessful)
            {
                _failureCount++;
                Print($"{LogPrefix} ⚠️ Brain API أعاد رمز حالة غير ناجح: HTTP {response.StatusCode}.");
                Print($"{LogPrefix}    ستُعاد المحاولة عند إغلاق شمعة M15 التالية.");
                return;
            }

            BrainDecision decision;
            try
            {
                decision = JsonSerializer.Deserialize<BrainDecision>(response.Body, JsonOptions);
            }
            catch (Exception ex)
            {
                _failureCount++;
                Print($"{LogPrefix} ⚠️ تعذّر تحليل JSON القادم من Brain API: {ex.Message}");
                Print($"{LogPrefix}    نص الاستجابة الخام: {Truncate(response.Body, 500)}");
                return;
            }

            if (decision == null)
            {
                _failureCount++;
                Print($"{LogPrefix} ⚠️ نتيجة تحليل JSON فارغة. ستُعاد المحاولة عند الشمعة التالية.");
                return;
            }

            _successCount++;
            PrintDecision(decision);

            // بعد تسجيل القرار: تقييم التنفيذ الفعلي وفق الطبقات 0..7.
            TryExecuteDecision(decision);
        }

        // ===========================================================================
        //  تسجيل القرار
        // ===========================================================================
        private void PrintDecision(BrainDecision d)
        {
            Print($"{LogPrefix} ✅ قرار مُستلَم (نجاح #{_successCount}) | mode={d.Mode} | " +
                  $"action={d.Action} | confidence={Fmt(d.Confidence)} | " +
                  $"position_size_usd={Fmt(d.PositionSizeUsd)}");

            if (d.Reasons != null && d.Reasons.Count > 0)
            {
                Print($"{LogPrefix}    reasons ({d.Reasons.Count}):");
                foreach (var reason in d.Reasons)
                {
                    Print($"{LogPrefix}      - {reason}");
                }
            }
            else
            {
                Print($"{LogPrefix}    reasons: (لا يوجد)");
            }

            if (d.DollarTrendDebug.HasValue &&
                d.DollarTrendDebug.Value.ValueKind != JsonValueKind.Null)
            {
                Print($"{LogPrefix}    dollar_trend_debug: {d.DollarTrendDebug.Value.GetRawText()}");
            }
            else
            {
                Print($"{LogPrefix}    dollar_trend_debug: null");
            }

            if (string.Equals(d.Action, "buy", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(d.Action, "sell", StringComparison.OrdinalIgnoreCase))
            {
                if (ExecutionEnabled)
                {
                    Print($"{LogPrefix}    ℹ️ القرار يدعو إلى '{d.Action}' — سيُقيَّم للتنفيذ وفق شروط الطبقة 1.");
                }
                else
                {
                    Print($"{LogPrefix}    ℹ️ القرار يدعو إلى '{d.Action}' — لكن التنفيذ معطّل " +
                          $"(ExecutionEnabled=false). وضع مراقبة فقط: لا يُفتح أي أمر.");
                }
            }
        }

        // ===========================================================================
        //  طبقات التنفيذ (0..7)
        // ===========================================================================
        private void TryExecuteDecision(BrainDecision decision)
        {
            // --- الطبقة 0: مفتاح الإيقاف الرئيسي -----------------------------------
            if (!ExecutionEnabled)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: ExecutionEnabled=false — وضع مراقبة فقط (لا يُفتح أي أمر).");
                return;
            }

            // --- الطبقة 1.2: إعادة تحقق من IsLive (منفصل عن مفتاح التنفيذ) ---------
            if (Account.IsLive)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: الحساب حقيقي (IsLive=true).");
                return;
            }

            // --- الطبقة 1.3: action يجب أن يكون buy أو sell حصراً ------------------
            var action = decision.Action;
            var isBuy = string.Equals(action, "buy", StringComparison.OrdinalIgnoreCase);
            var isSell = string.Equals(action, "sell", StringComparison.OrdinalIgnoreCase);
            if (!isBuy && !isSell)
            {
                Print($"{LogPrefix} [تنفيذ] لا أمر: action='{action ?? "(null)"}' ليس buy/sell.");
                return;
            }

            // --- الطبقة 1.4: position_size_usd صالح وضمن السقف ---------------------
            var sizeUsd = decision.PositionSizeUsd;
            if (double.IsNaN(sizeUsd) || double.IsInfinity(sizeUsd) || sizeUsd <= 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: position_size_usd غير صالح ({Fmt(sizeUsd)}).");
                return;
            }
            if (sizeUsd > MaxPositionSizeUsd)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: position_size_usd={Fmt(sizeUsd)} " +
                      $"يتجاوز MaxPositionSizeUsd={Fmt(MaxPositionSizeUsd)}.");
                return;
            }

            // --- الطبقة 7: منع التكرار لنفس شمعة القرار ---------------------------
            //     يُفحَص أولاً: لو استُدعيت المعالجة مرتين لنفس الشمعة (إعادة اتصال مثلاً)
            //     لا يُعاد إرسال أمر للشمعة نفسها حتى لو لم يبقَ مركز مفتوح.
            var barTime = _m15Bars.LastBar != null ? _m15Bars.LastBar.OpenTime : TimeInUtc;
            if (_lastExecutionBarTimeUtc.HasValue && _lastExecutionBarTimeUtc.Value == barTime)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: تمت محاولة أمر لهذه الشمعة " +
                      $"({barTime:yyyy-MM-dd HH:mm} UTC) مسبقاً — منع تكرار.");
                return;
            }

            // --- الطبقة 1.5: لا مركز مفتوح مسبقاً على الرمز من هذا الـcBot ----------
            //     يُفحَص من Positions الفعلية على الحساب (لا من متغير داخلي) — يصمد
            //     بعد إعادة التشغيل لأن الوسم (Label) محفوظ على الخادم.
            var openPositions = Positions != null ? Positions.FindAll(OrderLabel, _resolvedSymbolName) : null;
            if (openPositions != null && openPositions.Length > 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: يوجد مركز مفتوح مسبقاً على {_resolvedSymbolName} " +
                      $"(Id={openPositions[0].Id}). منع فتح صفقة ثانية فوق صفقة قائمة.");
                return;
            }

            // --- الطبقة 2: وقف الخسارة (إلزامي) من ATR محلياً ---------------------
            var atr = ComputeAtr();
            if (double.IsNaN(atr) || atr <= 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: تعذّر حساب ATR صالح (القيمة={Fmt(atr)}) — " +
                      $"لا وقف خسارة موثوق ⇒ لا صفقة (لا قيمة افتراضية بديلة).");
                return;
            }

            var symbol = _targetSymbol;
            if (symbol == null || symbol.PipSize <= 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: مواصفات الرمز غير متاحة (PipSize<=0).");
                return;
            }

            var slPriceDistance = atr * StopLossAtrMultiplier;
            var slPips = slPriceDistance / symbol.PipSize;
            if (double.IsNaN(slPips) || double.IsInfinity(slPips) || slPips <= 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: مسافة وقف الخسارة غير صالحة ({Fmt(slPips)} pips).");
                return;
            }

            // --- الطبقة 3: هدف الربح بنسبة RR ------------------------------------
            var tpPips = slPips * RiskRewardRatio;
            if (double.IsNaN(tpPips) || double.IsInfinity(tpPips) || tpPips <= 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: مسافة هدف الربح غير صالحة ({Fmt(tpPips)} pips).");
                return;
            }

            // --- الطبقة 4: تحويل المخاطرة بالدولار إلى حجم وحدات -------------------
            var pipValuePerUnit = symbol.PipValue;
            if (double.IsNaN(pipValuePerUnit) || pipValuePerUnit <= 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: PipValue غير صالح ({Fmt(pipValuePerUnit)}).");
                return;
            }

            var step = symbol.VolumeInUnitsStep;
            if (double.IsNaN(step) || step <= 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: VolumeInUnitsStep غير صالح ({Fmt(step)}).");
                return;
            }

            // الحجم الخام = المخاطرة$ / (مسافة SL بالنقاط × قيمة النقطة لكل وحدة).
            var rawVolume = sizeUsd / (slPips * pipValuePerUnit);
            if (double.IsNaN(rawVolume) || double.IsInfinity(rawVolume) || rawVolume <= 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: الحجم المحسوب غير صالح ({Fmt(rawVolume)}).");
                return;
            }

            // التقريب للأسفل لأقرب خطوة لوت — لا نتجاوز المخاطرة المقصودة أبداً.
            var volume = Math.Floor((rawVolume / step) + 1e-9) * step;

            if (rawVolume < symbol.VolumeInUnitsMin)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: الحجم المحسوب ({Fmt(rawVolume)}) أقل من الحد الأدنى " +
                      $"للوسيط ({Fmt(symbol.VolumeInUnitsMin)}) — لا تقريب للأعلى يتجاوز المخاطرة المقصودة.");
                return;
            }
            if (volume < symbol.VolumeInUnitsMin)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: الحجم بعد التقريب ({Fmt(volume)}) أقل من الحد الأدنى " +
                      $"({Fmt(symbol.VolumeInUnitsMin)}).");
                return;
            }
            if (volume > symbol.VolumeInUnitsMax)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: الحجم ({Fmt(volume)}) يتجاوز الحد الأقصى " +
                      $"({Fmt(symbol.VolumeInUnitsMax)}).");
                return;
            }

            // --- الطبقة 5: إرسال أمر سوقي مع SL/TP في نفس الاستدعاء ----------------
            var tradeType = isBuy ? TradeType.Buy : TradeType.Sell;
            var comment = $"XauBot {action}";
            var intendedRiskUsd = volume * slPips * pipValuePerUnit;

            Print($"{LogPrefix} [تنفيذ] إرسال أمر {action.ToUpperInvariant()}: volume={Fmt(volume)} وحدة | " +
                  $"SL={Fmt(slPips)} pips | TP={Fmt(tpPips)} pips | RR={Fmt(RiskRewardRatio)} | " +
                  $"المخاطرة المقصودة≈${Fmt(intendedRiskUsd)} (القيمة من Brain=${Fmt(sizeUsd)}، السقف=${Fmt(MaxPositionSizeUsd)})");
            Print($"{LogPrefix} [تنفيذ] ATR={Fmt(atr)} | مضاعف SL={Fmt(StopLossAtrMultiplier)} | PipSize={Fmt(symbol.PipSize)} | PipValue/وحدة={Fmt(pipValuePerUnit)}");

            // نعلّم الشمعة قبل الإرسال: أي فشل لا يُعاد تلقائياً بنفس الشمعة (الطبقة 5 و7).
            _lastExecutionBarTimeUtc = barTime;

            TradeResult result;
            try
            {
                result = ExecuteMarketOrder(tradeType, _resolvedSymbolName, volume, OrderLabel, slPips, tpPips, comment, false);
            }
            catch (Exception ex)
            {
                Print($"{LogPrefix} [تنفيذ] ❌ استثناء أثناء إرسال الأمر: {ex.GetType().Name}: {ex.Message}");
                return;
            }

            if (result == null)
            {
                Print($"{LogPrefix} [تنفيذ] ❌ فشل: لم تُرجِع المنصة أي TradeResult.");
                return;
            }

            if (!result.IsSuccessful)
            {
                Print($"{LogPrefix} [تنفيذ] ❌ فشل تنفيذ الأمر من المنصة: Error={result.Error?.ToString() ?? "—"} | " +
                      $"الوصف: {result.ToString()}");
                Print($"{LogPrefix} [تنفيذ] لن تُعاد المحاولة تلقائياً بنفس الشمعة — انتظار شمعة جديدة بقرار جديد.");
                return;
            }

            var pos = result.Position;
            if (pos == null)
            {
                Print($"{LogPrefix} [تنفيذ] ⚠️ TradeResult ناجح لكن بلا Position — لا يمكن تأكيد التفاصيل.");
                return;
            }

            Print($"{LogPrefix} [تنفيذ] ✅ فُتح المركز #{pos.Id} على {pos.SymbolName}:");
            Print($"{LogPrefix}    السعر الفعلي للتنفيذ (قد يختلف عن اللحظي بسبب الانزلاق): {Fmt(pos.EntryPrice)}");
            Print($"{LogPrefix}    الحجم الفعلي: {Fmt(pos.VolumeInUnits)} وحدة");
            Print($"{LogPrefix}    SL الفعلي على الخادم: {FmtNullable(pos.StopLoss)} | TP الفعلي على الخادم: {FmtNullable(pos.TakeProfit)}");
            Print($"{LogPrefix}    مفتاح منع التكرار (وقت الشمعة): {barTime:yyyy-MM-dd HH:mm} UTC");
        }

        // ===========================================================================
        //  حساب ATR محلياً من شموع الـcBot — بنفس منطق brain/analysis/indicators.py
        //  اختلاف موثَّق: pandas (في Brain) يحسب على 100 شمعة من مصدر الأسعار، بينما
        //  هنا نحسب على آخر min(Bars.Count, AtrLookbackBars) شمعة من شموع الـcBot.
        //  بما أن ATR_EMA مسار-اعتمادي (path-dependent)، قد تختلف القيمة قليلاً عن
        //  قيمة Brain. ATR غير صالح (>0) ⇒ لا صفقة.
        // ===========================================================================
        private double ComputeAtr()
        {
            var bars = _m15Bars;
            if (bars == null)
            {
                return double.NaN;
            }

            var highs = bars.HighPrices;
            var lows = bars.LowPrices;
            var closes = bars.ClosePrices;
            var count = bars.Count;

            if (highs == null || lows == null || closes == null) return double.NaN;
            if (count <= 0) return double.NaN;
            if (highs.Count < count || lows.Count < count || closes.Count < count) return double.NaN;

            var lookback = Math.Min(count, AtrLookbackBars);
            if (lookback < AtrPeriod + 1) return double.NaN;

            var start = count - lookback;
            var atr = 0.0;
            var first = true;

            for (var i = start; i < count; i++)
            {
                double trueRange;
                if (i == 0)
                {
                    trueRange = highs[0] - lows[0];
                }
                else
                {
                    var prevClose = closes[i - 1];
                    trueRange = Math.Max(
                        highs[i] - lows[i],
                        Math.Max(Math.Abs(highs[i] - prevClose), Math.Abs(lows[i] - prevClose)));
                }

                if (first)
                {
                    atr = trueRange;
                    first = false;
                }
                else
                {
                    // Wilder smoothing: alpha = 1/period (مطابق لـ ewm(alpha=1/period, adjust=False)).
                    atr += (trueRange - atr) / AtrPeriod;
                }
            }

            return atr;
        }

        // ===========================================================================
        //  الطبقة 6: إبلاغ Brain بنتيجة الإغلاق الفعلية
        // ===========================================================================
        private void OnPositionClosed(PositionClosedEventArgs args)
        {
            try
            {
                var pos = args != null ? args.Position : null;
                if (pos == null)
                {
                    return;
                }

                // NetProfit = الربح/الخسارة الفعلي بالدولار (بعد العمولات والسواب).
                var pnl = pos.NetProfit;
                Print($"{LogPrefix} [إغلاق] أُغلق المركز #{pos.Id} على {pos.SymbolName} | " +
                      $"السبب={args.Reason} | NetProfit=${Fmt(pnl)} | GrossProfit=${Fmt(pos.GrossProfit)}");
                ReportTradeResult(pnl);
            }
            catch (Exception ex)
            {
                Print($"{LogPrefix} ⚠️ خطأ في معالجة إغلاق المركز: {ex.GetType().Name}: {ex.Message}");
            }
        }

        private void ReportTradeResult(double pnlUsd)
        {
            var url = BuildTradeResultUrl(pnlUsd);
            Print($"{LogPrefix} [إغلاق] POST {url}");
            try
            {
                // POST عبر واجهة cTrader الموثّقة: HttpRequest + Http.Send (لا يوجد Http.Post مباشر).
                var request = new HttpRequest(new Uri(url))
                {
                    Method = HttpMethod.Post,
                    Body = string.Empty,
                };
                var response = Http.Send(request);

                if (response == null)
                {
                    Print($"{LogPrefix} [إغلاق] ⚠️ استجابة فارغة من /trade-result — لم يُحدَّث قاطع الدائرة.");
                    return;
                }
                if (response.Exception != null)
                {
                    Print($"{LogPrefix} [إغلاق] ⚠️ فشل /trade-result: {response.Exception.Message}");
                    return;
                }
                if (!response.IsSuccessful)
                {
                    Print($"{LogPrefix} [إغلاق] ⚠️ /trade-result أعاد HTTP {response.StatusCode}.");
                    return;
                }

                Print($"{LogPrefix} [إغلاق] ✅ أُرسل PnL الفعلي (${Fmt(pnlUsd)}) إلى Brain /trade-result — " +
                      $"تحديث قاطع الدائرة اليومي في RiskManager.");
            }
            catch (Exception ex)
            {
                Print($"{LogPrefix} [إغلاق] ⚠️ استثناء أثناء POST /trade-result: {ex.GetType().Name}: {ex.Message}");
            }
        }

        // ===========================================================================
        //  أدوات مساعدة
        // ===========================================================================
        private void PrintExecutionMode()
        {
            if (ExecutionEnabled)
            {
                Print($"{LogPrefix} ExecutionEnabled=true — التنفيذ الفعلي مُفعَّل.");
            }
            else
            {
                Print($"{LogPrefix} ExecutionEnabled=false — وضع مراقبة فقط.");
            }
        }

        private string NormalizeBasePath()
        {
            var basePath = (BrainApiBasePath ?? string.Empty).Trim();
            if (basePath.StartsWith("/", StringComparison.Ordinal))
            {
                basePath = basePath.Substring(1);
            }
            if (basePath.Length > 0 && !basePath.EndsWith("/", StringComparison.Ordinal))
            {
                basePath += "/";
            }
            return basePath;
        }

        private string BuildRequestUrl()
        {
            var scheme = UseHttps ? "https" : "http";
            return string.Format(
                CultureInfo.InvariantCulture,
                "{0}://{1}:{2}/{3}decide-log-only?symbol={4}&timeframe={5}",
                scheme,
                BrainApiHost,
                BrainApiPort,
                NormalizeBasePath(),
                Uri.EscapeDataString(_resolvedSymbolName ?? "XAUUSD"),
                BarTimeframe);
        }

        // نسخة بلا رمز/فريم — تُستخدم في سطر الإعداد الأولي قبل حلّ الرمز.
        private string BuildRequestUrlUnsigned()
        {
            var scheme = UseHttps ? "https" : "http";
            return string.Format(
                CultureInfo.InvariantCulture,
                "{0}://{1}:{2}/{3}decide-log-only?symbol=<symbol>&timeframe={4}",
                scheme,
                BrainApiHost,
                BrainApiPort,
                NormalizeBasePath(),
                BarTimeframe);
        }

        private string BuildTradeResultUrl(double pnlUsd)
        {
            var scheme = UseHttps ? "https" : "http";
            var pnl = pnlUsd.ToString("0.####", CultureInfo.InvariantCulture);
            return string.Format(
                CultureInfo.InvariantCulture,
                "{0}://{1}:{2}/{3}trade-result?pnl_usd={4}",
                scheme,
                BrainApiHost,
                BrainApiPort,
                NormalizeBasePath(),
                pnl);
        }

        private static string Fmt(double value)
        {
            return value.ToString("0.###", CultureInfo.InvariantCulture);
        }

        private static string FmtNullable(double? value)
        {
            return value.HasValue ? value.Value.ToString("0.#####", CultureInfo.InvariantCulture) : "(غير محدّد)";
        }

        private static string Truncate(string text, int max)
        {
            if (string.IsNullOrEmpty(text) || text.Length <= max)
            {
                return text;
            }
            return text.Substring(0, max) + "…(مقطوع)";
        }

        // ===========================================================================
        //  شكل استجابة /decide-log-only
        // ===========================================================================
        private sealed class BrainDecision
        {
            [JsonPropertyName("mode")]
            public string Mode { get; set; }

            [JsonPropertyName("action")]
            public string Action { get; set; }

            [JsonPropertyName("position_size_usd")]
            public double PositionSizeUsd { get; set; }

            [JsonPropertyName("confidence")]
            public double Confidence { get; set; }

            [JsonPropertyName("reasons")]
            public List<string> Reasons { get; set; }

            [JsonPropertyName("dollar_trend_debug")]
            public JsonElement? DollarTrendDebug { get; set; }
        }
    }
}
