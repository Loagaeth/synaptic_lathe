"""Stable protocol metadata for SynapticLathe clients and workers."""

from __future__ import annotations

from typing import Any

from synapse import __version__

SERVER_NAME = "SynapticLathe"
API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"
WS_PROTOCOL_VERSION = 1
MIN_SUPPORTED_WS_PROTOCOL_VERSION = 1

WS_CAPABILITIES = (
    "register",
    "hello",
    "send",
    "task",
    "accept",
    "chunk",
    "task_chunk",
    "return",
    "cancel",
    "broadcast",
    "offline_queue",
    "profile_dispatch",
)


def protocol_metadata(
    *,
    auth_required: bool | None = None,
    worker_auth_required: bool | None = None,
    public_read_context: bool | None = None,
) -> dict[str, Any]:
    """Return public protocol metadata for HTTP and WebSocket clients."""
    data: dict[str, Any] = {
        "server": SERVER_NAME,
        "server_version": __version__,
        "api_version": API_VERSION,
        "api_prefix": API_PREFIX,
        "legacy_http_paths": True,
        "ws_protocol_version": WS_PROTOCOL_VERSION,
        "min_supported_ws_protocol_version": MIN_SUPPORTED_WS_PROTOCOL_VERSION,
        "ws_paths": ["/ws", f"{API_PREFIX}/ws"],
        "capabilities": list(WS_CAPABILITIES),
    }
    if auth_required is not None:
        data["auth_required"] = auth_required
    if worker_auth_required is not None:
        data["worker_auth_required"] = worker_auth_required
    if public_read_context is not None:
        data["public_read_context"] = public_read_context
    return data


def parse_ws_protocol_version(value: Any) -> int | None:
    """Parse a client-supplied WebSocket protocol version.

    Missing values are treated as the current protocol for backwards
    compatibility with alpha clients.
    """
    if value in (None, ""):
        return WS_PROTOCOL_VERSION
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def is_supported_ws_protocol(version: int | None) -> bool:
    return version is not None and MIN_SUPPORTED_WS_PROTOCOL_VERSION <= version <= WS_PROTOCOL_VERSION
