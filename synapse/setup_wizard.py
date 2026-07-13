"""Source-tree setup wizard for SynapticLathe.

The module is intentionally stdlib-only so it can run before project
dependencies are installed: ``python -m synapse.setup_wizard``.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import stat
import subprocess  # nosec B404
import sys
import venv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from synapse.banner import print_banner
from synapse.file_utils import atomic_write_text, ensure_private_directory

DEFAULT_CONTROL_NAME = "synapticctl"
_IPV4_ANY_HOST = ".".join(("0", "0", "0", "0"))
_PUBLIC_BIND_HOSTS = {_IPV4_ANY_HOST, "::"}
_LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost", "::1"}
_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _valid_host(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        labels = value.split(".")
        return bool(value) and len(value) <= 253 and all(_DNS_LABEL_RE.fullmatch(label) for label in labels)


@dataclass(frozen=True)
class SetupOptions:
    base_dir: Path | str = Path.cwd()
    host: str = "127.0.0.1"
    port: int = 9112
    api_key: str = "auto"
    worker_api_key: str = "auto"
    public_read_context: bool = False
    install_deps: bool = True
    venv_dir: str = ".venv"
    force: bool = False
    python: str = sys.executable
    control_name: str = DEFAULT_CONTROL_NAME


@dataclass(frozen=True)
class SetupResult:
    base_dir: Path
    config_path: Path
    control_path: Path
    cmd_path: Path
    runtime_python: Path
    installed: bool
    config_created: bool
    generated_api_key: str
    generated_worker_api_key: str


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _quote_yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_config_text(*, host: str, port: int, api_key: str, worker_api_key: str, public_read_context: bool) -> str:
    cors_host = "127.0.0.1" if host in _PUBLIC_BIND_HOSTS else host
    display_cors_host = f"[{cors_host}]" if ":" in cors_host else cors_host
    cors_origin = f"http://{display_cors_host}:{port}"
    return f"""# SynapticLathe generated config
router:
  default_agent: ""
  rules: []

agents: {{}}

server:
  host: {_quote_yaml(host)}
  port: {port}
  api_key: {_quote_yaml(api_key)}
  worker_api_key: {_quote_yaml(worker_api_key)}
  cors_origins: [{_quote_yaml(cors_origin)}]
  behind_proxy: false
  trusted_proxy_hosts: ["127.0.0.1", "::1"]
  public_read_context: {_bool_text(public_read_context)}
  outbound_trust_env: false
  rate_limit_max: 60
  rate_limit_window: 60
  ws_rate_limit_max: 240
  ws_rate_limit_window: 60
  max_body_bytes: 1048576
  ws_receive_timeout: 60
  ws_ping_interval: 30
  pending_message_ttl_hours: 24
  auto_memory_threshold: 0
  auto_memory_max_chars: 4000

memory:
  scope: shared
  embedding_provider: local
  embedding_model: ""
  embedding_api_url: ""
  embedding_api_key: ""
  embedding_dimensions: 0
  embedding_timeout: 20
  embedding_trust_env: false

db_path: "data/synaptic_lathe.db"
"""


def _venv_python(venv_path: Path) -> Path:
    if os.name == "nt":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def ensure_venv(base_dir: Path, venv_dir: str) -> Path:
    venv_path = (base_dir / venv_dir).resolve()
    python = _venv_python(venv_path)
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(venv_path)
    return python


def install_project(base_dir: Path, python: Path) -> None:
    if (base_dir / "pyproject.toml").is_file():
        cmd = [str(python), "-m", "pip", "install", "-e", str(base_dir)]
    elif (base_dir / "requirements.txt").is_file():
        cmd = [str(python), "-m", "pip", "install", "-r", str(base_dir / "requirements.txt")]
    else:
        raise ValueError("No pyproject.toml or requirements.txt found in the project directory")
    subprocess.run(cmd, check=True)  # noqa: S603  # nosec B603


def _write_text(path: Path, content: str, *, force: bool, mode: int | None = None) -> bool:
    return atomic_write_text(path, content, overwrite=force, mode=mode)


def build_control_script(*, base_dir: Path, python: Path, config_path: Path, state_dir: Path, log_dir: Path) -> str:
    pid_file = state_dir / "synaptic.pid"
    stdout_log = log_dir / "synaptic_lathe.log"
    return f"""#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

PYTHON = {str(python)!r}
if os.path.abspath(sys.executable) != os.path.abspath(PYTHON):
    os.execv(PYTHON, [PYTHON, __file__, *sys.argv[1:]])

from synapse.file_utils import exclusive_file_lock
from synapse.process_control import (
    managed_process_running,
    make_process_record,
    read_process_record,
    write_process_record,
)

BASE_DIR = Path({str(base_dir)!r})
CONFIG = Path({str(config_path)!r})
PID_FILE = Path({str(pid_file)!r})
CONTROL_LOCK = PID_FILE.with_suffix(".control.lock")
LOG_FILE = Path({str(stdout_log)!r})
CMD = [PYTHON, "-m", "synapse.cli", str(CONFIG)]


def _record():
    return read_process_record(PID_FILE)


def _running(record) -> bool:
    return managed_process_running(record)


def _runtime_env(*, log_stdout: bool) -> dict[str, str]:
    env = os.environ.copy()
    env.update({{
        "SYNAPTIC_LOG_DIR": str(LOG_FILE.parent),
        "SYNAPTIC_LOG_FILE": LOG_FILE.name,
        "SYNAPTIC_LOG_STDOUT": "1" if log_stdout else "0",
    }})
    return env


def start() -> int:
    record = _record()
    if _running(record):
        print(f"SynapticLathe is already running: pid={{record.pid}}")
        return 0
    PID_FILE.unlink(missing_ok=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    env = _runtime_env(log_stdout=False)
    kwargs = {{
        "cwd": str(BASE_DIR),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(CMD, **kwargs)  # noqa: S603  # nosec B603 - generated launcher uses fixed argv.
    time.sleep(0.2)
    if proc.poll() is not None:
        print("SynapticLathe exited during startup; run foreground for details", file=sys.stderr)
        return 1
    write_process_record(PID_FILE, make_process_record(proc.pid, CMD))
    print(f"SynapticLathe started: pid={{proc.pid}}")
    print(f"Log: {{LOG_FILE}}")
    return 0


def stop() -> int:
    record = _record()
    if not _running(record):
        print("SynapticLathe is not running")
        PID_FILE.unlink(missing_ok=True)
        return 0
    pid = record.pid
    if os.name == "nt":
        taskkill = str(Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "taskkill.exe")
        subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"], check=False)  # noqa: S603  # nosec B603
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            PID_FILE.unlink(missing_ok=True)
            print("SynapticLathe stopped")
            return 0
        except PermissionError:
            print(f"Permission denied while stopping pid={{pid}}", file=sys.stderr)
            return 1
    for _ in range(30):
        if not _running(record):
            PID_FILE.unlink(missing_ok=True)
            print("SynapticLathe stopped")
            return 0
        time.sleep(0.2)
    print(f"SynapticLathe did not stop within timeout: pid={{pid}}", file=sys.stderr)
    return 1


def status() -> int:
    record = _record()
    if _running(record):
        print(f"running pid={{record.pid}}")
        return 0
    PID_FILE.unlink(missing_ok=True)
    print("stopped")
    return 1


def foreground() -> int:
    return subprocess.call(CMD, cwd=str(BASE_DIR), env=_runtime_env(log_stdout=True))  # noqa: S603  # nosec B603


def logs() -> int:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.touch(exist_ok=True, mode=0o600)
    with suppress(OSError):
        LOG_FILE.chmod(0o600)
    position = LOG_FILE.stat().st_size
    try:
        while True:
            size = LOG_FILE.stat().st_size
            if size < position:
                position = 0
            if size > position:
                with LOG_FILE.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(position)
                    print(fh.read(), end="")
                    position = fh.tell()
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 130


def usage() -> int:
    print("Usage: synapticctl [start|stop|restart|status|foreground|logs]")
    return 2


def _dispatch_control(cmd: str) -> int:
    if cmd == "start":
        return start()
    if cmd == "stop":
        return stop()
    if cmd == "restart":
        code = stop()
        if code not in (0, 1):
            return code
        return start()
    if cmd == "status":
        return status()
    return usage()


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "start"
    if cmd in {{"foreground", "fg"}}:
        return foreground()
    if cmd in {{"logs", "tail"}}:
        return logs()
    with exclusive_file_lock(CONTROL_LOCK):
        return _dispatch_control(cmd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
"""


def build_cmd_wrapper(control_path: Path, python: Path) -> str:
    python_text = str(python).replace("%", "%%")
    control_text = str(control_path).replace("%", "%%")
    return f'''@echo off
"{python_text}" "{control_text}" %*
'''


def run_setup(options: SetupOptions) -> SetupResult:
    if not options.control_name or Path(options.control_name).name != options.control_name:
        raise ValueError("control_name must be a plain filename")
    if not 1 <= options.port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not _valid_host(options.host):
        raise ValueError("host must be an IP address, localhost, or a valid DNS name")
    base_dir = Path(options.base_dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    config_path = base_dir / "config.yaml"
    preserving_config = config_path.exists() and not options.force
    if not preserving_config and options.host not in _LOCAL_BIND_HOSTS and options.api_key == "":
        raise ValueError("An empty admin API key is allowed only for an explicit local bind")
    if options.install_deps and not (
        (base_dir / "pyproject.toml").is_file() or (base_dir / "requirements.txt").is_file()
    ):
        raise ValueError("Setup with dependency installation must run from a source checkout")

    state_dir = base_dir / ".synaptic"
    log_dir = base_dir / "logs"
    data_dir = base_dir / "data"
    ensure_private_directory(state_dir)
    ensure_private_directory(log_dir)
    ensure_private_directory(data_dir)

    # Keep the venv launcher path: resolve() follows its Python symlink and can
    # silently replace it with the system interpreter.
    runtime_python = Path(os.path.abspath(Path(options.python).expanduser()))
    installed = False
    if options.install_deps:
        if options.venv_dir:
            runtime_python = ensure_venv(base_dir, options.venv_dir)
        install_project(base_dir, runtime_python)
        installed = True

    generated_api_key = ""
    generated_worker_api_key = ""
    api_key = options.api_key
    worker_api_key = options.worker_api_key
    if api_key == "auto":
        if preserving_config:
            api_key = ""
        else:
            generated_api_key = secrets.token_urlsafe(24)
            api_key = generated_api_key
    if worker_api_key == "auto":
        if preserving_config:
            worker_api_key = ""
        else:
            generated_worker_api_key = secrets.token_urlsafe(24)
            worker_api_key = generated_worker_api_key
    config_created = False
    if not config_path.exists() or options.force:
        config_text = build_config_text(
            host=options.host,
            port=options.port,
            api_key=api_key,
            worker_api_key=worker_api_key,
            public_read_context=options.public_read_context,
        )
        config_created = _write_text(config_path, config_text, force=True, mode=stat.S_IRUSR | stat.S_IWUSR)

    control_path = base_dir / options.control_name
    cmd_path = base_dir / f"{options.control_name}.cmd"
    control_script = build_control_script(
        base_dir=base_dir,
        python=runtime_python,
        config_path=config_path,
        state_dir=state_dir,
        log_dir=log_dir,
    )
    _write_text(control_path, control_script, force=True, mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    _write_text(cmd_path, build_cmd_wrapper(control_path, runtime_python), force=True)

    return SetupResult(
        base_dir=base_dir,
        config_path=config_path,
        control_path=control_path,
        cmd_path=cmd_path,
        runtime_python=runtime_python,
        installed=installed,
        config_created=config_created,
        generated_api_key=generated_api_key,
        generated_worker_api_key=generated_worker_api_key,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize a SynapticLathe source checkout")
    parser.add_argument("--base-dir", default=".", help="Project/runtime directory. Default: current directory.")
    parser.add_argument("--host", default="127.0.0.1", help="Server listen host written to config.yaml.")
    parser.add_argument("--port", type=int, default=9112, help="Server listen port written to config.yaml.")
    parser.add_argument("--api-key", default="auto", help="Administrator API key. Default: auto-generate.")
    parser.add_argument("--worker-api-key", default="auto", help="WebSocket worker key. Default: auto-generate.")
    parser.add_argument("--no-api-key", action="store_true", help="Write an empty admin key. Local-only use.")
    parser.add_argument("--public-read-context", action="store_true", help="Allow unauthenticated /context* reads.")
    parser.add_argument("--skip-install", action="store_true", help="Do not create venv or install dependencies.")
    parser.add_argument(
        "--venv", default=".venv", help="Virtualenv directory. Use --no-venv to install into current Python."
    )
    parser.add_argument(
        "--no-venv", action="store_true", help="Install dependencies into the current Python environment."
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing config.yaml and launcher files.")
    parser.add_argument("--control-name", default=DEFAULT_CONTROL_NAME, help="Generated launcher name.")
    parser.add_argument("--yes", action="store_true", help="Accepted for scriptable setup; setup is non-interactive.")
    return parser


def options_from_args(args: argparse.Namespace) -> SetupOptions:
    api_key = "" if args.no_api_key else args.api_key
    return SetupOptions(
        base_dir=args.base_dir,
        host=args.host,
        port=args.port,
        api_key=api_key,
        worker_api_key=args.worker_api_key,
        public_read_context=args.public_read_context,
        install_deps=not args.skip_install,
        venv_dir="" if args.no_venv else args.venv,
        force=args.force,
        control_name=args.control_name,
    )


def cli(argv: Sequence[str] | None = None) -> None:
    print_banner("server setup")
    args = build_parser().parse_args(argv)
    result = run_setup(options_from_args(args))
    print("SynapticLathe setup complete")
    print(f"Base:   {result.base_dir}")
    print(f"Config: {result.config_path}")
    print(f"Run:    {result.control_path} start")
    print(f"Stop:   {result.control_path} stop")
    print(f"Logs:   {result.control_path} logs")
    if result.generated_api_key:
        print(f"Admin API key:  {result.generated_api_key}")
    elif result.config_created:
        print("Admin API key:  empty")
    if result.generated_worker_api_key:
        print(f"Worker API key: {result.generated_worker_api_key}")
    elif result.config_created:
        print("Worker API key: empty (workers fall back to the admin key)")
    if result.generated_api_key or result.generated_worker_api_key:
        print("Generated keys are stored in config.yaml; setup prints them only once.")
        print("Empty API key is intended only for local trusted deployments.")


if __name__ == "__main__":
    cli()
