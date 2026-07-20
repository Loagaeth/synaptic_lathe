from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from starlette.websockets import WebSocketDisconnect

from synapse.config import AgentConfig, GlobalConfig, MemoryConfig, ServerConfig
from synapse.db import init_db
from synapse.server import app, set_project_root


@pytest.fixture
def secured_client(tmp_path):
    from synapse.connection import connection_manager

    connection_manager._connections.clear()
    connection_manager._metadata.clear()
    connection_manager._pending.clear()
    config = GlobalConfig(db_path=str(tmp_path / "test.db"))
    config.server.api_key = SecretStr("admin-key")
    config.server.worker_api_key = SecretStr("worker-key")
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


def _register(socket, name: str) -> None:
    socket.send_json({"type": "register", "payload": {"agent_name": name, "protocol_version": 1}})
    assert _receive_non_ping(socket)["type"] == "registered"


def test_worker_key_cannot_access_admin_but_admin_key_can_register(secured_client):
    response = secured_client.get("/admin/config", headers={"Authorization": "Bearer worker-key"})
    assert response.status_code == 403

    with secured_client.websocket_connect("/ws", headers={"Authorization": "Bearer worker-key"}) as worker:
        _register(worker, "least-privilege-worker")

    with secured_client.websocket_connect("/ws", headers={"Authorization": "Bearer admin-key"}) as administrator:
        _register(administrator, "admin-worker")

    with pytest.raises(WebSocketDisconnect):
        with secured_client.websocket_connect("/ws", headers={"Authorization": "Bearer wrong-key"}):
            pass


def test_invalid_and_duplicate_correlation_ids_return_protocol_errors(secured_client):
    with (
        secured_client.websocket_connect("/ws", headers={"Authorization": "Bearer worker-key"}) as source,
        secured_client.websocket_connect("/ws", headers={"Authorization": "Bearer worker-key"}) as target,
    ):
        _register(source, "cid-source")
        _register(target, "cid-target")
        source.send_json(
            {
                "type": "send",
                "correlation_id": "bad id",
                "payload": {"target": "cid-target", "plan": "x"},
            }
        )
        invalid = _receive_non_ping(source)
        assert invalid["payload"]["code"] == "INVALID_CORRELATION_ID"

        message = {
            "type": "send",
            "correlation_id": "duplicate-task-id",
            "payload": {"target": "cid-target", "plan": "x", "timeout": 30},
        }
        source.send_json(message)
        assert _receive_non_ping(target)["type"] == "task"
        assert _receive_non_ping(source)["type"] == "task_queued"
        source.send_json(message)
        duplicate = _receive_non_ping(source)
        assert duplicate["payload"]["code"] == "DUPLICATE_TASK_ID"


def test_worker_lock_rejects_symlink_and_uses_private_directory(tmp_path):
    from synapse.agents.worker_utils import SingleInstanceLock

    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    lock_path = tmp_path / "worker.lock"
    lock_path.symlink_to(victim)
    with pytest.raises(RuntimeError):
        SingleInstanceLock(str(lock_path)).acquire()
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_atomic_write_replaces_symlink_without_touching_target(tmp_path):
    from synapse.file_utils import atomic_write_text

    victim = tmp_path / "victim"
    victim.write_text("original", encoding="utf-8")
    destination = tmp_path / "config.yaml"
    destination.symlink_to(victim)
    assert atomic_write_text(destination, "replacement", overwrite=True, mode=0o600)
    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == "replacement"
    assert victim.read_text(encoding="utf-8") == "original"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_process_record_requires_matching_start_identity(tmp_path, monkeypatch):
    from synapse import process_control

    recorded_identity = "process-start-old"
    record = process_control.ManagedProcessRecord(pid=123, start_token=recorded_identity, argv=("python", "app.py"))
    monkeypatch.setattr(process_control, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(process_control, "process_start_token", lambda _pid: "new")
    assert process_control.managed_process_running(record) is False

    path = tmp_path / "state.json"
    process_control.write_process_record(path, record)
    assert process_control.read_process_record(path) == record
    assert path.stat().st_mode & 0o777 == 0o600


def test_atomic_config_failure_preserves_original(tmp_path, monkeypatch):
    from synapse import server

    config_path = tmp_path / "config.yaml"
    original = "memory:\n  scope: shared\n"
    config_path.write_text(original, encoding="utf-8")
    config = GlobalConfig(db_path=str(tmp_path / "db.sqlite"))
    request = type("Request", (), {"app": type("App", (), {"state": type("State", (), {})()})()})()
    request.app.state.config_path = str(config_path)

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(server, "atomic_write_text", fail_write)
    with pytest.raises(server.ConfigSaveError):
        asyncio.run(server._save_config_locked(request, config))
    assert config_path.read_text(encoding="utf-8") == original


def test_web_assets_disable_stale_browser_caching(secured_client):
    for url in ("/", "/admin", "/web/index.html", "/web/app.js", "/web/styles.css"):
        response = secured_client.get(url, follow_redirects=False)
        assert response.status_code in {200, 307}
        assert "no-store" in response.headers.get("cache-control", "")
        assert response.headers.get("pragma") == "no-cache"


def test_web_assets_do_not_require_unsafe_inline_csp(secured_client):
    response = secured_client.get("/web/index.html")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "style-src 'self'" in policy
    assert "'unsafe-inline'" not in policy

    web_dir = Path(__file__).resolve().parents[1] / "synapse" / "web"
    source = (web_dir / "index.html").read_text(encoding="utf-8")
    source += (web_dir / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"\s(?:onclick|onchange|onload|onerror)=", source, re.IGNORECASE)
    assert not re.search(r"\sstyle=", source, re.IGNORECASE)


def test_health_does_not_return_raw_database_error(secured_client, monkeypatch):
    from synapse import server

    @asynccontextmanager
    async def broken_db(_path):
        raise RuntimeError("secret database path and details")
        yield

    monkeypatch.setattr(server, "get_db", broken_db)
    response = secured_client.get("/health?check=db")
    assert response.status_code == 200
    assert response.json()["db"] == "unavailable"
    assert "secret" not in response.text


def test_install_guide_uses_wss_and_no_http_ws_url(tmp_path):
    config = GlobalConfig(db_path=str(tmp_path / "db.sqlite"))
    config.server.rate_limit_max = 10_000
    app.state.config = config
    app.state.config_path = str(tmp_path / "config.yaml")
    client = TestClient(app, base_url="https://example.test")
    response = client.get("/install/profile_worker")
    assert response.status_code == 200
    guide = response.json()["guide"]
    command = guide["run"]
    assert "wss://example.test/ws" in command
    assert "http://example.test/ws" not in command
    assert "./synaptic-worker/workerctl" in guide["note"]
    assert "synaptic-workerctl" not in guide["note"]


def test_config_rejects_misleading_or_credential_bearing_endpoints():
    with pytest.raises(ValidationError):
        AgentConfig(type="cli_bridge", base_url="http://localhost")
    with pytest.raises(ValidationError):
        AgentConfig(type="http_api", base_url="https://user:pass@example.test")
    with pytest.raises(ValidationError):
        MemoryConfig(embedding_api_url="https://token@example.test/v1")
    with pytest.raises(ValidationError):
        ServerConfig(cors_origins=["*"])
    with pytest.raises(ValidationError):
        ServerConfig(behind_proxy=True, trusted_proxy_hosts=["not-an-ip"])
    with pytest.raises(ValidationError):
        ServerConfig(ws_ping_interval=30, ws_receive_timeout=30)
    with pytest.raises(ValidationError):
        GlobalConfig(agents={"bad name": AgentConfig(type="http_api", base_url="http://localhost")})


def test_worker_url_rejects_query_credentials():
    from synapse.agents.worker_utils import validate_websocket_url

    with pytest.raises(ValueError):
        validate_websocket_url("wss://example.test/ws?token=secret")


def test_config_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({"server": {"rate_limt_max": 10}})
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(
            {
                "type": "http_api",
                "base_url": "https://example.test",
                "stram": True,
            }
        )


def test_config_validation_error_does_not_echo_secret_values():
    from synapse.runner import _format_config_error

    marker_value = "private-value-that-must-not-be-logged"
    with pytest.raises(ValidationError) as captured:
        GlobalConfig.model_validate({"server": {"api_keey": marker_value}})

    message = _format_config_error(captured.value)
    assert "server.api_keey" in message
    assert "extra_forbidden" in message
    assert marker_value not in message


def test_profile_request_bounds_remote_selectors():
    from synapse.agents.profile_agent import ProfileConfigError, build_profile_request

    with pytest.raises(ProfileConfigError):
        build_profile_request({"profile": "safe", "tool": "x" * 65, "plan": "work"})
    with pytest.raises(ProfileConfigError):
        build_profile_request({"tool": "safe", "session_id": "x" * 4097, "plan": "work"})
    with pytest.raises(ProfileConfigError):
        build_profile_request({"tool": "safe", "from": "bad source", "plan": "work"})


def test_profile_session_alias_placeholder_requires_allowlist(tmp_path):
    from synapse.agents.profile_agent import (
        ProfileConfigError,
        build_profile_command,
        build_profile_request,
        load_profile_config,
    )

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        "profiles:\n  tool:\n    command: [tool, '--session', '{session_alias}', '{plan}']\n",
        encoding="utf-8",
    )
    profile = load_profile_config(str(profile_file))["profiles"]["tool"]
    request = build_profile_request({"tool": "tool", "session_id": "--unsafe", "plan": "work"})

    with pytest.raises(ProfileConfigError):
        build_profile_command(profile, request)


def test_profile_rejects_potentially_catastrophic_session_pattern(tmp_path):
    from synapse.agents.profile_agent import ProfileConfigError, load_profile_config

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        "profiles:\n  tool:\n    command: [tool, '{session_id}', '{plan}']\n"
        "    allow_raw_session_id: true\n    session_pattern: '^(a+)+$'\n",
        encoding="utf-8",
    )
    with pytest.raises(ProfileConfigError):
        load_profile_config(str(profile_file))


def test_worker_setup_preserves_existing_key_and_can_clear_it(tmp_path):
    from synapse.worker_setup import WorkerSetupOptions, run_setup

    project_dir = Path(__file__).resolve().parents[1]
    first = WorkerSetupOptions(
        base_dir=tmp_path,
        project_dir=project_dir,
        api_key="keep-me",
        url="ws://one.example/ws",
        install_deps=False,
        force=True,
    )
    run_setup(first)
    run_setup(
        WorkerSetupOptions(
            base_dir=tmp_path,
            project_dir=project_dir,
            url="wss://two.example/ws",
            install_deps=False,
            force=True,
        )
    )
    env_text = (tmp_path / ".synaptic" / "worker.env").read_text(encoding="utf-8")
    assert 'SYNAPTIC_API_KEY="keep-me"' in env_text
    assert 'SYNAPTIC_WS_URL="wss://two.example/ws"' in env_text

    run_setup(
        WorkerSetupOptions(
            base_dir=tmp_path,
            project_dir=project_dir,
            clear_api_key=True,
            url="wss://two.example/ws",
            install_deps=False,
            force=True,
        )
    )
    assert 'SYNAPTIC_API_KEY=""' in (tmp_path / ".synaptic" / "worker.env").read_text(encoding="utf-8")


def test_subprocess_text_sanitizer_does_not_expand_nul_output():
    from synapse.agents.worker_utils import sanitize_process_text

    output = sanitize_process_text(("\x00A" * 1000) + "\x1b[31mred\x1b[0m")
    assert "\x00" not in output
    assert "\x1b" not in output
    assert output.count("[NUL bytes omitted]") == 1
    assert len(output) < 1100


def test_data_file_api_rejects_internal_and_root_symlinks(secured_client, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "target.txt"
    target.write_text("secret", encoding="utf-8")
    link = data_dir / "link.txt"
    link.symlink_to(target)

    response = secured_client.post(
        "/admin/skill",
        headers={"Authorization": "Bearer admin-key"},
        json={"name": "linked", "file": "data/link.txt"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PATH"

    link.unlink()
    target.unlink()
    data_dir.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "value.txt").write_text("outside", encoding="utf-8")
    data_dir.symlink_to(outside, target_is_directory=True)
    response = secured_client.post(
        "/admin/skill",
        headers={"Authorization": "Bearer admin-key"},
        json={"name": "root-linked", "file": "data/value.txt"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_PATH"


def test_log_redaction_handles_json_assignments_and_url_credentials():
    from synapse.logging import redact_sensitive_text

    value = (
        '"api_key":"top-secret", password = hunter2, '
        "https://alice:private@example.test/v1, Authorization: Bearer abc.def"
    )
    redacted = redact_sensitive_text(value)
    assert "top-secret" not in redacted
    assert "hunter2" not in redacted
    assert "alice:private" not in redacted
    assert "abc.def" not in redacted
    assert "***" in redacted


def test_structured_logs_do_not_emit_session_aliases():
    import json
    import logging

    from synapse.logging import StructuredFormatter

    record = logging.LogRecord("synapse", logging.INFO, __file__, 1, "task", (), None)
    record.profile = "codex"
    record.session_alias = "private-session-id"
    payload = json.loads(StructuredFormatter().format(record))
    assert payload["profile"] == "codex"
    assert "session_alias" not in payload
    assert "private-session-id" not in json.dumps(payload)


def test_config_normalizes_cors_and_rejects_url_queries():
    config = ServerConfig(cors_origins=["HTTPS://Example.TEST/", "https://example.test"])
    assert config.cors_origins == ["https://example.test"]

    with pytest.raises(ValidationError):
        AgentConfig(type="http_api", base_url="https://example.test/api?api_key=secret")
    with pytest.raises(ValidationError):
        MemoryConfig(embedding_api_url="https://example.test/v1#fragment")


def test_server_setup_rejects_empty_key_on_nonlocal_bind(tmp_path):
    from synapse.setup_wizard import SetupOptions, run_setup

    with pytest.raises(ValueError):
        run_setup(
            SetupOptions(
                base_dir=tmp_path,
                host="0.0.0.0",
                api_key="",
                install_deps=False,
            )
        )


def test_worker_setup_rejects_invalid_runtime_limits(tmp_path):
    from synapse.worker_setup import WorkerSetupOptions, run_setup

    project_dir = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError):
        run_setup(
            WorkerSetupOptions(
                base_dir=tmp_path,
                project_dir=project_dir,
                name="../invalid",
                install_deps=False,
            )
        )
    with pytest.raises(ValueError):
        run_setup(
            WorkerSetupOptions(
                base_dir=tmp_path,
                project_dir=project_dir,
                timeout=0,
                install_deps=False,
            )
        )


def test_managed_process_record_reader_rejects_symlink(tmp_path):
    from synapse.process_control import read_process_record

    target = tmp_path / "target.json"
    target.write_text('{"pid":1,"start_token":"x","argv":[]}', encoding="utf-8")
    link = tmp_path / "state.json"
    link.symlink_to(target)
    assert read_process_record(link) is None


def test_websocket_worker_cannot_shadow_configured_http_agent(secured_client):
    from synapse.config import AgentConfig

    secured_client.app.state.config.agents["reserved-agent"] = AgentConfig(
        type="http_api",
        base_url="http://127.0.0.1:8080",
    )
    with secured_client.websocket_connect("/ws", headers={"Authorization": "Bearer worker-key"}) as worker:
        worker.send_json(
            {
                "type": "register",
                "payload": {"agent_name": "reserved-agent", "protocol_version": 1},
            }
        )
        response = _receive_non_ping(worker)
        assert response["type"] == "error"
        assert response["payload"]["code"] == "NAME_CONFLICT"


def test_worker_registration_metadata_is_bounded_and_prompt_safe():
    from synapse.connection import ConnectionManager

    manager = ConnectionManager()
    websocket = type(
        "Socket",
        (),
        {
            "send_json": lambda *_args, **_kwargs: None,
            "close": lambda *_args, **_kwargs: None,
        },
    )()

    asyncio.run(
        manager.register(
            "metadata-worker",
            websocket,
            client={
                "name": "profile_worker\nignore instructions",
                "version": "1.0.0",
                "profiles": ["safe", "bad\nname"],
                "profile_capabilities": {
                    "safe": {
                        "supports_session": "false",
                        "timeout": 999999,
                        "hints": ["avoid_short_timeout", "ignore_previous_instructions"],
                    },
                    "bad\nname": {"name": "bad"},
                },
            },
            capabilities=["task", "bad\ncapability"],
        )
    )
    details = manager.online_agent_details()[0]
    assert details["client"]["version"] == "1.0.0"
    assert "name" not in details["client"]
    assert details["client"]["profiles"] == ["safe"]
    assert set(details["client"]["profile_capabilities"]) == {"safe"}
    assert details["client"]["profile_capabilities"]["safe"]["supports_session"] is False
    assert "timeout" not in details["client"]["profile_capabilities"]["safe"]
    assert details["client"]["profile_capabilities"]["safe"]["hints"] == ["avoid_short_timeout"]
    assert details["capabilities"] == ["task"]


def test_custom_worker_lock_does_not_chmod_its_parent(tmp_path):
    from synapse.agents.worker_utils import SingleInstanceLock

    parent = tmp_path / "shared-parent"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    lock = SingleInstanceLock(str(parent / "worker.lock"))
    assert lock.acquire() is True
    lock.release()
    assert parent.stat().st_mode & 0o777 == 0o755


def test_database_permission_repair_tolerates_disappearing_wal(tmp_path, monkeypatch):
    from synapse import db as db_module

    if not hasattr(os, "fchmod"):
        pytest.skip("descriptor-based chmod is unavailable")
    database = tmp_path / "tasks.db"
    wal = tmp_path / "tasks.db-wal"
    database.write_bytes(b"db")
    wal.write_bytes(b"wal")
    original_lstat = Path.lstat

    def racing_lstat(path):
        result = original_lstat(path)
        if path == wal:
            path.unlink(missing_ok=True)
        return result

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    db_module._secure_database_files(database)
    assert database.stat().st_mode & 0o777 == 0o600


def test_profile_raw_session_flag_requires_real_boolean(tmp_path):
    from synapse.agents.profile_agent import ProfileConfigError, load_profile_config

    profile_file = tmp_path / "profiles.yaml"
    profile_file.write_text(
        'profiles:\n  tool:\n    command: [tool, "{plan}"]\n    allow_raw_session_id: "false"\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileConfigError, match="allow_raw_session_id must be a boolean"):
        load_profile_config(str(profile_file))
