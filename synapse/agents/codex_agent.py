"""Codex Worker.

This module runs as an independent SynapticLathe WebSocket client. It registers
as an agent, receives tasks, and executes them through ``codex exec``.

Recommended usage:
    SYNAPTIC_API_KEY=<worker-api-key> synaptic-codex-worker \
      --url ws://127.0.0.1:9112/ws \
      --name codex-local \
      --workdir /path/to/repo \
      --sandbox workspace-write

Compatibility usage:
    python -m synapse.agents.codex_agent --url ws://127.0.0.1:9112/ws --workdir /path/to/repo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from synapse.agents.base import BaseAgent
from synapse.agents.worker_utils import (
    WORKER_MAX_OUTPUT_BYTES,
    WORKER_MAX_TIMEOUT_SECONDS,
    WORKER_WS_MAX_MESSAGE_BYTES,
    WORKER_WS_MAX_QUEUE,
    LimitedByteBuffer,
    bounded_task_timeout,
    build_child_env,
    child_process_group_id,
    child_process_group_kwargs,
    default_worker_lock_file,
    print_registration_banner,
    receive_registration_ack,
    run_worker_message_loop,
    terminate_child_process,
    validate_child_env_names,
    validate_websocket_url,
    validate_worker_agent_name,
    wait_for_instance_lock,
    websocket_headers_kwargs,
    worker_registration_payload,
)
from synapse.logging import synapse_logger

DEFAULT_WS_URL = "ws://127.0.0.1:9112/ws"
DEFAULT_AGENT_NAME = "codex-worker"
DEFAULT_CODEX_BIN = "codex"
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
DEFAULT_MAX_STDERR_BYTES = 64_000
SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")
APPROVAL_CHOICES = ("untrusted", "on-request", "never")


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _normalize_dir(value: str, label: str) -> str:
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_dir():
        raise ValueError(f"{label} does not exist or is not a directory: {value}")
    return str(path.resolve())


def _normalize_add_dirs(values: Sequence[str]) -> list[str]:
    return [_normalize_dir(value, "--add-dir") for value in values if value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SynapticLathe Codex worker")
    parser.add_argument(
        "--url",
        default=_first_env("SYNAPTIC_WS_URL", "SYNAPTIC_URL", default=DEFAULT_WS_URL),
        help="SynapticLathe WebSocket URL. Env: SYNAPTIC_WS_URL or SYNAPTIC_URL.",
    )
    parser.add_argument(
        "--name",
        default=os.environ.get("SYNAPTIC_AGENT_NAME", DEFAULT_AGENT_NAME),
        help="Agent name to register on the server. Env: SYNAPTIC_AGENT_NAME.",
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("SYNAPTIC_API_KEY", ""),
        help="Worker API key. Prefer env SYNAPTIC_API_KEY.",
    )
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get("SYNAPTIC_CODEX_BIN", DEFAULT_CODEX_BIN),
        help="Codex executable path. Env: SYNAPTIC_CODEX_BIN.",
    )
    parser.add_argument(
        "--workdir",
        default=_first_env("SYNAPTIC_CODEX_WORKDIR", "SYNAPTIC_WORKDIR"),
        help="Git workspace for Codex. Required. Env: SYNAPTIC_CODEX_WORKDIR or SYNAPTIC_WORKDIR.",
    )
    parser.add_argument(
        "--add-dir",
        action="append",
        default=[],
        help="Additional directory Codex may access. Repeatable.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_env_int("SYNAPTIC_CODEX_TIMEOUT", _env_int("SYNAPTIC_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
        help="Default Codex task timeout in seconds. Env: SYNAPTIC_CODEX_TIMEOUT or SYNAPTIC_TIMEOUT.",
    )
    parser.add_argument(
        "--lock-file",
        default=_first_env("SYNAPTIC_CODEX_LOCK_FILE", "SYNAPTIC_WORKER_LOCK_FILE"),
        help="Single-instance lock file. Default is derived from URL and agent name.",
    )
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        default=_env_bool("SYNAPTIC_ALLOW_DUPLICATE_WORKER", False),
        help="Disable local single-instance locking. Env: SYNAPTIC_ALLOW_DUPLICATE_WORKER=1.",
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_false",
        dest="reconnect",
        default=_env_bool("SYNAPTIC_RECONNECT", True),
        help="Exit instead of reconnecting when the WebSocket disconnects.",
    )
    parser.add_argument(
        "--reconnect-initial-delay",
        type=float,
        default=_env_float("SYNAPTIC_RECONNECT_INITIAL_DELAY", 1.0),
        help="Initial reconnect delay in seconds. Env: SYNAPTIC_RECONNECT_INITIAL_DELAY.",
    )
    parser.add_argument(
        "--reconnect-max-delay",
        type=float,
        default=_env_float("SYNAPTIC_RECONNECT_MAX_DELAY", 30.0),
        help="Maximum reconnect delay in seconds. Env: SYNAPTIC_RECONNECT_MAX_DELAY.",
    )
    parser.add_argument(
        "--lock-retry-interval",
        type=float,
        default=_env_float("SYNAPTIC_LOCK_RETRY_INTERVAL", 60.0),
        help="Seconds between duplicate-worker lock retries. Env: SYNAPTIC_LOCK_RETRY_INTERVAL.",
    )
    parser.add_argument(
        "--sandbox",
        choices=SANDBOX_CHOICES,
        default=os.environ.get("SYNAPTIC_CODEX_SANDBOX", "read-only"),
        help="Codex sandbox mode. Default: read-only. Env: SYNAPTIC_CODEX_SANDBOX.",
    )
    parser.add_argument(
        "--approval-policy",
        choices=APPROVAL_CHOICES,
        default=os.environ.get("SYNAPTIC_CODEX_APPROVAL_POLICY", "never"),
        help="Codex approval policy. Default: never. Env: SYNAPTIC_CODEX_APPROVAL_POLICY.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("SYNAPTIC_CODEX_MODEL", ""),
        help="Optional Codex model override. Env: SYNAPTIC_CODEX_MODEL.",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("SYNAPTIC_CODEX_PROFILE", ""),
        help="Optional Codex profile name. Env: SYNAPTIC_CODEX_PROFILE.",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        help="Codex config override as key=value. Repeatable.",
    )
    parser.add_argument(
        "--pass-env",
        action="append",
        default=_env_list("SYNAPTIC_CODEX_PASS_ENV"),
        help=(
            "Explicit environment variable to pass to codex exec. Repeatable. "
            "Env SYNAPTIC_CODEX_PASS_ENV accepts comma-separated names."
        ),
    )
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=_env_int("SYNAPTIC_CODEX_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES),
        help="Maximum stdout bytes captured from codex exec. Env: SYNAPTIC_CODEX_MAX_OUTPUT_BYTES.",
    )
    parser.add_argument(
        "--max-stderr-bytes",
        type=int,
        default=_env_int("SYNAPTIC_CODEX_MAX_STDERR_BYTES", DEFAULT_MAX_STDERR_BYTES),
        help="Maximum stderr tail bytes retained from codex exec. Env: SYNAPTIC_CODEX_MAX_STDERR_BYTES.",
    )
    parser.add_argument(
        "--skip-git-repo-check",
        action="store_true",
        default=_env_bool("SYNAPTIC_CODEX_SKIP_GIT_REPO_CHECK", False),
        help="Pass --skip-git-repo-check to codex exec.",
    )
    parser.add_argument(
        "--no-ephemeral",
        action="store_false",
        dest="ephemeral",
        default=_env_bool("SYNAPTIC_CODEX_EPHEMERAL", True),
        help="Do not pass --ephemeral to codex exec.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= WORKER_MAX_TIMEOUT_SECONDS:
        parser.error(f"--timeout must be between 1 and {WORKER_MAX_TIMEOUT_SECONDS} seconds")
    if not args.codex_bin or len(args.codex_bin) > 4096 or "\0" in args.codex_bin:
        parser.error("--codex-bin must be a non-empty executable name or path")
    if not args.workdir:
        parser.error("--workdir is required, or set SYNAPTIC_CODEX_WORKDIR")
    if not 1 <= args.max_output_bytes <= WORKER_MAX_OUTPUT_BYTES:
        parser.error(f"--max-output-bytes must be between 1 and {WORKER_MAX_OUTPUT_BYTES}")
    if not 1 <= args.max_stderr_bytes <= WORKER_MAX_OUTPUT_BYTES:
        parser.error(f"--max-stderr-bytes must be between 1 and {WORKER_MAX_OUTPUT_BYTES}")
    if len(args.add_dir) > 32:
        parser.error("--add-dir may be repeated at most 32 times")
    if len(args.config) > 64 or any(len(item) > 4096 or "\0" in item for item in args.config):
        parser.error("--config has too many or oversized values")
    if any(len(value) > 256 or "\0" in value for value in (args.model, args.profile)):
        parser.error("--model and --profile must be at most 256 characters")
    if args.reconnect_initial_delay <= 0:
        parser.error("--reconnect-initial-delay must be positive")
    if args.reconnect_max_delay < args.reconnect_initial_delay:
        parser.error("--reconnect-max-delay must be greater than or equal to the initial delay")
    if args.lock_retry_interval <= 0:
        parser.error("--lock-retry-interval must be positive")
    if not args.lock_file:
        args.lock_file = default_worker_lock_file("codex", args.url, args.name)
    try:
        args.url = validate_websocket_url(args.url)
        args.name = validate_worker_agent_name(args.name)
        args.workdir = _normalize_dir(args.workdir, "--workdir")
        args.add_dir = _normalize_add_dirs(args.add_dir)
        args.pass_env = validate_child_env_names(args.pass_env)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def build_agent_config(args: argparse.Namespace) -> dict:
    return {
        "synapse_url": args.url,
        "agent_name": args.name,
        "api_key": args.key,
        "extra": {
            "codex_bin": args.codex_bin,
            "workdir": args.workdir,
            "add_dir": args.add_dir,
            "timeout": args.timeout,
            "sandbox": args.sandbox,
            "approval_policy": args.approval_policy,
            "model": args.model,
            "profile": args.profile,
            "config": args.config,
            "pass_env": args.pass_env,
            "max_output_bytes": args.max_output_bytes,
            "max_stderr_bytes": args.max_stderr_bytes,
            "skip_git_repo_check": args.skip_git_repo_check,
            "ephemeral": args.ephemeral,
        },
    }


def build_codex_exec_args(settings: Mapping[str, Any], plan: str) -> list[str]:
    """Build a shell-free ``codex exec`` argv list."""
    codex_bin = str(settings.get("codex_bin") or DEFAULT_CODEX_BIN)
    workdir = str(settings.get("workdir") or "")
    sandbox = str(settings.get("sandbox") or "read-only")
    approval_policy = str(settings.get("approval_policy") or "never")

    args = [codex_bin, "exec"]
    if settings.get("ephemeral", True):
        args.append("--ephemeral")
    args.extend(["--sandbox", sandbox, "--config", f"approval_policy={approval_policy!r}"])
    if workdir:
        args.extend(["--cd", workdir])
    for add_dir in settings.get("add_dir", []) or []:
        args.extend(["--add-dir", str(add_dir)])
    if settings.get("model"):
        args.extend(["--model", str(settings["model"])])
    if settings.get("profile"):
        args.extend(["--profile", str(settings["profile"])])
    for item in settings.get("config", []) or []:
        args.extend(["--config", str(item)])
    if settings.get("skip_git_repo_check"):
        args.append("--skip-git-repo-check")
    args.extend(["--", plan])
    return args


class CodexAgent(BaseAgent):
    """Run SynapticLathe tasks through a local ``codex exec`` process."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        extra = config.get("extra", config)
        self.settings = {
            "codex_bin": extra.get("codex_bin", DEFAULT_CODEX_BIN),
            "workdir": extra.get("workdir", ""),
            "add_dir": extra.get("add_dir", []) or [],
            "timeout": bounded_task_timeout(extra.get("timeout"), DEFAULT_TIMEOUT_SECONDS),
            "sandbox": extra.get("sandbox", "read-only"),
            "approval_policy": extra.get("approval_policy", "never"),
            "model": extra.get("model", ""),
            "profile": extra.get("profile", ""),
            "config": extra.get("config", []) or [],
            "pass_env": validate_child_env_names(extra.get("pass_env", []) or []),
            "max_output_bytes": min(
                max(1, int(extra.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES))),
                WORKER_MAX_OUTPUT_BYTES,
            ),
            "max_stderr_bytes": min(
                max(1, int(extra.get("max_stderr_bytes", DEFAULT_MAX_STDERR_BYTES))),
                WORKER_MAX_OUTPUT_BYTES,
            ),
            "skip_git_repo_check": bool(extra.get("skip_git_repo_check", False)),
            "ephemeral": bool(extra.get("ephemeral", True)),
        }
        self.synapse_url = config.get("synapse_url", DEFAULT_WS_URL)
        self.agent_name = config.get("agent_name", DEFAULT_AGENT_NAME)
        self.api_key = config.get("api_key", "")
        self._ws: Any | None = None
        self._current_proc: asyncio.subprocess.Process | None = None
        self._current_pgid: int | None = None

    async def connect(self) -> bool:
        try:
            import websockets
        except ImportError:
            print(
                "websockets is required. Install with `pip install -r requirements.txt` or `pip install -e .`.",
                file=sys.stderr,
            )
            return False

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            self._ws = await websockets.connect(
                self.synapse_url,
                max_size=WORKER_WS_MAX_MESSAGE_BYTES,
                max_queue=WORKER_WS_MAX_QUEUE,
                **websocket_headers_kwargs(websockets.connect, headers),
            )
            await self._ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "payload": worker_registration_payload(
                            self.agent_name,
                            worker_kind="codex_worker",
                            capabilities=("task", "accept", "return", "cancel", "codex_exec", "keepalive"),
                            extra_client_fields={
                                "sandbox": self.settings.get("sandbox"),
                                "approval_policy": self.settings.get("approval_policy"),
                            },
                        ),
                    }
                )
            )
            resp = await receive_registration_ack(self._ws)
            if resp.get("type") == "error":
                print(f"Register failed: {resp}", file=sys.stderr)
                return False
            self._connected = True
            print_registration_banner(resp)
            return True
        except Exception as exc:
            print(f"Connection failed: {exc}", file=sys.stderr)
            return False

    async def disconnect(self) -> None:
        await self._kill_process()
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False

    async def _kill_process(self) -> None:
        """Terminate the current child and its process group."""
        proc = self._current_proc
        process_group_id = self._current_pgid
        self._current_proc = None
        self._current_pgid = None
        await terminate_child_process(proc, process_group_id)

    async def _read_stream(self, stream: asyncio.StreamReader | None, sink: LimitedByteBuffer) -> None:
        if stream is None:
            return
        while True:
            try:
                line = await stream.read(8192)
            except ValueError:
                break
            if not line:
                break
            sink.append(line)

    async def _run_codex(self, plan: str, timeout: int | None = None) -> dict[str, Any]:
        if not isinstance(plan, str) or not plan or "\0" in plan:
            return {"exit_code": -1, "output": "", "error": "Task plan must be non-empty text"}
        effective_timeout = bounded_task_timeout(timeout, int(self.settings["timeout"]))
        argv = build_codex_exec_args(self.settings, plan)
        workdir = self.settings.get("workdir") or None
        env = build_child_env(pass_env=self.settings.get("pass_env", []) or [])

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=workdir,
                env=env,
                **child_process_group_kwargs(),
            )
        except FileNotFoundError:
            return {"exit_code": -1, "output": "", "error": "Codex executable was not found"}
        except PermissionError:
            return {"exit_code": -1, "output": "", "error": "Codex executable is not runnable"}
        except OSError:
            synapse_logger.exception("codex worker failed to start executable")
            return {"exit_code": -1, "output": "", "error": "Failed to start Codex executable"}

        self._current_proc = proc
        self._current_pgid = child_process_group_id(proc)

        stdout_buffer = LimitedByteBuffer(int(self.settings["max_output_bytes"]))
        stderr_buffer = LimitedByteBuffer(int(self.settings["max_stderr_bytes"]), keep_tail=True)
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self._read_stream(proc.stdout, stdout_buffer),
                    self._read_stream(proc.stderr, stderr_buffer),
                    proc.wait(),
                ),
                timeout=effective_timeout,
            )
        except TimeoutError:
            await self._kill_process()
            return {
                "exit_code": -1,
                "output": stdout_buffer.text(),
                "stderr": stderr_buffer.text(),
                "output_truncated": stdout_buffer.truncated,
                "stderr_truncated": stderr_buffer.truncated,
                "error": f"Codex timed out after {effective_timeout}s",
            }
        except asyncio.CancelledError:
            await terminate_child_process(proc, self._current_pgid)
            raise
        except Exception:
            await terminate_child_process(proc, self._current_pgid)
            raise
        finally:
            self._current_proc = None
            self._current_pgid = None

        exit_code = proc.returncode if proc.returncode is not None else -1
        stdout = stdout_buffer.text()
        stderr = stderr_buffer.text()
        error = "" if exit_code == 0 else (stderr or f"Codex exited with status {exit_code}")
        return {
            "exit_code": exit_code,
            "output": stdout,
            "stderr": stderr,
            "output_truncated": stdout_buffer.truncated,
            "stderr_truncated": stderr_buffer.truncated,
            "error": error,
        }

    async def _handle_task_message(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload", {})
        task_id = payload.get("task_id", "")
        plan = payload.get("plan", "")
        cid = msg.get("correlation_id", "")
        task_timeout = payload.get("timeout")

        await self._ws.send(json.dumps({"type": "accept", "correlation_id": cid}))
        synapse_logger.info(
            "codex worker task started",
            extra={
                "event": "codex_task_started",
                "agent": self.agent_name,
                "task_id": task_id,
                "timeout": task_timeout or self.settings["timeout"],
            },
        )
        result = await self._run_codex(plan, timeout=task_timeout)
        synapse_logger.info(
            "codex worker task completed",
            extra={
                "event": "codex_task_completed",
                "agent": self.agent_name,
                "task_id": task_id,
                "exit_code": result.get("exit_code"),
                "output_truncated": bool(result.get("output_truncated")),
                "stderr_truncated": bool(result.get("stderr_truncated")),
            },
        )

        return_payload: dict[str, Any] = {
            "task_id": task_id,
            "exit_code": result["exit_code"],
            "result": result["output"],
        }
        if result.get("error"):
            return_payload["error"] = result["error"]
        if result.get("output_truncated"):
            return_payload["output_truncated"] = True
        if result.get("stderr_truncated"):
            return_payload["stderr_truncated"] = True
        if result.get("stderr") and result["exit_code"] != 0:
            return_payload["stderr"] = result["stderr"]

        await self._ws.send(
            json.dumps(
                {
                    "type": "return",
                    "payload": return_payload,
                    "correlation_id": cid,
                }
            )
        )

    async def run_loop(self) -> None:
        if self._ws:
            await run_worker_message_loop(self._ws, self._handle_task_message, self._kill_process)


async def _run_forever(args: argparse.Namespace) -> int:
    delay = args.reconnect_initial_delay
    while True:
        agent = CodexAgent(build_agent_config(args))
        connected = await agent.connect()
        connected_at = time.monotonic() if connected else 0.0
        if connected:
            print(
                "Connected Codex worker "
                f"{args.name!r} to {args.url}; workdir={args.workdir}; "
                f"sandbox={args.sandbox}; approval_policy={args.approval_policy}"
            )
            try:
                await agent.run_loop()
            except Exception as exc:
                print(f"Codex worker connection loop ended: {exc}", file=sys.stderr)
            finally:
                await agent.disconnect()
        else:
            await agent.disconnect()

        if not args.reconnect:
            return 0 if connected else 1
        if connected_at and time.monotonic() - connected_at >= 60:
            delay = args.reconnect_initial_delay
        print(f"Codex worker disconnected; reconnecting in {delay:g}s.", file=sys.stderr)
        await asyncio.sleep(delay)
        delay = min(delay * 2, args.reconnect_max_delay)


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    lock = None
    if not args.allow_duplicate:
        lock = await wait_for_instance_lock(args.lock_file, f"Codex worker {args.name!r}", args.lock_retry_interval)
    try:
        return await _run_forever(args)
    finally:
        if lock is not None:
            lock.release()


def cli() -> None:
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    cli()
