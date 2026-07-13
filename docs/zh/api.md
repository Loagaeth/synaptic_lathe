# REST API 与 WebSocket 协议

## 版本与认证

- REST 稳定前缀：`/api/v1/*`；Alpha 历史路径继续兼容。
- WebSocket：`/api/v1/ws`；历史 `/ws` 等价。
- 协议发现：`GET /version` 或 `GET /api/v1/version`。
- 当前 `protocol_version=1`，最低支持版本也是 1。
- REST 管理/默认上下文认证：`Authorization: Bearer <server.api_key>`。
- WebSocket 认证：`Authorization: Bearer <server.worker_api_key>`；管理员 key 也可用。

`public_read_context: true` 只公开五组 GET 上下文端点；`POST /context/memory` 和 `/context/knowledge` 仍需管理员 key，避免匿名触发 embedding。它不会公开管理、日志、连接提示词或 WebSocket。

## REST

### 公开与查询

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health?check=db` | 最小健康状态；可选数据库探测 |
| `GET` | `/version` | 服务/API/WS 版本、能力和认证标记 |
| `GET` | `/context?persona=x` | 最近上下文汇总 |
| `GET` | `/context/agents` | 配置 Agent、在线 worker 和能力声明 |
| `POST` | `/context/memory` | 记忆语义/关键词搜索 |
| `POST` | `/context/knowledge` | 知识语义/关键词搜索 |
| `GET` | `/context/skills?detail=1` | 技能 |
| `GET` | `/context/personas?detail=1` | 人设 |
| `GET` | `/context/prompts?detail=1` | 提示词文档 |
| `GET/POST` | `/connection-prompt` | 生成 bootstrap 提示词 |
| `GET` | `/install/{type}` | 生成安装指南 |

`/context/agents` 只公开 worker 自报的客户端名、版本、能力、profile 名、建议 timeout、输出上限和 session alias。它不返回命令路径、环境值或真实 session id。

### 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST/DELETE` | `/admin/memory` | 添加/删除记忆；删除使用 `?id=&persona=` |
| `POST/DELETE` | `/admin/skill` | 添加/更新/删除技能 |
| `POST/DELETE` | `/admin/knowledge` | 添加/删除知识；写入可用 `?chunk=true`，删除使用 `?id=&persona=` |
| `POST/DELETE` | `/admin/persona` | 设置/删除人设 |
| `POST/DELETE` | `/admin/prompt` | 设置/删除提示词文档 |
| `GET/POST` | `/admin/config` | 查看脱敏配置；只写 memory 段 |
| `GET` | `/admin/logs` | 最近结构化日志 |
| `GET` | `/admin/logs/stream` | SSE 实时日志 |

配置写回使用进程内锁、文件锁和同目录原子替换。管理员 API 不允许修改 server/key，embedding key 也只能手工编辑 `config.yaml`。

## WebSocket

### 连接与注册

```text
wss://<public-host>/api/v1/ws
Authorization: Bearer <worker-api-key>
```

本地可使用 `ws://127.0.0.1:9112/ws`。注册前可探测：

```json
{"type":"hello"}
```

推荐注册：

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

返回包含 `session_id`、`connection_id`、协议/API 版本、`max_body_bytes`、字符画和注册时的 `pending_available` 待补发数量；注册确认后才开始实际补发。同名 Agent 已在线，或名称已被配置 HTTP adapter 保留时，返回 `NAME_CONFLICT` 并关闭新连接。

服务端定期发送 `{"type":"ping"}`，客户端回复 `{"type":"pong"}`。内置 worker 在子进程执行期间仍持续接收心跳和取消帧。

### 发送与路由

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

- `correlation_id` 可省略，由服务端生成；自定义值需 1-128 个安全字符并全局唯一。
- `target` 显式提供时优先；省略时按 `router.rules` 顺序匹配，最后使用 `default_agent`。
- 同一 rule 同时设置 prefix/keyword 时必须两者都匹配。
- timeout 被限制在 1-3600 秒。
- `profile/tool` 和 `session_id/session/ssid` 只是受限字符串，目标 worker 再映射本地 allowlist。
- `persona` 在 shared 模式会归一为 `shared`；persona 模式保留显式值。
- `provider/model/username/stream` 只作为配置 HTTP adapter 的受控 override。

服务端先返回：

```json
{"type":"task_queued","payload":{"task_id":"...","target":"local-dispatcher"}}
```

目标收到：

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

### 接受、流式输出与返回

目标确认：

```json
{"type":"accept","correlation_id":"task-id"}
```

可选流式片段：

```json
{"type":"chunk","payload":{"text":"partial"},"correlation_id":"task-id"}
```

来源在线时收到 `task_chunk`。片段不进入断线队列。可在流中发送 `<</return>>` 提前结束；标记后的文本被丢弃。

最终返回：

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

`result` 或 `output` 会作为 SQLite 任务结果保存。返回必须属于当前 worker 且不能与它的活动任务冲突；终态任务的重复返回会被拒绝；完成、超时和断线使用原子条件更新，只有一个终态能生效。文本字段会清理 NUL/终端控制数据，序列化后的结果必须小于 `max_body_bytes`。

来源最终收到 `task_result`。来源断线时结果进入进程内有界队列；重启后队列丢失，但 SQLite 仍保留任务状态和结果。服务启动时会把上一进程遗留的非终态任务标记为 `ABANDONED`。

### 超时、断线与取消

超时后服务端：

1. 把任务标记为 `TIMEOUT`；
2. 向来源发送/排队 `TIMEOUT` error；
3. 向目标发送 `cancel`，payload 含 task id 和 reason。

内置 worker 会立即终止当前子进程组。目标断线会把其 `DISPATCHED/EXECUTING` 任务标记为 `ERROR` 并通知来源。调用方断线不会废弃已经发出的任务。

### 广播

```json
{"type":"broadcast","payload":{"data":{"event":"refresh"}}}
```

服务端向当前所有在线连接广播，并给发送者返回 `broadcast_ack` 的 sent/targets。广播不持久化，也不离线补发。

## 限制与常见错误

默认 HTTP/WS body 上限 1048576 字节，可配置但最大 16 MiB。帧和连接尝试还有独立速率限制。任务 plan、转发选项、persona、注册元数据和断线队列均有额外边界。

常见 code：

- `UNAUTHORIZED`、`AUTH_REQUIRED`
- `INVALID_REQUEST`、`INVALID_NAME`、`INVALID_CORRELATION_ID`
- `DUPLICATE_TASK_ID`、`TASK_NOT_OWNED`、`INVALID_TASK_STATE`、`AGENT_BUSY`
- `ROUTING_NOT_FOUND`、`NAME_CONFLICT`、`NOT_REGISTERED`、`ALREADY_REGISTERED`
- `TIMEOUT`、`TARGET_DISCONNECTED`、`PAYLOAD_TOO_LARGE`
- `UNSUPPORTED_PROTOCOL`、`UNKNOWN_MESSAGE_TYPE`
