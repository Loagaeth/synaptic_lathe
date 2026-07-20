# Troubleshooting

## Nothing Is Listening

Check the listener and foreground traceback before changing firewalls:

```bash
ss -ltnp | grep ':9112'
ps -ef | grep -E 'synaptic|main.py|uvicorn' | grep -v grep
./synapticctl foreground
```

`ModuleNotFoundError: No module named 'yaml'` means the wrong Python lacks dependencies. Run `python -m synapse.setup_wizard --yes`, or install `requirements.txt` in the intended virtual environment. A parent process may remain after the server child exits, so the listening PID and foreground traceback are authoritative.

## Embedding 401

A successful `POST /admin/memory` confirms the database write, not embedding. A provider 401 leaves the row without a vector and search falls back to keywords.

Check the full `memory.embedding_api_key`, API base URL, model name, and the exact `config.yaml` loaded by the listening process.

```bash
grep -aE "401 Unauthorized|embedding API call failed|Api key is invalid" logs/synaptic_lathe.log
```

If direct provider curl succeeds, compare the listener PID's cwd and cmdline with the log timestamp instead of relying on an old `/tmp` redirect.

## Three Different Keys

- REST/Web administration: `server.api_key`
- WebSocket workers: `server.worker_api_key`
- Embedding provider: `memory.embedding_api_key`

A worker key receiving 403 from `/admin/*` is expected. Only old configurations with an empty worker key fall back to the administrator key.

## Worker Cannot Connect

- Use `wss://host/ws` remotely and `ws://127.0.0.1:9112/ws` only on the same host.
- Match `SYNAPTIC_API_KEY` to `server.worker_api_key`.
- Use a 1-64 character Agent name containing letters, digits, `_`, or `-`.
- `NAME_CONFLICT` means the same Agent name is online. Local duplicate workers normally wait on the single-instance lock.
- Verify proxy WebSocket Upgrade forwarding, certificate validity, and system time.
- `websockets` 14-16 are supported through `additional_headers` / legacy `extra_headers` detection.

Workers reconnect in-process with exponential backoff; they do not require a systemd/Docker restart loop.

## Timeout, Approval, or `exit_code=-1`

Profile workers close child stdin. A CLI waiting for login, a TTY, or human approval is terminated at timeout. Complete login locally and configure a supported non-interactive/read-only policy before exposing the profile. Reasonix initialization can be slow; use `timeout: 1800` until the local setup is known to be lightweight.

Server timeout emits `cancel`. Built-in workers continue receiving control frames while a child runs and terminate the whole child process group. If a caller omits `timeout`, the server uses the target Worker's advertised `suggested_timeout` for the selected or default profile; an explicit caller value still wins. The full execution budget starts again when the Worker accepts the task, and the server keeps a separate 10-second delivery grace for process termination and the timeout result. That grace does not extend child execution.

“Online” proves only that the WebSocket registered. It does not prove that every local CLI is authenticated or can reach its provider. Run a minimal command as the same OS account that runs the Worker. If a proxy is required, explicitly add `pass_env: [HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY]` to the affected profile; also allow `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` when Claude depends on them. Do not run a user-home CLI as root, because it can leave credential, state, or log files unwritable by the Worker account. A `profile_task_completed` event without a following `profile_task_returned` event means local execution ended but WebSocket result delivery failed.

## Truncation or NUL Output

`--max-output-bytes` counts raw child bytes and reports `output_truncated=true`. NUL, ANSI, and control data are removed; a long NUL run produces one `[NUL bytes omitted]` notice instead of expanding into JSON `\\u0000` sequences.

Prefer summaries, paths, line counts, or paged results rather than very large limits.

## Find the Active Log

```bash
PID=$(ss -ltnp | sed -nE 's/.*:9112 .*pid=([0-9]+).*/\1/p' | head -n1)
readlink "/proc/$PID/cwd"
ls -l "/proc/$PID/fd"
```

The rotating application log is normally `logs/synaptic_lathe.log`. `/tmp/*.log` is often a launcher's stdout/stderr redirect; compare timestamps before treating it as current.
