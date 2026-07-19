"""Web task dispatch controller for HTTP and WebSocket Agent endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException, Request

from synapse.config import GlobalConfig
from synapse.connection import connection_manager
from synapse.handlers import handle_http_send
from synapse.logging import synapse_logger
from synapse.task_events import probe_coordinator, task_events
from synapse.task_queue import cancel_task as cancel_persisted_task
from synapse.task_queue import create_task, get_task, update_task_status
from synapse.task_status import NONTERMINAL_TASK_STATUSES, TERMINAL_TASK_STATUSES


class _WebTaskSink:
    """Minimal HTTP-adapter delivery sink backed by the authenticated task stream."""

    def __init__(self, request: Request, task_id: str) -> None:
        self.app = request.app
        self.task_id = task_id

    async def send_json(self, message: dict[str, Any]) -> None:
        message_type = str(message.get("type") or "")
        fallback_status = "COMPLETED" if message_type == "task_result" else "ERROR"
        task = await get_task(self.app.state.config.db_path, self.task_id)
        status = str((task or {}).get("status") or fallback_status)
        event_name = {
            "COMPLETED": "task_completed",
            "TIMEOUT": "task_timeout",
            "CANCELLED": "task_cancelled",
        }.get(status, "task_error")
        await task_events.publish(
            {
                "event": event_name,
                "task_id": self.task_id,
                "group_id": (task or {}).get("group_id", ""),
                "status": status,
                "target": (task or {}).get("target_agent", ""),
                "profile": (task or {}).get("profile", ""),
                "purpose": (task or {}).get("purpose", "execute"),
            }
        )


class WebTaskController:
    """Coordinate authenticated human-originated tasks without a synthetic WS Agent."""

    def __init__(
        self,
        *,
        task_persona,
        bounded_timeout,
        build_ws_message,
        error_detail,
        pending_ttl_seconds,
        watch_task_timeout,
        cancel_timeout_watcher,
        available_agent_details,
        timeout_tasks: dict[str, tuple[str, asyncio.Task]],
        http_tasks: dict[str, asyncio.Task[None]],
    ) -> None:
        self._task_persona = task_persona
        self._bounded_timeout = bounded_timeout
        self._build_ws_message = build_ws_message
        self._error_detail = error_detail
        self._pending_ttl_seconds = pending_ttl_seconds
        self._watch_task_timeout = watch_task_timeout
        self._cancel_timeout_watcher = cancel_timeout_watcher
        self._available_agent_details = available_agent_details
        self.timeout_tasks = timeout_tasks
        self.http_tasks = http_tasks

    async def resolve_endpoint(
        self,
        request: Request,
        agent_name: str,
        profile: str,
        advisory_required: bool,
    ) -> dict[str, Any]:
        """Resolve a Web-selected endpoint without trusting client capability claims."""

        cfg: GlobalConfig = request.app.state.config
        if agent_name in cfg.agents:
            if profile:
                raise HTTPException(
                    status_code=422,
                    detail=self._error_detail("HTTP adapters do not expose local profiles", "INVALID_PROFILE"),
                )
            if advisory_required:
                raise HTTPException(
                    status_code=409,
                    detail=self._error_detail(
                        "This HTTP adapter does not advertise an enforced read-only advisory profile",
                        "ADVISORY_PROFILE_REQUIRED",
                    ),
                )
            return {"agent": agent_name, "profile": "", "type": "http_api", "timeout": 60}

        metadata = connection_manager.metadata_for(agent_name)
        if not metadata:
            raise HTTPException(
                status_code=409,
                detail=self._error_detail(f"Agent {agent_name!r} is not online", "TARGET_DISCONNECTED"),
            )
        client = metadata.get("client") if isinstance(metadata.get("client"), dict) else {}
        capabilities = client.get("profile_capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
        advertised_profiles = client.get("profiles")
        names = set(str(name) for name in advertised_profiles) if isinstance(advertised_profiles, list) else set()
        names.update(str(name) for name in capabilities)
        selected = profile or str(client.get("default_profile") or "")
        if not selected and len(names) == 1:
            selected = next(iter(names))
        if profile and selected not in names:
            raise HTTPException(
                status_code=422,
                detail=self._error_detail(
                    f"Profile {selected!r} is not advertised by {agent_name!r}", "INVALID_PROFILE"
                ),
            )
        if names and not selected:
            raise HTTPException(
                status_code=422,
                detail=self._error_detail("Select one advertised profile", "PROFILE_REQUIRED"),
            )
        profile_meta = capabilities.get(selected, {}) if selected else {}
        if not isinstance(profile_meta, dict):
            profile_meta = {}
        if advisory_required and (not selected or profile_meta.get("advisory_safe") is not True):
            raise HTTPException(
                status_code=409,
                detail=self._error_detail(
                    "Bids, planning, and self-assessment require a profile with advisory_safe: true",
                    "ADVISORY_PROFILE_REQUIRED",
                ),
            )
        suggested_timeout = (
            profile_meta.get("suggested_timeout") or profile_meta.get("timeout") or client.get("default_timeout") or 60
        )
        return {
            "agent": agent_name,
            "profile": selected,
            "type": "websocket",
            "timeout": self._bounded_timeout(suggested_timeout),
            "profile_capability": profile_meta,
        }

    async def _run_web_http_task(
        self,
        request: Request,
        *,
        task_id: str,
        target: str,
        plan: str,
        timeout: int,
        title: str,
        purpose: str,
        group_id: str,
        persona: str,
    ) -> None:
        cfg: GlobalConfig = request.app.state.config
        sink = _WebTaskSink(request, task_id)
        try:
            await handle_http_send(
                cfg,
                sink,
                "web-console",
                target,
                plan,
                timeout,
                task_id,
                cfg.agents[target],
                {},
                persona=persona,
                source_kind="web",
                purpose=purpose,
                title=title,
                group_id=group_id,
                precreated=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            changed = await update_task_status(
                cfg.db_path,
                task_id,
                "ERROR",
                result="HTTP Agent task failed before dispatch",
                expected_statuses=NONTERMINAL_TASK_STATUSES,
            )
            if changed:
                await sink.send_json(
                    self._build_ws_message(
                        "error", self._error_detail("HTTP Agent task failed", "AGENT_CALL_FAILED"), task_id
                    )
                )
            synapse_logger.exception(
                "web HTTP agent task failed",
                extra={"event": "web_http_task_failed", "target": target, "task_id": task_id},
            )

    async def dispatch_task(
        self,
        request: Request,
        *,
        target: str,
        profile: str,
        plan: str,
        timeout: int | None,
        title: str,
        session_alias: str,
        persona: str,
        purpose: str,
        group_id: str,
    ) -> dict[str, Any]:
        cfg: GlobalConfig = request.app.state.config
        endpoint = await self.resolve_endpoint(request, target, profile, purpose in {"bid", "plan", "tag"})
        selected_profile = str(endpoint.get("profile") or "")
        effective_timeout = self._bounded_timeout(timeout, int(endpoint.get("timeout") or 60))
        scoped_persona, persona_error = self._task_persona(cfg, {"persona": persona})
        if persona_error:
            raise HTTPException(status_code=422, detail=self._error_detail(persona_error, "VALIDATION_ERROR"))
        if len(plan.encode("utf-8")) > cfg.server.max_body_bytes:
            raise HTTPException(
                status_code=413, detail=self._error_detail("Task plan is too large", "PAYLOAD_TOO_LARGE")
            )

        task_id = await create_task(
            cfg.db_path,
            "web-console",
            target,
            plan,
            timeout=effective_timeout,
            persona=scoped_persona,
            source_kind="web",
            purpose=purpose,
            title=title,
            profile=selected_profile,
            session_alias=session_alias,
            group_id=group_id,
        )

        if endpoint["type"] == "http_api":
            job = asyncio.create_task(
                self._run_web_http_task(
                    request,
                    task_id=task_id,
                    target=target,
                    plan=plan,
                    timeout=effective_timeout,
                    title=title,
                    purpose=purpose,
                    group_id=group_id,
                    persona=scoped_persona,
                ),
                name=f"web-http-task-{task_id}",
            )
            self.http_tasks[task_id] = job
            job.add_done_callback(lambda completed, current_id=task_id: self.http_tasks.pop(current_id, None))
            status = "CREATED"
            queued = False
        else:
            queued_status = await update_task_status(
                cfg.db_path,
                task_id,
                "QUEUED",
                expected_statuses=("CREATED",),
            )
            if not queued_status:
                raise HTTPException(
                    status_code=409, detail=self._error_detail("Task state changed", "INVALID_TASK_STATE")
                )
            task_payload: dict[str, Any] = {
                "task_id": task_id,
                "plan": plan,
                "from": "web-console",
                "timeout": effective_timeout,
            }
            if selected_profile:
                task_payload["profile"] = selected_profile
            if session_alias:
                task_payload["session_id"] = session_alias
            if scoped_persona:
                task_payload["persona"] = scoped_persona
            watcher = asyncio.create_task(
                self._watch_task_timeout(cfg, task_id, effective_timeout, "web-console", target, task_id),
                name=f"task-timeout-{task_id}",
            )
            self.timeout_tasks[task_id] = ("web-console", watcher)
            delivered = await connection_manager.send_or_queue(
                target,
                self._build_ws_message("task", task_payload, task_id),
                ttl=self._pending_ttl_seconds(cfg),
            )
            if delivered:
                await update_task_status(
                    cfg.db_path,
                    task_id,
                    "DISPATCHED",
                    expected_statuses=("QUEUED",),
                )
            status = "DISPATCHED" if delivered else "QUEUED"
            queued = not delivered

        await task_events.publish(
            {
                "event": "task_created",
                "task_id": task_id,
                "group_id": group_id,
                "status": status,
                "target": target,
                "profile": selected_profile,
                "purpose": purpose,
            }
        )
        synapse_logger.info(
            "web task dispatched",
            extra={
                "event": "web_task_dispatched",
                "source": "web-console",
                "target": target,
                "task_id": task_id,
                "profile": selected_profile,
                "purpose": purpose,
                "timeout": effective_timeout,
                "queued": queued,
            },
        )
        return {
            "task_id": task_id,
            "status": status,
            "target": target,
            "profile": selected_profile,
            "purpose": purpose,
            "group_id": group_id,
            "timeout": effective_timeout,
        }

    async def cancel_task(self, request: Request, task_id: str, reason: str) -> dict[str, Any]:
        cfg: GlobalConfig = request.app.state.config
        clean_reason = " ".join(reason.split())[:500]
        if not clean_reason:
            raise HTTPException(
                status_code=422, detail=self._error_detail("Cancellation reason is required", "VALIDATION_ERROR")
            )
        before = await get_task(cfg.db_path, task_id)
        if not before:
            raise HTTPException(status_code=404, detail=self._error_detail("Task not found", "NOT_FOUND"))
        if before.get("status") in TERMINAL_TASK_STATUSES:
            raise HTTPException(
                status_code=409, detail=self._error_detail("Task is already terminal", "INVALID_TASK_STATE")
            )
        cancelled = await cancel_persisted_task(cfg.db_path, task_id, clean_reason)
        if not cancelled or cancelled.get("status") != "CANCELLED":
            raise HTTPException(status_code=409, detail=self._error_detail("Task state changed", "INVALID_TASK_STATE"))
        await self._cancel_timeout_watcher(task_id)
        target = str(cancelled.get("target_agent") or "")
        connection_manager.remove_pending_task(target, task_id)
        job = self.http_tasks.pop(task_id, None)
        if job and not job.done():
            job.cancel()
        await connection_manager.send_if_online(
            target,
            self._build_ws_message("cancel", {"task_id": task_id, "reason": "cancelled_by_administrator"}, task_id),
        )
        await task_events.publish(
            {
                "event": "task_cancelled",
                "task_id": task_id,
                "group_id": cancelled.get("group_id", ""),
                "status": "CANCELLED",
                "target": target,
                "profile": cancelled.get("profile", ""),
                "purpose": cancelled.get("purpose", "execute"),
            }
        )
        synapse_logger.info(
            "web task cancelled",
            extra={
                "event": "web_task_cancelled",
                "target": target,
                "task_id": task_id,
                "reason_length": len(clean_reason),
            },
        )
        return {"task_id": task_id, "status": "CANCELLED", "cancel_reason": clean_reason}

    async def probe_agents(self, request: Request, targets: list[str], timeout: float) -> dict[str, Any]:
        details = {item["name"]: item for item in connection_manager.online_agent_details()}
        requested = list(dict.fromkeys(targets)) if targets else sorted(details)
        offline = [name for name in requested if name not in details]
        supported = [
            name for name in requested if name in details and "probe" in (details[name].get("capabilities") or [])
        ]
        unsupported = [name for name in requested if name in details and name not in supported]
        probe_id = probe_coordinator.create(supported)
        message = self._build_ws_message("probe", {"probe_id": probe_id}, probe_id)
        try:
            if not targets:
                await connection_manager.broadcast(message)
            else:
                for name in supported:
                    await connection_manager.send_if_online(name, message)
            acknowledgements = await probe_coordinator.collect(probe_id, timeout)
        finally:
            probe_coordinator.discard(probe_id)

        results = []
        for name in requested:
            if name in acknowledgements:
                result = dict(acknowledgements[name])
                result["profiles"] = (details[name].get("client") or {}).get("profiles", [])
                results.append(result)
            elif name in offline:
                results.append({"agent": name, "ok": False, "status": "offline"})
            elif name in unsupported:
                results.append({"agent": name, "ok": False, "status": "probe_unsupported"})
            else:
                results.append({"agent": name, "ok": False, "status": "timeout"})
        return {"probe_id": probe_id, "results": results}

    def agent_details(self, request: Request) -> dict[str, Any]:
        return self._available_agent_details(request.app.state.config)
