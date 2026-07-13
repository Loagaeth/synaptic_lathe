# REST API and WebSocket Protocol

## Version and Authentication

- Stable REST prefix: `/api/v1/*`; Alpha legacy paths remain compatible.
- WebSocket: `/api/v1/ws`; legacy `/ws` is equivalent.
- Discovery: `GET /version` or `GET /api/v1/version`.
- Current and minimum `protocol_version`: 1.
- REST administration/default context: `Authorization: Bearer <server.api_key>`.
- WebSocket: `Authorization: Bearer <server.worker_api_key>`; the administrator key is also accepted.

`public_read_context: true` opens only the five GET context endpoint groups. `POST /context/memory` and `/context/knowledge` still require the administrator key to prevent anonymous embedding spend. It never opens administration, logs, connection prompts, or WebSocket.

## REST

### Public and Query Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health?check=db` | Minimal health and optional DB probe |
| `GET` | `/version` | Server/API/WS metadata and auth flags |
| `GET` | `/context?persona=x` | Recent context summary |
| `GET` | `/context/agents` | Configured Agents, online workers, capabilities |
| `POST` | `/context/memory` | Semantic/keyword memory search |
| `POST` | `/context/knowledge` | Semantic/keyword knowledge search |
| `GET` | `/context/skills?detail=1` | Skills |
| `GET` | `/context/personas?detail=1` | Personas |
| `GET` | `/context/prompts?detail=1` | Prompt documents |
| `GET/POST` | `/connection-prompt` | Generate a bootstrap prompt |
| `GET` | `/install/{type}` | Generate an install guide |

`/context/agents` exposes declared client name/version, capabilities, profile names, suggested timeout, output limit, and session aliases. It does not expose executable paths, environment values, or real session IDs.

### Administration

| Method | Path | Description |
|--------|------|-------------|
| `POST/DELETE` | `/admin/memory` | Add/delete memory; deletion accepts `?id=&persona=` |
| `POST/DELETE` | `/admin/skill` | Add/update/delete skill |
| `POST/DELETE` | `/admin/knowledge` | Add/delete knowledge; `?chunk=true` on writes, `?id=&persona=` on deletes |
| `POST/DELETE` | `/admin/persona` | Set/delete persona |
| `POST/DELETE` | `/admin/prompt` | Set/delete prompt document |
| `GET/POST` | `/admin/config` | Read redacted config; update memory only |
| `GET` | `/admin/logs` | Recent structured logs |
| `GET` | `/admin/logs/stream` | SSE live log stream |

Config updates use an async lock, advisory file lock, and same-directory atomic replacement. Server/key settings and the embedding key are not writable through this API.

## WebSocket

### Connect and Register

```text
wss://<public-host>/api/v1/ws
Authorization: Bearer <worker-api-key>
```

For same-host development use `ws://127.0.0.1:9112/ws`. Probe before registration:

```json
{"type":"hello"}
```

Recommended registration:

```json
{
  "type": "register",
  "payload": {
    "agent_name": "local-dispatcher",
    "protocol_version": 1,
    "client": {
      "name": "custom-worker",
      "version": "0.1.0",
      "capabilities": ["task", "accept", "return"]
    }
  }
}
```

The response includes session/connection IDs, protocol/API versions, `max_body_bytes`, a banner, and the registration-time `pending_available` count; actual redelivery starts after the acknowledgement. A duplicate online name, or one reserved by a configured HTTP adapter, receives `NAME_CONFLICT` and the new connection closes.

The server sends `{"type":"ping"}`; reply with `{"type":"pong"}`. Built-in workers keep receiving heartbeat and cancellation frames while a child runs.

### Send and Route

```json
{
  "type": "send",
  "correlation_id": "caller-generated-id",
  "payload": {
    "target": "local-dispatcher",
    "plan": "review the repository",
    "timeout": 600,
    "persona": "alice",
    "profile": "codex",
    "session_id": "main"
  }
}
```

- `correlation_id` is generated when omitted; supplied IDs must contain 1-128 safe characters and be globally unique.
- An explicit `target` wins. Otherwise rules match in order, followed by `default_agent`.
- A rule containing both prefix and keyword requires both.
- Timeout is bounded to 1-3600 seconds.
- `profile/tool` and `session_id/session/ssid` are narrow strings mapped by the target's local allowlist.
- Shared memory mode normalizes persona to `shared`; persona mode preserves an explicit value.
- `provider/model/username/stream` are controlled overrides only for configured HTTP adapters.

The source first receives `task_queued`. The target receives:

```json
{
  "type": "task",
  "correlation_id": "...",
  "payload": {
    "task_id": "...",
    "plan": "...",
    "from": "caller",
    "timeout": 600,
    "persona": "alice",
    "profile": "codex"
  }
}
```

### Accept, Stream, Return

```json
{"type":"accept","correlation_id":"task-id"}
```

Optional stream fragment:

```json
{"type":"chunk","payload":{"text":"partial"},"correlation_id":"task-id"}
```

An online source receives `task_chunk`; chunks are not queued offline. `<</return>>` can terminate the stream and text after the marker is discarded.

Final return:

```json
{
  "type": "return",
  "payload": {
    "task_id": "task-id",
    "result": "final answer",
    "exit_code": 0,
    "output_truncated": false
  },
  "correlation_id": "task-id"
}
```

`result` or `output` is persisted as the SQLite task result. A return must belong to the worker and cannot conflict with its active task; repeated returns for terminal tasks are rejected. Completion, timeout, and disconnect use atomic conditional updates so only one terminal outcome wins. Text fields are stripped of NUL/terminal controls, and the serialized result must fit `max_body_bytes`.

The source receives `task_result`. If disconnected, it is placed in a bounded in-process queue. A restart loses that queue, while SQLite retains task state and result. At startup, nonterminal tasks left by the previous process are marked `ABANDONED`.

### Timeout, Disconnect, Cancel

At timeout the server marks the task `TIMEOUT`, reports an error to the source, and sends the target a `cancel` frame containing task ID and reason. Built-in workers terminate the active child process group immediately.

A target disconnect marks its `DISPATCHED/EXECUTING` tasks `ERROR` and reports `TARGET_DISCONNECTED`. A source disconnect does not abandon tasks already sent.

### Broadcast

```json
{"type":"broadcast","payload":{"data":{"event":"refresh"}}}
```

The server sends this to all currently online connections and replies with `broadcast_ack` containing sent/targets. Broadcasts are neither persisted nor redelivered.

## Limits and Errors

The default HTTP/WS body limit is 1048576 bytes and the configured maximum is 16 MiB. Frame and connection-attempt rates have independent limits. Plans, forwarded options, personas, registration metadata, and reconnect queues have tighter bounds.

Common codes:

- `UNAUTHORIZED`, `AUTH_REQUIRED`
- `INVALID_REQUEST`, `INVALID_NAME`, `INVALID_CORRELATION_ID`
- `DUPLICATE_TASK_ID`, `TASK_NOT_OWNED`, `INVALID_TASK_STATE`, `AGENT_BUSY`
- `ROUTING_NOT_FOUND`, `NAME_CONFLICT`, `NOT_REGISTERED`, `ALREADY_REGISTERED`
- `TIMEOUT`, `TARGET_DISCONNECTED`, `PAYLOAD_TOO_LARGE`
- `UNSUPPORTED_PROTOCOL`, `UNKNOWN_MESSAGE_TYPE`
