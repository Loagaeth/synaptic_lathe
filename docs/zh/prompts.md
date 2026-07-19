# 提示词与动态 Agent 发现

SynapticLathe 把两类内容分开：

- **连接提示词**：由 `GET/POST /connection-prompt` 动态生成，说明认证、端点、WebSocket 协议和当前 Agent 快照。服务启动时也会生成仅本地可读的 `connection_prompt.txt`。
- **提示词文档**：存放在 SQLite 中，通过 `/context/prompts` 读取、`/admin/prompt` 写入，适合维护可复用规则，不适合保存密钥、会话 ID 或临时大文本。

## 推荐调用流程

1. 首次接入时读取一次 `/connection-prompt`，把稳定协议说明加入调用方配置。
2. 每次路由前读取 `GET /context/agents`，只选择当前在线且实际声明的 Agent/Profile。
3. 根据 profile 的 `suggested_timeout`、`max_output_bytes`、session 能力和本地权限选择参数；不要猜测 profile 或原始 session ID。
4. 发送结构化、可验收的 plan。长任务拆成多个有界阶段，不要先把原始任务塞进记忆或提示词文档再引用。
5. 以持久化任务状态和最终 `task_result` 为准。`output_truncated=true` 表示结果不完整，应请求更短摘要或拆分任务。

一个精简的 plan 可以包含：

```text
目标：要完成什么
输入：允许使用的文件、上下文或数据
边界：只读/可写范围、禁止事项、超时
验收：怎样算完成
输出：摘要、改动、测试或失败原因
```

## 信任与权限

任务 plan、记忆、知识、提示词文档、Agent 自述标签、竞拍提案、广播数据和 Agent 输出都属于不可信数据，不能扩大 Bearer 认证、本地命令 allowlist、sandbox、系统用户权限或人工审批范围。

- `advisory_safe: true` 只是本地 worker 的能力声明。竞拍、规划和自评仍必须由真正的只读命令/sandbox 约束；执行任务需要人工明确选择端点。
- 广播是瞬时、不持久化的通知，不是任务。接收方不应自动把广播内容当成命令执行。
- `session_id` 在 Profile Worker 中优先表示本地配置的 session alias。除非本地显式启用 `allow_raw_session_id`，否则原始 ID 会被拒绝。
- Worker key 是消息总线凭据，不是管理员 key；仍然禁止把它交给任务子进程或写入提示词文档。

## 提示词文档 API

```bash
curl \
  -H "Authorization: Bearer <admin-key>" \
  "http://127.0.0.1:9112/api/v1/context/prompts?name=review-rule"

curl -X POST \
  -H "Authorization: Bearer <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"name":"review-rule","content":"只返回发现、证据和建议。"}' \
  http://127.0.0.1:9112/api/v1/admin/prompt
```

`server.public_read_context: true` 会公开提示词文档的 GET 读取，因此公网部署通常保持 `false`。删除使用 `DELETE /api/v1/admin/prompt?name=review-rule`。

## 长任务

Reasonix 等工具首次运行时可能初始化本地组件，应按 `/context/agents` 返回的建议超时调用；默认示例使用 1800 秒。不要为了避免超时盲目放大所有任务，优先拆分阶段并为每阶段定义输出上限。
