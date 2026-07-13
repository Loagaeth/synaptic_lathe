# Quick Start

## Server

Run the stdlib-only setup from a source checkout. It creates `.venv`, installs the project, writes a private `config.yaml`, creates `data/`, `logs/`, and `.synaptic/`, and generates a controller:

```bash
git clone https://github.com/Loagaeth/synaptic_lathe.git
cd synaptic_lathe
python -m synapse.setup_wizard --yes
./synapticctl start
./synapticctl status
```

Setup generates separate administrator `server.api_key` and least-privilege `server.worker_api_key` values and prints them once. Open `http://127.0.0.1:9112/` and enter the administrator key.

```bash
./synapticctl logs
./synapticctl foreground
./synapticctl restart
./synapticctl stop
```

On Windows, use the generated `synapticctl.cmd start|stop|status|logs`; it invokes the virtual-environment Python selected by setup.

An existing `config.yaml` is preserved unless `--force` is explicitly used.

## Local Profile Worker

Run this on the machine that should execute Claude, Codex, Hermes, or Reasonix. Remote connections must use WSS from your reverse proxy:

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

Setup stores the key in owner-only `.synaptic/worker.env`, not in the generated command line. Rerunning setup preserves it; only `--clear-api-key` removes it. `SynapticLathe ws connected` in the log means registration succeeded.

For same-host development, use `ws://127.0.0.1:9112/ws`. Keep executable paths, permission policy, workdirs, and session aliases in local `profiles.yaml`; do not commit it.

## Minimal Public Configuration

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

Expose HTTPS/WSS through Nginx, Caddy, or Traefik. Do not expose plaintext Bearer-token traffic publicly. See [Server Deployment](deployment.md) and [Security Boundaries](security.md).

## Optional Embedding Dependencies

The base install supports remote OpenAI-compatible, NVIDIA, and Ollama endpoints. Install extras for a local model or the Gemini SDK:

```bash
pip install -e ".[embedding]"
pip install -e ".[gemini]"
```

If embedding is unavailable, memory and knowledge search fall back to keyword matching; writes can still succeed.

## Connect a Caller

Startup writes `connection_prompt.txt`. `GET/POST /connection-prompt` also returns a stable bootstrap prompt. At runtime, read:

- `GET /context/agents` for current Agents, capabilities, and profile timeouts.
- `GET /context/prompts?name=xxx` for mutable prompt documents.
- `GET /version` for protocol metadata.

This avoids replacing the system prompt whenever a worker or prompt document changes.

## Development Checks

```bash
pip install -r requirements-dev.txt
ruff check .
ruff format . --check
pytest -q
bandit -q -r synapse
pip-audit -r requirements.txt
```
