// =====================================================================================
//  ExecutorHarness — يشغّل ملف الـcBot الحقيقي (XauBotExecutor.cs) آلياً خارج cTrader.
// -------------------------------------------------------------------------------------
//  ⚠️ هذا ليس تشغيلاً داخل cTrader، وليس حساب Demo حقيقي عند وسيط. إنه تشغيل لملف
//     الـcBot نفسه مقابل شيم cAlgo.API + خادم Brain API المحلي، لإثبات السلوك القابل
//     للإثبات خارج المنصة (فحص الحماية، شكل النداء، التسجيل، معالجة أخطاء الشبكة،
//     وطبقات التنفيذ 0..7).
//
//  الأوضاع:
//    scenarios : ينفّذ سيناريوهات التحقق ويكتب سجلاً لكل سيناريو في مجلد out/.
//    m15       : يشغّل الـcBot بإيقاع شمعة M15 حقيقي (كل 900 ثانية) لعدد دورات محدد.
// =====================================================================================

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Threading;
using cAlgo.API;
using cAlgo.API.Internals;
using cAlgo.Robots;

namespace ExecutorHarness
{
    internal sealed class Options
    {
        public string Mode = "scenarios";
        public string BaseUrl = "http://127.0.0.1:8000";
        public string Symbol = "XAUUSD";
        public int Cycles = 4;
        public int IntervalSeconds = 900;
        public string OutDir = "out";
    }

    // يجب أن يطابق OrderLabel داخل XauBotExecutor.cs.
    internal static class HarnessConstants
    {
        public const string OrderLabel = "XauBotExecutor";
    }

    internal sealed class TestAccount : IAccount
    {
        public AccountType AccountType => AccountType.Hedged;
        public bool IsLive { get; set; }
        public int Number { get; set; } = 12345678;
        public string BrokerName { get; set; } = "HarnessBroker";
        public double Balance => 10000.0;
        public double Equity => 10000.0;
    }

    internal sealed class OrderCall
    {
        public TradeType TradeType;
        public string SymbolName;
        public double Volume;
        public string Label;
        public double? StopLossPips;
        public double? TakeProfitPips;
        public string Comment;
        public bool HasTrailingStop;
    }

    // مواصفات رمز اصطناعية للاختبار — قيم صريحة كي تكون النتائج قابلة للحساب يدوياً.
    internal sealed class SymbolSpec
    {
        public string Name = "XAUUSD";
        public int Digits = 2;
        public double PipSize = 0.1;
        public double PipValue = 0.1;
        public double TickSize = 0.01;
        public double TickValue = 0.01;
        public double LotSize = 100.0;
        public double Bid = 2000.0;
        public double Ask = 2000.1;
        public double Min = 1.0;
        public double Max = 1000000.0;
        public double Step = 1.0;
    }

    internal sealed class ExecSettings
    {
        public bool ExecutionEnabled = true;
        public bool IsLive = false;
        public double MaxPositionSizeUsd = 50.0;
        public double RiskRewardRatio = 1.5;
        public double StopLossAtrMultiplier = 1.5;
        public int AtrPeriod = 14;
        public int AtrLookbackBars = 50;
        public int BarCount = 40;
        public double AtrTarget = 1.0;
        public SymbolSpec Spec = new SymbolSpec();
        public string Action = "buy";
        public double SizeUsd = 15.0;
        public double Confidence = 90.0;
        public bool PreOpenPosition = false;
        public Func<OrderCall, TradeResult> OrderResultFactory = null;
        public Func<string, HttpResponse> DecisionTransport = null;
    }

    internal sealed class ExecFixture
    {
        public XauBotExecutor Bot;
        public Bars Bars;
        public Positions Positions;
        public Symbol Symbol;
        public List<OrderCall> Orders = new List<OrderCall>();
        public List<string> HttpGets = new List<string>();
        public List<string> HttpSends = new List<string>();
    }

    internal static class Program
    {
        private static readonly List<string> Failures = new List<string>();
        private static readonly List<string> Passes = new List<string>();

        private static int Main(string[] args)
        {
            Options opts;
            try
            {
                opts = ParseArgs(args);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("bad args: " + ex.Message);
                return 2;
            }

            Directory.CreateDirectory(opts.OutDir);

            if (opts.Mode == "scenarios")
            {
                return RunScenarios(opts);
            }

            if (opts.Mode == "m15")
            {
                return RunM15(opts);
            }

            Console.Error.WriteLine("unknown mode: " + opts.Mode);
            return 2;
        }

        // -------------------------------------------------------------------------------
        //  بناء روبوت بسيناريو محدد (وضع المراقبة)
        // -------------------------------------------------------------------------------
        private sealed class BotFixture
        {
            public XauBotExecutor Bot;
            public Bars Bars;
            public List<string> HttpCalls = new List<string>();
        }

        private static BotFixture BuildBot(
            Options opts,
            bool isLive,
            Func<string, HttpResponse> transport,
            Func<string, Symbol> symbolResolver)
        {
            var fixture = new BotFixture();

            var host = new Uri(opts.BaseUrl);
            fixture.Bot = new XauBotExecutor
            {
                BrainApiHost = host.Host,
                BrainApiPort = host.Port,
                UseHttps = host.Scheme.Equals("https", StringComparison.OrdinalIgnoreCase),
                BrainApiBasePath = string.Empty,
                TargetSymbol = opts.Symbol,
            };

            fixture.Bot.Account = new TestAccount { IsLive = isLive };
            fixture.Bot.RunningMode = RunningMode.RealTime;
            fixture.Bot.IsBacktesting = false;
            fixture.Bot.Time = DateTime.UtcNow;
            fixture.Bot.TimeInUtc = DateTime.UtcNow;

            fixture.Bot.Symbol = new Symbol { Name = opts.Symbol };
            fixture.Bot.Symbols = new Symbols
            {
                Resolver = symbolResolver ?? (name => new Symbol { Name = name }),
            };
            fixture.Bot.Positions = new Positions();

            var bars = new Bars
            {
                SymbolName = opts.Symbol,
                TimeFrame = TimeFrame.Minute15,
            };
            fixture.Bars = bars;
            fixture.Bot.MarketData = new MarketData
            {
                BarsProvider = (tf, symbolName) => bars,
            };

            var http = new Http
            {
                Transport = url =>
                {
                    fixture.HttpCalls.Add(url);
                    return transport(url);
                },
            };
            fixture.Bot.Http = http;

            return fixture;
        }

        private static HttpResponse RealTransport(string url)
        {
            return new Http().Get(url);
        }

        // -------------------------------------------------------------------------------
        //  بناء روبوت مفعّل التنفيذ (سيناريوهات الطبقات 0..7)
        // -------------------------------------------------------------------------------
        private static ExecFixture BuildExecFixture(ExecSettings s)
        {
            var fixture = new ExecFixture();

            fixture.Symbol = new Symbol
            {
                Name = s.Spec.Name,
                Digits = s.Spec.Digits,
                PipSize = s.Spec.PipSize,
                PipValue = s.Spec.PipValue,
                TickSize = s.Spec.TickSize,
                TickValue = s.Spec.TickValue,
                LotSize = s.Spec.LotSize,
                Bid = s.Spec.Bid,
                Ask = s.Spec.Ask,
                VolumeInUnitsMin = s.Spec.Min,
                VolumeInUnitsMax = s.Spec.Max,
                VolumeInUnitsStep = s.Spec.Step,
            };

            var barStart = new DateTime(2026, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            var bars = new Bars
            {
                SymbolName = s.Spec.Name,
                TimeFrame = TimeFrame.Minute15,
            };
            for (var i = 0; i < s.BarCount; i++)
            {
                var mid = 2000.0;
                var half = s.AtrTarget / 2.0;
                bars.AddBarForTest(
                    barStart.AddMinutes(15 * i),
                    mid,
                    mid + half,
                    mid - half,
                    mid);
            }
            fixture.Bars = bars;

            var positions = new Positions();
            fixture.Positions = positions;
            if (s.PreOpenPosition)
            {
                positions.AddForTest(new Position
                {
                    Id = 555,
                    SymbolName = s.Spec.Name,
                    TradeType = TradeType.Buy,
                    VolumeInUnits = 1,
                    EntryPrice = s.Spec.Ask,
                    Label = HarnessConstants.OrderLabel,
                    EntryTime = barStart,
                });
            }

            fixture.Bot = new XauBotExecutor
            {
                BrainApiHost = "127.0.0.1",
                BrainApiPort = 8000,
                UseHttps = false,
                BrainApiBasePath = string.Empty,
                TargetSymbol = s.Spec.Name,
                ExecutionEnabled = s.ExecutionEnabled,
                MaxPositionSizeUsd = s.MaxPositionSizeUsd,
                RiskRewardRatio = s.RiskRewardRatio,
                StopLossAtrMultiplier = s.StopLossAtrMultiplier,
                AtrPeriod = s.AtrPeriod,
                AtrLookbackBars = s.AtrLookbackBars,
            };

            fixture.Bot.Account = new TestAccount { IsLive = s.IsLive };
            fixture.Bot.RunningMode = RunningMode.RealTime;
            fixture.Bot.IsBacktesting = false;
            fixture.Bot.Time = barStart;
            fixture.Bot.TimeInUtc = barStart;
            fixture.Bot.Symbol = fixture.Symbol;
            fixture.Bot.Symbols = new Symbols { Resolver = name => fixture.Symbol };
            fixture.Bot.Positions = positions;
            fixture.Bot.MarketData = new MarketData { BarsProvider = (tf, symbolName) => bars };

            var decisionBody = s.DecisionTransport != null
                ? null
                : DecisionBody(s.Action, s.SizeUsd, s.Confidence);

            var http = new Http
            {
                Transport = url =>
                {
                    fixture.HttpGets.Add(url);
                    if (s.DecisionTransport != null)
                    {
                        return s.DecisionTransport(url);
                    }
                    return new HttpResponse { StatusCode = 200, IsSuccessful = true, Body = decisionBody };
                },
                SendTransport = request =>
                {
                    fixture.HttpSends.Add(request.Method + " " + request.Uri);
                    return new HttpResponse { StatusCode = 200, IsSuccessful = true, Body = "{\"status\":\"recorded\"}" };
                },
            };
            fixture.Bot.Http = http;

            fixture.Bot.OrderExecutor = (tt, sym, vol, label, sl, tp, comment, trailing) =>
            {
                var call = new OrderCall
                {
                    TradeType = tt,
                    SymbolName = sym,
                    Volume = vol,
                    Label = label,
                    StopLossPips = sl,
                    TakeProfitPips = tp,
                    Comment = comment,
                    HasTrailingStop = trailing,
                };
                fixture.Orders.Add(call);

                if (s.OrderResultFactory != null)
                {
                    return s.OrderResultFactory(call);
                }

                var entry = tt == TradeType.Buy ? fixture.Symbol.Ask : fixture.Symbol.Bid;
                double? stopLoss = sl.HasValue
                    ? entry + (tt == TradeType.Buy ? -1 : 1) * sl.Value * fixture.Symbol.PipSize
                    : (double?)null;
                double? takeProfit = tp.HasValue
                    ? entry + (tt == TradeType.Buy ? 1 : -1) * tp.Value * fixture.Symbol.PipSize
                    : (double?)null;

                var position = new Position
                {
                    Id = 900001,
                    SymbolName = sym,
                    TradeType = tt,
                    VolumeInUnits = vol,
                    EntryPrice = entry,
                    StopLoss = stopLoss,
                    TakeProfit = takeProfit,
                    Label = label,
                    Comment = comment,
                    EntryTime = barStart,
                };
                positions.AddForTest(position);

                return new TradeResult { IsSuccessful = true, Position = position, Description = "Order executed" };
            };

            return fixture;
        }

        private static string DecisionBody(string action, double sizeUsd, double confidence)
        {
            return string.Format(
                CultureInfo.InvariantCulture,
                "{{\"mode\":\"log-only\",\"action\":\"{0}\",\"position_size_usd\":{1},\"confidence\":{2}," +
                "\"reasons\":[\"harness\"],\"dollar_trend_debug\":null}}",
                action,
                sizeUsd.ToString("R", CultureInfo.InvariantCulture),
                confidence.ToString("R", CultureInfo.InvariantCulture));
        }

        private static void RaiseBar(ExecFixture fx)
        {
            fx.Bot.TimeInUtc = fx.Bars.LastBar != null ? fx.Bars.LastBar.OpenTime : DateTime.UtcNow;
            fx.Bars.RaiseBarClosedForTest();
        }

        // -------------------------------------------------------------------------------
        //  السيناريوهات
        // -------------------------------------------------------------------------------
        private static int RunScenarios(Options opts)
        {
            var summary = new List<string>();

            // === سيناريو A: حساب Demo — يجب أن يستمر ويعمل النداء بنجاح ===============
            {
                var fx = BuildBot(opts, isLive: false, RealTransport, null);
                fx.Bot.TestStart();

                Check(!fx.Bot.Stopped, "A: حساب Demo لم يُوقف الـcBot");
                Check(Contains(fx.Bot, "فحص الحماية: الحساب Demo") && Contains(fx.Bot, "✅"),
                    "A: سجل رسالة اجتياز فحص الحماية");
                Check(fx.Bot.Stopped == false, "A: الـcBot يعمل بعد الفحص");

                for (var i = 0; i < 3; i++)
                {
                    fx.Bot.TimeInUtc = DateTime.UtcNow;
                    fx.Bars.RaiseBarClosedForTest();
                }

                Check(fx.HttpCalls.Count == 3, $"A: عدد نداءات HTTP = {fx.HttpCalls.Count} (المتوقع 3)");
                Check(fx.HttpCalls.All(u => u.Contains("/decide-log-only")), "A: كل النداءات على /decide-log-only");
                Check(fx.HttpCalls.All(u => u.Contains("symbol=" + opts.Symbol) && u.Contains("timeframe=M15")),
                    "A: النداءات تحمل symbol و timeframe=M15");
                Check(Count(fx.Bot, "قرار مُستلَم") == 3, "A: سُجِّل 3 قرارات ناجحة");
                Check(Contains(fx.Bot, "action=") && Contains(fx.Bot, "confidence="),
                    "A: السجل يحتوي action و confidence");
                Check(Contains(fx.Bot, "reasons"), "A: السجل يحتوي reasons");
                Check(Contains(fx.Bot, "dollar_trend_debug"), "A: السجل يحتوي dollar_trend_debug");
                Check(Contains(fx.Bot, "ExecutionEnabled=false — وضع مراقبة فقط"),
                    "A: سُجِّل أن مفتاح التنفيذ مطفأ (وضع مراقبة)");

                WriteLog(opts, "scenario_A_demo_ok.log", fx.Bot);
                summary.Add($"A demo-ok: calls={fx.HttpCalls.Count}, decisions={Count(fx.Bot, "قرار مُستلَم")}");
            }

            // === سيناريو B: حساب حقيقي (Live) — فحص الحماية يجب أن يوقف فوراً ========
            {
                var fx = BuildBot(opts, isLive: true, RealTransport, null);
                fx.Bot.TestStart();

                Check(fx.Bot.Stopped, "B: تم إيقاف الـcBot على الحساب الحقيقي (Stop)");
                Check(Contains(fx.Bot, "رفض التشغيل") && Contains(fx.Bot, "IsLive=true"),
                    "B: سجل رفض التشغيل بسبب IsLive=true");
                Check(fx.HttpCalls.Count == 0, "B: لم يُنفَّذ أي نداء HTTP على حساب حقيقي");

                // حتى لو جاء إغلاق شمعة لاحقاً — لا نداء (الفحص يمنع التشغيل كلياً)
                fx.Bars.RaiseBarClosedForTest();
                Check(fx.HttpCalls.Count == 0, "B: لا نداء HTTP حتى بعد إغلاق شمعة (الـcBot موقوف)");
                Check(!Contains(fx.Bot, "قرار مُستلَم"), "B: لم يُسجَّل أي قرار");

                WriteLog(opts, "scenario_B_live_guard_stop.log", fx.Bot);
                summary.Add($"B live-guard: stopped={fx.Bot.Stopped}, httpCalls={fx.HttpCalls.Count}");
            }

            // === سيناريو C: فشل النقل (استثناء من الشبكة) — لا إسقاط + إعادة محاولة ====
            {
                var callCount = 0;
                Func<string, HttpResponse> flaky = url =>
                {
                    callCount++;
                    if (callCount == 1)
                    {
                        return new HttpResponse
                        {
                            StatusCode = 0,
                            IsSuccessful = false,
                            Exception = new HttpException("Simulated: connection refused (network down)"),
                        };
                    }
                    return RealTransport(url);
                };

                var fx = BuildBot(opts, isLive: false, flaky, null);
                fx.Bot.TestStart();

                fx.Bot.TimeInUtc = DateTime.UtcNow;
                fx.Bars.RaiseBarClosedForTest();   // يفشل
                Check(!fx.Bot.Stopped, "C: الـcBot لم يُسقَط بعد فشل الشبكة");
                Check(Contains(fx.Bot, "خطأ HTTP من Brain API"), "C: سُجِّل خطأ HTTP بوضوح");

                fx.Bot.TimeInUtc = DateTime.UtcNow;
                fx.Bars.RaiseBarClosedForTest();   // ينجح (إعادة المحاولة عند الشمعة التالية)
                Check(Contains(fx.Bot, "قرار مُستلَم"), "C: نجحت إعادة المحاولة عند الشمعة التالية");
                Check(Count(fx.Bot, "⚠️") >= 1, "C: يوجد سطر تحذير واحد على الأقل");

                WriteLog(opts, "scenario_C_network_error_then_recover.log", fx.Bot);
                summary.Add($"C network-recover: httpCalls={fx.HttpCalls.Count}, failures={Count(fx.Bot, "⚠️")}");
            }

            // === سيناريو D: رمز حالة HTTP غير ناجح (500) =============================
            {
                Func<string, HttpResponse> failing = url => new HttpResponse
                {
                    StatusCode = 500,
                    IsSuccessful = false,
                    Body = "{\"detail\":\"internal error\"}",
                };

                var fx = BuildBot(opts, isLive: false, failing, null);
                fx.Bot.TestStart();
                fx.Bars.RaiseBarClosedForTest();

                Check(!fx.Bot.Stopped, "D: الـcBot يعمل رغم HTTP 500");
                Check(Contains(fx.Bot, "HTTP 500"), "D: سُجِّل رمز الحالة 500");

                WriteLog(opts, "scenario_D_http_500.log", fx.Bot);
                summary.Add("D http-500: survived & logged");
            }

            // === سيناريو E: JSON غير صالح ============================================
            {
                Func<string, HttpResponse> badJson = url => new HttpResponse
                {
                    StatusCode = 200,
                    IsSuccessful = true,
                    Body = "{ this is not valid json ",
                };

                var fx = BuildBot(opts, isLive: false, badJson, null);
                fx.Bot.TestStart();
                fx.Bars.RaiseBarClosedForTest();

                Check(!fx.Bot.Stopped, "E: الـcBot يعمل رغم JSON غير صالح");
                Check(Contains(fx.Bot, "تعذّر تحليل JSON"), "E: سُجِّل فشل تحليل JSON");

                WriteLog(opts, "scenario_E_bad_json.log", fx.Bot);
                summary.Add("E bad-json: survived & logged");
            }

            // === سيناريو F: استثناء فعلي مرفوع من Http.Get =============================
            {
                Func<string, HttpResponse> thrower = url => throw new InvalidOperationException("Simulated: Http.Get threw");
                var fx = BuildBot(opts, isLive: false, thrower, null);
                fx.Bot.TestStart();
                fx.Bars.RaiseBarClosedForTest();

                Check(!fx.Bot.Stopped, "F: الـcBot نجا من استثناء Http.Get");
                Check(Contains(fx.Bot, "فشل شبكة"), "F: سُجِّل فشل الشبكة");

                WriteLog(opts, "scenario_F_http_throws.log", fx.Bot);
                summary.Add("F http-throw: survived & logged");
            }

            // === سيناريو G: رمز غير موجود ============================================
            {
                Func<string, Symbol> missing = name => throw new ArgumentException($"Symbol '{name}' not found");
                var fx = BuildBot(opts, isLive: false, RealTransport, missing);
                fx.Bot.TestStart();

                Check(fx.Bot.Stopped, "G: أُوقف الـcBot عند رمز غير موجود");
                Check(Contains(fx.Bot, "تعذّر إيجاد الرمز"), "G: سُجِّلت رسالة الرمز غير الموجود");
                Check(fx.HttpCalls.Count == 0, "G: لا نداء HTTP برمز غير صالح");

                WriteLog(opts, "scenario_G_symbol_missing.log", fx.Bot);
                summary.Add("G symbol-missing: stopped & logged");
            }

            // =========================================================================
            //  سيناريوهات طبقات التنفيذ (0..7)
            // =========================================================================

            // === H: نجاح فتح صفقة شراء — مع SL و TP مرفقين في نفس الأمر ==============
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 1, $"H: أُرسل أمر واحد فقط (الفعل={fx.Orders.Count})");
                if (fx.Orders.Count == 1)
                {
                    var o = fx.Orders[0];
                    Check(o.TradeType == TradeType.Buy, "H: نوع الأمر Buy");
                    Check(o.Label == HarnessConstants.OrderLabel, "H: الوسم = XauBotExecutor");
                    // ATR=1.0 × 1.5 = 1.5 سعر ÷ PipSize 0.1 = 15 pip
                    Check(Math.Abs(o.StopLossPips.GetValueOrDefault() - 15.0) < 1e-6,
                        $"H: SL = 15 pip (الفعلي={o.StopLossPips})");
                    Check(o.TakeProfitPips.HasValue && Math.Abs(o.TakeProfitPips.Value - 22.5) < 1e-6,
                        $"H: TP = SL×1.5 = 22.5 pip (الفعلي={o.TakeProfitPips})");
                    Check(o.StopLossPips.HasValue && o.TakeProfitPips.HasValue,
                        "H: SL و TP مرفقان في نفس الأمر (لا أمر منفصل)");
                    // raw = 15 / (15 × 0.1) = 10 وحدات
                    Check(Math.Abs(o.Volume - 10.0) < 1e-6, $"H: الحجم = 10 وحدة (الفعلي={o.Volume})");
                }
                Check(Count(fx.Bot, "✅ فُتح المركز") == 1, "H: سُجِّل فتح المركز مع التفاصيل");
                Check(Contains(fx.Bot, "SL الفعلي على الخادم") && Contains(fx.Bot, "TP الفعلي على الخادم"),
                    "H: سُجِّل SL/TP الفعليان على الخادم");
                Check(fx.Positions.Count == 1, "H: صار في الحساب مركز مفتوح واحد");

                WriteLog(opts, "scenario_H_execution_success_buy.log", fx.Bot);
                summary.Add($"H exec-success-buy: orders={fx.Orders.Count}, volume={(fx.Orders.Count == 1 ? fx.Orders[0].Volume.ToString(CultureInfo.InvariantCulture) : "—")}");
            }

            // === H2: نجاح فتح صفقة بيع ===============================================
            {
                var s = new ExecSettings { Action = "sell", SizeUsd = 15.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 1 && fx.Orders[0].TradeType == TradeType.Sell,
                    "H2: أُرسل أمر Sell واحد مع SL/TP");
                Check(fx.Orders.Count == 1 && fx.Orders[0].StopLossPips.HasValue && fx.Orders[0].TakeProfitPips.HasValue,
                    "H2: SL/TP مرفقان في أمر البيع");

                WriteLog(opts, "scenario_H2_execution_success_sell.log", fx.Bot);
                summary.Add($"H2 exec-success-sell: orders={fx.Orders.Count}");
            }

            // === I: رفض لـExecutionEnabled=false =====================================
            {
                var s = new ExecSettings { ExecutionEnabled = false, Action = "buy", SizeUsd = 15.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 0, "I: لا أمر عند ExecutionEnabled=false");
                Check(Contains(fx.Bot, "ExecutionEnabled=false — وضع مراقبة فقط"),
                    "I: سُجِّل وضع المراقبة عند البدء");
                Check(Contains(fx.Bot, "ExecutionEnabled=false — وضع مراقبة فقط (لا يُفتح أي أمر)"),
                    "I: سُجِّل سبب الرفض عند محاولة التنفيذ");

                WriteLog(opts, "scenario_I_execution_disabled.log", fx.Bot);
                summary.Add("I exec-disabled: no order");
            }

            // === J: رفض لأن action = none ============================================
            {
                var s = new ExecSettings { Action = "none", SizeUsd = 0.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 0, "J: لا أمر عند action=none");
                Check(Contains(fx.Bot, "ليس buy/sell"), "J: سُجِّل أن القرار ليس buy/sell");

                WriteLog(opts, "scenario_J_action_none.log", fx.Bot);
                summary.Add("J action-none: no order");
            }

            // === K: رفض لتجاوز MaxPositionSizeUsd ===================================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 100.0, MaxPositionSizeUsd = 20.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 0, "K: لا أمر عند تجاوز سقف حجم المركز");
                Check(Contains(fx.Bot, "يتجاوز MaxPositionSizeUsd"), "K: سُجِّل سبب تجاوز السقف");

                WriteLog(opts, "scenario_K_over_max_size.log", fx.Bot);
                summary.Add("K over-max-size: no order");
            }

            // === L: رفض لحجم غير صالح (<= 0) ========================================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 0.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 0, "L: لا أمر عند position_size_usd <= 0");
                Check(Contains(fx.Bot, "position_size_usd غير صالح"), "L: سُجِّل سبب الحجم غير الصالح");

                WriteLog(opts, "scenario_L_invalid_size.log", fx.Bot);
                summary.Add("L invalid-size: no order");
            }

            // === M: رفض لوجود مركز مفتوح مسبقاً =====================================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, PreOpenPosition = true };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 0, "M: لا أمر عند وجود مركز مفتوح مسبقاً");
                Check(Contains(fx.Bot, "مركز مفتوح مسبقاً"), "M: سُجِّل سبب وجود مركز قائم");

                WriteLog(opts, "scenario_M_existing_position.log", fx.Bot);
                summary.Add("M existing-position: no order");
            }

            // === N: رفض لتعذّر حساب ATR (بيانات غير كافية) ==========================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, BarCount = 5, AtrPeriod = 14 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 0, "N: لا أمر عند تعذّر حساب ATR");
                Check(Contains(fx.Bot, "تعذّر حساب ATR صالح"), "N: سُجِّل سبب تعذّر ATR (لا SL ⇒ لا صفقة)");

                WriteLog(opts, "scenario_N_no_atr.log", fx.Bot);
                summary.Add("N no-atr: no order");
            }

            // === O: فشل تنفيذ من الخادم (TradeResult غير ناجح) =======================
            {
                var s = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 15.0,
                    OrderResultFactory = call => new TradeResult
                    {
                        IsSuccessful = false,
                        Error = ErrorCode.InsufficientMoney,
                        Description = "Not enough money to perform the operation",
                    },
                };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 1, "O: حُوِّل أمر واحد للخادم");
                Check(Contains(fx.Bot, "فشل تنفيذ الأمر من المنصة"), "O: سُجِّل فشل التنفيذ من المنصة");
                Check(Contains(fx.Bot, "InsufficientMoney"), "O: سُجِّل نص/رمز الخطأ من المنصة");
                Check(fx.Positions.Count == 0, "O: لم يُفتح أي مركز بعد الفشل");

                // الطبقة 7/5: لا إعادة محاولة تلقائية بنفس الشمعة
                RaiseBar(fx);
                Check(fx.Orders.Count == 1, "O: لم تُعَد المحاولة تلقائياً بنفس الشمعة");

                WriteLog(opts, "scenario_O_server_failure.log", fx.Bot);
                summary.Add($"O server-failure: orders={fx.Orders.Count}");
            }

            // === P: رفض لأن الحجم أقل من الحد الأدنى للوسيط =========================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 0.05 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 0, "P: لا أمر عندما يكون الحجم أقل من الحد الأدنى");
                Check(Contains(fx.Bot, "أقل من الحد الأدنى"), "P: سُجِّل سبب الحجم تحت الحد الأدنى");

                WriteLog(opts, "scenario_P_volume_below_min.log", fx.Bot);
                summary.Add("P volume-below-min: no order");
            }

            // === Q: منع التكرار لنفس شمعة القرار ====================================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();

                // نرفع نفس الشمعة مرتين (محاكاة إعادة اتصال تُعيد إطلاق الحدث لنفس الشمعة).
                RaiseBar(fx);
                RaiseBar(fx);

                Check(fx.Orders.Count == 1, $"Q: أمر واحد فقط لنفس الشمعة (الفعلي={fx.Orders.Count})");
                Check(Contains(fx.Bot, "منع تكرار"), "Q: سُجِّل منع التكرار لنفس الشمعة");

                WriteLog(opts, "scenario_Q_idempotency.log", fx.Bot);
                summary.Add($"Q idempotency: orders={fx.Orders.Count}");
            }

            // === R: إغلاق مركز → POST /trade-result بالـPnL الفعلي ==================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Positions.Count == 1, "R: فُتح مركز واحد قبل الإغلاق");
                var opened = fx.Positions[0];

                // إغلاق المركز على TP (كما تفعل المنصة) — يُطلق حدث Positions.Closed.
                fx.Positions.CloseForTest(opened, netProfit: 12.5, reason: PositionCloseReason.TakeProfit);

                Check(fx.HttpSends.Count == 1, $"R: أُرسل نداء POST واحد للإغلاق (الفعلي={fx.HttpSends.Count})");
                Check(fx.HttpSends.Count == 1 &&
                      fx.HttpSends[0].Contains("POST") &&
                      fx.HttpSends[0].Contains("/trade-result") &&
                      fx.HttpSends[0].Contains("pnl_usd=12.5"),
                    $"R: النداء POST /trade-result?pnl_usd=12.5 (الفعلي={(fx.HttpSends.Count == 1 ? fx.HttpSends[0] : "—")})");
                Check(Contains(fx.Bot, "أُرسل PnL الفعلي"), "R: سُجِّل إرسال الـPnL إلى Brain");

                WriteLog(opts, "scenario_R_position_closed_report.log", fx.Bot);
                summary.Add($"R position-closed-report: sends={fx.HttpSends.Count}");
            }

            // ---------------------------------------------------------------------------
            var sb = new System.Text.StringBuilder();
            sb.AppendLine("نتائج سيناريوهات ExecutorHarness");
            sb.AppendLine("=================================");
            foreach (var p in Passes)
            {
                sb.AppendLine("PASS  " + p);
            }
            foreach (var f in Failures)
            {
                sb.AppendLine("FAIL  " + f);
            }
            sb.AppendLine();
            sb.AppendLine($"المجموع: نجح {Passes.Count} | فشل {Failures.Count}");
            sb.AppendLine();
            sb.AppendLine("الملخص:");
            foreach (var s in summary)
            {
                sb.AppendLine("  - " + s);
            }

            var summaryPath = Path.Combine(opts.OutDir, "scenarios_summary.txt");
            File.WriteAllText(summaryPath, sb.ToString());
            Console.WriteLine(sb.ToString());
            Console.WriteLine("كُتب الملخص في: " + summaryPath);

            return Failures.Count == 0 ? 0 : 1;
        }

        // -------------------------------------------------------------------------------
        //  وضع M15: إيقاع شمعة حقيقي
        // -------------------------------------------------------------------------------
        private static int RunM15(Options opts)
        {
            var logPath = Path.Combine(opts.OutDir, "m15_run.log");
            if (File.Exists(logPath))
            {
                File.Delete(logPath);
            }

            var fx = BuildBot(opts, isLive: false, RealTransport, null);
            fx.Bot.OnPrint = line =>
            {
                File.AppendAllText(logPath, line + Environment.NewLine);
                Console.WriteLine(line);
            };

            var header = string.Format(
                CultureInfo.InvariantCulture,
                "# ExecutorHarness M15 run — base={0} symbol={1} cycles={2} intervalSeconds={3} startedUtc={4:O}",
                opts.BaseUrl, opts.Symbol, opts.Cycles, opts.IntervalSeconds, DateTime.UtcNow);
            File.AppendAllText(logPath, header + Environment.NewLine);
            Console.WriteLine(header);

            fx.Bot.TimeInUtc = DateTime.UtcNow;
            fx.Bot.TestStart();

            for (var i = 0; i < opts.Cycles; i++)
            {
                if (i > 0)
                {
                    Thread.Sleep(opts.IntervalSeconds * 1000);
                }

                fx.Bot.TimeInUtc = DateTime.UtcNow;
                fx.Bars.Count = i + 1;
                fx.Bars.RaiseBarClosedForTest();
            }

            fx.Bot.TestStop();

            var footer = string.Format(
                CultureInfo.InvariantCulture,
                "# finishedUtc={0:O} httpCalls={1}",
                DateTime.UtcNow, fx.HttpCalls.Count);
            File.AppendAllText(logPath, footer + Environment.NewLine);
            Console.WriteLine(footer);

            return 0;
        }

        // -------------------------------------------------------------------------------
        //  أدوات مساعدة
        // -------------------------------------------------------------------------------
        private static void Check(bool condition, string description)
        {
            if (condition)
            {
                Passes.Add(description);
            }
            else
            {
                Failures.Add(description);
            }
        }

        private static bool Contains(XauBotExecutor bot, string fragment)
        {
            return bot.Log.Any(l => l.Contains(fragment, StringComparison.Ordinal));
        }

        private static int Count(XauBotExecutor bot, string fragment)
        {
            return bot.Log.Count(l => l.Contains(fragment, StringComparison.Ordinal));
        }

        private static void WriteLog(Options opts, string fileName, XauBotExecutor bot)
        {
            var path = Path.Combine(opts.OutDir, fileName);
            File.WriteAllLines(path, bot.Log);
        }

        private static Options ParseArgs(string[] args)
        {
            var opts = new Options();

            for (var i = 0; i < args.Length; i++)
            {
                var a = args[i];
                string Next()
                {
                    if (i + 1 >= args.Length)
                    {
                        throw new ArgumentException("قيمة مفقودة بعد " + a);
                    }
                    return args[++i];
                }

                switch (a)
                {
                    case "--mode":
                        opts.Mode = Next();
                        break;
                    case "--base-url":
                        opts.BaseUrl = Next();
                        break;
                    case "--symbol":
                        opts.Symbol = Next();
                        break;
                    case "--cycles":
                        opts.Cycles = int.Parse(Next(), CultureInfo.InvariantCulture);
                        break;
                    case "--interval-seconds":
                        opts.IntervalSeconds = int.Parse(Next(), CultureInfo.InvariantCulture);
                        break;
                    case "--out":
                        opts.OutDir = Next();
                        break;
                    default:
                        throw new ArgumentException("وسيط غير معروف: " + a);
                }
            }

            return opts;
        }
    }
}
