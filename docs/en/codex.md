# Codex Worker

## Integration Choice

Codex is not limited to the interactive CLI. The official integration surfaces include:

- `codex exec`: non-interactive CLI for scripts, CI, and local automation.
- Codex SDK: TypeScript / Python SDKs for controlling Codex from application code.
- Codex app-server: a local JSON-RPC 2.0 service for rich clients with sessions, approvals, and event streams.
- Codex MCP server: exposes `codex` and `codex-reply` tools to MCP clients and the Agents SDK.
- GitHub Action: runs Codex in CI and can produce patches.

This project currently uses `codex exec` because it gives SynapticLathe the clearest permission boundary: the server only routes tasks, while Codex runs on the execution host as a separate worker process. Filesystem access is controlled by the worker's OS user, `--workdir`, and Codex sandbox settings.

## Install

Run the SynapticLathe server normally. On the machine that should execute Codex tasks, install this worker and the Codex CLI.

```bash
git clone https://github.com/Loagaeth/synaptic_lathe.git
cd synaptic_lathe
pip install -e .
```

Install Codex CLI from the official Codex Quickstart, then verify it is available:

```bash
codex --version
codex login
```

`codex login` stores local Codex credentials. Do not commit `~/.codex/auth.json`, project-local `.codex/`, API keys, or access tokens.

## Start the Worker

Read-only mode for review, summarization, and analysis:

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-codex-worker \
  --url ws://127.0.0.1:9112/ws \
  --name codex-local \
  --workdir /path/to/repo \
  --sandbox read-only
```

Allow Codex to edit the workspace:

```bash
SYNAPTIC_API_KEY='<worker-api-key>' synaptic-codex-worker \
  --url ws://127.0.0.1:9112/ws \
  --name codex-writer \
  --workdir /path/to/repo \
  --sandbox workspace-write
```

Use `danger-full-access` only inside an isolated container or disposable CI runner.

## Options

| Option | Environment Variable | Meaning |
|--------|----------------------|---------|
| `--url` | `SYNAPTIC_WS_URL` or `SYNAPTIC_URL` | SynapticLathe WebSocket URL |
| `--name` | `SYNAPTIC_AGENT_NAME` | Agent name registered on the server |
| `--key` | `SYNAPTIC_API_KEY` | Server `server.worker_api_key` |
| `--codex-bin` | `SYNAPTIC_CODEX_BIN` | Codex executable, default `codex` |
| `--workdir` | `SYNAPTIC_CODEX_WORKDIR` or `SYNAPTIC_WORKDIR` | Codex workspace, required |
| `--add-dir` | - | Additional accessible directory, repeatable |
| `--timeout` | `SYNAPTIC_CODEX_TIMEOUT` or `SYNAPTIC_TIMEOUT` | Task timeout in seconds, default 1800 |
| `--lock-file` | `SYNAPTIC_CODEX_LOCK_FILE` or `SYNAPTIC_WORKER_LOCK_FILE` | Single-instance lock file; default is derived from URL and agent name |
| `--allow-duplicate` | `SYNAPTIC_ALLOW_DUPLICATE_WORKER=1` | Disable local single-instance protection |
| `--no-reconnect` | `SYNAPTIC_RECONNECT=0` | Exit instead of reconnecting after disconnect |
| `--reconnect-initial-delay` | `SYNAPTIC_RECONNECT_INITIAL_DELAY` | Initial reconnect delay in seconds, default 1 |
| `--reconnect-max-delay` | `SYNAPTIC_RECONNECT_MAX_DELAY` | Maximum reconnect delay in seconds, default 30 |
| `--lock-retry-interval` | `SYNAPTIC_LOCK_RETRY_INTERVAL` | Duplicate-worker lock retry interval, default 60 |
| `--sandbox` | `SYNAPTIC_CODEX_SANDBOX` | `read-only` / `workspace-write` / `danger-full-access` |
| `--approval-policy` | `SYNAPTIC_CODEX_APPROVAL_POLICY` | `untrusted` / `on-request` / `never`, default `never` |
| `--model` | `SYNAPTIC_CODEX_MODEL` | Optional model override |
| `--profile` | `SYNAPTIC_CODEX_PROFILE` | Optional Codex profile |
| `--config` | - | Codex `key=value` config override, repeatable |
| `--pass-env` | `SYNAPTIC_CODEX_PASS_ENV` | Explicit environment variable name passed to `codex exec`, repeatable; env value is comma-separated |
| `--max-output-bytes` | `SYNAPTIC_CODEX_MAX_OUTPUT_BYTES` | stdout capture limit, default 1000000 |
| `--max-stderr-bytes` | `SYNAPTIC_CODEX_MAX_STDERR_BYTES` | retained stderr tail limit, default 64000 |
| `--skip-git-repo-check` | `SYNAPTIC_CODEX_SKIP_GIT_REPO_CHECK` | Pass through Codex's Git repo check bypass |
| `--no-ephemeral` | `SYNAPTIC_CODEX_EPHEMERAL=0` | Do not pass `--ephemeral` |

## Connection and Duplicate Instances

The worker keeps a long-lived WebSocket connection. If the initial connection fails or the running connection drops, it reconnects in-process with exponential backoff. The server still only accepts worker-initiated connections; it does not dial into private worker hosts.

Control reception and Codex execution run concurrently, so long tasks continue answering heartbeat frames. Server timeout, WebSocket disconnect, or worker shutdown terminates the Codex child process group.

On the same machine, one `agent_name + url` has one active Codex worker by default. Duplicate instances print one error and wait quietly for the lock instead of exiting into a systemd/Docker restart loop. Once the lock is released, the waiting process starts the connection loop.

## Call It

Once the worker is online, send a task to the registered name:

```json
{"type":"send","payload":{"target":"codex-local","plan":"review this repository and list the top 5 risks","timeout":900}}
```

The final Codex answer is returned as `task_result.payload.result`. `codex exec` writes progress to stderr; on failure, the worker includes the tail of stderr in the error payload for debugging.

## Security Notes

- Deploy the server and Codex worker separately; the server does not need local Codex filesystem permissions.
- Run one worker per permission boundary, for example separate read-only and write-enabled workers.
- Default to `--sandbox read-only`.
- Use `workspace-write` only when edits are needed, and keep `--workdir` scoped to the target repository.
- Do not commit OpenAI API keys, Codex access tokens, `~/.codex/auth.json`, or `.codex/`.
- The Codex child process inherits only an allowlisted environment by default; it does not inherit `SYNAPTIC_API_KEY` or other `SYNAPTIC_*` settings.
- The worker closes Codex stdin and inserts `--` before the remote plan, so callers cannot inject additional Codex CLI options through the plan.
- HTTP proxy variables are not inherited by default; pass `HTTPS_PROXY` / `NO_PROXY` explicitly when required.
- If you use `CODEX_API_KEY`, pass it explicitly with `--pass-env CODEX_API_KEY` only in trusted execution environments; avoid long-lived exposure in environments that run untrusted repository scripts.
- Large outputs are truncated by the worker and flagged as `output_truncated` / `stderr_truncated` in the return payload.

## Official References

- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [Codex SDK](https://developers.openai.com/codex/sdk)
- [Codex app-server](https://developers.openai.com/codex/app-server)
- [Codex MCP / Agents SDK](https://developers.openai.com/codex/guides/agents-sdk)
- [Codex authentication](https://developers.openai.com/codex/auth)
