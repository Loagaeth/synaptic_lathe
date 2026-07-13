"""Agent 适配器层。

包初始化只导入轻量基类；具体 Agent 通过懒加载暴露，避免独立 worker
被其他适配器的可选依赖阻塞。
"""

from synapse.agents.base import BaseAgent, Message, Response

__all__ = [
    "BaseAgent",
    "Message",
    "Response",
    "ClaudeCodeAgent",
    "CodexAgent",
    "ProfileDispatcherAgent",
    "SubprocessAgent",
]


def __getattr__(name: str):
    if name == "ClaudeCodeAgent":
        from synapse.agents.claude_code import ClaudeCodeAgent

        return ClaudeCodeAgent
    if name == "CodexAgent":
        from synapse.agents.codex_agent import CodexAgent

        return CodexAgent
    if name == "ProfileDispatcherAgent":
        from synapse.agents.profile_agent import ProfileDispatcherAgent

        return ProfileDispatcherAgent
    if name == "SubprocessAgent":
        from synapse.agents.subprocess_agent import SubprocessAgent

        return SubprocessAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
