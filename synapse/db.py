"""SQLite database initialization and additive schema migrations."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite

_TASK_ADDITIONAL_COLUMNS = {
    "source_agent": "TEXT",
    "target_agent": "TEXT",
    "result": "TEXT",
    "timeout": "INTEGER",
    "connection_id": "TEXT",
    "persona": "TEXT",
    "updated_at": "TEXT",
    "source_kind": "TEXT NOT NULL DEFAULT 'agent'",
    "purpose": "TEXT NOT NULL DEFAULT 'execute'",
    "title": "TEXT NOT NULL DEFAULT ''",
    "profile": "TEXT NOT NULL DEFAULT ''",
    "session_alias": "TEXT NOT NULL DEFAULT ''",
    "group_id": "TEXT NOT NULL DEFAULT ''",
    "cancel_reason": "TEXT NOT NULL DEFAULT ''",
    "started_at": "TEXT",
    "completed_at": "TEXT",
}


def escape_like(value: str) -> str:
    """Escape SQLite LIKE wildcards."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_db_path(db_path: str | Path) -> Path:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise PermissionError(f"Database path must be a regular non-symlink file: {path}")
        if not os.access(path, os.W_OK):
            raise PermissionError(f"Database file is not writable: {path}")
    else:
        if not os.access(path.parent, os.W_OK):
            raise PermissionError(f"Database directory is not writable: {path.parent}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            path_stat = path.lstat()
            if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
                raise PermissionError(f"Database path must be a regular non-symlink file: {path}") from None
        else:
            os.close(fd)
    return path


def _secure_database_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(candidate, flags)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                continue
            raise
        try:
            candidate_stat = os.fstat(fd)
            owned = not hasattr(os, "getuid") or candidate_stat.st_uid == os.getuid()
            if not stat.S_ISREG(candidate_stat.st_mode) or not owned:
                continue
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(fd, 0o600)
                continue

            # Windows fallback: re-check identity immediately before path chmod.
            try:
                path_stat = candidate.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISLNK(path_stat.st_mode)
                and stat.S_ISREG(path_stat.st_mode)
                and (path_stat.st_dev, path_stat.st_ino) == (candidate_stat.st_dev, candidate_stat.st_ino)
            ):
                with suppress(FileNotFoundError):
                    os.chmod(candidate, 0o600)
        finally:
            os.close(fd)


@asynccontextmanager
async def get_db(db_path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    """Open one WAL-mode SQLite connection with private database files."""

    path = get_db_path(db_path)
    db = await aiosqlite.connect(str(path), timeout=10)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=10000")
        _secure_database_files(path)
        yield db
    finally:
        await db.close()
        _secure_database_files(path)


async def _ensure_task_columns(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(tasks)")
    existing = {str(row["name"]) for row in await cur.fetchall()}
    for name, definition in _TASK_ADDITIONAL_COLUMNS.items():
        if name not in existing:
            await db.execute(f'ALTER TABLE tasks ADD COLUMN "{name}" {definition}')


async def _ensure_task_status_schema(db: aiosqlite.Connection) -> None:
    """Ensure all terminal task statuses exist without losing task history."""

    cur = await db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    row = await cur.fetchone()
    sql = row["sql"] if row else ""
    if not sql or ("CANCELLED" in sql and "ABANDONED" in sql):
        return

    await db.execute("PRAGMA foreign_keys=OFF")
    try:
        await db.executescript("""
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS tasks_new;
            CREATE TABLE tasks_new (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                source_agent TEXT,
                target_agent TEXT,
                source_kind TEXT NOT NULL DEFAULT 'agent',
                purpose TEXT NOT NULL DEFAULT 'execute',
                title TEXT NOT NULL DEFAULT '',
                profile TEXT NOT NULL DEFAULT '',
                session_alias TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                result TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED'
                    CHECK (status IN (
                        'CREATED','QUEUED','DISPATCHED','EXECUTING',
                        'COMPLETED','TIMEOUT','ERROR','CANCELLED','ABANDONED'
                    )),
                timeout INTEGER,
                connection_id TEXT,
                persona TEXT,
                cancel_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                started_at TEXT,
                completed_at TEXT
            );
            INSERT INTO tasks_new (
                id, type, source_agent, target_agent, source_kind, purpose, title,
                profile, session_alias, group_id, content, result, status, timeout,
                connection_id, persona, cancel_reason, created_at, updated_at,
                started_at, completed_at
            )
            SELECT
                id, type, source_agent, target_agent, source_kind, purpose, title,
                profile, session_alias, group_id, content, result, status, timeout,
                connection_id, persona, cancel_reason, created_at, updated_at,
                started_at, completed_at
            FROM tasks;
            DROP TABLE tasks;
            ALTER TABLE tasks_new RENAME TO tasks;
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source_agent);
            CREATE INDEX IF NOT EXISTS idx_tasks_target ON tasks(target_agent);
            CREATE INDEX IF NOT EXISTS idx_tasks_group ON tasks(group_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
            COMMIT;
        """)
    except Exception:
        with suppress(Exception):
            await db.execute("ROLLBACK")
        raise
    finally:
        await db.execute("PRAGMA foreign_keys=ON")


async def _ensure_task_indexes(db: aiosqlite.Connection) -> None:
    """Create task indexes after additive legacy-schema migrations."""

    await db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source_agent);
        CREATE INDEX IF NOT EXISTS idx_tasks_target ON tasks(target_agent);
        CREATE INDEX IF NOT EXISTS idx_tasks_group ON tasks(group_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
    """)


async def init_db(db_path: str | Path) -> None:
    try:
        async with get_db(db_path) as db:
            await db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                source_agent TEXT,
                target_agent TEXT,
                source_kind TEXT NOT NULL DEFAULT 'agent',
                purpose TEXT NOT NULL DEFAULT 'execute',
                title TEXT NOT NULL DEFAULT '',
                profile TEXT NOT NULL DEFAULT '',
                session_alias TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                result TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED'
                    CHECK (status IN (
                        'CREATED','QUEUED','DISPATCHED','EXECUTING',
                        'COMPLETED','TIMEOUT','ERROR','CANCELLED','ABANDONED'
                    )),
                timeout INTEGER,
                connection_id TEXT,
                persona TEXT,
                cancel_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                started_at TEXT,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS task_groups (
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                title TEXT NOT NULL,
                requirement TEXT NOT NULL,
                status TEXT NOT NULL,
                selected_task_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_task_groups_created ON task_groups(created_at DESC);

            CREATE TABLE IF NOT EXISTS agent_call_stats (
                day TEXT NOT NULL,
                target_agent TEXT NOT NULL,
                profile TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL,
                outcome TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
                PRIMARY KEY (day, target_agent, profile, purpose, outcome)
            );

            CREATE TABLE IF NOT EXISTS agent_profile_tags (
                agent_name TEXT NOT NULL,
                profile TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                strengths_json TEXT NOT NULL DEFAULT '[]',
                limitations_json TEXT NOT NULL DEFAULT '[]',
                suitable_tasks_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'configured',
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                PRIMARY KEY (agent_name, profile)
            );

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT DEFAULT '',
                content TEXT NOT NULL,
                embedding BLOB,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_persona ON memories(persona);

            CREATE TABLE IF NOT EXISTS skills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT DEFAULT '',
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_persona ON knowledge(persona);

            CREATE TABLE IF NOT EXISTS personas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT
            );
        """)
            await _ensure_task_columns(db)
            await _ensure_task_status_schema(db)
            await _ensure_task_indexes(db)
            await db.execute("PRAGMA user_version = 7")
            await db.commit()
    except Exception as exc:
        raise RuntimeError(f"Failed to initialize database at {db_path}: {exc}") from exc
