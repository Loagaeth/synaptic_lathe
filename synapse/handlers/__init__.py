"""http_api 类型的 Agent 处理器 — 直接 HTTP 调用，不走 WebSocket。

注意：本模块是 handlers/__init__.py（Python 包），非 handlers.py。
      导入方式为 `from synapse.handlers import handle_http_send`。
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException, Request

from synapse.config import GlobalConfig
from synapse.connection import connection_manager
from synapse.http_utils import public_request_urls
from synapse.logging import synapse_logger
from synapse.text_utils import sanitize_untrusted_text

_PROFILE_WORKER_GUIDE = {
    "needs_install": True,
    "commands": ['pip install "git+https://github.com/Loagaeth/synaptic_lathe.git"'],
    "run": (
        "SYNAPTIC_API_KEY='<worker-api-key>' synaptic-worker-setup "
        "--kind profile --url '{ws_url}/ws' --name {name} "
        "--base-dir ./synaptic-worker --project-dir . --skip-install"
    ),
    "note": (
        "The package install above supplies the runtime; setup creates an owner-only worker.env "
        "and an allowlisted profiles.yaml. "
        "Review command paths before starting ./synaptic-worker/workerctl."
    ),
}

_INSTALL_GUIDES = {
    "profile_worker": _PROFILE_WORKER_GUIDE,
    "claude_code": _PROFILE_WORKER_GUIDE,
    "codex_cli": {
        "needs_install": True,
        "commands": [
            'pip install "git+https://github.com/Loagaeth/synaptic_lathe.git"',
            "codex login",
        ],
        "run": (
            "SYNAPTIC_API_KEY='<worker-api-key>' synaptic-codex-worker "
            "--url '{ws_url}/ws' --name {name} --workdir /path/to/repo --sandbox read-only"
        ),
        "note": (
            "Install Codex CLI first and confirm codex --version. The worker defaults to read-only; "
            "use workspace-write only for trusted tasks. Child environments exclude SYNAPTIC_* credentials."
        ),
    },
    "subprocess_worker": {
        "needs_install": True,
        "commands": ['pip install "git+https://github.com/Loagaeth/synaptic_lathe.git"'],
        "run": (
            "SYNAPTIC_API_KEY='<worker-api-key>' synaptic-subprocess-worker "
            "--url '{ws_url}/ws' --name {name} --command /absolute/path/to/allowed-command"
        ),
        "note": "The plan is passed as one argv item; no shell is used. Prefer profile_worker for multiple tools.",
    },
    "astrbot_http": {
        "needs_install": False,
        "commands": [],
        "config": {
            "type": "http_api",
            "base_url": "http://<astrbot-ip>:6185/api/v1/chat",
            "api_key": "<astrbot-api-key>",
            "stream": False,
        },
        "extra": {
            "provider": "",
            "model": "",
            "username": "",
        },
        "note": (
            "Create an AstrBot API key, then add this adapter under config.yaml agents. "
            "Use a non-default config_name when your AstrBot deployment requires one."
        ),
    },
}


class AgentResponseTooLargeError(ValueError):
    pass


def _protocol_message(msg_type: str, payload: dict, correlation_id: str) -> dict:
    from synapse.protocol import WS_PROTOCOL_VERSION

    return {
        "type": msg_type,
        "protocol_version": WS_PROTOCOL_VERSION,
        "payload": payload,
        "correlation_id": correlation_id,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _ensure_message_fits(message: dict, max_bytes: int) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > max_bytes:
        raise AgentResponseTooLargeError("HTTP agent response exceeded max_body_bytes")


async def _deliver_to_source(
    ws,
    source: str,
    message: dict,
    ttl: float,
    *,
    source_kind: str = "agent",
) -> None:
    if source_kind == "web":
        await ws.send_json(message)
        return
    if connection_manager.is_online(source):
        await connection_manager.send_or_queue(source, message, ttl=ttl)
        return
    try:
        await ws.send_json(message)
    except Exception:
        await connection_manager.send_or_queue(source, message, ttl=ttl)


def _parse_sse_result(text: str) -> str:
    result_parts: list[str] = []
    for event in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "plain":
            result_parts.append(str(payload.get("data", "")))
    return "".join(result_parts)


async def handle_http_send(
    cfg: GlobalConfig,
    ws,
    source: str,
    target: str,
    plan: str,
    timeout: int,
    cid: str,
    agent_cfg,
    payload: dict,
    *,
    persona: str = "",
    source_kind: str = "agent",
    purpose: str = "execute",
    title: str = "",
    profile: str = "",
    session_alias: str = "",
    group_id: str = "",
    precreated: bool = False,
) -> None:
    """Call one configured HTTP agent and return a bounded result to the source."""

    base = agent_cfg.base_url.rstrip("/")
    url = base if base.endswith("/api/v1/chat") else f"{base}/api/v1/chat"
    api_key = agent_cfg.api_key.get_secret_value()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    extra = dict(agent_cfg.extra)
    for key in ("provider", "model", "username"):
        if payload.get(key):
            extra[key] = payload[key]

    stream_enabled = payload.get("stream", agent_cfg.stream)
    body = {
        "message": plan,
        "username": extra.get("username") or "synaptic_lathe",
        "enable_streaming": bool(stream_enabled),
    }
    if extra.get("config_id") not in (None, ""):
        body["config_id"] = extra["config_id"]
    elif extra.get("config_name") not in (None, ""):
        body["config_name"] = extra["config_name"]
    if extra.get("provider"):
        body["selected_provider"] = extra["provider"]
    if extra.get("model"):
        body["selected_model"] = extra["model"]

    from synapse.task_queue import create_task as _create
    from synapse.task_queue import update_task_status as _update

    if precreated:
        task_id = cid
    else:
        task_id = await _create(
            cfg.db_path,
            source,
            target,
            plan,
            timeout=timeout,
            persona=persona,
            correlation_id=cid,
            source_kind=source_kind,
            purpose=purpose,
            title=title,
            profile=profile,
            session_alias=session_alias,
            group_id=group_id,
        )
    dispatched = await _update(cfg.db_path, task_id, "DISPATCHED", expected_statuses=("CREATED",))
    if not dispatched:
        return
    started = time.perf_counter()
    status: int | None = None
    synapse_logger.info(
        "http agent call started",
        extra={
            "event": "http_agent_call_started",
            "source": source,
            "target": target,
            "task_id": task_id,
            "timeout": timeout,
        },
    )

    try:
        client = ws.app.state.http_client
        response_bytes = bytearray()
        if hasattr(client, "stream"):
            async with client.stream("POST", url, json=body, headers=headers, timeout=timeout) as response:
                status = response.status_code
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    response_bytes.extend(chunk)
                    if len(response_bytes) > cfg.server.max_body_bytes:
                        raise AgentResponseTooLargeError("HTTP agent response exceeded max_body_bytes")
        else:  # Compatibility for injected clients used by embedders and tests.
            response = await client.post(url, json=body, headers=headers, timeout=timeout)
            status = response.status_code
            response.raise_for_status()
            response_bytes.extend(str(response.text).encode("utf-8"))
            if len(response_bytes) > cfg.server.max_body_bytes:
                raise AgentResponseTooLargeError("HTTP agent response exceeded max_body_bytes")
        response_text = response_bytes.decode("utf-8", errors="replace")
        result = sanitize_untrusted_text(_parse_sse_result(response_text) or response_text)
        result_message = _protocol_message(
            "task_result",
            {"task_id": task_id, "result": result},
            cid,
        )
        _ensure_message_fits(result_message, cfg.server.max_body_bytes)

        completed = await _update(
            cfg.db_path,
            task_id,
            "COMPLETED",
            result=result,
            expected_statuses=("DISPATCHED",),
        )
        if not completed:
            return
        synapse_logger.info(
            "http agent call completed",
            extra={
                "event": "http_agent_call_completed",
                "source": source,
                "target": target,
                "task_id": task_id,
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        await _deliver_to_source(
            ws,
            source,
            result_message,
            cfg.server.pending_message_ttl_hours * 3600,
            source_kind=source_kind,
        )
    except Exception as exc:
        timed_out = isinstance(exc, (httpx.TimeoutException, TimeoutError))
        terminal_status = "TIMEOUT" if timed_out else "ERROR"
        public_error = f"HTTP agent call timed out after {timeout}s" if timed_out else "HTTP agent call failed"
        if status is not None and not timed_out:
            public_error += f" with status {status}"
        if isinstance(exc, AgentResponseTooLargeError):
            public_error = str(exc)
        failed = await _update(
            cfg.db_path,
            task_id,
            terminal_status,
            result=public_error,
            expected_statuses=("DISPATCHED",),
        )
        if not failed:
            return
        synapse_logger.warning(
            "http agent call timed out" if timed_out else "http agent call failed",
            exc_info=True,
            extra={
                "event": "http_agent_call_timed_out" if timed_out else "http_agent_call_failed",
                "source": source,
                "target": target,
                "task_id": task_id,
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        await _deliver_to_source(
            ws,
            source,
            _protocol_message(
                "error",
                {"error": public_error, "code": "AGENT_CALL_FAILED"},
                cid,
            ),
            cfg.server.pending_message_ttl_hours * 3600,
            source_kind=source_kind,
        )


async def install_guide(request: Request, agent_type: str):
    guide = _INSTALL_GUIDES.get(agent_type)
    if not guide:
        known = ", ".join(_INSTALL_GUIDES)
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Unknown agent type: {agent_type}. Known: {known}",
                "code": "NOT_FOUND",
            },
        )
    http_url, ws_url = public_request_urls(request)
    if not http_url or not ws_url:
        raise HTTPException(status_code=400, detail={"error": "Invalid request host", "code": "INVALID_HOST"})
    result = dict(guide)
    if "run" in result:
        result["run"] = result["run"].format(
            http_url=http_url,
            ws_url=ws_url,
            name=agent_type,
        )
    return {"agent_type": agent_type, "guide": result}
