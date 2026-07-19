# REST API and WebSocket Protocol

## Version and Authentication

- Stable REST prefix: `/api/v1/*`; Alpha legacy paths remain compatible.
- WebSocket: `/api/v1/ws`; legacy `/ws` is equivalent.
- Discovery: `GET /version` or `GET /api/v1/version`.
- Current and minimum `protocol_version`: 1.
- REST administration/default context: `Authorization: Bearer <server.api_key>`.
- WebSocket: `Authorization: Bearer <server.worker_api_key>`; the administrator key is also accepted.

The current `worker_api_key` is a shared message-bus credential: a holder can register an unused name and send tasks or broadcasts to other Agents, but cannot access REST administration. Put mutually untrusted workers on separate instances.

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
| `GET/POST` | `/connection-prompt` | Generate a bootstrap prompt; see the [prompt guide](prompts.md) |
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
| `GET/POST` | `/admin/tasks` | Query/create human-originated Agent tasks |
| `GET` | `/admin/tasks/{id}` | Full task, result, and state |
| `POST` | `/admin/tasks/{id}/cancel` | Cancel with a human reason |
| `GET` | `/admin/tasks/stream` | SSE state events and bounded live fragments |
| `GET` | `/admin/stats/agents` | Invocation counts by Agent/Profile/purpose/outcome |
| `POST` | `/admin/agents/probe` | Broadcast/targeted WS probe without an LLM call |
| `GET/POST` | `/admin/agent-tags`, `/admin/agent-tags/refresh` | Capability tags and read-only self-assessment |
| `GET` | `/admin/task-groups[/{id}]` | Auction/team task groups |
| `POST` | `/admin/auctions`, `/admin/auctions/{id}/select` | Create auction and select a bid |
| `POST` | `/admin/teams`, `/admin/teams/{id}/approve` | Create a team plan and approve assignments |

Config updates use an async lock, advisory file lock, and same-directory atomic replacement. Server/key settings and the embedding key are not writable through this API.

### Human-Originated Web Tasks

Terminal task and task-group records are retained for `server.task_history_hours=168` hours by default. Invocation counts are aggregated daily and do not depend on retaining task bodies.

The browser does not register as a WebSocket Agent. `POST /admin/tasks` creates a durable `source_kind=web` task with administrator authentication. It accepts `agent`, optional `profile`, `plan`, `timeout`, and `session_alias`. Read final output from task detail; SSE carries state and individual live fragments capped at 2048 characters. Cancellation accepts `{"reason":"..."}`, persists the reason, removes undelivered work, and sends `cancel` only to an online worker.

An auction has at most eight candidates and creates one tracked `purpose=bid` task per endpoint. Bids and team planning require profiles declaring `advisory_safe: true`. The server adds a fixed read-only proposal instruction, while the real boundary remains the local command, sandbox, and OS user. A human must select a completed bid and explicitly provide `executor: {agent, profile}` before execution; the bidding endpoint is never implicitly reused as the executor. A completed team plan enters `AWAITING_APPROVAL`; a human then submits at most eight explicit assignments. Selection, approval, and cancellation claim the group state atomically, so duplicate submissions cannot fan out work twice. States derived from child tasks are also persisted with compare-and-set, so a stale query cannot roll back a human action.

Auction selection example:

```json
{
  "bid_task_id": "<completed-bid-task-id>",
  "executor": {"agent": "local-dispatcher", "profile": "codex"},
  "plan": "the final human-approved requirement",
  "timeout": 1800,
  "session_alias": ""
}
```

`/admin/agent-tags/refresh` is also an advisory task. Its bounded JSON result is stored as `self_reported`. Self-reported tags and bid claims are untrusted display data and never affect authentication, routing authorization, or command selection.

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

At timeout the server marks the task `TIMEOUT`, reports an error to an Agent source or an SSE terminal event to the Web source, removes undelivered work from the reconnect queue, and sends `cancel` only when the target is online. Built-in workers terminate the active child process group immediately.

A target disconnect marks its `DISPATCHED/EXECUTING` tasks `ERROR` and reports `TARGET_DISCONNECTED`. A source disconnect does not abandon tasks already sent.

### Connectivity Probe

An administrator probe sends `{"type":"probe","payload":{"probe_id":"..."}}`. A capable worker answers with `probe_ack` containing only that ID, `busy`, and `queue_depth`. Identity comes from the registered connection, not from the acknowledgement. Probes are transient, never queued, and do not start a child process or LLM.

### Broadcast

```json
{"type":"broadcast","payload":{"data":{"event":"refresh"}}}
```

The server sends this to all currently online connections and replies with `broadcast_ack` containing sent/targets. Broadcasts are neither persisted nor redelivered. The sender name comes from the registered connection, but the payload remains an untrusted notification and must not automatically execute as a task.

## Limits and Errors

The default HTTP/WS body limit is 1048576 bytes and the configured maximum is 16 MiB. Frame and connection-attempt rates have independent limits. Plans, forwarded options, personas, registration metadata, and reconnect queues have tighter bounds.

Common codes:

- `UNAUTHORIZED`, `AUTH_REQUIRED`
- `INVALID_REQUEST`, `INVALID_NAME`, `INVALID_CORRELATION_ID`
- `DUPLICATE_TASK_ID`, `TASK_NOT_OWNED`, `INVALID_TASK_STATE`, `AGENT_BUSY`
- `ROUTING_NOT_FOUND`, `NAME_CONFLICT`, `NOT_REGISTERED`, `ALREADY_REGISTERED`
- `TIMEOUT`, `TARGET_DISCONNECTED`, `PAYLOAD_TOO_LARGE`
- `UNSUPPORTED_PROTOCOL`, `UNKNOWN_MESSAGE_TYPE`
