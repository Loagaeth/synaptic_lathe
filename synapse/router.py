"""Message routing by configured or registered Agent name."""

from __future__ import annotations

from synapse.config import RouterConfig
from synapse.connection import connection_manager


def select_target(config: RouterConfig, requested_target: str, plan: str) -> str:
    """Select an explicit target, then the first matching rule, then the default."""

    if requested_target:
        return requested_target
    for rule in config.rules:
        prefix_matches = not rule.prefix or plan.startswith(rule.prefix)
        keyword_matches = not rule.keyword or rule.keyword in plan
        if prefix_matches and keyword_matches:
            return rule.target
    return config.default_agent


async def resolve_target(agent_name: str) -> dict:
    """解析目标 agent。返回 {"online": bool, "agent_name": str, "error": str|None}。"""
    online = connection_manager.is_online(agent_name)
    if online:
        return {"online": True, "agent_name": agent_name, "error": None}
    return {
        "online": False,
        "agent_name": agent_name,
        "error": f"Agent '{agent_name}' is offline",
    }


async def route_message(
    target_agent: str,
    message: dict,
) -> dict:
    """路由消息到目标 agent。返回 {"sent": bool, "error": str|None}。"""
    resolution = await resolve_target(target_agent)
    if not resolution["online"]:
        return {"sent": False, "error": resolution["error"]}

    sent = await connection_manager.send_or_queue(target_agent, message)
    if not sent:
        return {"sent": False, "error": f"Failed to deliver to '{target_agent}'"}
    return {"sent": True, "error": None}
