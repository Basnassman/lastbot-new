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
//
//  الأعضاء التنفيذية (ExecuteMarketOrder / TradeResult / Position(s) / Symbol specs /
//  HttpRequest.Send) مضافةٌ بنفس أسماء ومقادير واجهة cTrader الرسمية، لتشغيل طبقات
//  التنفيذ الحقيقية في السيناريوهات خارج المنصة.
// =====================================================================================

using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;

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

    // ---- عناصر HTTP الموثّقة (Http / HttpRequest) ----

    public sealed class HttpRequest
    {
        public HttpRequest(Uri uri)
        {
            Uri = uri;
            Method = HttpMethod.Get;
        }

        public Uri Uri { get; }
        public string Body { get; set; }
        public TimeSpan Timeout { get; set; } = TimeSpan.FromSeconds(100);
        // القيمة الافتراضية في cTrader هي Get.
        public HttpMethod Method { get; set; }
    }

    /// <summary>
    /// شيم لـ cAlgo.API.Http. Get ينفّذ طلب GET حقيقياً عبر System.Net.Http
    /// (نفس ما تفعله المنصة فعلياً)، و Send ينفّذ الطلب حسب HttpRequest.Method.
    /// يمكن اختبار سيناريوهات الفشل عبر ضبط خاصية Transport / SendTransport.
    /// </summary>
    public class Http
    {
        public Http()
        {
            Transport = DefaultTransport;
            SendTransport = DefaultSend;
        }

        public Func<string, HttpResponse> Transport { get; set; }

        public Func<HttpRequest, HttpResponse> SendTransport { get; set; }

        public HttpResponse Get(string uri) => Transport(uri);

        public HttpResponse Get(Uri uri) => Transport(uri.ToString());

        public HttpResponse Send(HttpRequest request) => SendTransport(request);

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

        private static HttpResponse DefaultSend(HttpRequest request)
        {
            try
            {
                using (var client = new System.Net.Http.HttpClient())
                {
                    client.Timeout = request.Timeout;
                    var message = new System.Net.Http.HttpRequestMessage(request.Method, request.Uri);
                    if (!string.IsNullOrEmpty(request.Body))
                    {
                        message.Content = new System.Net.Http.StringContent(request.Body);
                    }
                    var response = client.SendAsync(message).GetAwaiter().GetResult();
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
        // هاتان المجموعتان موجودتان في cAlgo.API الحقيقي (HighPrices / LowPrices).
        public DataSeries HighPrices { get; } = new DataSeries();
        public DataSeries LowPrices { get; } = new DataSeries();
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

        // ---- إضافة شمعة للاختبار (تُحدّث كل السلاسل + LastBar + Count) ----
        public void AddBarForTest(DateTime openTimeUtc, double open, double high, double low, double close)
        {
            OpenTimes.Add(openTimeUtc);
            OpenPrices.Add(open);
            HighPrices.Add(high);
            LowPrices.Add(low);
            ClosePrices.Add(close);
            LastBar = new Bar
            {
                OpenTime = openTimeUtc,
                Open = open,
                High = high,
                Low = low,
                Close = close,
            };
            Count = ClosePrices.Count;
        }
    }

    // ---- أنواع التنفيذ الموثّقة ----

    public enum TradeType
    {
        Buy,
        Sell,
    }

    public enum RoundingMode
    {
        ToNearest,
        Up,
        Down,
    }

    public enum ErrorCode
    {
        TechnicalError,
        BadVolume,
        InsufficientMoney,
        MarketClosed,
        InvalidStopLoss,
        EntityNotFound,
        Unknown,
    }

    public enum PositionCloseReason
    {
        StopLoss,
        TakeProfit,
        StopOut,
        Closed,
    }

    public class Position
    {
        public int Id { get; set; }
        public string SymbolName { get; set; }
        public TradeType TradeType { get; set; }
        public double VolumeInUnits { get; set; }
        public double EntryPrice { get; set; }
        public double? StopLoss { get; set; }
        public double? TakeProfit { get; set; }
        public double GrossProfit { get; set; }
        public double NetProfit { get; set; }
        public double Pips { get; set; }
        public DateTime EntryTime { get; set; }
        public string Label { get; set; }
        public string Comment { get; set; }
        public bool HasTrailingStop { get; set; }
        public double Quantity { get; set; }
    }

    public class TradeResult
    {
        public bool IsSuccessful { get; set; }
        public ErrorCode? Error { get; set; }
        public Position Position { get; set; }
        public string Description { get; set; }

        // cAlgo.API.TradeResult.ToString() يعطي وصفاً نصيّاً للنتيجة.
        public override string ToString()
        {
            return Description ?? (IsSuccessful ? "Successful" : "Error: " + Error);
        }
    }

    public class PositionOpenedEventArgs
    {
        public Position Position { get; set; }
    }

    public class PositionModifiedEventArgs
    {
        public Position Position { get; set; }
    }

    public class PositionClosedEventArgs
    {
        public Position Position { get; set; }
        public PositionCloseReason Reason { get; set; }
    }

    /// <summary>
    /// شيم لمجموعة Positions (IEnumerable&lt;Position&gt;) مع Find/FindAll وعدد المراكز
    /// وأحداث Opened/Modified/Closed — كما في cAlgo.API الرسمي.
    /// </summary>
    public class Positions : IEnumerable<Position>
    {
        private readonly List<Position> _items = new List<Position>();

        public int Count => _items.Count;

        public Position this[int index] => _items[index];

        public event Action<PositionOpenedEventArgs> Opened;
#pragma warning disable CS0067 // الـevent موثّق في cAlgo.API.Robot لكن الـcBot الحالي لا يستخدمه
        public event Action<PositionModifiedEventArgs> Modified;
#pragma warning restore CS0067
        public event Action<PositionClosedEventArgs> Closed;

        public IEnumerator<Position> GetEnumerator() => _items.GetEnumerator();

        IEnumerator IEnumerable.GetEnumerator() => _items.GetEnumerator();

        public Position Find(string label)
        {
            return _items.Find(p => string.Equals(p.Label, label, StringComparison.Ordinal));
        }

        public Position Find(string label, string symbolName)
        {
            return _items.Find(p =>
                string.Equals(p.Label, label, StringComparison.Ordinal) &&
                string.Equals(p.SymbolName, symbolName, StringComparison.Ordinal));
        }

        public Position Find(string label, string symbolName, TradeType tradeType)
        {
            return _items.Find(p =>
                string.Equals(p.Label, label, StringComparison.Ordinal) &&
                string.Equals(p.SymbolName, symbolName, StringComparison.Ordinal) &&
                p.TradeType == tradeType);
        }

        public Position[] FindAll(string label)
        {
            return _items.FindAll(p => string.Equals(p.Label, label, StringComparison.Ordinal)).ToArray();
        }

        public Position[] FindAll(string label, string symbolName)
        {
            return _items.FindAll(p =>
                string.Equals(p.Label, label, StringComparison.Ordinal) &&
                string.Equals(p.SymbolName, symbolName, StringComparison.Ordinal)).ToArray();
        }

        public Position[] FindAll(string label, string symbolName, TradeType tradeType)
        {
            return _items.FindAll(p =>
                string.Equals(p.Label, label, StringComparison.Ordinal) &&
                string.Equals(p.SymbolName, symbolName, StringComparison.Ordinal) &&
                p.TradeType == tradeType).ToArray();
        }

        public Position FindById(int id)
        {
            return _items.Find(p => p.Id == id);
        }

        // ---- مُشغِّلات للاختبار فقط (لا وجود لها في cAlgo.API الحقيقي) ----
        public void AddForTest(Position position)
        {
            _items.Add(position);
            Opened?.Invoke(new PositionOpenedEventArgs { Position = position });
        }

        public void CloseForTest(Position position, double netProfit, PositionCloseReason reason)
        {
            position.NetProfit = netProfit;
            position.GrossProfit = netProfit;
            _items.Remove(position);
            Closed?.Invoke(new PositionClosedEventArgs { Position = position, Reason = reason });
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

    /// <summary>
    /// شيم لـ Symbol مع خصائص ومواصفات الرمز والتحويلات — نفس الأسماء الموثّقة.
    /// القيم الافتراضية هنا للاختبار فقط؛ الـcBot يقرؤها من المنصة الحقيقية.
    /// </summary>
    public class Symbol
    {
        public string Name { get; set; }
        public int Digits { get; set; } = 2;
        public double PipSize { get; set; } = 0.01;
        public double PipValue { get; set; } = 0.01;
        public double TickSize { get; set; } = 0.01;
        public double TickValue { get; set; } = 0.01;
        public double LotSize { get; set; } = 100000.0;
        public double Bid { get; set; } = 2000.0;
        public double Ask { get; set; } = 2000.1;
        public double VolumeInUnitsMin { get; set; } = 1.0;
        public double VolumeInUnitsMax { get; set; } = 1000000.0;
        public double VolumeInUnitsStep { get; set; } = 1.0;

        public double NormalizeVolumeInUnits(double volume, RoundingMode roundingMode)
        {
            if (VolumeInUnitsStep <= 0)
            {
                return volume;
            }

            var steps = volume / VolumeInUnitsStep;
            double roundedSteps;
            switch (roundingMode)
            {
                case RoundingMode.Down:
                    roundedSteps = Math.Floor(steps);
                    break;
                case RoundingMode.Up:
                    roundedSteps = Math.Ceiling(steps);
                    break;
                default:
                    roundedSteps = Math.Round(steps, MidpointRounding.AwayFromZero);
                    break;
            }

            return roundedSteps * VolumeInUnitsStep;
        }

        public double QuantityToVolumeInUnits(double quantity) => quantity * LotSize;

        public double VolumeInUnitsToQuantity(double volume) => LotSize <= 0 ? volume : volume / LotSize;

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

        public Symbol GetSymbol(string name) => new Symbol { Name = name };
    }

    /// <summary>
    /// الشيم الأساسي لكل الروبوتات. يوفر نفس الأعضاء الموثّقة (Account, Http, MarketData,
    /// Symbol, Symbols, Positions, RunningMode, IsBacktesting, Print, Stop,
    /// ExecuteMarketOrder) مع دوال دورة الحياة.
    /// </summary>
    public abstract class Algo
    {
        public IAccount Account { get; set; }
        public Http Http { get; set; }
        public MarketData MarketData { get; set; }
        public Symbol Symbol { get; set; }
        public Symbols Symbols { get; set; }
        public Positions Positions { get; set; }
        public RunningMode RunningMode { get; set; }
        public bool IsBacktesting { get; set; }
        public DateTime Time { get; set; }
        public DateTime TimeInUtc { get; set; }

        // ---- تسجيل الاختبار: كل سطر يُمرَّر إلى OnPrint ويُخزَّن ----
        public List<string> Log { get; } = new List<string>();
        public Action<string> OnPrint { get; set; }

        public bool Stopped { get; private set; }

        // ---- خطّاف تنفيذ الأمر للاختبار (لا وجود له في cAlgo.API الحقيقي) ----
        // الافتراضي يحاكي التنفيذ الناجح (يفتح مركزاً ويرجّع TradeResult ناجحاً).
        public Func<TradeType, string, double, string, double?, double?, string, bool, TradeResult> OrderExecutor { get; set; }

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

        /// <summary>
        /// شيم لـ Robot.ExecuteMarketOrder: نفس التوقيع الثماني الموثّق في دليل cTrader
        /// (TradeType, symbolName, volume, label, stopLossPips, takeProfitPips, comment,
        /// hasTrailingStop) ويعيد TradeResult.
        /// </summary>
        public TradeResult ExecuteMarketOrder(
            TradeType tradeType,
            string symbolName,
            double volume,
            string label = null,
            double? stopLossPips = null,
            double? takeProfitPips = null,
            string comment = null,
            bool hasTrailingStop = false)
        {
            if (OrderExecutor != null)
            {
                return OrderExecutor(tradeType, symbolName, volume, label, stopLossPips, takeProfitPips, comment, hasTrailingStop);
            }

            return DefaultOrderExecutor(tradeType, symbolName, volume, label, stopLossPips, takeProfitPips, comment, hasTrailingStop);
        }

        private TradeResult DefaultOrderExecutor(
            TradeType tradeType,
            string symbolName,
            double volume,
            string label,
            double? stopLossPips,
            double? takeProfitPips,
            string comment,
            bool hasTrailingStop)
        {
            var symbol = Symbol;
            var entry = tradeType == TradeType.Buy ? symbol.Ask : symbol.Bid;

            // تحويل SL/TP من pips إلى أسعار مطلقة كما تفعل المنصة عند التنفيذ.
            double? stopLoss = null;
            if (stopLossPips.HasValue)
            {
                stopLoss = tradeType == TradeType.Buy
                    ? entry - (stopLossPips.Value * symbol.PipSize)
                    : entry + (stopLossPips.Value * symbol.PipSize);
            }

            double? takeProfit = null;
            if (takeProfitPips.HasValue)
            {
                takeProfit = tradeType == TradeType.Buy
                    ? entry + (takeProfitPips.Value * symbol.PipSize)
                    : entry - (takeProfitPips.Value * symbol.PipSize);
            }

            var position = new Position
            {
                Id = _nextPositionId++,
                SymbolName = symbolName,
                TradeType = tradeType,
                VolumeInUnits = volume,
                EntryPrice = entry,
                StopLoss = stopLoss,
                TakeProfit = takeProfit,
                EntryTime = TimeInUtc,
                Label = label,
                Comment = comment,
                HasTrailingStop = hasTrailingStop,
            };

            Positions?.AddForTest(position);

            return new TradeResult
            {
                IsSuccessful = true,
                Position = position,
                Description = "Order executed",
            };
        }

        private int _nextPositionId = 100000;

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
