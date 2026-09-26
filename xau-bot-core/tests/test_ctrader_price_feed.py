"""
اختبارات CTraderPriceFeed — بدون أي اتصال شبكي حقيقي.

الفكرة: نحقن وحدات المكتبة الرسمية (ctrader_open_api) المزيفة في sys.modules
قبل استيراد الوحدة، ثم نحاكي استجابات خادم cTrader نفسها (الصيغة النسبية
للأسعار: low + deltas بوحدة 1/100000). الهدف: إثبات أن CTraderPriceFeed
يُنتج DataFrame بنفس شكل وأعمدة وترتيب MockPriceFeed تماماً (العقد الموحد)،
وأنه يفشل بصوت عالٍ عند أي خلل بدل إرجاع بيانات فارغة بصمت.
"""
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

# --- بدائل مبسطة لعناصر Twisted المستخدمة في الوحدة ---


class _FakeError:
    """يحاكي twisted.python.failure.Failure بالحد الأدنى (value + getErrorMessage)."""

    def __init__(self, value: BaseException) -> None:
        self.value = value

    def getErrorMessage(self) -> str:
        return str(self.value)


class _FakeDeferred:
    """بديل أدنى لـ twisted.internet.defer.Deferred — قيمته محسومة مسبقاً."""

    def __init__(self, result=None) -> None:
        self._result = result

    def addCallbacks(self, callback, errback=None):  # noqa: ARG002
        if isinstance(self._result, BaseException):
            if errback is not None:
                errback(_FakeError(self._result))
        else:
            callback(self._result)
        return self


def _fake_maybe_deferred(function, *args, **kwargs):
    """بديل defer.maybeDeferred: ينفّذ متزامناً؛ الخطأ يسير عبر مسار الخط (errback)."""

    class _Immediate:
        def __init__(self, value: object) -> None:
            self.value = value

        def addCallbacks(self, callback, errback=None):  # noqa: ARG002
            if isinstance(self.value, BaseException):
                if errback is not None:
                    errback(_FakeError(self.value))
            else:
                callback(self.value)
            return self

    try:
        return _Immediate(function(*args, **kwargs))
    except BaseException as error:  # نفس سلوك maybeDeferred الحقيقي
        return _Immediate(error)


def _fake_inline_callbacks(function):
    """بديل defer.inlineCallbacks: يقود المولّد حتى النهاية بشكل متزامن.

    يعمل لأن كل الـ Deferreds في بيئة الاختبار محسومة فوراً (لا أحداث شبكية).
    """

    def runner(*args, **kwargs):
        generator = function(*args, **kwargs)
        yielded = next(generator)
        while True:
            if isinstance(yielded, _FakeDeferred):
                if isinstance(yielded._result, BaseException):
                    raise yielded._result
                yielded = generator.send(yielded._result)
            else:
                yielded = generator.send(yielded)

    def guarded(*args, **kwargs):
        try:
            return runner(*args, **kwargs)
        except StopIteration as stop:
            return stop.value

    return guarded


class _FakeReactor:
    """بديل Twisted reactor: ينفّذ العمل المُوجَّه إليه فوراً في نفس الخيط."""

    running = False

    def callFromThread(self, function, *args, **kwargs) -> None:
        function(*args, **kwargs)

    def run(self, install_signal_handlers: bool = True) -> None:  # noqa: ARG002
        _FakeReactor.running = True

    def stop(self) -> None:
        _FakeReactor.running = False


# --- بدائل مبسطة لرسائل Protobuf الرسمية ---


class _FakeProtoMessage:
    """رسالة protobuf مزيفة: أي حقل غير موجود يُنشأ كقائمة (يحاكي repeated fields)."""

    def __getattr__(self, name: str):
        value: list = []
        object.__setattr__(self, name, value)
        return value

    def SerializeToString(self) -> bytes:
        """تسلسل مبسّط لكن حقيقي الشكل: بايتات عبر pickle لكل الحقول المعروفة."""
        import pickle

        state = {
            key: value
            for key, value in vars(self).items()
            if not key.startswith("_")
        }
        return pickle.dumps(state)

    def ParseFromString(self, data: bytes) -> None:
        """فكّ التسلسل إلى نفس الكائن (يكمل ثنائية SerializeToString أعلاه)."""
        import pickle

        vars(self).update(pickle.loads(data))


class _FakeRawEnvelope:
    """نسخة مزيفة من مغلّف الأسلاك الخام ProtoMessage (payloadType + payload=bytes).

    محاكاة لسلوك المكتبة الرسمية: client.send() يعيد هذا المغلّف دائماً دون فكّه،
    فحقل payloadType/payload مشتركان بين كل الرسائل — لهذا تُدار كموصّفات على
    مستوى الصنف لا كخصائص مثيل.
    """

    payloadType = 0
    payload = b""

    def __init__(self, payloadType: int | None = None, payload: bytes = b"") -> None:
        if payloadType is not None:
            self.payloadType = payloadType
        self.payload = payload

    def __repr__(self) -> str:  # مثل الطباعة الحقيقية للرسائل
        return f"payloadType: {self.payloadType}\npayload: {self.payload!r}"


# أرقام payloadType الرسمية من رسائل المكتبة الحقيقية (تحقق بروتوكولي فعلي، لا أرقام مخترعة)
_APP_AUTH_RES_PT = 2101
_ACCOUNT_AUTH_RES_PT = 2103
_SYMBOLS_LIST_RES_PT = 2115
_SYMBOL_BY_ID_RES_PT = 2117
_GET_TRENDBARS_RES_PT = 2138
_ERROR_RES_PT = 2142
_HEARTBEAT_PT = 51


class _FakeTrendbarPeriod:
    """بديل مبسط لـ ProtoOATrendbarPeriod (قيم enum الرسمية)."""

    _values = {"M1": 1, "M5": 5, "M15": 7, "M30": 8, "H1": 9, "H4": 10, "D1": 12}

    @classmethod
    def Value(cls, name: str) -> int:
        return cls._values[name]


class _FakeLoopingCall:
    def __init__(self, function, *args, **kwargs) -> None:  # noqa: ARG002
        self.running = False

    def start(self, interval, now: bool = True) -> None:  # noqa: ARG002
        self.running = True

    def stop(self) -> None:
        self.running = False


def _make_fake_pb2_module(name: str, **symbols: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in symbols.items():
        setattr(module, key, value)
    return module


def _field(name: str) -> property:
    """موصّف بيانات لحقل protobuf مزيف (يُقرأ ويُكتب كخصائص عادية)."""

    def getter(self):
        value = vars(self).get(name)
        if value is None:  # نفس سلوك __getattr__ القديم: قائمة repeated فارغة عند أول وصول
            value = []
            vars(self)[name] = value
        return value

    def setter(self, value) -> None:
        vars(self)[name] = value

    return property(getter, setter)


def _build_fake_modules(fake_client_cls, fake_reactor) -> types.ModuleType:
    """يبني وحدات ctrader_open_api و twisted المزيفة ويثبتها في sys.modules."""
    fake_failure = types.ModuleType("twisted.python.failure")
    fake_failure.Failure = _FakeError

    fake_defer = types.ModuleType("twisted.internet.defer")
    fake_defer.Deferred = _FakeDeferred
    fake_defer.maybeDeferred = _fake_maybe_deferred
    fake_defer.inlineCallbacks = _fake_inline_callbacks

    fake_task = types.ModuleType("twisted.internet.task")
    fake_task.LoopingCall = _FakeLoopingCall

    fake_internet = types.ModuleType("twisted.internet")
    fake_internet.reactor = fake_reactor
    fake_internet.defer = fake_defer

    fake_python = types.ModuleType("twisted.python")
    fake_twisted = types.ModuleType("twisted")
    fake_twisted.internet = fake_internet
    fake_twisted.python = fake_python

    fake_endpoints = types.ModuleType("ctrader_open_api.endpoints")

    class FakeEndPoints:
        AUTH_URI = "https://openapi.ctrader.com/apps/auth"
        TOKEN_URI = "https://openapi.ctrader.com/apps/token"
        PROTOBUF_DEMO_HOST = "demo.ctraderapi.com"
        PROTOBUF_LIVE_HOST = "live.ctraderapi.com"
        PROTOBUF_PORT = 5035

    fake_endpoints.EndPoints = FakeEndPoints

    fake_common = _make_fake_pb2_module(
        "ctrader_open_api.messages.OpenApiCommonMessages_pb2",
        # مغلّف الأسلاك الخام — كود الإنتاج يفحص isinstance(response, ProtoMessage) ثم يفكّه
        ProtoMessage=_FakeRawEnvelope,
        ProtoHeartbeatEvent=type(
            "ProtoHeartbeatEvent", (_FakeProtoMessage,), {"payloadType": _HEARTBEAT_PT}
        ),
        ProtoOAPayloadType=type("ProtoOAPayloadType", (_FakeProtoMessage,), {}),
    )

    class _FakeApplicationAuthRes(_FakeProtoMessage):
        payloadType = _APP_AUTH_RES_PT

    class _FakeAccountAuthRes(_FakeProtoMessage):
        payloadType = _ACCOUNT_AUTH_RES_PT

    class _FakeSymbolsListRes(_FakeProtoMessage):
        payloadType = _SYMBOLS_LIST_RES_PT
        symbol = _field("symbol")

    class _FakeSymbolByIdRes(_FakeProtoMessage):
        payloadType = _SYMBOL_BY_ID_RES_PT
        symbol = _field("symbol")

    class _FakeGetTrendbarsRes(_FakeProtoMessage):
        payloadType = _GET_TRENDBARS_RES_PT
        trendbar = _field("trendbar")

    class _FakeErrorRes(_FakeProtoMessage):
        payloadType = _ERROR_RES_PT
        errorCode = _field("errorCode")
        description = _field("description")

    fake_messages = _make_fake_pb2_module(
        "ctrader_open_api.messages.OpenApiMessages_pb2",
        ProtoOAAccountAuthReq=type("ProtoOAAccountAuthReq", (_FakeProtoMessage,), {}),
        ProtoOAAccountAuthRes=_FakeAccountAuthRes,
        ProtoOAApplicationAuthReq=type("ProtoOAApplicationAuthReq", (_FakeProtoMessage,), {}),
        ProtoOAApplicationAuthRes=_FakeApplicationAuthRes,
        ProtoOAErrorRes=_FakeErrorRes,
        ProtoOAGetTrendbarsReq=type("ProtoOAGetTrendbarsReq", (_FakeProtoMessage,), {}),
        ProtoOAGetTrendbarsRes=_FakeGetTrendbarsRes,
        ProtoOASymbolByIdReq=type("ProtoOASymbolByIdReq", (_FakeProtoMessage,), {}),
        ProtoOASymbolByIdRes=_FakeSymbolByIdRes,
        ProtoOASymbolsListReq=type("ProtoOASymbolsListReq", (_FakeProtoMessage,), {}),
        ProtoOASymbolsListRes=_FakeSymbolsListRes,
    )
    fake_model = _make_fake_pb2_module(
        "ctrader_open_api.messages.OpenApiModelMessages_pb2",
        ProtoOATrendbarPeriod=_FakeTrendbarPeriod,
    )

    fake_messages_pkg = types.ModuleType("ctrader_open_api.messages")
    fake_ctrader_pkg = types.ModuleType("ctrader_open_api")
    fake_ctrader_pkg.Client = fake_client_cls
    fake_ctrader_pkg.EndPoints = FakeEndPoints
    fake_ctrader_pkg.TcpProtocol = object
    fake_ctrader_pkg.messages = fake_messages_pkg
    fake_messages_pkg.OpenApiCommonMessages_pb2 = fake_common
    fake_messages_pkg.OpenApiMessages_pb2 = fake_messages
    fake_messages_pkg.OpenApiModelMessages_pb2 = fake_model

    modules_to_install = {
        "twisted": fake_twisted,
        "twisted.python": fake_python,
        "twisted.python.failure": fake_failure,
        "twisted.internet": fake_internet,
        "twisted.internet.defer": fake_defer,
        "twisted.internet.task": fake_task,
        "ctrader_open_api": fake_ctrader_pkg,
        "ctrader_open_api.endpoints": fake_endpoints,
        "ctrader_open_api.messages": fake_messages_pkg,
        "ctrader_open_api.messages.OpenApiCommonMessages_pb2": fake_common,
        "ctrader_open_api.messages.OpenApiMessages_pb2": fake_messages,
        "ctrader_open_api.messages.OpenApiModelMessages_pb2": fake_model,
    }
    for name, module in modules_to_install.items():
        sys.modules[name] = module
    return fake_ctrader_pkg


def _reload_settings_and_feed():
    """يعيد تحميل الإعدادات ووحدة الـ feed بعد تعديل متغيرات البيئة."""
    import config.settings as settings_module

    importlib.reload(settings_module)
    import brain.data.ctrader_price_feed as feed_module

    importlib.reload(feed_module)
    return feed_module


# --- الـ fixture الرئيسي: بنية مزيفة كاملة + إعدادات demo صالحة ---


@pytest.fixture()
def fake_feed_factory(monkeypatch):
    """يثبت البنية المزيفة ويعيد مصنع feed مع سيطرة كاملة على استجابات الشموع."""
    state = {"trendbars": [], "sent": []}

    saved_modules = {
        name: sys.modules.get(name)
        for name in list(sys.modules)
        if name.startswith(("twisted", "ctrader_open_api"))
    }

    class FakeClient:
        """عميل مزيف يتصرف كخادم cTrader: يقبّل المصادقة ويعيد الشموع المعطاة."""

        def __init__(self, host, port, protocol, retryPolicy=None, **kwargs) -> None:  # noqa: ARG002
            self.host = host
            self.isConnected = False
            self.running = False

        def startService(self) -> None:
            self.running = True
            self.isConnected = True

        def stopService(self) -> None:
            self.running = False
            self.isConnected = False

        def whenConnected(self):
            return _FakeDeferred(self)

        def send(self, message, clientMsgId=None, responseTimeoutInSeconds=5, **params):  # noqa: ARG002
            state["sent"].append(message)
            # نفس سلوك الخادم+المكتبة الحقيقيين: كل استجابة تخرج مغلّفةً في ProtoMessage خام
            def enveloped(result, payload_type: int):
                return _FakeDeferred(
                    _FakeRawEnvelope(payloadType=payload_type, payload=result.SerializeToString())
                )

            if isinstance(message, fake_messages.ProtoOAApplicationAuthReq):
                return enveloped(fake_messages.ProtoOAApplicationAuthRes(), _APP_AUTH_RES_PT)
            if isinstance(message, fake_messages.ProtoOAAccountAuthReq):
                return enveloped(fake_messages.ProtoOAAccountAuthRes(), _ACCOUNT_AUTH_RES_PT)
            if isinstance(message, fake_messages.ProtoOASymbolsListReq):
                res = fake_messages.ProtoOASymbolsListRes()
                res.symbol.append(types.SimpleNamespace(symbolId=845, symbolName="XAUUSD"))
                return enveloped(res, _SYMBOLS_LIST_RES_PT)
            if isinstance(message, fake_messages.ProtoOASymbolByIdReq):
                res = fake_messages.ProtoOASymbolByIdRes()
                res.symbol.append(types.SimpleNamespace(symbolId=845, digits=2))
                return enveloped(res, _SYMBOL_BY_ID_RES_PT)
            if isinstance(message, fake_messages.ProtoOAGetTrendbarsReq):
                res = fake_messages.ProtoOAGetTrendbarsRes()
                for trendbar in state["trendbars"]:
                    res.trendbar.append(trendbar)
                return enveloped(res, _GET_TRENDBARS_RES_PT)
            if type(message).__name__ == "ProtoHeartbeatEvent":
                heartbeat = type(message)()
                return _FakeDeferred(
                    _FakeRawEnvelope(payloadType=_HEARTBEAT_PT, payload=heartbeat.SerializeToString())
                )
            raise RuntimeError(f"رسالة غير متوقعة أُرسلت للشبكة: {type(message).__name__}")

    # تعطيل load_dotenv أثناء إعادة التحميل حتى لا تتسرب قيم .env المحلية للاختبار
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)

    fake_ctrader_pkg = _build_fake_modules(FakeClient, _FakeReactor())
    fake_messages = fake_ctrader_pkg.messages.OpenApiMessages_pb2

    # إعدادات demo صالحة (تُقرأ من config.settings — نفس آلية الإنتاج تماماً)
    monkeypatch.setenv("CTRADER_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CTRADER_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("CTRADER_ACCOUNT_ID", "123456")
    monkeypatch.setenv("CTRADER_ENVIRONMENT", "demo")
    monkeypatch.setenv("CTRADER_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setenv("CTRADER_CONNECTION_TIMEOUT_SECONDS", "2")

    feed_module = _reload_settings_and_feed()

    def make(trendbars: list) -> "feed_module.CTraderPriceFeed":
        state["trendbars"] = trendbars
        return feed_module.CTraderPriceFeed()

    yield make

    # استعادة sys.modules الأصلية (سلامة بقية الاختبارات)
    for name, module in saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _make_relative_trendbars(count: int, period_minutes: int) -> list:
    """شموع بصيغة cTrader النسبية: low بوحدة 1/100000 + deltas + طابع زمني بالدقائق."""
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(
        minutes=count * period_minutes
    )
    bars = []
    for i in range(count):
        bars.append(
            types.SimpleNamespace(
                utcTimestampInMinutes=int(start.timestamp() // 60) + i * period_minutes,
                low=2650 * 100_000 + i * 10_000,  # 2650.00 + 0.10 لكل شمعة (الوحدة 1/100000 كما في بروتوكول cTrader الرسمي)
                deltaOpen=0,
                deltaClose=10_000,  # +0.10 فوق low
                deltaHigh=20_000,   # +0.20 فوق low
                volume=100 + i,
            )
        )
    return bars


# --- الاختبارات ---


def test_contract_same_columns_and_shape_as_mock(fake_feed_factory):
    """العقد: نفس الأعمدة ونفس عدد الصفوف ونفس ترتيب الأعمدة مثل MockPriceFeed تماماً."""
    from brain.data.mock_price_feed import MockPriceFeed

    count = 50
    real_df = fake_feed_factory(_make_relative_trendbars(count, 15)).get_candles("XAUUSD", "M15", count)
    mock_df = MockPriceFeed().get_candles("XAUUSD", "M15", count)

    assert list(real_df.columns) == list(mock_df.columns) == [
        "timestamp", "open", "high", "low", "close", "volume",
    ]
    assert len(real_df) == count
    assert real_df.shape[1] == mock_df.shape[1]


def test_candles_sorted_oldest_to_newest_and_prices_decoded(fake_feed_factory):
    """الترتيب من الأقدم إلى الأحدث كما يشترط العقد، والأسعار مفكوكة من الصيغة النسبية."""
    count = 30
    df = fake_feed_factory(_make_relative_trendbars(count, 15)).get_candles("XAUUSD", "M15", count)

    timestamps = df["timestamp"].tolist()
    assert timestamps == sorted(timestamps)

    # low = 2650.00 + i*0.10 ، close = low + deltaClose(=10_000 → 0.10) ، high = low + deltaHigh(=20_000 → 0.20)
    first, last = df.iloc[0], df.iloc[-1]
    assert first["low"] == pytest.approx(2650.00)
    assert first["open"] == pytest.approx(2650.00)
    assert first["close"] == pytest.approx(2650.10)
    assert first["high"] == pytest.approx(2650.20)
    assert last["low"] == pytest.approx(2650.00 + (count - 1) * 0.10)
    assert (last["high"] - last["low"]) == pytest.approx(0.20)
    # التقريب إلى خانات الرمز (digits=2 مثل الذهب في معظم وسطاء cTrader)
    for column in ["open", "high", "low", "close"]:
        assert (df[column].round(2) == df[column]).all()


def test_empty_trendbars_raises_loudly(fake_feed_factory):
    """ممنوع الصمت: لا شموع → استثناء واضح، لا DataFrame فارغ."""
    feed = fake_feed_factory([])
    with pytest.raises(Exception) as excinfo:
        feed.get_candles("XAUUSD", "M15", 10)
    assert "لم يُعد cTrader أي شموع" in str(excinfo.value)


def test_network_failure_raises_instead_of_empty_data(fake_feed_factory, monkeypatch):
    """عطل الشبكة أثناء طلب الشموع → استثناء صريح (لا بيانات فارغة ولا وهمية)."""
    import brain.data.ctrader_price_feed as feed_module

    feed = fake_feed_factory(_make_relative_trendbars(10, 15))
    feed._session = None

    def failing_fetch(self, symbol, timeframe, count):  # noqa: ARG001
        raise RuntimeError("connection lost")

    monkeypatch.setattr(feed_module._Session, "fetch_candles_on_reactor", failing_fetch)

    with pytest.raises(Exception) as excinfo:
        feed.get_candles("XAUUSD", "M15", 10)
    assert "connection lost" in str(excinfo.value)


def test_unsupported_timeframe_raises(fake_feed_factory):
    feed = fake_feed_factory(_make_relative_trendbars(5, 15))
    with pytest.raises(Exception) as excinfo:
        feed.get_candles("XAUUSD", "M2", 10)
    assert "غير مدعوم" in str(excinfo.value)


def test_live_environment_is_blocked_by_default(fake_feed_factory, monkeypatch):
    """الحماية الصريحة: live ممنوع ما لم يُسمح به صراحةً بـ allow_live=True."""
    monkeypatch.setenv("CTRADER_ENVIRONMENT", "live")
    feed_module = _reload_settings_and_feed()

    with pytest.raises(Exception) as excinfo:
        feed_module.CTraderPriceFeed()
    assert "live" in str(excinfo.value).lower()


def test_live_environment_allowed_only_with_explicit_flag(fake_feed_factory, monkeypatch):
    """المسار الوحيد المسموح: live + allow_live=True (بدون أي اتصال فعلي في الاختبار)."""
    monkeypatch.setenv("CTRADER_ENVIRONMENT", "live")
    feed_module = _reload_settings_and_feed()

    feed = feed_module.CTraderPriceFeed(allow_live=True)
    assert feed.environment == "live"
    # لا جلسة ولا اتصال قبل أول استدعاء فعلي لـ get_candles
    assert feed._session is None


def test_missing_credentials_fail_fast(fake_feed_factory, monkeypatch):
    """لا توكن ولا رمز تجديد → فشل مبكر برسالة واضحة (لا تعليق ولا بيانات وهمية)."""
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("CTRADER_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CTRADER_REFRESH_TOKEN", raising=False)
    feed_module = _reload_settings_and_feed()

    with pytest.raises(Exception) as excinfo:
        feed_module.CTraderPriceFeed()
    assert "CTRADER_ACCESS_TOKEN" in str(excinfo.value)


# --- اختبارات فكّ المغلّف الخام (نقطة الفشل التي كشفها الاتصال الحقيقي فقط) ---
# ملاحظة: الـ FakeClient في الـ fixture يغلّف كل استجاباتها فعلياً كما يفعل الخادم+المكتبة
# الحقيقيان، فكل الاختبارات أعلاه تمر حقيقةً عبر _unwrap_response. الاختبارات الثلاثة
# أدناه تفحص مسار الفكّ نفسه صراحةً على مستوى الرسالة الواحدة.


def _make_envelope(message, envelope_cls):
    """يبني مغلف ProtoMessage حقيقياً: payloadType صحيح + payload مُسلسل فعلياً."""
    envelope = envelope_cls()
    envelope.payloadType = message.payloadType
    envelope.payload = message.SerializeToString()
    return envelope


def test_expect_unwraps_raw_envelope_and_returns_decoded_message(fake_feed_factory):
    """المسار الحقيقي: send() يعيد مغلف ProtoMessage(payloadType، payload=بايتات مُسلسلة)
    و _expect يجب أن يفكّه ويعيد الرسالة المفكوكة بنوعها وحقولها الصحيحة."""
    feed_module = _reload_settings_and_feed()
    Envelope = feed_module.ProtoMessage  # المغلّف الخام

    # رسالة بحقول فعلية (قائمة رموز كما يعيدها السيرفر)
    symbols_res = feed_module.ProtoOASymbolsListRes()
    symbols_res.symbol.append(types.SimpleNamespace(symbolId=845, symbolName="XAUUSD"))

    envelope = _make_envelope(symbols_res, Envelope)
    # إثبات الشكل: مغلّف خام وليس الرسالة نفسها
    assert isinstance(envelope, Envelope)
    assert envelope.payloadType == symbols_res.payloadType
    assert isinstance(envelope.payload, (bytes, bytearray)) and len(envelope.payload) > 0

    decoded = feed_module._Session._expect(envelope, feed_module.ProtoOASymbolsListRes, "جلب قائمة الرموز")
    # الفكّ ناجح: النوع المتوقع، والحقول المُسلسلة سليمة بعد الفكّ
    assert isinstance(decoded, feed_module.ProtoOASymbolsListRes)
    assert decoded.symbol[0].symbolId == 845
    assert decoded.symbol[0].symbolName == "XAUUSD"


def test_expect_unwraps_error_envelope_into_loud_failure(fake_feed_factory):
    """مغلّف خطأ من الخادم (ProtoOAErrorRes داخل ProtoMessage) → فشل بصوت عالٍ، لا صمت."""
    feed_module = _reload_settings_and_feed()

    error_res = feed_module.ProtoOAErrorRes()
    error_res.errorCode = "CH_ACCESS_TOKEN_INVALID"
    error_res.description = "token expired"

    envelope = _make_envelope(error_res, feed_module.ProtoMessage)
    with pytest.raises(Exception) as excinfo:
        feed_module._Session._expect(envelope, feed_module.ProtoOAAccountAuthRes, "مصادقة الحساب")
    assert "CH_ACCESS_TOKEN_INVALID" in str(excinfo.value)


def test_expect_rejects_unrelated_envelope_loudly(fake_feed_factory):
    """مغلّف برسالة غير متعلقة (مثل ProtoHeartbeatEvent صادراً في وقت غير متوقع)
    أثناء انتظار استجابة محددة → رفض صريح يذكر payloadType، لا صمت ولا تخطٍّ أعمى."""
    feed_module = _reload_settings_and_feed()

    envelope = feed_module.ProtoMessage()
    envelope.payloadType = 51  # ProtoHeartbeatEvent (قيمة بروتوكولية حقيقية)
    envelope.payload = b""

    with pytest.raises(Exception) as excinfo:
        feed_module._Session._expect(envelope, feed_module.ProtoOAApplicationAuthRes, "مصادقة التطبيق")
    assert "51" in str(excinfo.value)
