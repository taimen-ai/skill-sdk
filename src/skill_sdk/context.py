"""Контекст одного вызова скилла (TAI-ADR-0045 п.4).

Что скилл вправе знать и делать внутри вызова: кто и какой вызов, сколько осталось
времени, куда писать журнал, во что обошёлся вызов, параметры и секреты
инсталляции, настройки пакета скилла (CP-ADR-0081), LLM-клиент по конфигурации.
Клиента Control Plane в контексте нет намеренно: скилл не заводит и не двигает
задачи — это исходы approval и правила (TAI-ADR-0041), а результат скилла ядро
само кладёт куда надо. Есть только узкий
доступ к ядру (TAI-ADR-0056 Р5): ``ctx.artifacts`` — содержимое артефактов,
``ctx.knowledge`` — предпросмотр и применение снимка, документы, обход и выборка базы
знаний (``skill_sdk.core``). Подключения к внешним системам — ``ctx.connection(key)``:
сведения из ядра, материал доступа из хранилища секретов (``skill_sdk.connections``).
"""

from __future__ import annotations

import contextvars
import copy
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from skill_sdk import secrets
from skill_sdk.connections import Connection, ConnectionClient, connections_for
from skill_sdk.core import Artifacts, Core, Knowledge, core_for
from skill_sdk.errors import SkillError
from skill_sdk.pii import redact_messages

logger = logging.getLogger("skill_sdk")

ENV_LLM_BASE_URL = "SKILL_LLM_BASE_URL"
ENV_LLM_API_KEY = "SKILL_LLM_API_KEY"
ENV_LLM_MODELS = "SKILL_LLM_MODELS"
ENV_LLM_PROVIDER = "SKILL_LLM_PROVIDER"

# Секреты узла fleet (TAI-ADR-0052): канон чтения — ``skill_sdk.secrets``; имена
# оставлены здесь ради прежних импортов.
ENV_SECRETS_DIR = secrets.ENV_SECRETS_DIR
DEFAULT_SECRETS_DIR = secrets.DEFAULT_SECRETS_DIR
SECRET_FILE_NAME = secrets.SECRET_FILE_NAME
RESERVED_SECRET_NAMES = secrets.RESERVED_SECRET_NAMES
MAX_SECRET_FILE_BYTES = secrets.MAX_SECRET_FILE_BYTES

PROVIDER_OPENAI = "openai"
PROVIDER_CLAUDE_CODE = "claude-code"
PROVIDERS = (PROVIDER_OPENAI, PROVIDER_CLAUDE_CODE)

LlmFactory = Callable[[], Any]
_llm_factory: LlmFactory | None = None
# Подмена на время одного вызова (``skill_sdk.testing.invoke(llm=…)``): у каждой задачи
# asyncio своя копия контекста, поэтому параллельные вызовы не видят чужую подделку.
_llm_override: contextvars.ContextVar[LlmFactory | None] = contextvars.ContextVar(
    "skill_sdk_llm_override", default=None
)


def configure_llm(factory: LlmFactory | None) -> None:
    """Задать, откуда хостинг берёт LLM-клиент (провайдер — настройка инсталляции).

    По умолчанию провайдер выбирает ``SKILL_LLM_PROVIDER``: ``openai`` (так и без
    переменной) — ``platform_llm.OpenAICompatibleClient`` из ``SKILL_LLM_BASE_URL``,
    ``SKILL_LLM_API_KEY``, ``SKILL_LLM_MODELS``; ``claude-code`` — Claude по подписке
    через ``claude -p`` (``skill_sdk.claude_code``), модели из ``SKILL_LLM_MODELS``."""
    global _llm_factory
    _llm_factory = factory


def _models() -> tuple[str, ...]:
    return tuple(m.strip() for m in os.environ.get(ENV_LLM_MODELS, "").split(",") if m.strip())


def _default_llm() -> Any:
    provider = os.environ.get(ENV_LLM_PROVIDER, "").strip().lower() or PROVIDER_OPENAI
    if provider not in PROVIDERS:
        # Другой исполнитель может быть настроен верно: вызов повторяемый.
        raise SkillError(
            "llm_not_configured",
            f"{ENV_LLM_PROVIDER}={provider!r}: ожидается одно из {', '.join(PROVIDERS)}",
            retryable=True,
        )
    if provider == PROVIDER_CLAUDE_CODE:
        from skill_sdk.claude_code import ClaudeCodeLlm

        return ClaudeCodeLlm.from_environment(models=_models())
    return _openai_llm()


def _openai_llm() -> Any:
    try:
        from platform_llm import OpenAICompatibleClient
    except ImportError as error:
        raise SkillError(
            "llm_unavailable",
            "platform-llm не установлен у хостинга (skill-sdk[llm])",
            retryable=True,
        ) from error
    base_url, api_key = os.environ.get(ENV_LLM_BASE_URL), os.environ.get(ENV_LLM_API_KEY)
    models = _models()
    if not base_url or not api_key or not models:
        # Другой исполнитель может быть настроен: вызов повторяемый.
        raise SkillError(
            "llm_not_configured",
            f"у хостинга не заданы {ENV_LLM_BASE_URL}, {ENV_LLM_API_KEY}, {ENV_LLM_MODELS}",
            retryable=True,
        )
    return OpenAICompatibleClient(base_url=base_url, api_key=api_key, models=models)


_NO_SETTINGS: Mapping[str, Any] = MappingProxyType({})


def package_settings(raw: Any) -> Mapping[str, Any]:
    """Значения настроек пакета из ``settings`` контекста вызова (CP-ADR-0081 В3).

    Ядро отдаёт ``{package, version, schemaRevision, values}`` — ``values`` действующие
    значения (сохранённые поверх ``default`` схемы) — или ``null``, если скилл не из
    пакета или пакет настроек не объявляет. Не объект, нет ``values`` или ``values`` не
    объект — настроек нет: пустое отображение. Копия только для чтения, своя на вызов."""
    values = raw.get("values") if isinstance(raw, Mapping) else None
    if not isinstance(values, Mapping) or not values:
        return _NO_SETTINGS
    return MappingProxyType(copy.deepcopy(dict(values)))


@dataclass(frozen=True)
class Invocation:
    """Метаданные вызова, которые передаёт хостинг."""

    skill: str
    protocol: str
    invocation_id: str | None = None
    idempotency_key: str | None = None
    timeout_seconds: float | None = None
    # Проверенный контекст вызывающего (http/mcp с IAM) — TrustedAuthContext.
    caller: Any = None
    # Значения настроек пакета скилла — ``package_settings`` от того, что прислало ядро.
    settings: Mapping[str, Any] = _NO_SETTINGS


@dataclass
class _LlmUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    priced: bool = False
    models: list[str] = field(default_factory=list)


class _MeteredLlm:
    """Тот же ``StructuredChatClient``, но каждый ответ учитывается в cost вызова, а
    промпт проходит страж персональных данных (``skill_sdk.pii``, TAI-ADR-0056 Р13)."""

    def __init__(self, inner: Any, usage: _LlmUsage, log: Any = None) -> None:
        self._inner = inner
        self._usage = usage
        self._log = log or logger

    def _guard(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        system_prompt = kwargs.get("system_prompt")
        messages = kwargs.get("messages")
        if not isinstance(system_prompt, str) and not isinstance(messages, list):
            return kwargs
        system, clean, found = redact_messages(
            system_prompt if isinstance(system_prompt, str) else "",
            messages if isinstance(messages, list) else [],
        )
        if found:
            # только виды и счёт — значения в журнал не попадают
            self._log.warning("персональные данные заменены в промпте LLM: %s", found.as_log())
        guarded = dict(kwargs)
        if isinstance(system_prompt, str):
            guarded["system_prompt"] = system
        if isinstance(messages, list):
            guarded["messages"] = clean
        return guarded

    def _record(self, result: Any) -> Any:
        usage = self._usage
        usage.calls += 1
        tokens = getattr(result, "usage", None)
        if tokens is not None:
            usage.prompt_tokens += int(getattr(tokens, "prompt_tokens", 0) or 0)
            usage.completion_tokens += int(getattr(tokens, "completion_tokens", 0) or 0)
            usage.total_tokens += int(getattr(tokens, "total_tokens", 0) or 0)
        cost = getattr(result, "cost_usd", None)
        if cost is not None:
            usage.cost_usd += float(cost)
            usage.priced = True
        model = getattr(result, "model", None)
        if model and model not in usage.models:
            usage.models.append(str(model))
        return result

    async def chat_json(self, **kwargs: Any) -> Any:
        return self._record(await self._inner.chat_json(**self._guard(kwargs)))

    async def chat_json_object(self, **kwargs: Any) -> Any:
        return self._record(await self._inner.chat_json_object(**self._guard(kwargs)))

    async def aclose(self) -> None:
        await self._inner.aclose()


class SkillContext:
    """Передаётся функции скилла вторым аргументом, если она его принимает."""

    def __init__(self, invocation: Invocation, *, env: Mapping[str, str] | None = None) -> None:
        self.invocation = invocation
        self._env = env if env is not None else os.environ
        self._started = time.monotonic()
        self._units: dict[str, float] = {}
        self._llm_usage = _LlmUsage()
        self._llm: _MeteredLlm | None = None
        self._core: Core | None = None
        self._connections: ConnectionClient | None = None
        self.log = logging.LoggerAdapter(
            logger, {"skill": invocation.skill, "invocation_id": invocation.invocation_id}
        )

    # -- кто и какой вызов --

    @property
    def skill(self) -> str:
        return self.invocation.skill

    @property
    def invocation_id(self) -> str | None:
        return self.invocation.invocation_id

    @property
    def idempotency_key(self) -> str | None:
        """Ключ идемпотентности вызова: повтор с тем же ключом — тот же внешний эффект."""
        return self.invocation.idempotency_key

    @property
    def caller(self) -> Any:
        return self.invocation.caller

    @property
    def settings(self) -> Mapping[str, Any]:
        """Действующие значения настроек пакета скилла (CP-ADR-0081): сохранённые
        администратором поверх ``default`` схемы, только для чтения. Скилл не из пакета,
        пакет без настроек или ядро их не передало — пустое отображение."""
        return self.invocation.settings

    # -- время --

    def remaining(self) -> float | None:
        """Секунд до таймаута контракта; ``None`` — хостинг его не знает."""
        if self.invocation.timeout_seconds is None:
            return None
        return self.invocation.timeout_seconds - (time.monotonic() - self._started)

    def check_deadline(self) -> None:
        """Бросить повторяемый ``timeout``, если время вызова вышло."""
        left = self.remaining()
        if left is not None and left <= 0:
            raise SkillError("timeout", f"{self.skill}: время вызова вышло", retryable=True)

    # -- параметры и секреты инсталляции --

    def config(self, name: str, default: str | None = None) -> str | None:
        return self._env.get(name, default)

    def secret(self, name: str) -> str:
        """Секрет хостинга по имени: сначала окружение, затем файл секрета узла.

        1. Переменная окружения ``name`` — так секреты получает хостинг вне fleet.
        2. Файл ``$SKILL_SDK_SECRETS_DIR/<name>`` (по умолчанию ``/run/secrets``) — так
           узел fleet передаёт ``placement.secrets`` описания агента. Имя файла равно
           имени секрета, перевода нет: файл ищется, только если ``name`` подходит под
           шаблон имён секретов узла ``[a-z0-9][a-z0-9-]{0,62}``. Файл из одних
           пробельных символов (пустой после ``strip()``) — секрета нет; у непустого
           значения обрезаются только хвостовые ``\r`` и ``\n``, пробелы — его часть.

        Нет ни там, ни там — повторяемый ``config_missing`` (другой хост может его
        иметь), в сообщении оба места поиска, значений нет. Имя с ``/``, ``..``,
        абсолютный путь и зарезервированное ``agent-pat`` (PAT агента, который узел
        кладёт рядом) — ``secret_name_invalid``. Файл, который ссылкой уводит за
        пределы каталога секретов или подменён во время чтения (на каждой из трёх
        попыток), не обычный файл (каталог, FIFO), больше 64 КиБ или не UTF-8 —
        ``secret_file_rejected``. Файл без прав на чтение — повторяемый
        ``secret_unreadable``. Правило — :func:`skill_sdk.secrets.read_secret`."""
        return secrets.read_secret(name, environ=self._env)

    # -- стоимость --

    def add_cost(self, unit: str, amount: float) -> None:
        """Учесть потребление в своих единицах (запросы к API, страницы, рубли)."""
        self._units[unit] = self._units.get(unit, 0.0) + float(amount)

    @property
    def llm(self) -> Any:
        """LLM-клиент инсталляции (``platform_llm.StructuredChatClient``) с учётом токенов.

        Персональные данные в ``system_prompt`` и ``messages`` заменяются маркерами
        до вызова модели (``skill_sdk.pii``)."""
        if self._llm is None:
            factory = _llm_override.get() or _llm_factory or _default_llm
            self._llm = _MeteredLlm(factory(), self._llm_usage, self.log)
        return self._llm

    # -- ядро: артефакты и база знаний --

    def _core_access(self) -> Core:
        if self._core is None:
            self._core = core_for(self)
        return self._core

    @property
    def artifacts(self) -> Artifacts:
        """Содержимое артефактов через ядро учётной записью исполнителя скиллов."""
        return self._core_access().artifacts

    @property
    def knowledge(self) -> Knowledge:
        """База знаний через ядро: ``preview``, ``apply``, ``document``, ``recall``, ``query``."""
        return self._core_access().knowledge

    # -- подключения к внешним системам --

    async def connection(self, key: str) -> Connection:
        """Подключение по ключу из ``spec.connections`` агента: ``type``, ``account``,
        ``settings`` и ``await access_token()`` (кэш не дольше 60 с). Ошибки —
        ``connection_revoked``, ``connection_expired``, ``connection_not_found``,
        ``connection_pending`` и повторяемый ``secret_store_unavailable``."""
        if self._connections is None:
            self._connections = connections_for(self, self._env)
        return await self._connections.connection(key)

    def cost(self) -> dict[str, Any] | None:
        """Документ ``cost`` для ``:complete`` — ``None``, если учитывать нечего."""
        document: dict[str, Any] = {}
        usage = self._llm_usage
        if usage.calls:
            llm: dict[str, Any] = {
                "calls": usage.calls,
                "promptTokens": usage.prompt_tokens,
                "completionTokens": usage.completion_tokens,
                "totalTokens": usage.total_tokens,
                "models": usage.models,
            }
            if usage.priced:
                llm["costUsd"] = round(usage.cost_usd, 6)
            document["llm"] = llm
        if self._units:
            document["units"] = dict(self._units)
        return document or None

    async def aclose(self) -> None:
        if self._llm is not None:
            await self._llm.aclose()
        if self._core is not None:
            core, self._core = self._core, None
            await core.aclose()
        if self._connections is not None:
            connections, self._connections = self._connections, None
            await connections.aclose()
