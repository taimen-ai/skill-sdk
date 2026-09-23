"""Декларация скилла в коде и его контракт v1 (CP-ADR-0056 §1, TAI-ADR-0045 п.1–3).

Контракт выводится из кода, а не пишется вторым экземпляром: вход и выход — из
моделей pydantic в аннотациях функции (или из явной JSON Schema, если модель не
подходит), политика — из аргументов декоратора. Реализация (``implementation``)
зависит от того, как скилл хостят; по умолчанию это ``local`` с entrypoint
``модуль:имя`` самой функции, для ``http`` и ``mcp`` её задают при экспорте в пакет.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import jsonschema
from pydantic import BaseModel, ValidationError

from skill_sdk.context import Invocation, SkillContext
from skill_sdk.errors import input_violation, output_violation

JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"
SIDE_EFFECTS = ("none", "external_read", "external_write")
RISK_LEVELS = ("low", "medium", "high")
IDEMPOTENCY = ("required", "natural", "none")


# --- реализации ---------------------------------------------------------------


@dataclass(frozen=True)
class Local:
    """``module:function`` у исполнителя; ``None`` — entrypoint самой функции."""

    entrypoint: str | None = None


@dataclass(frozen=True)
class Http:
    """``POST endpoint``; ``audience`` — IAM audience, токен которой несёт исполнитель."""

    endpoint: str
    audience: str | None = None


@dataclass(frozen=True)
class Mcp:
    """Инструмент MCP-сервера: ``endpoint`` — ``http(s)://…`` или ``stdio:<имя>``."""

    endpoint: str
    tool: str | None = None
    audience: str | None = None


Implementation = Local | Http | Mcp


# --- схемы --------------------------------------------------------------------


def _model_of(annotation: Any) -> type[BaseModel] | None:
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _schema_of(model: type[BaseModel], mode: str) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True, mode=mode)  # type: ignore[arg-type]
    return {"$schema": JSON_SCHEMA_2020_12, **schema}


def schema_errors(schema: Mapping[str, Any], instance: Any) -> list[dict[str, str]]:
    """Та же проверка, что делают исполнитель и ядро (JSON Schema 2020-12)."""
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    found = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    return [
        {
            "path": "/" + "/".join(str(p) for p in error.absolute_path),
            "message": error.message[:500],
        }
        for error in found
    ]


# --- скилл --------------------------------------------------------------------


class Skill:
    """Функция скилла вместе с её контрактом. Возвращается декоратором ``@skill``.

    Вызывается как прежде — ``skill(inputs) -> outputs`` (так её зовёт исполнитель
    старше TAI-ADR-0045), а ``__skill_invoke__`` отдаёт исполнителю ещё и cost."""

    def __init__(
        self,
        function: Callable[..., Any],
        *,
        name: str,
        version: str,
        side_effects: str,
        risk: str,
        idempotency: str,
        timeout: int,
        retry: tuple[int, int],
        description: str | None,
        permissions: Iterable[str],
        preconditions: Iterable[Any],
        postconditions: Iterable[Any],
        cost_model: Mapping[str, Any] | None,
        implementation: Implementation | None,
        inputs_schema: Mapping[str, Any] | None,
        outputs_schema: Mapping[str, Any] | None,
    ) -> None:
        if side_effects not in SIDE_EFFECTS:
            raise ValueError(f"{name}: side_effects — один из {SIDE_EFFECTS}")
        if risk not in RISK_LEVELS:
            raise ValueError(f"{name}: risk — один из {RISK_LEVELS}")
        if idempotency not in IDEMPOTENCY:
            raise ValueError(f"{name}: idempotency — один из {IDEMPOTENCY}")
        max_attempts, backoff = retry
        if side_effects == "external_write" and idempotency == "none" and max_attempts > 1:
            # Ядро отвергнет такой контракт (require_safe_retries): повтор внешней
            # записи без идемпотентности — второй внешний эффект.
            raise ValueError(f"{name}: external_write без идемпотентности не повторяется (retry)")

        self.function = function
        self.name = name
        self.version = str(version)
        self.side_effects = side_effects
        self.risk = risk
        self.idempotency = idempotency
        self.timeout = int(timeout)
        self.retry = (int(max_attempts), int(backoff))
        self.permissions = sorted(set(permissions))
        self.preconditions = list(preconditions)
        self.postconditions = list(postconditions)
        self.cost_model = dict(cost_model) if cost_model else None
        self.implementation = implementation or Local()
        self.is_async = inspect.iscoroutinefunction(function)

        parameters = list(inspect.signature(function).parameters.values())
        if not 1 <= len(parameters) <= 2:
            raise TypeError(f"{name}: функция скилла принимает (inputs) или (inputs, ctx)")
        self.takes_context = len(parameters) == 2
        try:
            hints = typing.get_type_hints(function)
        except NameError as error:
            raise TypeError(
                f"{name}: аннотации функции не разрешаются ({error}) — модели входа и выхода "
                "объявляйте на уровне модуля"
            ) from error
        self.input_model = _model_of(hints.get(parameters[0].name))
        self.output_model = _model_of(hints.get("return"))

        if inputs_schema is not None:
            self.inputs_schema = dict(inputs_schema)
        elif self.input_model is not None:
            self.inputs_schema = _schema_of(self.input_model, "validation")
        else:
            raise TypeError(f"{name}: вход — модель pydantic в аннотации или inputs_schema=")
        if outputs_schema is not None:
            self.outputs_schema = dict(outputs_schema)
        elif self.output_model is not None:
            self.outputs_schema = _schema_of(self.output_model, "serialization")
        else:
            raise TypeError(f"{name}: выход — модель pydantic в аннотации или outputs_schema=")

        doc = inspect.getdoc(function) or ""
        self.description = description if description is not None else doc.split("\n\n")[0].strip()
        self.__doc__ = function.__doc__
        self.__name__ = function.__name__
        self.__qualname__ = function.__qualname__
        self.__module__ = function.__module__

    def __repr__(self) -> str:
        return f"<Skill {self.ref}>"

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def entrypoint(self) -> str:
        return f"{self.__module__}:{self.__qualname__}"

    # -- контракт --

    def implementation_document(
        self, implementation: Implementation | None = None
    ) -> dict[str, Any]:
        chosen = implementation or self.implementation
        if isinstance(chosen, Local):
            return {"protocol": "local", "entrypoint": chosen.entrypoint or self.entrypoint}
        if isinstance(chosen, Http):
            document: dict[str, Any] = {"protocol": "http", "endpoint": chosen.endpoint}
        else:
            document = {
                "protocol": "mcp",
                "endpoint": chosen.endpoint,
                "entrypoint": chosen.tool or self.name,
            }
        if chosen.audience:
            document["auth"] = {"audience": chosen.audience}
        return document

    def contract(self, implementation: Implementation | None = None) -> dict[str, Any]:
        """Контракт v1 в форме запроса ``POST /skills`` (без значений по умолчанию ядра)."""
        document: dict[str, Any] = {
            "inputs": self.inputs_schema,
            "outputs": self.outputs_schema,
            "idempotency": self.idempotency,
            "timeoutSeconds": self.timeout,
            "retryPolicy": {"maxAttempts": self.retry[0], "backoffSeconds": self.retry[1]},
        }
        if self.permissions:
            document["requiredPermissions"] = self.permissions
        if self.preconditions:
            document["preconditions"] = self.preconditions
        if self.postconditions:
            document["postconditions"] = self.postconditions
        if self.cost_model:
            document["costModel"] = self.cost_model
        document["implementation"] = self.implementation_document(implementation)
        return document

    def spec(self, implementation: Implementation | None = None) -> dict[str, Any]:
        """``spec`` объекта ``kind: Skill`` пакета каталога (TAI-ADR-0044)."""
        return {
            "version": self.version,
            "description": self.description,
            "sideEffects": self.side_effects,
            "riskLevel": self.risk,
            "contract": self.contract(implementation),
        }

    @property
    def __skill_contract__(self) -> dict[str, Any]:
        """По нему исполнитель находит local-скиллы в пакете (обнаружение без импорта SDK)."""
        return self.contract()

    # -- вызов --

    def _parse_inputs(self, inputs: Any) -> Any:
        if isinstance(inputs, BaseModel):
            inputs = inputs.model_dump(mode="json", by_alias=True)
        if not isinstance(inputs, dict):
            raise input_violation([{"path": "/", "message": "inputs must be a JSON object"}])
        errors = schema_errors(self.inputs_schema, inputs)
        if errors:
            raise input_violation(errors)
        if self.input_model is None:
            return inputs
        try:
            return self.input_model.model_validate(inputs)
        except ValidationError as error:
            raise input_violation(
                [
                    {"path": "/" + "/".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in error.errors()
                ]
            ) from error

    def _dump_outputs(self, result: Any) -> dict[str, Any]:
        if isinstance(result, BaseModel):
            result = result.model_dump(mode="json", by_alias=True)
        if not isinstance(result, dict):
            raise output_violation(
                [
                    {
                        "path": "/",
                        "message": f"outputs must be a JSON object, got {type(result).__name__}",
                    }
                ]
            )
        errors = schema_errors(self.outputs_schema, result)
        if errors:
            raise output_violation(errors)
        return result

    async def execute(
        self,
        inputs: Any,
        invocation: Invocation | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Проверить вход, вызвать функцию, проверить выход. Возвращает ``(outputs, cost)``."""
        invocation = invocation or Invocation(
            skill=self.ref, protocol="direct", timeout_seconds=self.timeout
        )
        context = SkillContext(invocation, env=env)
        try:
            parsed = self._parse_inputs(inputs)
            args = (parsed, context) if self.takes_context else (parsed,)
            if self.is_async:
                result = await self.function(*args)
            else:
                result = await asyncio.to_thread(self.function, *args)
            return self._dump_outputs(result), context.cost()
        finally:
            await context.aclose()

    def __skill_invoke__(self, inputs: dict[str, Any], meta: Mapping[str, Any]) -> dict[str, Any]:
        """Проводной контракт local-исполнителя (TAI-ADR-0045 п.5): синхронный вызов
        в потоке или дочернем процессе исполнителя, ответ ``{outputs, cost}``."""
        invocation = Invocation(
            skill=self.ref,
            protocol="local",
            invocation_id=meta.get("invocationId"),
            idempotency_key=meta.get("idempotencyKey"),
            timeout_seconds=meta.get("timeoutSeconds") or self.timeout,
        )
        outputs, cost = asyncio.run(self.execute(inputs, invocation))
        return {"outputs": outputs, "cost": cost}

    def __call__(self, inputs: Any) -> dict[str, Any]:
        """``run(inputs) -> outputs`` — как local-скилл зовёт исполнитель старше TAI-ADR-0045."""
        outputs: dict[str, Any] = self.__skill_invoke__(inputs, {})["outputs"]
        return outputs


def skill(
    name: str,
    *,
    version: str,
    side_effects: str,
    risk: str,
    idempotency: str = "none",
    timeout: int = 60,
    retry: tuple[int, int] = (1, 0),
    description: str | None = None,
    permissions: Iterable[str] = (),
    preconditions: Iterable[Any] = (),
    postconditions: Iterable[Any] = (),
    cost_model: Mapping[str, Any] | None = None,
    implementation: Implementation | None = None,
    inputs_schema: Mapping[str, Any] | None = None,
    outputs_schema: Mapping[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Skill]:
    """Объявить функцию скиллом. ``retry`` — ``(maxAttempts, backoffSeconds)``."""

    def decorate(function: Callable[..., Any]) -> Skill:
        return Skill(
            function,
            name=name,
            version=version,
            side_effects=side_effects,
            risk=risk,
            idempotency=idempotency,
            timeout=timeout,
            retry=retry,
            description=description,
            permissions=permissions,
            preconditions=preconditions,
            postconditions=postconditions,
            cost_model=cost_model,
            implementation=implementation,
            inputs_schema=inputs_schema,
            outputs_schema=outputs_schema,
        )

    return decorate


def discover(targets: Iterable[str]) -> list[Skill]:
    """Скиллы из ``модуль:имя``, модуля или пакета (со всеми подмодулями)."""
    found: dict[str, Skill] = {}

    def add(candidate: Any) -> None:
        if isinstance(candidate, Skill):
            other = found.get(candidate.ref)
            if other is not None and other is not candidate:
                raise ValueError(
                    f"{candidate.ref} объявлен дважды: {other.entrypoint}, {candidate.entrypoint}"
                )
            found[candidate.ref] = candidate

    for target in targets:
        if ":" in target:
            module_name, _, attribute = target.partition(":")
            add(getattr(importlib.import_module(module_name), attribute))
            continue
        module = importlib.import_module(target)
        modules = [module]
        for info in pkgutil.walk_packages(getattr(module, "__path__", []), f"{target}."):
            modules.append(importlib.import_module(info.name))
        for item in modules:
            for value in vars(item).values():
                add(value)
    return sorted(found.values(), key=lambda s: s.ref)
