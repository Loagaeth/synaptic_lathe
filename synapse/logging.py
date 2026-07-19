"""结构化日志系统 — JSON 格式，携带 correlation_id。"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

correlation_id_ctx = ContextVar("correlation_id", default="")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.getenv("SYNAPTIC_LOG_DIR", "logs")).expanduser()
LOG_FILE_NAME = Path(os.getenv("SYNAPTIC_LOG_FILE", "synaptic_lathe.log")).name
LOG_TO_STDOUT = os.getenv("SYNAPTIC_LOG_STDOUT", "1").strip().lower() not in {"0", "false", "no", "off"}
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{6,}")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
    r"client[_-]?secret|private[_-]?key|password|secret|token)\b[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s,;\"'&}]+)"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")


def redact_sensitive_text(value: object) -> str:
    """Redact common credentials before a value reaches any log handler."""

    text = str(value)
    text = _URL_CREDENTIAL_RE.sub(r"\1***:***@", text)
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _SK_KEY_RE.sub("sk-***", text)
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}***", text)


class SecureRotatingFileHandler(RotatingFileHandler):
    """Create current and rotated logs with owner-only permissions."""

    def _open(self):
        path = Path(self.baseFilename)
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OSError(f"Log path is not a regular file: {path}")
        if existing is not None and hasattr(os, "getuid") and existing.st_uid != os.getuid():
            raise OSError(f"Log path is not owned by this user: {path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        old_umask = os.umask(0o077)
        try:
            fd = os.open(path, flags, 0o600)
        finally:
            os.umask(old_umask)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


SAFE_EXTRA_KEYS = (
    "type",
    "event",
    "source",
    "target",
    "method",
    "path",
    "status",
    "duration_ms",
    "client_ip",
    "agent",
    "task_id",
    "connection_id",
    "profile",
    "exit_code",
    "timeout",
    "queued",
    "delivered",
    "count",
    "output_truncated",
    "stderr_truncated",
)


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_sensitive_text(record.getMessage()),
            "correlation_id": redact_sensitive_text(correlation_id_ctx.get()),
        }
        for key in SAFE_EXTRA_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = redact_sensitive_text(val) if isinstance(val, str) else val
        if record.exc_info and record.exc_info[1]:
            entry["error"] = redact_sensitive_text(record.exc_info[1])
        return json.dumps(entry, ensure_ascii=False)


def _setup_logger() -> logging.Logger:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=False, mode=0o700)
        created = True
    except FileExistsError:
        created = False
    directory_stat = LOG_DIR.lstat()
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise OSError(f"Unsafe log directory: {LOG_DIR}")
    if hasattr(os, "getuid") and directory_stat.st_uid != os.getuid():
        raise OSError(f"Log directory is not owned by this user: {LOG_DIR}")
    if created:
        os.chmod(LOG_DIR, 0o700)

    log = logging.getLogger("synapse")
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    if log.handlers:
        return log

    fmt = StructuredFormatter()
    handlers: list[logging.Handler] = []
    if LOG_TO_STDOUT:
        handlers.append(logging.StreamHandler(sys.stdout))
    handlers.append(
        SecureRotatingFileHandler(
            LOG_DIR / LOG_FILE_NAME,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    )
    for handler in handlers:
        handler.setFormatter(fmt)
        log.addHandler(handler)
    return log


synapse_logger = _setup_logger()


def log_request(correlation_id: str, msg_type: str, source: str, target: str) -> None:
    """记录一条带 correlation_id 上下文的请求日志。"""
    token = correlation_id_ctx.set(correlation_id)
    try:
        synapse_logger.info("request routed", extra={"type": msg_type, "source": source, "target": target})
    finally:
        correlation_id_ctx.reset(token)
