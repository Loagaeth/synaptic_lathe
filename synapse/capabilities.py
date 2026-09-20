"""One live catalog for discovery, Web selection, and MCP permissions.

Worker declarations describe an endpoint, never grant access to it. Runtime
observations and operator policy deliberately remain separate from claims.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from synapse.config import GlobalConfig
from synapse.connection import connection_manager

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def capability_id(agent: str, profile: str = "") -> str:
    return f"{agent}/{profile}" if profile else agent


def agent_snapshot(config: GlobalConfig) -> dict[str, list[dict[str, Any]]]:
    """Keep the legacy snapshot shape, but give every consumer the same source."""
    configured = [
        {"name": name, "type": agent.type, "source": "config", "online": connection_manager.is_online(name)}
        for name, agent in sorted(config.agents.items())
    ]
    online = [
        {**deepcopy(item), "type": "websocket", "source": "ws", "online": True}
        for item in connection_manager.online_agent_details()
    ]
    merged = {item["name"]: dict(item) for item in configured}
    for item in online:
        name = item["name"]
        if name in merged:
            adapter_type = merged[name]["type"]
            merged[name].update(item, source="config+ws", type=adapter_type)
        else:
            merged[name] = dict(item)
    return {"configured": configured, "online": online, "available": [merged[name] for name in sorted(merged)]}


def capability_records(config: GlobalConfig) -> list[dict[str, Any]]:
    records = []
    for agent in agent_snapshot(config)["available"]:
        name = agent["name"]
        client = agent.get("client", {})
        caps = client.get("profile_capabilities", {})
        # Configured adapters take precedence in the task dispatcher too.
        is_http = name in config.agents
        profiles = [] if is_http else sorted(set(client.get("profiles", [])) | set(caps))
        for profile in profiles or [""]:
            if profile and not _NAME.fullmatch(profile):
                continue
            metadata = caps.get(profile, {}) if profile else {}
            timeout = metadata.get("suggested_timeout") or metadata.get("timeout") or client.get("default_timeout")
            if is_http:
                timeout = 60
            declared = {
                "tags": metadata.get("tags", []),
                "advisory_safe": metadata.get("advisory_safe", False),
                "supports_session": metadata.get("supports_session", False),
                "session_required": metadata.get("session_required", False),
                "hints": metadata.get("hints", []),
            }
            record = {
                "id": capability_id(name, profile),
                "agent": name,
                "profile": profile,
                "execution_mode": "agent",
                "transport": "http" if is_http else "websocket",
                "declared": declared,
                "timeout_hint": timeout or 60,
                "risk_level": "unverified",
            }
            revision_data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            record["revision"] = hashlib.sha256(revision_data).hexdigest()[:16]
            record["observed"] = {"online": agent["online"], "last_seen": agent.get("last_seen")}
            # HTTP availability means configured, not a successful health check.
            record["dispatchable"] = is_http or agent["online"]
            records.append(record)
    return records


def resolve_profile_defaults(
    client: Mapping[str, Any],
    requested_profile: str = "",
    *,
    default_timeout: int = 60,
) -> tuple[str, int]:
    """Resolve a Worker's advertised profile and bounded default timeout."""

    capabilities = client.get("profile_capabilities")
    if not isinstance(capabilities, Mapping):
        capabilities = {}
    advertised = client.get("profiles")
    names = {str(name) for name in advertised} if isinstance(advertised, list) else set()
    names.update(str(name) for name in capabilities)

    selected = requested_profile or str(client.get("default_profile") or "")
    if not selected and len(names) == 1:
        selected = next(iter(names))

    profile_meta = capabilities.get(selected)
    if not isinstance(profile_meta, Mapping):
        profile_meta = {}
    raw_timeout = (
        profile_meta.get("suggested_timeout")
        or profile_meta.get("timeout")
        or client.get("default_timeout")
        or default_timeout
    )
    if isinstance(raw_timeout, bool):
        return selected, default_timeout
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError):
        timeout = default_timeout
    return selected, min(max(timeout, 1), 3600)
