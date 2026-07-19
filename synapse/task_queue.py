"""Persistent task state machine and Agent call accounting."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from synapse.db import get_db
from synapse.task_status import TASK_STATUSES, TERMINAL_TASK_STATUSES

TASK_PURPOSES = frozenset(("execute", "bid", "plan", "review", "tag"))
TASK_SOURCE_KINDS = frozenset(("agent", "web", "system"))


class TaskAlreadyExistsError(ValueError):
    """Raised when a caller reuses an existing task correlation ID."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


async def _increment_stat(
    db,
    *,
    target_agent: str,
    profile: str,
    purpose: str,
    outcome: str,
    timestamp: str,
    amount: int = 1,
) -> None:
    day = timestamp[:10]
    await db.execute(
        """
        INSERT INTO agent_call_stats (
            day, target_agent, profile, purpose, outcome, count
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(day, target_agent, profile, purpose, outcome)
        DO UPDATE SET count=count+excluded.count
        """,
        (day, target_agent or "unknown", profile or "", purpose or "execute", outcome, amount),
    )


async def create_task(
    db_path: str,
    source_agent: str,
    target_agent: str,
    plan: str,
    *,
    timeout: int = 60,
    persona: str = "",
    correlation_id: str | None = None,
    source_kind: str = "agent",
    purpose: str = "execute",
    title: str = "",
    profile: str = "",
    session_alias: str = "",
    group_id: str = "",
) -> str:
    """Create one task and account for one Agent invocation request."""

    from synapse.session import generate_correlation_id

    if source_kind not in TASK_SOURCE_KINDS:
        raise ValueError(f"Invalid task source_kind: {source_kind}")
    if purpose not in TASK_PURPOSES:
        raise ValueError(f"Invalid task purpose: {purpose}")

    task_id = correlation_id or generate_correlation_id()
    now = _utc_now()
    try:
        async with get_db(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO tasks (
                    id, type, source_agent, target_agent, source_kind, purpose,
                    title, profile, session_alias, group_id, content, status,
                    timeout, persona, created_at
                ) VALUES (?, 'send', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?)
                """,
                (
                    task_id,
                    source_agent,
                    target_agent,
                    source_kind,
                    purpose,
                    title,
                    profile,
                    session_alias,
                    group_id,
                    plan,
                    timeout,
                    persona,
                    now,
                ),
            )
            await _increment_stat(
                db,
                target_agent=target_agent,
                profile=profile,
                purpose=purpose,
                outcome="requested",
                timestamp=now,
            )
            await db.commit()
    except sqlite3.IntegrityError as exc:
        raise TaskAlreadyExistsError(f"Task ID already exists: {task_id}") from exc
    return task_id


async def get_task(db_path: str, task_id: str) -> dict[str, Any] | None:
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_pending_tasks(db_path: str, target_agent: str) -> list[dict[str, Any]]:
    """Return tasks which have not yet been dispatched."""

    async with get_db(db_path) as db:
        cur = await db.execute(
            """
            SELECT * FROM tasks
            WHERE target_agent=? AND status IN ('CREATED', 'QUEUED')
            ORDER BY created_at ASC
            """,
            (target_agent,),
        )
        return [dict(row) for row in await cur.fetchall()]


async def update_task_status(
    db_path: str,
    task_id: str,
    status: str,
    *,
    result: str | None = None,
    expected_statuses: tuple[str, ...] | None = None,
) -> bool:
    """Compare-and-set one task state and record the first terminal outcome."""

    if status not in TASK_STATUSES:
        raise ValueError(f"Invalid task status: {status}")
    if expected_statuses is not None:
        if not expected_statuses:
            return False
        invalid = [item for item in expected_statuses if item not in TASK_STATUSES]
        if invalid:
            raise ValueError(f"Invalid expected task status: {invalid[0]}")

    now = _utc_now()
    async with get_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT status, target_agent, profile, purpose, started_at FROM tasks WHERE id=?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row is None:
            await db.rollback()
            return False
        previous = str(row["status"])
        if expected_statuses is not None and previous not in expected_statuses:
            await db.rollback()
            return False

        update = await db.execute(
            """
            UPDATE tasks
            SET status=?,
                updated_at=?,
                result=CASE WHEN ?=1 THEN ? ELSE result END,
                started_at=CASE WHEN ?=1 AND started_at IS NULL THEN ? ELSE started_at END,
                completed_at=CASE WHEN ?=1 THEN ? ELSE completed_at END
            WHERE id=? AND status=?
            """,
            (
                status,
                now,
                int(result is not None),
                result,
                int(status == "EXECUTING"),
                now,
                int(status in TERMINAL_TASK_STATUSES),
                now,
                task_id,
                previous,
            ),
        )
        if update.rowcount <= 0:
            await db.rollback()
            return False

        if previous not in TERMINAL_TASK_STATUSES and status in TERMINAL_TASK_STATUSES:
            await _increment_stat(
                db,
                target_agent=str(row["target_agent"] or ""),
                profile=str(row["profile"] or ""),
                purpose=str(row["purpose"] or "execute"),
                outcome=status.lower(),
                timestamp=now,
            )
        await db.commit()
        return True


async def cancel_task(db_path: str, task_id: str, reason: str) -> dict[str, Any] | None:
    """Cancel one nonterminal task and preserve a bounded human reason."""

    now = _utc_now()
    async with get_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = await cur.fetchone()
        if row is None:
            await db.rollback()
            return None
        task = dict(row)
        if task["status"] in TERMINAL_TASK_STATUSES:
            await db.rollback()
            return task

        update = await db.execute(
            """
            UPDATE tasks
            SET status='CANCELLED', cancel_reason=?, result=?, updated_at=?, completed_at=?
            WHERE id=? AND status IN ('CREATED','QUEUED','DISPATCHED','EXECUTING')
            """,
            (reason, f"Cancelled by web administrator: {reason}", now, now, task_id),
        )
        if update.rowcount <= 0:
            await db.rollback()
            cur = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
            current = await cur.fetchone()
            return dict(current) if current else None
        await _increment_stat(
            db,
            target_agent=str(task.get("target_agent") or ""),
            profile=str(task.get("profile") or ""),
            purpose=str(task.get("purpose") or "execute"),
            outcome="cancelled",
            timestamp=now,
        )
        await db.commit()
        task.update(
            {
                "status": "CANCELLED",
                "cancel_reason": reason,
                "result": f"Cancelled by web administrator: {reason}",
                "updated_at": now,
                "completed_at": now,
            }
        )
        return task


async def abandon_incomplete_tasks(
    db_path: str,
    reason: str = "Server restarted before the task completed",
) -> int:
    """Abandon every nonterminal task because reconnect delivery is process-local."""

    now = _utc_now()
    async with get_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """
            SELECT id, target_agent, profile, purpose
            FROM tasks
            WHERE status IN ('CREATED','QUEUED','DISPATCHED','EXECUTING')
            """
        )
        rows = [dict(row) for row in await cur.fetchall()]
        await db.executemany(
            """
            UPDATE tasks
            SET status='ABANDONED',
                result=CASE WHEN result IS NULL OR result='' THEN ? ELSE result END,
                updated_at=?, completed_at=?
            WHERE id=?
            """,
            [(reason, now, now, row["id"]) for row in rows],
        )
        for row in rows:
            await _increment_stat(
                db,
                target_agent=str(row.get("target_agent") or ""),
                profile=str(row.get("profile") or ""),
                purpose=str(row.get("purpose") or "execute"),
                outcome="abandoned",
                timestamp=now,
            )
        await db.execute(
            """
            UPDATE task_groups
            SET status='ERROR', updated_at=?
            WHERE status='EXECUTING'
              AND NOT EXISTS (
                  SELECT 1 FROM tasks
                  WHERE tasks.group_id=task_groups.id AND tasks.purpose='execute'
              )
            """,
            (now,),
        )
        await db.execute(
            """
            UPDATE task_groups
            SET status='ERROR', updated_at=?
            WHERE status IN ('BIDDING','PLANNING')
              AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.group_id=task_groups.id)
            """,
            (now,),
        )
        await db.commit()
        return len(rows)


async def cleanup_stale_tasks(db_path: str, ttl_hours: int = 168) -> int:
    """Delete old terminal tasks and time out stale nonterminal tasks."""

    cutoff = (datetime.now(UTC) - timedelta(hours=ttl_hours)).isoformat()
    now = _utc_now()
    async with get_db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        deleted = await db.execute(
            """
            DELETE FROM tasks
            WHERE status IN ('COMPLETED','TIMEOUT','ERROR','CANCELLED','ABANDONED')
              AND COALESCE(updated_at, created_at) < ?
            """,
            (cutoff,),
        )
        cur = await db.execute(
            """
            SELECT id, target_agent, profile, purpose FROM tasks
            WHERE status IN ('DISPATCHED','EXECUTING','CREATED','QUEUED')
              AND COALESCE(updated_at, created_at) < ?
            """,
            (cutoff,),
        )
        stale = [dict(row) for row in await cur.fetchall()]
        if stale:
            await db.executemany(
                """
                UPDATE tasks
                SET status='TIMEOUT',
                    result=CASE WHEN result IS NULL OR result='' THEN ? ELSE result END,
                    updated_at=?, completed_at=?
                WHERE id=?
                """,
                [("Task expired during stale-task cleanup", now, now, row["id"]) for row in stale],
            )
            for row in stale:
                await _increment_stat(
                    db,
                    target_agent=str(row.get("target_agent") or ""),
                    profile=str(row.get("profile") or ""),
                    purpose=str(row.get("purpose") or "execute"),
                    outcome="timeout",
                    timestamp=now,
                )
        groups_deleted = await db.execute(
            """
            DELETE FROM task_groups
            WHERE COALESCE(updated_at, created_at) < ?
              AND NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.group_id=task_groups.id)
            """,
            (cutoff,),
        )
        await db.commit()
        return deleted.rowcount + len(stale) + groups_deleted.rowcount
