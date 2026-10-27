"""
سجل تدقيق دائم لنتائج الصفقات القادمة إلى Brain عبر POST /trade-result، مع منع
التكرار على مستوى قاعدة البيانات (ليس ذاكرة فقط).

التصميم (الخطوتان 1–2 فقط — لا يمس سياسة المخاطر إطلاقًا):
- التخزين: ملف SQLite واحد (stdlib sqlite3 — بلا أي اعتمادية جديدة) بوضع WAL
  وsynchronous=FULL لضمان قبول دائم قبل أي معالجة لاحقة.
- منع التكرار: فهرس UNIQUE على (account_scope, deal_id) مع ادّعاء ذرّي
  (BEGIN IMMEDIATE + INSERT) — طلبان متزامنان لنفس المعرّف لا ينجح منهما إلا واحد.
- دورة الحياة للسجل: claimed → applied | error | orphaned | rejected.
  * claimed: ادُّعِي المفتاح ولم يُحدَّث عداد المخاطر بعد (أو فشل الت_finalize).
  * applied: العداد حُدِّث فعليًا (risk_manager_updated=1).
  * error: تحديث العداد فشل (يعاد المحاولة عند تكرار الطلب — آمن لأن الفشل سابق
    على أي تعديل؛ عداد المخاطر يُحدَّث بعملية ذرّية واحدة لا يقع فيها استثناء
    بعد التعديل).
  * orphaned: سجل claimed قديم أُعلن منقطعًا عند إقلاع العملية (تجاوز حدّ سماح)
    — لا يُعاد تطبيقه تلقائيًا (انظر التوثيق أدناه).
- الحد الفاقد للتعافي (crash window) موثّق في end module docstring: الحالة المخزَّنة
  والعداد في الذاكرة غير معاملتان معاملة transactional واحدة، لذا لا يُدعى
  exactly-once عبر أعطال العملية؛ الفشل الآمن مُتحاز نحو عدم الاحتساب المزدوج.

هذه الوحدة لا تستورد أي من إعدادات المخاطر ولا تغيّرها؛ مسار الملف يأتي من
config.settings.trade_result_db_path (إعداد جديد اختياري بقيمة افتراضية آمنة).
"""
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# حالات السجل — قيم مستندة إلى متطلبات التدقيق (processed/duplicate/rejected/error)
STATUS_CLAIMED = "claimed"
STATUS_APPLIED = "applied"
STATUS_ERROR = "error"
STATUS_ORPHANED = "orphaned"
STATUS_REJECTED = "rejected"

# حدّ السماح: أي claimed أقدم من هذا العمر عند إقلاع العملية يُعلن منقطعًا.
# يحمي ادّعاءات العمليات الشقيقة الحيّة (workers متعددة على نفس الملف).
_DEFAULT_ORPHAN_GRACE_SECONDS = 600.0


@dataclass(frozen=True)
class ClaimOutcome:
    """نتيجة محاولة ادّعاء نتيجة صفقة واحدة — يحددها المتجر قبل أي تحديث للعداد."""

    claimed: bool          # True => يجب تطبيق pnl على RiskManager ثم mark_applied
    duplicate: bool        # True => مكرر (لا تطبيق ثانٍ إطلاقًا)
    status: str            # الحالة المخزَّنة الحالية للسجل (للتدقيق/الاستجابة)
    event_id: str
    detail: str = ""


class TradeResultStore:
    """متجر تدقيق + منع تكرار لنتائج الصفقات (SQLite، متعدد الخيوط/العمليات آمن)."""

    def __init__(self, db_path: str | Path, orphan_grace_seconds: float = _DEFAULT_ORPHAN_GRACE_SECONDS) -> None:
        self._db_path = str(db_path)
        self._orphan_grace = float(orphan_grace_seconds)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._mark_stale_claims_orphaned()

    # ------------------------------------------------------------------ internal

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30.0)
        # WAL للتشغيل المتزامن؛ FULL لقبول دائم (fsync) قبل إرجاع النجاح
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trade_results (
                    event_id TEXT PRIMARY KEY,
                    deal_id TEXT NOT NULL DEFAULT '',
                    account_scope TEXT NOT NULL,
                    pnl_usd REAL NOT NULL,
                    received_at_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    dedup_applied INTEGER NOT NULL DEFAULT 0,
                    risk_manager_updated INTEGER NOT NULL DEFAULT 0,
                    error_detail TEXT,
                    duplicate_attempts INTEGER NOT NULL DEFAULT 0,
                    last_duplicate_at_utc TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_trade_results_account_deal
                    ON trade_results(account_scope, deal_id) WHERE deal_id <> '';
                """
            )

    def _mark_stale_claims_orphaned(self) -> None:
        """_recovery عند الإقلاع: ادّعاءات claimed متجاوزة حدّ السماح تُعلن منقطعًا.

        لا تُعاد تطبيقها تلقائيًا — إعادة التطبيق قد تغيّر سلوك حد الخسارة اليومي
        خارج نطاق هذه المهمة؛ التوثيق يوجب الإبلاغ بدل التخمين. (الافتراض: أي
        claimed أحدث من العمر المسموح يعود إلى عملية أخرى شغّالة على نفس الملف.)
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self._orphan_grace)).isoformat()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE trade_results
                   SET status = ?,
                       error_detail = COALESCE(error_detail || ' | ', '')
                                     || 'interrupted_before_finalize_or_apply'
                 WHERE status = ? AND received_at_utc < ?
                """,
                (STATUS_ORPHANED, STATUS_CLAIMED, cutoff),
            )
            connection.commit()

    # ------------------------------------------------------------------ public

    def claim(
        self,
        *,
        event_id: str,
        deal_id: str | None,
        account_scope: str,
        pnl_usd: float,
        received_at_utc: str,
    ) -> ClaimOutcome:
        """ادّعاء ذرّي للنتيجة قبل تطبيقها على RiskManager.

        - مع deal_id: فهرس UNIQUE يضمن أن طلبًا واحدًا فقط ينجح بالادّعاء؛
          التكرار (حتى المتزامن) يُعاد كـduplicate دون تطبيق ثانٍ.
        - بلا deal_id (وضع متوافق عكسي): كل طلب يُسجَّل ويعالَج (بدون dedup)
          وdedup_applied=0 ليكون غياب الحماية موثّقًا لا مخفيًا.
        - تكرار سجل status=error يُسمح بإعادة الادّعاء: الفشل سابق على تعديل العداد.
        - تكرار سجل applied | claimed | orphaned = duplicate دائم (لا احتساب ثانٍ).
        """
        connection = self._connect()
        try:
            # معاملة ذرّية واحدة تحمل قفل الكتابة من بدايتها حتى نهايتها:
            # أي مطالب متزامن لنفس المفتاح ينتظر هنا ثم يقع على مسار التكرار.
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO trade_results
                        (event_id, deal_id, account_scope, pnl_usd, received_at_utc,
                         status, dedup_applied)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, deal_id or "", account_scope, pnl_usd, received_at_utc,
                     STATUS_CLAIMED, 1 if deal_id else 0),
                )
                connection.commit()
                return ClaimOutcome(True, False, STATUS_CLAIMED, event_id)
            except sqlite3.IntegrityError:
                # المفتاح موجود مسبقًا — سجل محاولة التكرار ثم قرّر الحالة
                row = connection.execute(
                    "SELECT event_id, status FROM trade_results WHERE account_scope = ? AND deal_id = ?",
                    (account_scope, deal_id),
                ).fetchone()
                connection.execute(
                    """
                    UPDATE trade_results
                       SET duplicate_attempts = duplicate_attempts + 1,
                           last_duplicate_at_utc = ?
                     WHERE account_scope = ? AND deal_id = ?
                    """,
                    (received_at_utc, account_scope, deal_id),
                )
                original_event, status = row if row else ("", "unknown")
                if status == STATUS_ERROR:
                    # إعادة الادّعاء لسجل الفاشل — آمنة لأن الفشل سابق على تحديث العداد.
                    # شرط status='error' يضمن أن مطالبًا واحدًا فقط ينجح (.rowcount).
                    cursor = connection.execute(
                        """
                        UPDATE trade_results
                           SET status = ?, risk_manager_updated = 0,
                               error_detail = COALESCE(error_detail || ' | ', '') || 'retry_claimed'
                         WHERE account_scope = ? AND deal_id = ? AND status = ?
                        """,
                        (STATUS_CLAIMED, account_scope, deal_id, STATUS_ERROR),
                    )
                    connection.commit()
                    if cursor.rowcount == 1:
                        return ClaimOutcome(True, False, STATUS_CLAIMED, original_event, "retry_after_error")
                    return ClaimOutcome(False, True, STATUS_CLAIMED, original_event, "duplicate_of_claimed")
                connection.commit()
                return ClaimOutcome(False, True, status, original_event, "duplicate_of_" + status)
        finally:
            connection.close()

    def mark_applied(self, event_id: str, applied_at_utc: str) -> None:
        """تثبيت نجاح تحديث العداد — يُستدعى بعد register_trade_result مباشرة."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE trade_results
                   SET status = ?, risk_manager_updated = 1,
                       error_detail = COALESCE(error_detail || ' | ', '')
                                     || ('applied_at=' || ?)
                 WHERE event_id = ?
                """,
                (STATUS_APPLIED, applied_at_utc, event_id),
            )
            connection.commit()

    def mark_error(self, event_id: str, detail: str) -> None:
        """تثبيت فشل تحديث العداد — يسمح بإعادة المحاولة الآمنة عند تكرار الطلب."""
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE trade_results SET status = ?, error_detail = ? WHERE event_id = ?",
                (STATUS_ERROR, detail[:500], event_id),
            )
            connection.commit()

    def record_rejected(
        self,
        *,
        event_id: str,
        deal_id: str | None,
        account_scope: str,
        raw_pnl: str,
        received_at_utc: str,
        detail: str,
    ) -> None:
        """تسجيل طلب مرفوض (pnl غير صالح أو معرّف فارغ) — لا يمس العداد إطلاقًا."""
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO trade_results
                    (event_id, deal_id, account_scope, pnl_usd, received_at_utc,
                     status, dedup_applied, error_detail)
                VALUES (?, ?, ?, 0.0, ?, ?, 0, ?)
                """,
                (event_id, (deal_id or "<invalid>").strip()[:200], account_scope,
                 received_at_utc, STATUS_REJECTED, f"{detail} | raw_pnl={raw_pnl[:100]!r}"),
            )
            connection.commit()
        finally:
            connection.close()

    # واجهة قراءة للتدقيق/الاختبارات (لا شيء يمسها في الإنتاج)
    def fetch_all(self) -> list[dict]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT event_id, deal_id, account_scope, pnl_usd, received_at_utc, status,"
                " dedup_applied, risk_manager_updated, error_detail, duplicate_attempts,"
                " last_duplicate_at_utc FROM trade_results ORDER BY received_at_utc, event_id"
            ).fetchall()
        columns = [
            "event_id", "deal_id", "account_scope", "pnl_usd", "received_at_utc", "status",
            "dedup_applied", "risk_manager_updated", "error_detail", "duplicate_attempts",
            "last_duplicate_at_utc",
        ]
        return [dict(zip(columns, row)) for row in rows]
