"""Скиллы для тестов SDK: pydantic-путь, явные схемы, async, ошибки, LLM."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from skill_sdk import SkillContext, SkillError, skill


class AddIn(BaseModel):
    a: int
    b: int = Field(ge=0)


class AddOut(BaseModel):
    sum: int
    note: str | None = None


@skill("math.add", version="1", side_effects="none", risk="low", timeout=30)
def add(inputs: AddIn, ctx: SkillContext) -> AddOut:
    """Сложить два числа.

    Второй абзац в описание контракта не попадает."""
    ctx.add_cost("ops", 1)
    return AddOut(sum=inputs.a + inputs.b, note=ctx.idempotency_key)


class Doc(BaseModel):
    text: str


class Verdict(BaseModel):
    kind: Literal["short", "long"]


@skill("doc.classify", version="2", side_effects="none", risk="low", retry=(2, 5))
async def classify(inputs: Doc) -> Verdict:
    """Классифицировать документ по длине."""
    return Verdict(kind="short" if len(inputs.text) < 10 else "long")


@skill(
    "legacy.echo",
    version="1",
    description="Эхо со схемами опубликованной версии",
    side_effects="none",
    risk="low",
    inputs_schema={"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}},
    outputs_schema={"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}},
)
def echo(inputs: dict[str, Any]) -> dict[str, Any]:
    return {"x": inputs["x"]}


@skill(
    "ext.write",
    version="1",
    side_effects="external_write",
    risk="high",
    idempotency="required",
    retry=(3, 10),
)
def write(inputs: Doc, ctx: SkillContext) -> Verdict:
    """Внешняя запись: ошибки среды повторяемые, отказ — нет."""
    if inputs.text == "busy":
        raise SkillError("upstream_busy", "позже", retryable=True)
    if inputs.text == "denied":
        raise SkillError("denied", "отказано", details={"why": "policy"})
    if inputs.text == "crash":
        raise RuntimeError("boom")
    if inputs.text == "bad-output":
        return {"kind": "medium"}  # type: ignore[return-value]
    ctx.secret("EXT_TOKEN")
    return Verdict(kind="short")


class Summary(BaseModel):
    summary: str


@skill("doc.summarize", version="1", side_effects="none", risk="low")
async def summarize(inputs: Doc, ctx: SkillContext) -> Summary:
    """Пересказать текст через LLM инсталляции."""
    result = await ctx.llm.chat_json(
        system_prompt="Перескажи",
        messages=[{"role": "user", "content": inputs.text}],
        response_model=Summary,
        schema_name="summary",
    )
    return result.data
