"""
تطبيق حقيقي لعقد PriceFeed عبر cTrader Open API الرسمي.

يستخدم المكتبة الرسمية من Spotware (ctrader-open-api / OpenApiPy) التي ترسل
رسائل Protobuf عبر TCP باستخدام Twisted. لا يوجد أي Web Scraping أو مكتبات
غير رسمية.

مبادئ ثابتة في هذا الملف:
1. المصادقة تُقرأ حصراً من config/settings.py (لا قيم إعداد هنا).
2. demo هو الوضع الوحيد المسموح افتراضياً — الاتصال بحساب live محظور ما لم
   يُحدَّد "live" صراحة في CTRADER_ENVIRONMENT مع allow_live=True.
3. عند أي فشل (اتصال، مصادقة، مهلة، استجابة ناقصة) يُطلق CTraderFeedError
   صراحة — لا بيانات فارغة ولا وهمية بصمت أبداً، حتى لا يتخذ محرك القرار
   قراراً بناءً على بيانات غير موجودة.

ملاحظة معمارية: مكتبة Spotware تعتمد على Twisted reactor (حلقة أحداث).
هذا الملف يشغّل الـ reactor في خيط خلفية (daemon) مرة واحدة، ثم يوجّه كل
استدعاءات العميل إليه عبر reactor.callFromThread، بينما ينتظر الخيط الرئيسي
النتيجة على threading.Event بمهلة زمنية قصيرة — فيبقى توقيع get_candles
متزامناً مطابقاً للعقد تماماً.

رسائل البروتوكول المستخدمة (من وثائق Spotware الرسمية):
- ProtoOAApplicationAuthReq/Res : مصادقة التطبيق (clientId + clientSecret)
- ProtoOAAccountAuthReq/Res     : مصادقة الحساب (ctidTraderAccountId + accessToken)
- ProtoOASymbolsListReq/Res     : تحويل اسم الرمز (مثل XAUUSD) إلى symbolId
- ProtoOASymbolByIdReq/Res      : معرفة خانات السعر (digits) للتحويل الصحيح
- ProtoHeartbeatEvent           : نبض دوري يمنع قطع الاتصال بسبب الخمول
- ProtoOAGetTrendbarsReq/Res    : الشموع التاريخية (Trendbars)
"""
import threading
from collections.abc import Callable, Generator, Iterable
from datetime import datetime, timedelta, timezone

import pandas as pd
from twisted.internet import reactor, defer
from twisted.internet.task import LoopingCall

from brain.data.interfaces import PriceFeed
from config.settings import settings

# المكتبة الرسمية من Spotware (OpenApiPy)
from ctrader_open_api import Client, EndPoints, TcpProtocol
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoHeartbeatEvent
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAErrorRes,
    ProtoOAGetTrendbarsReq,
    ProtoOAGetTrendbarsRes,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrendbarPeriod


class CTraderFeedError(RuntimeError):
    """يُطلق عند أي فشل في الاتصال أو المصادقة أو جلب البيانات من cTrader."""


# --- ثوابت البروتوكول (قيم بروتوكولية من وثائق Spotware، ليست قيم إعداد) ---

# مقياس السعر: cTrader يُرسل الأسعار كأعداد صحيحة بوحدة 1/100000
_PRICE_SCALE = 100_000

# الإطارات الزمنية المدعومة → قيم enum ProtoOATrendbarPeriod الرسمية
_TIMEFRAME_TO_PERIOD: dict[str, int] = {
    "M1": ProtoOATrendbarPeriod.Value("M1"),
    "M5": ProtoOATrendbarPeriod.Value("M5"),
    "M15": ProtoOATrendbarPeriod.Value("M15"),
    "M30": ProtoOATrendbarPeriod.Value("M30"),
    "H1": ProtoOATrendbarPeriod.Value("H1"),
    "H4": ProtoOATrendbarPeriod.Value("H4"),
    "D1": ProtoOATrendbarPeriod.Value("D1"),
}

# مدة الشمعة الواحدة بالدقائق (لحساب النافذة الزمنية المطلوبة)
_PERIOD_MINUTES: dict[str, int] = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440,
}

# فاصل النبض بالثواني (الوثائق: نبضة كل 10 ثوانٍ على الأقل — نستخدم 5 للأمان)
_HEARTBEAT_SECONDS = 5.0

# سياسة إعادة المحاولة للاتصال (تراجع أُسّي محدود بـ 10 ثوانٍ)
def _retry_policy(attempts: int) -> float:
    return min(float(2 ** attempts), 10.0)


def _trendbars_to_dataframe(trendbars: Iterable[object], digits: int) -> pd.DataFrame:
    """تحوّل شموع cTrader (الصيغة النسبية) إلى DataFrame مطابق لعقد PriceFeed.

    cTrader يُرسل: low كسعر صحيح بوحدة 1/100000، وبقية الأسعار كفروق (deltas)
    عن low. السعر الحقيقي = (low + delta) / 100000 مع التقريب إلى digits.
    """
    rows = []
    for trendbar in sorted(trendbars, key=lambda tb: tb.utcTimestampInMinutes):
        low = int(trendbar.low)
        rows.append(
            {
                "timestamp": datetime.fromtimestamp(
                    int(trendbar.utcTimestampInMinutes) * 60, tz=timezone.utc
                ),
                "open": round((low + trendbar.deltaOpen) / _PRICE_SCALE, digits),
                "high": round((low + trendbar.deltaHigh) / _PRICE_SCALE, digits),
                "low": round(low / _PRICE_SCALE, digits),
                "close": round((low + trendbar.deltaClose) / _PRICE_SCALE, digits),
                "volume": int(trendbar.volume),
            }
        )
    return pd.DataFrame(rows)


def _ensure_reactor_running() -> None:
    """يشغّل Twisted reactor في خيط خلفية واحد فقط طوال عمر العملية."""
    global _reactor_thread
    with _reactor_lock:
        if _reactor_thread is not None and _reactor_thread.is_alive():
            return
        # installSignalHandlers=False إلزامي خارج الخيط الرئيسي
        _reactor_thread = threading.Thread(
            target=reactor.run, args=(False,), name="ctrader-reactor", daemon=True
        )
        _reactor_thread.start()


_reactor_thread: threading.Thread | None = None
_reactor_lock = threading.Lock()


class _TokenStore:
    """يحفظ رموز الوصول/التجديد في ذاكرة العملية فقط (لا يكتب أي ملفات إطلاقاً).

    رمز التجديد الجديد (إن وُجد في الاستجابة) يُحفظ في الذاكرة أيضاً؛ عند إعادة
    تشغيل العملية تُقرأ القيم من .env كما هي — هذا مقصود في v1 (لا طبقة تخزين).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._access = settings.ctrader_access_token
        self._refresh = settings.ctrader_refresh_token

    def access(self) -> str:
        with self._lock:
            return self._access

    def refresh_available(self) -> bool:
        with self._lock:
            return bool(self._refresh)

    def refresh_access_token(self) -> str:
        """يستدعي نقطة النهاية الرسمية openapi.ctrader.com/apps/token (grant_type=refresh_token)."""
        from ctrader_open_api.auth import Auth  # استيراد محلي: يستهلك requests فقط عند الحاجة

        with self._lock:
            if not self._refresh:
                raise CTraderFeedError(
                    "انتهت صلاحية رمز الوصول ولا يوجد CTRADER_REFRESH_TOKEN في الإعدادات "
                    "لتجديده تلقائياً — أنشئ رمز وصول جديداً من openapi.ctrader.com"
                )
            auth = Auth(
                appClientId=settings.ctrader_client_id,
                appClientSecret=settings.ctrader_client_secret,
                redirectUri="",
            )
            response = auth.refreshToken(self._refresh)
            access = response.get("accessToken")
            if not access:
                raise CTraderFeedError(f"فشل تجديد رمز الوصول من cTrader: {response}")
            self._access = access
            new_refresh = response.get("refreshToken")
            if new_refresh:
                self._refresh = new_refresh
            return access


class _Session:
    """جلسة cTrader واحدة: اتصال → مصادقة تطبيق → مصادقة حساب → نبض دوري.

    كل الدوال المنتهية بـ _on_reactor تعمل حصراً داخل خيط الـ reactor
    (تُستدعى عبر reactor.callFromThread) — هذا شرط سلامة خيوط Twisted.
    """

    def __init__(self, host: str, account_id: int, access_token: str, request_timeout: float) -> None:
        self.host = host
        self.account_id = account_id
        self.access_token = access_token
        self.request_timeout = request_timeout
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol, retryPolicy=_retry_policy)
        self._symbol_ids: dict[str, int] = {}
        self._symbol_digits: dict[int, int] = {}
        self._heartbeat_loop: LoopingCall | None = None

    # --- فتح الجلسة والمصادقة (على خيط الـ reactor) ---

    @defer.inlineCallbacks
    def open_on_reactor(self) -> Generator[defer.Deferred, object, None]:  # noqa: UP043
        self.client.startService()
        yield self.client.whenConnected()

        app_auth_res = yield self.client.send(
            self._make_app_auth_request(), responseTimeoutInSeconds=self.request_timeout + 1
        )
        self._expect(app_auth_res, ProtoOAApplicationAuthRes, "مصادقة التطبيق")

        account_auth_res = yield self.client.send(
            self._make_account_auth_request(), responseTimeoutInSeconds=self.request_timeout + 1
        )
        self._expect(account_auth_res, ProtoOAAccountAuthRes, "مصادقة الحساب")

        # نبض دوري يمنع الخادم من قطع الجلسة بسبب الخمول
        self._heartbeat_loop = LoopingCall(self._send_heartbeat_on_reactor)
        self._heartbeat_loop.start(_HEARTBEAT_SECONDS, now=False)

    def _make_app_auth_request(self) -> ProtoOAApplicationAuthReq:
        request = ProtoOAApplicationAuthReq()
        request.clientId = settings.ctrader_client_id
        request.clientSecret = settings.ctrader_client_secret
        return request

    def _make_account_auth_request(self) -> ProtoOAAccountAuthReq:
        request = ProtoOAAccountAuthReq()
        request.ctidTraderAccountId = self.account_id
        request.accessToken = self.access_token
        return request

    def _send_heartbeat_on_reactor(self) -> None:
        # أحداث النبض ليس لها استجابة → مهلة قصيرة مع ابتلاع الخطأ المتوقع
        deferred = self.client.send(ProtoHeartbeatEvent(), responseTimeoutInSeconds=3)
        deferred.addErrback(lambda _failure: None)

    @staticmethod
    def _expect(response: object, expected_type: type, step_name: str) -> None:
        """يتحقق أن الاستجابة من النوع المتوقع، ويحوّل أخطاء الخادم إلى استثناء واضح."""
        if isinstance(response, expected_type):
            return
        if isinstance(response, ProtoOAErrorRes):
            raise CTraderFeedError(
                f"رفض cTrader طلب {step_name}: errorCode={response.errorCode} "
                f"description={response.description or '—'}"
            )
        raise CTraderFeedError(f"استجابة غير متوقعة أثناء {step_name}: {response}")

    # --- جلب الشموع (على خيط الـ reactor) ---

    @defer.inlineCallbacks
    def fetch_candles_on_reactor(self, symbol: str, timeframe: str, count: int) -> Generator[defer.Deferred, object, pd.DataFrame]:
        symbol_id, digits = yield self._ensure_symbol_on_reactor(symbol)
        period = _TIMEFRAME_TO_PERIOD[timeframe]

        # نافذة زمنية تغطي العدد المطلوب من الشموع بهامش مضاعف (عطلات/فجوات السوق)،
        # مع اعتماد count للحد من الاستجابة من جهة الخادم.
        span_minutes = count * _PERIOD_MINUTES[timeframe] * 2
        to_timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        from_timestamp = int(
            (datetime.now(timezone.utc) - timedelta(minutes=span_minutes)).timestamp() * 1000
        )

        request = ProtoOAGetTrendbarsReq()
        request.ctidTraderAccountId = self.account_id
        request.period = period
        request.symbolId = symbol_id
        request.count = count
        request.fromTimestamp = from_timestamp
        request.toTimestamp = to_timestamp

        response = yield self.client.send(request, responseTimeoutInSeconds=self.request_timeout + 1)
        self._expect(response, ProtoOAGetTrendbarsRes, "طلب الشموع")
        if not response.trendbar:
            raise CTraderFeedError(
                f"لم يُعد cTrader أي شموع للرمز {symbol} على الإطار {timeframe} — "
                "قد يكون السوق مغلقاً أو البيانات التاريخية غير متاحة من الوسيط"
            )
        return _trendbars_to_dataframe(response.trendbar, digits)

    @defer.inlineCallbacks
    def _ensure_symbol_on_reactor(self, symbol: str) -> Generator[defer.Deferred, object, tuple[int, int]]:
        """يخزّن symbolId و digits للرمز (استعلامان فقط في أول نداء لكل رمز)."""
        if symbol in self._symbol_ids:
            symbol_id = self._symbol_ids[symbol]
            return symbol_id, self._symbol_digits[symbol_id]

        list_req = ProtoOASymbolsListReq()
        list_req.ctidTraderAccountId = self.account_id
        list_res = yield self.client.send(list_req, responseTimeoutInSeconds=self.request_timeout + 1)
        self._expect(list_res, ProtoOASymbolsListRes, "جلب قائمة الرموز")

        matches = [
            light_symbol
            for light_symbol in list_res.symbol
            if light_symbol.symbolName.upper() == symbol.upper()
        ]
        if not matches:
            raise CTraderFeedError(f"الرمز {symbol} غير موجود على حساب cTrader هذا")
        symbol_id = int(matches[0].symbolId)
        self._symbol_ids[symbol] = symbol_id

        by_id_req = ProtoOASymbolByIdReq()
        by_id_req.ctidTraderAccountId = self.account_id
        by_id_req.symbolId.append(symbol_id)
        by_id_res = yield self.client.send(by_id_req, responseTimeoutInSeconds=self.request_timeout + 1)
        self._expect(by_id_res, ProtoOASymbolByIdRes, "جلب تفاصيل الرمز")
        if not by_id_res.symbol:
            raise CTraderFeedError(f"فشل جلب تفاصيل الرمز {symbol}")
        self._symbol_digits[symbol_id] = int(by_id_res.symbol[0].digits)
        return symbol_id, self._symbol_digits[symbol_id]

    # --- الإغلاق ---

    def close(self) -> None:
        """يوقف الجلسة بأمان (يُستدعى من أي خيط؛ العملية تُوجَّه إلى الـ reactor)."""
        _ensure_reactor_running()

        def close_on_reactor() -> None:
            if self._heartbeat_loop is not None and self._heartbeat_loop.running:
                self._heartbeat_loop.stop()
            self.client.stopService()

        try:
            reactor.callFromThread(close_on_reactor)
        except RuntimeError:
            # الـ reactor غير مشغّل → لا توجد موارد شبكية نشطة أصلاً
            pass


class CTraderPriceFeed(PriceFeed):
    """مصدر أسعار حقيقي من cTrader Open API يلتزم بعقد PriceFeed تماماً."""

    def __init__(self, allow_live: bool = False) -> None:
        self.allow_live = allow_live
        self.timeout = settings.ctrader_connection_timeout_seconds

        # التحقق الصريح من البيئة: demo افتراضياً ووضعية live محظورة إلا بصراحة
        self.environment = (settings.ctrader_environment or "demo").strip().lower()
        if self.environment not in ("demo", "live"):
            raise CTraderFeedError(
                f"قيمة CTRADER_ENVIRONMENT غير صحيحة: '{settings.ctrader_environment}' "
                "(المسموح: demo أو live فقط)"
            )
        if self.environment == "live" and not allow_live:
            raise CTraderFeedError(
                "رفض الاتصال بحساب cTrader حقيقي (live) — هذا محظور افتراضياً. "
                "للسماح به صراحةً: CTraderPriceFeed(allow_live=True) بعد التأكد يدوياً"
            )
        self._host = EndPoints.PROTOBUF_LIVE_HOST if self.environment == "live" else EndPoints.PROTOBUF_DEMO_HOST
        if self.environment == "demo":
            # فحص صريح إضافي: جلسة demo لا يجوز أن تستهدف خادم live أبداً
            assert self._host == EndPoints.PROTOBUF_DEMO_HOST

        # بيانات المصادقة الأساسية إلزامية — نفشل مبكراً برسالة واضحة
        if not settings.ctrader_client_id or not settings.ctrader_client_secret:
            raise CTraderFeedError("CTRADER_CLIENT_ID و CTRADER_CLIENT_SECRET مطلوبان في الإعدادات")
        if not settings.ctrader_account_id:
            raise CTraderFeedError("CTRADER_ACCOUNT_ID مطلوب في الإعدادات")
        try:
            self._account_id = int(settings.ctrader_account_id)
        except ValueError as error:
            raise CTraderFeedError(
                f"CTRADER_ACCOUNT_ID يجب أن يكون رقماً صحيحاً، حصلنا على: '{settings.ctrader_account_id}'"
            ) from error

        self._tokens = _TokenStore()
        if not self._tokens.access() and not self._tokens.refresh_available():
            raise CTraderFeedError(
                "لا يوجد CTRADER_ACCESS_TOKEN ولا CTRADER_REFRESH_TOKEN في الإعدادات — "
                "أضف أحدهما إلى .env (انظر .env.example)"
            )

        self._session: _Session | None = None
        self._lock = threading.Lock()

    # --- العقد العام ---

    def get_candles(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """نفس توقيع العقد تماماً: DataFrame بالأعمدة timestamp, open, high, low, close, volume."""
        if timeframe not in _TIMEFRAME_TO_PERIOD:
            raise CTraderFeedError(
                f"الإطار الزمني '{timeframe}' غير مدعوم — المدعوم: {sorted(_TIMEFRAME_TO_PERIOD)}"
            )
        if count <= 0:
            raise CTraderFeedError(f"count يجب أن يكون موجباً، حصلنا على: {count}")

        session = self._get_session()
        try:
            return self._run_sync(session.fetch_candles_on_reactor, symbol, timeframe, count)
        except CTraderFeedError:
            # أي فشل هنا يعني جلسة غير صالحة — نغلقها ليعاد بناؤها في النداء القادم
            self._invalidate_session()
            raise

    def close(self) -> None:
        """يغلق جلسة cTrader عند إنهاء العمل (اختياري — لا يشترطه العقد)."""
        self._invalidate_session()

    # --- إدارة الجلسة ---

    def _get_session(self) -> _Session:
        with self._lock:
            if self._session is not None and self._session.client.isConnected:
                return self._session
            if self._session is not None:
                self._session.close()
                self._session = None

            session = _Session(
                host=self._host,
                account_id=self._account_id,
                access_token=self._tokens.access(),
                request_timeout=self.timeout,
            )
            try:
                self._run_sync(session.open_on_reactor)
            except CTraderFeedError as error:
                session.close()
                # محاولة تجديد الرمز تلقائياً إذا كان السبب انتهاء صلاحيته
                if self._looks_like_token_expiry(error) and self._tokens.refresh_available():
                    refreshed_access = self._tokens.refresh_access_token()
                    retry_session = _Session(
                        host=self._host,
                        account_id=self._account_id,
                        access_token=refreshed_access,
                        request_timeout=self.timeout,
                    )
                    try:
                        self._run_sync(retry_session.open_on_reactor)
                    except BaseException:
                        retry_session.close()
                        raise
                    self._session = retry_session
                    return retry_session
                raise
            self._session = session
            return session

    def _invalidate_session(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None

    @staticmethod
    def _looks_like_token_expiry(error: CTraderFeedError) -> bool:
        text = str(error).lower()
        return "ch_access_token_invalid" in text or "oa_auth_token_expired" in text

    # --- الجسر المتزامن مع خيط الـ reactor ---

    def _run_sync(self, function: Callable[..., object], *args: object) -> object:
        """ينفّذ دالة على خيط الـ reactor وينتظر نتيجتها بمهلة صارمة.

        أي فشل (شبكة، مهلة، رفض خادم) يُطلق CTraderFeedError في الخيط الرئيسي.
        """
        _ensure_reactor_running()
        event = threading.Event()
        holder: list = []

        def on_success(value: object) -> None:
            holder.append(value)
            event.set()

        def on_failure(failure: object) -> None:
            value = failure.value  # type: ignore[attr-defined]
            if isinstance(value, CTraderFeedError):
                holder.append(value)
            else:
                holder.append(CTraderFeedError(f"فشل طلب cTrader: {value}"))
            event.set()

        def work_on_reactor() -> None:
            try:
                deferred = defer.maybeDeferred(function, *args)
            except BaseException as error:  # أخطاء بناء مبكرة قبل أي Deferred
                holder.append(CTraderFeedError(f"فشل تنفيذ طلب cTrader: {error}"))
                event.set()
                return
            deferred.addCallbacks(on_success, on_failure)

        reactor.callFromThread(work_on_reactor)

        # مهلة الجسر: تغطي أسوأ سلسلة طلبات (اتصال + مصادقتان + رمزان + شموع)
        net_timeout = self.timeout * 6 + 20
        if not event.wait(net_timeout):
            raise CTraderFeedError(
                f"انتهت المهلة ({net_timeout} ثانية) أثناء انتظار استجابة cTrader — "
                "لا تُعالج أي بيانات قبل التأكد من سلامة الاتصال"
            )
        result = holder[0]
        if isinstance(result, BaseException):
            raise result
        return result
