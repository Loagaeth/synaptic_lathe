"""Bounded in-process task events and WebSocket probe coordination."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from synapse.session import generate_correlation_id

_MAX_SUBSCRIBERS = 32
_MAX_QUEUE_SIZE = 256
_MAX_PROBE_RESULTS = 256


class TaskEventBroker:
    """Fan out small task state hints without retaining task output in memory."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def publish(self, event: Mapping[str, Any]) -> None:
        allowed = {"event", "task_id", "group_id", "status", "target", "profile", "purpose"}
        safe_event = {
            key: value
            for key, value in event.items()
            if key in allowed and isinstance(value, (str, int, float, bool, type(None)))
        }
        text = event.get("text")
        if isinstance(text, str) and text:
            safe_event["text"] = text[:2048]
        if not safe_event:
            return
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(dict(safe_event))
            except asyncio.QueueFull:
                continue

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        if len(self._subscribers) >= _MAX_SUBSCRIBERS:
            raise RuntimeError("Too many task event subscribers")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


@dataclass(slots=True)
class _PendingProbe:
    expected: set[str]
    sent_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)


class ProbeCoordinator:
    """Match short-lived probe acknowledgements to server-derived Agent identities."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingProbe] = {}

    def create(self, targets: Iterable[str]) -> str:
        self._prune()
        expected = set(targets)
        probe_id = generate_correlation_id()
        self._pending[probe_id] = _PendingProbe(expected=expected, sent_at=time.monotonic())
        return probe_id

    def record(self, agent_name: str, payload: Mapping[str, Any]) -> bool:
        probe_id = payload.get("probe_id")
        if not isinstance(probe_id, str):
            return False
        pending = self._pending.get(probe_id)
        if pending is None or agent_name not in pending.expected or len(pending.results) >= _MAX_PROBE_RESULTS:
            return False
        queue_depth = payload.get("queue_depth", 0)
        if isinstance(queue_depth, bool) or not isinstance(queue_depth, int):
            queue_depth = 0
        pending.results[agent_name] = {
            "agent": agent_name,
            "ok": True,
            "rtt_ms": round((time.monotonic() - pending.sent_at) * 1000, 2),
            "busy": payload.get("busy") is True,
            "queue_depth": max(0, min(queue_depth, 1024)),
        }
        if pending.expected.issubset(pending.results):
            pending.event.set()
        return True

    async def collect(self, probe_id: str, timeout: float) -> dict[str, dict[str, Any]]:
        pending = self._pending.get(probe_id)
        if pending is None:
            return {}
        if pending.expected and not pending.expected.issubset(pending.results):
            try:
                await asyncio.wait_for(pending.event.wait(), timeout=max(0.05, min(timeout, 10.0)))
            except TimeoutError:
                pass
        self._pending.pop(probe_id, None)
        return dict(pending.results)

    def discard(self, probe_id: str) -> None:
        self._pending.pop(probe_id, None)

    def _prune(self) -> None:
        cutoff = time.monotonic() - 30
        for probe_id, pending in tuple(self._pending.items()):
            if pending.sent_at < cutoff:
                self._pending.pop(probe_id, None)


task_events = TaskEventBroker()
probe_coordinator = ProbeCoordinator()
