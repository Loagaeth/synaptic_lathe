"""SynapticLathe 冒烟测试 — 核心 API 端点。"""

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from synapse.config import AgentConfig, GlobalConfig, MemoryConfig
from synapse.db import init_db
from synapse.server import app, set_project_root


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app.state.config = GlobalConfig(db_path=db_path)
    app.state.config_path = "config.yaml"
    set_project_root(str(tmp_path))
    import asyncio

    asyncio.run(init_db(db_path))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(tmp_path):
    """带 API key 的 client"""
    from pydantic import SecretStr

    db_path = str(tmp_path / "test.db")
    cfg = GlobalConfig(db_path=db_path)
    cfg.server.api_key = SecretStr("test-key")
    app.state.config = cfg
    app.state.config_path = "config.yaml"
    set_project_root(str(tmp_path))
    import asyncio

    asyncio.run(init_db(db_path))
    with TestClient(app) as test_client:
        yield test_client


# ── 认证测试 ──


def test_auth_missing_key_rejected(auth_client):
    r = auth_client.post("/admin/memory", json={"content": "x"})
    assert r.status_code == 403


def test_auth_wrong_key_rejected(auth_client):
    r = auth_client.post("/admin/memory", json={"content": "x"}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 403


def test_auth_valid_key_accepted(auth_client):
    r = auth_client.post("/admin/memory", json={"content": "x"}, headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200


def test_api_v1_admin_write_has_same_csrf_boundary(auth_client):
    headers = {
        "Authorization": "Bearer test-key",
        "Origin": "https://untrusted.example",
    }
    response = auth_client.post("/api/v1/admin/memory", json={"content": "x"}, headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "CSRF_BLOCKED"


# ── 安全测试 ──


def test_path_traversal_blocked(client):
    r = client.post("/admin/skill", json={"name": "x", "file": "../../etc/passwd"})
    assert r.status_code == 400
    assert "data/" in r.json()["detail"]["error"]


def test_path_traversal_absolute_blocked(client):
    r = client.post("/admin/skill", json={"name": "x", "file": "/etc/passwd"})
    assert r.status_code == 400


# ── 404 测试 ──


def test_delete_nonexistent_memory(client):
    r = client.delete("/admin/memory?id=99999")
    assert r.status_code == 404


def test_delete_nonexistent_skill(client):
    r = client.delete("/admin/skill?name=nonexistent")
    assert r.status_code == 404


def test_skill_not_found(client):
    r = client.get("/context/skills?name=nonexistent")
    assert r.status_code == 404


# ── 原有测试 ──


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_version_and_api_v1_aliases(client):
    r = client.get("/version")
    assert r.status_code == 200
    data = r.json()
    assert data["server"] == "SynapticLathe"
    assert data["api_version"] == "v1"
    assert data["api_prefix"] == "/api/v1"
    assert data["ws_protocol_version"] == 1
    assert "/api/v1/ws" in data["ws_paths"]

    r = client.get("/api/v1/version")
    assert r.status_code == 200
    assert r.json()["api_prefix"] == "/api/v1"

    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["api_version"] == "v1"

    r = client.get("/api/v1/context")
    assert r.status_code == 200
    assert "agents" in r.json()


def test_websocket_protocol_hello_register_and_discovery(client):
    from synapse.connection import connection_manager

    asyncio.run(connection_manager.unregister("proto-worker-test"))
    try:
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.send_json({"type": "hello", "correlation_id": "hello-cid"})
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["protocol_version"] == 1
            assert hello["correlation_id"] == "hello-cid"
            assert hello["payload"]["api_prefix"] == "/api/v1"

            ws.send_json(
                {
                    "type": "register",
                    "payload": {
                        "agent_name": "proto-worker-test",
                        "protocol_version": 1,
                        "client": {"name": "pytest-worker", "version": "0", "capabilities": ["task"]},
                    },
                }
            )
            registered = ws.receive_json()
            assert registered["type"] == "registered"
            assert registered["protocol_version"] == 1
            assert registered["payload"]["api_prefix"] == "/api/v1"
            assert registered["payload"]["accepted_protocol_version"] == 1
            assert "ws connected" in registered["payload"]["banner"]

            r = client.get("/context/agents")
            assert r.status_code == 200
            available = {item["name"]: item for item in r.json()["available"]}
            worker = available["proto-worker-test"]
            assert worker["protocol_version"] == 1
            assert worker["client"]["name"] == "pytest-worker"
            assert worker["capabilities"] == ["task"]
    finally:
        asyncio.run(connection_manager.unregister("proto-worker-test"))


def test_websocket_rejects_unsupported_protocol(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "register", "payload": {"agent_name": "bad-proto-test", "protocol_version": 999}})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["payload"]["code"] == "UNSUPPORTED_PROTOCOL"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_context_empty(client):
    r = client.get("/context")
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["memories"], list)
    assert isinstance(d["skills"], list)
    assert isinstance(d["knowledge"], list)
    assert d["personas"] == []
    assert d["prompts"] == []
    assert d["agents"]["available"] == []
    assert "default_skills" in d


def test_memory_crud(client):
    r = client.post("/admin/memory", json={"content": "hello", "persona": "test"})
    assert r.status_code == 200
    mid = r.json()["id"]

    # persona forced to "shared" by memory.scope
    r = client.get("/context?persona=shared")
    assert len(r.json()["memories"]) >= 1

    r = client.delete(f"/admin/memory?id={mid}")
    assert r.status_code == 200

    r = client.get("/context?persona=shared")
    # memory was deleted, should be empty
    assert r.json()["memories"] == []


def test_skill_crud(client):
    r = client.post("/admin/skill", json={"name": "test-skill", "content": "hello"})
    assert r.status_code == 200

    r = client.get("/context/skills?name=test-skill")
    assert r.status_code == 200
    assert r.json()["name"] == "test-skill"

    r = client.delete("/admin/skill?name=test-skill")
    assert r.status_code == 200

    r = client.get("/context/skills?name=test-skill")
    assert r.status_code == 404


def test_persona_crud(client):
    r = client.post("/admin/persona", json={"name": "bot", "content": "you are bot"})
    assert r.status_code == 200

    r = client.get("/context/personas?name=bot")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert "you are bot" in r.json().get("content", "")

    r = client.delete("/admin/persona?name=bot")
    assert r.status_code == 200


def test_prompt_crud(client):
    r = client.post("/admin/prompt", json={"name": "usage-rule", "content": "keep it short"})
    assert r.status_code == 200

    r = client.get("/context/prompts")
    assert r.status_code == 200
    assert "usage-rule" in r.json()

    r = client.get("/context/prompts?detail=1")
    assert r.status_code == 200
    assert any(x["name"] == "usage-rule" and x["content"] == "keep it short" for x in r.json())

    r = client.get("/context/prompts?name=usage-rule")
    assert r.status_code == 200
    assert r.json()["content"] == "keep it short"

    r = client.delete("/admin/prompt?name=usage-rule")
    assert r.status_code == 200

    r = client.get("/context/prompts?name=usage-rule")
    assert r.status_code == 404


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        self.closed = True


def test_context_agents_include_configured_and_online_workers(client):
    from synapse.connection import connection_manager

    client.app.state.config.agents["astrbot"] = AgentConfig(type="http_api", base_url="http://127.0.0.1:8080")
    asyncio.run(connection_manager.unregister("local-dispatcher-test"))
    profile_caps = {
        "reasonix": {
            "name": "reasonix",
            "suggested_timeout": 1800,
            "max_output_bytes": 200000,
            "supports_session": False,
            "session_required": False,
            "session_aliases": [],
            "hints": ["avoid_short_timeout"],
        }
    }
    asyncio.run(
        connection_manager.register(
            "local-dispatcher-test",
            FakeWebSocket(),
            "test-cid",
            client={
                "name": "profile_worker",
                "profiles": ["reasonix"],
                "profile_capabilities": profile_caps,
                "default_profile": "reasonix",
                "api_key": "should-not-leak",
                "env": {"SECRET": "should-not-leak"},
            },
            capabilities=["task", "profile_dispatch"],
        )
    )
    try:
        r = client.get("/context/agents")
        assert r.status_code == 200
        data = r.json()
        available = {item["name"]: item for item in data["available"]}
        assert available["astrbot"]["source"] == "config"
        worker = available["local-dispatcher-test"]
        assert worker["source"] == "ws"
        assert worker["online"] is True
        assert worker["capabilities"] == ["task", "profile_dispatch"]
        assert worker["client"]["profile_capabilities"]["reasonix"]["suggested_timeout"] == 1800
        assert worker["client"]["profile_capabilities"]["reasonix"]["hints"] == ["avoid_short_timeout"]
        assert "api_key" not in worker["client"]
        assert "env" not in worker["client"]
        assert "should-not-leak" not in str(worker["client"])
    finally:
        asyncio.run(connection_manager.unregister("local-dispatcher-test"))


def test_connection_prompt_includes_dynamic_agents_and_prompt_endpoints(client):
    from synapse.connection import connection_manager

    asyncio.run(connection_manager.unregister("prompt-worker-test"))
    asyncio.run(connection_manager.register("prompt-worker-test", FakeWebSocket(), "prompt-cid"))
    try:
        r = client.get("/connection-prompt")
        assert r.status_code == 200
        prompt = r.json()
        assert "prompt-worker-test" in prompt
        assert "/context/agents" in prompt
        assert "/context/prompts" in prompt
        assert "/admin/prompt" in prompt
        assert "reasonix" in prompt
        assert "timeout:1800" in prompt
    finally:
        asyncio.run(connection_manager.unregister("prompt-worker-test"))


def test_connection_prompt_post(client):
    r = client.post("/connection-prompt", json={"agent_name": "bot", "agent_type": "http_api"})
    assert r.status_code == 200
    assert "bot (http_api)" in r.json()


def test_resolve_target_uses_connection_manager_without_awaiting_bool(client):
    from synapse.connection import connection_manager
    from synapse.router import resolve_target

    asyncio.run(connection_manager.unregister("route-worker-test"))
    asyncio.run(connection_manager.register("route-worker-test", FakeWebSocket(), "route-cid"))
    try:
        result = asyncio.run(resolve_target("route-worker-test"))
        assert result["online"] is True
    finally:
        asyncio.run(connection_manager.unregister("route-worker-test"))


def test_context_requires_auth_when_key_configured(auth_client):
    r = auth_client.get("/admin/config")
    assert r.status_code == 403
    r = auth_client.get("/admin/config", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200


def test_context_read_requires_auth_by_default_when_key_configured(auth_client):
    r = auth_client.get("/context")
    assert r.status_code == 403


def test_context_public_flag_allows_read(auth_client):
    auth_client.app.state.config.server.public_read_context = True
    r = auth_client.get("/context")
    assert r.status_code == 200


def test_public_context_does_not_allow_anonymous_embedding_search(auth_client):
    auth_client.app.state.config.server.public_read_context = True
    response = auth_client.post("/context/memory", json={"query": "costly"})
    assert response.status_code == 403


def test_admin_logs_require_auth_and_redact_secrets(auth_client):
    import json

    from synapse.logging import synapse_logger

    marker = "web-log-test-marker Bearer secret-token sk-testsecret123"
    synapse_logger.warning(marker)
    for handler in synapse_logger.handlers:
        handler.flush()

    r = auth_client.get("/admin/logs")
    assert r.status_code == 403

    r = auth_client.get("/admin/logs?limit=50", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "logs/synaptic_lathe.log"
    assert isinstance(body["logs"], list)

    text = json.dumps(body["logs"], ensure_ascii=False)
    assert "web-log-test-marker" in text
    assert "secret-token" not in text
    assert "sk-testsecret123" not in text
    assert "Bearer ***" in text
    assert "sk-***" in text


def test_admin_log_stream_requires_auth(auth_client):
    r = auth_client.get("/admin/logs/stream")
    assert r.status_code == 403


def test_request_logs_include_http_fields(auth_client):
    from synapse.logging import synapse_logger

    r = auth_client.get("/health?check=db")
    assert r.status_code == 200
    for handler in synapse_logger.handlers:
        handler.flush()

    r = auth_client.get("/admin/logs?limit=20", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200
    logs = r.json()["logs"]
    matches = [item for item in logs if item.get("event") == "http_request" and item.get("path") == "/health"]
    assert matches
    assert any(item.get("method") == "GET" and item.get("status") == 200 for item in matches)
    assert all("duration_ms" in item for item in matches)
    assert all("check=db" not in item.get("msg", "") for item in matches)


def test_connection_prompt(client):
    r = client.get("/connection-prompt")
    assert r.status_code == 200
    assert "SynapticLathe" in r.text or "突触凝练机" in r.text


def test_connection_prompt_stays_concise_and_complete(client):
    r = client.get("/connection-prompt")
    assert r.status_code == 200
    prompt = r.json()
    assert len(prompt) < 7000
    assert "禁止泄露" in prompt
    assert "禁止请求或返回二进制" in prompt
    assert "/context" in prompt
    assert "/context/agents" in prompt
    assert "/context/prompts" in prompt
    assert "/admin/memory" in prompt
    assert "/admin/prompt" in prompt
    assert '"type":"send"' in prompt
    assert '"type":"return"' in prompt
    assert "--max-output-bytes" in prompt
    assert "output_truncated" in prompt
    assert "不可信数据" in prompt
    assert "广播是瞬时" in prompt
    assert "不要把记忆或提示词文档当作临时大文件传输层" in prompt
    assert "超过 2000 字先写入" not in prompt
    assert "<</return>>" in prompt
    assert "<< /return>>" not in prompt


def test_web_index_exposes_theme_and_management_controls():
    web_dir = Path(__file__).resolve().parents[1] / "synapse" / "web"
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    css = (web_dir / "styles.css").read_text(encoding="utf-8")
    javascript = (web_dir / "app.js").read_text(encoding="utf-8")
    document = html + css + javascript

    assert 'href="/web/styles.css"' in html
    assert 'src="/web/app.js"' in html
    html = document
    assert 'body[data-theme="light"]' in html
    assert "toggleTheme()" in html
    assert "searchMemory()" in html
    assert "searchKnowledge()" in html
    assert "chunk=true" in html
    assert "/install/" in html
    assert "copySlots" in html
    assert "deleteSlots" in html
    assert "/context/agents" in html
    assert "/context/prompts" in html
    assert "/admin/prompt" in html
    assert "/admin/logs" in html
    assert "/admin/logs/stream" in html
    assert 'data-tab="logs"' in html
    assert 'data-tab="agents"' in html
    assert 'data-tab="tasks"' in html
    assert "ragents()" in html
    assert "/admin/tasks" in html
    assert "/admin/auctions" in html
    assert "/admin/teams" in html
    assert "/admin/agents/probe" in html
    assert "profile_capabilities" in html
    assert "agentCallPayload" in html
    assert "startLogStream()" in html
    assert "showAuthRequired" in html
    assert "saveAuthToken" in html
    assert "server.api_key" in html
    assert "prompt(msg" not in html
    assert "duration_ms" in html
    assert "task_id" in html
    assert "exit_code" in html
    assert "不记录 query" in html
    assert "参数只影响本次生成" in html
    assert "del('${kind}','${e(id)}')" not in html


def test_admin_config(client):
    r = client.get("/admin/config")
    assert r.status_code == 200
    assert "config" in r.json()


# ── 新增安全/质量回归测试 ──


def test_full_context_requires_auth_when_public_read_disabled(auth_client):
    auth_client.app.state.config.server.public_read_context = False
    r = auth_client.get("/context")
    assert r.status_code == 403
    r = auth_client.get("/context", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200


def test_debug_memory_config_requires_auth(auth_client):
    r = auth_client.get("/_debug/memory_config")
    assert r.status_code == 403
    r = auth_client.get("/_debug/memory_config", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200
    data = r.json()
    assert "api_key_len" not in data


def test_websocket_requires_key_for_non_local_host(client):
    client.app.state.config.server.host = "0.0.0.0"
    client.app.state.config.server.api_key = SecretStr("")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_knowledge_persona_isolation(client):
    client.app.state.config.memory.scope = "persona"
    r = client.post("/admin/knowledge", json={"title": "a", "content": "alpha", "persona": "a"})
    assert r.status_code == 200
    r = client.post("/admin/knowledge", json={"title": "b", "content": "beta", "persona": "b"})
    assert r.status_code == 200

    r = client.get("/context?persona=a")
    assert r.status_code == 200
    contents = [k["content"] for k in r.json()["knowledge"]]
    assert contents == ["alpha"]


def test_shared_knowledge_delete_normalizes_persona(client):
    response = client.post(
        "/admin/knowledge",
        json={"title": "shared", "content": "shared content", "persona": "ignored"},
    )
    knowledge_id = response.json()["ids"][0]

    response = client.delete(f"/admin/knowledge?id={knowledge_id}&persona=also-ignored")
    assert response.status_code == 200
    assert client.get("/context?persona=shared").json()["knowledge"] == []


def test_abandoned_task_status_allowed(client):
    from synapse.task_queue import create_task as create_synapse_task
    from synapse.task_queue import update_task_status

    db_path = client.app.state.config.db_path
    task_id = asyncio.run(create_synapse_task(db_path, "a", "b", "plan"))
    ok = asyncio.run(update_task_status(db_path, task_id, "ABANDONED"))
    assert ok is True


def test_local_embedding_awaits_model(monkeypatch):
    from synapse.context import embedding

    class FakeVector(list):
        def tolist(self):
            return list(self)

    class FakeModel:
        def encode(self, text):
            return FakeVector([1.0, float(len(text))])

    async def fake_get_local_model(_model_name):
        return FakeModel()

    monkeypatch.setattr(embedding, "_get_local_model", fake_get_local_model)
    cfg = MemoryConfig(embedding_provider="local")
    vectors, status = asyncio.run(embedding.get_embeddings(["abc"], cfg))
    assert status == embedding.EmbeddingStatus.OK
    assert vectors == [[1.0, 3.0]]


def test_subprocess_agent_does_not_execute_plan_via_shell(monkeypatch):
    import types

    monkeypatch.setitem(
        sys.modules,
        "websockets",
        types.SimpleNamespace(WebSocketClientProtocol=object, connect=None),
    )
    from synapse.agents.subprocess_agent import SubprocessAgent

    agent = SubprocessAgent(
        {
            "extra": {
                "command": f'{sys.executable} -c "import sys; print(sys.argv[-1])"',
                "timeout": 5,
            }
        }
    )
    result = asyncio.run(agent._run_command("x; echo injected", "task", "cid", timeout=5))
    assert result["exit_code"] == 0
    assert result["output"].strip() == "x; echo injected"


def test_websocket_headers_kwargs_supports_modern_and_legacy_clients():
    from synapse.agents.worker_utils import websocket_headers_kwargs

    headers = {"Authorization": "Bearer worker-key"}

    def modern_connect(_url, *, additional_headers=None):
        return additional_headers

    def legacy_connect(_url, *, extra_headers=None):
        return extra_headers

    def kwargs_connect(_url, **_kwargs):
        return None

    assert websocket_headers_kwargs(modern_connect, headers) == {"additional_headers": headers}
    assert websocket_headers_kwargs(legacy_connect, headers) == {"extra_headers": headers}
    assert websocket_headers_kwargs(kwargs_connect, headers) == {"additional_headers": headers}
    assert websocket_headers_kwargs(modern_connect, {}) == {}


def test_registration_ack_helper_skips_ping_and_prints_sanitized_banner(capsys):
    import json

    from synapse.agents.worker_utils import print_registration_banner, receive_registration_ack
    from synapse.banner import format_banner

    class FakeWebSocket:
        def __init__(self):
            self.messages = [
                {"type": "ping"},
                "not-json",
                {"type": "registered", "payload": {"banner": format_banner("ws connected") + "\x1b[31m\x07"}},
            ]
            self.sent = []

        async def recv(self):
            item = self.messages.pop(0)
            return item if isinstance(item, str) else json.dumps(item)

        async def send(self, message):
            self.sent.append(json.loads(message))

    ws = FakeWebSocket()
    response = asyncio.run(receive_registration_ack(ws, timeout=1))

    assert response["type"] == "registered"
    assert ws.sent == [{"type": "pong"}]
    assert print_registration_banner(response) is True
    output = capsys.readouterr().out
    assert "SynapticLathe ws connected" in output
    assert "\x1b" not in output
    assert "\x07" not in output
    assert "[31m" not in output
    assert len(output.splitlines()) >= 10


def test_registration_ack_has_total_timeout():
    from synapse.agents.worker_utils import receive_registration_ack

    class PingOnlyWebSocket:
        async def recv(self):
            await asyncio.sleep(0)
            return '{"type":"ping"}'

        async def send(self, _message):
            return None

    with pytest.raises(TimeoutError):
        asyncio.run(receive_registration_ack(PingOnlyWebSocket(), timeout=0.001))


# ── 子进程 Worker CLI ──


def test_subprocess_worker_cli_uses_env(monkeypatch, tmp_path):
    from synapse.agents.subprocess_agent import build_agent_config, parse_args

    monkeypatch.setenv("SYNAPTIC_WS_URL", "ws://127.0.0.1:9112/ws")
    monkeypatch.setenv("SYNAPTIC_AGENT_NAME", "local-python")
    monkeypatch.setenv("SYNAPTIC_API_KEY", "worker-test-key")
    monkeypatch.setenv("SYNAPTIC_COMMAND", "python")
    monkeypatch.setenv("SYNAPTIC_WORKDIR", str(tmp_path))
    monkeypatch.setenv("SYNAPTIC_TIMEOUT", "123")
    monkeypatch.setenv("SYNAPTIC_SUBPROCESS_MAX_OUTPUT_BYTES", "456")

    args = parse_args([])
    cfg = build_agent_config(args)

    assert cfg["synapse_url"] == "ws://127.0.0.1:9112/ws"
    assert cfg["agent_name"] == "local-python"
    assert cfg["api_key"] == "worker-test-key"
    assert cfg["extra"]["command"] == "python"
    assert cfg["extra"]["command_args"] == ["python"]
    assert cfg["extra"]["protect_plan_options"] is True
    assert cfg["extra"]["workdir"] == str(tmp_path.resolve())
    assert cfg["extra"]["timeout"] == 123
    assert cfg["extra"]["max_output_bytes"] == 456


def test_subprocess_worker_cli_supports_pass_env(monkeypatch, tmp_path):
    from synapse.agents.subprocess_agent import build_agent_config, parse_args

    monkeypatch.setenv("SYNAPTIC_COMMAND", "python")
    monkeypatch.setenv("SYNAPTIC_WORKDIR", str(tmp_path))
    monkeypatch.setenv("SYNAPTIC_SUBPROCESS_PASS_ENV", "PYTHONPATH,SSL_CERT_FILE")

    args = parse_args([])
    cfg = build_agent_config(args)

    assert cfg["extra"]["pass_env"] == ["PYTHONPATH", "SSL_CERT_FILE"]


def test_subprocess_worker_rejects_synaptic_pass_env(monkeypatch):
    from synapse.agents.subprocess_agent import parse_args

    monkeypatch.setenv("SYNAPTIC_COMMAND", "python")
    with pytest.raises(SystemExit):
        parse_args(["--pass-env", "SYNAPTIC_API_KEY"])


def test_worker_rejects_dynamic_loader_env_names():
    from synapse.agents.worker_utils import validate_child_env_names

    with pytest.raises(ValueError):
        validate_child_env_names(["LD_PRELOAD"])


def test_subprocess_child_env_does_not_forward_synaptic_key(monkeypatch):
    from synapse.agents.subprocess_agent import SubprocessAgent

    monkeypatch.setenv("SYNAPTIC_API_KEY", "server-secret")
    code = "import os; print(os.environ.get('SYNAPTIC_API_KEY', 'missing'))"
    command = f'{sys.executable} -c "{code}"'
    agent = SubprocessAgent({"extra": {"command": command, "timeout": 5}})

    result = asyncio.run(agent._run_command("plan", "task", "cid", timeout=5))

    assert result["exit_code"] == 0
    assert result["output"].strip() == "missing"


def test_subprocess_child_env_can_explicitly_pass_pythonpath(monkeypatch, tmp_path):
    from synapse.agents.subprocess_agent import SubprocessAgent

    pythonpath = str(tmp_path / "pythonpath")
    monkeypatch.setenv("PYTHONPATH", pythonpath)
    code = "import os; print(os.environ.get('PYTHONPATH', ''))"
    command = f'{sys.executable} -c "{code}"'
    agent = SubprocessAgent(
        {
            "extra": {
                "command": command,
                "timeout": 5,
                "pass_env": ["PYTHONPATH"],
            }
        }
    )

    result = asyncio.run(agent._run_command("plan", "task", "cid", timeout=5))

    assert result["exit_code"] == 0
    assert result["output"].strip() == pythonpath


def test_subprocess_agent_truncates_large_output():
    from synapse.agents.subprocess_agent import SubprocessAgent

    code = "import sys; sys.stdout.write(chr(120) * 20)"
    command = f'{sys.executable} -c "{code}"'
    agent = SubprocessAgent(
        {
            "extra": {
                "command": command,
                "timeout": 5,
                "max_output_bytes": 5,
            }
        }
    )

    result = asyncio.run(agent._run_command("plan", "task", "cid", timeout=5))

    assert result["exit_code"] == 0
    assert result["output"] == "xxxxx"
    assert result["output_truncated"] is True


def test_subprocess_worker_cli_requires_command(monkeypatch):
    from synapse.agents.subprocess_agent import parse_args

    monkeypatch.delenv("SYNAPTIC_COMMAND", raising=False)
    with pytest.raises(SystemExit):
        parse_args([])


# ── Codex Worker CLI ──


def test_codex_worker_cli_uses_env(monkeypatch, tmp_path):
    from synapse.agents.codex_agent import build_agent_config, parse_args

    monkeypatch.setenv("SYNAPTIC_WS_URL", "ws://127.0.0.1:9112/ws")
    monkeypatch.setenv("SYNAPTIC_AGENT_NAME", "codex-local")
    monkeypatch.setenv("SYNAPTIC_API_KEY", "worker-test-key")
    monkeypatch.setenv("SYNAPTIC_CODEX_BIN", "codex")
    monkeypatch.setenv("SYNAPTIC_CODEX_WORKDIR", str(tmp_path))
    monkeypatch.setenv("SYNAPTIC_CODEX_TIMEOUT", "321")
    monkeypatch.setenv("SYNAPTIC_CODEX_SANDBOX", "workspace-write")
    monkeypatch.setenv("SYNAPTIC_CODEX_APPROVAL_POLICY", "never")

    args = parse_args([])
    cfg = build_agent_config(args)

    assert cfg["synapse_url"] == "ws://127.0.0.1:9112/ws"
    assert cfg["agent_name"] == "codex-local"
    assert cfg["api_key"] == "worker-test-key"
    assert cfg["extra"]["codex_bin"] == "codex"
    assert cfg["extra"]["workdir"] == str(tmp_path.resolve())
    assert cfg["extra"]["timeout"] == 321
    assert cfg["extra"]["sandbox"] == "workspace-write"
    assert cfg["extra"]["approval_policy"] == "never"


def test_codex_exec_args_are_shell_free(tmp_path):
    from synapse.agents.codex_agent import build_codex_exec_args

    args = build_codex_exec_args(
        {
            "codex_bin": "codex",
            "workdir": str(tmp_path),
            "add_dir": [],
            "sandbox": "read-only",
            "approval_policy": "never",
            "ephemeral": True,
        },
        "x; echo injected",
    )

    assert args[:2] == ["codex", "exec"]
    assert "--ephemeral" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--config") + 1] == "approval_policy='never'"
    assert args[args.index("--cd") + 1] == str(tmp_path)
    assert args[-2:] == ["--", "x; echo injected"]


def test_codex_stream_reader_handles_long_lines_without_deadlock():
    from synapse.agents.codex_agent import CodexAgent
    from synapse.agents.worker_utils import LimitedByteBuffer

    async def scenario():
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"x" * 1000)
        reader.feed_eof()
        sink = LimitedByteBuffer(2000)
        agent = CodexAgent({"extra": {"workdir": "."}})
        await agent._read_stream(reader, sink)
        return sink.text()

    assert asyncio.run(scenario()) == "x" * 1000


def test_codex_worker_cli_requires_workdir(monkeypatch):
    from synapse.agents.codex_agent import parse_args

    monkeypatch.delenv("SYNAPTIC_CODEX_WORKDIR", raising=False)
    monkeypatch.delenv("SYNAPTIC_WORKDIR", raising=False)
    with pytest.raises(SystemExit):
        parse_args([])


def test_codex_child_env_does_not_forward_synaptic_key(monkeypatch):
    from synapse.agents.worker_utils import build_child_env

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SYNAPTIC_API_KEY", "server-secret")
    monkeypatch.setenv("CODEX_API_KEY", "codex-secret")

    env = build_child_env()

    assert env["PATH"] == "/usr/bin"
    assert "SYNAPTIC_API_KEY" not in env
    assert "CODEX_API_KEY" not in env


def test_codex_child_env_requires_explicit_secret_pass(monkeypatch):
    from synapse.agents.worker_utils import build_child_env

    monkeypatch.setenv("CODEX_API_KEY", "codex-secret")

    env = build_child_env(pass_env=["CODEX_API_KEY"])

    assert env["CODEX_API_KEY"] == "codex-secret"


def test_codex_worker_rejects_synaptic_pass_env(tmp_path):
    from synapse.agents.codex_agent import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--workdir", str(tmp_path), "--pass-env", "SYNAPTIC_API_KEY"])


def test_limited_byte_buffer_caps_head_and_tail():
    from synapse.agents.worker_utils import LimitedByteBuffer

    head = LimitedByteBuffer(5)
    head.append(b"abcdef")
    assert head.text() == "abcde"
    assert head.truncated is True

    tail = LimitedByteBuffer(5, keep_tail=True)
    tail.append(b"abc")
    tail.append(b"def")
    assert tail.text() == "bcdef"
    assert tail.truncated is True


def test_worker_single_instance_lock_blocks_duplicate(tmp_path):
    from synapse.agents.worker_utils import SingleInstanceLock

    lock_path = str(tmp_path / "worker.lock")
    first = SingleInstanceLock(lock_path)
    second = SingleInstanceLock(lock_path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_worker_default_lock_file_is_stable():
    from synapse.agents.worker_utils import default_worker_lock_file

    a = default_worker_lock_file("codex", "ws://127.0.0.1:9112/ws", "codex-local")
    b = default_worker_lock_file("codex", "ws://127.0.0.1:9112/ws", "codex-local")
    c = default_worker_lock_file("codex", "ws://127.0.0.1:9112/ws", "other")

    assert a == b
    assert a != c
    assert Path(a).parent.name.startswith("synaptic-lathe-")
    assert Path(a).name.startswith("codex-")


def test_setup_wizard_generates_config_and_launcher(tmp_path):
    from synapse.setup_wizard import SetupOptions, run_setup

    result = run_setup(
        SetupOptions(
            base_dir=tmp_path,
            host="0.0.0.0",
            port=19112,
            api_key="setup-key",
            public_read_context=True,
            install_deps=False,
        )
    )

    config = result.config_path.read_text(encoding="utf-8")
    launcher = result.control_path.read_text(encoding="utf-8")

    assert result.installed is False
    assert result.config_created is True
    assert result.generated_api_key == ""
    assert result.api_key_configured is True
    assert result.worker_api_key_configured is True
    assert 'host: "0.0.0.0"' in config
    assert "port: 19112" in config
    assert 'api_key: "setup-key"' in config
    assert "public_read_context: true" in config
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / ".synaptic").is_dir()
    assert result.control_path.exists()
    assert result.cmd_path.exists()
    cmd_wrapper = result.cmd_path.read_text(encoding="utf-8")
    assert str(result.runtime_python) in cmd_wrapper
    assert '\npy "' not in cmd_wrapper
    assert 'CMD = [PYTHON, "-m", "synapse.cli", str(CONFIG)]' in launcher
    assert "os.execv(PYTHON" in launcher
    assert "synaptic_lathe.log" in launcher
    assert "SYNAPTIC_LOG_STDOUT" in launcher
    compile(launcher, str(result.control_path), "exec")
    assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700


def test_setup_wizard_auto_generates_api_key(tmp_path):
    from synapse.setup_wizard import SetupOptions, run_setup

    result = run_setup(SetupOptions(base_dir=tmp_path, install_deps=False))
    config = result.config_path.read_text(encoding="utf-8")

    assert result.generated_api_key
    assert result.generated_worker_api_key
    assert result.api_key_configured is True
    assert result.worker_api_key_configured is True
    assert result.generated_api_key in config
    assert result.generated_worker_api_key in config
    assert result.config_path.stat().st_mode & 0o777 == 0o600


def test_setup_wizard_cli_reports_explicit_keys_without_disclosing(tmp_path, capsys):
    from synapse.setup_wizard import cli

    cli(
        [
            "--base-dir",
            str(tmp_path),
            "--api-key",
            "explicit-admin-secret",
            "--worker-api-key",
            "explicit-worker-secret",
            "--skip-install",
            "--yes",
        ]
    )

    output = capsys.readouterr().out
    assert "Admin API key:  configured (not displayed)" in output
    assert "Worker API key: configured (not displayed)" in output
    assert "explicit-admin-secret" not in output
    assert "explicit-worker-secret" not in output


def test_setup_wizard_preserves_virtualenv_python_symlink(tmp_path):
    from synapse.setup_wizard import SetupOptions, run_setup

    python_link = tmp_path / "server-venv" / "bin" / "python"
    python_link.parent.mkdir(parents=True)
    python_link.symlink_to(sys.executable)

    result = run_setup(
        SetupOptions(
            base_dir=tmp_path / "server",
            install_deps=False,
            python=str(python_link),
        )
    )

    assert result.runtime_python == python_link
    assert f"PYTHON = {str(python_link)!r}" in result.control_path.read_text(encoding="utf-8")


def test_worker_setup_generates_profile_launcher(tmp_path):
    from synapse.worker_setup import WorkerSetupOptions, run_setup

    project_dir = Path(__file__).resolve().parents[1]
    result = run_setup(
        WorkerSetupOptions(
            base_dir=tmp_path,
            project_dir=project_dir,
            install_deps=False,
            api_key="worker-key",
            url="ws://server.example/ws",
            force=True,
        )
    )

    launcher = result.control_path.read_text(encoding="utf-8")
    env_file = result.env_path.read_text(encoding="utf-8")
    profiles = result.profiles_path.read_text(encoding="utf-8") if result.profiles_path else ""

    assert result.kind == "profile"
    assert result.name == "local-dispatcher"
    assert result.profiles_path is not None
    assert result.profiles_path.stat().st_mode & 0o777 == 0o600
    assert result.env_path.stat().st_mode & 0o777 == 0o600
    assert 'SYNAPTIC_API_KEY="worker-key"' in env_file
    assert 'SYNAPTIC_WS_URL="ws://server.example/ws"' in env_file
    assert "synapse.agents.profile_agent" in launcher
    assert "os.execv(PYTHON" in launcher
    assert "PROJECT_ON_PYTHONPATH = True" in launcher
    assert "sys.path.insert(0, str(PROJECT_DIR))" in launcher
    assert "--profiles" in launcher
    assert "TITLE =" not in launcher
    assert "codex:" in profiles
    assert "hermes:" in profiles
    assert "claude:" in profiles
    assert "reasonix:" in profiles
    assert '      - "--"\n      - "{plan}"' in profiles
    cmd_wrapper = result.cmd_path.read_text(encoding="utf-8")
    assert str(result.runtime_python) in cmd_wrapper
    compile(launcher, str(result.control_path), "exec")


def test_worker_setup_does_not_inject_non_source_project_dir(tmp_path):
    from synapse.worker_setup import WorkerSetupOptions, run_setup

    project_dir = tmp_path / "not-source"
    project_dir.mkdir()
    result = run_setup(
        WorkerSetupOptions(
            base_dir=tmp_path / "worker",
            project_dir=project_dir,
            install_deps=False,
            force=True,
        )
    )

    launcher = result.control_path.read_text(encoding="utf-8")
    assert "PROJECT_ON_PYTHONPATH = False" in launcher
    compile(launcher, str(result.control_path), "exec")


def test_worker_setup_generates_subprocess_launcher(tmp_path):
    from synapse.worker_setup import WorkerSetupOptions, run_setup

    project_dir = Path(__file__).resolve().parents[1]
    result = run_setup(
        WorkerSetupOptions(
            base_dir=tmp_path,
            project_dir=project_dir,
            kind="subprocess",
            command=sys.executable,
            workdir=str(tmp_path),
            install_deps=False,
            force=True,
        )
    )

    launcher = result.control_path.read_text(encoding="utf-8")

    assert result.kind == "subprocess"
    assert result.name == "local-subprocess"
    assert result.profiles_path is None
    assert "synapse.agents.subprocess_agent" in launcher
    assert "--command" in launcher
    assert sys.executable in launcher
    compile(launcher, str(result.control_path), "exec")


def test_worker_setup_preserves_virtualenv_python_symlink(tmp_path):
    from synapse.worker_setup import WorkerSetupOptions, run_setup

    python_link = tmp_path / "worker-venv" / "bin" / "python"
    python_link.parent.mkdir(parents=True)
    python_link.symlink_to(sys.executable)

    result = run_setup(
        WorkerSetupOptions(
            base_dir=tmp_path / "worker",
            project_dir=Path(__file__).resolve().parents[1],
            install_deps=False,
            python=str(python_link),
            force=True,
        )
    )

    assert result.runtime_python == python_link
    assert f"PYTHON = {str(python_link)!r}" in result.control_path.read_text(encoding="utf-8")


def test_connection_manager_keeps_pending_tail_when_delivery_fails():
    from synapse.connection import ConnectionManager

    class FailingWebSocket:
        async def send_json(self, _message):
            raise RuntimeError("offline")

        async def close(self, code=None):
            self.code = code

    class RecordingWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    async def run():
        manager = ConnectionManager()
        await manager.send_or_queue("agent", {"id": 1})
        await manager.send_or_queue("agent", {"id": 2})
        delivered = await manager.register("agent", FailingWebSocket(), "agent:fail")
        assert delivered == []
        assert [message[2]["id"] for message in manager._pending["agent"]] == [1, 2]

        ws = RecordingWebSocket()
        delivered = await manager.register("agent", ws, "agent:ok", deliver_pending=False)
        assert delivered == []
        assert ws.messages == []
        delivered = await manager.deliver_pending("agent", ws)
        assert [message["id"] for message in delivered] == [1, 2]
        assert [message["id"] for message in ws.messages] == [1, 2]

    asyncio.run(run())


def test_connection_manager_caps_pending_queue_per_agent():
    from synapse.connection import _MAX_PENDING_PER_AGENT, ConnectionManager

    async def run():
        manager = ConnectionManager()
        for index in range(_MAX_PENDING_PER_AGENT + 5):
            await manager.send_or_queue("offline", {"id": index})
        queued = [message["id"] for _, _, message in manager._pending["offline"]]
        assert len(queued) == _MAX_PENDING_PER_AGENT
        assert queued[0] == 5
        assert queued[-1] == _MAX_PENDING_PER_AGENT + 4

    asyncio.run(run())


def test_codex_worker_cli_sets_lock_and_reconnect_defaults(monkeypatch, tmp_path):
    from synapse.agents.codex_agent import parse_args

    monkeypatch.delenv("SYNAPTIC_CODEX_LOCK_FILE", raising=False)
    monkeypatch.delenv("SYNAPTIC_WORKER_LOCK_FILE", raising=False)
    args = parse_args(["--workdir", str(tmp_path), "--name", "codex-local"])

    assert args.reconnect is True
    assert args.allow_duplicate is False
    assert args.reconnect_initial_delay == 1.0
    assert args.reconnect_max_delay == 30.0
    assert Path(args.lock_file).parent.name.startswith("synaptic-lathe-")
    assert Path(args.lock_file).name.startswith("codex-")


# ── Profile Dispatcher Worker ──


def test_profile_worker_describes_non_secret_profile_capabilities(tmp_path):
    from synapse.agents.profile_agent import describe_profile_capabilities, load_profile_config

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        f"""
default_profile: claude
profiles:
  claude:
    command: [{sys.executable!r}, "-c", "print('{{plan}} {{session_id}}')"]
    workdir: {str(tmp_path)!r}
    timeout: 123
    max_output_bytes: 456
    sessions:
      main: real-session-id
  reasonix:
    command: [{sys.executable!r}, "-c", "print('{{plan}}')"]
    workdir: {str(tmp_path)!r}
    timeout: 1800
  rawdefault:
    command: [{sys.executable!r}, "-c", "print('{{session_id}} {{plan}}')"]
    workdir: {str(tmp_path)!r}
    default_session: real-default-session-id
    allow_raw_session_id: true
""",
        encoding="utf-8",
    )

    caps = describe_profile_capabilities(load_profile_config(str(profile_file)))

    assert caps["claude"]["suggested_timeout"] == 123
    assert caps["claude"]["max_output_bytes"] == 456
    assert caps["claude"]["supports_session"] is True
    assert caps["claude"]["session_required"] is True
    assert caps["claude"]["session_aliases"] == ["main"]
    assert "real-session-id" not in str(caps)
    assert "real-default-session-id" not in str(caps)
    assert caps["rawdefault"]["default_session_alias"] == ""
    assert "workdir" not in caps["claude"]
    assert "command" not in caps["claude"]
    assert caps["reasonix"]["suggested_timeout"] == 1800
    assert "avoid_short_timeout" in caps["reasonix"]["hints"]


def test_profile_worker_cli_uses_config(monkeypatch, tmp_path):
    from synapse.agents.profile_agent import build_agent_config, parse_args

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        """
default_profile: claude
profiles:
  claude:
    command:
      - python
      - -c
      - "print('ok')"
    workdir: .
    timeout: 123
    max_output_bytes: 456
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("SYNAPTIC_WS_URL", "ws://127.0.0.1:9112/ws")
    monkeypatch.setenv("SYNAPTIC_AGENT_NAME", "local-dispatcher")
    monkeypatch.setenv("SYNAPTIC_API_KEY", "worker-test-key")
    monkeypatch.setenv("SYNAPTIC_PROFILE_CONFIG", str(profile_file))
    args = parse_args(["--workdir", str(tmp_path)])
    cfg = build_agent_config(args)

    assert cfg["synapse_url"] == "ws://127.0.0.1:9112/ws"
    assert cfg["agent_name"] == "local-dispatcher"
    assert cfg["api_key"] == "worker-test-key"
    assert cfg["extra"]["profile_config"]["default_profile"] == "claude"
    assert cfg["extra"]["profile_config"]["profiles"]["claude"]["timeout"] == 123
    assert cfg["extra"]["profile_config"]["profiles"]["claude"]["max_output_bytes"] == 456


def test_profile_command_uses_session_alias_and_plan_placeholder(tmp_path):
    from synapse.agents.profile_agent import build_profile_command, build_profile_request, load_profile_config

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        f"""
profiles:
  claude:
    command:
      - {sys.executable}
      - -c
      - "import sys; print(sys.argv[1]); print(sys.argv[2])"
      - "{{session_id}}"
      - "{{plan}}"
    sessions:
      main: real-session-id
""",
        encoding="utf-8",
    )
    cfg = load_profile_config(str(profile_file))
    request = build_profile_request(
        {"tool": "claude", "session_id": "main", "plan": "x; echo injected"},
        cfg["default_profile"],
    )
    argv, session_alias = build_profile_command(cfg["profiles"]["claude"], request)

    assert session_alias == "main"
    assert argv[-2:] == ["real-session-id", "x; echo injected"]


def test_profile_worker_rejects_raw_session_by_default(tmp_path):
    from synapse.agents.profile_agent import (
        ProfileConfigError,
        build_profile_command,
        build_profile_request,
        load_profile_config,
    )

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        """
profiles:
  claude:
    command: [claude, --resume, "{session_id}", -p, "{plan}"]
    sessions:
      main: real-session-id
""",
        encoding="utf-8",
    )
    cfg = load_profile_config(str(profile_file))
    request = build_profile_request({"tool": "claude", "session_id": "unknown", "plan": "hello"})

    with pytest.raises(ProfileConfigError):
        build_profile_command(cfg["profiles"]["claude"], request)


def test_profile_worker_ignores_optional_session_when_profile_does_not_use_it(tmp_path):
    from synapse.agents.profile_agent import build_profile_command, build_profile_request, load_profile_config

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        """
profiles:
  claude:
    command: [claude, -p, "{plan}"]
""",
        encoding="utf-8",
    )
    cfg = load_profile_config(str(profile_file))
    request = build_profile_request({"tool": "claude", "session_id": "optional", "plan": "hello"})
    argv, session_alias = build_profile_command(cfg["profiles"]["claude"], request)

    assert session_alias == ""
    assert argv == ["claude", "-p", "hello"]


def test_profile_worker_allows_raw_session_when_enabled(tmp_path):
    from synapse.agents.profile_agent import build_profile_command, build_profile_request, load_profile_config

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        """
profiles:
  claude:
    command: [claude, --resume, "{session_id}", -p, "{plan}"]
    allow_raw_session_id: true
""",
        encoding="utf-8",
    )
    cfg = load_profile_config(str(profile_file))
    request = build_profile_request({"tool": "claude", "ssid": "session-123", "plan": "hello"})
    argv, session_alias = build_profile_command(cfg["profiles"]["claude"], request)

    assert session_alias == "session-123"
    assert argv == ["claude", "--resume", "session-123", "-p", "hello"]


def test_profile_worker_plan_json_fallback():
    from synapse.agents.profile_agent import build_profile_request

    request = build_profile_request(
        {"plan": '{"tool":"codex","session":"dev","plan":"review this"}'},
        "",
    )

    assert request["profile"] == "codex"
    assert request["session_alias"] == "dev"
    assert request["plan"] == "review this"


def test_profile_worker_does_not_rewrite_plain_json_plan():
    from synapse.agents.profile_agent import build_profile_request

    request = build_profile_request({"plan": '{"plan":"literal"}'}, "echo")

    assert request["profile"] == "echo"
    assert request["plan"] == '{"plan":"literal"}'


def test_profile_worker_does_not_execute_plan_via_shell(tmp_path):
    from synapse.agents.profile_agent import ProfileDispatcherAgent, load_profile_config

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        f"""
default_profile: echo
profiles:
  echo:
    command:
      - {sys.executable}
      - -c
      - "import sys; print(sys.argv[-1])"
""",
        encoding="utf-8",
    )
    cfg = load_profile_config(str(profile_file))
    agent = ProfileDispatcherAgent({"extra": {"profile_config": cfg}})

    result = asyncio.run(agent._run_profile({"plan": "x; echo injected", "profile": "echo"}, "task", "cid"))

    assert result["exit_code"] == 0
    assert result["output"].strip() == "x; echo injected"


def test_profile_worker_rejects_reserved_runtime_env():
    from synapse.agents.profile_agent import ProfileDispatcherAgent

    cfg = {
        "default_profile": "echo",
        "profiles": {
            "echo": {
                "command": [sys.executable, "-c", "print('should-not-run')"],
                "env": {"SYNAPTIC_API_KEY": "leak"},
                "timeout": 5,
                "max_output_bytes": 1024,
            }
        },
    }
    agent = ProfileDispatcherAgent({"extra": {"profile_config": cfg}})

    result = asyncio.run(agent._run_profile({"plan": "test", "profile": "echo"}, "task", "cid"))

    assert result["exit_code"] == -1
    assert "reserved environment variable" in result["error"]
    assert result["output"] == ""


def test_profile_worker_closes_child_stdin(tmp_path):
    from synapse.agents.profile_agent import ProfileDispatcherAgent, load_profile_config

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        f"""
default_profile: probe
profiles:
  probe:
    command:
      - {sys.executable}
      - -c
      - "import sys; print('stdin=' + ('closed' if sys.stdin.read() == '' else 'open'))"
""",
        encoding="utf-8",
    )
    cfg = load_profile_config(str(profile_file))
    agent = ProfileDispatcherAgent({"extra": {"profile_config": cfg}})

    result = asyncio.run(agent._run_profile({"plan": "test", "profile": "probe"}, "task", "cid"))

    assert result["exit_code"] == 0
    assert result["output"].strip() == "stdin=closed"


def test_profile_worker_times_out_blocked_approval_process(tmp_path):
    from synapse.agents.profile_agent import ProfileDispatcherAgent, load_profile_config

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        f"""
default_profile: wait
profiles:
  wait:
    command:
      - {sys.executable}
      - -c
      - "import time; print('waiting-for-approval', flush=True); time.sleep(30)"
    timeout: 30
""",
        encoding="utf-8",
    )
    cfg = load_profile_config(str(profile_file))
    agent = ProfileDispatcherAgent({"extra": {"profile_config": cfg}})

    result = asyncio.run(agent._run_profile({"plan": "test", "profile": "wait", "timeout": 1}, "task", "cid"))

    assert result["exit_code"] == -1
    assert result["error"] == "Process timed out after 1s"
    assert "waiting-for-approval" in result["output"]


def test_worker_keepalive_sends_pong():
    from synapse.agents.worker_utils import websocket_keepalive

    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send(self, message):
            self.messages.append(message)
            if len(self.messages) >= 2:
                raise RuntimeError("stop")

    async def run():
        ws = FakeWebSocket()
        await websocket_keepalive(ws, interval=0.001)
        return ws.messages

    messages = asyncio.run(run())

    assert messages
    assert all('"type": "pong"' in msg or '"type":"pong"' in msg for msg in messages)


def test_server_forwards_only_narrow_task_options():
    from synapse.server import _copy_forwarded_task_options

    forwarded, error = _copy_forwarded_task_options(
        {
            "profile": "claude",
            "tool": "claude",
            "session_id": "main",
            "provider": "ignored",
            "stdin": "input",
        },
        100,
    )

    assert error == ""
    assert forwarded == {"profile": "claude", "tool": "claude", "session_id": "main", "stdin": "input"}


def test_http_agent_call_uses_task_timeout(client):
    from synapse.handlers import handle_http_send

    class FakeHTTPResponse:
        status_code = 200
        text = 'data: {"type":"plain","data":"ok"}\n'

        def raise_for_status(self):
            return None

    class FakeHTTPClient:
        def __init__(self):
            self.url = ""
            self.kwargs = {}

        async def post(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            return FakeHTTPResponse()

    class FakeWs:
        def __init__(self, app):
            self.app = app
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)

    fake_http = FakeHTTPClient()
    client.app.state.http_client = fake_http
    ws = FakeWs(client.app)
    agent_cfg = AgentConfig(
        type="http_api",
        base_url="http://agent.local",
        api_key=SecretStr("agent-secret"),
        extra={"username": "tester"},
    )

    asyncio.run(
        handle_http_send(
            client.app.state.config,
            ws,
            "caller",
            "astrbot",
            "hello",
            7,
            "http-timeout-cid",
            agent_cfg,
            {},
        )
    )

    assert fake_http.url == "http://agent.local/api/v1/chat"
    assert fake_http.kwargs["timeout"] == 7
    assert fake_http.kwargs["headers"] == {"Authorization": "Bearer agent-secret"}
    assert ws.sent[0]["type"] == "task_result"
    assert ws.sent[0]["payload"]["result"] == "ok"


def test_http_agent_timeout_uses_timeout_terminal_state(client):
    from synapse.handlers import handle_http_send
    from synapse.task_queue import get_task

    class TimeoutHTTPClient:
        async def post(self, _url, **_kwargs):
            raise TimeoutError("simulated timeout")

    class FakeWs:
        def __init__(self, app):
            self.app = app
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)

    client.app.state.http_client = TimeoutHTTPClient()
    ws = FakeWs(client.app)
    agent_cfg = AgentConfig(type="http_api", base_url="http://agent.local")
    task_id = "http-timeout-state-cid"

    asyncio.run(
        handle_http_send(
            client.app.state.config,
            ws,
            "caller",
            "astrbot",
            "hello",
            7,
            task_id,
            agent_cfg,
            {},
        )
    )

    task = asyncio.run(get_task(client.app.state.config.db_path, task_id))
    assert task["status"] == "TIMEOUT"
    assert task["result"] == "HTTP agent call timed out after 7s"
    assert ws.sent[0]["type"] == "error"


def test_server_rejects_non_string_forwarded_task_option():
    from synapse.server import _copy_forwarded_task_options

    forwarded, error = _copy_forwarded_task_options({"profile": ["claude"]}, 100)

    assert forwarded == {}
    assert error == "'profile' must be a string"


def test_server_uses_configured_pending_message_ttl(client):
    from synapse.server import _pending_ttl_seconds

    client.app.state.config.server.pending_message_ttl_hours = 2

    assert _pending_ttl_seconds(client.app.state.config) == 7200


def test_server_source_disconnect_preserves_outbound_task(client):
    from synapse.connection import connection_manager
    from synapse.server import _fail_tasks_targeting_disconnected_agent, _timeout_tasks
    from synapse.task_queue import create_task as create_synapse_task
    from synapse.task_queue import get_task, update_task_status

    async def run():
        connection_manager._connections.clear()
        connection_manager._metadata.clear()
        connection_manager._pending.clear()
        _timeout_tasks.clear()
        db_path = client.app.state.config.db_path
        task_id = await create_synapse_task(db_path, "caller", "worker", "plan")
        await update_task_status(db_path, task_id, "DISPATCHED")
        watcher = asyncio.create_task(asyncio.sleep(60))
        _timeout_tasks[task_id] = ("caller", watcher)
        try:
            changed = await _fail_tasks_targeting_disconnected_agent(client.app.state.config, "caller")
            task = await get_task(db_path, task_id)
            assert changed == 0
            assert task["status"] == "DISPATCHED"
            assert task_id in _timeout_tasks
        finally:
            watcher.cancel()
            _timeout_tasks.clear()
            connection_manager._pending.clear()

    asyncio.run(run())


def test_server_target_disconnect_fails_active_task_and_notifies_source(client):
    from synapse.connection import connection_manager
    from synapse.server import _fail_tasks_targeting_disconnected_agent, _timeout_tasks
    from synapse.task_queue import create_task as create_synapse_task
    from synapse.task_queue import get_task, update_task_status

    async def run():
        connection_manager._connections.clear()
        connection_manager._metadata.clear()
        connection_manager._pending.clear()
        _timeout_tasks.clear()
        db_path = client.app.state.config.db_path
        task_id = await create_synapse_task(db_path, "caller", "worker", "plan")
        await update_task_status(db_path, task_id, "EXECUTING")
        watcher = asyncio.create_task(asyncio.sleep(60))
        _timeout_tasks[task_id] = ("caller", watcher)
        changed = await _fail_tasks_targeting_disconnected_agent(client.app.state.config, "worker")
        await asyncio.sleep(0)
        task = await get_task(db_path, task_id)

        assert changed == 1
        assert task["status"] == "ERROR"
        assert task_id not in _timeout_tasks
        assert watcher.cancelled()
        queued = connection_manager._pending["caller"][0][2]
        assert queued["type"] == "error"
        assert queued["payload"]["code"] == "TARGET_DISCONNECTED"
        connection_manager._pending.clear()

    asyncio.run(run())


def test_server_result_delivery_does_not_require_timeout_watcher(client):
    from synapse.connection import connection_manager
    from synapse.server import _complete_task_and_deliver, _timeout_tasks
    from synapse.task_queue import create_task as create_synapse_task
    from synapse.task_queue import get_task, update_task_status

    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    async def run():
        connection_manager._connections.clear()
        connection_manager._metadata.clear()
        connection_manager._pending.clear()
        _timeout_tasks.clear()
        db_path = client.app.state.config.db_path
        task_id = await create_synapse_task(db_path, "caller", "worker", "plan")
        await update_task_status(db_path, task_id, "EXECUTING")
        ws = FakeWebSocket()
        await connection_manager.register("caller", ws, "caller:test")
        await _complete_task_and_deliver(
            client.app.state.config,
            task_id,
            "caller",
            {"task_id": task_id, "result": "ok"},
            "ok",
        )
        task = await get_task(db_path, task_id)

        assert task["status"] == "COMPLETED"
        assert ws.messages[-1]["type"] == "task_result"
        assert ws.messages[-1]["payload"]["result"] == "ok"
        await connection_manager.unregister("caller", ws)

    asyncio.run(run())
