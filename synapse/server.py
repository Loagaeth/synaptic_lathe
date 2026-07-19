"""SynapticLathe 服务器 — FastAPI app + WebSocket + REST 端点。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import secrets
import stat
import time
from collections import deque
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from synapse import __version__
from synapse.agent_catalog import (
    _available_agent_details,
    _build_default_skills,
    build_connection_prompt,
)
from synapse.banner import format_banner
from synapse.config import GlobalConfig, MemoryConfig
from synapse.connection import connection_manager
from synapse.context.knowledge import (
    add_knowledge,
    delete_knowledge,
    list_knowledge,
)
from synapse.context.knowledge import (
    search_knowledge as ctx_search_knowledge,
)
from synapse.context.memory import (
    add_memory,
    delete_memory,
    get_recent_memories,
)
from synapse.context.memory import (
    search_memory as ctx_search_memory,
)
from synapse.context.persona import (
    delete_persona,
    list_personas,
    list_personas_detailed,
    set_persona,
)
from synapse.context.persona import (
    get_persona as ctx_get_persona,
)
from synapse.context.prompts import (
    delete_prompt,
    get_prompt,
    list_prompts,
    list_prompts_detailed,
    set_prompt,
)
from synapse.context.skills import (
    add_skill,
    delete_skill,
    get_skill,
    list_skills,
    list_skills_detailed,
)
from synapse.db import get_db
from synapse.file_utils import atomic_write_text, exclusive_file_lock
from synapse.handlers import handle_http_send, install_guide
from synapse.http_utils import public_request_urls
from synapse.logging import LOG_DIR, LOG_FILE_NAME, redact_sensitive_text, synapse_logger
from synapse.marker import MarkerScanner
from synapse.middleware import (
    BodySizeLimitMiddleware,
    csrf_middleware,
    log_requests_middleware,
    rate_cleanup_loop,
    rate_limit_middleware,
    resolve_client_ip,
    security_headers_middleware,
)
from synapse.protocol import (
    API_PREFIX,
    API_VERSION,
    MIN_SUPPORTED_WS_PROTOCOL_VERSION,
    WS_PROTOCOL_VERSION,
    is_supported_ws_protocol,
    parse_ws_protocol_version,
    protocol_metadata,
)
from synapse.router import resolve_target, select_target
from synapse.session import generate_correlation_id, generate_session_id
from synapse.task_api import create_task_router
from synapse.task_events import probe_coordinator, task_events
from synapse.task_management import parse_generated_tags, store_generated_tags
from synapse.task_queue import (
    TaskAlreadyExistsError,
    create_task,
    get_task,
    update_task_status,
)
from synapse.task_status import (
    ACTIVE_TARGET_TASK_STATUSES,
    COMPLETABLE_TASK_STATUSES,
    NONTERMINAL_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
)
from synapse.text_utils import sanitize_untrusted_text
from synapse.web_task_controller import WebTaskController

# ── 全局状态 ──────────────────────────────────

_project_root: Path | None = None
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_PUBLIC_BIND_HOSTS = {".".join(("0", "0", "0", "0")), "::"}
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MAX_PERSONA_BYTES = 256
_FORWARDED_TASK_STRING_FIELDS = {"profile", "tool", "session_id", "session", "ssid"}
_MAX_TASK_OPTION_BYTES = 4096
_MAX_TASK_TIMEOUT_SECONDS = 3600
_MAX_FILE_CONTENT_BYTES = 500_000
_MAX_PERSONA_COUNTERS = 10_000


def _is_local_host(host: str) -> bool:
    return host in _LOCAL_HOSTS


def _error_detail(error: str, code: str) -> dict[str, str]:
    return {"error": error, "code": code}


def _is_valid_agent_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_AGENT_NAME_RE.fullmatch(name))


def _is_valid_correlation_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_CORRELATION_ID_RE.fullmatch(value))


def _task_persona(cfg: GlobalConfig, payload: Mapping[str, Any]) -> tuple[str, str]:
    requested = payload.get("persona", "")
    if not isinstance(requested, str):
        return "", "'persona' must be a string"
    if len(requested.encode("utf-8")) > _MAX_PERSONA_BYTES or any(ord(char) < 32 for char in requested):
        return "", "'persona' is invalid or too large"
    return _scoped_persona(cfg.memory, requested), ""


def _scoped_persona(memory_config: MemoryConfig, requested: str = "") -> str:
    return "shared" if memory_config.scope == "shared" else requested


def _bounded_timeout(value: Any, default: int = 60) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return min(int(value), _MAX_TASK_TIMEOUT_SECONDS)


def _pending_ttl_seconds(cfg: GlobalConfig) -> int:
    return int(cfg.server.pending_message_ttl_hours * 3600)


def _copy_forwarded_task_options(payload: dict[str, Any], max_body_bytes: int) -> tuple[dict[str, str], str]:
    """Copy narrow dispatcher metadata from a caller payload to the target task."""
    forwarded: dict[str, str] = {}
    for name in _FORWARDED_TASK_STRING_FIELDS:
        value = payload.get(name)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            return {}, f"'{name}' must be a string"
        if len(value.encode("utf-8")) > _MAX_TASK_OPTION_BYTES:
            return {}, f"'{name}' is too large"
        forwarded[name] = value

    stdin = payload.get("stdin")
    if stdin not in (None, ""):
        if not isinstance(stdin, str):
            return {}, "'stdin' must be a string"
        if len(stdin.encode("utf-8")) > max_body_bytes:
            return {}, "stdin is too large"
        forwarded["stdin"] = stdin

    return forwarded, ""


def _copy_agent_overrides(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    overrides: dict[str, Any] = {}
    for name in ("provider", "model", "username"):
        value = payload.get(name)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            return {}, f"'{name}' must be a string"
        if len(value) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            return {}, f"'{name}' is invalid or too large"
        overrides[name] = value

    if "stream" in payload:
        if not isinstance(payload["stream"], bool):
            return {}, "'stream' must be a boolean"
        overrides["stream"] = payload["stream"]
    return overrides, ""


def _task_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _build_ws_message(msg_type: str, payload: dict[str, Any], correlation_id: str | None = None) -> dict:
    safe_correlation_id = correlation_id if _is_valid_correlation_id(correlation_id) else generate_correlation_id()
    return {
        "type": msg_type,
        "protocol_version": WS_PROTOCOL_VERSION,
        "payload": payload,
        "correlation_id": safe_correlation_id,
        "timestamp": _task_timestamp(),
    }


def _json_message_size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _sanitize_result_payload(payload: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    result = dict(payload)
    result["task_id"] = task_id
    for field in ("result", "output", "error", "stderr"):
        value = result.get(field)
        if isinstance(value, str):
            result[field] = sanitize_untrusted_text(value)
    return result


async def _send_ws_error(ws: WebSocket, error: str, code: str, correlation_id: str | None = None) -> None:
    await ws.send_json(_build_ws_message("error", _error_detail(error, code), correlation_id))


def _extract_bearer_token(headers) -> str:
    auth = headers.get("authorization", "")
    scheme, separator, token = auth.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or token != token.strip()
        or any(char.isspace() for char in token)
    ):
        return ""
    return token


def set_project_root(config_path: str) -> None:
    global _project_root
    _project_root = Path(config_path).resolve().parent


def _validated_data_path(filepath: str) -> Path:
    base = _project_root or Path.cwd()
    data_dir = Path(os.path.abspath(base / "data"))
    candidate = Path(filepath)
    if not candidate.is_absolute():
        candidate = base / candidate
    path = Path(os.path.abspath(candidate))
    try:
        relative = path.relative_to(data_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_detail(f"File must be under data/ directory: {filepath}", "INVALID_PATH"),
        ) from exc

    try:
        data_dir_stat = data_dir.lstat()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=_error_detail("Data directory not found", "NOT_FOUND")) from None
    if stat.S_ISLNK(data_dir_stat.st_mode) or not stat.S_ISDIR(data_dir_stat.st_mode):
        raise HTTPException(
            status_code=400,
            detail=_error_detail("Data directory must not be a symbolic link", "INVALID_PATH"),
        )

    current = data_dir
    for part in relative.parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=_error_detail("File not found", "NOT_FOUND")) from None
        if stat.S_ISLNK(current_stat.st_mode):
            raise HTTPException(
                status_code=400,
                detail=_error_detail("Symbolic links are not allowed", "INVALID_PATH"),
            )
    return path


def _read_bounded_utf8_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("File must be a regular file")
        if file_stat.st_size > _MAX_FILE_CONTENT_BYTES:
            raise OverflowError
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(_MAX_FILE_CONTENT_BYTES + 1)
        if len(data) > _MAX_FILE_CONTENT_BYTES:
            raise OverflowError
        return data.decode("utf-8")
    finally:
        os.close(fd)


async def _safe_read_file(filepath: str) -> str:
    path = _validated_data_path(filepath)
    try:
        content = await anyio.to_thread.run_sync(_read_bounded_utf8_file, path)
    except OverflowError:
        raise HTTPException(
            status_code=413,
            detail=_error_detail("File is too large", "PAYLOAD_TOO_LARGE"),
        ) from None
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail=_error_detail("File must be valid UTF-8 text", "INVALID_FILE"),
        ) from None
    except (OSError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=_error_detail("File is not a readable regular file", "INVALID_FILE"),
        ) from None
    if not content:
        raise HTTPException(status_code=400, detail=_error_detail(f"File is empty: {filepath}", "EMPTY_FILE"))
    return content


async def _content_or_file(content: str, filepath: str) -> str:
    if content:
        return content
    if not filepath:
        raise HTTPException(
            status_code=400,
            detail=_error_detail(
                "Either 'content' or 'file' is required",
                "INVALID_REQUEST",
            ),
        )
    return await _safe_read_file(filepath)


# ── 请求模型 ──────────────────────────────────


def _validate_label(value: str, label: str, *, allow_empty: bool = False) -> str:
    if not value:
        if allow_empty:
            return ""
        raise ValueError(f"{label} must not be empty")
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} must not contain surrounding whitespace or control characters")
    return value


class _WriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona: str = Field(default="", max_length=128)
    content: str = Field(default="", max_length=500_000)
    file: str = Field(default="", max_length=4096)

    @field_validator("persona")
    @classmethod
    def validate_persona(cls, value: str) -> str:
        return _validate_label(value, "persona", allow_empty=True)

    async def get_content(self) -> str:
        return await _content_or_file(self.content, self.file)


class WriteContentRequest(_WriteRequest):
    pass


class KnowledgeWriteRequest(_WriteRequest):
    title: str = Field(min_length=1, max_length=256)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _validate_label(value, "title")


class _NamedWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    content: str = Field(default="", max_length=500_000)
    file: str = Field(default="", max_length=4096)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_label(value, "name")

    async def get_content(self) -> str:
        return await _content_or_file(self.content, self.file)


class SkillWriteRequest(_NamedWriteRequest):
    pass


class PersonaWriteRequest(_NamedWriteRequest):
    pass


class PromptWriteRequest(_NamedWriteRequest):
    pass


class ConnectionPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_name: str = Field(default="agent", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    agent_type: str = Field(default="generic", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class MemoryConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = Field(default=None, max_length=512)
    embedding_api_url: str | None = Field(default=None, max_length=2048)
    embedding_dimensions: int | None = Field(default=None, ge=0, le=4096)
    embedding_timeout: int | None = Field(default=None, ge=1, le=300)
    embedding_trust_env: bool | None = None


class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory: MemoryConfigUpdate | None = None
    server: dict[str, Any] | None = None

    @model_validator(mode="after")
    def check_server_readonly(self) -> ConfigUpdateRequest:
        if self.server is not None:
            raise ValueError("Server config is read-only via API. Edit config.yaml directly.")
        return self


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=10_000)
    persona: str = Field(default="", max_length=128)
    limit: int = Field(default=5, ge=1, le=100)

    @field_validator("persona")
    @classmethod
    def validate_persona(cls, value: str) -> str:
        return _validate_label(value, "persona", allow_empty=True)


# ── 认证 ──────────────────────────────────────


async def verify_token(request: Request) -> str:
    cfg = request.app.state.config
    if _auth_disabled_allowed(cfg):
        return ""
    expected = cfg.server.api_key.get_secret_value()
    if not expected:
        raise HTTPException(
            status_code=403,
            detail=_error_detail(
                "API key not configured for non-local access",
                "AUTH_REQUIRED",
            ),
        )
    auth = request.headers.get("authorization", "")
    if not auth:
        raise HTTPException(status_code=403, detail=_error_detail("Missing Bearer token", "UNAUTHORIZED"))
    token = _extract_bearer_token(request.headers)
    if not token:
        raise HTTPException(status_code=403, detail=_error_detail("Invalid authorization format", "UNAUTHORIZED"))
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail=_error_detail("Invalid token", "UNAUTHORIZED"))
    return token


async def verify_context_read(request: Request) -> str:
    """Authenticate GET context reads, with an explicit public-read opt-in."""
    cfg = request.app.state.config
    if cfg.server.public_read_context:
        return ""
    return await verify_token(request)


# ── 管理日志 ──────────────────────────────────

_LOG_TAIL_BYTES = 1_048_576


def _log_file_path() -> Path:
    return Path(os.path.abspath(LOG_DIR / LOG_FILE_NAME))


def _regular_log_stat(path: Path) -> os.stat_result | None:
    try:
        path_stat = path.lstat()
    except OSError:
        return None
    return path_stat if stat.S_ISREG(path_stat.st_mode) else None


def _open_log_file(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("Log path is not a regular file")
    return os.fdopen(fd, "rb")


def _redact_log_text(text: str) -> str:
    return redact_sensitive_text(text)


def _parse_log_line(line: str) -> dict[str, Any]:
    redacted = _redact_log_text(line.strip())
    if not redacted:
        return {"raw": ""}
    try:
        data = json.loads(redacted)
    except json.JSONDecodeError:
        return {"raw": redacted}
    return data if isinstance(data, dict) else {"raw": str(data)}


def _read_recent_log_entries(limit: int) -> list[dict[str, Any]]:
    path = _log_file_path()
    if _regular_log_stat(path) is None:
        return []
    try:
        with _open_log_file(path) as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return [_parse_log_line(line) for line in lines[-limit:]]


def _log_end_state(path: Path) -> tuple[int, tuple[int, int] | None]:
    path_stat = _regular_log_stat(path)
    if path_stat is None:
        return 0, None
    return path_stat.st_size, (path_stat.st_dev, path_stat.st_ino)


def _read_log_since(
    path: Path,
    position: int,
    identity: tuple[int, int] | None,
) -> tuple[bytes, int, tuple[int, int] | None]:
    try:
        with _open_log_file(path) as fh:
            file_stat = os.fstat(fh.fileno())
            current_identity = (file_stat.st_dev, file_stat.st_ino)
            if identity != current_identity or file_stat.st_size < position:
                position = 0
            fh.seek(position)
            chunk = fh.read()
            return chunk, fh.tell(), current_identity
    except OSError:
        return b"", 0, None


def _sse_log_event(entry: dict[str, Any]) -> str:
    payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n"


# ── 连接提示词 ────────────────────────────────


def _auth_disabled_allowed(config: GlobalConfig) -> bool:
    """Allow unauthenticated HTTP administration only on an explicit local bind."""
    return not config.server.api_key.get_secret_value() and _is_local_host(config.server.host)


def _ws_auth_disabled_allowed(config: GlobalConfig) -> bool:
    return not config.server.get_worker_api_key() and _is_local_host(config.server.host)


def _server_protocol_metadata(config: GlobalConfig) -> dict[str, Any]:
    return protocol_metadata(
        auth_required=not _auth_disabled_allowed(config),
        worker_auth_required=not _ws_auth_disabled_allowed(config),
        public_read_context=config.server.public_read_context,
    )


# ── 记忆自动沉淀与任务可靠性 ──────────────────


_persona_interactions: dict[str, tuple[int, float]] = {}
_timeout_tasks: dict[str, tuple[str, asyncio.Task]] = {}
_auto_memory_tasks: set[asyncio.Task[None]] = set()
_web_http_tasks: dict[str, asyncio.Task[None]] = {}
_ws_connect_attempts: dict[str, deque[float]] = {}
_ws_connect_rate_lock = asyncio.Lock()
_MAX_WS_CLIENT_KEYS = 20_000


async def _allow_ws_connection(cfg: GlobalConfig, client_ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - cfg.server.ws_rate_limit_window
    async with _ws_connect_rate_lock:
        timestamps = _ws_connect_attempts.setdefault(client_ip, deque())
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= cfg.server.ws_rate_limit_max:
            return False
        if len(_ws_connect_attempts) > _MAX_WS_CLIENT_KEYS and len(timestamps) == 0:
            _ws_connect_attempts.pop(client_ip, None)
            return False
        timestamps.append(now)
        return True


async def _ws_rate_cleanup_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(120)
        window = app.state.config.server.ws_rate_limit_window
        cutoff = time.monotonic() - window
        async with _ws_connect_rate_lock:
            for client_ip in list(_ws_connect_attempts):
                timestamps = _ws_connect_attempts[client_ip]
                while timestamps and timestamps[0] <= cutoff:
                    timestamps.popleft()
                if not timestamps:
                    del _ws_connect_attempts[client_ip]
            if len(_ws_connect_attempts) > _MAX_WS_CLIENT_KEYS:
                oldest = sorted(
                    _ws_connect_attempts,
                    key=lambda address: _ws_connect_attempts[address][0],
                )
                for client_ip in oldest[: len(_ws_connect_attempts) - _MAX_WS_CLIENT_KEYS // 2]:
                    del _ws_connect_attempts[client_ip]


def _memory_fragment(value: Any, limit: int) -> str:
    text = redact_sensitive_text(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated before automatic memory storage]"


async def _maybe_condense_memory(cfg: GlobalConfig, task_id: str, result: str = "") -> None:
    """Store a bounded task transcript only when explicitly enabled."""

    threshold = cfg.server.auto_memory_threshold
    if threshold <= 0:
        return
    task = await get_task(cfg.db_path, task_id)
    if not task or task.get("purpose", "execute") != "execute":
        return
    persona = str(task.get("persona") or "")
    if cfg.memory.scope == "shared":
        persona = "shared"
    elif not persona:
        return

    if persona not in _persona_interactions and len(_persona_interactions) >= _MAX_PERSONA_COUNTERS:
        oldest = sorted(_persona_interactions, key=lambda key: _persona_interactions[key][1])
        for stale_persona in oldest[: _MAX_PERSONA_COUNTERS // 10]:
            _persona_interactions.pop(stale_persona, None)
    previous = _persona_interactions.get(persona, (0, 0.0))
    count = previous[0] + 1
    now = time.monotonic()
    _persona_interactions[persona] = (count, now)
    if count < threshold:
        return
    _persona_interactions[persona] = (0, now)

    max_chars = cfg.server.auto_memory_max_chars
    fragment_limit = max(128, max_chars // 2)
    plan = _memory_fragment(task.get("content", ""), fragment_limit)
    result_text = _memory_fragment(result, fragment_limit)
    summary = (
        f"[{task.get('source_agent', '?')} -> {task.get('target_agent', '?')}]\nPlan: {plan}\nResult: {result_text}"
    )[:max_chars]
    await add_memory(cfg.db_path, summary, persona, memory_config=cfg.memory)
    synapse_logger.info(
        "automatic task memory stored",
        extra={"event": "auto_memory_stored", "task_id": task_id},
    )


async def _store_auto_memory_safely(cfg: GlobalConfig, task_id: str, result: str) -> None:
    try:
        await _maybe_condense_memory(cfg, task_id, result)
    except asyncio.CancelledError:
        raise
    except Exception:
        synapse_logger.exception(
            "automatic task memory failed",
            extra={"event": "auto_memory_failed", "task_id": task_id},
        )


def _schedule_auto_memory(cfg: GlobalConfig, task_id: str, result: str) -> None:
    if cfg.server.auto_memory_threshold <= 0:
        return
    task = asyncio.create_task(
        _store_auto_memory_safely(cfg, task_id, result),
        name=f"auto-memory-{task_id}",
    )
    _auto_memory_tasks.add(task)
    task.add_done_callback(_auto_memory_tasks.discard)


async def _cancel_timeout_watcher(task_id: str) -> tuple[str, asyncio.Task] | None:
    watcher_info = _timeout_tasks.pop(task_id, None)
    if watcher_info is None:
        return None
    _, watcher = watcher_info
    if not watcher.done():
        watcher.cancel()
    return watcher_info


async def _complete_task_and_deliver(
    cfg: GlobalConfig,
    task_id: str,
    source_agent: str,
    payload: dict[str, Any],
    memory_result: str,
) -> None:
    task = await get_task(cfg.db_path, task_id)
    if not task:
        await _cancel_timeout_watcher(task_id)
        return
    completed = await update_task_status(
        cfg.db_path,
        task_id,
        "COMPLETED",
        result=memory_result,
        expected_statuses=COMPLETABLE_TASK_STATUSES,
    )
    await _cancel_timeout_watcher(task_id)
    if not completed:
        return
    source_kind = str(task.get("source_kind") or "agent")
    if source_kind == "web":
        delivered = True
        await task_events.publish(
            {
                "event": "task_completed",
                "task_id": task_id,
                "group_id": task.get("group_id", ""),
                "status": "COMPLETED",
                "target": task.get("target_agent", ""),
                "profile": task.get("profile", ""),
                "purpose": task.get("purpose", "execute"),
            }
        )
        if task.get("purpose") == "tag":
            generated = parse_generated_tags(memory_result)
            if generated:
                await store_generated_tags(
                    cfg.db_path,
                    agent_name=str(task.get("target_agent") or ""),
                    profile=str(task.get("profile") or ""),
                    values=generated,
                )
    else:
        delivered = await connection_manager.send_or_queue(
            source_agent,
            _build_ws_message("task_result", payload, task_id),
            ttl=_pending_ttl_seconds(cfg),
        )
    synapse_logger.info(
        "agent task completed",
        extra={
            "event": "agent_task_completed",
            "source": task.get("source_agent", source_agent),
            "target": task.get("target_agent", ""),
            "task_id": task_id,
            "profile": task.get("profile", ""),
            "purpose": task.get("purpose", "execute"),
            "queued": not delivered,
        },
    )
    _schedule_auto_memory(cfg, task_id, memory_result)


async def _mark_redelivered_tasks_dispatched(cfg: GlobalConfig, messages: list[dict[str, Any]]) -> None:
    """Move reconnect-queued task messages to DISPATCHED after a successful send."""

    for message in messages:
        if message.get("type") != "task":
            continue
        payload = message.get("payload")
        if not isinstance(payload, dict):
            continue
        task_id = payload.get("task_id")
        if _is_valid_correlation_id(task_id):
            await update_task_status(
                cfg.db_path,
                task_id,
                "DISPATCHED",
                expected_statuses=("QUEUED",),
            )


async def _fail_tasks_targeting_disconnected_agent(cfg: GlobalConfig, agent_name: str) -> int:
    if agent_name == "unknown":
        return 0

    async with get_db(cfg.db_path) as db:
        cur = await db.execute(
            """
            SELECT id, source_agent, source_kind, group_id, profile, purpose
            FROM tasks WHERE target_agent=? AND status IN (?,?)
            """,
            (agent_name, *ACTIVE_TARGET_TASK_STATUSES),
        )
        active_tasks = [dict(row) for row in await cur.fetchall()]

    failed_count = 0
    for task in active_tasks:
        task_id = task["id"]
        source_agent = task.get("source_agent", "")
        failed = await update_task_status(
            cfg.db_path,
            task_id,
            "ERROR",
            result=f"Target agent {agent_name!r} disconnected before completing task {task_id}",
            expected_statuses=ACTIVE_TARGET_TASK_STATUSES,
        )
        if not failed:
            continue
        failed_count += 1
        await _cancel_timeout_watcher(task_id)
        synapse_logger.warning(
            "agent task failed because target disconnected",
            extra={
                "event": "agent_task_target_disconnected",
                "source": source_agent,
                "target": agent_name,
                "task_id": task_id,
            },
        )
        if task.get("source_kind") == "web":
            await task_events.publish(
                {
                    "event": "task_error",
                    "task_id": task_id,
                    "group_id": task.get("group_id", ""),
                    "status": "ERROR",
                    "target": agent_name,
                    "profile": task.get("profile", ""),
                    "purpose": task.get("purpose", "execute"),
                }
            )
        elif source_agent:
            await connection_manager.send_or_queue(
                source_agent,
                _build_ws_message(
                    "error",
                    _error_detail(
                        f"Target agent {agent_name!r} disconnected before completing task {task_id}",
                        "TARGET_DISCONNECTED",
                    ),
                    task_id,
                ),
                ttl=_pending_ttl_seconds(cfg),
            )
    return failed_count


async def _watch_task_timeout(
    cfg: GlobalConfig,
    task_id: str,
    timeout_seconds: int,
    source_agent: str,
    target_agent: str,
    correlation_id: str,
) -> None:
    try:
        await asyncio.sleep(timeout_seconds)
        timed_out = await update_task_status(
            cfg.db_path,
            task_id,
            "TIMEOUT",
            result=f"Task timed out after {timeout_seconds}s",
            expected_statuses=NONTERMINAL_TASK_STATUSES,
        )
        if not timed_out:
            return
        _schedule_auto_memory(cfg, task_id, "TIMEOUT")
        task = await get_task(cfg.db_path, task_id)
        if task and task.get("source_kind") == "web":
            await task_events.publish(
                {
                    "event": "task_timeout",
                    "task_id": task_id,
                    "group_id": task.get("group_id", ""),
                    "status": "TIMEOUT",
                    "target": target_agent,
                    "profile": task.get("profile", ""),
                    "purpose": task.get("purpose", "execute"),
                }
            )
        else:
            await connection_manager.send_or_queue(
                source_agent,
                _build_ws_message(
                    "error",
                    _error_detail(f"Task {task_id} timed out after {timeout_seconds}s", "TIMEOUT"),
                    correlation_id,
                ),
                ttl=_pending_ttl_seconds(cfg),
            )
        connection_manager.remove_pending_task(target_agent, task_id)
        await connection_manager.send_if_online(
            target_agent,
            _build_ws_message(
                "cancel",
                {"task_id": task_id, "reason": "timeout"},
                correlation_id,
            ),
        )
        synapse_logger.warning(
            "agent task timed out",
            extra={
                "event": "agent_task_timeout",
                "source": source_agent,
                "target": target_agent,
                "task_id": task_id,
                "timeout": timeout_seconds,
            },
        )
    finally:
        current = _timeout_tasks.get(task_id)
        if current is not None and current[1] is asyncio.current_task():
            _timeout_tasks.pop(task_id, None)


# ── 配置文件写回 ──────────────────────────────

_config_write_lock = asyncio.Lock()  # 仅保护配置写入


class ConfigSaveError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _read_regular_config(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened_stat = os.fstat(fd)
        path_stat = path.lstat()
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or (opened_stat.st_dev, opened_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise ConfigSaveError("Config path must be a regular file", "CONFIG_UNSAFE")
        if opened_stat.st_size > 4 * 1024 * 1024:
            raise ConfigSaveError("Config file is too large", "CONFIG_TOO_LARGE")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as handle:
            fd = -1
            return handle.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _rewrite_memory_config(config_path: Path, cfg: GlobalConfig) -> None:
    lock_path = config_path.with_name(f".{config_path.name}.lock")
    with exclusive_file_lock(lock_path):
        source = _read_regular_config(config_path)
        try:
            from ruamel.yaml import YAML

            yaml = YAML()
            yaml.preserve_quotes = True
            data = yaml.load(source) or {}
            if not isinstance(data, Mapping):
                raise ConfigSaveError("Config root must be a mapping", "CONFIG_PARSE_ERROR")
            memory = data.setdefault("memory", {})
            if not isinstance(memory, Mapping):
                raise ConfigSaveError("Config memory section must be a mapping", "CONFIG_PARSE_ERROR")
            memory.update(_memory_defaults(cfg))
            output = io.StringIO()
            yaml.dump(data, output)
            serialized = output.getvalue()
        except ImportError:
            import yaml

            data = yaml.safe_load(source) or {}
            if not isinstance(data, dict):
                raise ConfigSaveError("Config root must be a mapping", "CONFIG_PARSE_ERROR") from None
            memory = data.setdefault("memory", {})
            if not isinstance(memory, dict):
                raise ConfigSaveError("Config memory section must be a mapping", "CONFIG_PARSE_ERROR") from None
            memory.update(_memory_defaults(cfg))
            serialized = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

        atomic_write_text(config_path, serialized, overwrite=True, mode=0o600)


async def _save_config_unlocked(request: Request, cfg: GlobalConfig) -> None:
    config_path_value = getattr(request.app.state, "config_path", "")
    if not config_path_value:
        raise ConfigSaveError("Config path is not available", "CONFIG_MISSING")
    try:
        await asyncio.to_thread(_rewrite_memory_config, Path(config_path_value), cfg)
    except FileNotFoundError as exc:
        raise ConfigSaveError("Config file not found", "CONFIG_MISSING") from exc
    except ConfigSaveError:
        raise
    except Exception as exc:
        synapse_logger.exception("safe config write failed")
        raise ConfigSaveError("Failed to update config safely", "CONFIG_WRITE_ERROR") from exc


async def _save_config_locked(request: Request, cfg: GlobalConfig) -> None:
    async with _config_write_lock:
        await _save_config_unlocked(request, cfg)


def _memory_defaults(cfg: GlobalConfig) -> dict[str, Any]:
    return {
        "scope": cfg.memory.scope,
        "embedding_provider": cfg.memory.embedding_provider,
        "embedding_model": cfg.memory.embedding_model,
        "embedding_api_url": cfg.memory.embedding_api_url,
        # Secrets are intentionally never written back by an API request.
        "embedding_dimensions": cfg.memory.embedding_dimensions,
        "embedding_timeout": cfg.memory.embedding_timeout,
        "embedding_trust_env": cfg.memory.embedding_trust_env,
    }


# ── 定期任务清理 ──────────────────────────────


async def _pending_cleanup_loop() -> None:
    """Periodically expire undeliverable offline messages."""

    while True:
        await asyncio.sleep(3600)
        try:
            removed = await connection_manager.cleanup_stale_pending()
            if removed:
                synapse_logger.info(
                    "stale pending messages removed",
                    extra={"event": "pending_cleanup", "count": removed},
                )
        except Exception:
            synapse_logger.exception("pending message cleanup failed")


async def _task_cleanup_loop(app: FastAPI) -> None:
    from synapse.task_queue import cleanup_stale_tasks

    while True:
        await asyncio.sleep(21600)
        try:
            removed = await cleanup_stale_tasks(
                app.state.config.db_path,
                app.state.config.server.task_history_hours,
            )
            if removed:
                synapse_logger.info(
                    "stale tasks removed",
                    extra={"event": "task_cleanup", "count": removed},
                )

            cutoff = time.monotonic() - 86400
            for persona, (_, last_seen) in list(_persona_interactions.items()):
                if last_seen < cutoff:
                    _persona_interactions.pop(persona, None)
        except Exception:
            synapse_logger.exception("task cleanup failed")


# ── App 初始化 ────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    import httpx

    http_client = httpx.AsyncClient(
        timeout=120,
        trust_env=app.state.config.server.outbound_trust_env,
    )
    app.state.http_client = http_client
    background_tasks = [
        asyncio.create_task(rate_cleanup_loop(app), name="http-rate-cleanup"),
        asyncio.create_task(_ws_rate_cleanup_loop(app), name="ws-rate-cleanup"),
        asyncio.create_task(_task_cleanup_loop(app), name="task-cleanup"),
        asyncio.create_task(_pending_cleanup_loop(), name="pending-cleanup"),
    ]
    try:
        yield
    finally:
        timeout_watchers = [watcher for _, watcher in _timeout_tasks.values()]
        auto_memory_tasks = list(_auto_memory_tasks)
        web_http_tasks = list(_web_http_tasks.values())
        _timeout_tasks.clear()
        _auto_memory_tasks.clear()
        _web_http_tasks.clear()
        for task in [*background_tasks, *timeout_watchers, *auto_memory_tasks, *web_http_tasks]:
            task.cancel()
        await asyncio.gather(
            *background_tasks,
            *timeout_watchers,
            *auto_memory_tasks,
            *web_http_tasks,
            return_exceptions=True,
        )
        await http_client.aclose()


app = FastAPI(title="SynapticLathe", version=__version__, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": str(exc.errors()[0]["msg"]) if exc.errors() else "Validation failed",
            "code": "VALIDATION_ERROR",
        },
    )


# ── 中间件注册 ────────────────────────────────

_app_cors_configured: bool = False


def _setup_cors(config: GlobalConfig) -> None:
    global _app_cors_configured
    if _app_cors_configured:
        return
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.server.get_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _app_cors_configured = True


app.add_middleware(BodySizeLimitMiddleware)
app.middleware("http")(rate_limit_middleware)
app.middleware("http")(security_headers_middleware)
app.middleware("http")(csrf_middleware)
app.middleware("http")(log_requests_middleware)


# ── 健康检查 ──────────────────────────────────


@app.get("/_debug/memory_config")
async def debug_memory_config(request: Request, _token: str = Depends(verify_token)):
    cfg = request.app.state.config
    mc = cfg.memory
    return {
        "provider": mc.embedding_provider,
        "model": mc.embedding_model,
        "api_url": mc.embedding_api_url,
        "api_key_set": bool(mc.embedding_api_key.get_secret_value()),
    }


@app.get("/health")
async def health(request: Request, check: str = Query(default="")):
    cfg = request.app.state.config
    payload = {
        "status": "ok",
        "version": __version__,
        "api_version": API_VERSION,
        "ws_protocol_version": WS_PROTOCOL_VERSION,
    }
    if check == "db":
        try:
            async with get_db(cfg.db_path) as db:
                await db.execute("SELECT 1")
            payload["db"] = "ok"
        except Exception:
            synapse_logger.warning(
                "database health check failed",
                extra={"event": "health_db_failed"},
                exc_info=True,
            )
            payload["status"] = "degraded"
            payload["db"] = "unavailable"
    return payload


@app.get("/version")
async def version_endpoint(request: Request):
    return _server_protocol_metadata(request.app.state.config)


@app.get("/admin/logs")
async def admin_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    _token: str = Depends(verify_token),
):
    path = _log_file_path()
    logs = await anyio.to_thread.run_sync(_read_recent_log_entries, limit)
    return {
        "source": str(LOG_DIR / LOG_FILE_NAME),
        "exists": _regular_log_stat(path) is not None,
        "logs": logs,
    }


@app.get("/admin/logs/stream")
async def admin_log_stream(
    request: Request,
    limit: int = Query(default=50, ge=0, le=500),
    _token: str = Depends(verify_token),
):
    async def event_generator():
        path = _log_file_path()
        if limit:
            entries = await anyio.to_thread.run_sync(_read_recent_log_entries, limit)
            for entry in entries:
                yield _sse_log_event(entry)
        position, identity = await anyio.to_thread.run_sync(_log_end_state, path)
        last_heartbeat = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            chunk, position, identity = await anyio.to_thread.run_sync(
                _read_log_since,
                path,
                position,
                identity,
            )
            for line in chunk.decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    yield _sse_log_event(_parse_log_line(line))
            now = time.monotonic()
            if not chunk and now - last_heartbeat >= 15:
                yield ": keepalive\n\n"
                last_heartbeat = now
            await anyio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 安装指南 ──────────────────────────────────


@app.get("/install/{agent_type}")
async def install_endpoint(request: Request, agent_type: str):
    return await install_guide(request, agent_type)


@app.get("/connection-prompt")
async def connection_prompt_endpoint(
    request: Request,
    agent_name: str = Query(default="agent", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    agent_type: str = Query(default="generic", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    _token: str = Depends(verify_token),
):
    cfg = request.app.state.config
    return build_connection_prompt(cfg, agent_name, agent_type, public_request_urls(request)[0])


@app.post("/connection-prompt")
async def connection_prompt_post_endpoint(
    body: ConnectionPromptRequest,
    request: Request,
    _token: str = Depends(verify_token),
):
    cfg = request.app.state.config
    return build_connection_prompt(cfg, body.agent_name, body.agent_type, public_request_urls(request)[0])


# ── WebSocket ─────────────────────────────────


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    cfg: GlobalConfig = app.state.config
    if not await _allow_ws_connection(cfg, resolve_client_ip(ws)):
        await ws.close(code=1013, reason="Too many WebSocket connection attempts")
        return
    worker_key = cfg.server.get_worker_api_key()
    admin_key = cfg.server.api_key.get_secret_value()
    token = _extract_bearer_token(ws.headers)

    valid_token = bool(token) and (
        (bool(worker_key) and secrets.compare_digest(token, worker_key))
        or (bool(admin_key) and secrets.compare_digest(token, admin_key))
    )
    if worker_key and not valid_token:
        await ws.close(code=4001, reason="Unauthorized")
        return
    if not worker_key and not _ws_auth_disabled_allowed(cfg):
        await ws.close(code=4001, reason="Worker API key required for non-local WebSocket access")
        return

    origin = ws.headers.get("origin", "")
    allowed = cfg.server.get_cors_origins()
    if origin and allowed != ["*"] and origin not in allowed:
        await ws.close(code=4001, reason="Origin not allowed")
        return

    await ws.accept()
    agent_name = "unknown"
    session_id = generate_session_id()
    scanner = MarkerScanner()
    current_task: dict[str, Any] | None = None
    streamed_result_parts: list[str] = []
    streamed_result_bytes = 0
    message_times: deque[float] = deque()
    registered = False

    async def heartbeat():
        while True:
            await asyncio.sleep(cfg.server.ws_ping_interval)
            try:
                await ws.send_json({"type": "ping", "protocol_version": WS_PROTOCOL_VERSION})
            except Exception:
                with suppress(Exception):
                    await ws.close()
                break

    heartbeat_task = asyncio.create_task(heartbeat())

    def reset_stream() -> None:
        nonlocal streamed_result_bytes
        scanner.reset()
        streamed_result_parts.clear()
        streamed_result_bytes = 0

    async def append_stream_content(cid: str, content: str) -> bool:
        """Append and relay a bounded task stream fragment; return False on overflow."""

        nonlocal current_task, streamed_result_bytes
        if not content or not current_task:
            return True
        encoded_size = len(content.encode("utf-8"))
        if streamed_result_bytes + encoded_size > cfg.server.max_body_bytes:
            source = str(current_task.get("source_agent") or "")
            failed = await update_task_status(
                cfg.db_path,
                cid,
                "ERROR",
                result="Streamed result exceeded the configured size limit",
                expected_statuses=COMPLETABLE_TASK_STATUSES,
            )
            await _cancel_timeout_watcher(cid)
            if failed and current_task.get("source_kind") == "web":
                await task_events.publish(
                    {
                        "event": "task_error",
                        "task_id": cid,
                        "group_id": current_task.get("group_id", ""),
                        "status": "ERROR",
                        "target": current_task.get("target_agent", agent_name),
                        "profile": current_task.get("profile", ""),
                        "purpose": current_task.get("purpose", "execute"),
                    }
                )
            elif source and failed:
                await connection_manager.send_or_queue(
                    source,
                    _build_ws_message(
                        "error",
                        _error_detail("Streamed result is too large", "PAYLOAD_TOO_LARGE"),
                        cid,
                    ),
                    ttl=_pending_ttl_seconds(cfg),
                )
            await _send_ws_error(ws, "Streamed result is too large", "PAYLOAD_TOO_LARGE", cid)
            current_task = None
            reset_stream()
            return False
        streamed_result_parts.append(content)
        streamed_result_bytes += encoded_size
        source = str(current_task.get("source_agent") or "")
        if current_task.get("source_kind") == "web":
            await task_events.publish(
                {
                    "event": "task_chunk",
                    "task_id": cid,
                    "group_id": current_task.get("group_id", ""),
                    "status": current_task.get("status", "EXECUTING"),
                    "target": current_task.get("target_agent", agent_name),
                    "profile": current_task.get("profile", ""),
                    "purpose": current_task.get("purpose", "execute"),
                    "text": content,
                }
            )
        elif source:
            await connection_manager.send_if_online(
                source,
                _build_ws_message("task_chunk", {"task_id": cid, "text": content}, cid),
            )
        return True

    async def consume_stream_text(cid: str, text_value: str) -> bool:
        """Consume one marker-aware fragment and complete the task when terminated."""

        nonlocal current_task
        content, found = scanner.scan(sanitize_untrusted_text(text_value))
        if not await append_stream_content(cid, content):
            return True
        if not found:
            return False
        if not current_task:
            reset_stream()
            return True
        source = str(current_task.get("source_agent") or "")
        result = "".join(streamed_result_parts)
        await _complete_task_and_deliver(
            cfg,
            cid,
            source,
            {"result": result, "task_id": cid},
            result,
        )
        current_task = None
        reset_stream()
        return True

    try:
        while True:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=cfg.server.ws_receive_timeout)
            now = time.monotonic()
            cutoff = now - cfg.server.ws_rate_limit_window
            while message_times and message_times[0] <= cutoff:
                message_times.popleft()
            if len(message_times) >= cfg.server.ws_rate_limit_max:
                await ws.close(code=1013, reason="WebSocket rate limit exceeded")
                return
            message_times.append(now)
            if len(raw.encode("utf-8")) > cfg.server.max_body_bytes:
                await ws.close(code=1009, reason="Message too large")
                return

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                if not current_task:
                    continue
                cid = str(current_task.get("id") or "")
                if cid:
                    await consume_stream_text(cid, raw)
                continue

            if not isinstance(data, dict):
                await _send_ws_error(ws, "WebSocket message must be a JSON object", "INVALID_REQUEST")
                continue

            msg_type = data.get("type", "")
            payload = data.get("payload", {})
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                await _send_ws_error(ws, "WebSocket payload must be an object", "INVALID_REQUEST")
                continue

            supplied_correlation_id = data.get("correlation_id")
            if supplied_correlation_id not in (None, "") and not _is_valid_correlation_id(supplied_correlation_id):
                await _send_ws_error(ws, "Invalid correlation_id", "INVALID_CORRELATION_ID")
                continue

            if registered:
                await connection_manager.touch(agent_name, ws)

            if msg_type == "pong":
                continue

            if msg_type == "hello":
                await ws.send_json(
                    _build_ws_message("hello", _server_protocol_metadata(cfg), data.get("correlation_id"))
                )
                continue

            if msg_type != "register" and not registered:
                await _send_ws_error(ws, "Register before sending WebSocket commands", "NOT_REGISTERED")
                continue

            if msg_type == "register":
                if registered:
                    await _send_ws_error(ws, "WebSocket connection is already registered", "ALREADY_REGISTERED")
                    continue
                requested_agent_name = payload.get("agent_name", "unknown")
                # 名称校验：1-64 字符，仅字母数字下划线和连字符
                if not _is_valid_agent_name(requested_agent_name):
                    await _send_ws_error(
                        ws,
                        "Invalid agent name. Use 1-64 chars: a-z, A-Z, 0-9, _ -",
                        "INVALID_NAME",
                    )
                    continue
                if requested_agent_name in cfg.agents:
                    await _send_ws_error(
                        ws,
                        "Agent name is reserved by a configured HTTP adapter",
                        "NAME_CONFLICT",
                    )
                    await ws.close(code=4002, reason="Agent name is reserved")
                    return
                protocol_version = parse_ws_protocol_version(payload.get("protocol_version"))
                if not is_supported_ws_protocol(protocol_version):
                    await _send_ws_error(
                        ws,
                        (
                            "Unsupported WebSocket protocol version. "
                            f"Supported range: {MIN_SUPPORTED_WS_PROTOCOL_VERSION}-{WS_PROTOCOL_VERSION}"
                        ),
                        "UNSUPPORTED_PROTOCOL",
                    )
                    await ws.close(code=4003, reason="Unsupported protocol version")
                    return
                client_meta = payload.get("client") if isinstance(payload.get("client"), dict) else {}
                capabilities = (
                    client_meta.get("capabilities") if isinstance(client_meta.get("capabilities"), list) else []
                )
                agent_name = requested_agent_name
                connection_id = f"{agent_name}:{session_id[:8]}"
                try:
                    await connection_manager.register(
                        agent_name,
                        ws,
                        connection_id,
                        protocol_version=protocol_version or WS_PROTOCOL_VERSION,
                        client=client_meta,
                        capabilities=capabilities,
                        deliver_pending=False,
                    )
                except RuntimeError:
                    agent_name = "unknown"
                    return
                registered = True
                await ws.send_json(
                    _build_ws_message(
                        "registered",
                        {
                            "agent_name": agent_name,
                            "session_id": session_id,
                            "connection_id": connection_id,
                            "pending_available": connection_manager.pending_count(agent_name),
                            "protocol_version": WS_PROTOCOL_VERSION,
                            "accepted_protocol_version": protocol_version or WS_PROTOCOL_VERSION,
                            "server_version": __version__,
                            "api_version": API_VERSION,
                            "api_prefix": API_PREFIX,
                            "max_body_bytes": cfg.server.max_body_bytes,
                            "banner": format_banner("ws connected"),
                        },
                    )
                )
                pending = await connection_manager.deliver_pending(agent_name, ws)
                await _mark_redelivered_tasks_dispatched(cfg, pending)
                synapse_logger.info(
                    "agent registered",
                    extra={
                        "event": "agent_registered",
                        "agent": agent_name,
                        "connection_id": connection_id,
                        "delivered": len(pending),
                    },
                )
                continue

            if msg_type == "probe_ack":
                # Late acknowledgements are harmless and may arrive after the HTTP probe timeout.
                probe_coordinator.record(agent_name, payload)
                continue

            # ── /send 路由 ──
            if msg_type == "send":
                requested_target = payload.get("target", "")
                plan = payload.get("plan", "")
                timeout = payload.get("timeout", 60)
                cid = data.get("correlation_id") or generate_correlation_id()

                if not isinstance(requested_target, str):
                    await _send_ws_error(ws, "'target' must be a string", "INVALID_REQUEST", cid)
                    continue
                if not plan or not isinstance(plan, str):
                    await _send_ws_error(ws, "Missing 'plan' in payload", "INVALID_REQUEST", cid)
                    continue
                if len(plan.encode("utf-8")) > cfg.server.max_body_bytes:
                    await _send_ws_error(ws, "Plan is too large", "PAYLOAD_TOO_LARGE", cid)
                    continue
                target = select_target(cfg.router, requested_target, plan)
                if not target:
                    await _send_ws_error(
                        ws,
                        "No target supplied and no router rule/default matched",
                        "ROUTING_NOT_FOUND",
                        cid,
                    )
                    continue
                if not _is_valid_agent_name(target):
                    await _send_ws_error(ws, "Invalid target agent name", "INVALID_NAME", cid)
                    continue
                persona, persona_error = _task_persona(cfg, payload)
                if persona_error:
                    await _send_ws_error(ws, persona_error, "INVALID_REQUEST", cid)
                    continue
                timeout = _bounded_timeout(timeout)
                forwarded_options, forward_error = _copy_forwarded_task_options(payload, cfg.server.max_body_bytes)
                if forward_error:
                    await _send_ws_error(ws, forward_error, "INVALID_REQUEST", cid)
                    continue
                overrides, override_error = _copy_agent_overrides(payload)
                if override_error:
                    await _send_ws_error(ws, override_error, "INVALID_REQUEST", cid)
                    continue

                agent_cfg = cfg.agents.get(target)
                if agent_cfg:
                    try:
                        await handle_http_send(
                            cfg,
                            ws,
                            agent_name,
                            target,
                            plan,
                            timeout,
                            cid,
                            agent_cfg,
                            {**payload, **overrides},
                            persona=persona,
                        )
                    except TaskAlreadyExistsError:
                        await _send_ws_error(ws, "Task correlation_id already exists", "DUPLICATE_TASK_ID", cid)
                    continue

                resolution = await resolve_target(target)
                if not resolution["online"]:
                    await _send_ws_error(ws, resolution["error"], "ROUTING_NOT_FOUND", cid)
                    continue

                try:
                    task_id = await create_task(
                        cfg.db_path,
                        agent_name,
                        target,
                        plan,
                        timeout=timeout,
                        persona=persona,
                        correlation_id=cid,
                        profile=forwarded_options.get("profile") or forwarded_options.get("tool", ""),
                        session_alias=(
                            forwarded_options.get("session_id")
                            or forwarded_options.get("session")
                            or forwarded_options.get("ssid", "")
                        ),
                    )
                except TaskAlreadyExistsError:
                    await _send_ws_error(ws, "Task correlation_id already exists", "DUPLICATE_TASK_ID", cid)
                    continue
                await update_task_status(cfg.db_path, task_id, "QUEUED")

                task_payload = {
                    "task_id": task_id,
                    "plan": plan,
                    "from": agent_name,
                    "timeout": timeout,
                }
                if persona:
                    task_payload["persona"] = persona
                task_payload.update(forwarded_options)
                if overrides:
                    task_payload["overrides"] = overrides

                watcher = asyncio.create_task(
                    _watch_task_timeout(cfg, task_id, timeout, agent_name, target, cid),
                    name=f"task-timeout-{task_id}",
                )
                _timeout_tasks[task_id] = (agent_name, watcher)
                delivered = await connection_manager.send_or_queue(
                    target,
                    _build_ws_message("task", task_payload, cid),
                    ttl=_pending_ttl_seconds(cfg),
                )
                if delivered:
                    await update_task_status(
                        cfg.db_path,
                        task_id,
                        "DISPATCHED",
                        expected_statuses=("QUEUED",),
                    )
                synapse_logger.info(
                    "agent task dispatched",
                    extra={
                        "event": "agent_task_dispatched",
                        "source": agent_name,
                        "target": target,
                        "task_id": task_id,
                        "timeout": timeout,
                        "queued": not delivered,
                    },
                )

                await ws.send_json(
                    _build_ws_message(
                        "task_queued",
                        {"task_id": task_id, "target": target},
                        cid,
                    )
                )
                continue

            # ── broadcast 路由 ──
            if msg_type == "broadcast":
                if "data" not in payload:
                    await _send_ws_error(ws, "Missing 'data' in broadcast payload", "INVALID_REQUEST")
                    continue
                data_to_broadcast = payload["data"]
                targets = await connection_manager.broadcast(
                    _build_ws_message("broadcast", {"from": agent_name, "data": data_to_broadcast})
                )
                await ws.send_json(_build_ws_message("broadcast_ack", {"sent": len(targets), "targets": targets}))
                continue

            # ── AgentB 确认接收 ──
            if msg_type == "accept":
                cid = data.get("correlation_id", "")
                task = await get_task(cfg.db_path, cid) if cid else None
                if not task or task.get("target_agent") != agent_name:
                    await _send_ws_error(ws, "Task is not assigned to this agent", "TASK_NOT_OWNED", cid)
                    continue
                if task.get("status") in TERMINAL_TASK_STATUSES:
                    await _send_ws_error(ws, "Task is already terminal", "INVALID_TASK_STATE", cid)
                    continue
                if current_task and current_task.get("id") != cid:
                    await _send_ws_error(ws, "Agent already has an active task", "AGENT_BUSY", cid)
                    continue
                accepted = await update_task_status(
                    cfg.db_path,
                    cid,
                    "EXECUTING",
                    expected_statuses=("QUEUED", "DISPATCHED"),
                )
                if not accepted:
                    await _send_ws_error(ws, "Task state changed before accept", "INVALID_TASK_STATE", cid)
                    continue
                current_task = task
                current_task["status"] = "EXECUTING"
                reset_stream()
                if task.get("source_kind") == "web":
                    await task_events.publish(
                        {
                            "event": "task_executing",
                            "task_id": cid,
                            "group_id": task.get("group_id", ""),
                            "status": "EXECUTING",
                            "target": agent_name,
                            "profile": task.get("profile", ""),
                            "purpose": task.get("purpose", "execute"),
                        }
                    )
                synapse_logger.info(
                    "agent task accepted",
                    extra={"event": "agent_task_accepted", "agent": agent_name, "task_id": cid},
                )
                continue

            # ── 流式 chunk ──
            if msg_type == "chunk":
                cid = data.get("correlation_id", "") or str((current_task or {}).get("id") or "")
                if not cid or not _is_valid_correlation_id(cid):
                    await _send_ws_error(ws, "Missing or invalid task correlation_id", "INVALID_CORRELATION_ID")
                    continue
                task = await get_task(cfg.db_path, cid)
                if not task or task.get("target_agent") != agent_name:
                    await _send_ws_error(ws, "Task is not assigned to this agent", "TASK_NOT_OWNED", cid)
                    continue
                if task.get("status") in TERMINAL_TASK_STATUSES:
                    continue
                if current_task and current_task.get("id") != cid:
                    await _send_ws_error(ws, "Chunk does not belong to the active task", "AGENT_BUSY", cid)
                    continue
                if not current_task:
                    current_task = task
                    reset_stream()
                chunk_text = payload.get("text", "")
                if not isinstance(chunk_text, str):
                    await _send_ws_error(ws, "Chunk text must be a string", "INVALID_REQUEST", cid)
                    continue
                await consume_stream_text(cid, chunk_text)
                continue

            # ── /return 路由 ──
            if msg_type == "return":
                cid = data.get("correlation_id", "") or str(payload.get("task_id", ""))
                if not _is_valid_correlation_id(cid):
                    await _send_ws_error(ws, "Missing or invalid task correlation_id", "INVALID_CORRELATION_ID")
                    continue
                task = await get_task(cfg.db_path, cid)
                if not task or task.get("target_agent") != agent_name:
                    await _send_ws_error(ws, "Task is not assigned to this agent", "TASK_NOT_OWNED", cid)
                    continue
                if task.get("status") in TERMINAL_TASK_STATUSES:
                    await _send_ws_error(ws, "Task is already terminal", "INVALID_TASK_STATE", cid)
                    continue
                if current_task and current_task.get("id") != cid:
                    await _send_ws_error(ws, "Return does not belong to the active task", "AGENT_BUSY", cid)
                    continue
                if current_task:
                    pending_text = scanner.flush()
                    if not await append_stream_content(cid, pending_text):
                        continue
                source = str(task.get("source_agent") or "")
                result_payload = _sanitize_result_payload(payload, cid)
                if "result" not in result_payload and "output" not in result_payload and streamed_result_parts:
                    result_payload["result"] = "".join(streamed_result_parts)
                result_message = _build_ws_message("task_result", result_payload, cid)
                if _json_message_size(result_message) > cfg.server.max_body_bytes:
                    failed = await update_task_status(
                        cfg.db_path,
                        cid,
                        "ERROR",
                        result="Task result exceeded the configured size limit",
                        expected_statuses=COMPLETABLE_TASK_STATUSES,
                    )
                    await _cancel_timeout_watcher(cid)
                    if failed and task.get("source_kind") == "web":
                        await task_events.publish(
                            {
                                "event": "task_error",
                                "task_id": cid,
                                "group_id": task.get("group_id", ""),
                                "status": "ERROR",
                                "target": task.get("target_agent", agent_name),
                                "profile": task.get("profile", ""),
                                "purpose": task.get("purpose", "execute"),
                            }
                        )
                    elif source and failed:
                        await connection_manager.send_or_queue(
                            source,
                            _build_ws_message(
                                "error",
                                _error_detail("Task result is too large", "PAYLOAD_TOO_LARGE"),
                                cid,
                            ),
                            ttl=_pending_ttl_seconds(cfg),
                        )
                    await _send_ws_error(ws, "Task result is too large", "PAYLOAD_TOO_LARGE", cid)
                    if current_task:
                        current_task = None
                        reset_stream()
                    continue
                memory_value = result_payload.get("result", result_payload.get("output", ""))
                if not isinstance(memory_value, str):
                    memory_value = json.dumps(memory_value, ensure_ascii=False, default=str)
                synapse_logger.info(
                    "agent task returned",
                    extra={
                        "event": "agent_task_returned",
                        "agent": agent_name,
                        "source": source,
                        "task_id": cid,
                        "profile": result_payload.get("profile"),
                        "exit_code": result_payload.get("exit_code"),
                        "output_truncated": bool(result_payload.get("output_truncated")),
                        "stderr_truncated": bool(result_payload.get("stderr_truncated")),
                    },
                )
                await _complete_task_and_deliver(cfg, cid, source, result_payload, memory_value)
                if current_task and current_task.get("id") == cid:
                    current_task = None
                    reset_stream()
                continue

            await _send_ws_error(ws, f"Unknown WebSocket message type: {msg_type}", "UNKNOWN_MESSAGE_TYPE")

    except (TimeoutError, WebSocketDisconnect):
        pass
    finally:
        # Source agent disconnects should not abandon outbound tasks; results can be queued for reconnect.
        if registered:
            await _fail_tasks_targeting_disconnected_agent(cfg, agent_name)
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        if registered:
            await connection_manager.unregister(agent_name, ws)


@app.websocket(f"{API_PREFIX}/ws")
async def ws_endpoint_v1(ws: WebSocket):
    await ws_endpoint(ws)


# ── Web 管理页面静态文件 ──────────────────────

_synapse_dir = Path(__file__).resolve().parent
_web_dir = _synapse_dir / "web"
if _web_dir.is_dir():
    app.mount("/web", StaticFiles(directory=str(_web_dir), html=True), name="web")


@app.get("/")
async def root():
    return RedirectResponse(url="/web/index.html")


@app.get("/admin")
async def admin_panel():
    return RedirectResponse(url="/web/index.html")


# ── 查询端点 ──────────────────────────────────


@app.get("/context")
async def get_context(
    request: Request,
    persona: str = Query(default="", max_length=128),
    limit: int = Query(default=20, ge=1, le=100),
    _token: str = Depends(verify_context_read),
):
    cfg = request.app.state.config
    db = cfg.db_path
    persona = _scoped_persona(cfg.memory, persona)
    memories = await get_recent_memories(db, persona, limit)
    knowledge = await list_knowledge(db, persona, limit)
    return {
        "memories": [{"id": r["id"], "content": r["content"], "created_at": r["created_at"]} for r in memories],
        "skills": await list_skills(db),
        "knowledge": [
            {"id": r["id"], "title": r["title"], "content": r["content"], "created_at": r["created_at"]}
            for r in knowledge
        ],
        "personas": await list_personas(db),
        "prompts": await list_prompts(db),
        "agents": _available_agent_details(cfg),
        "default_skills": _build_default_skills(cfg),
    }


@app.get("/context/agents")
async def ctx_list_agents(
    request: Request,
    _token: str = Depends(verify_context_read),
):
    return _available_agent_details(request.app.state.config)


@app.get("/context/skills")
async def ctx_list_skills(
    request: Request,
    name: str = Query(default="", max_length=128),
    _token: str = Depends(verify_context_read),
    detail: bool = Query(default=False),
):
    db = request.app.state.config.db_path
    if name:
        s = await get_skill(db, name)
        if not s:
            raise HTTPException(status_code=404, detail=_error_detail("Skill not found", "NOT_FOUND"))
        return s
    if detail:
        return await list_skills_detailed(db)
    return await list_skills(db)


@app.get("/context/personas")
async def ctx_list_personas(
    request: Request,
    name: str = Query(default="", max_length=128),
    _token: str = Depends(verify_context_read),
    detail: bool = Query(default=False),
):
    db = request.app.state.config.db_path
    if name:
        p = await ctx_get_persona(db, name)
        if not p:
            raise HTTPException(status_code=404, detail=_error_detail("Persona not found", "NOT_FOUND"))
        return p
    if detail:
        return await list_personas_detailed(db)
    return await list_personas(db)


@app.get("/context/prompts")
async def ctx_list_prompts(
    request: Request,
    name: str = Query(default="", max_length=128),
    _token: str = Depends(verify_context_read),
    detail: bool = Query(default=False),
):
    db = request.app.state.config.db_path
    if name:
        prompt = await get_prompt(db, name)
        if not prompt:
            raise HTTPException(status_code=404, detail=_error_detail("Prompt not found", "NOT_FOUND"))
        return prompt
    if detail:
        return await list_prompts_detailed(db)
    return await list_prompts(db)


@app.post("/context/memory")
async def ctx_search_memory_endpoint(
    body: SearchRequest,
    request: Request,
    _token: str = Depends(verify_token),
):
    cfg = request.app.state.config
    db = cfg.db_path
    # 搜索时也匹配写入时的 persona 覆盖逻辑
    persona = _scoped_persona(cfg.memory, body.persona)
    return await ctx_search_memory(db, body.query, persona, body.limit, cfg.memory)


@app.post("/context/knowledge")
async def ctx_search_knowledge_endpoint(
    body: SearchRequest,
    request: Request,
    _token: str = Depends(verify_token),
):
    cfg = request.app.state.config
    db = cfg.db_path
    persona = _scoped_persona(cfg.memory, body.persona)
    return await ctx_search_knowledge(db, body.query, persona, body.limit, cfg.memory)


# ── 管理端点（写入） ──────────────────────────


@app.post("/admin/memory")
async def admin_add_memory(
    body: WriteContentRequest,
    request: Request,
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    mc = request.app.state.config.memory
    # 根据 memory.scope 强制覆盖 persona
    persona = _scoped_persona(mc, body.persona or "")
    mid = await add_memory(db, await body.get_content(), persona, memory_config=mc)
    return {"id": mid, "status": "created"}


@app.delete("/admin/memory")
async def admin_delete_memory(
    request: Request,
    id: int = Query(..., ge=1),
    persona: str = Query(default="", max_length=128),
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    mc = request.app.state.config.memory
    persona = _scoped_persona(mc, persona)
    ok = await delete_memory(db, id, persona)
    if not ok:
        raise HTTPException(status_code=404, detail=_error_detail("Memory not found", "NOT_FOUND"))
    return {"id": id, "status": "deleted"}


@app.post("/admin/skill")
async def admin_add_skill(
    body: SkillWriteRequest,
    request: Request,
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    sid = await add_skill(db, body.name, await body.get_content())
    return {"id": sid, "status": "created"}


@app.delete("/admin/skill")
async def admin_delete_skill(
    request: Request,
    name: str = Query(..., min_length=1, max_length=128),
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    ok = await delete_skill(db, name)
    if not ok:
        raise HTTPException(status_code=404, detail=_error_detail("Skill not found", "NOT_FOUND"))
    return {"name": name, "status": "deleted"}


@app.post("/admin/persona")
async def admin_set_persona(
    body: PersonaWriteRequest,
    request: Request,
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    pid = await set_persona(db, body.name, await body.get_content())
    return {"id": pid, "status": "created"}


@app.delete("/admin/persona")
async def admin_delete_persona(
    request: Request,
    name: str = Query(..., min_length=1, max_length=128),
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    ok = await delete_persona(db, name)
    if not ok:
        raise HTTPException(status_code=404, detail=_error_detail("Persona not found", "NOT_FOUND"))
    return {"name": name, "status": "deleted"}


@app.post("/admin/prompt")
async def admin_set_prompt(
    body: PromptWriteRequest,
    request: Request,
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    prompt_id = await set_prompt(db, body.name, await body.get_content())
    return {"id": prompt_id, "status": "created"}


@app.delete("/admin/prompt")
async def admin_delete_prompt(
    request: Request,
    name: str = Query(..., min_length=1, max_length=128),
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    ok = await delete_prompt(db, name)
    if not ok:
        raise HTTPException(status_code=404, detail=_error_detail("Prompt not found", "NOT_FOUND"))
    return {"name": name, "status": "deleted"}


@app.post("/admin/knowledge")
async def admin_add_knowledge(
    body: KnowledgeWriteRequest,
    request: Request,
    chunk: bool = Query(default=False),
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    mc = request.app.state.config.memory
    persona = _scoped_persona(mc, body.persona or "")
    kids = await add_knowledge(db, body.title, await body.get_content(), persona, chunk=chunk, memory_config=mc)
    return {"ids": kids, "status": "created"}


@app.delete("/admin/knowledge")
async def admin_delete_knowledge(
    request: Request,
    id: int = Query(..., ge=1),
    persona: str = Query(default="", max_length=128),
    _token: str = Depends(verify_token),
):
    db = request.app.state.config.db_path
    persona = _scoped_persona(request.app.state.config.memory, persona)
    ok = await delete_knowledge(db, id, persona)
    if not ok:
        raise HTTPException(status_code=404, detail=_error_detail("Knowledge not found", "NOT_FOUND"))
    return {"id": id, "status": "deleted"}


@app.get("/admin/config")
async def admin_get_config(request: Request, _token: str = Depends(verify_token)):
    cfg = request.app.state.config
    return {
        "config": {
            "server": {
                "host": cfg.server.host,
                "port": cfg.server.port,
                "api_key": bool(cfg.server.api_key.get_secret_value()),
                "worker_api_key": bool(cfg.server.worker_api_key.get_secret_value()),
                "cors_origins": cfg.server.get_cors_origins(),
                "behind_proxy": cfg.server.behind_proxy,
                "public_read_context": cfg.server.public_read_context,
                "outbound_trust_env": cfg.server.outbound_trust_env,
                "rate_limit_max": cfg.server.rate_limit_max,
                "rate_limit_window": cfg.server.rate_limit_window,
                "ws_rate_limit_max": cfg.server.ws_rate_limit_max,
                "ws_rate_limit_window": cfg.server.ws_rate_limit_window,
                "max_body_bytes": cfg.server.max_body_bytes,
                "ws_receive_timeout": cfg.server.ws_receive_timeout,
                "ws_ping_interval": cfg.server.ws_ping_interval,
                "pending_message_ttl_hours": cfg.server.pending_message_ttl_hours,
                "task_history_hours": cfg.server.task_history_hours,
                "auto_memory_threshold": cfg.server.auto_memory_threshold,
                "auto_memory_max_chars": cfg.server.auto_memory_max_chars,
            },
            "memory": {
                "scope": cfg.memory.scope,
                "embedding_provider": cfg.memory.embedding_provider,
                "embedding_model": cfg.memory.embedding_model,
                "embedding_api_url": cfg.memory.embedding_api_url,
                "embedding_api_key": bool(cfg.memory.embedding_api_key.get_secret_value()),
                "embedding_dimensions": cfg.memory.embedding_dimensions,
                "embedding_timeout": cfg.memory.embedding_timeout,
                "embedding_trust_env": cfg.memory.embedding_trust_env,
            },
        }
    }


@app.post("/admin/config")
async def admin_update_config(
    body: ConfigUpdateRequest,
    request: Request,
    _token: str = Depends(verify_token),
):
    cfg = request.app.state.config
    if body.memory is None:
        return {"status": "no changes"}

    tmp_memory = body.memory.model_dump(exclude_none=True)
    if tmp_memory:
        candidate = cfg.memory.model_dump()
        candidate["embedding_api_key"] = cfg.memory.embedding_api_key
        candidate.update(tmp_memory)
        try:
            new_memory = MemoryConfig.model_validate(candidate)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=_error_detail(
                    "Invalid memory configuration",
                    "VALIDATION_ERROR",
                ),
            ) from exc

        async with _config_write_lock:
            saved_memory = cfg.memory
            cfg.memory = new_memory
            try:
                await _save_config_unlocked(request, cfg)
            except ConfigSaveError as e:
                cfg.memory = saved_memory
                raise HTTPException(status_code=400, detail=_error_detail(str(e), e.code)) from e
            except Exception:
                cfg.memory = saved_memory
                raise
    return {"status": "saved", "note": "Changes written to config.yaml"}


def _register_api_v1_aliases() -> None:
    aliases = [
        ("/health", health, ["GET"]),
        ("/version", version_endpoint, ["GET"]),
        ("/_debug/memory_config", debug_memory_config, ["GET"]),
        ("/admin/logs", admin_logs, ["GET"]),
        ("/admin/logs/stream", admin_log_stream, ["GET"]),
        ("/install/{agent_type}", install_endpoint, ["GET"]),
        ("/connection-prompt", connection_prompt_endpoint, ["GET"]),
        ("/connection-prompt", connection_prompt_post_endpoint, ["POST"]),
        ("/context", get_context, ["GET"]),
        ("/context/agents", ctx_list_agents, ["GET"]),
        ("/context/skills", ctx_list_skills, ["GET"]),
        ("/context/personas", ctx_list_personas, ["GET"]),
        ("/context/prompts", ctx_list_prompts, ["GET"]),
        ("/context/memory", ctx_search_memory_endpoint, ["POST"]),
        ("/context/knowledge", ctx_search_knowledge_endpoint, ["POST"]),
        ("/admin/memory", admin_add_memory, ["POST"]),
        ("/admin/memory", admin_delete_memory, ["DELETE"]),
        ("/admin/skill", admin_add_skill, ["POST"]),
        ("/admin/skill", admin_delete_skill, ["DELETE"]),
        ("/admin/persona", admin_set_persona, ["POST"]),
        ("/admin/persona", admin_delete_persona, ["DELETE"]),
        ("/admin/prompt", admin_set_prompt, ["POST"]),
        ("/admin/prompt", admin_delete_prompt, ["DELETE"]),
        ("/admin/knowledge", admin_add_knowledge, ["POST"]),
        ("/admin/knowledge", admin_delete_knowledge, ["DELETE"]),
        ("/admin/config", admin_get_config, ["GET"]),
        ("/admin/config", admin_update_config, ["POST"]),
    ]
    for path, endpoint, methods in aliases:
        app.add_api_route(f"{API_PREFIX}{path}", endpoint, methods=methods, include_in_schema=True)


_register_api_v1_aliases()

_web_task_controller = WebTaskController(
    task_persona=_task_persona,
    bounded_timeout=_bounded_timeout,
    build_ws_message=_build_ws_message,
    error_detail=_error_detail,
    pending_ttl_seconds=_pending_ttl_seconds,
    watch_task_timeout=_watch_task_timeout,
    cancel_timeout_watcher=_cancel_timeout_watcher,
    available_agent_details=_available_agent_details,
    timeout_tasks=_timeout_tasks,
    http_tasks=_web_http_tasks,
)
_task_admin_router = create_task_router(
    verify_token,
    dispatch_task=_web_task_controller.dispatch_task,
    cancel_task=_web_task_controller.cancel_task,
    probe_agents=_web_task_controller.probe_agents,
    resolve_endpoint=_web_task_controller.resolve_endpoint,
    agent_details=_web_task_controller.agent_details,
)
app.include_router(_task_admin_router)
app.include_router(_task_admin_router, prefix=API_PREFIX)


# ── 服务器启动 ────────────────────────────────


async def run_server(config: GlobalConfig, config_path: str = "") -> None:
    import uvicorn

    app.state.config = config
    app.state.config_path = config_path
    _setup_cors(config)
    srv = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.server.host,
            port=config.server.port,
            log_config=None,
            access_log=False,
            server_header=False,
            proxy_headers=config.server.behind_proxy,
            forwarded_allow_ips=",".join(config.server.trusted_proxy_hosts),
            ws_max_size=config.server.max_body_bytes,
            ws_ping_interval=float(config.server.ws_ping_interval),
            ws_ping_timeout=float(config.server.ws_receive_timeout),
            limit_max_requests=None,
        )
    )
    await srv.serve()
