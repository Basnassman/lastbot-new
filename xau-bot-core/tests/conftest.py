"""Fixtures مشتركة للاختبارات — عزل سجل تدقيق نتائج الصفقات عن ملفات الإنتاج."""
import pytest


@pytest.fixture(autouse=True)
def trade_store(tmp_path, monkeypatch):
    """يمنع أي اختبار من الكتابة في logs/trade_results.sqlite3 الخاص بالإنتاج.

    يُنشئ متجرًا معزولًا بمجلد مؤقت لكل اختبار (orphan_grace=0 لتسهيل اختبارات
    التعافي)، ويركّبه في نقطة التركيب الوحيدة المستخدمة بواسطة الـendpoint.
    """
    from brain.api import main as api_main
    from brain.risk.trade_result_store import TradeResultStore

    store = TradeResultStore(tmp_path / "trade_results_test.sqlite3", orphan_grace_seconds=0.0)
    monkeypatch.setattr(api_main, "_trade_result_store", store)
    return store


@pytest.fixture()
def fresh_risk_manager(monkeypatch):
    """مدير مخاطر نظيف لكل اختبار (المثيل المشترك في main لا يلوّث الاختبارات)."""
    from brain.api import main as api_main
    from brain.risk.risk_manager import RiskManager

    fresh = RiskManager()
    monkeypatch.setattr(api_main, "_risk_manager", fresh)
    return fresh
