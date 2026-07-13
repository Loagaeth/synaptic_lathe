"""本地子进程 Worker。

该模块作为独立客户端连接 SynapticLathe 服务器的 WebSocket，注册为一个 agent，
收到任务后在本机启动受控子进程执行，并把 stdout/stderr 流式回传。

推荐用法:
    SYNAPTIC_API_KEY=<worker-api-key> \
    synaptic-subprocess-worker --url ws://127.0.0.1:9112/ws --name local-python --command python

兼容用法:
    python -m synapse.agents.subprocess_agent --url ws://127.0.0.1:9112/ws --name local-python --command python
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
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
    sanitize_process_text,
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
DEFAULT_AGENT_NAME = "subprocess-agent"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
READ_CHUNK_BYTES = 8192


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


def _env_list(*names: str) -> list[str]:
    for name in names:
        raw = os.environ.get(name, "")
        if raw:
            return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _parse_command(value: str) -> list[str]:
    try:
        command = shlex.split(value, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError(f"Invalid command: {exc}") from exc
    if not command:
        raise ValueError("Command must not be empty")
    if len(command) > 128 or any(not item or len(item) > 8192 or "\0" in item for item in command):
        raise ValueError("Command has too many, empty, or oversized arguments")
    return command


def _normalize_workdir(workdir: str) -> str:
    if not workdir:
        return ""
    path = Path(workdir).expanduser()
    if not path.is_dir():
        raise ValueError(f"workdir does not exist or is not a directory: {workdir}")
    return str(path.resolve())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SynapticLathe local subprocess worker")
    parser.add_argument(
        "--url",
        default=os.environ.get("SYNAPTIC_WS_URL", os.environ.get("SYNAPTIC_URL", DEFAULT_WS_URL)),
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
        "--command",
        default=os.environ.get("SYNAPTIC_COMMAND", ""),
        help="Executable command. The task plan is passed as the final argv. Env: SYNAPTIC_COMMAND.",
    )
    parser.add_argument(
        "--allow-plan-options",
        action="store_false",
        dest="protect_plan_options",
        default=True,
        help="Do not insert -- before the remote plan. Only for commands that reject the option terminator.",
    )
    parser.add_argument(
        "--workdir",
        default=os.environ.get("SYNAPTIC_WORKDIR", ""),
        help="Working directory for the subprocess. Env: SYNAPTIC_WORKDIR.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_env_int("SYNAPTIC_TIMEOUT", DEFAULT_TIMEOUT_SECONDS),
        help="Default subprocess timeout in seconds. Env: SYNAPTIC_TIMEOUT.",
    )
    parser.add_argument(
        "--pass-env",
        action="append",
        default=_env_list("SYNAPTIC_SUBPROCESS_PASS_ENV", "SYNAPTIC_PASS_ENV"),
        help=(
            "Explicit environment variable to pass to the child process. Repeatable. "
            "Env SYNAPTIC_SUBPROCESS_PASS_ENV accepts comma-separated names."
        ),
    )
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=_env_int(
            "SYNAPTIC_SUBPROCESS_MAX_OUTPUT_BYTES",
            _env_int("SYNAPTIC_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES),
        ),
        help=(
            "Maximum stdout/stderr bytes captured and streamed from the child process. "
            "Env: SYNAPTIC_SUBPROCESS_MAX_OUTPUT_BYTES or SYNAPTIC_MAX_OUTPUT_BYTES."
        ),
    )
    parser.add_argument(
        "--lock-file",
        default=os.environ.get("SYNAPTIC_WORKER_LOCK_FILE", ""),
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
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("--command is required, or set SYNAPTIC_COMMAND")
    if not 1 <= args.timeout <= WORKER_MAX_TIMEOUT_SECONDS:
        parser.error(f"--timeout must be between 1 and {WORKER_MAX_TIMEOUT_SECONDS} seconds")
    if not 1 <= args.max_output_bytes <= WORKER_MAX_OUTPUT_BYTES:
        parser.error(f"--max-output-bytes must be between 1 and {WORKER_MAX_OUTPUT_BYTES}")
    if args.reconnect_initial_delay <= 0:
        parser.error("--reconnect-initial-delay must be positive")
    if args.reconnect_max_delay < args.reconnect_initial_delay:
        parser.error("--reconnect-max-delay must be greater than or equal to the initial delay")
    if args.lock_retry_interval <= 0:
        parser.error("--lock-retry-interval must be positive")
    if not args.lock_file:
        args.lock_file = default_worker_lock_file("subprocess", args.url, args.name)
    try:
        args.url = validate_websocket_url(args.url)
        args.name = validate_worker_agent_name(args.name)
        args.command_args = _parse_command(args.command)
        args.workdir = _normalize_workdir(args.workdir)
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
            "command": args.command,
            "command_args": args.command_args,
            "protect_plan_options": args.protect_plan_options,
            "workdir": args.workdir,
            "timeout": args.timeout,
            "pass_env": args.pass_env,
            "max_output_bytes": args.max_output_bytes,
        },
    }


class SubprocessAgent(BaseAgent):
    """通过 WebSocket 接收任务，并在本地子进程中执行。

    安全边界：服务器只负责路由任务；本类运行在哪台机器、哪个系统用户、哪个
    工作目录下，就只拥有该环境赋予它的本地执行权限。
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        extra = config.get("extra", config)  # 兼容裸 dict 和嵌套 extra
        raw_command = extra.get("command_args", extra.get("command", ""))
        if isinstance(raw_command, list):
            self.command_args = [str(item) for item in raw_command]
        else:
            self.command_args = _parse_command(str(raw_command)) if raw_command else []
        self.protect_plan_options = bool(extra.get("protect_plan_options", True))
        self.workdir = extra.get("workdir", "") or None
        self.timeout = int(extra.get("timeout", DEFAULT_TIMEOUT_SECONDS))
        self.pass_env = list(extra.get("pass_env", []) or [])
        self.max_output_bytes = min(
            max(1, int(extra.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES))),
            WORKER_MAX_OUTPUT_BYTES,
        )
        self.env = extra.get("env", {}) or {}

        self.synapse_url = config.get("synapse_url", DEFAULT_WS_URL)
        self.agent_name = config.get("agent_name", DEFAULT_AGENT_NAME)
        self.api_key = config.get("api_key", "")
        self._ws: Any | None = None
        self._current_proc: asyncio.subprocess.Process | None = None
        self._current_pgid: int | None = None

    async def connect(self) -> bool:
        """连接服务器 WebSocket 并注册当前 worker。"""
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
                            worker_kind="subprocess_worker",
                            capabilities=("task", "accept", "chunk", "return", "cancel", "keepalive"),
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
        except Exception as e:
            print(f"Connection failed: {e}", file=sys.stderr)
            return False

    async def disconnect(self) -> None:
        """断开连接并清理仍在运行的子进程。"""
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

    async def _run_command(
        self, plan: str, task_id: str, cid: str, stdin_data: str = "", timeout: int | None = None
    ) -> dict:
        """执行本地子进程，流式发送 chunk，返回结果摘要。"""
        if not self.command_args:
            return {"exit_code": -1, "output": "", "error": "No command configured"}
        if not isinstance(plan, str) or not plan or "\0" in plan:
            return {"exit_code": -1, "output": "", "error": "Task plan must be non-empty text"}
        if not isinstance(stdin_data, str):
            return {"exit_code": -1, "output": "", "error": "Task stdin must be text"}

        effective_timeout = bounded_task_timeout(timeout, self.timeout)
        command_args = list(self.command_args)
        if self.protect_plan_options:
            command_args.append("--")

        try:
            env = build_child_env(pass_env=self.pass_env)
            for name in validate_child_env_names(self.env.keys()):
                value = self.env.get(name)
                if value is not None:
                    env[name] = str(value)
        except ValueError as e:
            return {"exit_code": -1, "output": "", "error": str(e)}

        try:
            proc = await asyncio.create_subprocess_exec(
                *command_args,
                plan,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
                cwd=self.workdir,
                env=env,
                **child_process_group_kwargs(),
            )
        except FileNotFoundError:
            return {"exit_code": -1, "output": "", "error": "Configured command was not found"}
        except PermissionError:
            return {"exit_code": -1, "output": "", "error": "Configured command is not executable"}
        except OSError:
            synapse_logger.exception("subprocess worker failed to start configured command")
            return {"exit_code": -1, "output": "", "error": "Failed to start configured command"}

        self._current_proc = proc
        self._current_pgid = child_process_group_id(proc)

        output_buffer = LimitedByteBuffer(self.max_output_bytes)
        streamed_bytes = 0
        stream_enabled = True
        truncation_notice_sent = False

        async def _write_stdin() -> None:
            if proc.stdin is None:
                return
            try:
                if stdin_data:
                    proc.stdin.write(stdin_data.encode())
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
                    proc.stdin.close()

        async def _send_chunk(text: str) -> bool:
            text = sanitize_process_text(text)
            if not self._ws or not text:
                return True
            try:
                await self._ws.send(
                    json.dumps(
                        {
                            "type": "chunk",
                            "payload": {"text": text},
                            "correlation_id": cid,
                        }
                    )
                )
            except Exception:
                return False
            return True

        async def _read_and_stream() -> None:
            nonlocal stream_enabled, streamed_bytes, truncation_notice_sent
            if proc.stdout is None:
                return
            while True:
                try:
                    chunk = await proc.stdout.read(READ_CHUNK_BYTES)
                except ValueError:
                    break
                if not chunk:
                    break

                output_buffer.append(chunk)
                remaining_stream_bytes = max(0, self.max_output_bytes - streamed_bytes)
                if stream_enabled and remaining_stream_bytes:
                    stream_chunk = chunk[:remaining_stream_bytes]
                    streamed_bytes += len(stream_chunk)
                    stream_enabled = await _send_chunk(stream_chunk.decode(errors="replace"))

                if stream_enabled and output_buffer.truncated and not truncation_notice_sent:
                    truncation_notice_sent = True
                    stream_enabled = await _send_chunk(f"\n[output truncated after {self.max_output_bytes} bytes]\n")

        try:
            await asyncio.wait_for(
                asyncio.gather(_write_stdin(), _read_and_stream(), proc.wait()),
                timeout=effective_timeout,
            )
        except TimeoutError:
            await self._kill_process()
            return {
                "exit_code": -1,
                "output": output_buffer.text(),
                "output_truncated": output_buffer.truncated,
                "error": f"Process timed out after {effective_timeout}s",
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
        return {
            "exit_code": exit_code,
            "output": output_buffer.text(),
            "output_truncated": output_buffer.truncated,
            "error": "",
        }

    async def _handle_task_message(self, msg: dict) -> None:
        payload = msg.get("payload", {})
        task_id = payload.get("task_id", "")
        plan = payload.get("plan", "")
        cid = msg.get("correlation_id", "")
        stdin_data = payload.get("stdin", "")
        task_timeout = payload.get("timeout")

        await self._ws.send(json.dumps({"type": "accept", "correlation_id": cid}))
        synapse_logger.info(
            "subprocess worker task started",
            extra={
                "event": "subprocess_task_started",
                "agent": self.agent_name,
                "task_id": task_id,
                "timeout": task_timeout or self.timeout,
            },
        )

        result = await self._run_command(plan, task_id, cid, stdin_data=stdin_data, timeout=task_timeout)
        synapse_logger.info(
            "subprocess worker task completed",
            extra={
                "event": "subprocess_task_completed",
                "agent": self.agent_name,
                "task_id": task_id,
                "exit_code": result.get("exit_code"),
                "output_truncated": bool(result.get("output_truncated")),
            },
        )

        return_payload: dict = {
            "task_id": task_id,
            "exit_code": result["exit_code"],
            "output": result["output"],
        }
        if result["error"]:
            return_payload["error"] = result["error"]
        if result.get("output_truncated"):
            return_payload["output_truncated"] = True

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
        """Receive controls concurrently while executing child tasks in order."""
        if self._ws:
            await run_worker_message_loop(self._ws, self._handle_task_message, self._kill_process)


async def _run_forever(args: argparse.Namespace) -> int:
    delay = args.reconnect_initial_delay
    while True:
        agent = SubprocessAgent(build_agent_config(args))
        connected = await agent.connect()
        connected_at = time.monotonic() if connected else 0.0
        if connected:
            print(
                f"Connected worker '{args.name}' to {args.url}; command={args.command!r}; workdir={args.workdir or '.'}"
            )
            try:
                await agent.run_loop()
            except Exception as exc:
                print(f"Subprocess worker connection loop ended: {exc}", file=sys.stderr)
            finally:
                await agent.disconnect()
        else:
            await agent.disconnect()

        if not args.reconnect:
            return 0 if connected else 1
        if connected_at and time.monotonic() - connected_at >= 60:
            delay = args.reconnect_initial_delay
        print(f"Subprocess worker disconnected; reconnecting in {delay:g}s.", file=sys.stderr)
        await asyncio.sleep(delay)
        delay = min(delay * 2, args.reconnect_max_delay)


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    lock = None
    if not args.allow_duplicate:
        lock = await wait_for_instance_lock(
            args.lock_file, f"Subprocess worker {args.name!r}", args.lock_retry_interval
        )
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
