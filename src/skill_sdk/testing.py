"""Проверка скилла в тестах: тот же путь, что у хостинга, без исполнителя и ядра."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import jsonschema

from skill_sdk.context import Invocation
from skill_sdk.core import ArtifactContent, SnapshotStale
from skill_sdk.errors import SkillError
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


# --- подделка ядра для ctx.artifacts и ctx.knowledge (TAI-ADR-0056 Р5) ---


def _fingerprint(state: Mapping[str, Any]) -> str:
    import hashlib
    import json

    body = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
    return "st:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


class FakeArtifacts:
    def __init__(self) -> None:
        self.contents: dict[str, ArtifactContent] = {}
        self.reads: list[str] = []

    def put(self, artifact_id: str, data: bytes | str, media_type: str | None = None) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        self.contents[artifact_id] = ArtifactContent(raw, media_type)

    async def read(self, artifact_id: str, *, for_task: str | None = None) -> ArtifactContent:
        self.reads.append(artifact_id)
        if artifact_id not in self.contents:
            raise SkillError("artifact_not_found", f"нет артефакта {artifact_id}")
        return self.contents[artifact_id]


class FakeKnowledge:
    """Сверка снимка источника по естественным ключам, как её видит скилл.

    Состояние — открытые сущности пары ``(workspace, pack, source, scope)``;
    ``stateToken`` — отпечаток этого состояния. ``apply`` с устаревшим
    ``expectedState`` бросает ``SnapshotStale``. Ответ ``recall`` задаёт тест
    (``recall_answer``), запросы копятся в ``recalls``."""

    def __init__(self) -> None:
        self.state: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.applied: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.recalls: list[dict[str, Any]] = []
        self.recall_answer: dict[str, Any] = {"sections": []}

    @staticmethod
    def _slot(snapshot: Mapping[str, Any], workspace_id: str) -> tuple[str, str, str, str]:
        return (
            workspace_id,
            str(snapshot.get("pack", "")),
            str(snapshot.get("source", "")),
            str(snapshot.get("scope", "")),
        )

    @staticmethod
    def _entities(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for entity in snapshot.get("entities") or []:
            key = f"{entity.get('kind')}:{entity.get('key') or entity.get('naturalKey')}"
            out[key] = {k: v for k, v in entity.items() if k not in ("kind",)}
        return out

    def _plan(self, snapshot: Mapping[str, Any], workspace_id: str) -> dict[str, Any]:
        current = self.state.get(self._slot(snapshot, workspace_id), {})
        wanted = self._entities(snapshot)
        opened = [k for k in wanted if k not in current]
        closed = [k for k in current if k not in wanted]
        changed = [k for k in wanted if k in current and current[k] != wanted[k]]
        unchanged = len(wanted) - len(opened) - len(changed)
        return {
            "opened": len(opened),
            "changed": len(changed),
            "closed": len(closed),
            "unchanged": unchanged,
            "changes": (
                [{"op": "open", "key": k} for k in opened]
                + [{"op": "change", "key": k} for k in changed]
                + [{"op": "close", "key": k} for k in closed]
            ),
            "conflicts": [],
            "stateToken": _fingerprint(current),
        }

    async def preview(self, snapshot: Mapping[str, Any], *, workspace_id: str) -> dict[str, Any]:
        return self._plan(snapshot, workspace_id)

    async def apply(
        self,
        snapshot: Mapping[str, Any],
        *,
        workspace_id: str,
        expected_state: str | None = None,
    ) -> dict[str, Any]:
        plan = self._plan(snapshot, workspace_id)
        if expected_state is not None and expected_state != plan["stateToken"]:
            raise SnapshotStale(state_token=plan["stateToken"])
        self.state[self._slot(snapshot, workspace_id)] = self._entities(snapshot)
        self.applied.append(dict(snapshot))
        return {k: plan[k] for k in ("opened", "changed", "closed", "unchanged")}

    async def document(
        self, *, workspace_id: str, natural_key: str, **fields: Any
    ) -> dict[str, Any]:
        self.documents.append({"workspaceId": workspace_id, "naturalKey": natural_key, **fields})
        return {"naturalKey": natural_key, "status": "stored"}

    async def recall(self, **query: Any) -> dict[str, Any]:
        self.recalls.append(query)
        return self.recall_answer


class FakeCore:
    """``configure_core(lambda ctx: fake)`` — ядро для тестов скиллов."""

    def __init__(self) -> None:
        self.artifacts = FakeArtifacts()
        self.knowledge = FakeKnowledge()
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1
