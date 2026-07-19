# Security Boundaries

## Key Separation

| Setting | Purpose | Privilege |
|---------|---------|-----------|
| `server.api_key` | Web UI, REST administration/query endpoints, connection prompts | Administrator |
| `server.worker_api_key` | WebSocket Agent/Worker registration and messaging | Worker only |
| `memory.embedding_api_key` | Calls to the configured embedding provider | Upstream provider only |

The worker key cannot access REST administration endpoints. The administrator key is accepted for WebSocket registration as a super-credential. For compatibility, an empty `worker_api_key` falls back to `api_key`; new deployments should generate two different values. Never commit real keys, `config.yaml`, `profiles.yaml`, `.env`, or CLI credential directories.

The current `worker_api_key` defines a shared message-bus trust domain, not per-Agent credentials. Any holder can register an unused name, send tasks or broadcasts to other Agents, and receive messages addressed to its registered name; it still cannot call REST administration. Put mutually untrusted workers on separate instances, or isolate them with separate OS users, networks, and server keys.

## Authentication Matrix

- `/health` and `/version`: public and intentionally minimal.
- `GET /context` and `/context/agents|skills|personas|prompts`: administrator key by default and public when `public_read_context: true`. The two POST search endpoints always require the administrator key to prevent anonymous embedding spend.
- `/admin/*` (including tasks, SSE, stats, probes, auctions, and team approval), `/connection-prompt`, logs, and debug endpoints: administrator key.
- `/ws` and `/api/v1/ws`: worker key; the administrator key is also accepted.
- The static web page can load without credentials, but it only shows a key-entry panel until authenticated. Backend permissions do not change.

Empty keys are allowed only when explicitly binding to `127.0.0.1`, `localhost`, or `::1`. Keep public backends on a loopback address and expose them through an HTTPS/WSS reverse proxy.

`public_read_context: true` exposes memories, knowledge, skills, personas, prompt documents, and Agent status through those GET endpoints. Public deployments should normally keep it disabled.

## WebSocket

- Before registration, only `hello`, `register`, and `pong` are accepted. A second online connection with the same Agent name is rejected.
- Connection attempts, frame rate, and body size are bounded by the server rate-limit and `max_body_bytes` settings.
- Built-in workers keep receiving `ping` and `cancel` while a child is running. Timeout, disconnect, or shutdown terminates the current child process group.
- A source disconnect does not cancel submitted tasks. Results are queued in the current server process and redelivered when the same `agent_name` reconnects. This queue does not survive a server restart.
- A target disconnect marks its active tasks `ERROR` and reports `TARGET_DISCONNECTED`.
- Each worker executes tasks sequentially. Its local pending queue is bounded; overload forces a reconnect so the server fails affected tasks explicitly.
- `probe/probe_ack` is a transient no-side-effect connectivity frame and starts no LLM or child process. Acknowledgement identity comes from the registered connection; late or unknown acknowledgements are discarded.
- `broadcast` is a transient notification to currently online connections. It is neither durable nor queued offline. The sender name comes from the registered connection, but the payload remains untrusted and must not automatically become an executable task.
- Human cancellation atomically writes `CANCELLED` and its reason before removing undelivered work, stopping HTTP/timeout jobs, and sending cancellation only to an online target. This prevents cancelled work from running after reconnect.

## Web Tasks and Multi-Agent Coordination

- The browser uses administrator-authenticated HTTP/SSE and never registers as a synthetic Agent. Web results are not queued for a fake `web-console` connection. Static Web assets use a same-origin CSP and do not depend on `unsafe-inline` scripts or styles.
- Bidding, team planning, and self-assessment only accept profiles declaring `advisory_safe: true`. This is a local capability declaration, not a sandbox. Fixed argv, OS user, workdir, and the tool's read-only mode remain the real enforcement boundary.
- Bid claims and capability tags are untrusted self-reported display data. Candidate counts, field counts, and text lengths are bounded; the UI HTML-escapes values; none of them affect authentication, authorization, or executable command mapping.
- Team plans never fan out automatically. Execution tasks are created only after an administrator submits explicit assignments.
- Group states derived from child tasks are persisted with compare-and-set. A stale GET/list request cannot overwrite a concurrent human selection, approval, or cancellation.
- Each SSE live fragment is capped at 2048 characters, with bounded subscriber queues and subscriber count. Full output is read from authenticated task detail.

## Files and Database

File import endpoints only read UTF-8 regular files below project `data/`, up to 500000 bytes. Symlinks are rejected at the data root and in every path component. Database and WAL/SHM permissions are tightened through an opened descriptor after checking file type and ownership, while tolerating SQLite file rotation; configuration, process state, logs, and worker environment files are also made owner-only where supported. Configuration models reject unknown fields, so a misspelled key fails startup instead of silently selecting a default. Run the service as a dedicated OS user.

## Outbound HTTP

HTTP Agent adapters and embedding clients do not inherit ambient proxy variables by default. Enable `server.outbound_trust_env` or `memory.embedding_trust_env` explicitly when required. Base URLs must use HTTP(S) and may not contain credentials, queries, or fragments. An administrator-configured URL can still reach the server network; do not delegate URL configuration to untrusted callers, and use egress controls to block cloud metadata and other sensitive addresses.

## Subprocess Execution

The server never executes a shell. Local capability exists only in separate workers and is bounded by the worker OS user, workdir, Codex sandbox, and local profile allowlist.

- Commands and profile arguments are passed as argv without a shell. Fixed-command workers insert `--` before the plan by default; explicit positional profile placeholders should do the same.
- Child stdin is closed by default; commands waiting for a TTY, login, or human approval time out.
- The default child environment excludes `SYNAPTIC_*`, API keys, dynamic-loader variables, and proxy variables. Use `--pass-env` / `pass_env` for explicit exceptions.
- `--max-output-bytes` bounds capture and delivery. NUL, ANSI, and control data are removed at the protocol boundary.
- Do not expose a general shell as a public profile. Prefer read-only or narrowly scoped wrappers.

## Logs

HTTP logs contain method, path, status, duration, and client IP, but not query strings, bodies, or headers. Task logs contain Agent names, task IDs, profile/status fields, and truncation flags, but not plans, output bodies, session aliases, or raw session IDs. Common Bearer tokens, `sk-*` keys, assignment/JSON secrets, and URL credentials are redacted before handlers, but redaction is not a substitute for secret management. Log endpoints remain administrator-only.
