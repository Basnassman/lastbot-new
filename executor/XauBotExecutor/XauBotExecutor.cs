// =====================================================================================
//  XauBotExecutor  —  cBot لـ cTrader Automate  (C# / cAlgo.API)
// -------------------------------------------------------------------------------------
//  الدفعة الحالية = "مراقبة فقط" (مستهلك / Consumer):
//    * عند كل إغلاق شمعة M15 على رمز العملة (XAUUSD افتراضياً) يستدعي HTTP GET على
//      الـ endpoint  /decide-log-only  في Brain API.
//    * يسجّل كل استجابة (action / confidence / reasons / dollar_trend_debug) في سجل
//      cTrader الأصلي (Print) — مرئي مباشرة من واجهة المنصة.
//    * فحص إلزامي عند البدء: إن كان الحساب حقيقياً (Live) يوقف الـcBot فوراً. لا يمكن
//      تجاوز هذا الفحص بأي إعداد أو Parameter — مكتوب في الكود بشكل صريح.
//    * أخطاء الشبكة تُسجَّل بوضوح ولا تُسقط الـcBot، وتُعاد المحاولة عند الشمعة التالية.
//
//  ⚠️  ممنوع صريحاً في هذه الدفعة: لا فتح أي صفقة (Market / Pending Order) مهما كان
//      القرار. البحث عن ExecuteMarketOrder / PlaceLimitOrder / ExecuteMarketOrderAsync
//      في هذا الملف يجب أن يعطي صفر نتائج. التنفيذ هو الدفعة التالية بعد المراجعة.
//      هذا الملف طرف استهلاك فقط ولا يعدّل أي شيء في منطق الـ Brain.
//
//  طريقة الاستخدام: انظر executor/README.md
// =====================================================================================

using System;
using System.Collections.Generic;
using System.Globalization;
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

        // ---------------------------------------------------------------------------
        // حالة وقت التشغيل
        // ---------------------------------------------------------------------------
        private static readonly JsonSerializerOptions JsonOptions = new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true,
        };

        private Bars _m15Bars;
        private string _resolvedSymbolName;

        // علامة داخلية: لا يُسمح بأي نداء HTTP قبل اجتياز فحص الحماية.
        private bool _guardPassed;

        private long _successCount;
        private long _failureCount;

        // ===========================================================================
        //  دورة الحياة
        // ===========================================================================
        protected override void OnStart()
        {
            _guardPassed = false;
            _successCount = 0;
            _failureCount = 0;

            Print($"{LogPrefix} =================================================================");
            Print($"{LogPrefix} بدء تشغيل XauBotExecutor — وضع مراقبة فقط (لا تنفيذ صفقات في هذه الدفعة).");
            Print($"{LogPrefix} RunningMode={RunningMode} | IsBacktesting={IsBacktesting}");
            Print($"{LogPrefix} الحساب: النوع={Account.AccountType} | الرقم={Account.Number} | " +
                  $"الوسيط={Account.BrokerName} | IsLive={Account.IsLive}");

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

            // تنبيه صريح: هذه الدفعة لا تنفّذ صفقات مهما كان القرار.
            if (string.Equals(d.Action, "buy", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(d.Action, "sell", StringComparison.OrdinalIgnoreCase))
            {
                Print($"{LogPrefix}    ℹ️ القرار يدعو إلى '{d.Action}' — لكن التنفيذ معطّل عمداً في هذه " +
                      $"الدفعة (وضع المراقبة فقط). لا يُفتح أي أمر.");
            }
        }

        // ===========================================================================
        //  أدوات مساعدة
        // ===========================================================================
        private string BuildRequestUrl()
        {
            var scheme = UseHttps ? "https" : "http";

            var basePath = (BrainApiBasePath ?? string.Empty).Trim();
            if (basePath.StartsWith("/", StringComparison.Ordinal))
            {
                basePath = basePath.Substring(1);
            }
            if (basePath.Length > 0 && !basePath.EndsWith("/", StringComparison.Ordinal))
            {
                basePath += "/";
            }

            return string.Format(
                CultureInfo.InvariantCulture,
                "{0}://{1}:{2}/{3}decide-log-only?symbol={4}&timeframe={5}",
                scheme,
                BrainApiHost,
                BrainApiPort,
                basePath,
                Uri.EscapeDataString(_resolvedSymbolName ?? "XAUUSD"),
                BarTimeframe);
        }

        // نسخة بلا رمز/فريم — تُستخدم في سطر الإعداد الأولي قبل حلّ الرمز.
        private string BuildRequestUrlUnsigned()
        {
            var scheme = UseHttps ? "https" : "http";
            var basePath = (BrainApiBasePath ?? string.Empty).Trim();
            if (basePath.StartsWith("/", StringComparison.Ordinal))
            {
                basePath = basePath.Substring(1);
            }
            if (basePath.Length > 0 && !basePath.EndsWith("/", StringComparison.Ordinal))
            {
                basePath += "/";
            }

            return string.Format(
                CultureInfo.InvariantCulture,
                "{0}://{1}:{2}/{3}decide-log-only?symbol=<symbol>&timeframe={4}",
                scheme,
                BrainApiHost,
                BrainApiPort,
                basePath,
                BarTimeframe);
        }

        private static string Fmt(double value)
        {
            return value.ToString("0.###", CultureInfo.InvariantCulture);
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
