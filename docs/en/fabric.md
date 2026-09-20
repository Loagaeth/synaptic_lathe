# Capability Registry and Shared MCP

[Documentation](README.md) | [中文](../zh/fabric.md)

Fabric is an internal capability/access layer, not another server or an A2A wire implementation.
HTTP adapters and online Worker profiles feed one live catalog. Web and MCP share the task
controller, SQLite task state, and invocation statistics. Existing WS tasks keep their current
handlers and use the same task store.

## Enable

Inside the server environment:

```bash
python -m pip install '.[mcp]'
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Generate a separate token for each caller and add to the private config.yaml:

```yaml
fabric:
  enabled: true
  clients:
    planner:
      token: "<replace-with-a-unique-random-token>"
      capabilities: ["local-dispatcher/codex"]
      submit_tasks: true
      resources: ["synapse://prompts/team-guide"]
```

An administrator key is required; public_read_context must be false. Fabric credentials must
differ from administrator/Worker keys and from other callers' tokens. Grants default to empty;
wildcards are rejected. Restart after configuration changes. Missing optional dependencies fail
explicitly when enabled; the disabled core does not import the SDK.
Tokens must contain 32 to 512 characters using HTTP Bearer token syntax; the generator above meets this requirement.

Connect a Streamable HTTP MCP client to https://synapse.example.com/mcp with
Authorization: Bearer <planner-token>. The gateway uses the official Python MCP SDK 1.x
in stateless HTTP mode, without inventing an additional Agent registration/session mechanism.
Add the actual external origin to server.cors_origins; both Host and optional Origin are validated.
Preserve Host through the proxy. A CLI with no Origin still needs a token. Use HTTPS outside loopback.

## Catalog and Controls

Administrators use GET /admin/capabilities (also /api/v1/admin/capabilities).
The old /context/agents response is a compatibility view of the same source.

Each record has an agent or agent/profile ID, transport, execution mode, declared metadata,
observed connection state, timeout_hint, and a revision hash. Heartbeats do not change revision.
The hash is not a signature or proof of identity. Risk remains unverified; HTTP dispatchable
means configured, not health-checked.

| Tool | Purpose |
| --- | --- |
| capabilities_list | Search only permitted live capabilities using optional query |
| tasks_submit | capability, plan, optional title/timeout; immediately returns task_id |
| tasks_get | Status and bounded output of your own task |
| tasks_cancel | Cancel your own task with a reason |
| context_read | Read an allowed URI, optionally from a character offset |
| context_search | Keyword search of explicitly shared documents; optional kind/limit |

There are no redundant agents_search/agents_describe tools. Tools/list hides tasks_submit
for readers, and direct invocation still enforces permission checks.
Poll at intervals of at least two seconds. Use timeout_hint; Reasonix cold starts may need 1800 seconds.
Results are bounded to 32,000 characters and explicitly report output_truncated.
Existing task statuses are unchanged. MCP submission does not expose sessions, personas,
or caller identity overrides. There is no cross-retry idempotency key yet: check task status
before submitting replacement work.

Tasks use source_kind=api and a server-assigned owner. Web administrators see and can cancel
them; disconnecting an MCP client does not implicitly cancel accepted work.

## Shared Documents

Resources/list, resources/read and context_read share authorization and existing database readers.
Allowed URI forms are synapse://skills/name, synapse://prompts/name, synapse://personas/name.
Names accept ASCII letters, digits, underscore, dot and hyphen, up to 128 characters.
Create documents using the existing Web/admin API, then grant exact URIs.
Missing and unauthorized resources return the same not-found result.
Continue long reads using next_offset, counted in Unicode characters.
Documents and outputs remain untrusted data, never authorization instructions.

This version does not expose memory/knowledge pools, embedding search or document writes through MCP.
It does not aggregate arbitrary upstream MCP servers, federate servers, verify signed capabilities,
relay approval interactions, or introduce new lifecycle states.

## Migration and Trust

LLM-generated self-assessment storage and /admin/agent-tags* were removed.
Local Profile tags are retained as declarations. Existing historical database tables are not dropped;
old task results remain accessible until normal retention cleanup.
Auctions, human-gated team plans, probes, broadcast, existing REST/WS clients and data remain.
See [migration notes](../fabric-migration.md).

Fabric tokens restrict catalog/document access and task ownership, not operating-system privileges.
Granting a coding endpoint permits execution inside that Worker's configured workspace and sandbox.
Use separate OS users/workspaces for untrusted tenants. Existing shared WS Worker keys still grant
bus-wide trust; do not give them to scoped MCP callers.
SDK reference: [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x).
