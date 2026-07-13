# 快速开始

## 服务端

在源码目录运行 stdlib-only setup；它会创建 `.venv`、安装项目、生成私有 `config.yaml`、`data/`、`logs/`、`.synaptic/` 和快捷脚本：

```bash
git clone https://github.com/Loagaeth/synaptic_lathe.git
cd synaptic_lathe
python -m synapse.setup_wizard --yes
./synapticctl start
./synapticctl status
```

setup 分别生成管理员 `server.api_key` 和低权限 `server.worker_api_key`，只在终端打印一次。浏览器访问 `http://127.0.0.1:9112/`，输入管理员 key。常用命令：

```bash
./synapticctl logs
./synapticctl foreground
./synapticctl restart
./synapticctl stop
```

Windows 使用同目录生成的 `synapticctl.cmd start|stop|status|logs`；脚本会调用 setup 选定的虚拟环境 Python。

已有 `config.yaml` 默认不会覆盖；确需重建时先备份，再使用 `--force`。

## 本地 Profile Worker

在需要执行 Claude/Codex/Hermes/Reasonix 的机器运行。远程连接必须使用反向代理提供的 WSS 地址：

```bash
SYNAPTIC_API_KEY='<worker-api-key>' python -m synapse.worker_setup \
  --kind profile \
  --url wss://synapse.example.com/ws \
  --name local-dispatcher \
  --workdir /path/to/workspace \
  --yes
./workerctl start
./workerctl logs
```

setup 把 key 写入仅所有者可读的 `.synaptic/worker.env`，不会写入命令行生成的控制脚本。重复执行时保留已有 key；只有 `--clear-api-key` 会清空。看到日志中的 `SynapticLathe ws connected` 表示注册完成。

同机本地调试可使用 `ws://127.0.0.1:9112/ws`。真实命令、工作目录、权限策略和 session alias 保存在本机 `profiles.yaml`，不要提交。

## 最小公网配置

```yaml
server:
  host: "127.0.0.1"
  port: 9112
  api_key: "<admin-key>"
  worker_api_key: "<different-worker-key>"
  public_read_context: false
  cors_origins: ["https://synapse.example.com"]
  behind_proxy: true
  trusted_proxy_hosts: ["127.0.0.1", "::1"]
```

让 Nginx/Caddy/Traefik 暴露 HTTPS/WSS，不要把带 Bearer token 的明文端口直接暴露公网。详见 [服务端部署](deployment.md) 和 [安全边界](security.md)。

## Embedding 可选依赖

基础安装支持远程 OpenAI-compatible、NVIDIA 和 Ollama。使用本地模型或 Gemini SDK 时安装对应 extra：

```bash
pip install -e ".[embedding]"
pip install -e ".[gemini]"
```

Embedding 不可用时记忆/知识搜索会降级为关键词查询，写入本身仍可成功。

## 接入调用方

启动会生成 `connection_prompt.txt`。也可以调用 `GET/POST /connection-prompt` 获取稳定 bootstrap，运行时再读取：

- `GET /context/agents`：最新在线 Agent、能力和 profile timeout。
- `GET /context/prompts?name=xxx`：可更新的提示词文档。
- `GET /version`：协议版本和能力。

这样新增 worker 或提示词时不必频繁替换系统提示词。

## 开发检查

```bash
pip install -r requirements-dev.txt
ruff check .
ruff format . --check
pytest -q
bandit -q -r synapse
pip-audit -r requirements.txt
```
