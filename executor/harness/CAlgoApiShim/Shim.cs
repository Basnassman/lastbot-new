// =====================================================================================
//  CAlgoApiShim — بديل تطويري مصغّر لسطح  cAlgo.API  المستخدَم في XauBotExecutor.
// -------------------------------------------------------------------------------------
//  ⚠️ هذا ليس cAlgo.API الحقيقي. إنه "شيم" (stub) بُنيت أنواعه وأسماؤه ومقاديرها من
//     التوثيق الرسمي لـ cTrader Algo (help.ctrader.com/ctrader-algo/references) فقط،
//     لتمكين:
//       1) بناء ملف الـcBot الحقيقي (XauBotExecutor.cs) خارج cTrader (تحقق نحوي + تعاقد).
//       2) تشغيل الـcBot آلياً في سيناريوهات محاكاة (حساب Demo / حساب حقيقي / فشل شبكة).
//     الاسم AssemblyName=cAlgo.API كي يجد الملف الحقيقي الأنواع كما لو كان داخل cTrader.
//
//  كل عضو عام هنا له نظير موثّق بالاسم والمقدار نفسه. لا منطق تجاري هنا — فقط نقل
//  السلوك الموثّق (مثل: خطأ النقل يظهر في HttpResponse.Exception، لا يُرمى دائماً).
// =====================================================================================

using System;
using System.Collections.Generic;
using System.Globalization;

namespace cAlgo.API
{
    public enum AccessRights
    {
        None = 0,
        FullAccess = 1,
    }

    // أعضاء TimeZones يجب أن تكون const لأنها تُستخدَم كوسائط Attribute
    // ([Robot(TimeZone = TimeZones.UTC, ...)]) — والـ Attribute يتطلّب تعبيراً ثابتاً.
    public static class TimeZones
    {
        public const string UTC = "UTC";
        public const string EasternStandardTime = "Eastern Standard Time";
        public const string GMTStandardTime = "GMT Standard Time";
    }

    [AttributeUsage(AttributeTargets.Class, AllowMultiple = false, Inherited = false)]
    public sealed class RobotAttribute : Attribute
    {
        public RobotAttribute()
        {
        }

        public string Name { get; set; }
        public string TimeZone { get; set; }
        public AccessRights AccessRights { get; set; }
        public string DefaultSymbolName { get; set; }
        public string DefaultTimeFrame { get; set; }
        public string AdditionalInfoUrl { get; set; }
        public bool AddIndicators { get; set; }
    }

    [AttributeUsage(AttributeTargets.Property, AllowMultiple = false, Inherited = true)]
    public sealed class ParameterAttribute : Attribute
    {
        public ParameterAttribute()
        {
        }

        public ParameterAttribute(string name)
        {
            Name = name;
        }

        public string Name { get; set; }
        public object DefaultValue { get; set; }
        public string Group { get; set; }
        public double MinValue { get; set; }
        public double MaxValue { get; set; }
        public int Step { get; set; }
    }

    public enum RunningMode
    {
        RealTime,
        SilentBacktesting,
        VisualBacktesting,
        Optimization,
    }

    public sealed class TimeFrame
    {
        private TimeFrame(string name)
        {
            Name = name;
        }

        public string Name { get; }

        public static readonly TimeFrame Minute = new TimeFrame("m1");
        public static readonly TimeFrame Minute2 = new TimeFrame("m2");
        public static readonly TimeFrame Minute3 = new TimeFrame("m3");
        public static readonly TimeFrame Minute5 = new TimeFrame("m5");
        public static readonly TimeFrame Minute10 = new TimeFrame("m10");
        public static readonly TimeFrame Minute15 = new TimeFrame("m15");
        public static readonly TimeFrame Minute30 = new TimeFrame("m30");
        public static readonly TimeFrame Hour = new TimeFrame("h1");
        public static readonly TimeFrame Hour4 = new TimeFrame("h4");
        public static readonly TimeFrame Daily = new TimeFrame("d1");
        public static readonly TimeFrame Weekly = new TimeFrame("w1");
        public static readonly TimeFrame Monthly = new TimeFrame("mn1");

        public override string ToString() => Name;
    }

    public class HttpException : Exception
    {
        public HttpException(string message)
            : base(message)
        {
        }

        public HttpException(string message, Exception innerException)
            : base(message, innerException)
        {
        }
    }

    public sealed class HttpResponse
    {
        public int StatusCode { get; set; }
        public bool IsSuccessful { get; set; }
        public string Body { get; set; }
        public HttpException Exception { get; set; }
    }

    /// <summary>
    /// شيم لـ cAlgo.API.Http. الافتراضي ينفّذ طلب GET حقيقياً عبر System.Net.Http
    /// (نفس ما تفعله المنصة فعلياً)، مع نقل أي خطأ إلى HttpResponse.Exception بدل رميه
    /// إلى المستدعي. يمكن اختبار سيناريوهات الفشل عبر ضبط خاصية Transport.
    /// </summary>
    public class Http
    {
        public Http()
        {
            Transport = DefaultTransport;
        }

        public Func<string, HttpResponse> Transport { get; set; }

        public HttpResponse Get(string uri) => Transport(uri);

        public HttpResponse Get(Uri uri) => Transport(uri.ToString());

        private static HttpResponse DefaultTransport(string uri)
        {
            try
            {
                using (var client = new System.Net.Http.HttpClient())
                {
                    client.Timeout = TimeSpan.FromSeconds(30);
                    var response = client.GetAsync(uri).GetAwaiter().GetResult();
                    var body = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                    return new HttpResponse
                    {
                        StatusCode = (int)response.StatusCode,
                        IsSuccessful = response.IsSuccessStatusCode,
                        Body = body,
                    };
                }
            }
            catch (Exception ex)
            {
                return new HttpResponse
                {
                    StatusCode = 0,
                    IsSuccessful = false,
                    Body = null,
                    Exception = new HttpException(ex.Message, ex),
                };
            }
        }
    }

    public class DataSeries
    {
        private readonly List<double> _values = new List<double>();

        public int Count => _values.Count;
        public double this[int index] => _values[index];
        public double LastValue => _values.Count > 0 ? _values[_values.Count - 1] : 0.0;
        public void Add(double value) => _values.Add(value);
    }

    public class TimeSeries
    {
        private readonly List<DateTime> _values = new List<DateTime>();

        public int Count => _values.Count;
        public DateTime this[int index] => _values[index];
        public DateTime LastValue => _values.Count > 0 ? _values[_values.Count - 1] : default(DateTime);
        public void Add(DateTime value) => _values.Add(value);
    }

    public class Bar
    {
        public DateTime OpenTime { get; set; }
        public double Open { get; set; }
        public double High { get; set; }
        public double Low { get; set; }
        public double Close { get; set; }
    }

    public class BarClosedEventArgs
    {
        public Bars Bars { get; set; }
    }

    public class BarOpenedEventArgs
    {
        public Bars Bars { get; set; }
    }

    public class Bars
    {
        public string SymbolName { get; set; }
        public TimeFrame TimeFrame { get; set; }
        public int Count { get; set; }
        public Bar LastBar { get; set; }
        public DataSeries ClosePrices { get; } = new DataSeries();
        public DataSeries OpenPrices { get; } = new DataSeries();
        public TimeSeries OpenTimes { get; } = new TimeSeries();

        public event Action<BarClosedEventArgs> BarClosed;
        public event Action<BarOpenedEventArgs> BarOpened;

        // ---- مُشغِّل للاختبار فقط (لا وجود له في cAlgo.API الحقيقي) ----
        public void RaiseBarClosedForTest()
        {
            BarClosed?.Invoke(new BarClosedEventArgs { Bars = this });
        }

        public void RaiseBarOpenedForTest()
        {
            BarOpened?.Invoke(new BarOpenedEventArgs { Bars = this });
        }
    }
}

namespace cAlgo.API.Internals
{
    public enum AccountType
    {
        Hedged,
        Netted,
    }

    public interface IAccount
    {
        AccountType AccountType { get; }
        bool IsLive { get; }
        int Number { get; }
        string BrokerName { get; }
        double Balance { get; }
        double Equity { get; }
    }

    public class Symbol
    {
        public string Name { get; set; }

        public override string ToString() => Name;
    }

    public class Symbols
    {
        public Func<string, Symbol> Resolver { get; set; }

        public Symbol GetSymbol(string name)
        {
            if (Resolver != null)
            {
                return Resolver(name);
            }

            return new Symbol { Name = name };
        }
    }

    public class MarketData
    {
        public Func<TimeFrame, string, Bars> BarsProvider { get; set; }

        public Bars GetBars(TimeFrame timeFrame) => BarsProvider(timeFrame, null);

        public Bars GetBars(TimeFrame timeFrame, string symbolName) => BarsProvider(timeFrame, symbolName);
    }

    /// <summary>
    /// الشيم الأساسي لكل الروبوتات. يوفر نفس الأعضاء الموثّقة (Account, Http, MarketData,
    /// Symbol, Symbols, RunningMode, IsBacktesting, Print, Stop) مع دوال دورة الحياة.
    /// </summary>
    public abstract class Algo
    {
        public IAccount Account { get; set; }
        public Http Http { get; set; }
        public MarketData MarketData { get; set; }
        public Symbol Symbol { get; set; }
        public Symbols Symbols { get; set; }
        public RunningMode RunningMode { get; set; }
        public bool IsBacktesting { get; set; }
        public DateTime Time { get; set; }
        public DateTime TimeInUtc { get; set; }

        // ---- تسجيل الاختبار: كل سطر يُمرَّر إلى OnPrint ويُخزَّن ----
        public List<string> Log { get; } = new List<string>();
        public Action<string> OnPrint { get; set; }

        public bool Stopped { get; private set; }

        public void Print(object value)
        {
            Emit(value == null ? string.Empty : value.ToString());
        }

        public void Print(string message, params object[] parameters)
        {
            Emit(parameters == null || parameters.Length == 0
                ? message
                : string.Format(CultureInfo.InvariantCulture, message, parameters));
        }

        public void Stop()
        {
            Stopped = true;
        }

        private void Emit(string line)
        {
            var stamped = string.Format(
                CultureInfo.InvariantCulture,
                "{0:yyyy-MM-dd HH:mm:ss.fff} UTC | {1}",
                DateTime.UtcNow,
                line);
            Log.Add(stamped);
            OnPrint?.Invoke(stamped);
        }
    }

    public abstract class Robot : Algo
    {
        protected virtual void OnStart()
        {
        }

        protected virtual void OnStop()
        {
        }

        protected virtual void OnException(Exception exception)
        {
        }

        protected virtual void OnTick()
        {
        }

        protected virtual void OnBar()
        {
        }

        protected virtual void OnBarClosed()
        {
        }

        // ---- مُشغِّلات للاختبار فقط (لا وجود لها في cAlgo.API الحقيقي) ----
        public void TestStart() => OnStart();

        public void TestStop() => OnStop();

        public void TestException(Exception exception) => OnException(exception);
    }
}
