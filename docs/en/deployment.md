# Server Deployment

## Runtime Model

WebSocket connections, rate state, and reconnect delivery queues currently live in one server process. Do not use `uvicorn --workers N`; move connection discovery and delivery state to a shared backend before horizontal scaling.

Use setup to create a virtual environment, private runtime directories, and the controller:

```bash
python -m synapse.setup_wizard --yes
./synapticctl start
./synapticctl status
./synapticctl logs
```

Use `./synapticctl foreground` for diagnosis. The controller validates PID, process start identity, and argv instead of trusting a reusable PID alone.

## Public Configuration

Keep the backend on loopback and terminate TLS at a reverse proxy:

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

The proxy must forward HTTP and WebSocket Upgrade traffic and must overwrite, rather than blindly append, client-supplied `X-Forwarded-For`. List only real proxy IPs/CIDRs in `trusted_proxy_hosts`. Browser origins must exactly match `cors_origins`.

Remote workers use WSS and the least-privilege key:

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-profile-worker \
  --url wss://synapse.example.com/ws \
  --name build-host \
  --profiles ./profiles.yaml
```

Do not expose plaintext `ws://` or HTTP Bearer credentials publicly.

## Logs

Setup writes application logs to `logs/synaptic_lathe.log` and disables duplicate stdout logging for background mode. Manual startup uses a log directory relative to the current workdir. Override it with `SYNAPTIC_LOG_DIR`, `SYNAPTIC_LOG_FILE`, and `SYNAPTIC_LOG_STDOUT`.

On Linux, locate the listening process and open files with:

```bash
PID=$(ss -ltnp | sed -nE 's/.*:9112 .*pid=([0-9]+).*/\1/p' | head -n1)
readlink "/proc/$PID/cwd"
ls -l "/proc/$PID/fd"
```

Docker, systemd, supervisor, or AstrBot may separately redirect stdout/stderr to container logs or `/tmp`; those are not the application's rotating file log.

## Data and Upgrades

Stop the service before copying SQLite files and retain:

- `config.yaml`: administrator, worker, and provider keys; never commit it.
- `data/synaptic_lathe.db` plus WAL/SHM while active.
- `profiles.yaml` and `.synaptic/worker.env` on execution hosts only.

Logs, generated connection prompts, controllers, and PID state are runtime artifacts. During Alpha updates, reinstall the checkout with `pip install -e .` and restart; startup performs backward-compatible database migration.
