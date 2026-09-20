"""Queries and durable coordination records for Web-managed Agent tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from synapse.db import get_db
from synapse.session import generate_correlation_id

_GROUP_MODES = frozenset(("auction", "team"))
_GROUP_STATUSES = frozenset(
    (
        "BIDDING",
        "PLANNING",
        "AWAITING_SELECTION",
        "AWAITING_APPROVAL",
        "EXECUTING",
        "COMPLETED",
        "ERROR",
        "CANCELLED",
    )
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


async def list_tasks(
    db_path: str,
    *,
    status: str = "",
    target_agent: str = "",
    profile: str = "",
    purpose: str = "",
    group_id: str = "",
    managed_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    values = (
        status,
        status,
        target_agent,
        target_agent,
        profile,
        profile,
        purpose,
        purpose,
        group_id,
        group_id,
        int(managed_only),
        max(1, min(limit, 500)),
    )
    async with get_db(db_path) as db:
        cur = await db.execute(
            """
            SELECT * FROM tasks
            WHERE (?='' OR status=?)
              AND (?='' OR target_agent=?)
              AND (?='' OR profile=?)
              AND (?='' OR purpose=?)
              AND (?='' OR group_id=?)
              AND (?=0 OR source_kind IN ('web', 'api'))
            ORDER BY created_at DESC
            LIMIT ?
            """,
            values,
        )
        return [dict(row) for row in await cur.fetchall()]


async def invocation_stats(db_path: str, *, days: int = 30) -> list[dict[str, Any]]:
    cutoff = (datetime.now(UTC) - timedelta(days=max(1, min(days, 365)) - 1)).date().isoformat()
    async with get_db(db_path) as db:
        cur = await db.execute(
            """
            SELECT target_agent, profile, purpose, outcome, SUM(count) AS count
            FROM agent_call_stats
            WHERE day>=?
            GROUP BY target_agent, profile, purpose, outcome
            ORDER BY target_agent, profile, purpose, outcome
            """,
            (cutoff,),
        )
        return [dict(row) for row in await cur.fetchall()]


async def create_task_group(db_path: str, *, mode: str, title: str, requirement: str) -> str:
    if mode not in _GROUP_MODES:
        raise ValueError(f"Invalid task group mode: {mode}")
    group_id = generate_correlation_id()
    now = _utc_now()
    initial = "BIDDING" if mode == "auction" else "PLANNING"
    async with get_db(db_path) as db:
        await db.execute(
            """
            INSERT INTO task_groups (id, mode, title, requirement, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (group_id, mode, title, requirement, initial, now, now),
        )
        await db.commit()
    return group_id


async def update_task_group(
    db_path: str,
    group_id: str,
    status: str,
    *,
    selected_task_id: str | None = None,
) -> bool:
    if status not in _GROUP_STATUSES:
        raise ValueError(f"Invalid task group status: {status}")
    now = _utc_now()
    async with get_db(db_path) as db:
        if selected_task_id is None:
            cur = await db.execute(
                "UPDATE task_groups SET status=?, updated_at=? WHERE id=?",
                (status, now, group_id),
            )
        else:
            cur = await db.execute(
                "UPDATE task_groups SET status=?, updated_at=?, selected_task_id=? WHERE id=?",
                (status, now, selected_task_id, group_id),
            )
        await db.commit()
        return cur.rowcount > 0


async def claim_task_group(
    db_path: str,
    group_id: str,
    *,
    expected_status: str,
    new_status: str,
    selected_task_id: str | None = None,
) -> bool:
    """Atomically claim a human-gated group transition."""

    if expected_status not in _GROUP_STATUSES or new_status not in _GROUP_STATUSES:
        raise ValueError("Invalid task group status transition")
    now = _utc_now()
    async with get_db(db_path) as db:
        if selected_task_id is None:
            cur = await db.execute(
                "UPDATE task_groups SET status=?, updated_at=? WHERE id=? AND status=?",
                (new_status, now, group_id, expected_status),
            )
        else:
            cur = await db.execute(
                """
                UPDATE task_groups
                SET status=?, updated_at=?, selected_task_id=?
                WHERE id=? AND status=?
                """,
                (new_status, now, selected_task_id, group_id, expected_status),
            )
        await db.commit()
        return cur.rowcount > 0


async def _persist_derived_group_status(
    db_path: str,
    group_id: str,
    *,
    observed_status: str,
    derived_status: str,
) -> str:
    """Persist a derived state without overwriting a concurrent human transition."""

    if derived_status == observed_status:
        return observed_status
    if await claim_task_group(
        db_path,
        group_id,
        expected_status=observed_status,
        new_status=derived_status,
    ):
        return derived_status

    async with get_db(db_path) as db:
        cur = await db.execute("SELECT status FROM task_groups WHERE id=?", (group_id,))
        row = await cur.fetchone()
    return str(row["status"]) if row else observed_status


def _derive_group_status(group: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    if group["status"] in {"COMPLETED", "ERROR", "CANCELLED"}:
        return str(group["status"])
    mode = group["mode"]
    if mode == "auction":
        bids = [task for task in tasks if task.get("purpose") == "bid"]
        executions = [task for task in tasks if task.get("purpose") == "execute"]
        if group["status"] == "EXECUTING" and not executions:
            return "EXECUTING"
        if executions:
            statuses = {task["status"] for task in executions}
            if statuses <= {"COMPLETED"}:
                return "COMPLETED"
            if statuses <= {"COMPLETED", "ERROR", "TIMEOUT", "CANCELLED", "ABANDONED"}:
                return "ERROR"
            return "EXECUTING"
        if bids and all(task["status"] in {"COMPLETED", "ERROR", "TIMEOUT", "CANCELLED", "ABANDONED"} for task in bids):
            return "AWAITING_SELECTION" if any(task["status"] == "COMPLETED" for task in bids) else "ERROR"
        return "BIDDING"
    plans = [task for task in tasks if task.get("purpose") == "plan"]
    executions = [task for task in tasks if task.get("purpose") == "execute"]
    if group["status"] == "EXECUTING" and not executions:
        return "EXECUTING"
    if executions:
        statuses = {task["status"] for task in executions}
        if statuses <= {"COMPLETED"}:
            return "COMPLETED"
        if statuses <= {"COMPLETED", "ERROR", "TIMEOUT", "CANCELLED", "ABANDONED"}:
            return "ERROR"
        return "EXECUTING"
    if plans and all(task["status"] in {"COMPLETED", "ERROR", "TIMEOUT", "CANCELLED", "ABANDONED"} for task in plans):
        return "AWAITING_APPROVAL" if any(task["status"] == "COMPLETED" for task in plans) else "ERROR"
    return "PLANNING"


async def get_task_group(db_path: str, group_id: str) -> dict[str, Any] | None:
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT * FROM task_groups WHERE id=?", (group_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        group = dict(row)
        cur = await db.execute(
            "SELECT * FROM tasks WHERE group_id=? ORDER BY created_at ASC",
            (group_id,),
        )
        tasks = [dict(task) for task in await cur.fetchall()]
    observed_status = str(group["status"])
    derived = _derive_group_status(group, tasks)
    group["status"] = await _persist_derived_group_status(
        db_path,
        group_id,
        observed_status=observed_status,
        derived_status=derived,
    )
    group["tasks"] = tasks
    return group


async def list_task_groups(db_path: str, *, limit: int = 50) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 200))
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT * FROM task_groups ORDER BY created_at DESC LIMIT ?",
            (bounded_limit,),
        )
        groups = [dict(row) for row in await cur.fetchall()]
        cur = await db.execute(
            """
            SELECT tasks.*
            FROM tasks
            JOIN (
                SELECT id FROM task_groups ORDER BY created_at DESC LIMIT ?
            ) AS recent_groups ON recent_groups.id=tasks.group_id
            ORDER BY tasks.created_at ASC
            """,
            (bounded_limit,),
        )
        tasks_by_group: dict[str, list[dict[str, Any]]] = {}
        for row in await cur.fetchall():
            task = dict(row)
            tasks_by_group.setdefault(str(task.get("group_id") or ""), []).append(task)

        for group in groups:
            group["tasks"] = tasks_by_group.get(group["id"], [])

    for group in groups:
        observed_status = str(group["status"])
        derived = _derive_group_status(group, group["tasks"])
        group["status"] = await _persist_derived_group_status(
            db_path,
            str(group["id"]),
            observed_status=observed_status,
            derived_status=derived,
        )
    return groups
