"""Knowledge CRUD — 长期知识的存储与检索。"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

from synapse.config import MemoryConfig
from synapse.context.chunker import MarkdownChunker
from synapse.context.embedding import EmbeddingStatus, get_embedding, get_embeddings
from synapse.db import escape_like, get_db
from synapse.logging import synapse_logger


async def add_knowledge(
    db_path: str,
    title: str,
    content: str,
    persona: str = "",
    *,
    chunk: bool = False,
    memory_config: MemoryConfig | None = None,
) -> list[int]:
    """Add knowledge rows without holding SQLite open across embedding calls."""

    now = datetime.now(UTC).isoformat()
    chunks = MarkdownChunker().chunk(content) if chunk else [content]
    if not chunks:
        return []

    vectors: list[list[float]] | None = None
    if memory_config:
        vectors, _status = await get_embeddings(chunks, memory_config)

    rows: list[tuple[str, str, bytes | None]] = []
    for index, chunk_content in enumerate(chunks):
        chunk_title = f"{title} [part {index + 1}]" if len(chunks) > 1 else title
        embedding = None
        if vectors and index < len(vectors):
            vector = vectors[index]
            embedding = struct.pack(f"<{len(vector)}f", *vector)
        rows.append((chunk_title, chunk_content, embedding))

    ids: list[int] = []
    async with get_db(db_path) as db:
        for chunk_title, chunk_content, embedding in rows:
            cur = await db.execute(
                "INSERT INTO knowledge (persona, title, content, embedding, created_at) VALUES (?, ?, ?, ?, ?)",
                (persona, chunk_title, chunk_content, embedding, now),
            )
            ids.append(cur.lastrowid or 0)
        await db.commit()
    return ids


async def list_knowledge(db_path: str, persona: str = "", limit: int = 50) -> list[dict]:
    async with get_db(db_path) as db:
        cur = await db.execute(
            "SELECT id, title, content, created_at FROM knowledge WHERE persona=? ORDER BY created_at DESC LIMIT ?",
            (persona, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


async def delete_knowledge(db_path: str, knowledge_id: int, persona: str = "") -> bool:
    async with get_db(db_path) as db:
        if persona:
            cur = await db.execute(
                "DELETE FROM knowledge WHERE id=? AND persona=?",
                (knowledge_id, persona),
            )
        else:
            cur = await db.execute("DELETE FROM knowledge WHERE id=?", (knowledge_id,))
        await db.commit()
        return cur.rowcount > 0


async def search_knowledge(
    db_path: str,
    query: str,
    persona: str = "",
    limit: int = 5,
    memory_config: MemoryConfig | None = None,
) -> list[dict]:
    """语义搜索知识。先用预存向量，无 embedding 时 fallback 到 LIKE。"""
    if memory_config is not None:
        async with get_db(db_path) as db:
            cur = await db.execute(
                "SELECT id, title, content, embedding FROM knowledge "
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
                                    "Skipping knowledge embedding with mismatched dimensions: id=%s", row["id"]
                                )
                                continue
                            scored.append((row["id"], row["title"], row["content"], _cosine(query_vec, stored)))
                        except struct.error as exc:
                            synapse_logger.debug("Skipping invalid knowledge embedding for id=%s: %s", row["id"], exc)
                scored.sort(key=lambda item: item[3], reverse=True)
                if scored:
                    return [
                        {"id": item_id, "title": title, "content": content, "score": round(score, 4)}
                        for item_id, title, content, score in scored[:limit]
                    ]
            else:
                synapse_logger.info(
                    "Knowledge semantic search degraded: %s, falling back to LIKE",
                    emb_status.value,
                )

    # LIKE fallback
    async with get_db(db_path) as db:
        escaped = escape_like(query)
        cur = await db.execute(
            "SELECT id, title, content FROM knowledge WHERE persona=? AND content LIKE ? "
            "ESCAPE '\\' ORDER BY created_at DESC LIMIT ?",
            (persona, f"%{escaped}%", limit),
        )
        return [dict(r) for r in await cur.fetchall()]
