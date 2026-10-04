"""Страж white-label: имени продукта нет в именах кода SDK, имён провайдеров — нигде.

SDK поставляется под брендом инсталляции (ADR-0022 ядра). Имя продукта допустимо
только там, где оно — часть внешнего контракта или рассказ о проекте; каждое место
перечислено с причиной. Имена внешних провайдеров живут в пакетах интеграций, а не в
SDK: подключение SDK знает как строку ``type`` (TAI-ADR-0061, SC-008).
"""

from __future__ import annotations

import re
from pathlib import Path

import skill_sdk
from skill_sdk import connections

REPO = Path(__file__).resolve().parents[1]
CODENAME = re.compile(r"taimen", re.IGNORECASE)
PROVIDERS = re.compile(r"amo\s*crm|bitrix|hubspot|salesforce|pipedrive|zoho", re.IGNORECASE)
SELF = "tests/test_white_label.py"

ALLOWED_CODENAME = {
    # Описание пакета и docstring модуля называют проект, которому принадлежит SDK.
    "pyproject.toml",
    "src/skill_sdk/__init__.py",
    # apiVersion объектов каталога — машинный контракт пакетов (TAI-ADR-0044).
    "src/skill_sdk/package.py",
    SELF,
}

SCAN_GLOBS = ["src/**/*.py", "tests/**/*.py", "pyproject.toml"]


def _lines() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for pattern in SCAN_GLOBS:
        for path in sorted(REPO.glob(pattern)):
            if path.is_file():
                rel = path.relative_to(REPO).as_posix()
                for lineno, line in enumerate(path.read_text().splitlines(), 1):
                    found.append((rel, lineno, line))
    return found


def test_codename_only_in_allowlisted_files() -> None:
    offenders = [
        f"{rel}:{n}: {line.strip()[:80]}"
        for rel, n, line in _lines()
        if rel not in ALLOWED_CODENAME and CODENAME.search(line)
    ]
    assert not offenders, "имя продукта вне списка исключений:\n" + "\n".join(offenders)


def test_no_provider_names_in_sdk() -> None:
    offenders = [
        f"{rel}:{n}: {line.strip()[:80]}"
        for rel, n, line in _lines()
        if rel != SELF and PROVIDERS.search(line)
    ]
    assert not offenders, "имя провайдера в SDK:\n" + "\n".join(offenders)


def test_public_names_and_settings_are_neutral() -> None:
    assert not [name for name in skill_sdk.__all__ if CODENAME.search(name)]
    settings = [
        value
        for name, value in vars(connections).items()
        if name.startswith(("ENV_", "DEFAULT_", "ROLE_")) and isinstance(value, str)
    ]
    assert settings and not [v for v in settings if CODENAME.search(v) or PROVIDERS.search(v)]


def test_allowlisted_files_still_exist() -> None:
    for rel in sorted(ALLOWED_CODENAME):
        assert (REPO / rel).exists(), f"файл из списка исключений исчез: {rel}"
