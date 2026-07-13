"""Context CRUD 模块 — memories / skills / knowledge / personas。"""

from synapse.context.embedding import get_embedding, search_similar
from synapse.context.knowledge import (
    add_knowledge,
    delete_knowledge,
    list_knowledge,
    search_knowledge,
)
from synapse.context.memory import (
    add_memory,
    delete_memory,
    get_memories,
    get_recent_memories,
    search_memory,
)
from synapse.context.persona import (
    delete_persona,
    get_persona,
    list_personas,
    list_personas_detailed,
    set_persona,
)
from synapse.context.prompts import (
    delete_prompt,
    get_prompt,
    list_prompts,
    list_prompts_detailed,
    set_prompt,
)
from synapse.context.skills import add_skill, delete_skill, get_skill, list_skills, list_skills_detailed

__all__ = [
    "add_memory",
    "delete_memory",
    "get_memories",
    "get_recent_memories",
    "search_memory",
    "add_skill",
    "delete_skill",
    "get_skill",
    "list_skills",
    "list_skills_detailed",
    "add_knowledge",
    "delete_knowledge",
    "list_knowledge",
    "search_knowledge",
    "delete_persona",
    "get_persona",
    "list_personas",
    "list_personas_detailed",
    "set_persona",
    "delete_prompt",
    "get_prompt",
    "list_prompts",
    "list_prompts_detailed",
    "set_prompt",
    "get_embedding",
    "search_similar",
]
