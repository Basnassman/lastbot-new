"""اختبارات إصلاح صافي نتيجة اليوم (Daily Net PnL) — المجموعات التسع من التدقيق.

يغطي هذا الملف:
1) +100 ثم -30 = +70 صافي.
2) -60 ثم +50 ثم -60 = -70 صافي.
3) القاطع يُفعَّل عند -100 بالضبط (الحد 100) ولا قبله.
4) ربح لاحق يرفع الصافي فوق حد الخسارة ويُعيد التقييم الناجح.
5) تكامل الـendpoint مع نتائج موجبة وسالبة.
6) سلوك الخسائر فقط (legacy) لم يتغيّر.
7) حد اليوم UTC (وحدّ تاريخ اليوم في المدير) بلا اختلاط بين الأيام.
8) أمان منع التكرار والسجلات غير المحسومة.
9) مطابقة 30 أكتوبر: +153.48 ربح − 136.50 خسارة = +16.98 (حساب سنتات دقيق).

لا شبكة حقيقية ولا أوامر تداول: TestClient + متجر SQLite بمجلد مؤقت فقط.
"""
import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from brain.api import main as api_main
from brain.risk import risk_manager as risk_manager_module
from brain.risk.risk_manager import RiskManager
from brain.risk.trade_result_store import TradeResultStore
from config.settings import settings


@pytest.fixture()
def fixed_limit(monkeypatch):
    """حد خسارة يومي ثابت 100$ مستقل عن قيم .env وقت التشغيل (1000$ × 10%)."""
    monkeypatch.setattr(
        risk_manager_module,
        "settings",
        replace(settings, account_balance_usd=1000.0, max_daily_loss_percent=10.0),
    )
    return 100.0


def _client() -> TestClient:
    return TestClient(api_main.app)


# --------------------------------------------------------------------------- 1
def test_win_then_loss_yields_net_seventy(fixed_limit):
    manager = RiskManager()

    manager.register_trade_result(100.0)
    assert manager._daily_net_pnl_usd == pytest.approx(100.0)

    manager.register_trade_result(-30.0)
    assert manager._daily_net_pnl_usd == pytest.approx(70.0)

    assert manager.evaluate(confidence=95.0, atr_value=1.0).approved is True


# --------------------------------------------------------------------------- 2
def test_losses_and_win_accumulate_as_signed_net(fixed_limit):
    manager = RiskManager()
    for pnl in (-60.0, 50.0, -60.0):
        manager.register_trade_result(pnl)

    # القديم: 60+60=120 (خسائر مطلقة). الصحيح: -60+50-60 = -70.
    assert manager._daily_net_pnl_usd == pytest.approx(-70.0)
    assert manager.evaluate(confidence=95.0, atr_value=1.0).approved is True


# --------------------------------------------------------------------------- 3
def test_breaker_activates_exactly_at_daily_loss_limit(fixed_limit):
    manager = RiskManager()
    manager.register_trade_result(-99.99)
    assert manager.evaluate(confidence=95.0, atr_value=1.0).approved is True

    manager.register_trade_result(-0.01)
    assert manager._daily_net_pnl_usd == pytest.approx(-fixed_limit)
    decision = manager.evaluate(confidence=95.0, atr_value=1.0)
    assert decision.approved is False
    assert decision.position_size_usd == 0.0
    assert "قاطع الدائرة" in decision.reason
    assert "-100.00" in decision.reason  # الرسالة تصف الصافي السالب بدقة


def test_breaker_does_not_activate_above_limit_with_gross_losses(fixed_limit):
    # خسائر إجمالية 136.50 لكن الصافي موجب: لا قاطع (حالة 30 أكتوبر).
    manager = RiskManager()
    manager.register_trade_result(-136.50)
    manager.register_trade_result(153.48)

    assert manager._daily_net_pnl_usd == pytest.approx(16.98)
    assert manager.evaluate(confidence=95.0, atr_value=1.0).approved is True


# --------------------------------------------------------------------------- 4
def test_profit_releases_activated_breaker(fixed_limit):
    manager = RiskManager()
    manager.register_trade_result(-100.0)
    assert manager.evaluate(confidence=95.0, atr_value=1.0).approved is False

    manager.register_trade_result(40.0)
    assert manager._daily_net_pnl_usd == pytest.approx(-60.0)
    decision = manager.evaluate(confidence=95.0, atr_value=1.0)
    assert decision.approved is True
    assert decision.position_size_usd > 0.0


# --------------------------------------------------------------------------- 5
def test_endpoint_applies_positive_and_negative_results(trade_store, fresh_risk_manager):
    client = _client()

    assert client.post("/trade-result", params={"pnl_usd": "50", "deal_id": "W-1"}).json() == {"status": "recorded"}
    assert client.post("/trade-result", params={"pnl_usd": "-20", "deal_id": "L-1"}).json() == {"status": "recorded"}
    assert client.post("/trade-result", params={"pnl_usd": "10", "deal_id": "W-2"}).json() == {"status": "recorded"}

    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(40.0)
    rows = trade_store.fetch_all()
    assert [row["pnl_usd"] for row in rows] == pytest.approx([50.0, -20.0, 10.0])
    assert all(row["status"] == "applied" for row in rows)


def test_endpoint_net_loss_below_limit_blocks_and_profit_unblocks(trade_store, fresh_risk_manager, fixed_limit):
    client = _client()
    assert client.post("/trade-result", params={"pnl_usd": "-120", "deal_id": "L-BIG"}).json() == {"status": "recorded"}
    assert fresh_risk_manager.evaluate(confidence=95.0, atr_value=1.0).approved is False

    assert client.post("/trade-result", params={"pnl_usd": "30", "deal_id": "W-RELIEF"}).json() == {"status": "recorded"}
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-90.0)
    assert fresh_risk_manager.evaluate(confidence=95.0, atr_value=1.0).approved is True


# --------------------------------------------------------------------------- 6
def test_loss_only_legacy_behavior_is_preserved(trade_store, fresh_risk_manager):
    client = _client()
    client.post("/trade-result", params={"pnl_usd": "-12.5"})
    client.post("/trade-result", params={"pnl_usd": "-7.5"})

    # لا معرّفات: توافق عكسي بلا dedup، والخسائر تتراكم كما كانت (بإشارة سالبة).
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-20.0)
    rows = trade_store.fetch_all()
    assert len(rows) == 2
    assert all(row["dedup_applied"] == 0 for row in rows)


# --------------------------------------------------------------------------- 7
class _FrozenDate(date):
    """تاريخ قابل للتثبيت لاختبار حد اليوم بلا انتظار منتصف الليل الحقيقي."""

    frozen = date(2026, 10, 30)

    @classmethod
    def today(cls) -> date:
        return cls.frozen


def test_utc_day_boundary_resets_net_pnl(monkeypatch, fixed_limit):
    monkeypatch.setattr(risk_manager_module, "date", _FrozenDate)
    manager = RiskManager()

    manager.register_trade_result(-80.0)
    manager.register_trade_result(20.0)
    assert manager._daily_net_pnl_usd == pytest.approx(-60.0)
    assert manager._current_day == date(2026, 10, 30)

    # منتصف الليل UTC (اليوم التالي): الصافي يبدأ من صفر ثم يُطبَّق الموجب الجديد.
    _FrozenDate.frozen = date(2026, 10, 31)
    manager.register_trade_result(15.0)
    assert manager._daily_net_pnl_usd == pytest.approx(15.0)
    assert manager._current_day == date(2026, 10, 31)

    # التقييم نفسه يُصفّر عند تغيّر اليوم قبل قراءة القرار.
    _FrozenDate.frozen = date(2026, 11, 1)
    assert manager.evaluate(confidence=95.0, atr_value=1.0).approved is True
    assert manager._daily_net_pnl_usd == 0.0


# --------------------------------------------------------------------------- 8
def test_duplicate_deal_id_updates_net_pnl_once(trade_store, fresh_risk_manager):
    client = _client()
    first = client.post("/trade-result", params={"pnl_usd": "40", "deal_id": "DUP-WIN"})
    retry = client.post("/trade-result", params={"pnl_usd": "40", "deal_id": "DUP-WIN"})

    assert first.json() == {"status": "recorded"}
    assert retry.json()["status"] == "duplicate"
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(40.0)
    assert trade_store.fetch_all()[0]["duplicate_attempts"] == 1


def test_unresolved_record_never_moves_net_pnl(trade_store, fresh_risk_manager, tmp_path, monkeypatch):
    stale = TradeResultStore(tmp_path / "stale.sqlite3")
    outcome = stale.claim(
        event_id="stale-loss", deal_id="DLG-STUCK",
        account_scope=(settings.ctrader_account_id or "").strip(),
        pnl_usd=-50.0, received_at_utc="2026-10-30T00:00:00+00:00",
    )
    stale.mark_applying(outcome.event_id)
    monkeypatch.setattr(api_main, "_trade_result_store", stale)

    retry = _client().post("/trade-result", params={"pnl_usd": "-50", "deal_id": "DLG-STUCK"})
    assert retry.status_code == 503
    assert retry.json()["error"] == "trade_result_unresolved"
    assert fresh_risk_manager._daily_net_pnl_usd == 0.0
    assert stale.fetch_all()[0]["status"] == "applying"


def test_legacy_request_without_deal_id_is_logged_as_non_idempotent(trade_store, fresh_risk_manager, caplog):
    with caplog.at_level(logging.WARNING, logger="brain.api.trade_result"):
        response = _client().post("/trade-result", params={"pnl_usd": "5"})

    assert response.json() == {"status": "recorded"}
    assert any("without deal_id" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- 9
# نتائج 30 أكتوبر 2026 المخزّنة فعليًا (25 ربحًا / 14 خسارة) — تُستخدم كاختبار انحدار.
OCT_30_PROFITS = [
    5.42, 0.94, 0.64, 5.22, 11.26, 8.66, 6.76, 8.07, 15.89, 8.48,
    0.32, 7.61, 6.45, 12.93, 8.06, 6.86, 13.79, 9.11, 0.12, 8.75,
    0.11, 0.35, 7.52, 0.12, 0.04,
]
OCT_30_LOSSES = [
    -27.88, -9.30, -9.38, -0.04, -1.40, -0.63, -10.19,
    -20.20, -10.11, -0.05, -0.07, -9.71, -9.51, -28.03,
]


def test_october_30_reconciliation_exact_cents():
    to_decimal = lambda values: sum((Decimal(str(v)) for v in values), Decimal("0"))
    gross_profit = to_decimal(OCT_30_PROFITS)
    gross_loss = -to_decimal(OCT_30_LOSSES)
    net = to_decimal(OCT_30_PROFITS + OCT_30_LOSSES)

    assert len(OCT_30_PROFITS) == 25
    assert len(OCT_30_LOSSES) == 14
    assert gross_profit == Decimal("153.48")
    assert gross_loss == Decimal("136.50")
    assert gross_profit - gross_loss == Decimal("16.98")
    assert net == Decimal("16.98")


def test_october_30_day_is_not_blocked_by_gross_losses(fixed_limit):
    manager = RiskManager()
    for pnl in OCT_30_PROFITS + OCT_30_LOSSES:
        manager.register_trade_result(pnl)

    # 136.50 خسائر إجمالية > 100 لكن الصافي +16.98: القاطع يجب ألا يعمل.
    assert manager._daily_net_pnl_usd == pytest.approx(16.98, abs=1e-9)
    assert manager.evaluate(confidence=95.0, atr_value=1.0).approved is True
