"""SQLite 数据库初始化与管理。"""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiosqlite


def escape_like(value: str) -> str:
    """转义 SQLite LIKE 通配符 % 和 _。"""
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
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(candidate_stat.st_mode) and (not hasattr(os, "getuid") or candidate_stat.st_uid == os.getuid()):
            os.chmod(candidate, 0o600)


@asynccontextmanager
async def get_db(db_path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    """获取数据库连接。WAL 模式下每次连接开销可忽略。高并发时考虑连接池。"""
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


async def _ensure_task_status_schema(db: aiosqlite.Connection) -> None:
    """Migrate the task status CHECK constraint in one rollback-safe transaction."""

    cur = await db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    row = await cur.fetchone()
    sql = row["sql"] if row else ""
    if not sql or "ABANDONED" in sql:
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
                content TEXT NOT NULL,
                result TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED'
                    CHECK (status IN (
                        'CREATED','QUEUED','DISPATCHED','EXECUTING',
                        'COMPLETED','TIMEOUT','ERROR','ABANDONED'
                    )),
                timeout INTEGER,
                connection_id TEXT,
                persona TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT
            );
            INSERT INTO tasks_new (
                id, type, source_agent, target_agent, content, result, status,
                timeout, connection_id, persona, created_at, updated_at
            )
            SELECT
                id, type, source_agent, target_agent, content, result, status,
                timeout, connection_id, persona, created_at, updated_at
            FROM tasks;
            DROP TABLE tasks;
            ALTER TABLE tasks_new RENAME TO tasks;
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source_agent);
            CREATE INDEX IF NOT EXISTS idx_tasks_target ON tasks(target_agent);
            COMMIT;
        """)
    except Exception:
        with suppress(Exception):
            await db.execute("ROLLBACK")
        raise
    finally:
        await db.execute("PRAGMA foreign_keys=ON")


async def init_db(db_path: str | Path) -> None:
    try:
        async with get_db(db_path) as db:
            await db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                source_agent TEXT,
                target_agent TEXT,
                content TEXT NOT NULL,
                result TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED'
                    CHECK (status IN (
                        'CREATED','QUEUED','DISPATCHED','EXECUTING',
                        'COMPLETED','TIMEOUT','ERROR','ABANDONED'
                    )),
                timeout INTEGER,
                connection_id TEXT,
                persona TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source_agent);
            CREATE INDEX IF NOT EXISTS idx_tasks_target ON tasks(target_agent);

            CREATE TABLE IF NOT EXISTS pending_deliveries (
                id TEXT PRIMARY KEY,
                target_agent TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                result_content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                ttl_hours INTEGER DEFAULT 24
            );
            CREATE INDEX IF NOT EXISTS idx_pending_target ON pending_deliveries(target_agent);

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
            await _ensure_task_status_schema(db)
            await db.execute("PRAGMA user_version = 4")
            await db.commit()
    except Exception as e:
        raise RuntimeError(f"Failed to initialize database at {db_path}: {e}") from e
