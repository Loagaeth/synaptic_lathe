from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class _Section:
    heading_path: list[str]
    text: str
    has_body: bool


class MarkdownChunker:
    """Markdown 感知分块器。

    按照 Markdown 标题层级切分文档，每个章节作为独立的 chunk。
    如果某个章节内容超过 chunk_size，则在该章节内部进行递归分割。
    子章节可选继承父级标题作为上下文前缀。
    """

    def __init__(
        self,
        chunk_size: int = 8192,
        chunk_overlap: int = 50,
        include_heading_context: bool = True,
        max_heading_depth: int = 4,
        min_chunk_size: int = 50,
        continuation_prefix: str = "...",
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap must not be negative")
        self.chunk_size = int(chunk_size)
        self.chunk_overlap = min(int(chunk_overlap), self.chunk_size - 1)
        self.include_heading_context = include_heading_context
        self.max_heading_depth = max(1, min(int(max_heading_depth), 6))
        self.min_chunk_size = min_chunk_size
        self.continuation_prefix = continuation_prefix

    def chunk(self, text: str, **kwargs) -> list[str]:
        """按 Markdown 标题层级分块。"""
        if not text or not text.strip():
            return []

        chunk_size = int(kwargs.get("chunk_size", self.chunk_size))
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        sections = self._parse_sections(text)

        if not sections:
            return self._split_text(text, chunk_size)

        raw_chunks = self._sections_to_chunks(sections, chunk_size)
        merged = self._merge_heading_only_chunks(raw_chunks, chunk_size)
        return self._merge_short_chunks(merged, chunk_size)

    # ── section → chunk conversion ──

    def _sections_to_chunks(self, sections: list[_Section], chunk_size: int) -> list[tuple[str, bool]]:
        raw_chunks: list[tuple[str, bool]] = []
        for section in sections:
            section_text = section.text
            heading_path = section.heading_path
            has_body = section.has_body
            context_prefix = self._build_context_prefix(heading_path)
            full_text = context_prefix + section_text

            if len(full_text) <= chunk_size:
                raw_chunks.append((full_text.strip(), has_body))
            else:
                for split_chunk in self._split_text(full_text, chunk_size):
                    raw_chunks.append((split_chunk, True))
        return raw_chunks

    def _split_text(self, text: str, chunk_size: int) -> list[str]:
        """Split text on useful boundaries with bounded character overlap."""

        stripped = text.strip()
        if not stripped:
            return []
        if len(stripped) <= chunk_size:
            return [stripped]

        chunks: list[str] = []
        start = 0
        first = True
        while start < len(stripped):
            prefix = "" if first else self.continuation_prefix
            if len(prefix) >= chunk_size:
                prefix = prefix[: max(0, chunk_size // 4)]
            capacity = max(1, chunk_size - len(prefix))
            hard_end = min(len(stripped), start + capacity)
            end = hard_end
            if hard_end < len(stripped):
                search_from = min(hard_end, start + max(1, capacity // 2))
                for separator in ("\n\n", "\n", ". ", "。", " "):
                    boundary = stripped.rfind(separator, search_from, hard_end)
                    if boundary >= start:
                        end = boundary + len(separator)
                        break
            if end <= start:
                end = hard_end
            body = stripped[start:end].strip()
            if body:
                chunk = f"{prefix}{body}"
                chunks.append(chunk[:chunk_size])
            if end >= len(stripped):
                break
            overlap = min(self.chunk_overlap, max(0, end - start - 1), max(0, capacity - 1))
            start = max(start + 1, end - overlap)
            first = False
        return chunks

    # ── merge helpers ──

    def _merge_heading_only_chunks(self, raw_chunks: list[tuple[str, bool]], chunk_size: int) -> list[str]:
        merged: list[str] = []
        pending = ""
        for chunk_text, has_body in raw_chunks:
            if not chunk_text:
                continue
            if not has_body:
                if pending and len(pending) + len(chunk_text) + 2 > chunk_size:
                    merged.append(pending.strip())
                    pending = ""
                pending += chunk_text + "\n\n"
            else:
                if pending:
                    combined = pending + chunk_text
                    if len(combined) <= chunk_size:
                        merged.append(combined.strip())
                    else:
                        merged.append(pending.strip())
                        merged.append(chunk_text.strip())
                    pending = ""
                else:
                    merged.append(chunk_text.strip())
        if pending:
            pt = pending.strip()
            if merged and len(merged[-1] + "\n\n" + pt) <= chunk_size:
                merged[-1] = merged[-1] + "\n\n" + pt
            else:
                merged.append(pt)
        return [c for c in merged if c.strip()]

    def _merge_short_chunks(self, chunks: list[str], chunk_size: int) -> list[str]:
        if self.min_chunk_size <= 0 or len(chunks) <= 1:
            return chunks
        final: list[str] = []
        buf = ""
        for c in chunks:
            if buf:
                combined = buf + "\n\n" + c
                if len(combined) <= chunk_size:
                    buf = combined
                else:
                    final.append(buf)
                    buf = c if len(c) < self.min_chunk_size else ""
                    if len(c) >= self.min_chunk_size:
                        final.append(c)
            elif len(c) < self.min_chunk_size:
                buf = c
            else:
                final.append(c)
        if buf:
            if final and len(final[-1] + "\n\n" + buf) <= chunk_size:
                final[-1] = final[-1] + "\n\n" + buf
            else:
                final.append(buf)
        return final

    # ── heading context ──

    def _build_context_prefix(self, heading_path: list[str]) -> str:
        if self.include_heading_context and heading_path:
            return " > ".join(heading_path) + "\n\n"
        return ""

    def _parse_sections(self, text: str) -> list[_Section]:
        fenced_ranges = self._find_fenced_code_ranges(text)
        heading_pattern = re.compile(r"^(#{1," + str(self.max_heading_depth) + r"})\s*(.+)$", re.MULTILINE)
        headings = []
        for match in heading_pattern.finditer(text):
            if self._is_in_fenced_block(match.start(), fenced_ranges):
                continue
            headings.append(
                {
                    "level": len(match.group(1)),
                    "title": match.group(2).strip(),
                    "start": match.start(),
                    "end": match.end(),
                }
            )
        if not headings:
            return []

        sections: list[_Section] = []
        preamble = text[: headings[0]["start"]].strip()
        if preamble:
            sections.append(_Section(heading_path=[], text=preamble, has_body=True))

        heading_stack: list[dict] = []
        for i, heading in enumerate(headings):
            while heading_stack and heading_stack[-1]["level"] >= heading["level"]:
                heading_stack.pop()
            heading_stack.append({"level": heading["level"], "title": heading["title"]})
            content_start = heading["end"]
            content_end = headings[i + 1]["start"] if i + 1 < len(headings) else len(text)
            heading_line = text[heading["start"] : heading["end"]]
            body = text[content_start:content_end].strip()
            section_text = heading_line
            if body:
                section_text += "\n" + body
            heading_path = [h["title"] for h in heading_stack[:-1]]
            sections.append(
                _Section(
                    heading_path=heading_path,
                    text=section_text,
                    has_body=bool(body),
                )
            )
        return sections

    @staticmethod
    def _find_fenced_code_ranges(text: str) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        fence_pattern = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)
        matches = list(fence_pattern.finditer(text))
        i = 0
        while i < len(matches):
            open_match = matches[i]
            open_fence = open_match.group(1)
            fence_char = open_fence[0]
            fence_len = len(open_fence)
            for j in range(i + 1, len(matches)):
                close_match = matches[j]
                close_fence = close_match.group(1)
                if close_fence[0] == fence_char and len(close_fence) >= fence_len:
                    ranges.append((open_match.start(), close_match.end()))
                    i = j + 1
                    break
            else:
                ranges.append((open_match.start(), len(text)))
                i += 1  # 继续扫描后续可能的围栏块
        return ranges

    @staticmethod
    def _is_in_fenced_block(pos: int, ranges: list[tuple[int, int]]) -> bool:
        for start, end in ranges:
            if start <= pos < end:
                return True
        return False
