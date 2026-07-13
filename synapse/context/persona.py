"""Persona CRUD — 人设的存取与列表。"""

from __future__ import annotations

from datetime import UTC, datetime

from synapse.db import get_db


async def set_persona(db_path: str, name: str, content: str) -> int:
    """创建或更新人设（upsert），返回 id。"""
    now = datetime.now(UTC).isoformat()
    async with get_db(db_path) as db:
        cur = await db.execute(
            "INSERT INTO personas (name, content, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET content=excluded.content",
            (name, content, now),
        )
        await db.commit()
        cur = await db.execute("SELECT id FROM personas WHERE name=?", (name,))
        row = await cur.fetchone()
        return int(row["id"]) if row else 0


async def get_persona(db_path: str, name: str) -> dict | None:
    """按名获取人设，返回 {name, content} 或 None。"""
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT name, content FROM personas WHERE name=?", (name,))
        row = await cur.fetchone()
        return {"name": row["name"], "content": row["content"]} if row else None


async def list_personas(db_path: str) -> list[str]:
    """列出所有人设名。"""
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT name FROM personas ORDER BY name")
        return [r["name"] for r in await cur.fetchall()]


async def list_personas_detailed(db_path: str) -> list[dict]:
    """列出所有人设名 + 内容，一次查询。"""
    async with get_db(db_path) as db:
        cur = await db.execute("SELECT name, content FROM personas ORDER BY name")
        return [{"name": r["name"], "content": r["content"]} for r in await cur.fetchall()]


async def delete_persona(db_path: str, name: str) -> bool:
    """按名删除人设。"""
    async with get_db(db_path) as db:
        cur = await db.execute("DELETE FROM personas WHERE name=?", (name,))
        await db.commit()
        return cur.rowcount > 0
