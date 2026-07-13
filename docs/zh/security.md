# 安全边界

## 密钥分工

| 配置 | 用途 | 权限 |
|------|------|------|
| `server.api_key` | Web 管理页、REST 管理/查询接口、连接提示词 | 管理员 |
| `server.worker_api_key` | WebSocket Agent/Worker 注册与互调 | 仅 Worker |
| `memory.embedding_api_key` | 服务端调用 embedding provider | 仅上游 provider |

`worker_api_key` 不能访问 REST 管理端点；管理员 key 可以注册 WebSocket，便于兼容和紧急运维。旧配置未填写 `worker_api_key` 时会回退到 `api_key`，新部署应使用两个不同的随机值。不要把任何真实 key、`config.yaml`、`profiles.yaml`、`.env` 或 CLI 认证目录提交到仓库。

## 认证矩阵

- `/health`、`/version`：公开，只返回最小状态和协议元数据。
- `GET /context`、`/context/agents|skills|personas|prompts`：默认需要管理员 key；`public_read_context: true` 时公开。两个 `POST` 搜索始终需要管理员 key，避免匿名触发 embedding 费用。
- `/admin/*`、`/connection-prompt`、日志和调试接口：始终使用管理员 key。
- `/ws`、`/api/v1/ws`：使用 worker key，管理员 key 也可作为上级凭据。
- Web 管理页可以公开加载静态文件，但未认证时只显示 key 输入面板，后端权限不会放宽。

空 key 只允许显式监听 `127.0.0.1`、`localhost` 或 `::1`。非本地监听时缺少对应 key 的请求会被拒绝。公网部署应把后端保持在回环地址，通过 HTTPS/WSS 反向代理暴露。

`public_read_context: true` 会通过上述 GET 端点公开记忆、知识、技能、人设、提示词文档和 Agent 状态。提示词可能包含内部策略，因此公网通常保持 `false`。

## WebSocket

- 注册前只接受 `hello`、`register`、`pong`；同名在线 Agent 的第二个连接会被拒绝。
- 连接尝试、帧速率和消息体都有上限；相关参数位于 `server.*rate_limit*` 和 `max_body_bytes`。
- 内置 worker 在执行子进程时仍持续接收 `ping` 和 `cancel`。超时、断线或 worker 关闭会终止当前子进程及其进程组。
- 调用方断线不会取消已经发出的任务；返回结果会在当前服务进程内排队，使用相同 `agent_name` 重连后补发。该队列不跨服务重启。
- 目标 worker 断线时，它的活动任务会变为 `ERROR`，调用方收到 `TARGET_DISCONNECTED`。
- 一个 worker 顺序执行任务；本地待执行队列有界，过载时连接会重建，由服务端明确失败相关任务。

## 文件与数据库

API 的文件导入只允许读取项目 `data/` 下的 UTF-8 普通文件，限制为 500000 字节，并拒绝根目录或任意路径组件中的符号链接。数据库、WAL/SHM、配置、运行状态、日志和 worker 环境文件会尽量设置为仅所有者可读写。配置模型拒绝未知字段，拼错字段会让启动失败而不是静默使用默认值。运行目录仍应由专用系统用户持有。

## 出站 HTTP

Agent HTTP adapter 和 embedding 客户端默认不继承 `HTTP_PROXY`、`HTTPS_PROXY` 等环境变量。确需代理时显式设置 `server.outbound_trust_env` 或 `memory.embedding_trust_env`。Base URL 只接受 HTTP(S)，拒绝 URL 内凭据、query 和 fragment。管理员配置的 URL 仍拥有访问服务端内网的能力；不要把 URL 配置权交给不可信调用方，并用网络出口策略阻断云元数据等敏感地址。

## 子进程执行

服务端不执行 shell。本地能力只由独立 worker 提供，权限取决于运行用户、工作目录、Codex sandbox 和本地 profile allowlist。

- 固定命令和 profile 参数通过 argv 传递，不经过 shell。固定命令默认用 `--` 隔离 plan；profile 的显式位置参数应自行加入 `--`。
- 子进程 stdin 默认关闭；会等待 TTY、登录或人工审批的命令会超时。
- 默认环境白名单不包含 `SYNAPTIC_*`、API key、动态加载器变量或代理变量。确有需要时用 `--pass-env` / `pass_env` 逐项放行。
- `--max-output-bytes` 限制捕获和回传；NUL、ANSI 和控制字符会在协议边界清理。
- 不要把通用 shell 作为公网可调用 profile。优先使用只读或功能单一的 wrapper。

## 日志

HTTP 日志记录 method、path、status、duration 和 client IP，不记录 query、请求体或 header。任务日志记录 Agent、task id、profile、状态和截断标记，不记录 plan 或输出正文。日志写入前会对常见 Bearer、`sk-*`、JSON/赋值形式密钥和 URL 凭据做脱敏，但脱敏不是秘密管理替代品；日志接口仍只对管理员开放。
