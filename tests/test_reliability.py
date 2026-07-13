from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synapse.config import GlobalConfig, MemoryConfig, RouterConfig, RouteRule
from synapse.db import init_db
from synapse.server import app, set_project_root


@pytest.fixture
def api_client(tmp_path):
    from synapse.connection import connection_manager

    connection_manager._connections.clear()
    connection_manager._metadata.clear()
    connection_manager._pending.clear()
    config = GlobalConfig(db_path=str(tmp_path / "test.db"))
    config.server.rate_limit_max = 10_000
    config.server.ws_rate_limit_max = 10_000
    app.state.config = config
    app.state.config_path = str(tmp_path / "config.yaml")
    set_project_root(str(tmp_path / "config.yaml"))
    asyncio.run(init_db(config.db_path))
    with TestClient(app) as client:
        yield client


def _receive_non_ping(socket) -> dict:
    while True:
        message = socket.receive_json()
        if message.get("type") != "ping":
            return message
        socket.send_json({"type": "pong"})


def _register(socket, name: str) -> dict:
    socket.send_json(
        {
            "type": "register",
            "payload": {"agent_name": name, "protocol_version": 1},
        }
    )
    response = _receive_non_ping(socket)
    assert response["type"] == "registered"
    return response


def test_admin_memory_config_update_is_atomic_and_completes(api_client):
    config_path = Path(api_client.app.state.config_path)
    config_path.write_text("server:\n  host: 127.0.0.1\nmemory:\n  scope: shared\n", encoding="utf-8")

    response = api_client.post(
        "/admin/config",
        json={"memory": {"scope": "persona", "embedding_trust_env": False}},
    )

    assert response.status_code == 200
    saved = config_path.read_text(encoding="utf-8")
    assert "scope: persona" in saved
    assert "embedding_trust_env: false" in saved
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_admin_config_rejects_unknown_fields(api_client):
    config_path = Path(api_client.app.state.config_path)
    config_path.write_text("memory:\n  scope: shared\n", encoding="utf-8")
    response = api_client.post("/admin/config", json={"memory": {"embedding_api_keey": "typo"}})
    assert response.status_code == 422
    assert "embedding_api_keey" not in config_path.read_text(encoding="utf-8")


def test_websocket_rejects_invalid_overrides_and_unknown_types(api_client):
    with api_client.websocket_connect("/ws") as source, api_client.websocket_connect("/ws") as target:
        _register(source, "validation-source")
        _register(target, "validation-target")
        source.send_json(
            {
                "type": "send",
                "payload": {"target": "validation-target", "plan": "work", "provider": {"bad": True}},
            }
        )
        invalid = _receive_non_ping(source)
        assert invalid["type"] == "error"
        assert invalid["payload"]["code"] == "INVALID_REQUEST"

        source.send_json({"type": "future-command", "payload": {}})
        unknown = _receive_non_ping(source)
        assert unknown["type"] == "error"
        assert unknown["payload"]["code"] == "UNKNOWN_MESSAGE_TYPE"


def test_stream_chunks_are_relayed_and_marker_discards_trailing_text(api_client):
    with api_client.websocket_connect("/ws") as source, api_client.websocket_connect("/ws") as target:
        _register(source, "source")
        _register(target, "target")
        source.send_json(
            {
                "type": "send",
                "correlation_id": "stream-task-1",
                "payload": {"target": "target", "plan": "work", "timeout": 30},
            }
        )
        task = _receive_non_ping(target)
        assert task["type"] == "task"
        assert _receive_non_ping(source)["type"] == "task_queued"
        target.send_json({"type": "accept", "correlation_id": "stream-task-1"})

        target.send_json({"type": "chunk", "correlation_id": "stream-task-1", "payload": {"text": "A" * 20}})
        first_chunk = _receive_non_ping(source)
        assert first_chunk["type"] == "task_chunk"
        assert first_chunk["payload"]["text"] == "A" * 10

        target.send_json(
            {
                "type": "chunk",
                "correlation_id": "stream-task-1",
                "payload": {"text": "B<</return>>TRAILING"},
            }
        )
        second_chunk = _receive_non_ping(source)
        final = _receive_non_ping(source)
        assert second_chunk["type"] == "task_chunk"
        assert final["type"] == "task_result"
        assert final["payload"]["result"] == "A" * 20 + "B"
        assert "TRAILING" not in final["payload"]["result"]


def test_router_rule_and_default_selection():
    from synapse.router import select_target

    config = RouterConfig(
        default_agent="fallback",
        rules=[
            RouteRule(prefix="/code", keyword="review", target="codex"),
            RouteRule(keyword="search", target="research"),
        ],
    )
    assert select_target(config, "explicit", "anything") == "explicit"
    assert select_target(config, "", "/code review this") == "codex"
    assert select_target(config, "", "/code write this") == "fallback"
    assert select_target(config, "", "please search") == "research"


def test_auto_memory_is_opt_in_and_persona_scoped(tmp_path, monkeypatch):
    from synapse import server
    from synapse.task_queue import create_task

    config = GlobalConfig(db_path=str(tmp_path / "memory.db"))
    config.memory.scope = "persona"
    asyncio.run(init_db(config.db_path))
    stored: list[tuple[str, str]] = []

    async def fake_add_memory(_db_path, content, persona, *, memory_config):
        stored.append((persona, content))
        return 1

    monkeypatch.setattr(server, "add_memory", fake_add_memory)
    server._persona_interactions.clear()

    task_id = asyncio.run(create_task(config.db_path, "a", "b", "Bearer hidden", persona="alice"))
    asyncio.run(server._maybe_condense_memory(config, task_id, "sk-example-secret-value"))
    assert stored == []

    config.server.auto_memory_threshold = 1
    asyncio.run(server._maybe_condense_memory(config, task_id, "sk-example-secret-value"))
    assert stored[0][0] == "alice"
    assert "Bearer ***" in stored[0][1]
    assert "sk-***" in stored[0][1]

    anonymous = asyncio.run(create_task(config.db_path, "a", "b", "anonymous"))
    asyncio.run(server._maybe_condense_memory(config, anonymous, "result"))
    assert len(stored) == 1


def test_local_embedding_loader_does_not_hold_thread_lock_across_await(monkeypatch):
    from synapse.context import embedding

    calls = []

    class FakeModel:
        def __init__(self, name):
            calls.append(name)
            time.sleep(0.05)

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=FakeModel))
    monkeypatch.setattr(embedding, "_local_model", None)
    monkeypatch.setattr(embedding, "_local_model_name", "")
    monkeypatch.setattr(embedding, "_local_failures", {})

    async def load_twice():
        return await asyncio.wait_for(
            asyncio.gather(embedding._get_local_model("model-a"), embedding._get_local_model("model-a")),
            timeout=1,
        )

    first, second = asyncio.run(load_twice())
    assert first is second
    assert calls == ["model-a"]


def test_openai_embeddings_are_reordered_by_index(monkeypatch):
    from synapse.context import embedding

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": 1, "embedding": [2.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **_kwargs):
            return Response()

    monkeypatch.setattr(embedding.httpx, "AsyncClient", Client)
    config = MemoryConfig(
        embedding_provider="openai",
        embedding_model="test-model",
        embedding_api_url="https://embedding.example/v1",
    )
    vectors, status = asyncio.run(embedding.get_embeddings(["first", "second"], config))
    assert status is embedding.EmbeddingStatus.OK
    assert vectors == [[1.0, 0.0], [2.0, 0.0]]


def test_ollama_uses_local_default_url(monkeypatch):
    from synapse.context import embedding

    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [[1.0, 2.0]]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **_kwargs):
            seen["url"] = url
            return Response()

    monkeypatch.setattr(embedding.httpx, "AsyncClient", Client)
    config = MemoryConfig(embedding_provider="ollama", embedding_model="nomic-embed-text")
    vectors, status = asyncio.run(embedding.get_embeddings(["hello"], config))
    assert status is embedding.EmbeddingStatus.OK
    assert vectors == [[1.0, 2.0]]
    assert seen["url"] == "http://127.0.0.1:11434/api/embed"


def test_markdown_chunker_enforces_size_and_overlap():
    from synapse.context.chunker import MarkdownChunker

    chunker = MarkdownChunker(chunk_size=20, chunk_overlap=5, min_chunk_size=0, continuation_prefix="")
    chunks = chunker.chunk("0123456789" * 5)
    assert chunks
    assert all(len(chunk) <= 20 for chunk in chunks)
    assert chunks[0][-5:] == chunks[1][:5]

    markdown = "# Heading\n" + ("long paragraph " * 20)
    assert all(len(chunk) <= 30 for chunk in chunker.chunk(markdown, chunk_size=30))


def test_ollama_accepts_a_full_embed_endpoint(monkeypatch):
    from synapse.context import embedding

    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [[1.0]]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **_kwargs):
            seen["url"] = url
            return Response()

    monkeypatch.setattr(embedding.httpx, "AsyncClient", Client)
    config = MemoryConfig(
        embedding_provider="ollama",
        embedding_model="nomic-embed-text",
        embedding_api_url="http://ollama.local:11434/api/embed",
    )
    _, status = asyncio.run(embedding.get_embeddings(["hello"], config))
    assert status is embedding.EmbeddingStatus.OK
    assert seen["url"] == "http://ollama.local:11434/api/embed"


def test_subprocess_reads_stdout_while_writing_large_stdin():
    from synapse.agents.subprocess_agent import SubprocessAgent

    code = (
        "import sys; "
        "sys.stdout.write('x' * 200000); sys.stdout.flush(); "
        "data = sys.stdin.read(); print('stdin=' + str(len(data)))"
    )
    command = f'{sys.executable} -c "{code}"'
    agent = SubprocessAgent(
        {
            "extra": {
                "command": command,
                "timeout": 5,
                "max_output_bytes": 300000,
            }
        }
    )

    result = asyncio.run(agent._run_command("work", "pipe-task", "pipe-cid", stdin_data="i" * 200000, timeout=5))
    assert result["exit_code"] == 0
    assert "stdin=200000" in result["output"]


def test_worker_message_loop_processes_cancel_while_task_is_active():
    from synapse.agents.worker_utils import run_worker_message_loop

    started = asyncio.Event()
    cancelled = asyncio.Event()
    finished = asyncio.Event()
    cancel_calls = 0

    class FakeWebSocket:
        def __init__(self):
            self.step = 0
            self.sent = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.step == 0:
                self.step += 1
                return '{"type":"task","payload":{"task_id":"task-1"},"correlation_id":"task-1"}'
            if self.step == 1:
                self.step += 1
                await started.wait()
                return '{"type":"cancel","payload":{"task_id":"task-1"},"correlation_id":"task-1"}'
            await finished.wait()
            raise StopAsyncIteration

        async def send(self, message):
            self.sent.append(message)

    async def handle_task(_message):
        started.set()
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        finished.set()

    async def cancel_active():
        nonlocal cancel_calls
        cancel_calls += 1
        cancelled.set()

    asyncio.run(run_worker_message_loop(FakeWebSocket(), handle_task, cancel_active))
    assert cancel_calls >= 1
    assert started.is_set()
    assert finished.is_set()


def test_return_must_match_the_worker_active_task(api_client):
    with api_client.websocket_connect("/ws") as source, api_client.websocket_connect("/ws") as target:
        _register(source, "return-source")
        _register(target, "return-target")

        for task_id in ("return-task-1", "return-task-2"):
            source.send_json(
                {
                    "type": "send",
                    "correlation_id": task_id,
                    "payload": {"target": "return-target", "plan": task_id, "timeout": 30},
                }
            )
            assert _receive_non_ping(target)["type"] == "task"
            assert _receive_non_ping(source)["type"] == "task_queued"

        target.send_json({"type": "accept", "correlation_id": "return-task-1"})
        target.send_json(
            {
                "type": "return",
                "correlation_id": "return-task-2",
                "payload": {"task_id": "return-task-2", "result": "wrong task"},
            }
        )
        error = _receive_non_ping(target)
        assert error["type"] == "error"
        assert error["payload"]["code"] == "AGENT_BUSY"

        target.send_json(
            {
                "type": "return",
                "correlation_id": "return-task-1",
                "payload": {"task_id": "return-task-1", "result": "\u0000ok\u001b[31m"},
            }
        )
        result = _receive_non_ping(source)
        assert result["type"] == "task_result"
        assert "\u0000" not in result["payload"]["result"]
        assert "\u001b" not in result["payload"]["result"]


def test_direct_return_uses_accumulated_chunks_when_result_is_omitted(api_client):
    with api_client.websocket_connect("/ws") as source, api_client.websocket_connect("/ws") as target:
        _register(source, "chunk-source")
        _register(target, "chunk-target")
        source.send_json(
            {
                "type": "send",
                "correlation_id": "chunk-fallback-task",
                "payload": {"target": "chunk-target", "plan": "work", "timeout": 30},
            }
        )
        assert _receive_non_ping(target)["type"] == "task"
        assert _receive_non_ping(source)["type"] == "task_queued"
        target.send_json({"type": "accept", "correlation_id": "chunk-fallback-task"})
        target.send_json(
            {
                "type": "chunk",
                "correlation_id": "chunk-fallback-task",
                "payload": {"text": "streamed answer"},
            }
        )
        assert _receive_non_ping(source)["type"] == "task_chunk"
        target.send_json(
            {
                "type": "return",
                "correlation_id": "chunk-fallback-task",
                "payload": {"task_id": "chunk-fallback-task"},
            }
        )
        result = _receive_non_ping(source)
        while result["type"] == "task_chunk":
            result = _receive_non_ping(source)
        assert result["type"] == "task_result"
        assert result["payload"]["result"] == "streamed answer"


def test_terminal_task_transitions_are_compare_and_set(tmp_path):
    from synapse.task_queue import create_task, get_task, update_task_status
    from synapse.task_status import COMPLETABLE_TASK_STATUSES, NONTERMINAL_TASK_STATUSES

    db_path = str(tmp_path / "task-race.db")
    asyncio.run(init_db(db_path))
    task_id = asyncio.run(create_task(db_path, "source", "target", "work"))
    asyncio.run(update_task_status(db_path, task_id, "QUEUED"))

    async def race_terminal_updates():
        return await asyncio.gather(
            update_task_status(
                db_path,
                task_id,
                "COMPLETED",
                result="done",
                expected_statuses=COMPLETABLE_TASK_STATUSES,
            ),
            update_task_status(
                db_path,
                task_id,
                "TIMEOUT",
                result="late",
                expected_statuses=NONTERMINAL_TASK_STATUSES,
            ),
        )

    outcomes = asyncio.run(race_terminal_updates())
    assert outcomes.count(True) == 1
    assert asyncio.run(get_task(db_path, task_id))["status"] in {"COMPLETED", "TIMEOUT"}


def test_dispatch_transition_cannot_overwrite_fast_accept(tmp_path):
    from synapse.task_queue import create_task, get_task, update_task_status

    db_path = str(tmp_path / "fast-accept.db")
    asyncio.run(init_db(db_path))
    task_id = asyncio.run(create_task(db_path, "source", "target", "work"))
    asyncio.run(update_task_status(db_path, task_id, "QUEUED"))

    assert asyncio.run(update_task_status(db_path, task_id, "EXECUTING", expected_statuses=("QUEUED", "DISPATCHED")))
    assert not asyncio.run(update_task_status(db_path, task_id, "DISPATCHED", expected_statuses=("QUEUED",)))
    assert asyncio.run(get_task(db_path, task_id))["status"] == "EXECUTING"


def test_task_schema_migration_preserves_existing_rows(tmp_path):
    from synapse.task_queue import get_task, update_task_status

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                source_agent TEXT,
                target_agent TEXT,
                content TEXT NOT NULL,
                result TEXT,
                status TEXT NOT NULL DEFAULT 'CREATED'
                    CHECK (status IN ('CREATED','QUEUED','DISPATCHED','EXECUTING','COMPLETED','TIMEOUT','ERROR')),
                timeout INTEGER,
                connection_id TEXT,
                persona TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT
            );
            INSERT INTO tasks (id, type, content, status, created_at)
            VALUES ('legacy-task', 'send', 'work', 'CREATED', '2026-01-01T00:00:00+00:00');
        """)

    asyncio.run(init_db(db_path))
    assert asyncio.run(get_task(str(db_path), "legacy-task"))["content"] == "work"
    assert asyncio.run(update_task_status(str(db_path), "legacy-task", "ABANDONED"))


def test_startup_abandons_nonterminal_tasks(tmp_path):
    from synapse.task_queue import (
        abandon_incomplete_tasks,
        create_task,
        get_task,
        update_task_status,
    )

    db_path = str(tmp_path / "tasks.db")
    asyncio.run(init_db(db_path))
    task_id = asyncio.run(create_task(db_path, "source", "target", "work"))
    asyncio.run(update_task_status(db_path, task_id, "DISPATCHED"))

    assert asyncio.run(abandon_incomplete_tasks(db_path)) == 1
    task = asyncio.run(get_task(db_path, task_id))
    assert task["status"] == "ABANDONED"
    assert "restarted" in task["result"].lower()


def test_chunked_knowledge_batches_embeddings_before_database_write(tmp_path, monkeypatch):
    from synapse.context import knowledge

    events = []

    async def fake_get_embeddings(texts, _config):
        events.append(("embedding", len(texts)))
        return [[float(index), 1.0] for index, _ in enumerate(texts)], object()

    original_get_db = knowledge.get_db

    def tracked_get_db(db_path):
        events.append(("database", db_path))
        return original_get_db(db_path)

    monkeypatch.setattr(knowledge, "get_embeddings", fake_get_embeddings)
    monkeypatch.setattr(knowledge, "get_db", tracked_get_db)
    db_path = str(tmp_path / "knowledge.db")
    asyncio.run(init_db(db_path))

    ids = asyncio.run(
        knowledge.add_knowledge(
            db_path,
            "title",
            "# One\n" + ("a" * 9000),
            chunk=True,
            memory_config=MemoryConfig(),
        )
    )
    assert len(ids) > 1
    assert events[0][0] == "embedding"
    assert events[1][0] == "database"
    assert events[0][1] == len(ids)


def test_undelivered_task_stays_queued_for_reconnect(api_client, monkeypatch):
    from synapse.connection import connection_manager
    from synapse.task_queue import get_task

    original_send_or_queue = connection_manager.send_or_queue

    async def queue_task_only(agent_name, message, ttl=3600):
        if message.get("type") == "task":
            return False
        return await original_send_or_queue(agent_name, message, ttl)

    with api_client.websocket_connect("/ws") as source, api_client.websocket_connect("/ws") as target:
        _register(source, "queued-source")
        _register(target, "queued-target")
        monkeypatch.setattr(connection_manager, "send_or_queue", queue_task_only)
        source.send_json(
            {
                "type": "send",
                "correlation_id": "queued-for-reconnect",
                "payload": {"target": "queued-target", "plan": "work", "timeout": 30},
            }
        )
        assert _receive_non_ping(source)["type"] == "task_queued"
        task = asyncio.run(get_task(api_client.app.state.config.db_path, "queued-for-reconnect"))
        assert task["status"] == "QUEUED"


def test_redelivered_task_moves_from_queued_to_dispatched(tmp_path):
    from synapse import server
    from synapse.task_queue import create_task, get_task, update_task_status

    config = GlobalConfig(db_path=str(tmp_path / "redelivery.db"))
    asyncio.run(init_db(config.db_path))
    task_id = asyncio.run(create_task(config.db_path, "source", "target", "work", correlation_id="redelivered-task"))
    asyncio.run(update_task_status(config.db_path, task_id, "QUEUED"))

    messages = [
        {"type": "task", "payload": {"task_id": task_id}},
        {"type": "task_result", "payload": {"task_id": "not-a-task-delivery"}},
    ]
    asyncio.run(server._mark_redelivered_tasks_dispatched(config, messages))

    assert asyncio.run(get_task(config.db_path, task_id))["status"] == "DISPATCHED"


def test_failed_pending_delivery_keeps_queue_record_shape():
    from synapse.connection import ConnectionManager

    class FailingWebSocket:
        async def send_json(self, _message):
            raise ConnectionError("disconnected")

    class GoodWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    async def scenario():
        manager = ConnectionManager()
        message = {"type": "task", "payload": {"task_id": "pending-1"}}
        assert await manager.send_or_queue("worker", message, ttl=60) is False

        failing = FailingWebSocket()
        await manager.register("worker", failing, deliver_pending=False)
        assert await manager.deliver_pending("worker", failing) == []
        assert len(manager._pending["worker"][0]) == 3

        working = GoodWebSocket()
        await manager.register("worker", working, deliver_pending=False)
        assert await manager.deliver_pending("worker", working) == [message]
        assert working.messages == [message]
        assert "worker" not in manager._pending

    asyncio.run(scenario())


def test_live_log_reader_resets_after_rotation_and_rejects_symlink(tmp_path):
    from synapse.server import _log_end_state, _read_log_since

    log_path = tmp_path / "app.log"
    log_path.write_bytes(b"old\n")
    position, identity = _log_end_state(log_path)
    with log_path.open("ab") as handle:
        handle.write(b"next\n")
    chunk, position, identity = _read_log_since(log_path, position, identity)
    assert chunk == b"next\n"

    replacement = tmp_path / "replacement.log"
    replacement.write_bytes(b"new\n")
    replacement.replace(log_path)
    chunk, _, _ = _read_log_since(log_path, position, identity)
    assert chunk == b"new\n"

    target = tmp_path / "secret.log"
    target.write_bytes(b"secret")
    log_path.unlink()
    log_path.symlink_to(target)
    chunk, position, identity = _read_log_since(log_path, 0, None)
    assert chunk == b""
    assert position == 0
    assert identity is None
