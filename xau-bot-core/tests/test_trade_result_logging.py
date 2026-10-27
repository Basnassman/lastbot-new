"""اختبارات الخطوتين 1–2: تدقيق نتائج الصفقات + منع التكرار.

تغطية المتطلبات:
 1) pnl سالب صالح يُسجَّل ويُعالَج مرة واحدة.
 2) pnl موجب يمر بالسلوك الحالي دون تغيير سياسة المخاطر (العداد لا يزيد).
 3) تكرار نفس deal_id لا يطبّق pnl مرتين.
 4) معرّفان مختلفان بنفس قيمة pnl يُعالَجان باستقلالية.
 5) طلبان متزامنان لنفس deal_id → تحديث واحد فقط للعدّاد.
 6) المعرّف المفقود/الملوّث يتبع السياسة الموثّقة (توافق عكسي / رفض).
 7) قيم pnl غير الصالحة (NaN/Infinity/نص) تُرفض.
 8) فشل التخزين لا ينتج استجابة نجاح زائفة.
 9) معالجة مقاطعة/فاشلة تُعاد بأمان دون احتساب مزدوج.
10) سلوك التعافي بعد "إعادة التشغيل" بقدر ما يدعمه التنفيذ.
11) سلوك الـendpoint القديم والاختبارات غير المرتبطة تبقى كما هي (يُشغَّل السوت كله).

لا شبكة حقيقية ولا أوامر تداول: TestClient + متجر SQLite بمجلد مؤقت فقط.
"""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from brain.api import main as api_main
from brain.risk.risk_manager import RiskManager
from brain.risk.trade_result_store import TradeResultStore
from config.settings import settings


def _client() -> TestClient:
    return TestClient(api_main.app)


# --------------------------------------------------------------------------- 1
def test_negative_pnl_is_logged_and_processed_once(trade_store, fresh_risk_manager):
    response = _client().post("/trade-result", params={"pnl_usd": "-12.5", "deal_id": "DLG-1"})

    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}  # عقد قديم محفوظ حرفيًا
    assert fresh_risk_manager._daily_loss_usd == pytest.approx(12.5)

    rows = trade_store.fetch_all()
    assert len(rows) == 1
    row = rows[0]
    assert row["deal_id"] == "DLG-1"
    assert row["pnl_usd"] == pytest.approx(-12.5)  # الإشارة والقيمة كما وردتا
    assert row["status"] == "applied"
    assert row["risk_manager_updated"] == 1
    assert row["dedup_applied"] == 1
    assert row["received_at_utc"].endswith("+00:00")  # طابع UTC


# --------------------------------------------------------------------------- 2
def test_positive_pnl_keeps_existing_risk_policy(trade_store, fresh_risk_manager):
    response = _client().post("/trade-result", params={"pnl_usd": "100", "deal_id": "DLG-WIN"})

    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}
    # سياسة العداد الحالية (مجموع الخسائر فقط) لم تتغير: الربح لا يزيد ولا ينقص
    assert fresh_risk_manager._daily_loss_usd == 0.0

    row = trade_store.fetch_all()[0]
    assert row["pnl_usd"] == pytest.approx(100.0)  # موجب كما ورد، مسجّل
    assert row["status"] == "applied"


# --------------------------------------------------------------------------- 3
def test_same_deal_id_is_not_applied_twice(trade_store, fresh_risk_manager):
    client = _client()
    first = client.post("/trade-result", params={"pnl_usd": "-30", "deal_id": "DLG-DUP"})
    second = client.post("/trade-result", params={"pnl_usd": "-30", "deal_id": "DLG-DUP"})
    third = client.post("/trade-result", params={"pnl_usd": "-30", "deal_id": "DLG-DUP"})

    assert first.json() == {"status": "recorded"}
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert third.json()["status"] == "duplicate"
    assert fresh_risk_manager._daily_loss_usd == pytest.approx(30.0)  # احتساب واحد فقط

    rows = trade_store.fetch_all()
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"
    assert rows[0]["duplicate_attempts"] == 2  # محاولا التكرار مسجّلتان للتدقيق


# --------------------------------------------------------------------------- 4
def test_distinct_ids_with_identical_pnl_are_independent(trade_store, fresh_risk_manager):
    client = _client()
    first = client.post("/trade-result", params={"pnl_usd": "-30", "deal_id": "DLG-A"})
    second = client.post("/trade-result", params={"pnl_usd": "-30", "deal_id": "DLG-B"})

    assert first.json() == {"status": "recorded"}
    assert second.json() == {"status": "recorded"}
    # قيم pnl متطابقة لكن المعرّفات مختلفة → مستقلتان تمامًا (لا dedup بالقيمة)
    assert fresh_risk_manager._daily_loss_usd == pytest.approx(60.0)
    assert len(trade_store.fetch_all()) == 2


# --------------------------------------------------------------------------- 5
def test_concurrent_duplicate_requests_update_counter_once(trade_store, fresh_risk_manager):
    client = _client()
    start = threading.Barrier(8)

    def _post() -> dict:
        start.wait(timeout=10)
        return client.post("/trade-result", params={"pnl_usd": "-20", "deal_id": "DLG-RACE"}).json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        bodies = list(pool.map(lambda _: _post(), range(8)))

    recorded = [b for b in bodies if b.get("status") == "recorded"]
    duplicates = [b for b in bodies if b.get("status") == "duplicate"]
    assert len(recorded) == 1  # ادّعاء واحد فقط ينجح
    assert len(duplicates) == 7
    assert fresh_risk_manager._daily_loss_usd == pytest.approx(20.0)  # تحديث واحد فقط

    rows = trade_store.fetch_all()
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"
    assert rows[0]["duplicate_attempts"] == 7


# --------------------------------------------------------------------------- 6
def test_missing_deal_id_backward_compatible_no_silent_dedup(trade_store, fresh_risk_manager):
    client = _client()
    first = client.post("/trade-result", params={"pnl_usd": "-10"})
    second = client.post("/trade-result", params={"pnl_usd": "-10"})

    # توافق عكسي كامل: بلا معرّف تعمل القديم (سجّل + عالج) — بلا dedup ومُوثّقة
    assert first.json() == {"status": "recorded"}
    assert second.json() == {"status": "recorded"}
    assert fresh_risk_manager._daily_loss_usd == pytest.approx(20.0)

    rows = trade_store.fetch_all()
    assert len(rows) == 2
    assert all(row["dedup_applied"] == 0 for row in rows)


def test_whitespace_deal_id_is_rejected(trade_store, fresh_risk_manager):
    response = _client().post("/trade-result", params={"pnl_usd": "-10", "deal_id": "   "})

    assert response.status_code == 400
    assert response.json() == {"status": "rejected", "error": "empty_deal_id"}
    assert fresh_risk_manager._daily_loss_usd == 0.0
    row = trade_store.fetch_all()[0]
    assert row["status"] == "rejected"


def test_missing_pnl_param_is_framework_rejected(trade_store, fresh_risk_manager):
    response = _client().post("/trade-result", params={"deal_id": "DLG-X"})
    assert response.status_code == 422  # رفض آمن من تحقق FastAPI قبل أي معالجة
    assert fresh_risk_manager._daily_loss_usd == 0.0
    assert trade_store.fetch_all() == []  # لا سجل ولا عداد — موثّق كحدّ في التقرير


# --------------------------------------------------------------------------- 7
@pytest.mark.parametrize("bad_pnl", ["NaN", "Infinity", "-Infinity", "abc", ""])
def test_invalid_pnl_values_are_rejected(bad_pnl, trade_store, fresh_risk_manager):
    response = _client().post("/trade-result", params={"pnl_usd": bad_pnl, "deal_id": f"DLG-{bad_pnl or 'EMPTY'}"})

    assert response.status_code == 400
    assert response.json() == {"status": "rejected", "error": "invalid_pnl_usd"}
    assert fresh_risk_manager._daily_loss_usd == 0.0
    assert trade_store.fetch_all()[0]["status"] == "rejected"


# --------------------------------------------------------------------------- 8
def test_persistence_failure_never_returns_false_success(trade_store, fresh_risk_manager, monkeypatch):
    class _BrokenStore:
        def claim(self, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(api_main, "_trade_result_store", _BrokenStore())
    response = _client().post("/trade-result", params={"pnl_usd": "-50", "deal_id": "DLG-BROKEN"})

    assert response.status_code == 503
    assert response.json() == {"status": "error", "error": "audit_store_unavailable"}
    assert fresh_risk_manager._daily_loss_usd == 0.0  # لا تحديث قبل قبول دائم


def test_finalize_failure_after_apply_is_not_reported_success(trade_store, fresh_risk_manager, monkeypatch):
    original_mark_applied = TradeResultStore.mark_applied
    calls = {"n": 0}

    def _flaky_mark_applied(self, event_id, applied_at_utc):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("finalize down")
        return original_mark_applied(self, event_id, applied_at_utc)

    monkeypatch.setattr(TradeResultStore, "mark_applied", _flaky_mark_applied)
    client = _client()
    first = client.post("/trade-result", params={"pnl_usd": "-15", "deal_id": "DLG-FIN"})

    # العداد حُدِّث لكن التثبيت فشل → لا يُعلَم نجاح (503) والسجل يبقى claimed
    assert first.status_code == 503
    assert first.json() == {"status": "error", "error": "audit_finalize_failed"}
    assert fresh_risk_manager._daily_loss_usd == pytest.approx(15.0)

    # أي إعادة إرسال لاحقة = duplicate (لا احتساب ثانٍ رغم أن التسجيل لم يكتمل)
    retry = client.post("/trade-result", params={"pnl_usd": "-15", "deal_id": "DLG-FIN"})
    assert retry.json()["status"] == "duplicate"
    assert fresh_risk_manager._daily_loss_usd == pytest.approx(15.0)


# --------------------------------------------------------------------------- 9
class _FlakyRiskManager(RiskManager):
    """مدير مخاطر يفشل تسجيل النتيجة في أول محاولة فقط (محاكاة خطأ معالجة)."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0
        self._fail_once = True

    def register_trade_result(self, pnl_usd: float) -> None:
        self.attempts += 1
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("simulated processing failure")
        super().register_trade_result(pnl_usd)


def test_interrupted_processing_can_be_retried_safely(trade_store, monkeypatch):
    flaky = _FlakyRiskManager()
    monkeypatch.setattr(api_main, "_risk_manager", flaky)
    client = _client()

    first = client.post("/trade-result", params={"pnl_usd": "-40", "deal_id": "DLG-RETRY"})
    assert first.status_code == 500
    assert first.json() == {"status": "error", "error": "risk_update_failed"}
    assert flaky._daily_loss_usd == 0.0
    row = trade_store.fetch_all()[0]
    assert row["status"] == "error"
    assert "simulated processing failure" in row["error_detail"]

    # إعادة الطلب بعد الفشل: تُعيد الادّعاء وتُنجح مرة واحدة فقط
    retry = client.post("/trade-result", params={"pnl_usd": "-40", "deal_id": "DLG-RETRY"})
    assert retry.json() == {"status": "recorded"}
    assert flaky._daily_loss_usd == pytest.approx(40.0)  # احتساب واحد
    row = trade_store.fetch_all()[0]
    assert row["status"] == "applied"
    assert row["risk_manager_updated"] == 1


# --------------------------------------------------------------------------- 10
def test_restart_recovery_orphans_stale_claims_without_double_apply(trade_store, fresh_risk_manager, tmp_path, monkeypatch):
    client = _client()
    db_path = tmp_path / "trade_results_test.sqlite3"

    # ادّعاء قديم "من عملية سابقة" توقف قبل التثبيت (crash بين الادّعاء والتطبيق).
    # نفس نطاق الحساب الذي يستخدمه الـendpoint (المفتاح المركب account+deal).
    stale = TradeResultStore(db_path, orphan_grace_seconds=0.0)
    outcome = stale.claim(
        event_id="stale-event", deal_id="DLG-STALE",
        account_scope=(settings.ctrader_account_id or "").strip(),
        pnl_usd=-99.0, received_at_utc="2020-01-01T00:00:00+00:00",
    )
    assert outcome.claimed is True

    # "إعادة التشغيل": متجر جديد على نفس الملف يُعلن الادّعاء القديم منقطعًا
    restarted = TradeResultStore(db_path, orphan_grace_seconds=0.0)
    monkey_store_rows = restarted.fetch_all()
    assert monkey_store_rows[0]["status"] == "orphaned"
    assert "interrupted_before_finalize_or_apply" in monkey_store_rows[0]["error_detail"]

    # إعادة الطلب بعد الإقلاع = duplicate؛ العداد لا يُحدَّث تلقائيًا (سياسة بلا تغيير)
    monkeypatch.setattr(api_main, "_trade_result_store", restarted)
    retry = client.post("/trade-result", params={"pnl_usd": "-99", "deal_id": "DLG-STALE"})
    assert retry.json()["status"] == "duplicate"
    assert fresh_risk_manager._daily_loss_usd == 0.0  # لا تطبيق تلقائي — موثّق في التقرير


def test_grace_period_protects_live_claims_from_other_workers(tmp_path):
    path = tmp_path / "shared.sqlite3"
    first_worker = TradeResultStore(path, orphan_grace_seconds=600.0)
    first_worker.claim(
        event_id="live-event", deal_id="DLG-LIVE", account_scope="",
        pnl_usd=-10.0, received_at_utc="2099-01-01T00:00:00+00:00",
    )
    second_worker = TradeResultStore(path, orphan_grace_seconds=600.0)  # "إقلاع" آخر

    row = second_worker.fetch_all()[0]
    assert row["status"] == "claimed"  # ادّعاء حيّ (ضمن حدّ السماح) لم يُعلن منقطعًا


# --------------------------------------------------------------------------- 11
def test_legacy_success_contract_shape_is_unchanged(trade_store, fresh_risk_manager):
    """استجابة النجاح القديمة يجب أن تبقى حرفيًا كما هو متوقَّع من cBot."""
    response = _client().post("/trade-result", params={"pnl_usd": -12.5})
    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}
