"""任务队列 — 状态机 + SQLite 持久化。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from synapse.db import get_db
from synapse.task_status import TASK_STATUSES


class TaskAlreadyExistsError(ValueError):
    """Raised when a caller reuses an existing task correlation ID."""


async def create_task(
    db_path: str,
    source_agent: str,
    target_agent: str,
    plan: str,
    *,
    timeout: int = 60,
    persona: str = "",
    correlation_id: str | None = None,
) -> str:
    """创建任务，返回 task_id。"""
    from synapse.session import generate_correlation_id

    task_id = correlation_id or generate_correlation_id()
    now = datetime.now(UTC).isoformat()
    try:
        async with get_db(db_path) as db:
            await db.execute(
                "INSERT INTO tasks (id, type, source_agent, target_agent, content, "
                "status, timeout, persona, created_at) "
                "VALUES (?, 'send', ?, ?, ?, 'CREATED', ?, ?, ?)",
                (task_id, source_agent, target_agent, plan, timeout, persona, now),
            )
            await db.commit()
    except sqlite3.IntegrityError as exc:
        raise TaskAlreadyExistsError(f"Task ID already exists: {task_id}") from exc
    return task_id


async def get_task(db_path: str, task_id: str) -> dict | None:
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_pending_tasks(db_path: str, target_agent: str) -> list[dict]:
    """获取待分发的任务（已创建但未派发）。"""
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT * FROM tasks WHERE target_agent=? AND status IN ('CREATED', 'QUEUED') ORDER BY created_at ASC",
            (target_agent,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def update_task_status(
    db_path: str,
    task_id: str,
    status: str,
    *,
    result: str | None = None,
    expected_statuses: tuple[str, ...] | None = None,
) -> bool:
    """Update a task, optionally only when its current state is expected.

    Conditional updates are the compare-and-set primitive used by timeout,
    disconnect, and completion paths so only one terminal outcome can win.
    """

    if status not in TASK_STATUSES:
        raise ValueError(f"Invalid task status: {status}")
    if expected_statuses is not None:
        if not expected_statuses:
            return False
        invalid = [item for item in expected_statuses if item not in TASK_STATUSES]
        if invalid:
            raise ValueError(f"Invalid expected task status: {invalid[0]}")

    updated_at = datetime.now(UTC).isoformat()
    async with get_db(db_path) as db:
        if expected_statuses is None:
            if result is not None:
                cur = await db.execute(
                    "UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=?",
                    (status, result, updated_at, task_id),
                )
            else:
                cur = await db.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                    (status, updated_at, task_id),
                )
        else:
            expected = set(expected_statuses)
            state_flags = tuple(int(item in expected) for item in TASK_STATUSES)
            if result is not None:
                cur = await db.execute(
                    """
                    UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=?
                    AND (
                        (? AND status='CREATED') OR (? AND status='QUEUED') OR
                        (? AND status='DISPATCHED') OR (? AND status='EXECUTING') OR
                        (? AND status='COMPLETED') OR (? AND status='TIMEOUT') OR
                        (? AND status='ERROR') OR (? AND status='ABANDONED')
                    )
                    """,
                    (status, result, updated_at, task_id, *state_flags),
                )
            else:
                cur = await db.execute(
                    """
                    UPDATE tasks SET status=?, updated_at=? WHERE id=?
                    AND (
                        (? AND status='CREATED') OR (? AND status='QUEUED') OR
                        (? AND status='DISPATCHED') OR (? AND status='EXECUTING') OR
                        (? AND status='COMPLETED') OR (? AND status='TIMEOUT') OR
                        (? AND status='ERROR') OR (? AND status='ABANDONED')
                    )
                    """,
                    (status, updated_at, task_id, *state_flags),
                )
        await db.commit()
        return cur.rowcount > 0


async def abandon_incomplete_tasks(
    db_path: str,
    reason: str = "Server restarted before the task completed",
) -> int:
    """Mark tasks from a previous process as terminal before accepting traffic."""

    now = datetime.now(UTC).isoformat()
    async with get_db(db_path) as db:
        cur = await db.execute(
            "UPDATE tasks SET status='ABANDONED', "
            "result=CASE WHEN result IS NULL OR result='' THEN ? ELSE result END, "
            "updated_at=? "
            "WHERE status IN ('CREATED','QUEUED','DISPATCHED','EXECUTING')",
            (reason, now),
        )
        await db.commit()
        return cur.rowcount


async def cleanup_stale_tasks(db_path: str, ttl_hours: int = 24) -> int:
    """清理超时/异常/残留非终态任务。"""
    cutoff = (datetime.now(UTC) - timedelta(hours=ttl_hours)).isoformat()
    async with get_db(db_path) as db:
        cur = await db.execute(
            "DELETE FROM tasks WHERE status IN ('COMPLETED','TIMEOUT','ERROR','ABANDONED') "
            "AND COALESCE(updated_at, created_at) < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        cur = await db.execute(
            "UPDATE tasks SET status='TIMEOUT', "
            "result=CASE WHEN result IS NULL OR result='' THEN ? ELSE result END, updated_at=? "
            "WHERE status IN ('DISPATCHED','EXECUTING','CREATED','QUEUED') "
            "AND COALESCE(updated_at, created_at) < ?",
            ("Task expired during stale-task cleanup", datetime.now(UTC).isoformat(), cutoff),
        )
        await db.commit()
        return deleted + cur.rowcount
