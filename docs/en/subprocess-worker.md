# Local Subprocess / Profile Worker

## Permission Boundary

A worker is a separate client that initiates an outbound WebSocket connection. The server never needs the execution host's inbound IP and does not dial back, so NAT and different private networks are fine as long as the worker can reach server WSS.

Local commands have the permissions of the worker OS user. Use one worker per permission boundary, with a dedicated user, narrow workdir, read-only sandbox, and fixed wrapper. Do not expose a general shell.

## Install

Editable source checkout:

```bash
git clone https://github.com/Loagaeth/synaptic_lathe.git
cd synaptic_lathe
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Install the current Git version on an execution host:

```bash
python -m venv .venv
. .venv/bin/activate
pip install "git+https://github.com/Loagaeth/synaptic_lathe.git"
```

`python -m synapse.agents.*` requires the package to be installed or the current directory to be the source root. `--workdir` only affects task children; it does not repair the worker's own `PYTHONPATH`.
If the Git URL package is already installed in the current virtual environment and the current directory is not a source checkout, pass `--skip-install` to worker setup. Otherwise setup requires `pyproject.toml` or `requirements.txt` below `--project-dir` so it cannot silently generate an unusable empty environment.

## Recommended Setup

Profile Dispatcher:

```bash
SYNAPTIC_API_KEY='<worker-api-key>' python -m synapse.worker_setup \
  --kind profile \
  --url wss://synapse.example.com/ws \
  --name local-dispatcher \
  --workdir /srv/workspace \
  --yes
./workerctl start
./workerctl logs
```

Fixed-command worker:

```bash
SYNAPTIC_API_KEY='<worker-api-key>' python -m synapse.worker_setup \
  --kind subprocess \
  --url wss://synapse.example.com/ws \
  --name safe-runner \
  --command /usr/local/bin/safe-runner \
  --workdir /srv/tasks \
  --timeout 300 \
  --max-output-bytes 200000 \
  --yes
./workerctl start
```

Setup creates `.venv`, owner-only `.synaptic/worker.env`, logs, optional `profiles.yaml`, and `workerctl`. Existing keys are preserved; only `--clear-api-key` removes one. `SynapticLathe ws connected` in the log means registration succeeded.

With a source checkout and `--skip-install`, generated `workerctl` adds the explicit source root to its own `sys.path` and the child `PYTHONPATH` only when `synapse/__init__.py` is present. An ordinary non-source directory is not injected. For a Git URL installation, run setup from the same Python environment where the package was installed.

## Manual Fixed Command

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-subprocess-worker \
  --url ws://127.0.0.1:9112/ws \
  --name local-python \
  --command python \
  --workdir /path/to/project \
  --timeout 300 \
  --max-output-bytes 200000
```

The fixed-command worker passes tasks as `command ... -- "<plan>"` without shell composition, so a plan such as `--help` is not parsed as a program option. Use `--allow-plan-options` only for a target that explicitly rejects the `--` convention. A bounded `stdin` field can be forwarded when required. Never expose `bash`, `sh`, PowerShell, or `cmd.exe` to untrusted callers.

## Profile Dispatcher

Copy the public `profiles.example.yaml` to local `profiles.yaml`:

```yaml
default_profile: codex
timeout: 600
max_output_bytes: 200000

profiles:
  claude:
    command: ["claude", "-p", "{plan}", "--max-turns", "10", "--permission-mode", "plan"]
    workdir: .
    timeout: 600
    advisory_safe: true
    tags: [analysis, review, planning]

  codex:
    command:
      - codex
      - exec
      - --ephemeral
      - --sandbox
      - read-only
      - --config
      - "approval_policy='never'"
      - --skip-git-repo-check
      - --
      - "{plan}"
    workdir: .
    timeout: 1800
    advisory_safe: true
    tags: [code, review, planning]

  hermes:
    command: ["hermes", "--oneshot", "{plan}"]
    workdir: .
    timeout: 600
    advisory_safe: false
    tags: [general]

  reasonix:
    command: ["reasonix", "run", "--effort", "low", "--budget", "0.10", "{plan}"]
    workdir: .
    timeout: 1800
    advisory_safe: false
    tags: [reasoning]
```

Start it:

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-profile-worker \
  --url ws://127.0.0.1:9112/ws \
  --name local-dispatcher \
  --profiles ./profiles.yaml
```

The caller sends only profile, plan, optional session alias, and timeout:

```json
{"type":"send","payload":{"target":"local-dispatcher","profile":"codex","plan":"review this repo","timeout":1800}}
```

Only simple `{plan}`, `{profile}`, `{tool}`, `{session_id}`, `{session_alias}`, and `{source}` placeholders are allowed. Attribute/index access, format specifications, and conversions are rejected. Commands run as argv without a shell. If `{plan}` is absent, the worker appends `--` and the plan; an explicit positional `{plan}` should be preceded by `--` in the local profile.

A local `sessions` table maps aliases to real session IDs. Both `{session_id}` and `{session_alias}` pass through this allowlist; profiles without session placeholders ignore an extra alias. Raw IDs are denied by default; enabling them requires `allow_raw_session_id: true` and a bounded character-class `session_pattern` without groups or nested quantifiers. `allow_raw_session_id` and `advisory_safe` must be real YAML booleans (`true`/`false`); quoted values such as `"false"` are rejected to avoid ambiguous permission switches. Capability metadata contains alias names, not real values.

`tags` contains at most eight public short labels. Set `advisory_safe: true` only when the local profile is already enforced read-only, non-interactive, and suitable for proposals/planning; Web auctions, team planning, and self-assessment filter on this field. The declaration does not change command privileges. Hermes/Reasonix remain `false` by default until their local permission behavior is verified.

## Approval and Interaction

Profile child stdin is closed; the worker cannot click approval prompts. Complete login and initialization locally, then configure a non-interactive policy supported by that exact CLI version.

- The Claude example uses `--permission-mode plan` and is intended for analysis. Create a separate minimal-permission profile for tools.
- The Codex example uses `read-only` plus `approval_policy='never'`. Use a separate narrow `workspace-write` profile for edits.
- Hermes `--oneshot` and Reasonix `run` behavior can vary by version/local config; test them under the worker user first.
- Reasonix may initialize MCP on first run. Use `timeout: 1800` until local startup is known to be lightweight.

A command still waiting for a TTY or approval is terminated at timeout and returns `exit_code=-1`.

## Connection, Cancellation, Duplicates

- Workers keep a long-lived connection and reconnect in-process with exponential backoff.
- `websockets` 14-16 are supported through `additional_headers` / legacy `extra_headers` detection.
- Control reception and child execution run concurrently, so long tasks still answer ping and receive cancel.
- Server timeout, WS disconnect, or worker shutdown terminates the current child process group.
- The same kind + URL + Agent name uses an owner-only local lock. Duplicates wait quietly instead of creating a restart storm.
- The local pending task queue holds at most 16 tasks. Overload reconnects so the server fails affected work explicitly.

## Options

| Option | Environment | Meaning |
|--------|-------------|---------|
| `--url` | `SYNAPTIC_WS_URL` / `SYNAPTIC_URL` | `ws://` or `wss://`, no URL credentials |
| `--name` | `SYNAPTIC_AGENT_NAME` | 1-64 letters, digits, `_`, `-` |
| `--key` | `SYNAPTIC_API_KEY` | `server.worker_api_key` |
| `--workdir` | `SYNAPTIC_WORKDIR` | Child workdir |
| `--timeout` | `SYNAPTIC_TIMEOUT` | Default task timeout |
| `--max-output-bytes` | Worker-specific variable | Capture/stream limit |
| `--pass-env NAME` | Worker-specific `*_PASS_ENV` | Explicit child env, repeatable |
| `--no-reconnect` | `SYNAPTIC_RECONNECT=0` | Exit after disconnect |
| `--allow-duplicate` | `SYNAPTIC_ALLOW_DUPLICATE_WORKER=1` | Disable local lock; not recommended |

The default child environment contains basic PATH/HOME/locale/temp/certificate values. It excludes API keys, `SYNAPTIC_*`, dynamic-loader variables, and HTTP proxies. Pass a required proxy explicitly with `--pass-env HTTPS_PROXY --pass-env NO_PROXY`, or set `pass_env: [HTTPS_PROXY, NO_PROXY]` on the affected profile. CLI credentials belong to the OS account running the Worker; complete login and a minimal call as that same account first.

Output is bounded by raw bytes and NUL/ANSI/control data is removed before return. On `output_truncated=true`, request a summary or paged result instead of blindly increasing the limit.

## systemd Example

```ini
[Unit]
Description=SynapticLathe profile worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=synaptic-worker
WorkingDirectory=/srv/synaptic-worker
EnvironmentFile=/srv/synaptic-worker/.synaptic/worker.env
ExecStart=/srv/synaptic-worker/.venv/bin/synaptic-profile-worker --url wss://synapse.example.com/ws --name local-dispatcher --profiles /srv/synaptic-worker/profiles.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Do not start a second copy through systemd when the setup-generated `workerctl` already manages the same Agent.
