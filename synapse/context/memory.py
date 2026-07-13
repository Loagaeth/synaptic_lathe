"""Memory CRUD — 短期记忆的增删查。"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

from synapse.config import MemoryConfig
from synapse.context.embedding import EmbeddingStatus, get_embedding
from synapse.db import escape_like, get_db
from synapse.logging import synapse_logger


async def add_memory(
    db_path: str, content: str, persona: str = "", *, memory_config: MemoryConfig | None = None
) -> int:
    """添加记忆，返回 id。有 embedding 配置时同时存储向量。"""
    now = datetime.now(UTC).isoformat()
    emb_blob = None
    if memory_config:
        vec, _status = await get_embedding(content, memory_config)
        if vec:
            emb_blob = struct.pack(f"<{len(vec)}f", *vec)
    async with get_db(db_path) as db:
        cur = await db.execute(
            "INSERT INTO memories (persona, content, embedding, created_at) VALUES (?, ?, ?, ?)",
            (persona, content, emb_blob, now),
        )
        await db.commit()
        return cur.lastrowid or 0


async def get_recent_memories(db_path: str, persona: str = "", limit: int = 20) -> list[dict]:
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT id, content, created_at FROM memories WHERE persona=? ORDER BY created_at DESC LIMIT ?",
            (persona, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def delete_memory(db_path: str, memory_id: int, persona: str = "") -> bool:
    async with get_db(db_path) as db:
        if persona:
            cur = await db.execute(
                "DELETE FROM memories WHERE id=? AND persona=?",
                (memory_id, persona),
            )
        else:
            cur = await db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_memories(db_path: str, persona: str = "") -> list[dict]:
    async with get_db(db_path) as db:
        if persona:
            cur = await db.execute(
                "SELECT id, content, embedding, created_at FROM memories WHERE persona=? ORDER BY created_at DESC",
                (persona,),
            )
        else:
            cur = await db.execute(
                "SELECT id, content, embedding, created_at FROM memories ORDER BY created_at DESC",
            )
        return [dict(r) for r in await cur.fetchall()]


async def search_memory(
    db_path: str,
    query: str,
    persona: str = "",
    limit: int = 5,
    memory_config: MemoryConfig | None = None,
) -> list[dict]:
    """语义搜索记忆。先用预存向量，无 embedding 时 fallback 到 LIKE。"""
    if memory_config is not None:
        async with get_db(db_path) as db:
            cur = await db.execute(
                "SELECT id, content, embedding FROM memories "
                "WHERE persona=? AND embedding IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 500",
                (persona,),
            )
            rows = await cur.fetchall()

        if rows:
            query_vec, emb_status = await get_embedding(query, memory_config)
            if emb_status == EmbeddingStatus.OK and query_vec:
                from synapse.context.embedding import _cosine

                scored = []
                for row in rows:
                    emb_data = row["embedding"]
                    if emb_data:
                        try:
                            stored = list(struct.unpack(f"<{len(emb_data) // 4}f", emb_data))
                            if len(stored) != len(query_vec):
                                synapse_logger.debug(
                                    "Skipping memory embedding with mismatched dimensions: id=%s", row["id"]
                                )
                                continue
                            scored.append((row["id"], row["content"], _cosine(query_vec, stored)))
                        except struct.error as exc:
                            synapse_logger.debug("Skipping invalid memory embedding for id=%s: %s", row["id"], exc)
                scored.sort(key=lambda item: item[2], reverse=True)
                if scored:
                    return [
                        {"id": memory_id, "content": content, "score": round(score, 4)}
                        for memory_id, content, score in scored[:limit]
                    ]
            else:
                synapse_logger.info(
                    "Memory semantic search degraded: %s, falling back to LIKE",
                    emb_status.value,
                )

    # LIKE fallback
    async with get_db(db_path) as db:
        escaped = escape_like(query)
        cur = await db.execute(
            "SELECT id, content FROM memories WHERE persona=? AND content LIKE ? "
            "ESCAPE '\\' ORDER BY created_at DESC LIMIT ?",
            (persona, f"%{escaped}%", limit),
        )
        return [dict(r) for r in await cur.fetchall()]
