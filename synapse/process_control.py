"""Identity-aware process records used by generated control scripts."""

from __future__ import annotations

import ctypes
import json
import os
import stat
import subprocess  # nosec B404
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path

from synapse.file_utils import atomic_write_text


@dataclass(frozen=True)
class ManagedProcessRecord:
    pid: int
    start_token: str
    argv: tuple[str, ...]


def _linux_start_token(pid: int) -> str:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat_text[stat_text.rfind(")") + 2 :].split()
        return fields[19]
    except (OSError, IndexError, ValueError):
        return ""


def _windows_start_token(pid: int) -> str:
    if os.name != "nt":
        return ""
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ""
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return ""
        return str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _portable_posix_start_token(pid: int) -> str:
    for executable in ("/bin/ps", "/usr/bin/ps"):
        if not Path(executable).is_file():
            continue
        try:
            # Fixed utility, numeric PID, and no shell.
            result = subprocess.run(  # noqa: S603  # nosec B603
                [executable, "-o", "lstart=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return result.stdout.strip()
    return ""


def process_start_token(pid: int) -> str:
    if os.name == "nt":
        return _windows_start_token(pid)
    return _linux_start_token(pid) or _portable_posix_start_token(pid)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _linux_cmdline(pid: int) -> tuple[str, ...]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(part.decode(errors="replace") for part in raw.split(b"\0") if part)


def make_process_record(pid: int, argv: list[str]) -> ManagedProcessRecord:
    return ManagedProcessRecord(pid=pid, start_token=process_start_token(pid), argv=tuple(argv))


def write_process_record(path: Path, record: ManagedProcessRecord) -> None:
    payload = json.dumps(asdict(record), ensure_ascii=True, separators=(",", ":")) + "\n"
    atomic_write_text(path, payload, overwrite=True, mode=0o600)


def read_process_record(path: Path) -> ManagedProcessRecord | None:
    try:
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > 64_000:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        pid = int(payload["pid"])
        start_token = str(payload.get("start_token") or "")
        raw_argv = payload.get("argv", [])
        if pid <= 0 or len(start_token) > 128 or not isinstance(raw_argv, list) or len(raw_argv) > 128:
            return None
        argv = tuple(str(item) for item in raw_argv)
        if any(len(item) > 4096 for item in argv):
            return None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    return ManagedProcessRecord(pid=pid, start_token=start_token, argv=argv)


def managed_process_running(record: ManagedProcessRecord | None) -> bool:
    if record is None or not _process_alive(record.pid):
        return False
    current_token = process_start_token(record.pid)
    if record.start_token:
        return bool(current_token and current_token == record.start_token)
    current_argv = _linux_cmdline(record.pid)
    return bool(current_argv and current_argv == record.argv)
