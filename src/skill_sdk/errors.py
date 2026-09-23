"""Ошибка скилла — как её видит исполнитель и ядро (CP-ADR-0056 §5).

Исход, предусмотренный контрактом (конфликт merge, «не найдено»), — это выход
скилла, а не исключение. ``SkillError`` — только сбой: ``retryable=True`` для
сбоя среды (сеть, лимит, недоступный сервис), ``False`` — для того, что повтор
не исправит. Исполнитель читает ``code``, ``retryable`` и ``details`` у любого
исключения, поэтому зависимости от SDK у него нет.
"""

from __future__ import annotations

import re
from typing import Any

CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")


def valid_code(code: str) -> bool:
    return bool(CODE_RE.match(code))


class SkillError(Exception):
    """Сбой вызова скилла: ``code`` — машинный код, ``retryable`` — стоит ли повторять."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        if not CODE_RE.match(code):
            raise ValueError(f"код ошибки {code!r} не соответствует {CODE_RE.pattern}")
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.details = details

    def as_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


def from_exception(exc: BaseException) -> SkillError:
    """Любое исключение реализации как ``SkillError``: ``code``/``retryable`` — если есть."""
    if isinstance(exc, SkillError):
        return exc
    code = getattr(exc, "code", None)
    return SkillError(
        code if isinstance(code, str) and valid_code(code) else "skill_error",
        type(exc).__name__,
        retryable=getattr(exc, "retryable", False) is True,
    )


def input_violation(errors: list[dict[str, str]]) -> SkillError:
    return SkillError(
        "input_contract_violation",
        "inputs do not match the skill's input schema",
        details={"errors": errors[:20]},
    )


def output_violation(errors: list[dict[str, str]]) -> SkillError:
    return SkillError(
        "output_contract_violation",
        "outputs do not match the skill's output schema",
        details={"errors": errors[:20]},
    )
