# SynapticLathe 文档

## 推荐阅读顺序

1. [快速开始](quickstart.md)
2. [服务端部署](deployment.md)
3. [本地 Subprocess / Profile Worker](subprocess-worker.md)
4. [Codex Worker](codex.md)
5. [安全边界](security.md)
6. [REST API 与 WebSocket 协议](api.md)
7. [排查指南](troubleshooting.md)

SynapticLathe 是单进程 Agent 消息总线。服务端负责鉴权、路由、任务状态、SQLite 上下文和 Web 管理；本地命令只在独立 worker 中执行。当前内置固定命令 worker、Profile Dispatcher、Codex CLI worker 和 HTTP Agent adapter。

English docs: [../en/README.md](../en/README.md)
