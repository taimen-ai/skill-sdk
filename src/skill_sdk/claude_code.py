"""Провайдер ``ctx.llm`` через Claude Code CLI — модель по подписке, без API-ключа.

Та же поверхность, что у ``platform_llm.StructuredChatClient`` (``chat_json``,
``chat_json_object``, ``aclose``), но ответ даёт ``claude -p`` в неинтерактивном
режиме: так скилл на исполнителе с подпиской Claude (раннер с
``CLAUDE_CODE_OAUTH_TOKEN``) обходится без отдельного OpenAI-совместимого эндпоинта.

Что сделано намеренно:

* **Чистое завершение текста.** Инструменты отключены (``--tools ""``), MCP-серверов
  нет (``--strict-mcp-config`` с пустым списком), сессия не сохраняется, рабочий
  каталог — пустой временный: модель не читает файлы и не видит ``CLAUDE.md``.
* **Промпт — через stdin, не аргументом.** Аргументы видны в таблице процессов, а
  в промпте может быть текст документа. В argv — только постоянный нейтральный
  system prompt; system prompt скилла и сообщения сводятся в текст на stdin.
* **Credential — только окружение процесса.** ``CLAUDE_CODE_OAUTH_TOKEN`` CLI
  наследует от исполнителя; SDK его не читает, не пишет в файлы и не логирует.
  ``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN`` у дочернего процесса убираются:
  они выиграли бы у подписки, а LLM скиллов по решению владельца — подписка.
* **JSON из ответа.** Схема ответа — в промпте (из pydantic-модели), ответ
  разбирается и проверяется; невалидный — одна повторная попытка с указанием
  ошибки, потом следующая модель из ``SKILL_LLM_MODELS``.
* **Лимит окна подписки / 429** — ``SkillError("llm_rate_limited", retryable=True)``
  сразу, без перебора моделей: лимит общий на подписку.
* **Стоимость.** ``total_cost_usd`` CLI под подпиской — условная цена, а не
  расход, поэтому в ``cost`` уходят только токены (``cost_usd=None``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from skill_sdk.errors import SkillError

ENV_CLAUDE_BINARY = "SKILL_LLM_CLAUDE_BINARY"
ENV_TIMEOUT = "SKILL_LLM_TIMEOUT_SECONDS"
ENV_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"

DEFAULT_BINARY = "claude"
DEFAULT_TIMEOUT_SECONDS = 300.0
#: Повторов на модель при ответе не по схеме: исходный вызов плюс один повтор.
ATTEMPTS_PER_MODEL = 2
#: Сколько stderr/текста ошибки попадает в сообщение ``SkillError``.
DETAIL_LIMIT = 500
#: Переменные, которые подменили бы подписку ключом API.
_API_KEY_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

SYSTEM_PROMPT = (
    "Ты — модель структурированного вывода. Выполни инструкции из сообщения "
    "пользователя и ответь ровно одним JSON-объектом без пояснений и без markdown."
)

_RATE_LIMIT_RE = re.compile(
    r"\b429\b|rate.?limit|usage limit|limit reached|hour limit|weekly limit|"
    r"overloaded|too many requests",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class ClaudeUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ClaudeResult:
    """Форма ``platform_llm.LlmResult``/``JsonResult``: ``data``, ``model``, ``usage``."""

    data: Any
    model: str
    usage: ClaudeUsage
    cost_usd: float | None = None


class _InvalidJson(Exception):
    """Ответ модели не разобрался или не прошёл схему — повод для повтора."""


def extract_json(text: str) -> Any:
    """JSON из ответа: как есть, из блока ```json``` или от первой ``{`` до последней ``}``."""
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE_RE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise _InvalidJson("в ответе нет JSON")


def render_prompt(
    system_prompt: str,
    messages: Sequence[Mapping[str, str]],
    schema: dict[str, Any] | None,
    *,
    correction: str | None = None,
) -> str:
    """System prompt скилла, сообщения и требование к ответу — одним текстом для stdin."""
    parts = ["# Инструкции", system_prompt.strip()]
    for message in messages:
        role = str(message.get("role") or "user")
        title = {"user": "Сообщение", "assistant": "Ответ ассистента"}.get(role, role)
        parts += [f"# {title}", str(message.get("content") or "").strip()]
    parts.append("# Формат ответа")
    if schema is not None:
        parts.append(
            "Ответь одним JSON-объектом, соответствующим JSON-схеме ниже, без текста до и "
            "после него:\n" + json.dumps(schema, ensure_ascii=False)
        )
    else:
        parts.append("Ответь одним JSON-объектом без текста до и после него.")
    if correction:
        parts += [
            "# Исправление",
            f"Предыдущий ответ не принят: {correction}. Верни исправленный JSON-объект.",
        ]
    return "\n\n".join(parts)


class ClaudeCodeLlm:
    """``ctx.llm`` поверх ``claude -p --output-format json`` без инструментов."""

    def __init__(
        self,
        *,
        models: Sequence[str] = (),
        binary: str = DEFAULT_BINARY,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        env: Mapping[str, str] | None = None,
    ) -> None:
        # Пустой список — модель по умолчанию CLI (одна попытка без --model).
        self._models: tuple[str | None, ...] = tuple(models) or (None,)
        self._binary = binary
        self._timeout = timeout
        self._env = env

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, models: Sequence[str] = ()
    ) -> ClaudeCodeLlm:
        values = os.environ if environ is None else environ
        binary = values.get(ENV_CLAUDE_BINARY, "").strip() or DEFAULT_BINARY
        raw_timeout = values.get(ENV_TIMEOUT, "").strip()
        try:
            timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        except ValueError as error:
            raise SkillError(
                "llm_not_configured", f"{ENV_TIMEOUT} должен быть числом", retryable=True
            ) from error
        if shutil.which(binary, path=values.get("PATH")) is None:
            raise SkillError(
                "llm_unavailable",
                f"у хостинга нет Claude Code CLI ({binary})",
                retryable=True,
            )
        return cls(models=models, binary=binary, timeout=timeout)

    @property
    def models(self) -> tuple[str | None, ...]:
        return self._models

    async def aclose(self) -> None:
        return None

    # -- поверхность StructuredChatClient --

    async def chat_json[ModelT: BaseModel](
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        response_model: type[ModelT],
        schema_name: str,
        temperature: float = 0.0,
    ) -> ClaudeResult:
        def parse(text: str) -> ModelT:
            payload = extract_json(text)
            try:
                return response_model.model_validate(payload)
            except ValidationError as error:
                raise _InvalidJson(f"не по схеме {schema_name}: {_short(str(error))}") from error

        return await self._complete(
            system_prompt, messages, response_model.model_json_schema(), parse
        )

    async def chat_json_object(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> ClaudeResult:
        def parse(text: str) -> dict[str, Any]:
            payload = extract_json(text)
            if not isinstance(payload, dict):
                raise _InvalidJson("ответ не JSON-объект")
            return payload

        return await self._complete(system_prompt, messages, None, parse)

    # -- исполнение --

    async def _complete(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None,
        parse: Callable[[str], Any],
    ) -> ClaudeResult:
        last_error = ""
        prompt_tokens = completion_tokens = 0
        for model in self._models:
            correction: str | None = None
            for _attempt in range(ATTEMPTS_PER_MODEL):
                prompt = render_prompt(system_prompt, messages, schema, correction=correction)
                reply = await self._run(prompt, model)
                raw_usage = reply.get("usage")
                usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
                prompt_tokens += _prompt_tokens(usage)
                completion_tokens += _int(usage.get("output_tokens"))
                text = reply.get("result")
                try:
                    data = parse(text if isinstance(text, str) else "")
                except _InvalidJson as error:
                    correction = str(error)
                    last_error = f"{model or 'default'}: {error}"
                    continue
                return ClaudeResult(
                    data=data,
                    model=_model_of(reply, model),
                    usage=ClaudeUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens,
                    ),
                )
        raise SkillError(
            "llm_invalid_response",
            f"модель не вернула JSON по схеме ({last_error})"[:DETAIL_LIMIT],
            retryable=True,
        )

    def command(self, model: str | None) -> list[str]:
        """argv вызова — без промпта и без секретов (промпт идёт через stdin)."""
        args = [
            self._binary,
            "--print",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--system-prompt",
            SYSTEM_PROMPT,
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            "",
        ]
        if model:
            args += ["--model", model]
        return args

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ if self._env is None else self._env)
        for name in _API_KEY_VARIABLES:
            env.pop(name, None)
        return env

    async def _run(self, prompt: str, model: str | None) -> dict[str, Any]:
        env = self._child_env()
        with tempfile.TemporaryDirectory(prefix="skill-llm-") as workdir:
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.command(model),
                    cwd=workdir,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as error:
                raise SkillError(
                    "llm_unavailable",
                    f"Claude Code CLI не запустился: {type(error).__name__}",
                    retryable=True,
                ) from error
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode()), timeout=self._timeout
                )
            except TimeoutError as error:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
                raise SkillError(
                    "llm_timeout",
                    f"Claude Code не ответил за {self._timeout:g} с",
                    retryable=True,
                ) from error
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                raise
        return self._reply(stdout, stderr, process.returncode or 0, env)

    def _reply(
        self, stdout: bytes, stderr: bytes, exit_code: int, env: Mapping[str, str]
    ) -> dict[str, Any]:
        reply: dict[str, Any] | None = None
        with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
            parsed = json.loads(stdout.decode())
            if isinstance(parsed, dict):
                reply = parsed
        detail = ""
        if reply is not None and isinstance(reply.get("result"), str):
            detail = reply["result"]
        if not detail:
            detail = stderr.decode(errors="replace").strip()
        detail = _redact(detail, env)[:DETAIL_LIMIT]
        failed = reply is None or bool(reply.get("is_error")) or exit_code != 0
        if not failed and reply is not None:
            return reply
        status = reply.get("api_error_status") if reply is not None else None
        if status == 429 or _RATE_LIMIT_RE.search(detail):
            raise SkillError(
                "llm_rate_limited",
                f"лимит подписки Claude: {detail}" if detail else "лимит подписки Claude",
                retryable=True,
            )
        raise SkillError(
            "llm_failed",
            f"Claude Code завершился с ошибкой (код {exit_code}): {detail}".rstrip(": "),
            retryable=True,
        )


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _prompt_tokens(usage: Mapping[str, Any]) -> int:
    return (
        _int(usage.get("input_tokens"))
        + _int(usage.get("cache_creation_input_tokens"))
        + _int(usage.get("cache_read_input_tokens"))
    )


def _model_of(reply: Mapping[str, Any], requested: str | None) -> str:
    used = reply.get("modelUsage")
    if isinstance(used, dict) and used:
        return str(next(iter(used)))
    return requested or "claude-code"


def _short(text: str) -> str:
    return text if len(text) <= DETAIL_LIMIT else text[: DETAIL_LIMIT - 1] + "…"


def _redact(text: str, env: Mapping[str, str]) -> str:
    """Токен подписки не должен попасть в сообщение ошибки, даже если CLI его напечатал."""
    token = env.get(ENV_TOKEN)
    return text.replace(token, "***") if token else text
