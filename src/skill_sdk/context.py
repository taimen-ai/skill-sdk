"""Контекст одного вызова скилла (TAI-ADR-0045 п.4).

Что скилл вправе знать и делать внутри вызова: кто и какой вызов, сколько осталось
времени, куда писать журнал, во что обошёлся вызов, параметры и секреты
инсталляции, LLM-клиент по конфигурации. Клиента Control Plane в контексте нет
намеренно: скилл не заводит и не двигает задачи — это исходы approval и правила
(TAI-ADR-0041), а результат скилла ядро само кладёт куда надо. Есть только узкий
доступ к ядру (TAI-ADR-0056 Р5): ``ctx.artifacts`` — содержимое артефактов,
``ctx.knowledge`` — предпросмотр и применение снимка, документы и обход базы
знаний (``skill_sdk.core``).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from skill_sdk.core import Artifacts, Core, Knowledge, core_for
from skill_sdk.errors import SkillError
from skill_sdk.pii import redact_messages

logger = logging.getLogger("skill_sdk")

ENV_LLM_BASE_URL = "SKILL_LLM_BASE_URL"
ENV_LLM_API_KEY = "SKILL_LLM_API_KEY"
ENV_LLM_MODELS = "SKILL_LLM_MODELS"
ENV_LLM_PROVIDER = "SKILL_LLM_PROVIDER"

PROVIDER_OPENAI = "openai"
PROVIDER_CLAUDE_CODE = "claude-code"
PROVIDERS = (PROVIDER_OPENAI, PROVIDER_CLAUDE_CODE)

LlmFactory = Callable[[], Any]
_llm_factory: LlmFactory | None = None


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
        """Секрет хостинга по имени; его нет — повторяемый сбой (другой хост может его иметь)."""
        value = self._env.get(name)
        if not value:
            raise SkillError("config_missing", f"у хостинга не задан {name}", retryable=True)
        return value

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
            factory = _llm_factory or _default_llm
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
        """База знаний через ядро: ``preview``, ``apply``, ``document``, ``recall``."""
        return self._core_access().knowledge

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
