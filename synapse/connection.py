from __future__ import annotations

import json
import re
import time
from typing import Any

from fastapi import WebSocket

from synapse.logging import synapse_logger
from synapse.protocol import WS_PROTOCOL_VERSION

_MAX_CLIENT_TEXT = 128
_MAX_CLIENT_LIST_ITEMS = 64
_MAX_PROFILE_CAPABILITIES = 64
_MAX_PENDING_PER_AGENT = 256
_MAX_PENDING_TOTAL = 4096
_MAX_PENDING_MESSAGE_BYTES = 16 * 1_048_576
_MAX_PENDING_TOTAL_BYTES = 16 * 1_048_576
_METADATA_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")
_ALLOWED_PROFILE_HINTS = {"avoid_short_timeout", "may_initialize_mcp", "raw_session_id_allowed"}
_ALLOWED_PLAN_DELIVERY = {"argv_tail", "placeholder"}


def _safe_text(value: Any, *, limit: int = _MAX_CLIENT_TEXT) -> str:
    return " ".join(str(value).split())[:limit]


def _safe_identifier(value: Any, *, limit: int = _MAX_CLIENT_TEXT) -> str:
    text = _safe_text(value, limit=limit)
    return text if _METADATA_IDENTIFIER_RE.fullmatch(text) else ""


def _safe_bool(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def _safe_positive_int(value: Any, *, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= maximum else None


def _safe_identifier_list(value: Any, *, limit: int = _MAX_CLIENT_LIST_ITEMS) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:limit]:
        text = _safe_identifier(item)
        if text and text not in result:
            result.append(text)
    return result


def _sanitize_profile_capability(name: str, raw: Any) -> dict[str, Any]:
    meta = raw if isinstance(raw, dict) else {}
    result: dict[str, Any] = {"name": name}
    for key in ("timeout", "suggested_timeout"):
        value = _safe_positive_int(meta.get(key), maximum=3600)
        if value is not None:
            result[key] = value
    max_output_bytes = _safe_positive_int(meta.get("max_output_bytes"), maximum=16 * 1024 * 1024)
    if max_output_bytes is not None:
        result["max_output_bytes"] = max_output_bytes
    for key in ("supports_session", "session_required", "allow_raw_session_id", "advisory_safe"):
        result[key] = _safe_bool(meta.get(key))
    result["tags"] = _safe_identifier_list(meta.get("tags"), limit=8)
    default_alias = _safe_identifier(meta.get("default_session_alias") or "")
    if default_alias:
        result["default_session_alias"] = default_alias
    plan_delivery = _safe_identifier(meta.get("plan_delivery") or "")
    if plan_delivery in _ALLOWED_PLAN_DELIVERY:
        result["plan_delivery"] = plan_delivery
    result["session_aliases"] = _safe_identifier_list(meta.get("session_aliases"))
    result["hints"] = [hint for hint in _safe_identifier_list(meta.get("hints")) if hint in _ALLOWED_PROFILE_HINTS]
    return result


def _sanitize_profile_capabilities(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for raw_name, raw_meta in list(value.items())[:_MAX_PROFILE_CAPABILITIES]:
        name = _safe_identifier(raw_name, limit=64)
        if name:
            result[name] = _sanitize_profile_capability(name, raw_meta)
    return result


def _pending_message_size(message: dict[str, Any]) -> int:
    try:
        return len(json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return _MAX_PENDING_MESSAGE_BYTES + 1


def _sanitize_client_meta(client: Any) -> dict[str, Any]:
    if not isinstance(client, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("name", "version", "default_profile"):
        value = _safe_identifier(client.get(key) or "")
        if value:
            result[key] = value
    for key in ("capabilities", "profiles"):
        values = _safe_identifier_list(client.get(key))
        if values:
            result[key] = values
    default_timeout = _safe_positive_int(client.get("default_timeout"), maximum=3600)
    if default_timeout is not None:
        result["default_timeout"] = default_timeout
    default_max_output = _safe_positive_int(
        client.get("default_max_output_bytes"),
        maximum=16 * 1024 * 1024,
    )
    if default_max_output is not None:
        result["default_max_output_bytes"] = default_max_output
    profile_capabilities = _sanitize_profile_capabilities(client.get("profile_capabilities"))
    if profile_capabilities:
        result["profile_capabilities"] = profile_capabilities
    return result


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._pending: dict[str, list[tuple[float, int, dict[str, Any]]]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}

    async def register(
        self,
        agent_name: str,
        websocket: WebSocket,
        connection_id: str | None = None,
        *,
        protocol_version: int = WS_PROTOCOL_VERSION,
        client: dict[str, Any] | None = None,
        capabilities: list[Any] | None = None,
        deliver_pending: bool = True,
    ) -> list[dict[str, Any]]:
        if agent_name in self._connections:
            await websocket.send_json(
                {
                    "type": "error",
                    "protocol_version": WS_PROTOCOL_VERSION,
                    "payload": {"error": "duplicate agent_name online", "code": "NAME_CONFLICT"},
                }
            )
            await websocket.close(code=4002)
            raise RuntimeError("duplicate agent_name online")
        self._connections[agent_name] = websocket
        now = time.time()
        client_meta = _sanitize_client_meta(client)
        self._metadata[agent_name] = {
            "connection_id": connection_id or agent_name,
            "connected_at": now,
            "last_seen": now,
            "protocol_version": protocol_version,
            "client": client_meta,
            "capabilities": _safe_identifier_list(capabilities) or client_meta.get("capabilities") or [],
        }
        if deliver_pending:
            return await self._deliver_pending(agent_name, websocket)
        return []

    async def deliver_pending(self, agent_name: str, websocket: WebSocket) -> list[dict[str, Any]]:
        return await self._deliver_pending(agent_name, websocket)

    async def unregister(self, agent_name: str, websocket: WebSocket | None = None) -> None:
        current = self._connections.get(agent_name)
        if current is not None and (websocket is None or current is websocket):
            self._connections.pop(agent_name, None)
            self._metadata.pop(agent_name, None)

    async def touch(self, agent_name: str, websocket: WebSocket | None = None) -> None:
        current = self._connections.get(agent_name)
        if current is None or (websocket is not None and current is not websocket):
            return
        meta = self._metadata.get(agent_name)
        if meta is not None:
            meta["last_seen"] = time.time()

    def pending_count(self, agent_name: str) -> int:
        now = time.time()
        return sum(1 for expires_at, _, _ in self._pending.get(agent_name, []) if expires_at >= now)

    def _pending_count(self) -> int:
        return sum(len(messages) for messages in self._pending.values())

    def _pending_bytes(self) -> int:
        return sum(size for messages in self._pending.values() for _, size, _ in messages)

    def _drop_oldest_pending(self) -> bool:
        candidates = [(messages[0][0], agent_name) for agent_name, messages in self._pending.items() if messages]
        if not candidates:
            return False
        _, agent_name = min(candidates)
        queue = self._pending.get(agent_name, [])
        if queue:
            queue.pop(0)
        if not queue:
            self._pending.pop(agent_name, None)
        return True

    def _enforce_pending_limits(self, agent_name: str) -> int:
        dropped = 0
        queue = self._pending.get(agent_name, [])
        while len(queue) > _MAX_PENDING_PER_AGENT:
            queue.pop(0)
            dropped += 1
        if not queue:
            self._pending.pop(agent_name, None)
        while self._pending_count() > _MAX_PENDING_TOTAL or self._pending_bytes() > _MAX_PENDING_TOTAL_BYTES:
            if not self._drop_oldest_pending():
                break
            dropped += 1
        return dropped

    async def send_if_online(self, agent_name: str, message: dict[str, Any]) -> bool:
        """Deliver a transient event only while the recipient is online."""

        websocket = self._connections.get(agent_name)
        if websocket is None:
            return False
        try:
            await websocket.send_json(message)
            await self.touch(agent_name, websocket)
            return True
        except Exception:
            await self.unregister(agent_name, websocket)
            return False

    async def send_or_queue(self, agent_name: str, message: dict[str, Any], ttl: float = 3600) -> bool:
        if await self.send_if_online(agent_name, message):
            return True
        message_size = _pending_message_size(message)
        if message_size > _MAX_PENDING_MESSAGE_BYTES:
            synapse_logger.warning(
                "dropping oversized pending message",
                extra={"event": "pending_message_dropped", "target": agent_name},
            )
            return False
        expires_at = time.time() + ttl
        self._pending.setdefault(agent_name, []).append((expires_at, message_size, message))
        dropped = self._enforce_pending_limits(agent_name)
        if dropped:
            synapse_logger.warning(
                "dropped pending messages after queue limit",
                extra={"event": "pending_queue_trimmed", "target": agent_name},
            )
        return False

    async def broadcast(self, message: dict[str, Any]) -> list[str]:
        delivered: list[str] = []
        for agent_name, websocket in list(self._connections.items()):
            try:
                await websocket.send_json(message)
                await self.touch(agent_name, websocket)
                delivered.append(agent_name)
            except Exception:
                await self.unregister(agent_name, websocket)
        return delivered

    async def _deliver_pending(self, agent_name: str, websocket: WebSocket) -> list[dict[str, Any]]:
        now = time.time()
        messages = self._pending.pop(agent_name, [])
        delivered: list[dict[str, Any]] = []
        remaining: list[tuple[float, int, dict[str, Any]]] = []
        for index, (expires_at, _message_size, message) in enumerate(messages):
            if expires_at < now:
                continue
            try:
                await websocket.send_json(message)
                delivered.append(message)
            except Exception:
                remaining.append((expires_at, _message_size, message))
                remaining.extend(messages[index + 1 :])
                break
        if remaining:
            self._pending[agent_name] = remaining + self._pending.get(agent_name, [])
            await self.unregister(agent_name, websocket)
        elif delivered:
            await self.touch(agent_name, websocket)
        return delivered

    def metadata_for(self, agent_name: str) -> dict[str, Any]:
        """Return a detached sanitized metadata snapshot for one online Agent."""

        metadata = self._metadata.get(agent_name)
        if metadata is None:
            return {}
        return json.loads(json.dumps(metadata, ensure_ascii=False, default=str))

    def remove_pending_task(self, agent_name: str, task_id: str) -> int:
        """Remove queued task deliveries after an administrator cancels a task."""

        messages = self._pending.get(agent_name, [])
        kept: list[tuple[float, int, dict[str, Any]]] = []
        removed = 0
        for item in messages:
            message = item[2]
            payload = message.get("payload") if isinstance(message, dict) else None
            queued_task_id = payload.get("task_id") if isinstance(payload, dict) else None
            if message.get("type") == "task" and queued_task_id == task_id:
                removed += 1
            else:
                kept.append(item)
        if kept:
            self._pending[agent_name] = kept
        else:
            self._pending.pop(agent_name, None)
        return removed

    def is_online(self, agent_name: str) -> bool:
        return agent_name in self._connections

    async def cleanup_stale_pending(self) -> int:
        now = time.time()
        removed = 0
        for agent_name in list(self._pending.keys()):
            messages = self._pending[agent_name]
            kept = [item for item in messages if item[0] >= now]
            removed += len(messages) - len(kept)
            if kept:
                self._pending[agent_name] = kept
            else:
                self._pending.pop(agent_name, None)
        return removed

    def online_agents(self) -> list[str]:
        return sorted(self._connections.keys())

    def online_agent_details(self) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        for agent_name in self.online_agents():
            meta = self._metadata.get(agent_name, {})
            details.append(
                {
                    "name": agent_name,
                    "connection_id": meta.get("connection_id", agent_name),
                    "connected_at": meta.get("connected_at"),
                    "last_seen": meta.get("last_seen"),
                    "protocol_version": meta.get("protocol_version", WS_PROTOCOL_VERSION),
                    "client": meta.get("client", {}),
                    "capabilities": meta.get("capabilities", []),
                }
            )
        return details


connection_manager = ConnectionManager()
