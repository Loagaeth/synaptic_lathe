# Fabric 收敛改动

## 移除与合并

- 移除 LLM 自评任务、生成标签解析/存储代码、Web 自评按钮和 /admin/agent-tags* 路由。
- 新数据库不再创建 agent_profile_tags；已有数据库保留旧表，不自动删数据，不把历史自述当作授权。
- 原配置 Agent、在线 Worker、Profile 标签和 Web 端点选择收敛到 capabilities.py。
- 旧 /context/agents 保留数据结构，改为同源兼容视图，不是另一套目录。
- Web 专用调度器改为 task_controller.py，MCP 与 Web 共用提交、取消、超时、统计和结果状态。
- 连接提示词与默认技能说明加入 MCP 入口，默认技能不再重复整份 WS 协议。

## 新增边界

- 默认关闭的官方 SDK Streamable HTTP /mcp；pyproject 的 mcp extra 可选安装。
- 独立 Fabric client token、精确能力/文档 allowlist、任务所有权校验。
- 启用要求管理员认证并关闭公开上下文；拒绝复用 admin/Worker/client 凭据。
- Host/Origin 校验、请求大小上限、无会话 HTTP、内容分页/截断、错误输入不回显。
- REST 与 Web 接入统一能力目录；MCP 任务也可由管理员查看和取消。
- 任务表增加 output_truncated 字段，随终态一起原子写入，避免 Worker 截断标记在 Web/MCP 查询时丢失；迁移保留旧数据。
- Pydantic 范围更新为 >=2.11,<3，以满足官方 MCP SDK 依赖；核心 requirements 不强制安装 MCP。
- 配置错误隐藏输入值，避免新权限配置校验时在报错中直接打印凭据。

## 有意保留

竞拍、团队人工审批是工作流；probe 是无副作用健康检查；broadcast 是瞬时通知。
这些并非能力发现的重复实现，保留人工门控，避免重构时丢失既有功能。
不重命名原任务状态，不清空任务、上下文、配置，不改变已有 Worker 的 WS 注册协议。
Profile 本地 tags/advisory_safe 声明仍保留，但绝不代表已验证安全或 MCP 授权。

## 升级

1. 备份私有 config.yaml 和 SQLite 数据库，然后升级依赖。
2. 不需要 MCP 时保持 fabric.enabled=false；旧 Worker 无需改动。
3. 使用过 /admin/agent-tags* 的脚本切换到 /admin/capabilities，读取 declared.tags。
4. 需要 MCP 时按中文/英文 Fabric 指南安装 extra、生成独立凭据、逐项授权，然后重启。
5. 验证 Web 能力目录、旧 WS 调用、MCP 越权拒绝与新任务返回，再安排正式部署。

本次没有自动部署服务器或重启本地 Worker；Git 发布在独立目录准备，由维护者执行 push。
任意上游 MCP 聚合、跨服务器联邦、签名验证及完整审批交互暂不宣称完成。

## 验证范围

- 回归测试覆盖现有 HTTP/WS、任务取消/超时、数据库迁移，以及官方 MCP 客户端和权限隔离。
- 浏览器检查覆盖桌面/手机的能力目录、连接探测、任务提交/结果和主题切换；使用模拟 Worker，不调用真实付费模型。
- Ruff、Bandit、JavaScript 语法检查和构建检查通过；依赖审计针对本次安装的运行/测试依赖，不覆盖未安装的可选模型依赖。
- 发布源码的隐私模式扫描未发现真实凭据、私钥或本机私有路径；真实配置、数据库、日志和缓存仍由 ignore 排除。

## 推送前复核（2026-09-21）

已修复：

- tasks_submit 可触发本地文件或外部状态修改，不应声明为无破坏性工具；MCP 注解改为保守标记，并补充回归测试。
- CORS 配置校验原先在错误信息中拼接完整 URL，可能回显误填的凭据；改为不带输入值的错误，并覆盖凭据、查询参数和非法端口。
- 连接提示词区分受限 MCP 与具有 HTTP 权限的旧客户端，避免要求 MCP 调用方访问管理员端点。
- 修正 README 中被写成字面量反斜杠 n 的架构/目录图和段落，移除遗留的标签存储描述。
- 修正示例 fabric.clients 注释的嵌套方式，并统一新模块和测试的 Ruff 格式。

本轮检查结果：

- Python 3.12：190 项测试通过；两条第三方测试依赖弃用警告不影响结果。
- Ruff lint/format、Bandit、JavaScript 语法及 Git diff 空白检查通过。
- requirements 与 pyproject 的核心/开发依赖一致；示例配置和本地文档链接校验通过。
- 本次安装的核心、MCP、开发依赖审计未发现已知漏洞；未安装的 embedding/gemini 可选依赖不在审计范围。
- 发布源码及保留的 Git 历史隐私模式扫描无命中；这不是对任意形式秘密或漏洞的完整证明。

剩余边界：server.py 的 WS 会话处理与 Web 脚本仍较集中，后续应按行为边界拆分，
不在发布前进行无关的大规模重写。MCP 尚无跨重试幂等提交键，不聚合任意上游 MCP，也不提供操作系统级多租户隔离。
