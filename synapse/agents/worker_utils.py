"""Shared helpers for standalone WebSocket workers."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import json
import os
import re
import signal
import stat
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from synapse import __version__
from synapse.file_utils import ensure_private_directory
from synapse.protocol import WS_PROTOCOL_VERSION
from synapse.text_utils import sanitize_untrusted_text as sanitize_process_text

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is best-effort only.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX.
    msvcrt = None

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BLOCKED_CHILD_ENV_PREFIXES = ("SYNAPTIC_",)
_BLOCKED_CHILD_ENV_NAMES = {
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
}
_MAX_REGISTRATION_ACK_BYTES = 64_000
WORKER_WS_MAX_MESSAGE_BYTES = 17 * 1024 * 1024
WORKER_WS_MAX_QUEUE = 4
WORKER_MAX_TIMEOUT_SECONDS = 3600
WORKER_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_MAX_REGISTRATION_BANNER_CHARS = 8_000
DEFAULT_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "CODEX_HOME",
    "CODEX_CA_CERTIFICATE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)


def worker_registration_payload(
    agent_name: str,
    *,
    worker_kind: str,
    capabilities: Iterable[str] = (),
    extra_client_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable WebSocket register payload for standalone workers."""
    client: dict[str, Any] = {
        "name": worker_kind,
        "version": __version__,
        "capabilities": sorted({str(item) for item in capabilities if str(item)}),
    }
    if extra_client_fields:
        for key, value in extra_client_fields.items():
            if key not in {"name", "version", "capabilities"}:
                client[str(key)] = value
    return {
        "agent_name": agent_name,
        "protocol_version": WS_PROTOCOL_VERSION,
        "client": client,
    }


def validate_websocket_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("WebSocket URL must use ws:// or wss://")
    if parsed.username or parsed.password:
        raise ValueError("WebSocket URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("WebSocket URL must not contain a query or fragment")
    return value


def validate_worker_agent_name(value: str) -> str:
    if not _AGENT_NAME_RE.fullmatch(value):
        raise ValueError("Agent name must contain 1-64 letters, digits, underscores, or hyphens")
    return value


def bounded_task_timeout(value: Any, default: int, *, maximum: int = WORKER_MAX_TIMEOUT_SECONDS) -> int:
    """Accept a positive server timeout and otherwise use a bounded local default."""

    if isinstance(value, bool):
        return min(default, maximum)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return min(default, maximum)
    if timeout <= 0:
        return min(default, maximum)
    return min(timeout, maximum)


def websocket_headers_kwargs(connect_func, headers: dict[str, str]) -> dict[str, dict[str, str]]:
    """Return the right headers kwarg for websockets legacy and 14+ clients."""
    if not headers:
        return {}
    try:
        params = inspect.signature(connect_func).parameters
    except (TypeError, ValueError):
        return {"additional_headers": headers}
    if "additional_headers" in params:
        return {"additional_headers": headers}
    if "extra_headers" in params:
        return {"extra_headers": headers}
    return {"additional_headers": headers}


async def receive_registration_ack(ws, *, timeout: float = 10.0) -> dict[str, Any]:
    """Wait for a register response and ignore heartbeat pings before it."""

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for WebSocket registration acknowledgement")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        if isinstance(raw, bytes):
            if len(raw) > _MAX_REGISTRATION_ACK_BYTES:
                continue
            raw = raw.decode("utf-8", errors="replace")
        elif not isinstance(raw, str):
            continue
        elif len(raw.encode("utf-8", errors="ignore")) > _MAX_REGISTRATION_ACK_BYTES:
            continue
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict):
            continue
        msg_type = message.get("type")
        if msg_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            continue
        if msg_type in {"registered", "error"}:
            return message


def sanitize_terminal_text(value: str, *, limit: int = _MAX_REGISTRATION_BANNER_CHARS) -> str:
    """Remove terminal control characters from text printed by local workers."""

    return sanitize_process_text(value)[:limit]


def print_registration_banner(response: Mapping[str, Any], *, file: TextIO | None = None) -> bool:
    """Print the server-provided terminal banner after WS registration succeeds."""

    payload = response.get("payload")
    if not isinstance(payload, Mapping):
        return False
    banner = payload.get("banner")
    if not isinstance(banner, str) or not banner.strip():
        return False
    print(sanitize_terminal_text(banner), file=file or sys.stdout)
    return True


async def websocket_keepalive(ws, *, interval: float = 15.0) -> None:
    """Send lightweight app-level pongs while a worker is busy.

    Server-side WebSocket handling expects inbound traffic within
    ws_receive_timeout. Long-running child processes can keep the worker from
    reading server pings, so this task keeps the registration alive even when
    the child is quiet.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send(json.dumps({"type": "pong"}))
        except Exception:
            return


def _message_task_id(message: Mapping[str, Any]) -> str:
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    return str(payload.get("task_id") or message.get("correlation_id") or "")


async def run_worker_message_loop(
    ws,
    handle_task: Callable[[dict[str, Any]], Awaitable[None]],
    cancel_active: Callable[[], Awaitable[None]],
    *,
    max_pending_tasks: int = 16,
) -> None:
    """Receive control frames while executing one child task at a time.

    Keeping reception separate from execution makes timeout cancellation and
    disconnect cleanup effective even while a child process is quiet.
    """

    task_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_pending_tasks)
    queued_task_ids: set[str] = set()
    cancelled_task_ids: set[str] = set()
    active_task_id = ""

    async def receive_messages() -> None:
        nonlocal active_task_id
        async for raw in ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict):
                continue

            msg_type = message.get("type", "")
            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue
            if msg_type == "cancel":
                task_id = _message_task_id(message)
                if not task_id or task_id == active_task_id:
                    await cancel_active()
                elif task_id in queued_task_ids:
                    cancelled_task_ids.add(task_id)
                continue
            if msg_type != "task":
                continue
            if task_queue.full():
                raise RuntimeError("Worker task queue is full; reconnecting to fail queued tasks safely")
            task_id = _message_task_id(message)
            if task_id:
                queued_task_ids.add(task_id)
            task_queue.put_nowait(message)

    async def execute_messages() -> None:
        nonlocal active_task_id
        while True:
            message = await task_queue.get()
            task_id = _message_task_id(message)
            queued_task_ids.discard(task_id)
            if task_id and task_id in cancelled_task_ids:
                cancelled_task_ids.discard(task_id)
                task_queue.task_done()
                continue
            active_task_id = task_id
            try:
                await handle_task(message)
            finally:
                active_task_id = ""
                task_queue.task_done()

    receiver = asyncio.create_task(receive_messages())
    executor = asyncio.create_task(execute_messages())
    done, pending = await asyncio.wait((receiver, executor), return_when=asyncio.FIRST_COMPLETED)
    await cancel_active()
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


def validate_child_env_names(names: Iterable[str]) -> list[str]:
    """Validate explicit child env names and block SynapticLathe secrets."""
    result = []
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid environment variable name: {raw}")
        if name.startswith(_BLOCKED_CHILD_ENV_PREFIXES) or name in _BLOCKED_CHILD_ENV_NAMES:
            raise ValueError(f"Refusing to pass reserved environment variable to child process: {name}")
        result.append(name)
    return result


def build_child_env(
    *,
    pass_env: Iterable[str] = (),
    base_env: Mapping[str, str] | None = None,
    default_allowlist: Iterable[str] = DEFAULT_CHILD_ENV_ALLOWLIST,
) -> dict[str, str]:
    """Build a small child-process environment from an allowlist.

    The worker's own SYNAPTIC_* settings include server credentials and routing
    details. They are intentionally never forwarded to child processes.
    """
    source = os.environ if base_env is None else base_env
    names = [*default_allowlist, *validate_child_env_names(pass_env)]
    env = {}
    for name in names:
        if name.startswith(_BLOCKED_CHILD_ENV_PREFIXES) or name in _BLOCKED_CHILD_ENV_NAMES:
            continue
        value = source.get(name)
        if value is not None:
            env[name] = value
    return env


class LimitedByteBuffer:
    """Bounded byte buffer used for subprocess stdout/stderr capture."""

    def __init__(self, limit: int, *, keep_tail: bool = False) -> None:
        self.limit = max(0, limit)
        self.keep_tail = keep_tail
        self._data = bytearray()
        self.truncated = False

    def append(self, data: bytes) -> None:
        if not data:
            return
        if self.limit == 0:
            self.truncated = True
            return
        if self.keep_tail:
            self._data.extend(data)
            if len(self._data) > self.limit:
                del self._data[: len(self._data) - self.limit]
                self.truncated = True
            return

        remaining = self.limit - len(self._data)
        if remaining <= 0:
            self.truncated = True
            return
        self._data.extend(data[:remaining])
        if len(data) > remaining:
            self.truncated = True

    def text(self) -> str:
        return sanitize_process_text(bytes(self._data).decode(errors="replace"))


def default_worker_lock_file(kind: str, url: str, agent_name: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{url}\0{agent_name}".encode()).hexdigest()[:16]
    if hasattr(os, "getuid"):
        owner = str(os.getuid())
    else:
        username = os.environ.get("USERNAME", "user")
        owner = hashlib.sha256(username.encode("utf-8", errors="replace")).hexdigest()[:12]
    lock_dir = Path(tempfile.gettempdir()) / f"synaptic-lathe-{owner}"
    safe_kind = re.sub(r"[^A-Za-z0-9_-]", "_", kind)[:32] or "worker"
    return str(lock_dir / f"{safe_kind}-{digest}.lock")


def child_process_group_kwargs() -> dict[str, Any]:
    """Return platform-appropriate process-group creation arguments."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def child_process_group_id(proc: asyncio.subprocess.Process) -> int | None:
    if os.name == "nt" or not hasattr(os, "getpgid"):
        return None
    try:
        return os.getpgid(proc.pid)
    except ProcessLookupError:
        return None


async def _windows_taskkill_tree(pid: int, *, force: bool) -> None:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    taskkill = str(Path(system_root) / "System32" / "taskkill.exe")
    argv = [taskkill, "/PID", str(pid), "/T"]
    if force:
        argv.append("/F")
    try:
        # Fixed Windows system utility and a numeric PID; no shell is involved.
        killer = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    except (FileNotFoundError, PermissionError):
        pass


async def terminate_child_process(
    proc: asyncio.subprocess.Process | None,
    process_group_id: int | None,
    *,
    grace_seconds: float = 5.0,
) -> None:
    """Terminate a child and its process group without assuming POSIX APIs."""

    if proc is None or proc.returncode is not None:
        return
    if os.name == "nt":
        await _windows_taskkill_tree(proc.pid, force=False)
    elif process_group_id is not None and hasattr(os, "killpg"):
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        with suppress(ProcessLookupError):
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    if os.name == "nt":
        await _windows_taskkill_tree(proc.pid, force=True)
    elif process_group_id is not None and hasattr(os, "killpg"):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        with suppress(ProcessLookupError):
            proc.kill()
    with suppress(Exception):
        await proc.wait()


class SingleInstanceLock:
    """Advisory lock that keeps duplicate local workers from running."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._file: TextIO | None = None

    def acquire(self) -> bool:
        parent = self.path.parent
        try:
            if parent.exists():
                parent_stat = parent.lstat()
                if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
                    raise OSError(f"Unsafe worker lock directory: {parent}")
                if hasattr(os, "getuid") and parent_stat.st_uid != os.getuid():
                    raise OSError(f"Worker lock directory is not owned by this user: {parent}")
                if os.name != "nt" and stat.S_IMODE(parent_stat.st_mode) & 0o022:
                    raise OSError(f"Worker lock directory is writable by another user: {parent}")
            else:
                ensure_private_directory(parent)
        except OSError as exc:
            raise RuntimeError(f"Unsafe worker lock directory: {parent}") from exc
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise RuntimeError(f"Refusing symbolic-link worker lock: {self.path}") from exc
            raise
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(fd)
            raise RuntimeError(f"Worker lock is not a regular file: {self.path}")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            os.close(fd)
            raise RuntimeError(f"Worker lock is not owned by this user: {self.path}")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        lock_file = os.fdopen(fd, "r+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.write("\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - unsupported platforms fail closed.
                raise RuntimeError("No supported file-locking API is available")
        except (BlockingIOError, OSError) as exc:
            lock_file.close()
            lock_conflict = (
                isinstance(exc, BlockingIOError)
                or exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                or getattr(exc, "winerror", None) in {33, 36}
            )
            if lock_conflict:
                return False
            raise
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()}\n")
        lock_file.flush()
        self._file = lock_file
        return True

    def release(self) -> None:
        if self._file is None:
            return
        if fcntl is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            self._file.seek(0)
            with suppress(OSError):
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        self._file.close()
        self._file = None


async def wait_for_instance_lock(lock_file: str, label: str, retry_interval: float) -> SingleInstanceLock:
    warned = False
    while True:
        lock = SingleInstanceLock(lock_file)
        if lock.acquire():
            if warned:
                print(f"{label} acquired worker lock; starting connection loop.", file=sys.stderr)
            return lock
        if not warned:
            print(
                f"{label} is already running locally; waiting silently for lock {lock_file!r}.",
                file=sys.stderr,
            )
            warned = True
        await asyncio.sleep(retry_interval)
