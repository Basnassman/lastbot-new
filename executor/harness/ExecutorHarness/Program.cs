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
        public AccountType AccountType { get; set; } = AccountType.Hedged;
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
        // موثّق في cAlgo.API: MinStopLossDistance + MinDistanceType (Pips/Percentage).
        public double MinStopLossDistance = 0.0;
        public SymbolMinDistanceType MinDistanceType = SymbolMinDistanceType.Pips;
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

        // --- تقسيم الصفقة وحماية الأرباح ---
        public AccountType AccountType = AccountType.Hedged;
        public double CommissionPerPosition = 0.0;
        public double SwapPerPosition = 0.0;
        public Func<Position, double?, double?, TradeResult> ModifyResultFactory = null;
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

    internal static class LabelRoles
    {
        public static string Role(string label)
        {
            if (string.IsNullOrEmpty(label))
            {
                return null;
            }

            var parts = label.Split('-');
            return parts.Length == 3 ? parts[2] : null;
        }
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
                MinStopLossDistance = s.Spec.MinStopLossDistance,
                MinDistanceType = s.Spec.MinDistanceType,
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

            fixture.Bot.Account = new TestAccount { IsLive = s.IsLive, AccountType = s.AccountType };
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
                    // ToString().ToUpperInvariant() كي يبقى شكل السجل مطابقاً (POST/PUT…)
                    // بعد تحويل HttpRequest.Method إلى cAlgo.API.HttpMethod الموثّق.
                    fixture.HttpSends.Add(request.Method.ToString().ToUpperInvariant() + " " + request.Uri);
                    return new HttpResponse { StatusCode = 200, IsSuccessful = true, Body = "{\"status\":\"recorded\"}" };
                },
            };
            fixture.Bot.Http = http;

            var nextPositionId = 900000;
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
                    Id = ++nextPositionId,
                    SymbolName = sym,
                    TradeType = tt,
                    VolumeInUnits = vol,
                    EntryPrice = entry,
                    StopLoss = stopLoss,
                    TakeProfit = takeProfit,
                    Label = label,
                    Comment = comment,
                    EntryTime = barStart,
                    Commissions = s.CommissionPerPosition,
                    Swap = s.SwapPerPosition,
                };
                positions.AddForTest(position);

                return new TradeResult { IsSuccessful = true, Position = position, Description = "Order executed" };
            };

            // حقن فشل في تعديل الحماية عند طلبه (سيناريو الرفض). في الحالات الطبيعية يُستعمل
            // السلوك الموثّق داخل الشيم (العكس: ProtectionType.Absolute = أسعار مطلقة).
            if (s.ModifyResultFactory != null)
            {
                fixture.Bot.ModifyExecutor = (position, sl, tp, protectionType) =>
                    s.ModifyResultFactory(position, sl, tp);
            }

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

        private static Position PositionByRole(ExecFixture fx, string role)
        {
            return fx.Positions.FirstOrDefault(p => LabelRoles.Role(p.Label) == role);
        }

        // صافي الربح التقديري عند سعر وقف معيّن بنفس صيغة الـcBot: إجمالي المسافة
        // مضروبة في قيمة الوحدة، ناقص التكاليف المعروفة/المقدّرة. تُستخدم للتحقق من أن
        // التقريب لم يُنزل النتيجة تحت الهدف — وليست ادعاءً بضمان التنفيذ الفعلي.
        private static double EstimatedNetAt(Position position, double stopPrice, Symbol symbol)
        {
            var gross = Math.Abs(stopPrice - position.EntryPrice) / symbol.PipSize * symbol.PipValue *
                        position.VolumeInUnits;
            var costs = 2.0 * Math.Abs(position.Commissions) - position.Swap;
            return gross - costs;
        }

        // محاكاة وصول السوق إلى هدف Position A = 20% من مسافة TP الأصلية (22.5 pip) = 4.5 pip.
        private static void MoveMarketToA(ExecFixture fx, TradeType tradeType)
        {
            var posA = PositionByRole(fx, "A");
            if (posA == null)
            {
                return;
            }

            var distance = 4.5 * fx.Symbol.PipSize;
            var spread = fx.Symbol.PipSize;

            if (tradeType == TradeType.Buy)
            {
                fx.Symbol.Bid = posA.EntryPrice + distance;
                fx.Symbol.Ask = fx.Symbol.Bid + spread;
            }
            else
            {
                fx.Symbol.Ask = posA.EntryPrice - distance;
                fx.Symbol.Bid = fx.Symbol.Ask - spread;
            }
        }

        // إعادة تشغيل الـcBot فوق نفس الحساب (نفس Positions/Bars) — الحالة الداخلية تُفقد.
        private static void RestartBot(ExecFixture fx)
        {
            var previous = fx.Bot;
            previous.TestStop();

            var bot = new XauBotExecutor
            {
                BrainApiHost = previous.BrainApiHost,
                BrainApiPort = previous.BrainApiPort,
                UseHttps = previous.UseHttps,
                BrainApiBasePath = previous.BrainApiBasePath,
                TargetSymbol = previous.TargetSymbol,
                ExecutionEnabled = previous.ExecutionEnabled,
                MaxPositionSizeUsd = previous.MaxPositionSizeUsd,
                RiskRewardRatio = previous.RiskRewardRatio,
                StopLossAtrMultiplier = previous.StopLossAtrMultiplier,
                AtrPeriod = previous.AtrPeriod,
                AtrLookbackBars = previous.AtrLookbackBars,
            };

            bot.Account = previous.Account;
            bot.RunningMode = previous.RunningMode;
            bot.IsBacktesting = previous.IsBacktesting;
            bot.Time = previous.Time;
            bot.TimeInUtc = previous.TimeInUtc;
            bot.Symbol = previous.Symbol;
            bot.Symbols = previous.Symbols;
            bot.Positions = previous.Positions;
            bot.MarketData = previous.MarketData;
            bot.Http = previous.Http;
            bot.OrderExecutor = previous.OrderExecutor;
            bot.ModifyExecutor = previous.ModifyExecutor;

            fx.Bot = bot;
            bot.TestStart();
        }

        // نتيجة تنفيذ ناجحة اصطناعية (لاختبار فشل جزء واحد فقط من الأوامر الثلاثة).
        private static TradeResult Succeed(ExecFixture fx, OrderCall call, int id)
        {
            var entry = call.TradeType == TradeType.Buy ? fx.Symbol.Ask : fx.Symbol.Bid;
            var sign = call.TradeType == TradeType.Buy ? 1 : -1;

            var position = new Position
            {
                Id = id,
                SymbolName = call.SymbolName,
                TradeType = call.TradeType,
                VolumeInUnits = call.Volume,
                EntryPrice = entry,
                StopLoss = entry - sign * call.StopLossPips.GetValueOrDefault() * fx.Symbol.PipSize,
                TakeProfit = entry + sign * call.TakeProfitPips.GetValueOrDefault() * fx.Symbol.PipSize,
                Label = call.Label,
                Comment = call.Comment,
                EntryTime = DateTime.UtcNow,
            };

            fx.Positions.AddForTest(position);
            return new TradeResult { IsSuccessful = true, Position = position, Description = "Order executed" };
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

            // === H: نجاح فتح صفقة شراء — تقسيم إلى ثلاثة مراكز (20%/50%/100%) ======
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0 }; // 10 وحدات
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var orderA = fx.Orders.FirstOrDefault(o => LabelRoles.Role(o.Label) == "A");
                var orderB = fx.Orders.FirstOrDefault(o => LabelRoles.Role(o.Label) == "B");
                var orderC = fx.Orders.FirstOrDefault(o => LabelRoles.Role(o.Label) == "C");

                Check(fx.Orders.Count == 3, $"H: أُرسل 3 أوامر (الفعلي={fx.Orders.Count})");
                Check(orderA != null && orderB != null && orderC != null, "H: المراكز الثلاثة A/B/C موجودة");
                Check(fx.Orders.All(o => o.Label.StartsWith(HarnessConstants.OrderLabel, StringComparison.Ordinal)),
                    "H: كل الأوامر تحمل وسم هذا الـcBot (بادئة XauBotExecutor)");
                Check(fx.Orders.All(o => o.TradeType == TradeType.Buy), "H: كل الأوامر Buy (نفس اتجاه القرار)");
                // ATR=1.0 × 1.5 = 1.5 سعر ÷ PipSize 0.1 = 15 pip — نفس المسافة الأصلية على كل مركز
                Check(fx.Orders.All(o => o.StopLossPips.HasValue && Math.Abs(o.StopLossPips.Value - 15.0) < 1e-6),
                    "H: مسافة SL الأصلية = 15 pip على كل مركز (بلا تغيير)");
                Check(orderA != null && Math.Abs(orderA.TakeProfitPips.GetValueOrDefault() - 4.5) < 1e-9,
                    $"H: TP لـ A = 20% من المسافة الأصلية = 4.5 pip (الفعلي={orderA?.TakeProfitPips})");
                Check(orderB != null && Math.Abs(orderB.TakeProfitPips.GetValueOrDefault() - 11.25) < 1e-9,
                    $"H: TP لـ B = 50% = 11.25 pip (الفعلي={orderB?.TakeProfitPips})");
                Check(orderC != null && Math.Abs(orderC.TakeProfitPips.GetValueOrDefault() - 22.5) < 1e-9,
                    $"H: TP لـ C = 100% من المسافة الأصلية = 22.5 pip (الفعلي={orderC?.TakeProfitPips})");
                Check(orderA != null && Math.Abs(orderA.Volume - 4.0) < 1e-9 &&
                      orderB != null && Math.Abs(orderB.Volume - 3.0) < 1e-9 &&
                      orderC != null && Math.Abs(orderC.Volume - 3.0) < 1e-9,
                    $"H: توزيع 10 وحدات = 4/3/3 (الفعلي={orderA?.Volume}/{orderB?.Volume}/{orderC?.Volume})");
                Check(Math.Abs(fx.Orders.Sum(o => o.Volume) - 10.0) < 1e-9,
                    $"H: مجموع الأحجام = الإجمالي المطلوب 10 (الفعلي={fx.Orders.Sum(o => o.Volume)})");
                Check(Count(fx.Bot, "✅ فُتح المركز") == 3, "H: سُجِّل فتح المراكز الثلاثة مع تفاصيلها");
                Check(Contains(fx.Bot, "SL الفعلي") && Contains(fx.Bot, "TP الفعلي"),
                    "H: سُجِّل SL/TP الفعليان على الخادم");
                Check(fx.Positions.Count == 3, $"H: صار في الحساب 3 مراكز مفتوحة (الفعلي={fx.Positions.Count})");
                Check(fx.HttpGets.Count == 1 && fx.HttpGets[0].Contains("/decide-log-only") &&
                      fx.HttpGets[0].Contains("symbol=" + opts.Symbol) && fx.HttpGets[0].Contains("timeframe=M15"),
                    "H: عقد Brain API لم يتغيّر (نداء واحد إلى /decide-log-only)");
                Check(fx.Bot.ModifyCalls.Count == 0, "H: لا تعديل على المراكز قبل إغلاق Position A");

                WriteLog(opts, "scenario_H_execution_success_buy.log", fx.Bot);
                summary.Add($"H split-buy: orders={fx.Orders.Count}, volumes={string.Join("/", fx.Orders.Select(o => o.Volume.ToString(CultureInfo.InvariantCulture)).ToArray())}");
            }

            // === H2: نجاح فتح صفقة بيع مقسومة ==========================================
            {
                var s = new ExecSettings { Action = "sell", SizeUsd = 15.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 3 && fx.Orders.All(o => o.TradeType == TradeType.Sell),
                    $"H2: أُرسلت ثلاثة أوامر Sell (الفعلي={fx.Orders.Count})");
                Check(fx.Orders.All(o => o.StopLossPips.HasValue && o.TakeProfitPips.HasValue),
                    "H2: SL/TP مرفقان في كل أمر بيع");
                var sellOrderC = fx.Orders.FirstOrDefault(o => LabelRoles.Role(o.Label) == "C");
                Check(sellOrderC != null && Math.Abs(sellOrderC.TakeProfitPips.GetValueOrDefault() - 22.5) < 1e-9,
                    "H2: TP لـ C في البيع = المسافة الأصلية 22.5 pip");

                WriteLog(opts, "scenario_H2_execution_success_sell.log", fx.Bot);
                summary.Add($"H2 split-sell: orders={fx.Orders.Count}");
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

                Check(fx.Orders.Count == 3, $"Q: ثلاثة أوامر فقط لنفس الشمعة (الفعلي={fx.Orders.Count})");
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

                var opened = fx.Positions.FirstOrDefault(p => LabelRoles.Role(p.Label) == "A");
                Check(fx.Positions.Count == 3 && opened != null, "R: فُتحت المراكز الثلاثة قبل الإغلاق");

                // إغلاق Position A على TP (كما تفعل المنصة) — يُطلق حدث Positions.Closed.
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

            // =========================================================================
            //  سيناريوهات تقسيم الصفقة وحماية الأرباح
            //  مواصفات الرمز الاصطناعية: PipSize=0.1، PipValue=0.1، خطوة=1 وحدة،
            //  ATR=1.0 × 1.5 ÷ 0.1 ⇒ SL=15 pip، و TP=22.5 pip، والحجم = المخاطرة ÷ 1.5.
            //  كل 100 وحدة = لوت واحد ⇒ الأرقام في جدول المواصفة (0.04 لوت = 4 وحدات).
            // =========================================================================

            // === S1: جدول توزيع الحجم (يحفظ الإجمالي بالضبط) =========================
            {
                var rows = new[]
                {
                    new { Units = 4.0, Expected = new[] { 2.0, 1.0, 1.0 } },
                    new { Units = 6.0, Expected = new[] { 2.0, 2.0, 2.0 } },
                    new { Units = 7.0, Expected = new[] { 3.0, 2.0, 2.0 } },
                    new { Units = 8.0, Expected = new[] { 4.0, 2.0, 2.0 } },
                    new { Units = 9.0, Expected = new[] { 3.0, 3.0, 3.0 } },
                    new { Units = 10.0, Expected = new[] { 4.0, 3.0, 3.0 } },
                    new { Units = 12.0, Expected = new[] { 4.0, 4.0, 4.0 } },
                };

                var tableSummary = new List<string>();

                foreach (var row in rows)
                {
                    var s = new ExecSettings { Action = "buy", SizeUsd = row.Units * 1.5 };
                    var fx = BuildExecFixture(s);
                    fx.Bot.TestStart();
                    RaiseBar(fx);

                    var volumes = new double[3];
                    var roleNames = new[] { "A", "B", "C" };
                    for (var i = 0; i < 3; i++)
                    {
                        var role = roleNames[i];
                        var order = fx.Orders.FirstOrDefault(o => LabelRoles.Role(o.Label) == role);
                        volumes[i] = order != null ? order.Volume : -1.0;
                    }

                    var countOk = fx.Orders.Count == 3;
                    var volumesOk = Math.Abs(volumes[0] - row.Expected[0]) < 1e-9 &&
                                    Math.Abs(volumes[1] - row.Expected[1]) < 1e-9 &&
                                    Math.Abs(volumes[2] - row.Expected[2]) < 1e-9;

                    Check(countOk && volumesOk,
                        $"S1: توزيع {row.Units} وحدة (= {row.Units / 100.0} لوت) = " +
                        $"{row.Expected[0]}/{row.Expected[1]}/{row.Expected[2]} " +
                        $"(الفعلي={volumes[0]}/{volumes[1]}/{volumes[2]}, الأوامر={fx.Orders.Count})");

                    Check(Math.Abs(fx.Orders.Sum(o => o.Volume) - row.Units) < 1e-9,
                        $"S1: مجموع الأحجام = الإجمالي {row.Units} بلا حذف أو تجاوز");

                    tableSummary.Add($"{row.Units}→{volumes[0]}/{volumes[1]}/{volumes[2]}");
                }

                summary.Add("S1 volume-allocation: " + string.Join(", ", tableSummary.ToArray()));
            }

            // === S2: حالات استحالة التقسيم (حد أدنى/خطوة/حساب غير Hedged) ============
            {
                // (أ) الإجمالي أقل من ثلاث خطوات
                var small = BuildExecFixture(new ExecSettings { Action = "buy", SizeUsd = 3.0 });
                small.Bot.TestStart();
                RaiseBar(small);
                Check(small.Orders.Count == 0, $"S2a: لا أمر عندما يستحيل تقسيم الحجم (الفعلي={small.Orders.Count})");
                Check(Contains(small.Bot, "أقل من ثلاث خطوات"), "S2a: سُجِّل القيد الفعلي المانع للتقسيم");
                Check(Contains(small.Bot, "لم يُرسل أي أمر"), "S2a: سُجِّل عدم إرسال أي أمر بلا تقريب/حذف");
                Check(Contains(small.Bot, "البدائل المطلوبة"), "S2a: سُجِّلت البدائل المطلوبة من مالك المشروع");

                // (ب) الحد الأدنى للمركز (2 وحدات) يمنع توزيع 4 وحدات = 2/1/1
                var minCase = BuildExecFixture(new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 6.0,
                    Spec = new SymbolSpec { Min = 2.0 },
                });
                minCase.Bot.TestStart();
                RaiseBar(minCase);
                Check(minCase.Orders.Count == 0, "S2b: لا أمر عندما يكون حجم أحد المراكز تحت الحد الأدنى");
                Check(Contains(minCase.Bot, "أقل من الحد الأدنى"), "S2b: سُجِّل قيد الحد الأدنى بالتفصيل");

                // (ج) خطوة غير صحيحة (0.5) مع 7 وحدات ⇒ 3/2/2
                var stepCase = BuildExecFixture(new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 10.5,
                    Spec = new SymbolSpec { Step = 0.5 },
                });
                stepCase.Bot.TestStart();
                RaiseBar(stepCase);
                Check(stepCase.Orders.Count == 3 &&
                      Math.Abs(stepCase.Orders.Sum(o => o.Volume) - 7.0) < 1e-9,
                    $"S2c: خطوة 0.5 تحفظ الإجمالي 7 (الأوامر={stepCase.Orders.Count})");
                Check(stepCase.Orders.Count == 3 &&
                      Math.Abs(stepCase.Orders.First(o => LabelRoles.Role(o.Label) == "A").Volume - 3.0) < 1e-9,
                    "S2c: الباقي كله إلى A (3/2/2)");

                // (د) حساب Netted لا يقبل ثلاثة مراكز مستقلة
                var netted = BuildExecFixture(new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 15.0,
                    AccountType = AccountType.Netted,
                });
                netted.Bot.TestStart();
                RaiseBar(netted);
                Check(netted.Orders.Count == 0, "S2d: لا أمر على حساب Netted (لا تقسيم ممكن)");
                Check(Contains(netted.Bot, "وليس Hedged"), "S2d: سُجِّل نوع الحساب المانع");

                summary.Add($"S2 allocation-guards: small={small.Orders.Count}, min={minCase.Orders.Count}, " +
                            $"step={stepCase.Orders.Count}, netted={netted.Orders.Count}");
            }

            // === S3: الحماية بعد تأكيد إغلاق A عند هدفه (شراء + تكاليف + استقلال) ===
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, CommissionPerPosition = 0.05 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");
                var tpB = posB.TakeProfit;
                var tpC = posC.TakeProfit;
                var originalSlB = posB.StopLoss;

                // سواب سالب على C فقط ⇒ تعادل مختلف عن B (إدارة مستقلة لكل مركز).
                posC.Swap = -0.20;

                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                var callB = fx.Bot.ModifyCalls.FirstOrDefault(c => c.PositionId == posB.Id);
                var callC = fx.Bot.ModifyCalls.FirstOrDefault(c => c.PositionId == posC.Id);

                Check(fx.Bot.ModifyCalls.Count == 2, $"S3: تعديلان فقط (B و C) بعد إغلاق A (الفعلي={fx.Bot.ModifyCalls.Count})");
                // C's TP profit = 2.25 × 3 × (0.1/0.1) = $6.75 ⇒ target net = $0.0675.
                // B: تكاليف 0.05×2 = 0.10 ⇒ إجمالي 0.1675 على 3 وحدات ⇒ 0.05583 سعر ⇒ تقريب لأعلى ⇒ 2000.16
                Check(callB != null && Math.Abs(callB.StopLoss.GetValueOrDefault() - 2000.16) < 1e-9,
                    $"S3: وقف B يحقق صافي 0.0675 مع العمولة = 2000.16 (الفعلي={callB?.StopLoss})");
                // C: + سواب -0.20 ⇒ تكاليف 0.30 ⇒ إجمالي 0.3675 ⇒ 0.1225 سعر ⇒ 2000.23
                Check(callC != null && Math.Abs(callC.StopLoss.GetValueOrDefault() - 2000.23) < 1e-9,
                    $"S3: وقف C يحقق نفس الصافي مع العمولة والسواب = 2000.23 (الفعلي={callC?.StopLoss})");
                Check(Contains(fx.Bot, "ربح C المتوقع عند TP=$6.75") && Contains(fx.Bot, "= $0.0675 (1%"),
                    "S3: سُجِّل ربح C المرجعي وهدف 1% الصافي");
                Check(EstimatedNetAt(posB, posB.StopLoss.GetValueOrDefault(), fx.Symbol) >= 0.0675 - 1e-9 &&
                      EstimatedNetAt(posC, posC.StopLoss.GetValueOrDefault(), fx.Symbol) >= 0.0675 - 1e-9,
                    "S3: صافي B و C التقديري عند الوقف ≥ هدف 1% (التقريب لم يُنزل تحته)");
                Check(callB != null && callB.ProtectionType == ProtectionType.Absolute,
                    "S3: التعديل بأسعار مطلقة (ProtectionType.Absolute)");
                Check(callB != null && callB.TakeProfit == tpB && callC != null && callC.TakeProfit == tpC,
                    "S3: هدف B و C مُمرَّر كما هو في طلب التعديل (لا فقدان للهدف)");
                Check(posB.TakeProfit == tpB && posC.TakeProfit == tpC, "S3: هدف 50%/100% لم يتغيّر على الخادم");
                Check(posB.StopLoss.GetValueOrDefault() > posB.EntryPrice &&
                      posC.StopLoss.GetValueOrDefault() > posC.EntryPrice,
                    "S3: وقف الشراء أصبح أعلى من الدخول (تغطية التكاليف)");
                Check(posB.StopLoss != originalSlB, "S3: وقف B لم يبقَ الأصلي (نُقل للتعادل)");
                Check(fx.Orders.Count == 3, "S3: لا أوامر جديدة بعد الحماية");
                Check(Contains(fx.Bot, "✅ المركز B") && Contains(fx.Bot, "✅ المركز C"),
                    "S3: سُجِّل نجاح حماية B و C");
                Check(!Contains(fx.Bot, "لم تكتمل الحماية"), "S3: لم يُسجَّل أي فشل حماية");

                WriteLog(opts, "scenario_S3_protection_buy_costs.log", fx.Bot);
                summary.Add($"S3 protect-buy: modifies={fx.Bot.ModifyCalls.Count}, SL_B={posB.StopLoss}, SL_C={posC.StopLoss}");
            }

            // === S4: الحماية لا تُفعَّل قبل تأكيد إغلاق A عند هدفه ==================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);
                RaiseBar(fx);

                Check(fx.Bot.ModifyCalls.Count == 0, "S4: لا حماية قبل أي إغلاق");

                var posB = PositionByRole(fx, "B");
                var posA = PositionByRole(fx, "A");
                MoveMarketToA(fx, TradeType.Buy);

                // مجرد وصول السوق إلى هدف A لا يفعّل الحماية — التفعيل يتطلب حدث إغلاق فعلي.
                Check(fx.Bot.ModifyCalls.Count == 0, "S4: لمس السعر لهدف A بلا إغلاق لا يفعّل الحماية");

                fx.Positions.CloseForTest(posB, netProfit: 2.0, reason: PositionCloseReason.TakeProfit);
                Check(fx.Bot.ModifyCalls.Count == 0, "S4: إغلاق B عند هدفه لا يفعّل الحماية");
                Check(Contains(fx.Bot, "ليس Position A"), "S4: سُجِّل أن المركز المغلق ليس A");

                fx.Positions.CloseForTest(posA, netProfit: 1.0, reason: PositionCloseReason.Closed);
                Check(fx.Bot.ModifyCalls.Count == 0, "S4: إغلاق A يدوياً (ليس TP) لا يفعّل الحماية");
                Check(Contains(fx.Bot, "وليس TakeProfit"), "S4: سُجِّل سبب عدم التفعيل");
                Check(fx.Orders.Count == 3, "S4: لا أوامر جديدة");

                WriteLog(opts, "scenario_S4_no_premature_protection.log", fx.Bot);
                summary.Add($"S4 no-premature-protection: modifies={fx.Bot.ModifyCalls.Count}");
            }

            // === S5: اتجاه وقف الحماية للبيع =========================================
            {
                var s = new ExecSettings { Action = "sell", SizeUsd = 15.0, CommissionPerPosition = 0.05 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");

                MoveMarketToA(fx, TradeType.Sell);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                Check(fx.Bot.ModifyCalls.Count == 2, "S5: تعديلان بعد إغلاق A في البيع");
                Check(posB.StopLoss.GetValueOrDefault() < posB.EntryPrice &&
                      posC.StopLoss.GetValueOrDefault() < posC.EntryPrice,
                    "S5: وقف SELL أسفل سعر الدخول (تغطية التكاليف)");
                Check(Math.Abs(posB.StopLoss.GetValueOrDefault() - 1999.94) < 1e-9,
                    $"S5: وقف B للبيع يحقق صافي 0.0675 = 1999.94 (الفعلي={posB.StopLoss})");
                Check(EstimatedNetAt(posB, posB.StopLoss.GetValueOrDefault(), fx.Symbol) >= 0.0675 - 1e-9,
                    "S5: صافي B التقديري عند الوقف ≥ هدف 1% للبيع");
                Check(posB.StopLoss.GetValueOrDefault() > fx.Symbol.Ask,
                    "S5: الوقف أعلى من Ask الحالي ⇒ جهة صحيحة للبيع");
                Check(posB.TakeProfit.HasValue && posC.TakeProfit.HasValue,
                    "S5: أهداف البيع محفوظة بعد التعديل");

                WriteLog(opts, "scenario_S5_protection_sell.log", fx.Bot);
                summary.Add($"S5 protect-sell: SL_B={posB.StopLoss}, Ask={fx.Symbol.Ask}");
            }

            // === S6: تعادل غير صالح (في الجهة الخاطئة من السوق) =====================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, CommissionPerPosition = 0.05 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");
                var slB = posB.StopLoss;
                var slC = posC.StopLoss;

                // السوق لم يتحرك: وقف الحماية (2000.16) أعلى من Bid (2000.0) ⇒ طلب غير صالح.
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                Check(fx.Bot.ModifyCalls.Count == 0, "S6: لم يُرسل أي تعديل غير صالح للمنصة");
                Check(Contains(fx.Bot, "الجهة الخاطئة"), "S6: سُجِّل سبب رفض وقف التعادل");
                Check(posB.StopLoss == slB && posC.StopLoss == slC, "S6: الوقفان الأصليان بقيا كما هما");
                Check(!Contains(fx.Bot, "✅ المركز B"), "S6: لم يُدَّعَ نجاح الحماية");
                Check(Contains(fx.Bot, "لم تكتمل الحماية"), "S6: سُجِّل فشل الحماية بوضوح");

                WriteLog(opts, "scenario_S6_invalid_protective_side.log", fx.Bot);
                summary.Add("S6 invalid-protective-side: no modify, original SL kept");
            }

            // === S7: رفض المنصة لتعديل وقف الحماية ===================================
            {
                var s = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 15.0,
                    CommissionPerPosition = 0.05,
                    ModifyResultFactory = (position, sl, tp) => new TradeResult
                    {
                        IsSuccessful = false,
                        Error = ErrorCode.InvalidStopLoss,
                        Description = "Invalid stop loss",
                    },
                };
                var fx = BuildExecFixture(s);

                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");
                var slB = posB.StopLoss;
                var slC = posC.StopLoss;

                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                Check(fx.Bot.ModifyCalls.Count == 2, "S7: حُوِّل تعديلان للمنصة");
                Check(Contains(fx.Bot, "رُفض تعديل"), "S7: سُجِّل رفض المنصة للتعديل");
                Check(Contains(fx.Bot, "InvalidStopLoss"), "S7: سُجِّل رمز خطأ المنصة");
                Check(posB.StopLoss == slB && posC.StopLoss == slC, "S7: الوقفان الأصليان بقيا كما هما");
                Check(!Contains(fx.Bot, "✅ المركز B"), "S7: لا ادعاء بنجاح الحماية");
                Check(Contains(fx.Bot, "لم تكتمل الحماية"), "S7: سُجِّل فشل الحماية");

                WriteLog(opts, "scenario_S7_modify_rejected.log", fx.Bot);
                summary.Add("S7 modify-rejected: logged, SL unchanged, no false success");
            }

            // === S8: أحداث إغلاق مكرّرة + إعادة النداء =============================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, CommissionPerPosition = 0.05 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                Check(fx.Bot.ModifyCalls.Count == 2, "S8: تعديلان بعد الإغلاق الأول");

                // نفس الحدث يُطلق مرة أخرى (إعادة اتصال / استدعاء متكرر).
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                Check(fx.Bot.ModifyCalls.Count == 2, "S8: حدث إغلاق مكرّر لا يُنتج تعديلاً إضافياً");
                Check(Contains(fx.Bot, "مكرّر"), "S8: سُجِّل تجاهل الحدث المكرّر");

                RaiseBar(fx);
                Check(fx.Bot.ModifyCalls.Count == 2 && fx.Orders.Count == 3,
                    "S8: إعادة النداء/الشمعة لا تُكرّر الحماية ولا تفتح أوامر");

                WriteLog(opts, "scenario_S8_duplicate_close_events.log", fx.Bot);
                summary.Add($"S8 duplicate-close: modifies={fx.Bot.ModifyCalls.Count}, orders={fx.Orders.Count}");
            }

            // === S9: فشل جزئي في إرسال الأوامر =======================================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, CommissionPerPosition = 0.05 };
                var fx = BuildExecFixture(s);
                var nextId = 800000;

                // A ينجح، B يرفضه الوسيط، C لا يُرسل إطلاقاً.
                s.OrderResultFactory = call => LabelRoles.Role(call.Label) == "B"
                    ? new TradeResult
                    {
                        IsSuccessful = false,
                        Error = ErrorCode.TechnicalError,
                        Description = "Rejected by broker",
                    }
                    : Succeed(fx, call, ++nextId);

                fx.Bot.TestStart();
                RaiseBar(fx);

                Check(fx.Orders.Count == 2, $"S9: حُوِّل أمران فقط (A ثم B) (الفعلي={fx.Orders.Count})");
                Check(fx.Positions.Count == 1, $"S9: فُتح مركز واحد فقط (الفعلي={fx.Positions.Count})");
                Check(Contains(fx.Bot, "حالة جزئية"), "S9: سُجِّلت الحالة الجزئية بوضوح");
                Check(Contains(fx.Bot, "لن تُفتح أي أوامر بديلة"), "S9: سُجِّل عدم فتح أوامر بديلة");
                Check(!Contains(fx.Bot, "اكتمل التقسيم"), "S9: لم يُدَّعَ اكتمال التقسيم");

                // إغلاق A لاحقاً: لا B/C لحمایتهما، ولا أمر تعويضي.
                var posA = PositionByRole(fx, "A");
                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 1.5, reason: PositionCloseReason.TakeProfit);

                Check(fx.Bot.ModifyCalls.Count == 0, "S9: لا تعديل حماية عند عدم وجود B/C");
                Check(Contains(fx.Bot, "لم يُفتح أصلاً"), "S9: سُجِّل أن B/C لم تُفتح");
                Check(fx.Orders.Count == 2, "S9: لم يُفتح أي أمر تعويضي بعد الفشل");

                WriteLog(opts, "scenario_S9_partial_placement.log", fx.Bot);
                summary.Add($"S9 partial-placement: attempts={fx.Orders.Count}, open={fx.Positions.Count}, modifies={fx.Bot.ModifyCalls.Count}");
            }

            // === S10: إعادة التشغيل مع بقاء A/B/C مفتوحة =============================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, CommissionPerPosition = 0.05 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);
                Check(fx.Positions.Count == 3, "S10: ثلاثة مراكز مفتوحة قبل إعادة التشغيل");

                RestartBot(fx);
                Check(Contains(fx.Bot, "[استعادة]") && Contains(fx.Bot, "Position A مفتوح"),
                    "S10: استُعيدت المجموعة من المراكز المفتوحة بعد إعادة التشغيل");
                Check(fx.Orders.Count == 3, "S10: لا أوامر جديدة عند إعادة التشغيل");

                RaiseBar(fx);
                Check(fx.Orders.Count == 3, "S10: لا أوامر جديدة بعد إعادة التشغيل");
                Check(Contains(fx.Bot, "مركز مفتوح مسبقاً"), "S10: منع الدخول لوجود مراكز مفتوحة");

                var posA = PositionByRole(fx, "A");
                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                Check(fx.Bot.ModifyCalls.Count == 2, $"S10: الحماية عملت بعد إعادة التشغيل (الفعلي={fx.Bot.ModifyCalls.Count})");
                Check(Contains(fx.Bot, "✅ المركز B"), "S10: سُجِّل نجاح حماية B بعد إعادة التشغيل");

                WriteLog(opts, "scenario_S10_restart_positions_open.log", fx.Bot);
                summary.Add($"S10 restart-open: modifies={fx.Bot.ModifyCalls.Count}, orders={fx.Orders.Count}");
            }

            // === S11: إعادة التشغيل وبقاء B/C فقط (A أُغلق أثناء التوقّف) ==========
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, CommissionPerPosition = 0.05 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");
                var slB = posB.StopLoss;
                var slC = posC.StopLoss;

                // A اختفى بلا حدث إغلاق (أُغلق والـcBot متوقف) ⇒ لا دليل على أنه أُغلق عند هدفه.
                fx.Positions.RemoveForTest(posA);

                RestartBot(fx);

                Check(Contains(fx.Bot, "لا دليل"), "S11: سُجِّل أن حالة A غير معروفة بعد إعادة التشغيل");
                Check(Contains(fx.Bot, "يلزم تحقق يدوي"), "S11: طُلب تحقق يدوي من سجل الحساب");
                Check(fx.Bot.ModifyCalls.Count == 0, "S11: لم تُعدَّل أوقاف B/C بلا دليل على إغلاق A عند هدفه");
                Check(posB.StopLoss == slB && posC.StopLoss == slC, "S11: الوقفان الأصليان بقيا كما هما");
                Check(fx.Orders.Count == 3, "S11: لا أوامر جديدة");

                WriteLog(opts, "scenario_S11_restart_missing_a.log", fx.Bot);
                summary.Add("S11 restart-missing-A: no blind modification, manual review flagged");
            }

            // === S12: إعادة التشغيل بعد حماية ناجحة (لا تعديل مكرّر) ================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, CommissionPerPosition = 0.05 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                Check(fx.Bot.ModifyCalls.Count == 2, "S12: تمت الحماية قبل إعادة التشغيل");

                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");
                var protectedSlB = posB.StopLoss;
                var protectedSlC = posC.StopLoss;

                RestartBot(fx);

                Check(Contains(fx.Bot, "وقف حماية سابق"), "S12: اكتُشف أن B/C تحت وقف حماية سابق بعد إعادة التشغيل");
                Check(fx.Bot.ModifyCalls.Count == 0, "S12: لا تعديل مكرّر بعد إعادة التشغيل");
                Check(posB.StopLoss == protectedSlB && posC.StopLoss == protectedSlC, "S12: وقف الحماية محفوظ");

                WriteLog(opts, "scenario_S12_restart_already_protected.log", fx.Bot);
                summary.Add("S12 restart-already-protected: no repeat modification");
            }

            // === S13: حدث إغلاق متأخر لـ A بعد إعادة التشغيل ======================
            {
                var s = new ExecSettings { Action = "buy", SizeUsd = 15.0, CommissionPerPosition = 0.05 };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var labelA = posA.Label;
                var entryA = posA.EntryPrice;
                var tpA = posA.TakeProfit;

                // A يختفي بلا حدث (أُغلق والـcBot متوقف) ثم يُعاد التشغيل.
                fx.Positions.RemoveForTest(posA);
                RestartBot(fx);
                Check(fx.Bot.ModifyCalls.Count == 0, "S13: لا تعديل قبل وصول الحدث المتأخر");

                // الحدث المتأخر يصل بعد إعادة التشغيل (نفس وسم المركز ومعرّفه).
                var lateA = new Position
                {
                    Id = posA.Id,
                    SymbolName = posA.SymbolName,
                    TradeType = posA.TradeType,
                    VolumeInUnits = posA.VolumeInUnits,
                    EntryPrice = entryA,
                    TakeProfit = tpA,
                    Label = labelA,
                    EntryTime = posA.EntryTime,
                };

                fx.Symbol.Bid = entryA + 4.5 * fx.Symbol.PipSize;
                fx.Symbol.Ask = fx.Symbol.Bid + fx.Symbol.PipSize;
                fx.Positions.CloseForTest(lateA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                Check(fx.Bot.ModifyCalls.Count == 2, $"S13: الحدث المتأخر فعّل الحماية (الفعلي={fx.Bot.ModifyCalls.Count})");
                Check(posB.StopLoss.GetValueOrDefault() > posB.EntryPrice, "S13: وقف B نُقل لوقف حماية فوق الدخول بعد الحدث المتأخر");

                // حدث مكرّر لنفس المركز المتأخر: لا تعديل إضافي.
                fx.Positions.CloseForTest(lateA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                Check(fx.Bot.ModifyCalls.Count == 2, "S13: لا تعديل مكرّر عند تكرار الحدث المتأخر");

                WriteLog(opts, "scenario_S13_delayed_close_event.log", fx.Bot);
                summary.Add($"S13 delayed-close-after-restart: modifies={fx.Bot.ModifyCalls.Count}");
            }

            // === S14: ربح C المتوقع $10 ⇒ $0.10 صافي لكل من B و C ================
            {
                var s = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");
                var tpB = posB.TakeProfit;
                var tpC = posC.TakeProfit;
                var originalSlB = posB.StopLoss;
                var originalSlC = posC.StopLoss;

                // C: حجم 4، مسافة TP = 2.5 سعر ⇒ الربح المتوقع عند TP = 10.00 USD.
                Check(Math.Abs(posC.VolumeInUnits - 4.0) < 1e-9,
                    $"S14: حجم C = 4 (الفعلي={posC.VolumeInUnits})");
                var cExpected = Math.Abs(tpC.GetValueOrDefault() - posC.EntryPrice) / fx.Symbol.PipSize *
                                fx.Symbol.PipValue * posC.VolumeInUnits;
                Check(Math.Abs(cExpected - 10.0) < 1e-6,
                    $"S14: ربح C المتوقع عند TP = $10 (الفعلي={cExpected})");

                // القرار الأصلي لم يتغيّر: 3 أوامر بأحجام 4/4/4 وأهداف 5/12.5/25 pip.
                Check(fx.Orders.Count == 3, "S14: ثلاثة أوامر قبل الحماية");
                Check(Math.Abs(fx.Orders[0].Volume - 4.0) < 1e-9 &&
                      Math.Abs(fx.Orders[1].Volume - 4.0) < 1e-9 &&
                      Math.Abs(fx.Orders[2].Volume - 4.0) < 1e-9,
                    "S14: أحجام الأوامر الأصلية 4/4/4 بلا تغيير");
                Check(Math.Abs(fx.Orders[0].TakeProfitPips.GetValueOrDefault() - 5.0) < 1e-6 &&
                      Math.Abs(fx.Orders[1].TakeProfitPips.GetValueOrDefault() - 12.5) < 1e-6 &&
                      Math.Abs(fx.Orders[2].TakeProfitPips.GetValueOrDefault() - 25.0) < 1e-6,
                    "S14: مسافات TP الأصلية 20%/50%/100% بلا تغيير");

                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                var callB = fx.Bot.ModifyCalls.FirstOrDefault(c => c.PositionId == posB.Id);
                var callC = fx.Bot.ModifyCalls.FirstOrDefault(c => c.PositionId == posC.Id);

                Check(fx.Bot.ModifyCalls.Count == 2, "S14: تعديلان فقط (B و C)");
                Check(Contains(fx.Bot, "ربح C المتوقع عند TP=$10") && Contains(fx.Bot, "= $0.1 (1%"),
                    "S14: $10 → هدف صافي $0.10 (1% ديناميكي)");
                // لكل من B و C: (0.10 هدف + 0.10 عمولة×2) / 4 وحدة = 0.05 سعر ⇒ 2000.15
                Check(callB != null && Math.Abs(callB.StopLoss.GetValueOrDefault() - 2000.15) < 1e-9,
                    $"S14: وقف B = 2000.15 (الفعلي={callB?.StopLoss})");
                Check(callC != null && Math.Abs(callC.StopLoss.GetValueOrDefault() - 2000.15) < 1e-9,
                    $"S14: وقف C = 2000.15 (الفعلي={callC?.StopLoss})");
                Check(EstimatedNetAt(posB, posB.StopLoss.GetValueOrDefault(), fx.Symbol) >= 0.10 - 1e-9,
                    "S14: صافي B عند الوقف ≥ 0.10");
                Check(EstimatedNetAt(posC, posC.StopLoss.GetValueOrDefault(), fx.Symbol) >= 0.10 - 1e-9,
                    "S14: صافي C عند الوقف ≥ 0.10");
                Check(posB.TakeProfit == tpB && posC.TakeProfit == tpC,
                    "S14: أهداف B (50%) و C (100%) لم تتغيّر");
                Check(posB.StopLoss != originalSlB && posC.StopLoss != originalSlC,
                    "S14: الأوقاف الأصلية استُبدلت بأوقاف الحماية");

                WriteLog(opts, "scenario_S14_protection_10_to_010.log", fx.Bot);
                summary.Add($"S14 target-10→0.10: SL_B={posB.StopLoss}, SL_C={posC.StopLoss}");
            }

            // === S15: ربح C المتوقع $25 ⇒ $0.25 صافي لكل من B و C ================
            {
                var s = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");

                // مسافة هدف C الأصلية 6.25 سعر مع حجم 4 ⇒ 25.00 USD عند TP.
                posC.TakeProfit = posC.EntryPrice + 6.25;
                var tpB = posB.TakeProfit;
                var tpC = posC.TakeProfit;

                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                var callB = fx.Bot.ModifyCalls.FirstOrDefault(c => c.PositionId == posB.Id);
                var callC = fx.Bot.ModifyCalls.FirstOrDefault(c => c.PositionId == posC.Id);

                Check(Contains(fx.Bot, "ربح C المتوقع عند TP=$25") && Contains(fx.Bot, "= $0.25 (1%"),
                    "S15: $25 → هدف صافي $0.25 (1% ديناميكي)");
                // (0.25 + 0.10) / 4 = 0.0875 ⇒ 2000.1875 ⇒ تقريب لأعلى إلى 0.01 ⇒ 2000.19
                Check(callB != null && Math.Abs(callB.StopLoss.GetValueOrDefault() - 2000.19) < 1e-9,
                    $"S15: وقف B = 2000.19 بعد تقريب لأعلى (الفعلي={callB?.StopLoss})");
                Check(callC != null && Math.Abs(callC.StopLoss.GetValueOrDefault() - 2000.19) < 1e-9,
                    $"S15: وقف C = 2000.19 (الفعلي={callC?.StopLoss})");
                Check(EstimatedNetAt(posB, posB.StopLoss.GetValueOrDefault(), fx.Symbol) >= 0.25 - 1e-9,
                    "S15: التقريب لأعلى لم يُنزل صافي B تحت 0.25");
                Check(posB.TakeProfit == tpB && posC.TakeProfit == tpC, "S15: TP B/C محفوظان");

                WriteLog(opts, "scenario_S15_protection_25_to_025.log", fx.Bot);
                summary.Add($"S15 target-25→0.25: SL_B={posB.StopLoss}, SL_C={posC.StopLoss}");
            }

            // === S16: نفس الهدف النقدي مع أحجام مختلفة ⇒ وقفان مستقلان =============
            {
                var s = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");

                // حجم C = 8 (بدل 4) ⇒ ربحه المتوقع 2.5 × 8 = 20 ⇒ الهدف 0.20 لكل من B و C.
                posC.VolumeInUnits = 8.0;

                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                var callB = fx.Bot.ModifyCalls.FirstOrDefault(c => c.PositionId == posB.Id);
                var callC = fx.Bot.ModifyCalls.FirstOrDefault(c => c.PositionId == posC.Id);

                Check(Contains(fx.Bot, "= $0.2 (1%"),
                    "S16: الهدف النقدي المشترك = 0.20 لكل من B و C");
                // B: (0.20 + 0.10) / 4 = 0.075 ⇒ 2000.175 ⇒ 2000.18
                Check(callB != null && Math.Abs(callB.StopLoss.GetValueOrDefault() - 2000.18) < 1e-9,
                    $"S16: وقف B (حجم 4) = 2000.18 (الفعلي={callB?.StopLoss})");
                // C: (0.20 + 0.10) / 8 = 0.0375 ⇒ 2000.1375 ⇒ 2000.14
                Check(callC != null && Math.Abs(callC.StopLoss.GetValueOrDefault() - 2000.14) < 1e-9,
                    $"S16: وقف C (حجم 8) = 2000.14 (الفعلي={callC?.StopLoss})");
                Check(callB != null && callC != null &&
                      Math.Abs(callB.StopLoss.GetValueOrDefault() - callC.StopLoss.GetValueOrDefault()) > 1e-6,
                    "S16: الوقفان مختلفان رغم وحدة الهدف النقدي");
                Check(EstimatedNetAt(posB, posB.StopLoss.GetValueOrDefault(), fx.Symbol) >= 0.20 - 1e-9 &&
                      EstimatedNetAt(posC, posC.StopLoss.GetValueOrDefault(), fx.Symbol) >= 0.20 - 1e-9,
                    "S16: صافي كل مركز ≥ 0.20 رغم اختلاف الأحجام");

                WriteLog(opts, "scenario_S16_protection_different_volumes.log", fx.Bot);
                summary.Add($"S16 different-volumes: SL_B={posB.StopLoss}, SL_C={posC.StopLoss}");
            }

            // === S17: البيع — $25 → $0.25 وتقريب السعر لأسفل =======================
            {
                var s = new ExecSettings
                {
                    Action = "sell",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");
                posC.TakeProfit = posC.EntryPrice - 6.25; // 25.00 USD على حجم 4
                var tpB = posB.TakeProfit;
                var tpC = posC.TakeProfit;

                MoveMarketToA(fx, TradeType.Sell);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                Check(Contains(fx.Bot, "= $0.25 (1%"), "S17: هدف SELL الصافي = 0.25");
                Check(Math.Abs(posB.StopLoss.GetValueOrDefault() - 1999.91) < 1e-9,
                    $"S17: وقف B للبيع = 1999.91 (الفعلي={posB.StopLoss})");
                Check(posB.StopLoss.GetValueOrDefault() < posB.EntryPrice &&
                      posC.StopLoss.GetValueOrDefault() < posC.EntryPrice,
                    "S17: وقف SELL أسفل الدخول وفي جهة الربح");
                Check(posB.StopLoss.GetValueOrDefault() > fx.Symbol.Ask,
                    "S17: الوقف أعلى من Ask الحالي ⇒ جهة صحيحة للبيع");
                Check(EstimatedNetAt(posB, posB.StopLoss.GetValueOrDefault(), fx.Symbol) >= 0.25 - 1e-9,
                    "S17: تقريب لأسفل لم يُنزل صافي SELL تحت 0.25");
                Check(posB.TakeProfit == tpB && posC.TakeProfit == tpC, "S17: TP B/C محفوظان");

                WriteLog(opts, "scenario_S17_protection_sell_25.log", fx.Bot);
                summary.Add($"S17 sell-target: SL_B={posB.StopLoss}, Ask={fx.Symbol.Ask}");
            }

            // === S18: إشارة العمولة (موجب/سالب) والتأثير المتوقّع للسواب ==========
            {
                var baseSettings = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };
                var positive = BuildExecFixture(baseSettings);
                positive.Bot.TestStart();
                RaiseBar(positive);
                MoveMarketToA(positive, TradeType.Buy);
                positive.Positions.CloseForTest(PositionByRole(positive, "A"), netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                var negativeSettings = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = -0.05,
                };
                var negative = BuildExecFixture(negativeSettings);
                negative.Bot.TestStart();
                RaiseBar(negative);
                MoveMarketToA(negative, TradeType.Buy);
                negative.Positions.CloseForTest(PositionByRole(negative, "A"), netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                var posBp = PositionByRole(positive, "B");
                var posBn = PositionByRole(negative, "B");
                Check(Math.Abs(posBp.StopLoss.GetValueOrDefault() - posBn.StopLoss.GetValueOrDefault()) < 1e-9,
                    $"S18: إشارة العمولة لا تغيّر الوقف (موجب={posBp.StopLoss}، سالب={posBn.StopLoss})");
                Check(Math.Abs(posBp.StopLoss.GetValueOrDefault() - 2000.15) < 1e-9,
                    "S18: العمولة تُحسب تكلفةً في الحالتين (2000.15)");

                // سواب سالب (تكلفة) على C يرفع وقفه بالمقدار المعروف: 0.20/4 = 0.05 سعر.
                var debit = BuildExecFixture(baseSettings);
                debit.Bot.TestStart();
                RaiseBar(debit);
                var debitC = PositionByRole(debit, "C");
                debitC.Swap = -0.20;
                MoveMarketToA(debit, TradeType.Buy);
                debit.Positions.CloseForTest(PositionByRole(debit, "A"), netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                var debitB = PositionByRole(debit, "B");
                Check(Math.Abs(debitB.StopLoss.GetValueOrDefault() - 2000.15) < 1e-9,
                    $"S18: سواب C لا يغيّر وقف B (الفعلي={debitB.StopLoss})");
                Check(Math.Abs(debitC.StopLoss.GetValueOrDefault() - 2000.20) < 1e-9,
                    $"S18: سواب سالب -0.20 على C يرفع وقفه إلى 2000.20 (الفعلي={debitC.StopLoss})");
                Check(EstimatedNetAt(debitC, debitC.StopLoss.GetValueOrDefault(), debit.Symbol) >= 0.10 - 1e-9,
                    "S18: صافي C بعد السواب السالب ≥ 0.10");

                // سواب موجب (رصيد دائن) يقلّل الإجمالي المطلوب؛ وعندما يتجاوز الهدف +
                // التكاليف يصبح الوقف عند الدخول، مع بقاء الصافي ≥ الهدف.
                var credit = BuildExecFixture(baseSettings);
                credit.Bot.TestStart();
                RaiseBar(credit);
                var creditC = PositionByRole(credit, "C");
                creditC.Swap = 0.20;
                MoveMarketToA(credit, TradeType.Buy);
                credit.Positions.CloseForTest(PositionByRole(credit, "A"), netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                Check(Math.Abs(creditC.StopLoss.GetValueOrDefault() - creditC.EntryPrice) < 1e-9,
                    $"S18: سواب دائن 0.20 ⇒ الوقف عند الدخول (الفعلي={creditC.StopLoss})");
                Check(EstimatedNetAt(creditC, creditC.StopLoss.GetValueOrDefault(), credit.Symbol) >= 0.10 - 1e-9,
                    "S18: الصافي مع السواب الدائن ≥ 0.10");

                WriteLog(opts, "scenario_S18_commission_sign_swap.log", positive.Bot);
                summary.Add($"S18 sign/swap: pos={posBp.StopLoss}, neg={posBn.StopLoss}, " +
                            $"swapDebitC={debitC.StopLoss}, swapCreditC={creditC.StopLoss}");
            }

            // === S19: الانتشار لا يُضاف مرتين ======================================
            {
                var narrow = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };
                var wide = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };
                wide.Spec.Bid = 1999.8; // انتشار 0.30 بدل 0.10 — نفس Ask/الدخول

                var fxNarrow = BuildExecFixture(narrow);
                var fxWide = BuildExecFixture(wide);
                fxNarrow.Bot.TestStart();
                fxWide.Bot.TestStart();
                RaiseBar(fxNarrow);
                RaiseBar(fxWide);

                MoveMarketToA(fxNarrow, TradeType.Buy);
                MoveMarketToA(fxWide, TradeType.Buy);
                // نُبقي Ask (سعر الدخول المرجعي) ونوسّع Bid فقط في اللحظة التي تُحسب فيها
                // الحماية، كي يكون فرق الانتشار فعلياً وقت الحساب لا قبل فتح المراكز.
                fxWide.Symbol.Bid = fxWide.Symbol.Ask - 0.30;
                Check(Math.Abs((fxWide.Symbol.Ask - fxWide.Symbol.Bid) -
                               (fxNarrow.Symbol.Ask - fxNarrow.Symbol.Bid)) > 0.1,
                    "S19: الانتشاران مختلفان فعلاً وقت حساب الحماية");

                fxNarrow.Positions.CloseForTest(PositionByRole(fxNarrow, "A"), netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                fxWide.Positions.CloseForTest(PositionByRole(fxWide, "A"), netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                var bNarrow = PositionByRole(fxNarrow, "B");
                var bWide = PositionByRole(fxWide, "B");
                Check(Math.Abs(bNarrow.EntryPrice - bWide.EntryPrice) < 1e-9,
                    "S19: سعر دخول الشراء متطابق (Ask نفسه)");
                Check(Math.Abs(bNarrow.StopLoss.GetValueOrDefault() - bWide.StopLoss.GetValueOrDefault()) < 1e-9,
                    $"S19: انتشار مختلف لا يغيّر الوقف (ضيق={bNarrow.StopLoss}، واسع={bWide.StopLoss}) ⇒ لا ازدواج للانتشار");
                Check(EstimatedNetAt(bNarrow, bNarrow.StopLoss.GetValueOrDefault(), fxNarrow.Symbol) >= 0.10 - 1e-9,
                    "S19: الصافي ≥ 0.10 دون إضافة انتشار");

                WriteLog(opts, "scenario_S19_spread_not_double_counted.log", fxNarrow.Bot);
                summary.Add($"S19 spread: SL narrow={bNarrow.StopLoss}, wide={bWide.StopLoss}");
            }

            // === S20: الحد الأدنى لمسافة الوقف — لا تخفيض للهدف ====================
            {
                // (أ) حد أدنى صغير: لا يمنع وقفاً أبعد منه.
                var okSettings = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };
                okSettings.Spec.MinStopLossDistance = 1.0; // 1 pip = 0.1 سعر
                var ok = BuildExecFixture(okSettings);
                ok.Bot.TestStart();
                RaiseBar(ok);
                MoveMarketToA(ok, TradeType.Buy);
                ok.Positions.CloseForTest(PositionByRole(ok, "A"), netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                Check(ok.Bot.ModifyCalls.Count == 2,
                    $"S20: حد أدنى 1 pip لا يمنع وقفاً أبعد منه (الفعلي={ok.Bot.ModifyCalls.Count})");
                Check(Math.Abs(PositionByRole(ok, "B").StopLoss.GetValueOrDefault() - 2000.15) < 1e-9,
                    $"S20: الوقف بقي 2000.15 مع حد أدنى صغير (الفعلي={PositionByRole(ok, "B").StopLoss})");
                WriteLog(opts, "scenario_S20a_min_stop_distance_ok.log", ok.Bot);

                // (ب) حد أدنى أكبر من المسافة المطلوبة: لا تعديل ولا خفض للهدف.
                var s = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };
                s.Spec.MinStopLossDistance = 100.0; // 100 pip = 10.0 سعر
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");
                var slB = posB.StopLoss;
                var slC = posC.StopLoss;

                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);

                Check(fx.Bot.ModifyCalls.Count == 0,
                    $"S20: لم يُرسل وقف أقرب من الحد الأدنى (الفعلي={fx.Bot.ModifyCalls.Count})");
                Check(posB.StopLoss == slB && posC.StopLoss == slC, "S20: الوقفان الأصليان بقيا كما هما");
                Check(Contains(fx.Bot, "أقرب إلى السوق من الحد الأدنى"), "S20: سُجِّل سبب رفض الحد الأدنى");
                Check(!Contains(fx.Bot, "✅ المركز B") && !Contains(fx.Bot, "✅ المركز C"),
                    "S20: لا ادعاء بنجاح الحماية");
                Check(Contains(fx.Bot, "لم تكتمل الحماية"), "S20: سُجِّل فشل الحماية بوضوح");

                WriteLog(opts, "scenario_S20_min_stop_distance.log", fx.Bot);
                summary.Add("S20 min-stop-distance: no modify, original SL kept, target not lowered");
            }

            // === S21: مرجع الهدف غير متاح/غير صالح ⇒ لا تعديل ولا صيغة قديمة ======
            {
                var s = new ExecSettings
                {
                    Action = "buy",
                    SizeUsd = 20.0,
                    AtrTarget = 10.0 / 9.0,
                    CommissionPerPosition = 0.05,
                };

                // (أ) C أُغلق قبل A: لا مرجع لربح C المتوقع.
                var fx = BuildExecFixture(s);
                fx.Bot.TestStart();
                RaiseBar(fx);

                var posA = PositionByRole(fx, "A");
                var posB = PositionByRole(fx, "B");
                var posC = PositionByRole(fx, "C");
                var slB = posB.StopLoss;

                fx.Positions.CloseForTest(posC, netProfit: 2.5, reason: PositionCloseReason.TakeProfit);
                Check(fx.Bot.ModifyCalls.Count == 0, "S21a: إغلاق C لا يفعّل الحماية (ليس A)");

                MoveMarketToA(fx, TradeType.Buy);
                fx.Positions.CloseForTest(posA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                Check(fx.Bot.ModifyCalls.Count == 0, "S21a: لا تعديل عند تعذّر مرجع C");
                Check(posB.StopLoss == slB, "S21a: وقف B الأصلي بقي (لا تعادل ولا صيغة قديمة)");
                Check(Contains(fx.Bot, "تعذّر حساب هدف الحماية الصافي") && Contains(fx.Bot, "غير مفتوح"),
                    "S21a: سُجِّل سبب تعذّر الهدف بوضوح");
                Check(Contains(fx.Bot, "لا يوجد بديل"), "S21a: سُجِّل رفض البديل القديم");
                WriteLog(opts, "scenario_S21a_target_unavailable.log", fx.Bot);

                // (ب) C بلا هدف ربح على الخادم.
                var noTp = BuildExecFixture(s);
                noTp.Bot.TestStart();
                RaiseBar(noTp);
                var noTpA = PositionByRole(noTp, "A");
                var noTpB = PositionByRole(noTp, "B");
                var noTpC = PositionByRole(noTp, "C");
                noTpC.TakeProfit = null;
                var noTpSlB = noTpB.StopLoss;
                MoveMarketToA(noTp, TradeType.Buy);
                noTp.Positions.CloseForTest(noTpA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                Check(noTp.Bot.ModifyCalls.Count == 0, "S21b: لا تعديل بلا هدف C");
                Check(noTpB.StopLoss == noTpSlB, "S21b: وقف B بقي أصلياً");
                Check(Contains(noTp.Bot, "بلا هدف ربح"), "S21b: سُجِّل غياب هدف C");
                WriteLog(opts, "scenario_S21b_c_missing_tp.log", noTp.Bot);

                // (ج) مدخلات نقدية غير صالحة (حجم C = 0).
                var bad = BuildExecFixture(s);
                bad.Bot.TestStart();
                RaiseBar(bad);
                var badA = PositionByRole(bad, "A");
                var badB = PositionByRole(bad, "B");
                var badC = PositionByRole(bad, "C");
                badC.VolumeInUnits = 0.0;
                var badSlB = badB.StopLoss;
                MoveMarketToA(bad, TradeType.Buy);
                bad.Positions.CloseForTest(badA, netProfit: 3.0, reason: PositionCloseReason.TakeProfit);
                Check(bad.Bot.ModifyCalls.Count == 0, "S21c: لا تعديل بمدخلات غير صالحة");
                Check(badB.StopLoss == badSlB, "S21c: وقف B بقي أصلياً");
                Check(Contains(bad.Bot, "حجم Position C غير صالح"), "S21c: سُجِّل المدخل غير الصالح");
                WriteLog(opts, "scenario_S21c_invalid_inputs.log", bad.Bot);

                summary.Add("S21 target-unavailable/invalid: no modify, no old-formula fallback");
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
