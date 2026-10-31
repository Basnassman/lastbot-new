"""اختبارات الخطوتين 1–2: تدقيق نتائج الصفقات + منع التكرار.

تغطية المتطلبات:
 1) pnl سالب صالح يُسجَّل ويُعالَج مرة واحدة.
 2) pnl موجب يزيد صافي نتيجة اليوم (PnL موقّع) ولا يُتجاهل.
 3) تكرار نفس deal_id لا يطبّق pnl مرتين.
 4) معرّفان مختلفان بنفس قيمة pnl يُعالَجان باستقلالية.
 5) طلبان متزامنان لنفس deal_id → تحديث واحد فقط للعدّاد.
 6) المعرّف المفقود/الملوّث يتبع السياسة الموثّقة (توافق عكسي / رفض).
 7) قيم pnl غير الصالحة (NaN/Infinity/نص) تُرفض.
 8) فشل التخزين لا ينتج استجابة نجاح زائفة.
 9) أخطاء التطبيق تبقى unresolved ولا يعاد تطبيقها تلقائيًا.
10) سلوك التعافي بعد "إعادة التشغيل" بقدر ما يدعمه التنفيذ.
11) سلوك الـendpoint القديم والاختبارات غير المرتبطة تبقى كما هي (يُشغَّل السوت كله).

لا شبكة حقيقية ولا أوامر تداول: TestClient + متجر SQLite بمجلد مؤقت فقط.
"""
import sqlite3
import threading
from dataclasses import replace
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
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-12.5)  # خسارة موقّعة

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
def test_positive_pnl_increases_daily_net_pnl(trade_store, fresh_risk_manager):
    response = _client().post("/trade-result", params={"pnl_usd": "100", "deal_id": "DLG-WIN"})

    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}
    # صافي PnL موقّع: الربح يزيد الصافي (كان يُتجاهل قبل الإصلاح)
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(100.0)

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
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-30.0)  # احتساب واحد فقط

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
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-60.0)
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
    unresolved = [b for b in bodies if b.get("error") == "trade_result_unresolved"]
    assert len(recorded) == 1  # ادّعاء واحد فقط ينجح
    assert len(duplicates) + len(unresolved) == 7
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-20.0)  # تحديث واحد فقط

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
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-20.0)

    rows = trade_store.fetch_all()
    assert len(rows) == 2
    assert all(row["dedup_applied"] == 0 for row in rows)


def test_whitespace_deal_id_is_rejected(trade_store, fresh_risk_manager):
    response = _client().post("/trade-result", params={"pnl_usd": "-10", "deal_id": "   "})

    assert response.status_code == 400
    assert response.json() == {"status": "rejected", "error": "empty_deal_id"}
    assert fresh_risk_manager._daily_net_pnl_usd == 0.0
    row = trade_store.fetch_all()[0]
    assert row["status"] == "rejected"


def test_missing_pnl_param_is_framework_rejected(trade_store, fresh_risk_manager):
    response = _client().post("/trade-result", params={"deal_id": "DLG-X"})
    assert response.status_code == 422  # رفض آمن من تحقق FastAPI قبل أي معالجة
    assert fresh_risk_manager._daily_net_pnl_usd == 0.0
    assert trade_store.fetch_all() == []  # لا سجل ولا عداد — موثّق كحدّ في التقرير


# --------------------------------------------------------------------------- 7
@pytest.mark.parametrize("bad_pnl", ["NaN", "Infinity", "-Infinity", "abc", ""])
def test_invalid_pnl_values_are_rejected(bad_pnl, trade_store, fresh_risk_manager):
    response = _client().post("/trade-result", params={"pnl_usd": bad_pnl, "deal_id": f"DLG-{bad_pnl or 'EMPTY'}"})

    assert response.status_code == 400
    assert response.json() == {"status": "rejected", "error": "invalid_pnl_usd"}
    assert fresh_risk_manager._daily_net_pnl_usd == 0.0
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
    assert fresh_risk_manager._daily_net_pnl_usd == 0.0  # لا تحديث قبل قبول دائم


def test_timeout_retry_after_applied_response_was_lost_is_duplicate(trade_store, fresh_risk_manager):
    client = _client()
    first = client.post("/trade-result", params={"pnl_usd": "-13", "deal_id": "DLG-TIMEOUT"})
    assert first.json() == {"status": "recorded"}  # افترض أن العميل انتهت مهلته ولم يستلمها
    retry = client.post("/trade-result", params={"pnl_usd": "-13", "deal_id": "DLG-TIMEOUT"})
    assert retry.json()["status"] == "duplicate"
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-13.0)


def test_claimed_timeout_before_application_stays_unresolved(trade_store, fresh_risk_manager, monkeypatch):
    def _stop_after_claim(_self, _event_id):
        raise TimeoutError("claim acknowledged, apply stage not reached")

    monkeypatch.setattr(TradeResultStore, "mark_applying", _stop_after_claim)
    response = _client().post("/trade-result", params={"pnl_usd": "-17", "deal_id": "DLG-PRE-APPLY"})
    assert response.status_code == 503
    assert response.json()["error"] == "trade_result_unresolved"
    assert fresh_risk_manager._daily_net_pnl_usd == 0.0
    row = trade_store.fetch_all()[0]
    assert row["status"] == "claimed"


def test_applying_state_write_failure_prevents_risk_update(trade_store, fresh_risk_manager, monkeypatch):
    def _fail_mark_applying(_self, _event_id):
        raise sqlite3.OperationalError("disk I/O error before risk update")

    monkeypatch.setattr(TradeResultStore, "mark_applying", _fail_mark_applying)
    response = _client().post("/trade-result", params={"pnl_usd": "-17", "deal_id": "DLG-PRE-APPLY"})
    assert response.status_code == 503
    assert response.json()["error"] == "trade_result_unresolved"
    assert fresh_risk_manager._daily_net_pnl_usd == 0.0
    row = trade_store.fetch_all()[0]
    assert row["status"] == "claimed"


def test_finalize_and_error_audit_failure_do_not_hide_counter_change(trade_store, fresh_risk_manager, monkeypatch):
    def _fail_audit(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error after risk update")

    monkeypatch.setattr(TradeResultStore, "mark_applied", _fail_audit)
    monkeypatch.setattr(TradeResultStore, "mark_error", _fail_audit)
    response = _client().post("/trade-result", params={"pnl_usd": "-19", "deal_id": "DLG-AUDIT-AFTER"})
    assert response.status_code == 503
    assert response.json()["error"] == "trade_result_unresolved"
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-19.0)
    row = trade_store.fetch_all()[0]
    assert row["status"] == "applying"


def test_finalize_failure_after_apply_is_marked_unresolved(trade_store, fresh_risk_manager, monkeypatch):
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

    # العداد حُدِّث لكن تثبيت applied فشل → يحاول تسجيل error غير محسوم.
    assert first.status_code == 503
    assert first.json() == {"status": "error", "error": "trade_result_unresolved", "deal_id": "DLG-FIN"}
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-15.0)
    assert trade_store.fetch_all()[0]["status"] == "error"

    # إعادة الإرسال لا تُعلن نجاحًا ولا تعيد التطبيق ما دام السجل غير محسوم.
    retry = client.post("/trade-result", params={"pnl_usd": "-15", "deal_id": "DLG-FIN"})
    assert retry.status_code == 503
    assert retry.json()["error"] == "trade_result_unresolved"
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-15.0)
    assert trade_store.fetch_all()[0]["risk_manager_updated"] == 1


# --------------------------------------------------------------------------- 9
class _FlakyRiskManager(RiskManager):
    """مدير مخاطر يفشل تسجيل النتيجة في أول محاولة فقط (محاكاة خطأ معالجة)."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0
        self._fail_once = True

    def register_trade_result(self, pnl_usd: float) -> None:
        self.attempts += 1
        super().register_trade_result(pnl_usd)
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("simulated failure after counter mutation")


def test_interrupted_processing_is_unresolved_and_never_reapplied_automatically(trade_store, monkeypatch):
    flaky = _FlakyRiskManager()
    monkeypatch.setattr(api_main, "_risk_manager", flaky)
    client = _client()

    first = client.post("/trade-result", params={"pnl_usd": "-40", "deal_id": "DLG-RETRY"})
    assert first.status_code == 500
    assert first.json() == {"status": "error", "error": "risk_update_unresolved", "deal_id": "DLG-RETRY"}
    assert flaky._daily_net_pnl_usd == pytest.approx(-40.0)  # الاستثناء وقع بعد التعديل
    row = trade_store.fetch_all()[0]
    assert row["status"] == "error"
    assert "RuntimeError" in row["error_detail"]
    assert "simulated failure" not in row["error_detail"]  # لا نخزن رسالة الاستثناء

    # حتى الاستثناء قد يقع بعد تعديل العداد؛ لذلك لا يعاد التطبيق تلقائيًا.
    retry = client.post("/trade-result", params={"pnl_usd": "-40", "deal_id": "DLG-RETRY"})
    assert retry.status_code == 503
    assert retry.json()["error"] == "trade_result_unresolved"
    assert flaky._daily_net_pnl_usd == pytest.approx(-40.0)
    row = trade_store.fetch_all()[0]
    assert row["status"] == "error"
    assert row["risk_manager_updated"] == 0  # عاد exception بعد تغيير عداد الذاكرة؛ النتيجة غير محسومة


# --------------------------------------------------------------------------- 10
def test_restart_keeps_old_applying_record_unresolved_without_double_apply(trade_store, fresh_risk_manager, tmp_path, monkeypatch):
    client = _client()
    db_path = tmp_path / "trade_results_test.sqlite3"

    # ادّعاء تاريخي من عملية سابقة في مرحلة applying؛ لا يثبت موضع الانقطاع.
    # نفس نطاق الحساب الذي يستخدمه الـendpoint (المفتاح المركب account+deal).
    stale = TradeResultStore(db_path)
    outcome = stale.claim(
        event_id="stale-event", deal_id="DLG-STALE",
        account_scope=(settings.ctrader_account_id or "").strip(),
        pnl_usd=-99.0, received_at_utc="2020-01-01T00:00:00+00:00",
    )
    assert outcome.claimed is True
    stale.mark_applying(outcome.event_id)

    # إعادة التشغيل لا تحوّل الحالة تلقائيًا أو تفترض هل غُيّر العداد.
    restarted = TradeResultStore(db_path)
    monkey_store_rows = restarted.fetch_all()
    assert monkey_store_rows[0]["status"] == "applying"

    # إعادة الطلب بعد الإقلاع لا تطبّق PnL ولا تعلن النجاح.
    monkeypatch.setattr(api_main, "_trade_result_store", restarted)
    retry = client.post("/trade-result", params={"pnl_usd": "-99", "deal_id": "DLG-STALE"})
    assert retry.status_code == 503
    assert retry.json()["error"] == "trade_result_unresolved"
    assert fresh_risk_manager._daily_net_pnl_usd == 0.0  # لا تطبيق تلقائي — موثّق في التقرير


def test_same_deal_id_is_scoped_by_account(trade_store, fresh_risk_manager, monkeypatch):
    client = _client()
    monkeypatch.setattr(api_main, "settings", replace(settings, ctrader_environment="demo", ctrader_account_id="101"))
    first = client.post("/trade-result", params={"pnl_usd": "-7", "deal_id": "same"})
    monkeypatch.setattr(api_main, "settings", replace(settings, ctrader_environment="demo", ctrader_account_id="202"))
    second = client.post("/trade-result", params={"pnl_usd": "-7", "deal_id": "same"})
    assert first.json() == {"status": "recorded"}
    assert second.json() == {"status": "recorded"}
    assert fresh_risk_manager._daily_net_pnl_usd == pytest.approx(-14.0)
    assert len(trade_store.fetch_all()) == 2


def test_missing_live_account_scope_is_rejected_before_claim(trade_store, fresh_risk_manager, monkeypatch):
    monkeypatch.setattr(api_main, "settings", replace(settings, ctrader_environment="live", ctrader_account_id=""))
    response = _client().post("/trade-result", params={"pnl_usd": "-7", "deal_id": "live-deal"})
    assert response.status_code == 503
    assert response.json()["error"] == "invalid_account_scope"
    assert fresh_risk_manager._daily_net_pnl_usd == 0.0
    assert trade_store.fetch_all() == []


def test_restart_does_not_mutate_any_claim_for_worker_age(tmp_path):
    path = tmp_path / "shared.sqlite3"
    first_worker = TradeResultStore(path)
    first_worker.claim(
        event_id="live-event", deal_id="DLG-LIVE", account_scope="",
        pnl_usd=-10.0, received_at_utc="2099-01-01T00:00:00+00:00",
    )
    second_worker = TradeResultStore(path)  # "إقلاع" آخر

    row = second_worker.fetch_all()[0]
    assert row["status"] == "claimed"  # لا ترقية تلقائية عند أي إعادة تشغيل


# --------------------------------------------------------------------------- 11
def test_unresolved_report_is_read_only_and_includes_reconciliation_fields(trade_store, tmp_path):
    trade_store.claim(event_id="report-event", deal_id="report-deal", account_scope="acct",
                      pnl_usd=-8.0, received_at_utc="2026-10-27T00:00:00+00:00")
    trade_store.mark_applying("report-event")
    before = trade_store.fetch_all()
    unresolved = TradeResultStore.read_unresolved(tmp_path / "trade_results_test.sqlite3")
    after = trade_store.fetch_all()
    assert len(unresolved) == 1
    assert unresolved[0]["deal_id"] == "report-deal"
    assert unresolved[0]["pnl_usd"] == pytest.approx(-8.0)
    assert unresolved[0]["received_at_utc"] == "2026-10-27T00:00:00+00:00"
    assert unresolved[0]["status"] == "applying"
    assert before == after


def test_legacy_success_contract_shape_is_unchanged(trade_store, fresh_risk_manager):
    """استجابة النجاح القديمة يجب أن تبقى حرفيًا كما هو متوقَّع من cBot."""
    response = _client().post("/trade-result", params={"pnl_usd": -12.5})
    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}
