# 统一能力目录与共享 MCP

[文档首页](README.md) | [English](../en/fabric.md)

## 边界

Fabric 是现有服务内部的能力与调用层，不是另一台服务，也不是 A2A 协议实现。
HTTP adapters、在线 Worker/Profile 形成唯一能力目录；Web 和 MCP 共用任务控制器、SQLite 任务状态与统计。
旧 WS 仍使用原有任务处理路径和同一个任务存储，没有新增第二套队列。

每项能力包含 `id`（`agent` 或 `agent/profile`）、执行方式、传输方式、
`declared` 本地声明、`observed` 连接观测、`timeout_hint` 和 `revision`。
revision 是声明内容的摘要，不是签名或身份证明，心跳不会改变它。
`risk_level=unverified` 明确表示服务端未验证工具安全性。
HTTP adapter 的 `dispatchable` 表示已配置，不表示健康检查成功。

管理员通过 `GET /admin/capabilities` 或 `/api/v1/admin/capabilities` 获取目录。
`/context/agents` 保留为同源兼容视图；旧 Worker 不必升级注册协议。

## 启用

在服务端虚拟环境中安装可选依赖：

```bash
python -m pip install '.[mcp]'
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

为每个调用方分别生成随机 token，再编辑已有 `config.yaml`：

```yaml
fabric:
  enabled: true
  clients:
    planner:
      token: "<replace-with-a-unique-random-token>"
      capabilities:
        - "local-dispatcher/codex"
      submit_tasks: true
      resources:
        - "synapse://prompts/team-guide"
```

不要提交真实配置或 token。必须同时设置 `server.api_key`，保持 `public_read_context: false`；
Fabric token 不得与 admin/worker key 相同，也不能在不同调用方之间复用。
token 必须为 32 至 512 个字符并符合 HTTP Bearer token 字符集；上面的随机生成命令满足要求。
修改配置后重启服务。服务未安装 MCP extra 时，启用会明确报错；默认关闭时核心服务不依赖 SDK。

`server.cors_origins` 必须包含客户端实际使用的服务地址，例如
`https://synapse.example.com`。网关按这些地址校验 Host 和可选的 Origin，反向代理需保留正确 Host。
这不是来源认证：无 Origin 的 CLI 客户端仍需 token。公网只能通过 HTTPS 使用。

支持 MCP Streamable HTTP 的客户端连接 `https://synapse.example.com/mcp`，
请求头为 `Authorization: Bearer <planner-token>`。不要使用管理员 key 代替。
传输由官方 Python MCP SDK 1.x 提供，使用无会话 HTTP 模式；无需把 MCP 会话绑定到某个 WS Agent。

## 六个控制工具

| 工具 | 输入与用途 |
| --- | --- |
| `capabilities_list` | 可选 query；仅返回获准的能力、超时建议和是否允许执行 |
| `tasks_submit` | capability、plan、可选 title/timeout；立即返回 task_id |
| `tasks_get` | task_id；只读取本调用方创建的任务 |
| `tasks_cancel` | task_id、reason；只中断本调用方任务 |
| `context_read` | uri、可选字符 offset；分段读取获准文档 |
| `context_search` | query、可选 kind/limit；只对获准文档做关键词检索 |

不再同时增加 agents_search/agents_describe 等重复发现工具，统一由能力目录检索承担。
未授权提交时 tools/list 不显示 tasks_submit，直接猜测工具名调用也会被拒绝。

使用 `tasks_submit` 后以至少 2 秒间隔轮询 tasks_get，采用目录建议的 timeout；
Reasonix 冷启动可能需 1800 秒。任务结果最多返回 32,000 字符，截断会明确设置
`output_truncated=true`。任务状态沿用原有状态机，不重命名现有终态。
暂不暴露 session alias、persona 或原始 session ID 给 MCP 提交方。
任务出错后先检查状态，不应盲目重发 tasks_submit；当前没有跨重试幂等提交键。

MCP 任务持久化为 `source_kind=api`，身份由服务端绑定，调用方不能自行覆盖。
它们也显示在 Web 任务页，可由管理员中断；连接断开不等于取消已经提交的任务。

## 共享文档

MCP resources/list 和 resources/read 与 context_read 使用同一份权限规则和数据库，
不是另一套上传或同步机制。允许的 URI：

- `synapse://skills/<name>`
- `synapse://prompts/<name>`
- `synapse://personas/<name>`

名称目前限定 ASCII 字母、数字、下划线、点、连字符，最多 128 字符。
通过现有 Web/管理员 API 创建文档，在 resources 中逐项共享；不存在的文档读取返回错误。
未授权文档与不存在的文档都返回相同的未找到结果，不暴露内容。
长文档通过 next_offset 继续读取，offset 按 Unicode 字符计算，而不是字节。
文档与工具输出都属于不可信内容，不可改变权限或本地执行策略。
这一版不通过 MCP 暴露记忆/知识库的全部内容或向量搜索，也不提供文档写入工具。

## 权限与限制

权限默认全空，不支持 `*` 或 `agent/*` 通配符。capabilities 控制目录可见性与可选的执行目标，
submit_tasks 单独控制提交；resources 控制逐文档读取。
能力 tags/advisory_safe 只是 Worker 声明，不能替代调用方授权或操作系统 sandbox。

授予本地代码工具执行权限意味着允许它在 Worker 的本地权限范围内运行任务；
这不是操作系统级多租户隔离。互不信任的用户必须使用独立 Worker 用户/工作区/沙箱。
现有 WS worker key 仍代表整个消息总线信任域，不因本次新增 MCP 而获得逐 Agent 隔离；
受限调用方只能拿 Fabric token，不能同时拿管理员或共享 Worker key。

当前未实现任意上游 MCP 聚合、远端工具自动导入、跨服务器联邦、签名能力验证、
审批交互回传和新任务状态扩展。后续接入这些能力应复用当前目录与授权服务，
不再另建同类功能。官方 SDK 说明见 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。
