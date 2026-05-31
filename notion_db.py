"""
SQLite-хранилище задач, уведомлений и участников чатов.
scope — идентификатор чата: telegram chat_id (строка) или chat_instance для личных диалогов.
"""
import aiosqlite
import os
from datetime import datetime, date

DB_PATH = os.path.join(os.path.dirname(__file__), "assistant.db")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                text         TEXT    NOT NULL,
                scope        TEXT    NOT NULL,
                chat_title   TEXT    NOT NULL,
                from_user    TEXT    NOT NULL,
                from_user_id INTEGER NOT NULL DEFAULT 0,
                status       TEXT    NOT NULL DEFAULT 'open',
                deadline     TEXT,
                reminder_at  TEXT,
                added_at     TEXT    NOT NULL,
                closed_at    TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_users (
                scope      TEXT    NOT NULL,
                user_id    INTEGER NOT NULL,
                chat_title TEXT    NOT NULL,
                joined_at  TEXT    NOT NULL,
                last_seen  TEXT    NOT NULL,
                PRIMARY KEY (scope, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                scope      TEXT    NOT NULL,
                task_id    INTEGER NOT NULL,
                text       TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                is_read    INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.commit()
        await _ensure_reminder_column(db)
        await _migrate_legacy(db)


async def _ensure_reminder_column(db: aiosqlite.Connection) -> None:
    """Добавляет колонку reminder_at в таблицу tasks, если её нет"""
    cursor = await db.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "reminder_at" not in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN reminder_at TEXT")
        await db.commit()


async def _ensure_task_columns(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "scope" not in cols and "chat_id" in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN scope TEXT")
        await db.execute("UPDATE tasks SET scope = CAST(chat_id AS TEXT) WHERE scope IS NULL")
    if "deadline" not in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN deadline TEXT")
    if "reminder_at" not in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN reminder_at TEXT")
    if "from_user_id" not in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN from_user_id INTEGER NOT NULL DEFAULT 0")
    if "closed_at" not in cols:
        await db.execute("ALTER TABLE tasks ADD COLUMN closed_at TEXT")
    await db.execute("UPDATE tasks SET from_user_id = CAST(scope AS INTEGER) WHERE from_user_id = 0")


async def _migrate_chat_users(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(chat_users)")
    cols = {row[1] for row in await cursor.fetchall()}
    if not cols:
        return
    if "scope" not in cols and "chat_id" in cols:
        now = datetime.utcnow().isoformat(timespec="seconds")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_users_new (
                scope      TEXT    NOT NULL,
                user_id    INTEGER NOT NULL,
                chat_title TEXT    NOT NULL,
                joined_at  TEXT    NOT NULL,
                last_seen  TEXT    NOT NULL,
                PRIMARY KEY (scope, user_id)
            )
        """)
        await db.execute("""
            INSERT OR IGNORE INTO chat_users_new (scope, user_id, chat_title, joined_at, last_seen)
            SELECT CAST(chat_id AS TEXT), user_id, chat_title, joined_at, joined_at
            FROM chat_users
        """)
        await db.execute("DROP TABLE chat_users")
        await db.execute("ALTER TABLE chat_users_new RENAME TO chat_users")


async def _migrate_notifications(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(notifications)")
    cols = {row[1] for row in await cursor.fetchall()}
    if not cols:
        return
    if "scope" not in cols and "chat_id" in cols:
        await db.execute("ALTER TABLE notifications ADD COLUMN scope TEXT")
        await db.execute("UPDATE notifications SET scope = CAST(chat_id AS TEXT) WHERE scope IS NULL")


async def _migrate_legacy(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
    )
    if not await cursor.fetchone():
        return

    await _ensure_task_columns(db)
    await _migrate_chat_users(db)
    await _migrate_notifications(db)

    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='deals'"
    )
    if not await cursor.fetchone():
        await db.commit()
        return

    cursor = await db.execute("SELECT * FROM deals")
    deals = await cursor.fetchall()
    cursor = await db.execute("PRAGMA table_info(deals)")
    deal_cols = [d[1] for d in await cursor.fetchall()]
    for deal in deals:
        deal_d = dict(zip(deal_cols, deal))
        status = "done" if deal_d["status"] == "closed" else "open"
        closed_at = deal_d["added_at"] if status == "done" else None
        scope = str(deal_d["chat_id"])
        await db.execute(
            """INSERT INTO tasks
               (text, scope, chat_title, from_user, from_user_id, status, deadline, added_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                deal_d["text"], scope, deal_d["chat_title"],
                deal_d["who_promised"], deal_d["chat_id"], status,
                deal_d["deadline"], deal_d["added_at"], closed_at,
            ),
        )

    await db.execute("DROP TABLE IF EXISTS deals")
    await db.commit()


# ─────────────────────────── УЧАСТНИКИ ЧАТОВ ──────────────────

async def register_chat_user(scope: str, chat_title: str, user_id: int) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO chat_users (scope, user_id, chat_title, joined_at, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(scope, user_id) DO UPDATE SET
                 chat_title=excluded.chat_title,
                 last_seen=excluded.last_seen""",
            (scope, user_id, chat_title, now, now),
        )
        await db.commit()


async def get_user_scopes(user_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT scope FROM chat_users WHERE user_id=? ORDER BY last_seen DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_scope_user_ids(scope: str) -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM chat_users WHERE scope=?", (scope,),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def get_last_scope(user_id: int) -> str | None:
    scopes = await get_user_scopes(user_id)
    return scopes[0] if scopes else None


# ─────────────────────────── ЗАДАЧИ ─────────────────────────────

async def add_task(
    text: str,
    scope: str,
    chat_title: str,
    from_user: str,
    from_user_id: int,
    deadline: str | None = None,
    reminder_at: str | None = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO tasks
               (text, scope, chat_title, from_user, from_user_id, deadline, reminder_at, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                text, scope, chat_title, from_user, from_user_id, deadline, reminder_at,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_tasks(scope: str, only_open: bool = True) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if only_open:
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE scope=? AND status='open' ORDER BY id DESC",
                (scope,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE scope=? ORDER BY id DESC",
                (scope,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_tasks_for_scopes(scopes: list[str], only_open: bool = True) -> list[dict]:
    if not scopes:
        return []
    placeholders = ",".join("?" * len(scopes))
    status_filter = "AND status='open'" if only_open else ""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT * FROM tasks WHERE scope IN ({placeholders}) {status_filter} ORDER BY id DESC",
            scopes,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_archived_tasks(scope: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE scope=? AND status='done' ORDER BY closed_at DESC, id DESC",
            (scope,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_archived_for_scopes(scopes: list[str]) -> list[dict]:
    if not scopes:
        return []
    placeholders = ",".join("?" * len(scopes))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT * FROM tasks
                WHERE scope IN ({placeholders}) AND status='done'
                ORDER BY closed_at DESC, id DESC""",
            scopes,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_overdue_tasks(scopes: list[str] | None = None) -> list[dict]:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if scopes:
            placeholders = ",".join("?" * len(scopes))
            cursor = await db.execute(
                f"""SELECT * FROM tasks
                    WHERE status='open' AND deadline IS NOT NULL AND deadline < ?
                    AND scope IN ({placeholders})
                    ORDER BY deadline ASC""",
                [today, *scopes],
            )
        else:
            cursor = await db.execute(
                """SELECT * FROM tasks
                   WHERE status='open' AND deadline IS NOT NULL AND deadline < ?
                   ORDER BY deadline ASC""",
                (today,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_tasks_with_reminders() -> list[dict]:
    """Получает все открытые задачи, у которых есть reminder_at и время ещё не прошло"""
    now = datetime.utcnow().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM tasks
               WHERE status='open' 
               AND reminder_at IS NOT NULL 
               AND reminder_at <= ?
               ORDER BY reminder_at ASC""",
            (now,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def close_task(task_id: int) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET status='done', closed_at=? WHERE id=?",
            (now, task_id),
        )
        await db.commit()


async def get_task(task_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


# ─────────────────────────── УВЕДОМЛЕНИЯ ────────────────────────

async def add_notification(user_id: int, scope: str, task_id: int, text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO notifications (user_id, scope, task_id, text, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, scope, task_id, text,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        await db.commit()


async def get_user_notifications(user_id: int, only_unread: bool = False) -> list[dict]:
    scopes = await get_user_scopes(user_id)
    if not scopes:
        return []
    placeholders = ",".join("?" * len(scopes))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = f"""
            SELECT n.*, t.text AS task_text, t.deadline
            FROM notifications n
            LEFT JOIN tasks t ON t.id = n.task_id
            WHERE n.user_id=? AND n.scope IN ({placeholders})
        """
        params: list = [user_id, *scopes]
        if only_unread:
            query += " AND n.is_read=0"
        query += " ORDER BY n.created_at DESC LIMIT 50"
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def mark_notifications_read(user_id: int, scope: str | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if scope is not None:
            await db.execute(
                "UPDATE notifications SET is_read=1 WHERE user_id=? AND scope=?",
                (user_id, scope),
            )
        else:
            await db.execute(
                "UPDATE notifications SET is_read=1 WHERE user_id=?",
                (user_id,),
            )
        await db.commit()