"""Skills CRUD — 技能的定义与检索。"""

from __future__ import annotations

from datetime import UTC, datetime

from synapse.db import get_db


async def add_skill(db_path: str, name: str, content: str) -> int:
    """添加技能，返回自增 id。"""
    now = datetime.now(UTC).isoformat()
    async with get_db(db_path) as db:
        cur = await db.execute(
            "INSERT INTO skills (name, content, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET content=excluded.content",
            (name, content, now),
        )
        await db.commit()
        cur = await db.execute("SELECT id FROM skills WHERE name=?", (name,))
        row = await cur.fetchone()
        return int(row["id"]) if row else 0


async def get_skill(db_path: str, name: str) -> dict | None:
    """按名获取技能，返回 {name, content} 或 None。"""
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT name, content FROM skills WHERE name=?", (name,))
        row = await cur.fetchone()
        return {"name": row["name"], "content": row["content"]} if row else None


async def list_skills(db_path: str) -> list[str]:
    """列出所有技能名。"""
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT name FROM skills ORDER BY name")
        return [r["name"] for r in await cur.fetchall()]


async def list_skills_detailed(db_path: str) -> list[dict]:
    """列出所有技能名 + 内容，一次查询。"""
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT name, content FROM skills ORDER BY name")
        return [{"name": r["name"], "content": r["content"]} for r in await cur.fetchall()]


async def delete_skill(db_path: str, name: str) -> bool:
    """删除技能，返回是否成功删除。"""
    async with get_db(db_path) as db:
        cur = await db.execute("DELETE FROM skills WHERE name=?", (name,))
        await db.commit()
        return cur.rowcount > 0
