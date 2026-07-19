# 本地 Subprocess / Profile Worker

## 权限边界

Worker 是主动连接服务端 WebSocket 的独立客户端。服务器不需要访问执行机器的 IP，也不会反向拨号；因此 NAT、不同网段或没有公网入站地址都不影响，只要执行机器能访问服务端 WSS。

本地命令拥有启动 worker 的系统用户权限。一个权限边界使用一个 worker，优先选择专用用户、受限 workdir、只读 sandbox 和固定 wrapper，不要暴露通用 shell。

## 安装

同一源码仓库开发：

```bash
git clone https://github.com/Loagaeth/synaptic_lathe.git
cd synaptic_lathe
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

只在执行机器安装当前 Git 版本：

```bash
python -m venv .venv
. .venv/bin/activate
pip install "git+https://github.com/Loagaeth/synaptic_lathe.git"
```

`python -m synapse.agents.*` 需要当前环境已安装项目，或从源码根目录运行。`--workdir` 只控制任务子进程，不会修复 worker 自身的 `PYTHONPATH`。
若已经用 Git URL 安装到当前虚拟环境、当前目录不是源码仓库，运行 worker setup 时加 `--skip-install`；否则 setup 会要求 `--project-dir` 下存在 `pyproject.toml` 或 `requirements.txt`，以免生成无法启动的空虚拟环境。

## 推荐 setup

Profile Dispatcher 同时暴露多个本地 allowlist 工具：

```bash
SYNAPTIC_API_KEY='<worker-api-key>' python -m synapse.worker_setup \
  --kind profile \
  --url wss://synapse.example.com/ws \
  --name local-dispatcher \
  --workdir /srv/workspace \
  --yes
./workerctl start
./workerctl logs
```

固定命令 worker：

```bash
SYNAPTIC_API_KEY='<worker-api-key>' python -m synapse.worker_setup \
  --kind subprocess \
  --url wss://synapse.example.com/ws \
  --name safe-runner \
  --command /usr/local/bin/safe-runner \
  --workdir /srv/tasks \
  --timeout 300 \
  --max-output-bytes 200000 \
  --yes
./workerctl start
```

setup 创建 `.venv`、owner-only `.synaptic/worker.env`、`logs/`、可选 `profiles.yaml` 和 `workerctl`。已有 key 默认保留；`--clear-api-key` 才清空。字符画来自注册成功响应，日志出现 `SynapticLathe ws connected` 表示 WS 已建立并注册。

使用源码目录配合 `--skip-install` 时，生成的 `workerctl` 只在检测到 `synapse/__init__.py` 后把该明确的源码根加入自身 `sys.path` 和子进程 `PYTHONPATH`；普通非源码目录不会被注入。使用 Git URL 安装时应从安装该包的同一 Python 环境运行 setup。

## 手动启动固定命令

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-subprocess-worker \
  --url ws://127.0.0.1:9112/ws \
  --name local-python \
  --command python \
  --workdir /path/to/project \
  --timeout 300 \
  --max-output-bytes 200000
```

固定命令 worker 默认以 `command ... -- "<plan>"` 形式传参，不做 shell 拼接，避免 `--help` 一类 plan 被解释为程序选项。只有目标程序明确不支持 `--` 时才使用 `--allow-plan-options` 关闭该保护。需要 stdin 时，调用 payload 可传受限 `stdin` 字段。不要把 `--command bash`、`sh`、PowerShell 或 `cmd.exe` 暴露给不可信调用方。

## Profile Dispatcher

公共示例位于 `profiles.example.yaml`，真实配置复制到本机 `profiles.yaml`：

```yaml
default_profile: codex
timeout: 600
max_output_bytes: 200000

profiles:
  claude:
    command: ["claude", "-p", "{plan}", "--max-turns", "10", "--permission-mode", "plan"]
    workdir: .
    timeout: 600
    advisory_safe: true
    tags: [analysis, review, planning]

  codex:
    command:
      - codex
      - exec
      - --ephemeral
      - --sandbox
      - read-only
      - --config
      - "approval_policy='never'"
      - --skip-git-repo-check
      - --
      - "{plan}"
    workdir: .
    timeout: 1800
    advisory_safe: true
    tags: [code, review, planning]

  hermes:
    command: ["hermes", "--oneshot", "{plan}"]
    workdir: .
    timeout: 600
    advisory_safe: false
    tags: [general]

  reasonix:
    command: ["reasonix", "run", "--effort", "low", "--budget", "0.10", "{plan}"]
    workdir: .
    timeout: 1800
    advisory_safe: false
    tags: [reasoning]
```

启动：

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-profile-worker \
  --url ws://127.0.0.1:9112/ws \
  --name local-dispatcher \
  --profiles ./profiles.yaml
```

调用方只传 profile、plan、可选 session alias 和 timeout：

```json
{"type":"send","payload":{"target":"local-dispatcher","profile":"codex","plan":"review this repo","timeout":1800}}
```

占位符只支持简单 `{plan}`、`{profile}`、`{tool}`、`{session_id}`、`{session_alias}`、`{source}`；禁止属性访问、索引、格式说明和转换。命令通过 argv 执行，不经过 shell。未使用 `{plan}` 时 worker 自动追加 `--` 和 plan；显式使用 `{plan}` 时，本地 profile 应在位置参数前自行放置 `--`。

`sessions` 保存 alias 到真实 session id 的本地映射。`{session_id}` 和 `{session_alias}` 都会先经过该 allowlist；未使用 session 占位符的 profile 会忽略多余 alias。默认拒绝原始 session id；确需开放时同时设置 `allow_raw_session_id: true` 和无分组/无嵌套量词的字符类 `session_pattern`。`allow_raw_session_id` 与 `advisory_safe` 必须使用 YAML 真布尔值 `true/false`，写成字符串（例如 `"false"`）会被拒绝，避免权限开关歧义。能力上报只包含 alias 名，不包含真实值。

`tags` 是最多 8 个公开短标签。`advisory_safe: true` 只应标在本地已强制只读、无人工审批且适合提案/规划的 Profile 上；Web 竞拍、团队规划和自评会据此筛选。该声明本身不会改变命令权限，错误标记可导致只读提案运行在可写工具中。Hermes/Reasonix 默认示例保持 `false`，直到用户在本机验证其权限行为。

## 审批和交互

Profile worker 关闭子进程 stdin，不能替用户点击批准。先在本机完成登录和初始化，并为每个 CLI 配置其当前版本明确支持的非交互安全模式：

- Claude 示例使用 `--permission-mode plan`，默认只适合分析；需要工具时建立单独、最小权限 profile。
- Codex 示例使用 `read-only` + `approval_policy='never'`；可写任务单独使用受限 `workspace-write` profile。
- Hermes `--oneshot` 和 Reasonix `run` 的审批行为可能随版本/本地配置变化，必须先在同一用户环境实测。
- Reasonix 首次启动可能初始化 MCP；未确认轻量配置前使用 `timeout: 1800`。

任何仍等待 TTY/审批的命令会在 timeout 后被终止并返回 `exit_code=-1`。

## 连接、取消和重复实例

- worker 保持长连接，失败或断线后在当前进程内指数退避重连。
- `websockets` 14-16 自动选择 `additional_headers` 或旧 `extra_headers`。
- 接收控制帧和执行子进程并发进行，因此长任务期间仍能回复 ping、接收 cancel。
- 服务端超时、WS 断开或进程关闭会终止当前子进程组，避免孙进程遗留。
- 同一机器上相同 kind + URL + Agent 名默认使用 owner-only 文件锁。重复实例低频等待，不退出制造 restart storm。
- 本地待执行队列最多 16 个任务；过载时断线重连，让服务端把活动任务明确标记失败。

## 参数

| 参数 | 环境变量 | 说明 |
|------|----------|------|
| `--url` | `SYNAPTIC_WS_URL` / `SYNAPTIC_URL` | 仅 `ws://` 或 `wss://`，禁止 URL 凭据 |
| `--name` | `SYNAPTIC_AGENT_NAME` | 1-64 个字母、数字、`_`、`-` |
| `--key` | `SYNAPTIC_API_KEY` | `server.worker_api_key` |
| `--workdir` | `SYNAPTIC_WORKDIR` | 子进程工作目录 |
| `--timeout` | `SYNAPTIC_TIMEOUT` | 默认任务超时 |
| `--max-output-bytes` | 对应 worker 环境变量 | 捕获/流式上限 |
| `--pass-env NAME` | 对应 `*_PASS_ENV` | 显式传给子进程，可重复 |
| `--no-reconnect` | `SYNAPTIC_RECONNECT=0` | 断线后退出 |
| `--allow-duplicate` | `SYNAPTIC_ALLOW_DUPLICATE_WORKER=1` | 关闭本地单实例保护，不推荐 |

默认 child env 只保留 PATH、HOME、locale、临时目录和证书路径。API key、`SYNAPTIC_*`、动态加载器变量和 HTTP proxy 不继承；代理需要显式 `--pass-env HTTPS_PROXY --pass-env NO_PROXY`。

输出按原始字节限制，返回前清理 NUL、ANSI 和控制字符。看到 `output_truncated=true` 应让调用方请求摘要或分页，不要盲目提高上限。

## systemd 示例

```ini
[Unit]
Description=SynapticLathe profile worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=synaptic-worker
WorkingDirectory=/srv/synaptic-worker
EnvironmentFile=/srv/synaptic-worker/.synaptic/worker.env
ExecStart=/srv/synaptic-worker/.venv/bin/synaptic-profile-worker --url wss://synapse.example.com/ws --name local-dispatcher --profiles /srv/synaptic-worker/profiles.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

如果使用 setup 生成的 `workerctl`，不要再让 systemd 同时启动第二份相同 Agent。
