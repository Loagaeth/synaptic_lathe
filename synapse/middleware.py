"""中间件集合 — 速率限制、安全头、CSRF、日志、body 大小限制。"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import defaultdict, deque

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from synapse.logging import correlation_id_ctx, synapse_logger
from synapse.session import generate_correlation_id

_ADMIN_PATH_PREFIXES = ("/admin", "/api/v1/admin")


class BodyTooLargeError(Exception):
    pass


# ── Body 大小限制 ─────────────────────────────


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Limit request bodies by Content-Length and by bytes read from the ASGI stream."""

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            max_bytes = request.app.state.config.server.max_body_bytes
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        return JSONResponse(
                            status_code=413,
                            content={"error": "Request body too large", "code": "PAYLOAD_TOO_LARGE"},
                        )
                except ValueError:
                    return JSONResponse(
                        status_code=400,
                        content={"error": "Invalid Content-Length", "code": "INVALID_REQUEST"},
                    )

            received = 0
            original_receive = request._receive

            async def limited_receive():
                nonlocal received
                message = await original_receive()
                if message.get("type") == "http.request":
                    received += len(message.get("body", b""))
                    if received > max_bytes:
                        raise BodyTooLargeError()
                return message

            request._receive = limited_receive
            try:
                return await call_next(request)
            except BodyTooLargeError:
                return JSONResponse(
                    status_code=413,
                    content={"error": "Request body too large", "code": "PAYLOAD_TOO_LARGE"},
                )
        return await call_next(request)


# ── 速率限制 ──────────────────────────────────

_rate_limits: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = asyncio.Lock()
_MAX_TRACKED_CLIENTS = 20_000
_TRIMMED_CLIENTS = 10_000


def _trusted_proxy_address(address: str, trusted_hosts: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    for item in trusted_hosts:
        try:
            if ip in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def resolve_client_ip(request: HTTPConnection) -> str:
    """Resolve a client address from a trusted proxy chain, right to left."""

    peer = request.client.host if request.client else "unknown"
    try:
        cfg = request.app.state.config
    except AttributeError:
        return peer
    if not cfg.server.behind_proxy or not _trusted_proxy_address(peer, cfg.server.trusted_proxy_hosts):
        return peer

    forwarded = request.headers.get("X-Forwarded-For", "")
    addresses: list[str] = []
    for item in forwarded.split(","):
        candidate = item.strip()
        try:
            addresses.append(str(ipaddress.ip_address(candidate)))
        except ValueError:
            continue
    addresses.append(peer)
    for address in reversed(addresses):
        if not _trusted_proxy_address(address, cfg.server.trusted_proxy_hosts):
            return address
    return addresses[0] if addresses else peer


def _prune_timestamps(timestamps: deque[float], now: float, window: int) -> None:
    cutoff = now - window
    while timestamps and timestamps[0] <= cutoff:
        timestamps.popleft()


async def rate_limit_middleware(request: Request, call_next):
    try:
        cfg = request.app.state.config
    except AttributeError:
        return await call_next(request)
    ip = resolve_client_ip(request)
    now = time.monotonic()
    window = cfg.server.rate_limit_window
    max_req = cfg.server.rate_limit_max
    async with _rate_lock:
        if len(_rate_limits) >= _MAX_TRACKED_CLIENTS and ip not in _rate_limits:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Too many unique clients, retry later",
                    "code": "OVERLOAD",
                },
            )
        timestamps = _rate_limits[ip]
        _prune_timestamps(timestamps, now, window)
        if len(timestamps) >= max_req:
            return JSONResponse(status_code=429, content={"error": "Too many requests", "code": "RATE_LIMITED"})
        timestamps.append(now)
    return await call_next(request)


async def rate_cleanup_loop(app) -> None:
    while True:
        window = app.state.config.server.rate_limit_window
        await asyncio.sleep(max(30, min(120, window)))
        async with _rate_lock:
            now = time.monotonic()
            for ip in list(_rate_limits):
                _prune_timestamps(_rate_limits[ip], now, window)
                if not _rate_limits[ip]:
                    del _rate_limits[ip]
            if len(_rate_limits) > _MAX_TRACKED_CLIENTS:
                sorted_ips = sorted(
                    _rate_limits,
                    key=lambda address: _rate_limits[address][0] if _rate_limits[address] else 0,
                )
                for ip in sorted_ips[: len(_rate_limits) - _TRIMMED_CLIENTS]:
                    del _rate_limits[ip]


# ── 安全响应头 ────────────────────────────────


async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'"
    )
    path = request.url.path
    if path in {"/", "/admin", "/web"} or path.startswith("/web/"):
        # The admin UI and its assets are deployed as one versioned unit. Stale
        # HTML or JavaScript can otherwise hide controls or call an incompatible API.
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── CSRF 保护 ─────────────────────────────────


async def csrf_middleware(request: Request, call_next):
    path = request.url.path
    is_admin_path = any(path == prefix or path.startswith(f"{prefix}/") for prefix in _ADMIN_PATH_PREFIXES)
    if is_admin_path and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            cfg = request.app.state.config
        except AttributeError:
            return await call_next(request)
        key = cfg.server.api_key.get_secret_value()
        if not key:
            return await call_next(request)
        origin = request.headers.get("origin", "")
        allowed = cfg.server.get_cors_origins()
        # 同源请求（Origin 与服务器 scheme/host/port 匹配）直接放行；admin 写入不接受通配 CORS。
        server_origin = f"{request.url.scheme}://{request.url.netloc}" if request.url.hostname else ""
        if origin and (origin == server_origin or origin in allowed):
            pass
        elif origin:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Cross-origin admin requests blocked",
                    "code": "CSRF_BLOCKED",
                },
            )
        # 无 Origin header（API 客户端）不做 CSRF 检查
    return await call_next(request)


# ── 请求日志 ──────────────────────────────────


async def log_requests_middleware(request: Request, call_next):
    started = time.perf_counter()
    path = request.url.path
    method = request.method
    client_ip = resolve_client_ip(request)
    correlation_id = generate_correlation_id()
    context_token = correlation_id_ctx.set(correlation_id)
    try:
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            synapse_logger.exception(
                f"HTTP {method} {path} -> 500",
                extra={
                    "event": "http_request",
                    "method": method,
                    "path": path,
                    "status": 500,
                    "duration_ms": duration_ms,
                    "client_ip": client_ip,
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Correlation-ID"] = correlation_id
        synapse_logger.info(
            f"HTTP {method} {path} -> {response.status_code}",
            extra={
                "event": "http_request",
                "method": method,
                "path": path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": client_ip,
            },
        )
        return response
    finally:
        correlation_id_ctx.reset(context_token)
