"""会话 ID 生成 — UUID7 毫秒级排序 correlation_id + UUID4 session_id。"""

from __future__ import annotations

import os
import time
import uuid as _uuid


def generate_correlation_id() -> str:
    """生成 UUID7 correlation_id，按毫秒时间戳排序，适合数据库索引。"""
    timestamp_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = timestamp_ms.to_bytes(6, "big")
    b[6] = 0x70 | (rand[0] & 0x0F)
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)
    b[9:16] = rand[3:10]
    return str(_uuid.UUID(bytes=bytes(b)))


def generate_session_id() -> str:
    """生成 UUID4 session_id，用于标识一个连接会话。"""
    return str(_uuid.uuid4())
