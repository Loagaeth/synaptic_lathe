# SynapticLathe (突触凝练机) — Agent 消息总线

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-AGPLv3-red.svg)](LICENSE)
![Status](https://img.shields.io/badge/Status-Alpha-orange.svg)

**中文文档**: [docs/zh/README.md](docs/zh/README.md)  
**English docs**: [docs/en/README.md](docs/en/README.md)

SynapticLathe 是一个 Agent 消息总线，让不同 AI Agent 框架互相调用，并共享记忆、技能、知识、人设和提示词文档。

服务端负责鉴权、路由、任务状态、上下文存储和 Web 管理。本地命令执行由独立的子进程 Worker 负责，服务器本体不直接执行本地命令。worker 内置断线重连和本地单实例锁，并在长任务期间持续处理 WebSocket 心跳与取消；服务端区分调用方断线和目标 worker 断线，避免误废弃仍可返回的任务。

## 功能

- 共享上下文层：记忆、技能、知识库、人设和提示词文档集中管理
- REST API v1 + 旧路径兼容：`/api/v1/*` 作为稳定集成入口，旧 `/context`、`/admin` 等路径继续可用
- Web 管理界面：日/夜主题、上下文 CRUD、人工 Agent 任务、竞拍/团队审批、调用统计、连通性探测和实时结构化日志
- WebSocket Agent 互调协议：注册、任务路由、流式输出、返回结果、广播
- 可选语义搜索：本地 `sentence-transformers` 或 OpenAI 兼容 / Gemini / NVIDIA / Ollama
- 人设隔离：共享模式或按 persona 分区
- 独立本地子进程 Worker：按权限边界独立启动
- Profile Dispatcher Worker：本地 allowlist 映射 Claude/Codex/Hermes/Reasonix 等固定命令
- 独立 Codex Worker：通过本地 `codex exec` 接入 Codex CLI
- 自动生成连接提示词：system prompt 保持稳定，运行时通过 `/context/agents` 和 `/context/prompts` 拉取最新状态

## 架构概览

```text
LLM / Bot / Web Client
        │ HTTP / WebSocket + Bearer token
        ▼
SynapticLathe Server
  ├─ FastAPI REST: /api/v1/* stable paths + legacy /context, /admin, /connection-prompt, /install
  ├─ WebSocket Router: /ws + /api/v1/ws, register/send/stream/cancel, broadcast, probe/probe_ack
  ├─ Task Management: direct tasks, auction bids, human-gated team plans, cancellation reasons, SSE events
  ├─ Task Store: SQLite task/group state + results + per-Agent invocation counters; reconnect queue is process-local
  ├─ Context Store: memory, knowledge, skills, personas, prompt documents
  ├─ Web Admin: context CRUD, Agent/Profile selection, live output, task groups, probe, stats, live logs
  └─ Structured Logs: HTTP method/path/status/duration, agent/task lifecycle
        │
        ├─ http_api agent adapter: call configured remote HTTP agents, such as AstrBot
        ├─ synaptic-subprocess-worker: expose one fixed local command per worker
        ├─ synaptic-profile-worker: expose local Claude/Codex/Hermes/Reasonix profiles
        └─ synaptic-codex-worker: dedicated Codex CLI worker using codex exec
```

服务端不执行 shell，也不持有本地工具的文件权限。所有本地执行能力都在独立 worker 进程内，由运行该 worker 的系统用户、工作目录、sandbox 参数和本地 profile allowlist 决定。

当前 profile dispatcher 已支持 `claude`、`codex`、`hermes`、`reasonix` 示例配置。Hermes 不是待规划项，默认通过 `hermes --oneshot "{plan}"` 接入；这里的 `plan` 是 WebSocket payload 中的任务文本字段，不表示功能仍处于计划阶段。

## 快速开始

```bash
git clone https://github.com/Loagaeth/synaptic_lathe.git
cd synaptic_lathe
python -m synapse.setup_wizard --yes
./synapticctl start
```

`setup` 会创建 `.venv`、安装当前项目和依赖、生成 `config.yaml`、创建 `data/`/`logs/`，并写入快捷控制脚本 `synapticctl`。后续常用命令：

```bash
./synapticctl status
./synapticctl logs
./synapticctl restart
./synapticctl stop
```

Windows 使用同目录生成的 `synapticctl.cmd start|stop|status|logs`，它会调用 setup 选定的 Python。

服务默认启动在 `http://127.0.0.1:9112`。setup 默认分别生成管理员 `server.api_key` 和低权限 WebSocket `server.worker_api_key`，并只在终端打印一次；浏览器使用管理员 key，worker 使用 worker key。需要前台调试时可运行 `./synapticctl foreground`，也可手动运行 `synaptic-server config.yaml` 或 `python main.py config.yaml`。

外网部署时必须设置管理员 key 和 worker key，并放在 HTTPS/WSS 反向代理后面；管理员 key 可作为 worker 的上级凭据，但反向不成立。当前一个 `worker_api_key` 代表同一消息总线信任域：持有者可以注册任意未占用 Agent 名称、向其他 Agent 发任务和广播，但不能访问 REST 管理端点。互不信任的 worker 应拆分部署或使用网络隔离。服务端 HTTP/embedding 默认不继承环境代理；确需代理时显式启用对应 `*_trust_env`。完整部署说明见 [docs/zh/deployment.md](docs/zh/deployment.md)。

## Web 管理界面

Web 管理页位于 `/web/index.html`，根路径 `/` 和 `/admin` 会自动跳转过去。当前页面提供：

- 日间/夜间模式切换，主题只保存在本地浏览器。
- 记忆、技能、知识、人设、提示词文档的查看、添加、删除和复制。
- 记忆/知识搜索，支持 persona 和 limit。
- 从 `data/` 下的文件写入内容，知识写入可选择 Markdown 分块。
- 健康检查、实时结构化日志、配置查看/部分 memory 配置写回、安装指南、动态 Agent 列表和连接提示词生成。
- 人工发布指定任务，查看实时片段/最终结果，填写理由中断任务，并按 Agent/Profile 查看 30 天调用次数。
- 广播式无 LLM 连通性探测；只读 Profile 可选择生成自述能力标签。自述标签只用于展示和人工判断，不参与授权。
- 竞拍模式会为每个候选建立独立只读提案任务；人工选标时必须另行确认执行 Agent/Profile，提案端点不会被隐式当成执行端点。团队模式先生成只读分工，人工重新指定端点并批准后才执行。

直接打开 `/web/index.html` 且未认证时会显示 API key 输入面板，不会放宽后端权限。浏览器端只保存本页会话 API key；服务端密钥、embedding key、profile session id 仍应只放在服务器或本地 worker 配置中。页面脚本和样式只允许同源静态资源，CSP 不依赖 `unsafe-inline`。

## 稳定边界

当前仍是 Alpha，但已开始固定外部集成边界：

- REST v1 前缀：`/api/v1/*`。旧路径继续兼容，但新客户端建议绑定 v1。
- WebSocket v1：`/ws` 和 `/api/v1/ws` 等价；注册 payload 支持 `protocol_version`、`client` 和 `capabilities`。
- 协议发现：`GET /version` 或 `GET /api/v1/version` 返回服务端版本、API 版本、WS 协议版本和能力列表。
- 旧 worker 仍可只发送 `agent_name` 注册；新 worker 会声明协议版本和本地能力。
- `chunk` 会实时转发为 `task_chunk`，最终仍以 `task_result` 为准；实时片段不写入离线队列。
- Web 人工任务使用持久化 `source_kind=web`，浏览器通过带管理员认证的 HTTP/SSE 读取，不伪装成 WebSocket Agent，也不会把结果排入名为 `web-console` 的离线队列。
- 手工连通性检查使用 `probe/probe_ack`，不调用 LLM；竞拍与团队规划只允许选择声明 `advisory_safe: true` 的 Profile。该声明用于选择，本地命令/sandbox 才是实际权限边界。
- 内置 worker 在子进程运行期间并发接收心跳和 `cancel`；timeout、断线或退出会终止整个子进程组。完成、超时和断线通过 SQLite 条件更新竞争终态，不会相互覆盖；任务组的派生状态也使用 compare-and-set 持久化，不会回滚并发中的人工选标、批准或取消。
- 离线结果队列只在当前服务进程内有效，服务重启不会恢复；SQLite 会保留任务状态和最终结果。重连队列中的任务保持 `QUEUED`，实际补发后才进入 `DISPATCHED`；上一进程遗留的非终态任务会在启动时标记为 `ABANDONED`。
- 配置模型拒绝未知字段；拼错配置名会在启动时直接报错，不会静默回退到不安全或无效的默认值。
- 自动任务记忆默认关闭（`auto_memory_threshold: 0`）；启用后只沉淀 `purpose=execute` 的实际执行任务，并按 memory scope、长度上限和基础脱敏规则写入。竞拍、规划和自评不会进入长期记忆。
- Web/Agent 终态任务默认保留 168 小时（`task_history_hours`）；调用次数按日单独聚合，任务清理后统计仍保留。过期且无子任务的任务组同步清理。

## 动态 Agent 和提示词文档

连接提示词不再要求每次新增 worker 或规则后手动改 system prompt。推荐把 `/connection-prompt` 生成的稳定说明贴入 Agent，然后让 Agent 在执行前按需读取。完整的 plan 结构、动态发现、截断处理与信任边界见 [中文提示词指南](docs/zh/prompts.md) / [English prompt guide](docs/en/prompts.md)：

- `GET /context/agents`：返回配置内 Agent、当前 WebSocket 在线 worker、合并后的 `available` 列表，以及 worker 声明的 profile 能力元数据。
- `GET /context/prompts?name=xxx`：读取可复用提示词文档。
- `POST /admin/prompt` / `DELETE /admin/prompt?name=xxx`：通过 API 或 Web 管理页维护提示词文档。
- `POST /connection-prompt`：用 JSON 重新生成连接提示词，适合 Web/脚本调用。

任务文本、上下文、提示词文档、广播和 Agent 输出都按不可信数据处理，不能扩大认证、Profile allowlist、sandbox 或人工审批范围。提示词文档用于复用规则，不用于保存 key、原始 session id 或临时大文本。

`server.public_read_context: true` 会公开 `GET /context`、`/context/agents`、`/context/skills`、`/context/personas`、`/context/prompts`；内容包括记忆、知识、技能、人设、提示词和 Agent 状态。两个 `POST` 语义搜索仍要求管理员 key，避免匿名请求触发付费 embedding；公网部署通常保持 `false`。Web 的 Agents 页会展示这些能力声明、公开标签、只读 advisory 标记和自述标签，并提供可复制的调用 payload 与广播连通性探测。

## 本地子进程 Worker

需要把任务分发到某台机器的本地子进程时，在那台机器上单独启动 worker。推荐先用 worker setup 生成本地运行目录和快捷脚本：

```bash
SYNAPTIC_API_KEY='<worker-api-key>' python -m synapse.worker_setup \
  --kind subprocess \
  --url wss://<public-host>/ws \
  --name local-python \
  --command python \
  --workdir /path/to/project \
  --yes
./workerctl start
./workerctl logs
```

`workerctl` 只负责后台拉起进程；worker 的字符画由服务端在 WebSocket 注册成功后返回，worker 会先过滤终端控制字符再打印。看到 `SynapticLathe ws connected` 就表示已经连上并完成注册。也可以手动启动：

```bash
pip install -e .
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-subprocess-worker \
  --url ws://127.0.0.1:9112/ws \
  --name local-python \
  --command python \
  --workdir /path/to/project
```

推荐一个权限边界启动一个 worker：独立系统用户、限制 `--workdir`，必要时用 wrapper 命令代替通用 shell。固定命令 worker 默认在任务参数前插入 `--`，防止以连字符开头的 plan 被当作 CLI 选项；仅当目标程序不支持该约定时才显式使用 `--allow-plan-options`。子进程默认不会继承 `SYNAPTIC_*`、动态加载器或 HTTP proxy 变量；确需模块路径或代理时用 `--pass-env` 逐项放行。stdout/stderr 按原始字节计入 `--max-output-bytes`；NUL 与终端控制序列在回传前会被清理，避免 `\u0000` 放大。完整说明见 [docs/zh/subprocess-worker.md](docs/zh/subprocess-worker.md) / [docs/en/subprocess-worker.md](docs/en/subprocess-worker.md)。

## Profile Dispatcher Worker

如果希望一个本地 worker 同时暴露 `claude`、`codex`、`hermes`、`reasonix` 等受控能力，使用 profile dispatcher。云端 bot 只传 `profile/tool`、任务文本 `plan` 和可选 `session_id`，真实命令、工作目录、环境变量和 session 映射都保存在本地配置中。

完整示例见 `profiles.example.yaml`。默认示例包含：

| profile | 本地命令入口 | 说明 |
|---------|--------------|------|
| `claude` | `claude -p "{plan}" --permission-mode plan` | 默认只分析；写入权限需在本地 profile 明确放行 |
| `codex` | `codex exec ... -- "{plan}"` | 默认 `read-only` sandbox + `approval_policy='never'` |
| `hermes` | `hermes --oneshot "{plan}"` | 已接入；stdin 关闭，需在本地预先配置非交互权限 |
| `reasonix` | `reasonix run ... "{plan}"` | 推荐先走非交互 CLI；Dashboard `/api/*` 属内部接口 |

也可以用 setup 直接生成默认 `profiles.yaml`、`.synaptic/worker.env` 和 `workerctl`：

```bash
SYNAPTIC_API_KEY='<worker-api-key>' python -m synapse.worker_setup \
  --kind profile \
  --url wss://<public-host>/ws \
  --name local-dispatcher \
  --workdir /path/to/workspace \
  --yes
./workerctl start
./workerctl logs
```

Profile worker 会关闭子进程 stdin，不支持人工审批输入。需要执行工具调用的 CLI 必须在本地 profile 中显式配置非交互审批策略；未知 CLI 至少设置较短 `timeout`，避免任务卡死。

Reasonix 默认从本机 `~/.reasonix/config.json` 读取认证；先用 `reasonix setup` 或 `reasonix chat` 保存 key。不要把真实 key 写进 `profiles.yaml`。如果明确改用环境变量认证，再在本地 profile 中添加 `pass_env: [DEEPSEEK_API_KEY]`。调用方显式传入的 `timeout` 会覆盖 profile 默认值；Reasonix 默认配置可能初始化 MCP，未确认本地轻量配置前建议传 `timeout:1800`，不要沿用短任务示例里的 `60` 秒。

```yaml
# profiles.yaml；真实命令路径和 session 映射只保存在本机
default_profile: codex
profiles:
  claude:
    command: ["claude", "-p", "{plan}", "--max-turns", "10", "--permission-mode", "plan"]
    workdir: .
    timeout: 600
    max_output_bytes: 200000
    advisory_safe: true
    tags: [analysis, review, planning]
```

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-profile-worker \
  --url ws://127.0.0.1:9112/ws \
  --name local-dispatcher \
  --profiles ./profiles.yaml
```

Reasonix 0.x 有本地 Dashboard API 和 `reasonix acp`，但 Dashboard `/api/*` 是内部会话接口，不建议作为稳定 HTTP API 依赖。需要 HTTP 化时，优先在本项目外层包一层受控 wrapper。

## Codex Worker

需要把任务交给本机 Codex CLI 时，单独启动 Codex worker。服务端不直接持有 Codex 的本地文件权限。

```bash
pip install -e .
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-codex-worker \
  --url ws://127.0.0.1:9112/ws \
  --name codex-local \
  --workdir /path/to/repo \
  --sandbox read-only
```

默认 `read-only`；需要允许 Codex 修改工作区时显式使用 `--sandbox workspace-write`。完整说明见 [docs/zh/codex.md](docs/zh/codex.md) / [docs/en/codex.md](docs/en/codex.md)。

## 文档

| 主题 | 中文 | English |
|------|------|---------|
| 文档首页 | [docs/zh/README.md](docs/zh/README.md) | [docs/en/README.md](docs/en/README.md) |
| 快速开始 | [docs/zh/quickstart.md](docs/zh/quickstart.md) | [docs/en/quickstart.md](docs/en/quickstart.md) |
| 服务端部署 | [docs/zh/deployment.md](docs/zh/deployment.md) | [docs/en/deployment.md](docs/en/deployment.md) |
| 本地子进程 / Profile Worker | [docs/zh/subprocess-worker.md](docs/zh/subprocess-worker.md) | [docs/en/subprocess-worker.md](docs/en/subprocess-worker.md) |
| Codex Worker | [docs/zh/codex.md](docs/zh/codex.md) | [docs/en/codex.md](docs/en/codex.md) |
| 安全边界 | [docs/zh/security.md](docs/zh/security.md) | [docs/en/security.md](docs/en/security.md) |
| API 与 WebSocket | [docs/zh/api.md](docs/zh/api.md) | [docs/en/api.md](docs/en/api.md) |
| 提示词与动态 Agent 发现 | [docs/zh/prompts.md](docs/zh/prompts.md) | [docs/en/prompts.md](docs/en/prompts.md) |
| 排查指南 | [docs/zh/troubleshooting.md](docs/zh/troubleshooting.md) | [docs/en/troubleshooting.md](docs/en/troubleshooting.md) |

## Embedding 可选依赖

远程 OpenAI-compatible、NVIDIA 和 Ollama 使用基础依赖。本地模型与 Gemini SDK 分别安装：

```bash
pip install -e ".[embedding]"
pip install -e ".[gemini]"
```

Embedding 不可用时语义搜索会降级为关键词匹配。

## 开发检查

```bash
pip install -r requirements-dev.txt
ruff check .
ruff format . --check
pytest -q
bandit -q -r synapse
pip-audit -r requirements.txt
```

## 项目结构

```text
synaptic_lathe/
├── main.py
├── config.example.yaml
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml
├── MANIFEST.in
├── docs/
│   ├── zh/
│   └── en/
├── synapse/
│   ├── agents/
│   ├── context/
│   ├── web/                 # index.html + styles.css + app.js
│   ├── agent_catalog.py     # Agent discovery and dynamic prompts
│   ├── task_api.py          # authenticated Web task routes
│   ├── task_management.py   # durable groups, stats, and tags
│   ├── task_events.py       # bounded SSE events and probe coordination
│   ├── web_task_controller.py
│   └── server.py            # app lifecycle, legacy APIs, WS session
└── tests/
```

## License

GNU Affero General Public License v3.0
