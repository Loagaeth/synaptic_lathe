"""Setup wizard for standalone SynapticLathe worker runtimes."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import venv
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from synapse.agents.worker_utils import validate_websocket_url
from synapse.banner import print_banner
from synapse.file_utils import atomic_write_text, ensure_private_directory

DEFAULT_WS_URL = "ws://127.0.0.1:9112/ws"
DEFAULT_CONTROL_NAME = "workerctl"
SUPPORTED_KINDS = ("profile", "subprocess")
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class WorkerSetupOptions:
    base_dir: Path | str = Path.cwd()
    project_dir: Path | str = Path.cwd()
    kind: str = "profile"
    url: str = DEFAULT_WS_URL
    name: str = ""
    api_key: str = ""
    clear_api_key: bool = False
    command: str = ""
    workdir: str = ""
    profiles_path: Path | str | None = None
    timeout: int = 600
    max_output_bytes: int = 1_000_000
    install_deps: bool = True
    venv_dir: str = ".venv"
    force: bool = False
    python: str = sys.executable
    control_name: str = DEFAULT_CONTROL_NAME


@dataclass(frozen=True)
class WorkerSetupResult:
    base_dir: Path
    control_path: Path
    cmd_path: Path
    env_path: Path
    runtime_python: Path
    kind: str
    name: str
    installed: bool
    profiles_path: Path | None


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


def install_project(project_dir: Path, python: Path) -> None:
    if (project_dir / "pyproject.toml").is_file():
        cmd = [str(python), "-m", "pip", "install", "-e", str(project_dir)]
    elif (project_dir / "requirements.txt").is_file():
        cmd = [str(python), "-m", "pip", "install", "-r", str(project_dir / "requirements.txt")]
    else:
        raise ValueError("No pyproject.toml or requirements.txt found in the project directory")
    subprocess.run(cmd, check=True)  # noqa: S603  # nosec B603


def _write_text(path: Path, content: str, *, force: bool, mode: int | None = None) -> bool:
    return atomic_write_text(path, content, overwrite=force, mode=mode)


def _which(name: str) -> str:
    return shutil.which(name) or name


def _yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _profile_command(name: str, *args: str) -> str:
    items = [_which(name), *args]
    return "\n".join(f"      - {_yaml_quote(item)}" for item in items)


def build_profiles_text(*, workdir: Path, timeout: int, max_output_bytes: int) -> str:
    safe_workdir = _yaml_quote(str(workdir))
    codex_command = _profile_command(
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--config",
        "approval_policy='never'",
        "--skip-git-repo-check",
        "--",
        "{plan}",
    )
    return f"""# SynapticLathe generated profile dispatcher config.
# Keep real secrets and session ids on this machine; do not commit profiles.yaml.
default_profile: codex
profiles:
  codex:
    command:
{codex_command}
    workdir: {safe_workdir}
    timeout: 1800
    max_output_bytes: {max_output_bytes}
    advisory_safe: true
    tags: [code, review, planning]

  hermes:
    command:
{_profile_command("hermes", "--oneshot", "{plan}")}
    workdir: {safe_workdir}
    timeout: {timeout}
    max_output_bytes: {max_output_bytes}
    advisory_safe: false
    tags: [general]

  claude:
    command:
{_profile_command("claude", "-p", "{plan}", "--permission-mode", "plan")}
    workdir: {safe_workdir}
    timeout: {timeout}
    max_output_bytes: {max_output_bytes}
    advisory_safe: true
    tags: [analysis, review, planning]

  reasonix:
    command:
{_profile_command("reasonix", "run", "--effort", "low", "--budget", "0.10", "{plan}")}
    workdir: {safe_workdir}
    timeout: 1800
    max_output_bytes: {max_output_bytes}
    advisory_safe: false
    tags: [reasoning]
"""


def _read_env_values(path: Path) -> dict[str, str]:
    try:
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > 64_000:
            return {}
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        values[name] = str(value)
    return values


def build_env_text(*, api_key: str, url: str) -> str:
    values = {
        "SYNAPTIC_API_KEY": api_key,
        "SYNAPTIC_WS_URL": url,
    }
    return "".join(f"{name}={json.dumps(value)}\n" for name, value in values.items())


def _read_env_function_source() -> str:
    return r"""
def _load_env() -> dict[str, str]:
    env = os.environ.copy()
    if PROJECT_ON_PYTHONPATH:
        project = str(PROJECT_DIR)
        current_pythonpath = env.get("PYTHONPATH", "")
        paths = [item for item in current_pythonpath.split(os.pathsep) if item]
        if project and project not in paths:
            env["PYTHONPATH"] = project + (os.pathsep + current_pythonpath if current_pythonpath else "")
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        if not name:
            continue
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        env[name] = str(value)
    return env
"""


def build_control_script(
    *,
    base_dir: Path,
    project_dir: Path,
    python: Path,
    env_path: Path,
    state_dir: Path,
    log_dir: Path,
    command: list[str],
) -> str:
    pid_file = state_dir / "worker.pid"
    stdout_log = log_dir / "synaptic_worker.log"
    project_on_pythonpath = (project_dir / "synapse" / "__init__.py").is_file()
    return f"""#!/usr/bin/env python3
from __future__ import annotations

import json
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

BASE_DIR = Path({str(base_dir)!r})
PROJECT_DIR = Path({str(project_dir)!r})
PROJECT_ON_PYTHONPATH = {project_on_pythonpath!r}
if PROJECT_ON_PYTHONPATH and str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from synapse.file_utils import exclusive_file_lock
from synapse.process_control import (
    managed_process_running,
    make_process_record,
    read_process_record,
    write_process_record,
)

ENV_FILE = Path({str(env_path)!r})
PID_FILE = Path({str(pid_file)!r})
CONTROL_LOCK = PID_FILE.with_suffix(".control.lock")
LOG_FILE = Path({str(stdout_log)!r})
CMD = [{", ".join(repr(part) for part in command)}]
{_read_env_function_source()}


def _record():
    return read_process_record(PID_FILE)


def _running(record) -> bool:
    return managed_process_running(record)


def _runtime_env(*, log_stdout: bool) -> dict[str, str]:
    env = _load_env()
    env.update({{
        "SYNAPTIC_LOG_DIR": str(LOG_FILE.parent),
        "SYNAPTIC_LOG_FILE": LOG_FILE.name,
        "SYNAPTIC_LOG_STDOUT": "1" if log_stdout else "0",
    }})
    return env


def start() -> int:
    record = _record()
    if _running(record):
        print(f"SynapticLathe worker is already running: pid={{record.pid}}")
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
        print("SynapticLathe worker exited during startup; run foreground for details", file=sys.stderr)
        return 1
    write_process_record(PID_FILE, make_process_record(proc.pid, CMD))
    print(f"SynapticLathe worker started: pid={{proc.pid}}")
    print(f"Log: {{LOG_FILE}}")
    return 0


def stop() -> int:
    record = _record()
    if not _running(record):
        print("SynapticLathe worker is not running")
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
            print("SynapticLathe worker stopped")
            return 0
        except PermissionError:
            print(f"Permission denied while stopping pid={{pid}}", file=sys.stderr)
            return 1
    for _ in range(30):
        if not _running(record):
            PID_FILE.unlink(missing_ok=True)
            print("SynapticLathe worker stopped")
            return 0
        time.sleep(0.2)
    print(f"SynapticLathe worker did not stop within timeout: pid={{pid}}", file=sys.stderr)
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
    print("Usage: workerctl [start|stop|restart|status|foreground|logs]")
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


def _normalize_kind(kind: str) -> str:
    normalized = kind.strip().lower()
    if normalized not in SUPPORTED_KINDS:
        choices = ", ".join(SUPPORTED_KINDS)
        raise ValueError(f"worker kind must be one of: {choices}")
    return normalized


def _worker_name(kind: str, name: str) -> str:
    if name:
        return name
    return "local-dispatcher" if kind == "profile" else "local-subprocess"


def _build_worker_command(options: WorkerSetupOptions, *, base_dir: Path, profiles_path: Path | None) -> list[str]:
    kind = _normalize_kind(options.kind)
    name = _worker_name(kind, options.name)
    command = [str(options.python), "-m"]
    if kind == "profile":
        if profiles_path is None:
            raise ValueError("profiles_path is required for profile worker setup")
        return [
            str(options.python),
            "-m",
            "synapse.agents.profile_agent",
            "--url",
            options.url,
            "--name",
            name,
            "--profiles",
            str(profiles_path),
        ]
    if not options.command:
        raise ValueError("--command is required when --kind subprocess is used")
    result = [
        *command,
        "synapse.agents.subprocess_agent",
        "--url",
        options.url,
        "--name",
        name,
        "--command",
        options.command,
        "--timeout",
        str(options.timeout),
        "--max-output-bytes",
        str(options.max_output_bytes),
    ]
    if options.workdir:
        result.extend(["--workdir", options.workdir])
    else:
        result.extend(["--workdir", str(base_dir)])
    return result


def run_setup(options: WorkerSetupOptions) -> WorkerSetupResult:
    if not options.control_name or Path(options.control_name).name != options.control_name:
        raise ValueError("control_name must be a plain filename")
    validate_websocket_url(options.url)
    kind = _normalize_kind(options.kind)
    name = _worker_name(kind, options.name)
    if not _AGENT_NAME_RE.fullmatch(name):
        raise ValueError("worker name must contain 1-64 letters, digits, underscores, or hyphens")
    if not 1 <= options.timeout <= 3600:
        raise ValueError("timeout must be between 1 and 3600 seconds")
    if not 1024 <= options.max_output_bytes <= 16 * 1024 * 1024:
        raise ValueError("max_output_bytes must be between 1024 and 16777216")
    base_dir = Path(options.base_dir).expanduser().resolve()
    project_dir = Path(options.project_dir).expanduser().resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    worker_workdir = Path(options.workdir).expanduser().resolve() if options.workdir else base_dir
    if not worker_workdir.is_dir():
        raise ValueError(f"workdir does not exist or is not a directory: {worker_workdir}")
    if options.install_deps and not (
        (project_dir / "pyproject.toml").is_file() or (project_dir / "requirements.txt").is_file()
    ):
        raise ValueError("--project-dir must be a source checkout, or use --skip-install")

    state_dir = base_dir / ".synaptic"
    log_dir = base_dir / "logs"
    ensure_private_directory(state_dir)
    ensure_private_directory(log_dir)

    # Keep the venv launcher path: resolve() follows its Python symlink and can
    # silently replace it with the system interpreter.
    runtime_python = Path(os.path.abspath(Path(options.python).expanduser()))
    installed = False
    if options.install_deps:
        if options.venv_dir:
            runtime_python = ensure_venv(base_dir, options.venv_dir)
        install_project(project_dir, runtime_python)
        installed = True

    profiles_path: Path | None = None
    if kind == "profile":
        profiles_path = Path(options.profiles_path or base_dir / "profiles.yaml").expanduser().resolve()
        profiles_text = build_profiles_text(
            workdir=worker_workdir,
            timeout=options.timeout,
            max_output_bytes=options.max_output_bytes,
        )
        _write_text(profiles_path, profiles_text, force=options.force, mode=stat.S_IRUSR | stat.S_IWUSR)

    env_path = state_dir / "worker.env"
    existing_env = _read_env_values(env_path)
    api_key = "" if options.clear_api_key else options.api_key or existing_env.get("SYNAPTIC_API_KEY", "")
    _write_text(
        env_path,
        build_env_text(api_key=api_key, url=options.url),
        force=True,
        mode=stat.S_IRUSR | stat.S_IWUSR,
    )

    effective_options = replace(
        options,
        python=str(runtime_python),
        kind=kind,
        name=name,
        workdir=str(worker_workdir),
    )
    command = _build_worker_command(effective_options, base_dir=base_dir, profiles_path=profiles_path)
    control_path = base_dir / options.control_name
    cmd_path = base_dir / f"{options.control_name}.cmd"
    control_script = build_control_script(
        base_dir=base_dir,
        project_dir=project_dir,
        python=runtime_python,
        env_path=env_path,
        state_dir=state_dir,
        log_dir=log_dir,
        command=command,
    )
    _write_text(control_path, control_script, force=True, mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    _write_text(cmd_path, build_cmd_wrapper(control_path, runtime_python), force=True)

    return WorkerSetupResult(
        base_dir=base_dir,
        control_path=control_path,
        cmd_path=cmd_path,
        env_path=env_path,
        runtime_python=runtime_python,
        kind=kind,
        name=effective_options.name,
        installed=installed,
        profiles_path=profiles_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize a standalone SynapticLathe worker runtime")
    parser.add_argument("--base-dir", default=".", help="Worker runtime directory. Default: current directory.")
    parser.add_argument("--project-dir", default=".", help="SynapticLathe source checkout used for editable install.")
    parser.add_argument("--kind", choices=SUPPORTED_KINDS, default="profile", help="Worker type to generate.")
    parser.add_argument("--url", default=DEFAULT_WS_URL, help="SynapticLathe WebSocket URL.")
    parser.add_argument("--name", default="", help="Agent name registered on the server.")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("SYNAPTIC_API_KEY", ""),
        help="Worker API key; stored in .synaptic/worker.env. Prefer SYNAPTIC_API_KEY.",
    )
    parser.add_argument(
        "--clear-api-key",
        action="store_true",
        help="Explicitly remove a previously stored worker API key.",
    )
    parser.add_argument("--command", default="", help="Local command for --kind subprocess.")
    parser.add_argument("--workdir", default="", help="Child process workdir, or profile workdir.")
    parser.add_argument("--profiles", default="", help="Profile config path for --kind profile.")
    parser.add_argument("--timeout", type=int, default=600, help="Default worker task timeout.")
    parser.add_argument("--max-output-bytes", type=int, default=1_000_000, help="Default output capture limit.")
    parser.add_argument("--skip-install", action="store_true", help="Do not create venv or install dependencies.")
    parser.add_argument(
        "--venv", default=".venv", help="Virtualenv directory. Use --no-venv to use the current Python."
    )
    parser.add_argument("--no-venv", action="store_true", help="Install dependencies into the current Python.")
    parser.add_argument("--force", action="store_true", help="Overwrite generated launcher and local config.")
    parser.add_argument("--control-name", default=DEFAULT_CONTROL_NAME, help="Generated launcher name.")
    parser.add_argument("--yes", action="store_true", help="Accepted for scriptable setup; setup is non-interactive.")
    return parser


def options_from_args(args: argparse.Namespace) -> WorkerSetupOptions:
    return WorkerSetupOptions(
        base_dir=args.base_dir,
        project_dir=args.project_dir,
        kind=args.kind,
        url=args.url,
        name=args.name,
        api_key=args.api_key,
        clear_api_key=args.clear_api_key,
        command=args.command,
        workdir=args.workdir,
        profiles_path=args.profiles or None,
        timeout=args.timeout,
        max_output_bytes=args.max_output_bytes,
        install_deps=not args.skip_install,
        venv_dir="" if args.no_venv else args.venv,
        force=args.force,
        control_name=args.control_name,
    )


def cli(argv: Sequence[str] | None = None) -> None:
    print_banner("worker setup")
    args = build_parser().parse_args(argv)
    try:
        result = run_setup(options_from_args(args))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print("SynapticLathe worker setup complete")
    print(f"Base:     {result.base_dir}")
    print(f"Kind:     {result.kind}")
    print(f"Name:     {result.name}")
    if result.profiles_path is not None:
        print(f"Profiles: {result.profiles_path}")
    print(f"Env:      {result.env_path}")
    print(f"Run:      {result.control_path} start")
    print(f"Stop:     {result.control_path} stop")
    print(f"Logs:     {result.control_path} logs")


if __name__ == "__main__":
    cli()
