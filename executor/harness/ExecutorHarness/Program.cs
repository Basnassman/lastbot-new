// =====================================================================================
//  ExecutorHarness — يشغّل ملف الـcBot الحقيقي (XauBotExecutor.cs) آلياً خارج cTrader.
// -------------------------------------------------------------------------------------
//  ⚠️ هذا ليس تشغيلاً داخل cTrader، وليس حساب Demo حقيقي عند وسيط. إنه تشغيل لملف
//     الـcBot نفسه مقابل شيم cAlgo.API + خادم Brain API المحلي، لإثبات السلوك القابل
//     للإثبات خارج المنصة (فحص الحماية، شكل النداء، التسجيل، معالجة أخطاء الشبكة).
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

    internal sealed class TestAccount : IAccount
    {
        public AccountType AccountType => AccountType.Hedged;
        public bool IsLive { get; set; }
        public int Number { get; set; } = 12345678;
        public string BrokerName { get; set; } = "HarnessBroker";
        public double Balance => 10000.0;
        public double Equity => 10000.0;
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
        //  بناء روبوت بسيناريو محدد
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
