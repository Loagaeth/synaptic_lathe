"""Small, stdlib-only helpers for durable local file writes."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX.
    msvcrt = None


def ensure_private_directory(path: Path) -> Path:
    """Create an owner-only directory and reject symlink/non-owner replacements."""

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path_stat = path.lstat()
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise OSError(f"Unsafe private directory: {path}")
    if hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
        raise OSError(f"Private directory is not owned by this user: {path}")
    os.chmod(path, 0o700)
    return path


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync so a rename survives a sudden power loss."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold a blocking owner-only advisory lock without following symlinks."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        raise OSError(f"Lock path is not a regular file: {path}")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        os.close(fd)
        raise OSError(f"Lock path is not owned by this user: {path}")
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)
    handle = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            handle.seek(0)
            if not handle.read(1):
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - unsupported platforms fail closed.
            raise RuntimeError("No supported file-locking API is available")
        yield
    finally:
        if fcntl is not None:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            with suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def atomic_write_text(
    path: Path,
    content: str,
    *,
    overwrite: bool,
    mode: int | None = None,
) -> bool:
    """Atomically write UTF-8 text without following a destination symlink."""

    path = Path(path)
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)

    existing_mode: int | None = None
    try:
        existing_stat = path.stat(follow_symlinks=False)
        if stat.S_ISREG(existing_stat.st_mode):
            existing_mode = stat.S_IMODE(existing_stat.st_mode)
    except FileNotFoundError:
        pass

    target_mode = mode if mode is not None else existing_mode
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        if target_mode is not None:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, target_mode)
            else:  # pragma: no cover - Windows mode bits are best-effort.
                os.chmod(temporary_path, target_mode)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary_path, path)
        else:
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                return False
        _fsync_directory(path.parent)
        return True
    finally:
        if fd >= 0:
            os.close(fd)
        temporary_path.unlink(missing_ok=True)
