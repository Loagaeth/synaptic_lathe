"""Permission-scoped access to the existing catalog, task bus, and documents."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from synapse.capabilities import capability_records
from synapse.config import FabricClient
from synapse.context.persona import get_persona
from synapse.context.prompts import get_prompt
from synapse.context.skills import get_skill
from synapse.task_queue import get_task

_DOCUMENT_READERS = {"skills": get_skill, "prompts": get_prompt, "personas": get_persona}
_MAX_TEXT = 32_000


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CatalogQuery(StrictRequest):
    query: str = Field(default="", max_length=128)


class SubmitTask(StrictRequest):
    capability: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}(?:/[A-Za-z0-9_-]{1,64})?$")
    plan: str = Field(min_length=1, max_length=200_000)
    title: str = Field(default="", max_length=128)
    timeout: int | None = Field(default=None, ge=1, le=3600)


class TaskQuery(StrictRequest):
    task_id: str = Field(min_length=1, max_length=128)


class CancelTask(TaskQuery):
    reason: str = Field(min_length=1, max_length=500)


class ReadContext(StrictRequest):
    uri: str = Field(min_length=1, max_length=256)
    offset: int = Field(default=0, ge=0, le=16_777_216)


class SearchContext(StrictRequest):
    query: str = Field(min_length=1, max_length=128)
    kind: Literal["skills", "prompts", "personas"] | None = None
    limit: int = Field(default=10, ge=1, le=50)


class FabricService:
    """Bind one authenticated principal to one request, without global sessions."""

    def __init__(self, request: Request, controller, name: str, policy: FabricClient) -> None:
        self.request = request
        self.controller = controller
        self.name = name
        self.policy = policy

    def capabilities_list(self, query: str = "") -> dict:
        allowed = set(self.policy.capabilities)
        records = [
            {**record, "execution_allowed": self.policy.submit_tasks}
            for record in capability_records(self.request.app.state.config)
            if record["id"] in allowed
            and query.casefold() in (record["id"] + " " + " ".join(record["declared"]["tags"])).casefold()
        ]
        return {"capabilities": records, "input_schema": SubmitTask.model_json_schema()}

    async def tasks_submit(self, body: SubmitTask) -> dict:
        if not self.policy.submit_tasks or body.capability not in self.policy.capabilities:
            raise HTTPException(403, "Capability execution is not permitted")
        record = next(
            (item for item in self.capabilities_list()["capabilities"] if item["id"] == body.capability), None
        )
        if record is None:
            raise HTTPException(404, "Capability is not available")
        return await self.controller.dispatch_task(
            self.request,
            target=record["agent"],
            profile=record["profile"],
            plan=body.plan,
            timeout=body.timeout,
            title=body.title,
            session_alias="",
            persona="",
            purpose="execute",
            group_id="",
            source=f"mcp:{self.name}",
            source_kind="api",
        )

    async def _owned_task(self, task_id: str) -> dict:
        task = await get_task(self.request.app.state.config.db_path, task_id)
        if not task or task["source_kind"] != "api" or task["source_agent"] != f"mcp:{self.name}":
            raise HTTPException(404, "Task not found")
        return task

    async def tasks_get(self, task_id: str) -> dict:
        task = await self._owned_task(task_id)
        result = str(task.get("result") or "")
        return {
            "task_id": task["id"],
            "status": task["status"],
            "target": task["target_agent"],
            "profile": task["profile"],
            "result": result[:_MAX_TEXT],
            "output_truncated": bool(task.get("output_truncated")) or len(result) > _MAX_TEXT,
        }

    async def tasks_cancel(self, task_id: str, reason: str) -> dict:
        await self._owned_task(task_id)
        return await self.controller.cancel_task(self.request, task_id, reason)

    async def _document(self, uri: str) -> dict:
        if uri not in self.policy.resources:
            raise HTTPException(404, "Resource not found")
        kind, name = uri.removeprefix("synapse://").split("/", 1)
        document = await _DOCUMENT_READERS[kind](self.request.app.state.config.db_path, name)
        if document is None:
            raise HTTPException(404, "Resource not found")
        return document

    async def context_read(self, uri: str, offset: int = 0) -> dict:
        document = await self._document(uri)
        content = str(document.get("content") or "")
        end = min(offset + _MAX_TEXT, len(content))
        return {
            "uri": uri,
            "content": content[offset:end],
            "offset": offset,
            "next_offset": end if end < len(content) else None,
            "untrusted": True,
        }

    async def context_search(self, body: SearchContext) -> dict:
        matches = []
        for uri in self.policy.resources:
            if body.kind and not uri.startswith(f"synapse://{body.kind}/"):
                continue
            try:
                document = await self._document(uri)
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            content = str(document.get("content") or "")
            if body.query.casefold() in (uri + "\n" + content).casefold():
                matches.append({"uri": uri, "preview": content[:500]})
            if len(matches) >= body.limit:
                break
        return {"results": matches, "untrusted": True}
