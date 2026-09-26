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
        ProtoHeartbeatEvent=type("ProtoHeartbeatEvent", (_FakeProtoMessage,), {}),
        ProtoOAPayloadType=type("ProtoOAPayloadType", (_FakeProtoMessage,), {}),
    )
    fake_messages = _make_fake_pb2_module(
        "ctrader_open_api.messages.OpenApiMessages_pb2",
        ProtoOAAccountAuthReq=type("ProtoOAAccountAuthReq", (_FakeProtoMessage,), {}),
        ProtoOAAccountAuthRes=type("ProtoOAAccountAuthRes", (_FakeProtoMessage,), {}),
        ProtoOAApplicationAuthReq=type("ProtoOAApplicationAuthReq", (_FakeProtoMessage,), {}),
        ProtoOAApplicationAuthRes=type("ProtoOAApplicationAuthRes", (_FakeProtoMessage,), {}),
        ProtoOAErrorRes=type(
            "ProtoOAErrorRes",
            (_FakeProtoMessage,),
            {"errorCode": "GENERIC_ERROR", "description": "fake error"},
        ),
        ProtoOAGetTrendbarsReq=type("ProtoOAGetTrendbarsReq", (_FakeProtoMessage,), {}),
        ProtoOAGetTrendbarsRes=type("ProtoOAGetTrendbarsRes", (_FakeProtoMessage,), {}),
        ProtoOASymbolByIdReq=type("ProtoOASymbolByIdReq", (_FakeProtoMessage,), {}),
        ProtoOASymbolByIdRes=type("ProtoOASymbolByIdRes", (_FakeProtoMessage,), {}),
        ProtoOASymbolsListReq=type("ProtoOASymbolsListReq", (_FakeProtoMessage,), {}),
        ProtoOASymbolsListRes=type("ProtoOASymbolsListRes", (_FakeProtoMessage,), {}),
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
            if isinstance(message, fake_messages.ProtoOAApplicationAuthReq):
                return _FakeDeferred(fake_messages.ProtoOAApplicationAuthRes())
            if isinstance(message, fake_messages.ProtoOAAccountAuthReq):
                return _FakeDeferred(fake_messages.ProtoOAAccountAuthRes())
            if isinstance(message, fake_messages.ProtoOASymbolsListReq):
                res = fake_messages.ProtoOASymbolsListRes()
                res.symbol.append(types.SimpleNamespace(symbolId=845, symbolName="XAUUSD"))
                return _FakeDeferred(res)
            if isinstance(message, fake_messages.ProtoOASymbolByIdReq):
                res = fake_messages.ProtoOASymbolByIdRes()
                res.symbol.append(types.SimpleNamespace(symbolId=845, digits=2))
                return _FakeDeferred(res)
            if isinstance(message, fake_messages.ProtoOAGetTrendbarsReq):
                res = fake_messages.ProtoOAGetTrendbarsRes()
                for trendbar in state["trendbars"]:
                    res.trendbar.append(trendbar)
                return _FakeDeferred(res)
            if type(message).__name__ == "ProtoHeartbeatEvent":
                return _FakeDeferred(None)
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
                low=2650_00 + i * 10,  # 2650.00 + 0.10 لكل شمعة (وحدة 1/100000)
                deltaOpen=0,
                deltaClose=10,
                deltaHigh=20,
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

    # low = 2650.00 + i*0.10 ، close = low + deltaClose(=10 → 0.10) ، high = low + deltaHigh(=20 → 0.20)
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
