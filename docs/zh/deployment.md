# 服务端部署

## 运行模型

当前服务端把 WebSocket 连接、速率状态和断线补发队列保存在单进程内存中。不要使用 `uvicorn --workers N`；需要高可用时先把连接目录和补发队列迁移到共享后端。

推荐通过 setup 生成虚拟环境、私有运行目录和控制脚本：

```bash
python -m synapse.setup_wizard --yes
./synapticctl start
./synapticctl status
./synapticctl logs
```

前台排查使用 `./synapticctl foreground`。控制脚本用 PID、进程启动标识和 argv 校验托管进程，不会仅凭复用的 PID 杀进程。

## 公网配置

后端只监听回环地址，由反向代理终止 TLS：

```yaml
server:
  host: "127.0.0.1"
  port: 9112
  api_key: "<random-admin-key>"
  worker_api_key: "<different-random-worker-key>"
  public_read_context: false
  cors_origins: ["https://synapse.example.com"]
  behind_proxy: true
  trusted_proxy_hosts: ["127.0.0.1", "::1"]
  outbound_trust_env: false
  max_body_bytes: 1048576
```

反向代理必须转发 HTTP 和 WebSocket Upgrade，并覆盖而不是盲目追加客户端传入的 `X-Forwarded-For`。只把实际代理 IP/CIDR 放进 `trusted_proxy_hosts`。浏览器 Origin 必须精确匹配 `cors_origins`。

远程 worker 使用 TLS 地址和低权限 key：

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-profile-worker \
  --url wss://synapse.example.com/ws \
  --name build-host \
  --profiles ./profiles.yaml
```

不要在公网直接暴露明文 `ws://`/HTTP Bearer token。

## 日志

setup 启动时，应用日志默认写入 `logs/synaptic_lathe.log`，标准输出由控制脚本关闭以避免重复。手动启动时日志目录相对于当前工作目录，也可通过 `SYNAPTIC_LOG_DIR`、`SYNAPTIC_LOG_FILE` 和 `SYNAPTIC_LOG_STDOUT` 调整。

Linux 上定位监听进程和实际文件描述符：

```bash
PID=$(ss -ltnp | sed -nE 's/.*:9112 .*pid=([0-9]+).*/\1/p' | head -n1)
readlink "/proc/$PID/cwd"
ls -l "/proc/$PID/fd"
```

Docker、systemd、supervisor 或 AstrBot 也可能把 stdout/stderr 重定向到容器日志或 `/tmp`，这些不是应用文件日志本身。

## 数据与升级

升级前停止服务并备份：

- `config.yaml`：管理员/worker/provider 密钥，本地保留，不提交。
- `data/synaptic_lathe.db` 及 WAL/SHM：先停服务再复制。
- `profiles.yaml`、`.synaptic/worker.env`：只留在执行机器。
- `logs/`、`connection_prompt.txt`、控制脚本和 PID 状态：运行时生成，不提交。

当前 Alpha 使用单实例 SQLite。更新代码后在同一虚拟环境重新安装 `pip install -e .`，再重启；启动会执行向后兼容的数据库迁移。
