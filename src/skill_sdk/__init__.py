"""skill-sdk — скиллы платформы Taimen в коде (TAI-ADR-0045).

from pydantic import BaseModel
from skill_sdk import SkillContext, SkillError, skill

class Query(BaseModel):
    text: str

class Answer(BaseModel):
    summary: str

@skill("doc.summarize", version="1", side_effects="none", risk="low", timeout=120)
async def summarize(inputs: Query, ctx: SkillContext) -> Answer:
    '''Кратко пересказать текст.'''
    result = await ctx.llm.chat_json(system_prompt="…", messages=[…],
                                     response_model=Answer, schema_name="answer")
    return result.data
"""

from skill_sdk.context import Invocation, SkillContext, configure_llm
from skill_sdk.core import ArtifactContent, SnapshotStale, configure_core
from skill_sdk.errors import SkillError
from skill_sdk.skill import Http, Local, Mcp, Skill, discover, skill

__all__ = [
    "ArtifactContent",
    "Http",
    "Invocation",
    "Local",
    "Mcp",
    "Skill",
    "SkillContext",
    "SkillError",
    "SnapshotStale",
    "configure_core",
    "configure_llm",
    "discover",
    "skill",
]
