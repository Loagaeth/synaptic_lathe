# 排查指南

## 端口没有监听

先看监听和进程，不要只看防火墙：

```bash
ss -ltnp | grep ':9112'
ps -ef | grep -E 'synaptic|main.py|uvicorn' | grep -v grep
./synapticctl foreground
```

`ModuleNotFoundError: No module named 'yaml'` 表示启动了没有安装依赖的 Python。运行 `python -m synapse.setup_wizard --yes`，或在正确虚拟环境执行 `pip install -r requirements.txt`。如果进程存在但端口没有监听，它可能只是宿主程序，或服务子进程已经退出；以前台 traceback 为准。

## embedding 401

`POST /admin/memory` 返回 created 只代表数据库写入成功，不代表 embedding 成功。provider 401 时该记录没有向量，搜索会降级为关键词。

检查：

- `memory.embedding_api_key` 是完整 provider key，不是 SynapticLathe key。
- `embedding_api_url` 是 API base URL；OpenAI-compatible 会追加 `/embeddings`。
- 模型名由 provider 支持。
- 实际监听进程读取的是预期 `config.yaml`。

```bash
grep -aE "401 Unauthorized|embedding API call failed|Api key is invalid" logs/synaptic_lathe.log
```

直接 curl provider 成功而服务仍失败时，核对监听 PID 的 cwd、cmdline 和配置文件，而不是旧的 `/tmp` 重定向日志。

## 三种 key

- REST/Web 管理：`Authorization: Bearer <server.api_key>`
- WebSocket worker：`Authorization: Bearer <server.worker_api_key>`
- Embedding provider：`memory.embedding_api_key`

worker key 访问 `/admin/*` 返回 403 是预期行为。旧配置中 worker key 为空时才回退到 admin key。

## Worker 连接不上

- 公网使用 `wss://域名/ws`，同机才使用 `ws://127.0.0.1:9112/ws`。
- `SYNAPTIC_API_KEY` 与 `server.worker_api_key` 一致。
- Agent 名只含字母、数字、`_`、`-`，长度 1-64。
- 同名 Agent 已在线会得到 `NAME_CONFLICT`；本地重复实例通常等待单实例锁。
- 代理已转发 WebSocket Upgrade，证书和系统时间有效。
- `websockets 14-16` 都受支持；代码会在 `additional_headers` 和旧 `extra_headers` 间适配。

worker 默认在当前进程内指数退避重连，不依赖 systemd/Docker 反复拉起。

## 任务超时、审批或 `exit_code=-1`

Profile worker 关闭 stdin。CLI 如果等待登录、TTY 或人工审批，只会在 timeout 后终止。先在本机完成登录，使用本地 CLI 支持的非交互/只读策略，再接入 profile。Reasonix 首次初始化可能较慢，未确认轻量配置时使用 `timeout: 1800`。

服务端超时会发送 `cancel`；内置 worker 现在会在子进程运行期间继续接收控制帧，并终止整个子进程组。调用方未传 `timeout` 时，服务端使用目标 worker 为所选或默认 Profile 声明的 `suggested_timeout`；调用方显式值仍优先。任务被 worker 接受后才重新计算完整执行时限，服务端另留 10 秒用于终止子进程并送达超时结果，这 10 秒不会扩大子进程执行时间。

“在线”只证明 WebSocket 已注册，不证明每个本地 CLI 已登录或能联网。必须用运行 worker 的同一系统用户逐个执行最小测试。需要代理时在对应 Profile 中显式加入 `pass_env: [HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY]`；Claude 若依赖 `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` 也要逐项放行。不要以 root 运行指向普通用户 HOME 的 CLI，否则可能生成普通用户不可写的认证、状态或日志文件。日志中出现 `profile_task_completed` 但没有 `profile_task_returned`，表示本地命令已经结束但 WS 结果送达失败。

## 输出被截断或出现大量 NUL

`--max-output-bytes` 按子进程原始输出字节限制。超过上限会设置 `output_truncated=true`。NUL、ANSI 和控制字符会被清理，NUL 长串只生成一条 `[NUL bytes omitted]` 提示，避免 JSON `\\u0000` 膨胀。

让调用方返回摘要、文件路径、行数或分段结果，不要简单提高到超大上限。

## 找到实际日志

```bash
PID=$(ss -ltnp | sed -nE 's/.*:9112 .*pid=([0-9]+).*/\1/p' | head -n1)
readlink "/proc/$PID/cwd"
ls -l "/proc/$PID/fd"
```

应用轮转日志通常是 `logs/synaptic_lathe.log`；`/tmp/*.log` 往往只是启动器重定向的 stdout/stderr。比较时间戳后再判断是否为当前故障。
