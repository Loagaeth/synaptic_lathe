"""Server runtime helpers for SynapticLathe CLI entry points."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from synapse.config import GlobalConfig
from synapse.db import init_db
from synapse.file_utils import atomic_write_text
from synapse.logging import synapse_logger
from synapse.server import build_connection_prompt, run_server, set_project_root
from synapse.task_queue import abandon_incomplete_tasks


def _format_config_error(exc: Exception) -> str:
    """Describe config failures without echoing values that may be secrets."""

    if isinstance(exc, ValidationError):
        details = []
        for error in exc.errors(include_url=False, include_context=False, include_input=False)[:5]:
            safe_parts = []
            for part in error.get("loc", ()):
                cleaned = "".join(char if char.isalnum() or char in "_-" else "?" for char in str(part))
                safe_parts.append(cleaned[:128] or "?")
            location = ".".join(safe_parts) or "config"
            details.append(f"{location} ({error.get('type', 'invalid')})")
        suffix = "; ".join(details) if details else "unknown field"
        return f"configuration validation failed: {suffix}"

    mark = getattr(exc, "problem_mark", None)
    if mark is not None and hasattr(mark, "line") and hasattr(mark, "column"):
        return f"YAML syntax error at line {mark.line + 1}, column {mark.column + 1}"
    if isinstance(exc, FileNotFoundError):
        return "configuration file not found"
    if isinstance(exc, PermissionError):
        return "configuration file is not readable"
    return f"configuration could not be parsed ({type(exc).__name__})"


def save_connection_prompt(config: GlobalConfig, config_path: str) -> None:
    prompt = build_connection_prompt(config, "agent", "generic")
    prompt_path = Path(config_path).resolve().parent / "connection_prompt.txt"
    if prompt_path.exists() and prompt_path.read_text(encoding="utf-8") == prompt:
        synapse_logger.debug("connection prompt unchanged, skipping write")
        return
    atomic_write_text(prompt_path, prompt, overwrite=True, mode=0o600)
    synapse_logger.info("connection prompt saved", extra={"type": "startup", "source": str(prompt_path)})


def _secure_config_permissions(config_path: str) -> None:
    path = Path(config_path).expanduser()
    try:
        path_stat = path.lstat()
        owned_by_current_user = not hasattr(os, "getuid") or path_stat.st_uid == os.getuid()
        if not path.is_symlink() and owned_by_current_user:
            path.chmod(0o600)
        elif path.is_symlink():
            synapse_logger.warning("config file is a symlink; live API updates will be refused")
        else:
            synapse_logger.warning("config file is not owned by the current user; permissions were not changed")
    except OSError as exc:
        synapse_logger.warning("could not enforce config file permissions: %s", exc)


async def main(config_path: str = "config.yaml") -> None:
    try:
        config = GlobalConfig.load(config_path)
    except Exception as exc:
        print(f"Failed to load config: {_format_config_error(exc)}")
        print(f"Check {config_path} for errors or copy config.example.yaml as a starting point.")
        sys.exit(1)
    synapse_logger.info("config loaded", extra={"type": "startup"})
    _secure_config_permissions(config_path)
    set_project_root(config_path)

    admin_key = config.server.api_key.get_secret_value()
    worker_key = config.server.get_worker_api_key()
    local_bind = config.server.host in ("127.0.0.1", "localhost", "::1")
    if not admin_key:
        if local_bind:
            synapse_logger.warning("No admin API key configured; local admin endpoints are unprotected")
        else:
            synapse_logger.warning("No admin API key configured; remote admin requests will be rejected")
    if not worker_key:
        if local_bind:
            synapse_logger.warning("No worker API key configured; local WebSocket registration is unprotected")
        else:
            synapse_logger.warning("No worker API key configured; remote WebSocket registration will be rejected")
    if (admin_key or worker_key) and not local_bind:
        synapse_logger.warning(
            "Credentials may traverse a non-local connection. Terminate TLS at a trusted reverse proxy "
            "and expose only the TLS endpoint."
        )
    if config.server.public_read_context:
        synapse_logger.warning(
            "public_read_context is enabled — /context read endpoints may expose memories, "
            "knowledge, skills, personas, prompts, and agent status without authentication."
        )

    save_connection_prompt(config, config_path)

    await init_db(config.db_path)
    abandoned = await abandon_incomplete_tasks(config.db_path)
    synapse_logger.info("database initialized", extra={"type": "startup", "source": config.db_path})
    if abandoned:
        synapse_logger.warning(
            "marked %d incomplete tasks from a previous process as abandoned",
            abandoned,
            extra={"event": "startup_tasks_abandoned"},
        )

    synapse_logger.info(
        "server starting",
        extra={"type": "startup", "target": f"{config.server.host}:{config.server.port}"},
    )
    try:
        await run_server(config, config_path)
    finally:
        synapse_logger.info("server shutting down", extra={"type": "shutdown"})


def run(config_path: str = "config.yaml") -> None:
    asyncio.run(main(config_path))
