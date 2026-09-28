"""Страж персональных данных в промптах ИИ (TAI-ADR-0056 Р13, FR-025).

Персональные данные физических лиц не уходят в LLM: ``ctx.llm`` пропускает
``system_prompt`` и ``messages`` через ``redact`` перед вызовом модели. Найденное
заменяется маркером вида ``[ПДн:фио]``; в журнал пишется только сколько и чего
найдено, без значений.

Шаблоны консервативны к деловому тексту: ИНН и КПП организаций, коды ОКПД2,
суммы и даты не трогаются. Паспорт — только рядом со словом «паспорт» или
«серия» (десять цифр подряд — это и ИНН организации). ФИО — по отчеству
(суффиксы -вич, -вна, -ична, оглы, кызы) или по инициалам рядом с фамилией.
E-mail маскируется любой: по адресу не отличить личный от служебного.

Поля, которые пакет онтологии явно разрешил хранить (``x-personal-data:
allowed``), в промпт не передаются вовсе — ``strip_personal_fields``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

_UP = "А-ЯЁ"
_LOW = "а-яё"
_NAME = rf"[{_UP}][{_LOW}]+(?:-[{_UP}][{_LOW}]+)?"
_PATRONYMIC = (
    rf"[{_UP}][{_LOW}]+(?:ович|евич|ич|овна|евна|ична|инична)|[{_UP}][{_LOW}]+\s(?:оглы|кызы)"
)

#: Порядок важен: более специфичные шаблоны раньше (паспорт до телефона).
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("снилс", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[ -]\d{2}(?!\d)")),
    (
        "паспорт",
        re.compile(r"(?i)(?:паспорт\w*|серия)[^\n\d]{0,20}\d{2}\s?\d{2}[^\n\d]{0,12}\d{6}(?!\d)"),
    ),
    (
        "телефон",
        re.compile(r"(?<![\d.])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?![\d.])"),
    ),
    ("email", re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")),
    ("фио", re.compile(rf"\b{_NAME}\s{_NAME}\s(?:{_PATRONYMIC})\b")),
    ("фио", re.compile(rf"\b(?:{_NAME}\s)?(?:{_PATRONYMIC})\s{_NAME}\b")),
    ("фио", re.compile(rf"\b{_NAME}\s(?:{_PATRONYMIC})\b")),
    ("фио", re.compile(rf"\b{_NAME}\s[{_UP}]\.\s?[{_UP}]\.")),
    ("фио", re.compile(rf"(?<![{_UP}{_LOW}])[{_UP}]\.\s?[{_UP}]\.\s?{_NAME}\b")),
)


def marker(kind: str) -> str:
    return f"[ПДн:{kind}]"


@dataclass
class Redaction:
    """Итог стража: сколько и чего заменено (без значений)."""

    found: Counter[str] = field(default_factory=Counter)

    def __bool__(self) -> bool:
        return bool(self.found)

    def merge(self, other: Redaction) -> None:
        self.found.update(other.found)

    def as_log(self) -> dict[str, int]:
        return dict(sorted(self.found.items()))


def redact(text: str) -> tuple[str, Redaction]:
    """Заменить персональные данные в тексте маркерами."""
    result = Redaction()
    for kind, pattern in PATTERNS:
        text, count = pattern.subn(marker(kind), text)
        if count:
            result.found[kind] += count
    return text, result


def redact_messages(
    system_prompt: str, messages: list[Mapping[str, Any]]
) -> tuple[str, list[dict[str, Any]], Redaction]:
    """``system_prompt`` и ``content`` каждого сообщения через ``redact``."""
    total = Redaction()
    clean_system, found = redact(system_prompt)
    total.merge(found)
    clean: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, str):
            item["content"], found = redact(content)
            total.merge(found)
        clean.append(item)
    return clean_system, clean, total


def personal_fields(attributes_schema: Mapping[str, Any] | None) -> set[str]:
    """Имена атрибутов, помеченных ``x-personal-data: allowed`` в схеме вида."""
    props = (attributes_schema or {}).get("properties")
    if not isinstance(props, Mapping):
        return set()
    return {
        name
        for name, spec in props.items()
        if isinstance(spec, Mapping) and spec.get("x-personal-data") == "allowed"
    }


def strip_personal_fields(
    attributes: Mapping[str, Any], attributes_schema: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Копия атрибутов без полей, разрешённых пакетом как персональные данные."""
    hidden = personal_fields(attributes_schema)
    return {name: value for name, value in attributes.items() if name not in hidden}
