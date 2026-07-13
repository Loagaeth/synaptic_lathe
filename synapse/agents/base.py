"""BaseAgent 抽象类 — 所有 Agent 适配器的统一接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Message:
    """发送给 Agent 的消息。"""

    def __init__(self, content: str, **kwargs) -> None:
        self.content = content
        self.metadata: dict = kwargs


class Response:
    """Agent 返回的响应。"""

    def __init__(self, content: str, success: bool = True, error: str = "") -> None:
        self.content = content
        self.success = success
        self.error = error


class BaseAgent(ABC):
    """Agent 适配器基类。"""

    marker = "<</return>>"

    def __init__(self, config: dict) -> None:
        self.config = config
        self._connected = False

    @abstractmethod
    async def connect(self) -> bool:
        """建立连接。"""

    async def send(self, message: Message) -> Response:
        """发送消息。默认未实现——子类可用 run_loop() 替代。"""
        raise NotImplementedError("Use run_loop() or override send()")

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接。"""

    @property
    def connected(self) -> bool:
        return self._connected

    @staticmethod
    def _scan_for_marker(text: str, marker: str = "<</return>>") -> tuple[str, bool]:
        idx = text.find(marker)
        if idx >= 0:
            return text[:idx], True
        return text, False
