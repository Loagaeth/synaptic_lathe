"""Prompt CRUD — 可复用提示词文档的存取与列表。"""

from __future__ import annotations

from datetime import UTC, datetime

from synapse.db import get_db


async def set_prompt(db_path: str, name: str, content: str) -> int:
    """添加或更新提示词，返回记录 id。"""
    now = datetime.now(UTC).isoformat()
    async with get_db(db_path) as db:
        await db.execute(
            """
            INSERT INTO prompts (name, content, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                content=excluded.content,
                updated_at=excluded.updated_at
            """,
            (name, content, now, now),
        )
        await db.commit()
        cur = await db.execute("SELECT id FROM prompts WHERE name=?", (name,))
        row = await cur.fetchone()
        return int(row["id"]) if row else 0


async def get_prompt(db_path: str, name: str) -> dict | None:
    """按名获取提示词，返回 {name, content, created_at, updated_at} 或 None。"""
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT name, content, created_at, updated_at FROM prompts WHERE name=?",
            (name,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "content": row["content"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


async def list_prompts(db_path: str) -> list[str]:
    """列出所有提示词名称。"""
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT name FROM prompts ORDER BY name")
        return [row["name"] for row in await cur.fetchall()]


async def list_prompts_detailed(db_path: str) -> list[dict]:
    """列出所有提示词名称和内容。"""
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT name, content, created_at, updated_at FROM prompts ORDER BY name",
        )
        return [
            {
                "name": row["name"],
                "content": row["content"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in await cur.fetchall()
        ]


async def delete_prompt(db_path: str, name: str) -> bool:
    """删除提示词，返回是否成功删除。"""
    async with get_db(db_path) as db:
        cur = await db.execute("DELETE FROM prompts WHERE name=?", (name,))
        await db.commit()
        return cur.rowcount > 0
