"""Проверка скилла в тестах: тот же путь, что у хостинга, без исполнителя и ядра."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jsonschema

from skill_sdk import context as _context
from skill_sdk.connections import (
    ConnectionClient,
    ConnectionInfo,
    ConnectionNotFound,
    Material,
    MaterialRefused,
    SecretStoreUnavailable,
)
from skill_sdk.context import Invocation
from skill_sdk.core import QUERY_MAX_ITEMS, QUERY_PAGE, ArtifactContent, SnapshotStale, _too_many
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
    llm: Any = None,
    settings: Mapping[str, Any] | None = None,
) -> Result:
    invocation = Invocation(
        skill=skill.ref,
        protocol="test",
        invocation_id="test",
        idempotency_key=idempotency_key,
        timeout_seconds=skill.timeout,
        settings=_context.package_settings({"values": settings}),
    )
    if llm is None:
        outputs, cost = await skill.execute(inputs, invocation, env=env)
        return Result(outputs, cost)
    # Подмена — переменная контекста, а не глобальная фабрика: параллельные вызовы
    # (asyncio.gather) видят каждый свою подделку, глобальная настройка не трогается.
    token = _context._llm_override.set(lambda: llm)
    try:
        outputs, cost = await skill.execute(inputs, invocation, env=env)
    finally:
        _context._llm_override.reset(token)
    return Result(outputs, cost)


def invoke(
    skill: Skill,
    inputs: Any,
    *,
    env: Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
    llm: Any = None,
    settings: Mapping[str, Any] | None = None,
) -> Result:
    """Вызвать скилл с проверкой входа и выхода по контракту; ``SkillError`` пробрасывается.

    ``llm`` — клиент для ``ctx.llm`` на время вызова (обычно ``FakeLlm``); без него —
    тот, что задан ``configure_llm`` или окружением. Подмена живёт в переменной
    контекста: её видят задачи asyncio и ``asyncio.to_thread`` этого вызова, но не
    потоки, запущенные вручную (``threading.Thread``, ``ThreadPoolExecutor.submit``) —
    там действует настройка инсталляции.

    ``settings`` — значения настроек пакета для ``ctx.settings`` (``values`` контекста
    вызова); без них — скилл не из пакета, ``ctx.settings`` пуст."""
    return asyncio.run(
        ainvoke(skill, inputs, env=env, idempotency_key=idempotency_key, llm=llm, settings=settings)
    )


# --- подделка LLM для ctx.llm ---


@dataclass(frozen=True)
class FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    total_tokens: int = 15


@dataclass(frozen=True)
class FakeResult:
    """Та же форма, что у ``platform_llm.LlmResult`` и ``JsonResult``."""

    data: Any
    model: str
    usage: FakeUsage
    cost_usd: float | None


@dataclass(frozen=True)
class LlmCall:
    """Один запрос скилла к модели — уже после стража персональных данных."""

    mode: str  # "json" (chat_json) | "object" (chat_json_object)
    system_prompt: str
    messages: list[dict[str, str]]
    schema_name: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None

    @property
    def prompt(self) -> str:
        """Текст последнего сообщения — обычно то, что скилл спрашивает."""
        return self.messages[-1]["content"] if self.messages else ""


Answer = Mapping[str, Any] | Callable[[LlmCall], Mapping[str, Any]]


@dataclass
class FakeLlm:
    """Подделка LLM-клиента инсталляции для тестов скилла.

    ``answers`` — один ответ на все вызовы (словарь), ответы по порядку вызовов
    (список) или функция от ``LlmCall``. Ответ ``chat_json`` проверяется моделью
    ``response_model`` — несовместимый ответ падает так же, как у настоящего клиента.
    Каждый вызов учитывается в ``calls`` и в cost вызова скилла (``usage``,
    ``cost_usd``, ``model``). Исчерпанный список — ``AssertionError``: тест ждал
    меньше вызовов, чем сделал скилл."""

    answers: Answer | Sequence[Answer]
    model: str = "fake-model"
    usage: FakeUsage = field(default_factory=FakeUsage)
    cost_usd: float | None = None
    calls: list[LlmCall] = field(default_factory=list)

    def _answer(self, call: LlmCall) -> dict[str, Any]:
        self.calls.append(call)
        answer: Any = self.answers
        if isinstance(answer, Sequence) and not isinstance(answer, (str, bytes)):
            index = len(self.calls) - 1
            if index >= len(answer):
                raise AssertionError(f"FakeLlm: вызов №{index + 1}, а ответов задано {len(answer)}")
            answer = answer[index]
        if callable(answer):
            answer = answer(call)
        return copy.deepcopy(dict(answer))

    def _result(self, data: Any) -> FakeResult:
        return FakeResult(data, self.model, self.usage, self.cost_usd)

    async def chat_json(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        response_model: Any,
        schema_name: str,
        temperature: float = 0.0,
    ) -> FakeResult:
        call = LlmCall("json", system_prompt, list(messages), schema_name, temperature)
        return self._result(response_model.model_validate(self._answer(call)))

    async def chat_json_object(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> FakeResult:
        call = LlmCall(
            "object", system_prompt, list(messages), temperature=temperature, max_tokens=max_tokens
        )
        return self._result(self._answer(call))

    async def aclose(self) -> None:
        return None


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


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _scalar_matches(actual: Any, op: str, value: Any) -> bool:
    if op == "eq":
        if _number(value):
            return _number(actual) and actual == value
        if isinstance(value, bool):
            return actual is value
        return isinstance(actual, str) and actual == value
    if op == "in":
        return any(_scalar_matches(actual, "eq", v) for v in value)
    if op == "prefix":
        return isinstance(actual, str) and (actual == value or actual.startswith(value + "."))
    if _number(value):
        left, right = (actual if _number(actual) else None), value
    else:
        left, right = _date(actual), _date(value)
    if left is None or right is None:
        return False
    return bool(left <= right if op == "lte" else left >= right)


def _where_matches(attributes: Mapping[str, Any], condition: Mapping[str, Any]) -> bool:
    """Условие ``where`` так, как его выполняет память (``context/where.py``)."""
    op, value = condition["op"], condition.get("value")
    actual = attributes.get(condition["attr"])
    if op == "exists":
        return (actual is not None and actual != []) is (True if value is None else value)
    if actual is None:
        return False
    if isinstance(actual, list):
        return any(_scalar_matches(item, op, value) for item in actual)
    return _scalar_matches(actual, op, value)


class FakeKnowledge:
    """Сверка снимка источника по естественным ключам, как её видит скилл.

    Состояние — открытые сущности пары ``(workspace, pack, source, scope)``;
    ``stateToken`` — отпечаток этого состояния. ``apply`` с устаревшим
    ``expectedState`` бросает ``SnapshotStale``. Ответ ``recall`` задаёт тест
    (``recall_answer``), запросы копятся в ``recalls``. ``query`` перечисляет
    сущности применённых снимков workspace с той же семантикой ``where``, что у
    памяти (MEM-ADR-020); ``as_of`` подделка не различает — состояние одно, текущее;
    запросы копятся в ``queries``."""

    def __init__(self) -> None:
        self.state: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.applied: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.recalls: list[dict[str, Any]] = []
        self.recall_answer: dict[str, Any] = {"sections": []}
        self.queries: list[dict[str, Any]] = []

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
            ident = f"{entity.get('kind')}\x1f{entity.get('key') or entity.get('naturalKey')}"
            out[ident] = {k: v for k, v in entity.items() if k not in ("kind",)}
        return out

    def _plan(self, snapshot: Mapping[str, Any], workspace_id: str) -> dict[str, Any]:
        """Ответ сверки в форме памяти (MEM-ADR-020): изменившийся элемент считается и
        закрытым, и открытым (``superseded``); ``changes`` — ключи ``{kind, key}``."""
        current = self.state.get(self._slot(snapshot, workspace_id), {})
        wanted = self._entities(snapshot)
        opened = [k for k in wanted if k not in current]
        closed = [k for k in current if k not in wanted]
        changed = [k for k in wanted if k in current and current[k] != wanted[k]]
        unchanged = len(wanted) - len(opened) - len(changed)

        def refs(idents: list[str]) -> list[dict[str, str]]:
            return [dict(zip(("kind", "key"), i.split("\x1f", 1), strict=True)) for i in idents]

        return {
            "opened": len(opened) + len(changed),
            "closed": len(closed) + len(changed),
            "superseded": len(changed),
            "unchanged": unchanged,
            "changes": {
                "opened": refs(opened),
                "changed": refs(changed),
                "closed": refs(closed),
                "limit": 1000,
                "truncated": False,
            },
            "conflicts": {"items": [], "limit": 1000, "truncated": False},
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
        return {**plan, "stateToken": _fingerprint(self.state[self._slot(snapshot, workspace_id)])}

    async def document(
        self, *, workspace_id: str, natural_key: str, **fields: Any
    ) -> dict[str, Any]:
        self.documents.append({"workspaceId": workspace_id, "naturalKey": natural_key, **fields})
        return {"naturalKey": natural_key, "status": "stored"}

    async def recall(self, **query: Any) -> dict[str, Any]:
        self.recalls.append(query)
        return self.recall_answer

    async def query(
        self,
        *,
        workspace_id: str,
        kinds: list[str],
        where: list[Mapping[str, Any]] | None = None,
        as_of: str | None = None,
        limit: int = QUERY_PAGE,
        max_items: int = QUERY_MAX_ITEMS,
    ) -> list[dict[str, Any]]:
        self.queries.append(
            {
                "workspaceId": workspace_id,
                "kinds": list(kinds),
                "where": list(where or []),
                "asOf": as_of,
            }
        )
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for slot, entities in self.state.items():
            if slot[0] != workspace_id:
                continue
            for ident, entity in entities.items():
                kind, key = ident.split("\x1f", 1)
                attributes = entity.get("attributes") or {}
                if kind in kinds and all(_where_matches(attributes, c) for c in where or []):
                    found[(kind, key)] = {
                        "kind": kind,
                        "key": key,
                        "title": entity.get("title") or "",
                        "attributes": dict(attributes),
                        "source": slot[2],
                        "scope": slot[3],
                    }
        items = [found[k] for k in sorted(found)]
        if len(items) > max_items:
            raise _too_many(max_items)
        return items


class FakeCore:
    """``configure_core(lambda ctx: fake)`` — ядро для тестов скиллов."""

    def __init__(self) -> None:
        self.artifacts = FakeArtifacts()
        self.knowledge = FakeKnowledge()
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


# --- подделка подключений для ctx.connection и ConnectionClient (TAI-ADR-0061) ---


class FakeConnectionDirectory:
    """Сведения ядра о подключениях агента (``GET /agents/me/connections/{key}``)."""

    def __init__(self) -> None:
        self.infos: dict[str, ConnectionInfo] = {}
        self.lookups: list[str] = []
        self.unavailable = False
        self.closed = 0

    async def connection(self, key: str) -> ConnectionInfo:
        self.lookups.append(key)
        if self.unavailable:
            raise SkillError("core_unavailable", "ядро недоступно", retryable=True)
        if key not in self.infos:
            raise ConnectionNotFound(key)
        return self.infos[key]

    async def aclose(self) -> None:
        self.closed += 1


class FakeSecretStore:
    """Хранилище в памяти, которое ведёт себя как настоящее: материал выдаётся только
    по пути из политики агента (``granted``), нет политики — отказ ``403``, нет
    материала — ``404``, запечатанное — повторяемый ``secret_store_unavailable``."""

    def __init__(self) -> None:
        self.materials: dict[str, Material] = {}
        self.granted: set[str] = set()
        self.refusals: dict[str, int] = {}
        self.reads: list[str] = []
        self.sealed = False
        self.closed = 0

    async def read(self, secret_ref: str) -> Material:
        self.reads.append(secret_ref)
        if self.sealed:
            raise SecretStoreUnavailable(
                "хранилище секретов запечатано или не готово", reason="sealed_or_standby"
            )
        if secret_ref in self.refusals:
            raise MaterialRefused(self.refusals[secret_ref])
        if secret_ref not in self.granted:
            raise MaterialRefused(403)
        if secret_ref not in self.materials:
            raise MaterialRefused(404)
        return self.materials[secret_ref]

    async def aclose(self) -> None:
        self.closed += 1


class FakeConnections:
    """Подключения агента для тестов: сведения ядра, хранилище и часы в одном месте.

    ``add`` заводит активное подключение с материалом, ``rotate`` меняет токен (как
    обновление плагином), ``revoke``/``expire`` — как отзыв и потеря доступа в ядре
    (материал и политика уходят из хранилища), ``seal``/``unseal`` — запечатанное
    хранилище, ``advance`` двигает часы клиента. Клиент — настоящий
    ``ConnectionClient``, поэтому кэш и ошибки те же, что у хостинга::

        fake = FakeConnections()
        fake.add("crm", type="crm-x", account="example.test", token="t-1")
        configure_connections(lambda ctx: fake.client())
    """

    def __init__(self, *, tenant: str = "tenant-1") -> None:
        self.tenant = tenant
        self.directory = FakeConnectionDirectory()
        self.store = FakeSecretStore()
        self.monotonic = 0.0
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.monotonic += seconds
        self.now += timedelta(seconds=seconds)

    def _ref(self, key: str, auth: str) -> str:
        root = "oauth2/creds" if auth == "oauth2" else "kv/data"
        return f"{root}/tenants/{self.tenant}/connections/{key}"

    def add(
        self,
        key: str,
        *,
        type: str,
        account: str | None = None,
        settings: Mapping[str, Any] | None = None,
        token: str = "fake-access-token",
        auth: str = "token",
        expires_at: datetime | None = None,
        type_version: int = 1,
    ) -> ConnectionInfo:
        ref = self._ref(key, auth)
        info = ConnectionInfo(
            key=key,
            type=type,
            status="active",
            type_version=type_version,
            account=account,
            auth=auth,
            settings=dict(settings or {}),
            secret_ref=ref,
            expires_at=expires_at if auth == "token" else None,
        )
        self.directory.infos[key] = info
        self.store.materials[ref] = Material(access_token=token, expires_at=expires_at)
        self.store.granted.add(ref)
        return info

    def pending(self, key: str, *, type: str) -> ConnectionInfo:
        info = ConnectionInfo(key=key, type=type, status="pending")
        self.directory.infos[key] = info
        return info

    def rotate(self, key: str, token: str, *, expires_at: datetime | None = None) -> None:
        ref = self.directory.infos[key].secret_ref
        assert ref is not None
        self.store.materials[ref] = Material(access_token=token, expires_at=expires_at)

    def _withdraw(self, key: str, status: str, *, keep_ref: bool) -> None:
        info = self.directory.infos[key]
        if info.secret_ref is not None:
            self.store.granted.discard(info.secret_ref)
            if not keep_ref:
                self.store.materials.pop(info.secret_ref, None)
        self.directory.infos[key] = ConnectionInfo(
            key=info.key,
            type=info.type,
            status=status,
            type_version=info.type_version,
            account=info.account,
            auth=info.auth if keep_ref else None,
            settings=info.settings,
            secret_ref=info.secret_ref if keep_ref else None,
            expires_at=info.expires_at,
        )

    def revoke(self, key: str) -> None:
        """Отзыв (CP-ADR-0079 п.10): материал удалён, политика снята, учёт — ``revoked``."""
        self._withdraw(key, "revoked", keep_ref=False)

    def expire(self, key: str) -> None:
        """Потеря доступа: учёт — ``expired``, в политику агента подключение не входит."""
        self._withdraw(key, "expired", keep_ref=True)

    def seal(self) -> None:
        self.store.sealed = True

    def unseal(self) -> None:
        self.store.sealed = False

    def client(self, *, cache_seconds: float = 60.0) -> ConnectionClient:
        return ConnectionClient(
            self.directory,
            self.store,
            cache_seconds=cache_seconds,
            clock=lambda: self.monotonic,
            now=lambda: self.now,
        )
