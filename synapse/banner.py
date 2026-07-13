"""Tiny terminal banner helpers."""

from __future__ import annotations

import sys
from typing import TextIO

_BANNER_LINES = (
    "   _____                         __  _",
    "  / ___/__  ______  ____ _____  / /_(_)____",
    "  \\__ \\/ / / / __ \\/ __ `/ __ \\/ __/ / ___/",
    " ___/ / /_/ / / / / /_/ / /_/ / /_/ / /__",
    "/____/\\__, /_/ /_/\\__,_/ .___/\\__/_/\\___/",
    "     /____/           /_/  ",
    "        _           __  __",
    "       / /   ____ _/ /_/ /_  ___",
    "      / /   / __ `/ __/ __ \\/ _ \\",
    "     / /___/ /_/ / /_/ / / /  __/",
    "    /_____/\\__,_/\\__/_/ /_/\\___/",
)


def format_banner(subtitle: str = "") -> str:
    """Return a compact SynapticLathe ASCII title."""

    lines = list(_BANNER_LINES)
    if subtitle:
        lines.append(f"== SynapticLathe {subtitle} ==")
    return "\n".join(lines)


def print_banner(subtitle: str = "", *, file: TextIO | None = None) -> None:
    """Print the terminal banner."""

    print(format_banner(subtitle), file=file or sys.stdout)
