"""Проверка скилла в тестах: тот же путь, что у хостинга, без исполнителя и ядра."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jsonschema

from skill_sdk.context import Invocation
from skill_sdk.skill import Implementation, Skill


@dataclass(frozen=True)
class Result:
    outputs: dict[str, Any]
    cost: dict[str, Any] | None


async def ainvoke(
    skill: Skill,
    inputs: Any,
    *,
    env: Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
) -> Result:
    invocation = Invocation(
        skill=skill.ref,
        protocol="test",
        invocation_id="test",
        idempotency_key=idempotency_key,
        timeout_seconds=skill.timeout,
    )
    outputs, cost = await skill.execute(inputs, invocation, env=env)
    return Result(outputs, cost)


def invoke(
    skill: Skill,
    inputs: Any,
    *,
    env: Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
) -> Result:
    """Вызвать скилл с проверкой входа и выхода по контракту; ``SkillError`` пробрасывается."""
    return asyncio.run(ainvoke(skill, inputs, env=env, idempotency_key=idempotency_key))


def check_contract(skill: Skill, implementation: Implementation | None = None) -> dict[str, Any]:
    """Контракт, который примет ядро. Валидаторы control-plane — если он установлен рядом,
    иначе проверка схем входа и выхода как JSON Schema 2020-12. Возвращает контракт."""
    contract = skill.contract(implementation)
    try:
        from control_plane.domain import skill_contract
    except ImportError:
        for name in ("inputs", "outputs"):
            jsonschema.Draft202012Validator.check_schema(contract[name])
        return contract
    normalized = skill_contract.normalize_contract(contract)
    side_effects, _risk = skill_contract.validate_policy_columns(skill.side_effects, skill.risk)
    skill_contract.require_safe_retries(normalized, side_effects)
    return contract
