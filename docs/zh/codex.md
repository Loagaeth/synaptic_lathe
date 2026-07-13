# Codex Worker

## 适配结论

Codex 不只有 CLI。官方面向集成的形态主要有：

- `codex exec`：非交互 CLI，适合脚本、CI 和本地自动化。
- Codex SDK：TypeScript / Python SDK，用于在应用里以代码控制 Codex。
- Codex app-server：本地 JSON-RPC 2.0 服务，面向富客户端，包含会话、审批、事件流等能力。
- Codex MCP server：通过 MCP 暴露 `codex` / `codex-reply` 工具，适合接入 MCP 客户端或 Agents SDK。
- GitHub Action：适合 CI 中运行 Codex 并产出 patch。

当前项目先实现 `codex exec` 适配，因为它和 SynapticLathe 的权限边界最清楚：服务端只负责路由，Codex CLI 在执行机器上以独立 worker 进程运行，文件读写权限由 worker 的系统用户、`--workdir` 和 Codex sandbox 控制。

## 安装

服务端按常规方式启动。需要执行 Codex 的机器单独安装 worker 和 Codex CLI。

```bash
git clone https://github.com/Loagaeth/synaptic_lathe.git
cd synaptic_lathe
pip install -e .
```

按官方 Codex Quickstart 安装 Codex CLI，并确认命令可用：

```bash
codex --version
codex login
```

`codex login` 会在本机保存 Codex 凭据。不要把 `~/.codex/auth.json`、项目内 `.codex/`、API key 或 access token 提交到仓库。

## 启动 Worker

只读模式，适合代码审查、总结、分析：

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-codex-worker \
  --url ws://127.0.0.1:9112/ws \
  --name codex-local \
  --workdir /path/to/repo \
  --sandbox read-only
```

允许 Codex 修改工作区：

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-codex-worker \
  --url ws://127.0.0.1:9112/ws \
  --name codex-writer \
  --workdir /path/to/repo \
  --sandbox workspace-write
```

`danger-full-access` 只建议在隔离容器或一次性 CI runner 中使用。

## 参数

| 参数 | 环境变量 | 说明 |
|------|----------|------|
| `--url` | `SYNAPTIC_WS_URL` 或 `SYNAPTIC_URL` | SynapticLathe WebSocket 地址 |
| `--name` | `SYNAPTIC_AGENT_NAME` | 注册到服务端的 agent 名称 |
| `--key` | `SYNAPTIC_API_KEY` | 服务端 `server.worker_api_key` |
| `--codex-bin` | `SYNAPTIC_CODEX_BIN` | Codex 可执行文件，默认 `codex` |
| `--workdir` | `SYNAPTIC_CODEX_WORKDIR` 或 `SYNAPTIC_WORKDIR` | Codex 工作目录，必填 |
| `--add-dir` | - | 额外授权目录，可重复 |
| `--timeout` | `SYNAPTIC_CODEX_TIMEOUT` 或 `SYNAPTIC_TIMEOUT` | 任务超时秒数，默认 1800 |
| `--lock-file` | `SYNAPTIC_CODEX_LOCK_FILE` 或 `SYNAPTIC_WORKER_LOCK_FILE` | 单实例锁文件；默认由 URL 和 agent 名生成 |
| `--allow-duplicate` | `SYNAPTIC_ALLOW_DUPLICATE_WORKER=1` | 关闭本地单实例保护 |
| `--no-reconnect` | `SYNAPTIC_RECONNECT=0` | 断线后退出，不自动重连 |
| `--reconnect-initial-delay` | `SYNAPTIC_RECONNECT_INITIAL_DELAY` | 初始重连等待秒数，默认 1 |
| `--reconnect-max-delay` | `SYNAPTIC_RECONNECT_MAX_DELAY` | 最大重连等待秒数，默认 30 |
| `--lock-retry-interval` | `SYNAPTIC_LOCK_RETRY_INTERVAL` | 重复 worker 等待锁的重试间隔，默认 60 |
| `--sandbox` | `SYNAPTIC_CODEX_SANDBOX` | `read-only` / `workspace-write` / `danger-full-access` |
| `--approval-policy` | `SYNAPTIC_CODEX_APPROVAL_POLICY` | `untrusted` / `on-request` / `never`，默认 `never` |
| `--model` | `SYNAPTIC_CODEX_MODEL` | 可选模型覆盖 |
| `--profile` | `SYNAPTIC_CODEX_PROFILE` | 可选 Codex profile |
| `--config` | - | Codex `key=value` 配置覆盖，可重复 |
| `--pass-env` | `SYNAPTIC_CODEX_PASS_ENV` | 显式传给 `codex exec` 的环境变量名，可重复；环境变量使用逗号分隔 |
| `--max-output-bytes` | `SYNAPTIC_CODEX_MAX_OUTPUT_BYTES` | stdout 捕获上限，默认 1000000 |
| `--max-stderr-bytes` | `SYNAPTIC_CODEX_MAX_STDERR_BYTES` | stderr 尾部保留上限，默认 64000 |
| `--skip-git-repo-check` | `SYNAPTIC_CODEX_SKIP_GIT_REPO_CHECK` | 透传 Codex 的 Git 仓库检查跳过开关 |
| `--no-ephemeral` | `SYNAPTIC_CODEX_EPHEMERAL=0` | 不传 `--ephemeral` |

## 连接与重复实例

worker 会保持 WebSocket 长连接。连接失败或运行中断线时，默认在本进程内指数退避重连。服务器仍然只接受 worker 主动连接，不主动拨号到内网机器。

接收控制帧和执行 Codex 并发进行；长任务期间仍会回复心跳。服务端 timeout、WebSocket 断开或 worker 关闭会终止 Codex 子进程组，避免后台残留。

同一台机器上，同一 `agent_name + url` 默认只有一个活跃 Codex worker 能拿到锁。重复实例拿不到锁时会打印一次错误并低频等待，不退出触发 systemd/Docker restart storm；锁释放后会继续启动连接循环。

## 调用

Worker 在线后，向注册名发送任务即可：

```json
{"type":"send","payload":{"target":"codex-local","plan":"review this repository and list the top 5 risks","timeout":900}}
```

Codex 的最终回答会作为 `task_result.payload.result` 返回。`codex exec` 的 stderr 主要是进度信息；失败时 worker 会把尾部 stderr 放入错误 payload 方便排查。

## 安全建议

- 服务端和 Codex worker 分开部署；服务端不需要 Codex 本地文件权限。
- 一个权限边界启动一个 worker，例如只读审查 worker 和可写修复 worker 分开。
- 默认使用 `--sandbox read-only`。
- 需要写文件时使用 `workspace-write`，并把 `--workdir` 限制到目标仓库。
- 不要把 OpenAI API key、Codex access token、`~/.codex/auth.json` 或 `.codex/` 提交到 git。
- Codex 子进程默认只继承白名单环境变量，不会继承 `SYNAPTIC_API_KEY` 或其他 `SYNAPTIC_*` 配置。
- worker 关闭 Codex stdin，并在远程 plan 前插入 `--`；调用方不能通过 plan 注入新的 Codex CLI 选项。
- HTTP proxy 变量也不默认继承；确需代理时用 `--pass-env HTTPS_PROXY` / `--pass-env NO_PROXY` 显式放行。
- 如果用 `CODEX_API_KEY`，只在可信执行环境中通过 `--pass-env CODEX_API_KEY` 显式传入；不要在会运行不可信仓库脚本的环境里长期暴露。
- 大输出会被 worker 截断并在返回 payload 中标记 `output_truncated` / `stderr_truncated`。

## 官方参考

- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [Codex SDK](https://developers.openai.com/codex/sdk)
- [Codex app-server](https://developers.openai.com/codex/app-server)
- [Codex MCP / Agents SDK](https://developers.openai.com/codex/guides/agents-sdk)
- [Codex authentication](https://developers.openai.com/codex/auth)
