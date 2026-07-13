"""Validated request URL helpers shared by public command generators."""

from __future__ import annotations

import ipaddress
import re

from fastapi import Request

_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_ROOT_PATH_RE = re.compile(r"^$|^/[A-Za-z0-9._~/-]*$")


def _display_hostname(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        if not value or len(value) > 253 or not all(_DNS_LABEL_RE.fullmatch(label) for label in labels):
            return ""
        return value.lower()
    return f"[{address}]" if address.version == 6 else str(address)


def public_request_urls(request: Request) -> tuple[str, str]:
    """Return sanitized HTTP and WS base URLs or two empty strings."""

    scheme = request.url.scheme
    hostname = request.url.hostname or ""
    display_host = _display_hostname(hostname)
    if scheme not in {"http", "https"} or not display_host:
        return "", ""
    try:
        port = request.url.port
    except ValueError:
        return "", ""
    authority = f"{display_host}:{port}" if port else display_host
    root_path = str(request.scope.get("root_path", "")).rstrip("/")
    root_segments = root_path.split("/")[1:] if root_path else []
    if not _ROOT_PATH_RE.fullmatch(root_path) or any(segment in {"", ".", ".."} for segment in root_segments):
        return "", ""
    http_url = f"{scheme}://{authority}{root_path}"
    ws_scheme = "wss" if scheme == "https" else "ws"
    return http_url, f"{ws_scheme}://{authority}{root_path}"
