from __future__ import annotations

import asyncio
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from synapse.config import AgentConfig, FabricConfig, GlobalConfig
from synapse.connection import connection_manager
from synapse.context.prompts import set_prompt
from synapse.db import init_db
from synapse.server import app, set_project_root

pytest.importorskip("mcp")


@pytest.fixture
def fabric_client(tmp_path):
    admin, worker, alice, bob = [secrets.token_urlsafe(32) for _ in range(4)]
    config = GlobalConfig(
        db_path=str(tmp_path / "fabric.db"),
        server={"api_key": admin, "worker_api_key": worker, "cors_origins": ["http://testserver"]},
        fabric={
            "enabled": True,
            "clients": {
                "alice": {
                    "token": alice,
                    "capabilities": ["local/codex"],
                    "submit_tasks": True,
                    "resources": ["synapse://prompts/team-guide"],
                },
                "bob": {"token": bob},
            },
        },
    )
    config.server.rate_limit_max = 10_000
    app.state.config = config
    app.state.config_path = str(tmp_path / "config.yaml")
    set_project_root(app.state.config_path)
    asyncio.run(init_db(config.db_path))
    asyncio.run(set_prompt(config.db_path, "team-guide", "Untrusted shared guidance"))
    asyncio.run(set_prompt(config.db_path, "private", "Do not reveal this document"))
    connection_manager._connections.clear()
    connection_manager._metadata.clear()
    connection_manager._pending.clear()
    with TestClient(app) as client:
        yield client, {"admin": admin, "worker": worker, "alice": alice, "bob": bob}
    connection_manager._connections.clear()
    connection_manager._metadata.clear()
    connection_manager._pending.clear()


def _rpc(client, token, method, params=None, **headers):
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
            **headers,
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    return response


def _tool(client, token, name, arguments=None):
    response = _rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def _receive(worker):
    while True:
        value = worker.receive_json()
        if value["type"] != "ping":
            return value
        worker.send_json({"type": "pong"})


def _register(worker):
    worker.send_json(
        {
            "type": "register",
            "payload": {
                "agent_name": "local",
                "client": {
                    "profiles": ["codex", "private"],
                    "default_profile": "private",
                    "profile_capabilities": {"codex": {"suggested_timeout": 120, "tags": ["review"]}},
                },
            },
        }
    )
    assert _receive(worker)["type"] == "registered"


def test_fabric_auth_and_origin_are_independent(fabric_client):
    client, keys = fabric_client
    for token in ("", keys["admin"], keys["worker"]):
        assert _rpc(client, token, "tools/list").status_code == 401
    assert _rpc(client, keys["alice"], "tools/list", Origin="https://untrusted.invalid").status_code == 403
    assert _rpc(client, keys["alice"], "tools/list", Host="untrusted.invalid").status_code == 421
    assert client.get("/admin/capabilities", headers={"Authorization": f"Bearer {keys['alice']}"}).status_code == 403
    result = _rpc(
        client,
        keys["alice"],
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "integration-test", "version": "1"},
        },
    )
    assert result.status_code == 200
    assert result.json()["result"]["serverInfo"]["name"] == "Synaptic Lathe"


def test_fabric_permissions_and_resource_isolation(fabric_client):
    client, keys = fabric_client
    tools = _rpc(client, keys["bob"], "tools/list").json()["result"]["tools"]
    assert "tasks_submit" not in [tool["name"] for tool in tools]
    result = _tool(client, keys["bob"], "tasks_submit", {"capability": "local/codex", "plan": "no"})
    assert result["isError"] is True
    allowed = _tool(client, keys["alice"], "context_read", {"uri": "synapse://prompts/team-guide"})
    assert allowed["structuredContent"]["content"] == "Untrusted shared guidance"
    for caller in ("alice", "bob"):
        denied = _tool(client, keys[caller], "context_read", {"uri": "synapse://prompts/private"})
        assert denied["isError"] is True
        assert "Do not reveal" not in json.dumps(denied)
    resources = _rpc(client, keys["bob"], "resources/list").json()["result"]["resources"]
    assert resources == []
    resource = _rpc(client, keys["alice"], "resources/read", {"uri": "synapse://prompts/team-guide"})
    assert "Untrusted shared guidance" in resource.text
    found = _tool(client, keys["alice"], "context_search", {"query": "reveal"})
    assert found["structuredContent"]["results"] == []
    invalid = _tool(
        client,
        keys["alice"],
        "tasks_submit",
        {
            "capability": "local/codex",
            "plan": "no",
            "source": "mcp:bob",
        },
    )
    assert invalid["isError"] is True
    assert "mcp:bob" not in json.dumps(invalid)


def test_mcp_annotations_do_not_claim_execution_is_non_destructive(fabric_client):
    client, keys = fabric_client
    tools = _rpc(client, keys["alice"], "tools/list").json()["result"]["tools"]
    for tool in tools:
        mutates = tool["name"] in {"tasks_submit", "tasks_cancel"}
        assert tool["annotations"]["destructiveHint"] is mutates
        assert tool["annotations"]["readOnlyHint"] is (not mutates)


def test_fabric_ws_task_uses_shared_bus_and_owner(fabric_client):
    client, keys = fabric_client
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {keys['worker']}"}) as worker:
        _register(worker)
        catalog = _tool(client, keys["alice"], "capabilities_list")["structuredContent"]["capabilities"]
        assert [record["id"] for record in catalog] == ["local/codex"]
        assert _tool(client, keys["bob"], "capabilities_list")["structuredContent"]["capabilities"] == []
        denied = _tool(client, keys["alice"], "tasks_submit", {"capability": "local/private", "plan": "no"})
        assert denied["isError"] is True
        created = _tool(
            client,
            keys["alice"],
            "tasks_submit",
            {
                "capability": "local/codex",
                "plan": "test plan",
            },
        )["structuredContent"]
        assert created["timeout"] == 120
        task_id = created["task_id"]
        message = _receive(worker)
        assert message["payload"]["profile"] == "codex"
        assert message["payload"]["from"] == "mcp:alice"
        assert message["payload"]["task_id"] == task_id
        for operation in ("tasks_get", "tasks_cancel"):
            args = {"task_id": task_id}
            if operation == "tasks_cancel":
                args["reason"] = "unauthorized"
            assert _tool(client, keys["bob"], operation, args)["isError"] is True
        worker.send_json({"type": "accept", "correlation_id": task_id})
        worker.send_json(
            {
                "type": "return",
                "correlation_id": task_id,
                "payload": {"task_id": task_id, "result": "done", "output_truncated": True},
            }
        )
        deadline = time.monotonic() + 2
        while True:
            task = _tool(client, keys["alice"], "tasks_get", {"task_id": task_id})["structuredContent"]
            if task["status"] == "COMPLETED":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert task["result"] == "done"
        assert task["output_truncated"] is True
        admin_headers = {"Authorization": f"Bearer {keys['admin']}"}
        assert client.get(f"/admin/tasks/{task_id}", headers=admin_headers).json()["source_kind"] == "api"
        assert any(t["id"] == task_id for t in client.get("/admin/tasks", headers=admin_headers).json()["tasks"])
        assert "mcp:alice" not in connection_manager._pending


def test_fabric_cancellation_reaches_worker(fabric_client):
    client, keys = fabric_client
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {keys['worker']}"}) as worker:
        _register(worker)
        task_id = _tool(
            client,
            keys["alice"],
            "tasks_submit",
            {
                "capability": "local/codex",
                "plan": "test",
            },
        )["structuredContent"]["task_id"]
        _receive(worker)
        cancelled = _tool(client, keys["alice"], "tasks_cancel", {"task_id": task_id, "reason": "stop"})
        assert cancelled["structuredContent"]["status"] == "CANCELLED"
        assert _receive(worker)["type"] == "cancel"


@pytest.mark.parametrize("capability", ["*", "local/*", "../local", "local/codex/extra"])
def test_fabric_rejects_wildcard_or_ambiguous_grants(capability):
    with pytest.raises(ValidationError):
        FabricConfig(clients={"client": {"token": "x" * 32, "capabilities": [capability]}})


def test_fabric_tokens_cannot_escalate_to_admin_or_worker():
    with pytest.raises(ValidationError):
        GlobalConfig(server={"api_key": "x" * 32}, fabric={"clients": {"client": {"token": "x" * 32}}})
    with pytest.raises(ValidationError):
        FabricConfig(enabled=True)
    with pytest.raises(ValidationError):
        FabricConfig(clients={"one": {"token": "x" * 32}, "two": {"token": "x" * 32}})


@pytest.mark.parametrize("token", ["x" * 31, "x" * 513, "x" * 32 + "\x00", "x" * 32 + "\n", "x" * 32 + ":"])
def test_fabric_rejects_unusable_bearer_tokens(token):
    with pytest.raises(ValidationError) as error:
        FabricConfig(clients={"client": {"token": token}})
    assert "x" * 31 not in str(error.value)


def test_official_sdk_can_initialize_list_and_read(fabric_client):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    client, keys = fabric_client

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": f"Bearer {keys['alice']}"},
        ) as http:
            async with streamable_http_client("http://testserver/mcp", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    assert (await session.initialize()).serverInfo.name == "Synaptic Lathe"
                    assert "capabilities_list" in [tool.name for tool in (await session.list_tools()).tools]
                    assert len((await session.list_resources()).resources) == 1
                    result = await session.call_tool("context_read", {"uri": "synapse://prompts/team-guide"})
                    assert result.structuredContent["content"] == "Untrusted shared guidance"

    client.portal.call(scenario)


def test_concurrent_clients_do_not_share_permission_context(fabric_client):
    client, keys = fabric_client

    def read(caller):
        return _tool(client, keys[caller], "context_read", {"uri": "synapse://prompts/team-guide"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        callers = ["alice", "bob"] * 8
        results = list(pool.map(read, callers))
    for caller, result in zip(callers, results, strict=True):
        assert bool(result.get("isError")) is (caller == "bob")


def test_mcp_http_adapter_result_uses_same_task_store(fabric_client, monkeypatch):
    client, keys = fabric_client
    config = client.app.state.config
    config.agents["http-test"] = AgentConfig(type="http_api", base_url="https://agent.invalid")
    config.fabric.clients["alice"].capabilities.append("http-test")

    class FakeHTTPClient:
        async def post(self, url, **kwargs):
            return httpx.Response(
                200, text='data: {"type":"plain","data":"adapter result"}\n', request=httpx.Request("POST", url)
            )

    monkeypatch.setattr(app.state, "http_client", FakeHTTPClient())
    task_id = _tool(
        client,
        keys["alice"],
        "tasks_submit",
        {
            "capability": "http-test",
            "plan": "adapter test",
        },
    )["structuredContent"]["task_id"]
    deadline = time.monotonic() + 2
    while True:
        result = _tool(client, keys["alice"], "tasks_get", {"task_id": task_id})["structuredContent"]
        if result["status"] == "COMPLETED":
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert result["result"] == "adapter result"
    assert "mcp:alice" not in connection_manager._pending


def test_resources_are_paged_without_silent_truncation(fabric_client):
    client, keys = fabric_client
    content = "a" * 32_000 + "tail"
    client.portal.call(set_prompt, app.state.config.db_path, "team-guide", content)
    first = _tool(
        client,
        keys["alice"],
        "context_read",
        {
            "uri": "synapse://prompts/team-guide",
        },
    )["structuredContent"]
    assert first["content"] == "a" * 32_000
    assert first["next_offset"] == 32_000
    second = _tool(
        client,
        keys["alice"],
        "context_read",
        {
            "uri": "synapse://prompts/team-guide",
            "offset": first["next_offset"],
        },
    )["structuredContent"]
    assert second["content"] == "tail"
    assert second["next_offset"] is None


def test_enabled_fabric_cannot_bypass_policy_through_public_rest():
    config = {"enabled": True, "clients": {"alice": {"token": "x" * 32}}}
    with pytest.raises(ValidationError):
        GlobalConfig(fabric=config)
    with pytest.raises(ValidationError):
        GlobalConfig(server={"api_key": "different", "public_read_context": True}, fabric=config)


def test_disabled_gateway_has_no_authless_fallback(fabric_client):
    client, keys = fabric_client
    client.app.state.config.fabric.enabled = False
    assert _rpc(client, keys["alice"], "tools/list").status_code == 404


def test_registry_revision_and_snapshot_are_detached(fabric_client):
    from synapse.capabilities import capability_records

    client, keys = fabric_client
    with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {keys['worker']}"}) as worker:
        _register(worker)
        config = client.app.state.config
        before = capability_records(config)
        connection_manager._metadata["local"]["last_seen"] += 1
        after = capability_records(config)
        assert [record["revision"] for record in before] == [record["revision"] for record in after]
        before[0]["declared"]["tags"].append("forged")
        assert "forged" not in capability_records(config)[0]["declared"]["tags"]

        config.agents["local"] = AgentConfig(type="http_api", base_url="https://agent.invalid")
        connection_manager._metadata["local"]["client"]["default_timeout"] = 1800
        records = capability_records(config)
        assert len(records) == 1
        assert records[0]["id"] == "local"
        assert records[0]["transport"] == "http"
        assert records[0]["timeout_hint"] == 60
