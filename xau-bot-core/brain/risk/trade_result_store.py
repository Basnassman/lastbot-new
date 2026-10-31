"""
سجل تدقيق دائم لنتائج الصفقات القادمة إلى Brain عبر POST /trade-result، مع منع
التكرار على مستوى قاعدة البيانات (ليس ذاكرة فقط).

التصميم (الخطوتان 1–2 فقط — لا يمس سياسة المخاطر إطلاقًا):
- التخزين: ملف SQLite واحد (stdlib sqlite3 — بلا أي اعتمادية جديدة) بوضع WAL
  وsynchronous=FULL لضمان قبول دائم قبل أي معالجة لاحقة.
- منع التكرار: فهرس UNIQUE على (account_scope, deal_id) مع ادّعاء ذرّي
  (BEGIN IMMEDIATE + INSERT) — طلبان متزامنان لنفس المعرّف لا ينجح منهما إلا واحد.
- الحالات: claimed (حجز دائم، التطبيق لم يبدأ)، applying (على وشك استدعاء مدير المخاطر)،
  applied (استدعاء المدير عاد دون استثناء)، error (استثناء أثناء التطبيق والنتيجة ملتبسة)،
  orphaned (تصنيف إداري فقط)، rejected. تبقى الحالات غير المحسومة كما هي بعد إعادة التشغيل؛
  لا يُعاد تطبيق PnL منها تلقائيًا لأن السجل لا يثبت حالة عداد الذاكرة.
- حالة SQLite وعدّاد RiskManager في الذاكرة غير معاملتين ذريتين؛ لا يُدعى exactly-once
  عبر الأعطال/إعادة التشغيل. نفضّل حجب إعادة التطبيق التلقائي على خطر الاحتساب المزدوج.

هذه الوحدة لا تستورد أي من إعدادات المخاطر ولا تغيّرها؛ مسار الملف يأتي من
config.settings.trade_result_db_path (إعداد جديد اختياري بقيمة افتراضية آمنة).
"""
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# حالات السجل — قيم مستندة إلى متطلبات التدقيق (processed/duplicate/rejected/error)
STATUS_CLAIMED = "claimed"
STATUS_APPLIED = "applied"
STATUS_ERROR = "error"
STATUS_ORPHANED = "orphaned"
STATUS_REJECTED = "rejected"

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

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

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
          أي duplicate لا يعيد التطبيق؛ الحالات غير applied تبقى unresolved.
        - بلا deal_id (وضع متوافق عكسي): كل طلب يُسجَّل ويعالَج (بدون dedup)
          وdedup_applied=0 ليكون غياب الحماية موثّقًا لا مخفيًا.
        - تكرار أي سجل قائم، بما فيه error/claimed/applying/orphaned، لا يعيد التطبيق؛
          لا يثبت استثناء التطبيق أن العداد لم يتغير.
        - deal_id مفقود يبقى متوافقًا لكنه لا يملك ضمان dedup.
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
                if not deal_id:
                    raise
                row = connection.execute(
                    "SELECT event_id, status FROM trade_results"
                    " WHERE account_scope = ? AND deal_id = ? AND status <> ?",
                    (account_scope, deal_id, STATUS_REJECTED),
                ).fetchone()
                connection.execute(
                    """
                    UPDATE trade_results
                       SET duplicate_attempts = duplicate_attempts + 1,
                           last_duplicate_at_utc = ?
                     WHERE account_scope = ? AND deal_id = ? AND status <> ?
                    """,
                    (received_at_utc, account_scope, deal_id, STATUS_REJECTED),
                )
                original_event, status = row if row else ("", "unknown")
                connection.commit()
                return ClaimOutcome(False, True, status, original_event, "duplicate_of_" + status)
        finally:
            connection.close()

    def mark_applied(self, event_id: str, applied_at_utc: str) -> None:
        """تثبيت نجاح عودة register_trade_result؛ لا يجعل العداد وSQLite معاملة واحدة."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE trade_results
                   SET status = ?, risk_manager_updated = 1,
                       error_detail = COALESCE(error_detail || ' | ', '')
                                     || ('applied_at=' || ?)
                 WHERE event_id = ? AND status = 'applying'
                """,
                (STATUS_APPLIED, applied_at_utc, event_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("trade result was not in applying state during finalize")
            connection.commit()

    def mark_applying(self, event_id: str) -> None:
        """يسجل قبل استدعاء المدير أن التطبيق على وشك البدء؛ لا يضمن اكتماله."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE trade_results SET status = 'applying' WHERE event_id = ? AND status = ?",
                (event_id, STATUS_CLAIMED),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("trade result was not claimed before risk update")
            connection.commit()

    def mark_error(self, event_id: str, detail: str, *, risk_manager_returned: bool = False) -> None:
        """يسجل حالة غير محسومة؛ العلامة توضح فقط عودة استدعاء المدير دون استثناء."""
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE trade_results SET status = ?,"
                " risk_manager_updated = CASE WHEN ? THEN 1 ELSE risk_manager_updated END,"
                " error_detail = ? WHERE event_id = ? AND status = 'applying'",
                (STATUS_ERROR, int(risk_manager_returned), detail[:500], event_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("trade result was not applying while recording application error")
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
                 received_at_utc, STATUS_REJECTED,
                 f"{detail} | raw_pnl_length={len(raw_pnl)}"),
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

    def fetch_unresolved(self) -> list[dict]:
        """قراءة السجلات غير المحسومة فقط؛ لا تُعد تطبيق PnL ولا تغيّر الحالة."""
        return [
            row for row in self.fetch_all()
            if            row["status"] in (STATUS_CLAIMED, "applying", STATUS_ERROR, STATUS_ORPHANED)
        ]

    @staticmethod
    def read_unresolved(db_path: str | Path) -> list[dict]:
        """استعلام SQLite للقراءة فقط؛ لا ينشئ الملف/المخطط ولا يغيّر حالة stale claims."""
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT event_id, deal_id, account_scope, pnl_usd, received_at_utc, status,"
                " dedup_applied, risk_manager_updated, error_detail,"
                " duplicate_attempts, last_duplicate_at_utc FROM trade_results"
                " WHERE status IN (?, ?, ?, ?) ORDER BY received_at_utc, event_id",
                (STATUS_CLAIMED, "applying", STATUS_ERROR, STATUS_ORPHANED),
            ).fetchall()
        return [dict(row) for row in rows]
