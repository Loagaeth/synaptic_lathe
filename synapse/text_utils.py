"""Text normalization for data crossing logs, subprocesses, and protocols."""

from __future__ import annotations

import re
import unicodedata

_ANSI_CONTROL_SEQUENCE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))")
_NUL_RUN_RE = re.compile(r"\x00+")
_BIDI_CONTROL_CLASSES = {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}


def sanitize_untrusted_text(value: str) -> str:
    """Remove terminal/control data without expanding long NUL runs."""

    had_nul = "\x00" in value
    cleaned = _NUL_RUN_RE.sub("", value)
    if had_nul:
        cleaned = "[NUL bytes omitted]\n" + cleaned
    cleaned = _ANSI_CONTROL_SEQUENCE_RE.sub("", cleaned)
    return "".join(
        char
        for char in cleaned
        if char in "\n\t"
        or (unicodedata.category(char) != "Cc" and unicodedata.bidirectional(char) not in _BIDI_CONTROL_CLASSES)
    )
