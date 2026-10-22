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
//  تقسيم الصفقة وحماية الأرباح (الميزة المعتمدة — بعد الطبقات 0..7):
//   أ) القرار الأصلي محفوظ كما هو: الاتجاه، الثقة، إجمالي الحجم، مسافة SL الأصلية،
//      ومسافة TP الأصلية (100%). لا يُعاد حساب أي منها هنا.
//   ب) يُقسَّم إجمالي الحجم إلى ثلاثة مراكز مستقلة (Hedged فقط) بأهداف:
//      A = 20% و B = 50% و C = 100% من مسافة TP الأصلية. مجموع الأحجام = الإجمالي
//      بالضبط بعد تطبيع خطوة الحجم، وبلا تقريب أو حذف أو تجاوز.
//   ج) بعد تأكيد إغلاق Position A عند هدفه (حدث Positions.Closed + السبب=TakeProfit)
//      يُنقل وقف B و C إلى التعادل الصافي بعد التكاليف (عمولات + سواب؛ الانتشار محتسب
//      في سعر الدخول) مع الحفاظ على هدفيهما. لا يُرخّى أي وقف قائم أبداً.
//   د) كل التعديلات مقيّدة بمعرّفات الخادم (Position.Id) والوسم، وليست بالتعليق.
//      التكرار والمحاولات المرفوضة وإعادة التشغيل تُسجَّل ولا تُنتج أوامر مكرّرة.
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

        // أدوار المراكز الثلاثة في مجموعة واحدة، ضمن وسم المركز نفسه: Label = OrderLabel-token-role
        // الوسم محفوظ على الخادم فلا يعتمد الربط على نص التعليق (comment) ولا على الرمز فقط.
        // الفاصل '-' آمن في وسوم/تعليقات الوسيط (لا رموز خاصة).
        private const char LabelSeparator = '-';

        private const string RoleA = "A";
        private const string RoleB = "B";
        private const string RoleC = "C";

        // نسب هدف الربح من مسافة TP الأصلية (لا تُعاد المسافة الأصلية نفسها أبداً).
        private const double TargetFractionA = 0.20;
        private const double TargetFractionB = 0.50;
        private const double TargetFractionC = 1.00;

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

        // مجموعة (3 مراكز) لكل قرار تنفيذي — تُستعاد من المراكز المفتوحة عند إعادة التشغيل.
        private sealed class TradeGroup
        {
            public string Token;
            public TradeType TradeType;
            public int IdA;
            public int IdB;
            public int IdC;
            public bool ProtectionDone;
            public bool ProtectionFailed;
            public DateTime CreatedUtc;
        }

        private readonly List<TradeGroup> _groups = new List<TradeGroup>();

        // معرّفات المراكز التي عولج إغلاقها — يمنع تطبيق الحماية مرتين عند تكرار الأحداث.
        private readonly HashSet<int> _processedClosures = new HashSet<int>();

        private enum ProtectionOutcome
        {
            Applied,
            AlreadyProtected,
            NotOpen,
            Failed,
        }

        // ===========================================================================
        //  دورة الحياة
        // ===========================================================================
        protected override void OnStart()
        {
            _guardPassed = false;
            _successCount = 0;
            _failureCount = 0;
            _lastExecutionBarTimeUtc = null;
            _groups.Clear();
            _processedClosures.Clear();

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

            // -----------------------------------------------------------------------
            // إعادة التشغيل: استعادة مجموعات المراكز المفتوحة من الوسوم المحفوظة على الخادم.
            // لا تُفتح أي أوامر هنا ولا تُعدّل أي أوقاف — قراءة واستعادة حالة فقط.
            // -----------------------------------------------------------------------
            try
            {
                AdoptExistingGroups();
            }
            catch (Exception ex)
            {
                Print($"{LogPrefix} ⚠️ تعذّرت استعادة المراكز المفتوحة بعد إعادة التشغيل: " +
                      $"{ex.GetType().Name}: {ex.Message}");
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
            //     بعد التقسيم: أيٌّ من مراكز المجموعة (A/B/C) يمنع قراراً جديداً.
            var openPositions = GetBotOwnedPositions();
            if (openPositions.Count > 0)
            {
                var ids = new List<string>();
                foreach (var openPosition in openPositions)
                {
                    ids.Add("#" + openPosition.Id);
                }

                Print($"{LogPrefix} [تنفيذ] مرفوض: يوجد مركز مفتوح مسبقاً على {_resolvedSymbolName} " +
                      $"(Ids={string.Join(", ", ids.ToArray())}). منع فتح صفقة ثانية فوق صفقة قائمة.");
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
            var minVolume = symbol.VolumeInUnitsMin;
            var maxVolume = symbol.VolumeInUnitsMax;

            if (double.IsNaN(step) || step <= 0 ||
                double.IsNaN(minVolume) || minVolume <= 0 ||
                double.IsNaN(maxVolume) || maxVolume < minVolume)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: مواصفات حجم الرمز غير صالحة. " +
                      $"Min={Fmt(minVolume)} Step={Fmt(step)} Max={Fmt(maxVolume)}.");
                return;
            }

            // الحجم الخام = المخاطرة بالدولار / (مسافة SL بالنقاط × قيمة النقطة لكل وحدة).
            var rawVolume = sizeUsd / (slPips * pipValuePerUnit);
            if (double.IsNaN(rawVolume) || double.IsInfinity(rawVolume) || rawVolume <= 0)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: الحجم المحسوب غير صالح ({Fmt(rawVolume)}).");
                return;
            }

            // سجّل خصائص الرمز لتشخيص اختلاف الوحدات لدى الوسيط.
            var minRiskUsd = minVolume * slPips * pipValuePerUnit;
            Print($"{LogPrefix} [حجم] الرمز={symbol.Name} | PipSize={Fmt(symbol.PipSize)} | " +
                  $"PipValue={Fmt(pipValuePerUnit)} | SL={Fmt(slPips)} pips | " +
                  $"RawVolume={Fmt(rawVolume)} | Min={Fmt(minVolume)} | Step={Fmt(step)} | " +
                  $"Max={Fmt(maxVolume)} | المخاطرة المقدرة عند الحد الأدنى≈${Fmt(minRiskUsd)}.");

            // إذا كان الحد الأدنى يتجاوز ميزانية المخاطرة، لا نفتح الصفقة ولا نرفع الحجم تلقائياً.
            if (rawVolume < minVolume)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض بأمان: الحجم الخام ({Fmt(rawVolume)}) أقل من الحد الأدنى " +
                      $"({Fmt(minVolume)}). المخاطرة المقدرة عند الحد الأدنى=${Fmt(minRiskUsd)} " +
                      $"مقابل ميزانية=${Fmt(sizeUsd)}. تحقّق من وحدات الحجم وقيمة PipValue لدى الوسيط.");
                return;
            }

            // التقريب للأسفل إلى خطوة الحجم حتى لا يتجاوز الحجم المخاطرة المحسوبة.
            var volume = Math.Floor((rawVolume / step) + 1e-9) * step;

            if (volume < minVolume)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض بأمان: الحجم بعد التقريب ({Fmt(volume)}) أقل من الحد الأدنى " +
                      $"({Fmt(minVolume)}).");
                return;
            }
            if (volume > maxVolume)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: الحجم ({Fmt(volume)}) يتجاوز الحد الأقصى " +
                      $"({Fmt(maxVolume)}).");
                return;
            }

            // لا ترسل الحجم إلا بعد تطبيعه والتحقق من أنه لا يتجاوز ميزانية المخاطرة.
            var normalizedVolume = symbol.NormalizeVolumeInUnits(volume, RoundingMode.Down);
            if (normalizedVolume < minVolume || normalizedVolume > maxVolume)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض: الحجم المطبع ({Fmt(normalizedVolume)}) خارج حدود الرمز.");
                return;
            }

            var normalizedRiskUsd = normalizedVolume * slPips * pipValuePerUnit;
            if (normalizedRiskUsd > sizeUsd + 1e-8)
            {
                Print($"{LogPrefix} [تنفيذ] مرفوض بأمان: المخاطرة المقدرة بعد التطبيع " +
                      $"(${Fmt(normalizedRiskUsd)}) تتجاوز الميزانية (${Fmt(sizeUsd)}).");
                return;
            }
            volume = normalizedVolume;

            // --- الطبقة 5: إرسال ثلاثة أوامر سوقية (كل منها SL/TP في نفس الاستدعاء) ---
            var tradeType = isBuy ? TradeType.Buy : TradeType.Sell;
            var intendedRiskUsd = volume * slPips * pipValuePerUnit;

            Print($"{LogPrefix} [تنفيذ] إجمالي القرار {action.ToUpperInvariant()}: volume={Fmt(volume)} وحدة | " +
                  $"SL={Fmt(slPips)} pips (بلا تغيير) | TP الأصلي={Fmt(tpPips)} pips (100%) | RR={Fmt(RiskRewardRatio)} | " +
                  $"المخاطرة المقصودة≈${Fmt(intendedRiskUsd)} (القيمة من Brain=${Fmt(sizeUsd)}، السقف=${Fmt(MaxPositionSizeUsd)})");
            Print($"{LogPrefix} [تنفيذ] ATR={Fmt(atr)} | مضاعف SL={Fmt(StopLossAtrMultiplier)} | PipSize={Fmt(symbol.PipSize)} | PipValue/وحدة={Fmt(pipValuePerUnit)}");

            // نعلّم الشمعة قبل الإرسال: أي فشل لا يُعاد تلقائياً بنفس الشمعة (الطبقة 5 و7).
            _lastExecutionBarTimeUtc = barTime;

            PlaceSplitOrders(tradeType, action, volume, slPips, tpPips, barTime);
        }

        // ===========================================================================
        //  تقسيم القرار إلى ثلاثة مراكز مستقلة (A=20%، B=50%، C=100% من مسافة TP)
        // ---------------------------------------------------------------------------
        //  الاتجاه وإجمالي الحجم ومسافة SL ومسافة TP الأصلية تُمرَّر كما حُسبت أعلاه بلا
        //  أي تعديل. التقسيم يوزّع *الحجم نفسه* فقط، ويستعمل نفس اصطلاح pips القائم.
        // ===========================================================================
        private void PlaceSplitOrders(
            TradeType tradeType,
            string action,
            double totalVolume,
            double slPips,
            double tpPips,
            DateTime barTime)
        {
            var symbol = _targetSymbol;

            // التقسيم إلى ثلاثة مراكز مستقلة متاح على الحساب Hedged فقط؛ على Netted تدمج
            // المنصة المراكز في مركز واحد فلا معنى للتقسيم ولا لحماية مستقلة.
            if (Account.AccountType != AccountType.Hedged)
            {
                Print($"{LogPrefix} [تقسيم] ❌ رفض التنفيذ: نوع الحساب {Account.AccountType} وليس Hedged — " +
                      $"لا يمكن فتح ثلاثة مراكز مستقلة على الرمز نفسه. لم يُرسل أي أمر.");
                Print($"{LogPrefix} [تقسيم] البدائل المطلوبة من مالك المشروع: (1) حساب Hedged، " +
                      $"(2) أمر واحد بلا تقسيم (يتطلب موافقة صريحة على تغيير السلوك).");
                return;
            }

            double[] childVolumes;
            string splitError;
            if (!TryPlanVolumeSplit(totalVolume, symbol.VolumeInUnitsStep, symbol.VolumeInUnitsMin,
                    symbol.VolumeInUnitsMax, out childVolumes, out splitError))
            {
                Print($"{LogPrefix} [تقسيم] ❌ رفض التنفيذ: {splitError}. لم يُرسل أي أمر (لا تقريب ولا حذف حجم).");
                Print($"{LogPrefix} [تقسيم] القيود الفعلية للرمز {symbol.Name}: الإجمالي={Fmt(totalVolume)} | " +
                      $"Min={Fmt(symbol.VolumeInUnitsMin)} | Step={Fmt(symbol.VolumeInUnitsStep)} | Max={Fmt(symbol.VolumeInUnitsMax)}.");
                Print($"{LogPrefix} [تقسيم] البدائل المطلوبة من مالك المشروع: " +
                      $"(1) زيادة الحجم الإجمالي (رصيد/مخاطرة أكبر أو وقف خسارة أقصر)، " +
                      $"(2) وسيط بحد أدنى/خطوة أصغر، (3) أمر واحد غير مقسوم (يخالف لزوم التقسيم).");
                return;
            }

            var token = barTime.ToString("yyyyMMddHHmm", CultureInfo.InvariantCulture);
            Print($"{LogPrefix} [تقسيم] خطة المجموعة {token}: الإجمالي={Fmt(totalVolume)} | " +
                  $"A(20%)={Fmt(childVolumes[0])} | B(50%)={Fmt(childVolumes[1])} | C(100%)={Fmt(childVolumes[2])} | " +
                  $"المجموع={Fmt(childVolumes[0] + childVolumes[1] + childVolumes[2])}");

            var roles = new[] { RoleA, RoleB, RoleC };
            var fractions = new[] { TargetFractionA, TargetFractionB, TargetFractionC };

            var group = new TradeGroup
            {
                Token = token,
                TradeType = tradeType,
                CreatedUtc = TimeInUtc,
            };

            var placed = 0;
            var actualTotalVolume = 0.0;
            var failureReason = (string)null;

            for (var i = 0; i < roles.Length; i++)
            {
                var role = roles[i];
                var label = BuildChildLabel(token, role);
                var childTpPips = tpPips * fractions[i];
                var comment = $"XauBot {action} {role}";

                Print($"{LogPrefix} [تقسيم] إرسال {role}: volume={Fmt(childVolumes[i])} | " +
                      $"SL={Fmt(slPips)} pips | TP={Fmt(childTpPips)} pips ({Fmt(fractions[i] * 100.0)}%) | label={label}");

                TradeResult result;
                try
                {
                    result = ExecuteMarketOrder(tradeType, _resolvedSymbolName, childVolumes[i], label,
                        slPips, childTpPips, comment, false);
                }
                catch (Exception ex)
                {
                    failureReason = $"استثناء أثناء الإرسال: {ex.GetType().Name}: {ex.Message}";
                    Print($"{LogPrefix} [تقسيم] ❌ فشل فتح المركز {role}: {failureReason}");
                    break;
                }

                if (result == null)
                {
                    failureReason = "لم تُرجِع المنصة أي TradeResult";
                    Print($"{LogPrefix} [تقسيم] ❌ فشل فتح المركز {role}: {failureReason}");
                    break;
                }

                if (!result.IsSuccessful)
                {
                    failureReason = $"Error={result.Error?.ToString() ?? "—"} | {result}";
                    Print($"{LogPrefix} [تقسيم] ❌ فشل فتح المركز {role}: فشل تنفيذ الأمر من المنصة — {failureReason}");
                    break;
                }

                var position = result.Position;
                if (position == null)
                {
                    failureReason = "TradeResult ناجح لكن بلا Position";
                    Print($"{LogPrefix} [تقسيم] ❌ فشل فتح المركز {role}: {failureReason}");
                    break;
                }

                if (role == RoleA) group.IdA = position.Id;
                else if (role == RoleB) group.IdB = position.Id;
                else group.IdC = position.Id;

                placed++;
                actualTotalVolume += position.VolumeInUnits;

                Print($"{LogPrefix} [تقسيم] ✅ فُتح المركز {role} #{position.Id} على {position.SymbolName}: " +
                      $"الحجم الفعلي={Fmt(position.VolumeInUnits)} | سعر التنفيذ={Fmt(position.EntryPrice)} | " +
                      $"SL الفعلي={FmtNullable(position.StopLoss)} | TP الفعلي={FmtNullable(position.TakeProfit)} | " +
                      $"وقت الشمعة={barTime:yyyy-MM-dd HH:mm} UTC");
            }

            if (placed < 3)
            {
                Print($"{LogPrefix} [تقسيم] ⚠️ حالة جزئية: أُرسل {placed}/3 أوامر " +
                      $"(A=#{group.IdA}، B=#{group.IdB}، C=#{group.IdC}). السبب: {failureReason ?? "—"}.");
                Print($"{LogPrefix} [تقسيم] لن تُفتح أي أوامر بديلة ولن تُغلق المراكز المفتوحة تلقائياً. " +
                      $"المراكز المفتوحة تحتفظ بوقف الخسارة الأصلي كاملاً.");
            }
            else if (Math.Abs(actualTotalVolume - totalVolume) > 1e-6)
            {
                Print($"{LogPrefix} [تقسيم] ⚠️ الحجم الفعلي المنفَّذ ({Fmt(actualTotalVolume)}) لا يساوي الحجم " +
                      $"المطلوب ({Fmt(totalVolume)}) — قد يكون هناك تنفيذ جزئي على الخادم. لن يُفتح أي أمر تعويضي.");
            }
            else
            {
                Print($"{LogPrefix} [تقسيم] ✅ اكتمل التقسيم: 3 مراكز | مجموع الحجم={Fmt(actualTotalVolume)} = الإجمالي المطلوب.");
            }

            RememberGroup(group);
        }

        // ===========================================================================
        //  توزيع الحجم: B و C متساويان (base) والباقي إلى A — في حدود خطوة الوسيط.
        //  يُعاد false عند أي استحالة، بلا تقريب أو حذف أو تجاوز للحجم الأصلي.
        // ===========================================================================
        private static bool TryPlanVolumeSplit(
            double totalVolume,
            double step,
            double minVolume,
            double maxVolume,
            out double[] childVolumes,
            out string errorReason)
        {
            childVolumes = null;
            errorReason = null;

            if (step <= 0 || minVolume <= 0 || maxVolume < minVolume)
            {
                errorReason = $"خطوة/حدود الحجم غير صالحة (Min={minVolume}, Step={step}, Max={maxVolume})";
                return false;
            }

            var exactSteps = totalVolume / step;
            var totalSteps = (long)Math.Round(exactSteps);
            if (Math.Abs(exactSteps - totalSteps) > 1e-6)
            {
                errorReason = $"إجمالي الحجم ({totalVolume}) ليس من مضاعفات خطوة الحجم ({step})";
                return false;
            }

            if (totalSteps < 3)
            {
                errorReason = $"إجمالي الحجم ({totalVolume}) أقل من ثلاث خطوات ({step}) — لا يمكن تكوين ثلاثة مراكز صالحة";
                return false;
            }

            var baseSteps = totalSteps / 3;
            var remainder = totalSteps % 3;

            // B و C متساويان دائماً (baseSteps)، والباقي كله إلى A (20%).
            var steps = new[] { baseSteps + remainder, baseSteps, baseSteps };
            var roles = new[] { RoleA, RoleB, RoleC };
            var planned = new double[3];

            for (var i = 0; i < 3; i++)
            {
                var volume = steps[i] * step;
                if (volume + 1e-9 < minVolume)
                {
                    errorReason = $"المركز {roles[i]} سيحصل على {volume} وهو أقل من الحد الأدنى {minVolume}";
                    return false;
                }

                if (volume - 1e-9 > maxVolume)
                {
                    errorReason = $"المركز {roles[i]} سيحصل على {volume} وهو أكبر من الحد الأقصى {maxVolume}";
                    return false;
                }

                planned[i] = volume;
            }

            var sum = planned[0] + planned[1] + planned[2];
            if (Math.Abs(sum - totalVolume) > 1e-9)
            {
                errorReason = $"مجموع الأحجام ({sum}) لا يساوي الإجمالي ({totalVolume})";
                return false;
            }

            childVolumes = planned;
            return true;
        }

        // ===========================================================================
        //  وسوم المراكز والربط بمجموعة القرار
        // ===========================================================================
        private static bool IsBotOwnedLabel(string label)
        {
            if (string.IsNullOrEmpty(label))
            {
                return false;
            }

            return string.Equals(label, OrderLabel, StringComparison.Ordinal) ||
                   label.StartsWith(OrderLabel + LabelSeparator, StringComparison.Ordinal);
        }

        private static string BuildChildLabel(string token, string role)
        {
            return $"{OrderLabel}{LabelSeparator}{token}{LabelSeparator}{role}";
        }

        // وسم المركز = OrderLabel:token:role — يُحلّل من الخادم بعد إعادة التشغيل أيضاً.
        private static bool TryParseChildLabel(string label, out string token, out string role)
        {
            token = null;
            role = null;

            if (!IsBotOwnedLabel(label) || string.Equals(label, OrderLabel, StringComparison.Ordinal))
            {
                return false;
            }

            var parts = label.Split(LabelSeparator);
            if (parts.Length != 3 || parts[1].Length == 0)
            {
                return false;
            }

            if (parts[2] != RoleA && parts[2] != RoleB && parts[2] != RoleC)
            {
                return false;
            }

            token = parts[1];
            role = parts[2];
            return true;
        }

        // كل مراكز هذا الـcBot المفتوحة على الرمز المستهدف (أي دور في أي مجموعة).
        private List<Position> GetBotOwnedPositions()
        {
            var result = new List<Position>();
            if (Positions == null)
            {
                return result;
            }

            foreach (var position in Positions)
            {
                if (position == null)
                {
                    continue;
                }

                if (!string.Equals(position.SymbolName, _resolvedSymbolName, StringComparison.Ordinal))
                {
                    continue;
                }

                if (!IsBotOwnedLabel(position.Label))
                {
                    continue;
                }

                result.Add(position);
            }

            return result;
        }

        // البحث بمعرّف الخادم فقط (لا بالرمز ولا بالتعليق).
        private Position FindOpenPositionById(int positionId)
        {
            if (Positions == null || positionId == 0)
            {
                return null;
            }

            foreach (var position in Positions)
            {
                if (position != null && position.Id == positionId)
                {
                    return position;
                }
            }

            return null;
        }

        private TradeGroup FindGroup(string token)
        {
            foreach (var group in _groups)
            {
                if (string.Equals(group.Token, token, StringComparison.Ordinal))
                {
                    return group;
                }
            }

            return null;
        }

        private void RememberGroup(TradeGroup group)
        {
            _groups.Add(group);

            // سقف ذاكرة بسيط: لا تُحفظ إلا مجموعة واحدة لكل شمعة قرار، ونُزيل الأقدم المكتملة.
            const int maxGroups = 64;
            if (_groups.Count <= maxGroups)
            {
                return;
            }

            for (var i = 0; i < _groups.Count; i++)
            {
                var candidate = _groups[i];
                if (candidate == group)
                {
                    continue;
                }

                if (candidate.ProtectionDone || candidate.ProtectionFailed)
                {
                    _groups.RemoveAt(i);
                    return;
                }
            }
        }

        // ===========================================================================
        //  استعادة المجموعات من المراكز المفتوحة (بعد إعادة التشغيل / إعادة الاتصال)
        //  قراءة فقط: لا أوامر ولا تعديل أوقاف. الغرض منع التكرار واستئناف التتبّع.
        // ===========================================================================
        private void AdoptExistingGroups()
        {
            var owned = GetBotOwnedPositions();
            if (owned.Count == 0)
            {
                Print($"{LogPrefix} [استعادة] لا مراكز مفتوحة لهذا الـcBot — لا شيء لاستعادته.");
                return;
            }

            var tokens = new List<string>();
            foreach (var position in owned)
            {
                string token;
                string role;
                if (!TryParseChildLabel(position.Label, out token, out role))
                {
                    continue;
                }

                if (!tokens.Contains(token))
                {
                    tokens.Add(token);
                }
            }

            foreach (var token in tokens)
            {
                var group = FindGroup(token);
                if (group == null)
                {
                    group = new TradeGroup { Token = token };
                    _groups.Add(group);
                }

                var openRoles = new List<string>();
                foreach (var position in owned)
                {
                    string positionToken;
                    string role;
                    if (!TryParseChildLabel(position.Label, out positionToken, out role))
                    {
                        continue;
                    }

                    if (!string.Equals(positionToken, token, StringComparison.Ordinal))
                    {
                        continue;
                    }

                    group.TradeType = position.TradeType;
                    if (role == RoleA) group.IdA = position.Id;
                    else if (role == RoleB) group.IdB = position.Id;
                    else group.IdC = position.Id;

                    openRoles.Add($"{role}#{position.Id}");
                }

                Print($"{LogPrefix} [استعادة] مجموعة {token}: مراكز مفتوحة = {string.Join(", ", openRoles.ToArray())}");

                if (group.IdA != 0)
                {
                    Print($"{LogPrefix} [استعادة] Position A مفتوح — ستُفعَّل الحماية عند تأكيد إغلاقه عند هدفه.");
                    continue;
                }

                if (group.IdB == 0 && group.IdC == 0)
                {
                    continue;
                }

                // Position A غير مفتوح بعد إعادة التشغيل. لا دليل محلي على أن إغلاقه كان عند
                // هدف الـ20% (الحالة داخل الذاكرة ضاعت وهذه المعمارية لا تخزّن حالة على القرص)،
                // لذلك لا نعدّل أي وقف خسارة: الوقف الأصلي يبقى كما هو ولا يُرخّى.
                if (AreChildrenAlreadyAtBreakEven(group))
                {
                    group.ProtectionDone = true;
                    Print($"{LogPrefix} [استعادة] المراكز المتبقية لمجموعة {token} عند التعادل مسبقاً — لا تعديل.");
                }
                else
                {
                    Print($"{LogPrefix} [استعادة] ⚠️ مجموعة {token}: Position A غير مفتوح ولا دليل على أنه أُغلق " +
                          $"عند هدفه — لم تُفعَّل الحماية تلقائياً. وقف الخسارة الأصلي على B/C يبقى قائماً " +
                          $"(لا يُرخّى ولا يُعدَّل). يلزم تحقق يدوي من سجل الحساب.");
                }
            }
        }

        private bool AreChildrenAlreadyAtBreakEven(TradeGroup group)
        {
            var checkedAny = false;

            foreach (var positionId in new[] { group.IdB, group.IdC })
            {
                var position = FindOpenPositionById(positionId);
                if (position == null)
                {
                    continue;
                }

                if (!position.StopLoss.HasValue)
                {
                    return false;
                }

                var entryPrice = position.EntryPrice;
                var atOrBetter = position.TradeType == TradeType.Buy
                    ? position.StopLoss.Value >= entryPrice - 1e-9
                    : position.StopLoss.Value <= entryPrice + 1e-9;

                if (!atOrBetter)
                {
                    return false;
                }

                checkedAny = true;
            }

            return checkedAny;
        }

        // ===========================================================================
        //  حماية B و C بعد تأكيد إغلاق A عند هدفه (20%)
        // ===========================================================================
        private void HandleChildClosure(Position closedPosition, PositionCloseReason reason)
        {
            if (closedPosition == null)
            {
                return;
            }

            string token;
            string role;
            if (!TryParseChildLabel(closedPosition.Label, out token, out role))
            {
                return; // مركز ليس من هذا الـcBot
            }

            if (!string.Equals(closedPosition.SymbolName, _resolvedSymbolName, StringComparison.Ordinal))
            {
                return;
            }

            // منع معالجة نفس الإغلاق مرتين (أحداث مكرّرة / إعادة اتصال / استدعاء متكرر).
            if (!_processedClosures.Add(closedPosition.Id))
            {
                Print($"{LogPrefix} [حماية] تجاهل حدث إغلاق مكرّر للمركز #{closedPosition.Id} (عولج مسبقاً).");
                return;
            }

            Print($"{LogPrefix} [حماية] أُغلق المركز {role} #{closedPosition.Id} | السبب={reason} | " +
                  $"NetProfit=${Fmt(closedPosition.NetProfit)}");

            if (role != RoleA)
            {
                Print($"{LogPrefix} [حماية] المركز {role} ليس Position A — لا يبدأ أي تفعيل للحماية.");
                return;
            }

            var group = FindGroup(token);
            if (group == null)
            {
                group = new TradeGroup { Token = token, TradeType = closedPosition.TradeType };
                _groups.Add(group);
                Print($"{LogPrefix} [حماية] مجموعة غير معروفة في الذاكرة (إعادة تشغيل؟) — أُنشئت من وسم المركز #{closedPosition.Id}.");
            }

            if (group.IdA != 0 && group.IdA != closedPosition.Id)
            {
                Print($"{LogPrefix} [حماية] ❌ المركز #{closedPosition.Id} لا يطابق Position A المسجَّل للمجموعة " +
                      $"{token} (#{group.IdA}) — لا تعديل.");
                return;
            }

            group.IdA = closedPosition.Id;

            if (group.ProtectionDone || group.ProtectionFailed)
            {
                Print($"{LogPrefix} [حماية] المجموعة {token} عولجت مسبقاً " +
                      $"(Done={group.ProtectionDone}, Failed={group.ProtectionFailed}) — لا تعديل مكرّر.");
                return;
            }

            // شرط التفعيل: تأكيد الإغلاق عند الهدف (منفّذ من الوسيط)، لا مجرد لمس السعر.
            if (reason != PositionCloseReason.TakeProfit)
            {
                Print($"{LogPrefix} [حماية] ❌ لم تُفعَّل الحماية: سبب إغلاق Position A هو '{reason}' وليس TakeProfit.");
                return;
            }

            Print($"{LogPrefix} [حماية] ✅ تأكيد إغلاق Position A عند هدفه (20%) — بدء حماية B و C.");
            ActivateProtection(group);
        }

        private void ActivateProtection(TradeGroup group)
        {
            if (Account.AccountType != AccountType.Hedged)
            {
                Print($"{LogPrefix} [حماية] ❌ نوع الحساب {Account.AccountType} وليس Hedged — لا يمكن إدارة مركزين " +
                      $"مستقلين. لم تُفعَّل الحماية.");
                group.ProtectionFailed = true;
                return;
            }

            var applied = 0;
            var alreadyProtected = 0;
            var notOpen = 0;
            var failed = 0;

            var outcomeB = ProtectChild(group, RoleB, group.IdB);
            CountOutcome(outcomeB, ref applied, ref alreadyProtected, ref notOpen, ref failed);

            var outcomeC = ProtectChild(group, RoleC, group.IdC);
            CountOutcome(outcomeC, ref applied, ref alreadyProtected, ref notOpen, ref failed);

            if (failed > 0)
            {
                group.ProtectionFailed = true;
                Print($"{LogPrefix} [حماية] ⚠️ لم تكتمل الحماية: طُبِّق {applied} | تعادل مسبق {alreadyProtected} | " +
                      $"غير مفتوح {notOpen} | فشل {failed}. وقف الخسارة الأصلي يبقى على ما فشل تعديله، " +
                      $"ولا تُعاد المحاولة تلقائياً. يلزم تحقق يدوي.");
            }
            else if (applied == 0 && alreadyProtected == 0)
            {
                // لا ادعاء حماية: لم يكن هناك مركز مفتوح لحمایته أصلاً.
                group.ProtectionFailed = true;
                Print($"{LogPrefix} [حماية] ⚠️ لم تُطبَّق أي حماية: لا مراكز مفتوحة لـ B/C عند إغلاق A " +
                      $"(غير مفتوح {notOpen}). لا أوامر تعويضية ولا وفقة وهمية.");
            }
            else
            {
                group.ProtectionDone = true;
                Print($"{LogPrefix} [حماية] ✅ انتهت معالجة الحماية: طُبِّق {applied} | تعادل مسبق {alreadyProtected} | " +
                      $"غير مفتوح {notOpen}.");
            }
        }

        private static void CountOutcome(
            ProtectionOutcome outcome, ref int applied, ref int alreadyProtected, ref int notOpen, ref int failed)
        {
            switch (outcome)
            {
                case ProtectionOutcome.Applied:
                    applied++;
                    break;
                case ProtectionOutcome.AlreadyProtected:
                    alreadyProtected++;
                    break;
                case ProtectionOutcome.NotOpen:
                    notOpen++;
                    break;
                default:
                    failed++;
                    break;
            }
        }

        private ProtectionOutcome ProtectChild(TradeGroup group, string role, int positionId)
        {
            if (positionId == 0)
            {
                Print($"{LogPrefix} [حماية] المركز {role} لم يُفتح أصلاً — لا شيء لحمايته.");
                return ProtectionOutcome.NotOpen;
            }

            var position = FindOpenPositionById(positionId);
            if (position == null)
            {
                Print($"{LogPrefix} [حماية] المركز {role} #{positionId} غير مفتوح (أُغلق مسبقاً) — لا شيء لحمايته.");
                return ProtectionOutcome.NotOpen;
            }

            // تحقق مزدوج من الربط: المعرّف من الخادم + الوسم (نفس المجموعة ونفس الدور).
            string positionToken;
            string positionRole;
            if (!TryParseChildLabel(position.Label, out positionToken, out positionRole) ||
                !string.Equals(positionToken, group.Token, StringComparison.Ordinal) ||
                positionRole != role)
            {
                Print($"{LogPrefix} [حماية] ❌ المركز #{positionId} لا يطابق مجموعة/دور {role} — لا تعديل.");
                return ProtectionOutcome.Failed;
            }

            // لا نعدّل وقفاً على مركز بلا هدف ربح على الخادم: تعديل الحماية لا يجوز أن يُفقد الهدف.
            if (!position.TakeProfit.HasValue)
            {
                Print($"{LogPrefix} [حماية] ❌ المركز {role} #{positionId} بلا هدف ربح على الخادم — " +
                      $"لن يُعدَّل وقف الخسارة كي لا يُفقد الهدف الأصلي.");
                return ProtectionOutcome.Failed;
            }

            double breakEvenPrice;
            string errorReason;
            if (!TryComputeBreakEvenStop(position, out breakEvenPrice, out errorReason))
            {
                Print($"{LogPrefix} [حماية] ❌ المركز {role} #{positionId}: {errorReason}");
                return ProtectionOutcome.Failed;
            }

            var existingStop = position.StopLoss;
            var tick = _targetSymbol != null && _targetSymbol.TickSize > 0 ? _targetSymbol.TickSize : 0.0;
            var epsilon = tick > 0 ? tick / 2.0 : 1e-9;

            if (existingStop.HasValue && Math.Abs(existingStop.Value - breakEvenPrice) <= epsilon)
            {
                Print($"{LogPrefix} [حماية] المركز {role} #{positionId} عند التعادل مسبقاً " +
                      $"(SL={Fmt(existingStop.Value)}) — لا تعديل.");
                return ProtectionOutcome.AlreadyProtected;
            }

            if (existingStop.HasValue)
            {
                var loosens = position.TradeType == TradeType.Buy
                    ? breakEvenPrice < existingStop.Value - epsilon
                    : breakEvenPrice > existingStop.Value + epsilon;

                if (loosens)
                {
                    Print($"{LogPrefix} [حماية] المركز {role} #{positionId}: وقف التعادل ({Fmt(breakEvenPrice)}) " +
                          $"أسوأ من الوقف الحالي ({Fmt(existingStop.Value)}) — لن يُرخّى الوقف. لا تعديل.");
                    return ProtectionOutcome.AlreadyProtected;
                }
            }

            if (!IsProtectiveStopOnValidSide(position, breakEvenPrice))
            {
                Print($"{LogPrefix} [حماية] ❌ المركز {role} #{positionId}: وقف التعادل ({Fmt(breakEvenPrice)}) " +
                      $"في الجهة الخاطئة من السوق (Bid={Fmt(_targetSymbol.Bid)} / Ask={Fmt(_targetSymbol.Ask)}) — " +
                      $"لم يُعدَّل الوقف، والوقف الأصلي يبقى قائماً.");
                return ProtectionOutcome.Failed;
            }

            TradeResult result;
            try
            {
                // الأسعار مطلقة، وهدف الربح الحالي يُمرَّر صريحاً كي يبقى 50%/100% كما هو.
                result = ModifyPosition(position, breakEvenPrice, position.TakeProfit, ProtectionType.Absolute);
            }
            catch (Exception ex)
            {
                Print($"{LogPrefix} [حماية] ❌ استثناء أثناء تعديل المركز {role} #{positionId}: " +
                      $"{ex.GetType().Name}: {ex.Message}");
                return ProtectionOutcome.Failed;
            }

            if (result == null)
            {
                Print($"{LogPrefix} [حماية] ❌ لم تُرجِع المنصة أي TradeResult لتعديل المركز {role} #{positionId}.");
                return ProtectionOutcome.Failed;
            }

            if (!result.IsSuccessful)
            {
                Print($"{LogPrefix} [حماية] ❌ رُفض تعديل المركز {role} #{positionId}: " +
                      $"Error={result.Error?.ToString() ?? "—"} | {result}");
                return ProtectionOutcome.Failed;
            }

            Print($"{LogPrefix} [حماية] ✅ المركز {role} #{positionId}: SL {FmtNullable(existingStop)} → " +
                  $"{Fmt(breakEvenPrice)} (تعادل صافي بعد العمولات/السواب) | TP محفوظ: {FmtNullable(position.TakeProfit)}");
            return ProtectionOutcome.Applied;
        }

        // ===========================================================================
        //  حساب وقف التعادل الصافي بعد التكاليف
        // ---------------------------------------------------------------------------
        //  التكاليف المطبَّقة: عمولة الذهاب والعودة (Position.Commissions موثّقة كاتجاه واحد)
        //  ناقص السواب إن كان دائناً. الانتشار مُحتسب أصلاً في سعر الدخول (Ask للشراء و Bid
        //  للبيع)، فإغلاق الشراء عند سعر الدخول يعادل صفر ربح إجمالي — لا يُضاف الانتشار مرتين.
        //  التقريب دائماً في اتجاه يغطي التكاليف: لأعلى للشراء ولأسفل للبيع.
        // ===========================================================================
        private bool TryComputeBreakEvenStop(Position position, out double stopPrice, out string errorReason)
        {
            stopPrice = 0.0;
            errorReason = null;

            var symbol = _targetSymbol;
            if (symbol == null || symbol.PipSize <= 0 || symbol.PipValue <= 0)
            {
                errorReason = "مواصفات الرمز غير صالحة لحساب التعادل (PipSize/PipValue)";
                return false;
            }

            if (position.VolumeInUnits <= 0)
            {
                errorReason = $"حجم المركز غير صالح ({position.VolumeInUnits})";
                return false;
            }

            var roundTripCommission = 2.0 * Math.Max(0.0, position.Commissions);
            var costsUsd = roundTripCommission - position.Swap;
            if (costsUsd < 0.0)
            {
                // لا نرخّي الوقف بسبب سواب دائن: الحد الأدنى هو نقطة الدخول نفسها.
                costsUsd = 0.0;
            }

            var costPerUnit = costsUsd / position.VolumeInUnits;
            var costPips = costPerUnit / symbol.PipValue;
            var costPriceDistance = costPips * symbol.PipSize;

            var raw = position.TradeType == TradeType.Buy
                ? position.EntryPrice + costPriceDistance
                : position.EntryPrice - costPriceDistance;

            stopPrice = RoundPriceToTick(raw, position.TradeType);
            if (double.IsNaN(stopPrice) || double.IsInfinity(stopPrice) || stopPrice <= 0.0)
            {
                errorReason = $"سعر التعادل المحسوب غير صالح ({stopPrice})";
                return false;
            }

            return true;
        }

        private double RoundPriceToTick(double price, TradeType tradeType)
        {
            var symbol = _targetSymbol;
            if (symbol == null)
            {
                return price;
            }

            var digits = Math.Max(0, symbol.Digits);
            var tick = symbol.TickSize > 0 ? symbol.TickSize : Math.Pow(10, -digits);
            if (tick <= 0)
            {
                return Math.Round(price, digits);
            }

            var steps = price / tick;
            // هامش 1e-6 خطوة يمنع أخطاء الفاصلة العائمة من تحويل سعر على الحد إلى الخطوة التالية.
            var roundedSteps = tradeType == TradeType.Buy
                ? Math.Ceiling(steps - 1e-6)
                : Math.Floor(steps + 1e-6);

            return Math.Round(roundedSteps * tick, digits, MidpointRounding.AwayFromZero);
        }

        private bool IsProtectiveStopOnValidSide(Position position, double stopPrice)
        {
            var symbol = _targetSymbol;
            if (symbol == null)
            {
                return false;
            }

            return position.TradeType == TradeType.Buy
                ? stopPrice < symbol.Bid
                : stopPrice > symbol.Ask;
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

                // الحماية أولاً (حماية رأس المال)، ثم إبلاغ Brain بنتيجة الإغلاق كما كان.
                HandleChildClosure(pos, args.Reason);
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
                    Method = cAlgo.API.HttpMethod.Post,
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