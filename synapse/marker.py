"""标记扫描器 — 流式扫描 << /return>> 结束标记。"""

from __future__ import annotations


class MarkerScanner:
    """跨 chunk 缓冲扫描 << /return>> 标记。"""

    def __init__(self, marker: str = "<</return>>") -> None:
        if not marker:
            raise ValueError("marker must not be empty")
        self.marker = marker
        self._buffer = ""

    @property
    def _marker_len(self) -> int:
        return len(self.marker)

    def scan(self, chunk: str) -> tuple[str, bool]:
        """Return the newly safe prefix and whether the terminator was found."""
        self._buffer += chunk

        idx = self._buffer.find(self.marker)
        if idx >= 0:
            content = self._buffer[:idx]
            # The return marker terminates the result. Discard anything after it.
            self._buffer = ""
            return (content, True)

        # 跨 chunk 缓冲：保留最后 n-1 字符防止标记被截断
        if len(self._buffer) > self._marker_len:
            safe = self._buffer[: -self._marker_len + 1]
            self._buffer = self._buffer[-self._marker_len + 1 :]
            return (safe, False)

        return ("", False)

    def flush(self) -> str:
        """返回缓冲区剩余内容（未找到标记时调用）。"""
        remainder = self._buffer
        self._buffer = ""
        return remainder

    def reset(self) -> None:
        self._buffer = ""
