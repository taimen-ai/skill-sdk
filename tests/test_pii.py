"""Страж персональных данных в промптах LLM (TAI-ADR-0056 Р13, FR-025, SC-004)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import BaseModel

from skill_sdk import SkillContext, configure_llm, skill
from skill_sdk.pii import redact, redact_messages, strip_personal_fields
from skill_sdk.testing import invoke

PERSONAL = [
    ("Директор Иванов Иван Иванович подписал", "фио", "Иванов Иван Иванович"),
    ("подписал Мамедов Эльдар Гусейн оглы", "фио", "Гусейн оглы"),
    ("Ответственный — Петрова А. С.", "фио", "Петрова А. С."),
    ("Согласовано: А.С. Петрова", "фио", "Петрова"),
    ("Контакт: Анна Сергеевна", "фио", "Анна Сергеевна"),
    ("Мария Ильинична Кузнецова", "фио", "Кузнецова"),
    ("СНИЛС 112-233-445 95", "снилс", "112-233-445 95"),
    ("Паспорт серия 45 10 № 123456 выдан", "паспорт", "123456"),
    ("паспорт 4510123456", "паспорт", "4510123456"),
    ("тел. +7 (912) 345-67-89", "телефон", "345-67-89"),
    ("звоните 8 912 345 67 89", "телефон", "345 67 89"),
    ("пишите ivan.petrov@mail.ru", "email", "ivan.petrov@mail.ru"),
]

BUSINESS = [
    "ООО «Ромашка», ИНН 7707083893, КПП 770701001, ОГРН 1027700132195",
    "ИНН физлица не в тексте; ОКПД2 62.01.11.000, КТРУ 58.29.50.000-00000001",
    "НМЦК 2 500 000,00 руб., срок исполнения до 31.12.2026, п. 4.2 проекта контракта",
    "Требование ET44 и TR442; преимущество PVS33044 — 15%",
    "Разработка программного обеспечения для Министерства финансов",
    "Лицензия ФСТЭК № 1234 от 12.03.2024, действует до 12.03.2029",
    "Реестровый номер 0338100003725000008, лот 1, код ОКЕИ 876",
]


@pytest.mark.parametrize(("text", "kind", "value"), PERSONAL)
def test_personal_data_is_replaced_with_a_marker(text, kind, value):
    clean, found = redact(text)
    assert value not in clean
    assert f"[ПДн:{kind}]" in clean
    assert found.found[kind] >= 1


@pytest.mark.parametrize("text", BUSINESS)
def test_business_text_is_unchanged(text):
    clean, found = redact(text)
    assert clean == text and not found


def test_messages_are_redacted_and_other_fields_kept():
    system, messages, found = redact_messages(
        "Ты помощник. Не упоминай Иванова И.И.",
        [{"role": "user", "content": "Звонил Сидоров Пётр Петрович, +7 999 111-22-33"}],
    )
    assert "Иванова И.И." not in system and "Сидоров" not in messages[0]["content"]
    assert messages[0]["role"] == "user"
    assert found.as_log() == {"телефон": 1, "фио": 2}


def test_fields_marked_personal_are_stripped():
    schema = {
        "type": "object",
        "properties": {
            "contactName": {"type": "string", "x-personal-data": "allowed"},
            "inn": {"type": "string"},
        },
    }
    assert strip_personal_fields({"contactName": "Иванов", "inn": "7707083893"}, schema) == {
        "inn": "7707083893"
    }
    assert strip_personal_fields({"a": 1}, None) == {"a": 1}


class _Recorder:
    calls: list[dict[str, Any]]

    def __init__(self) -> None:
        self.calls = []

    async def chat_json_object(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _Result:
            data: dict[str, Any] = {"ok": True}  # noqa: RUF012
            usage = None
            model = "fake"

        return _Result()

    async def aclose(self) -> None:
        return None


class Text(BaseModel):
    text: str


class Done(BaseModel):
    ok: bool


@skill("test.ask", version="1", side_effects="none", risk="low")
async def ask(inputs: Text, ctx: SkillContext) -> Done:
    """Спросить модель."""
    result = await ctx.llm.chat_json_object(
        system_prompt="Отвечай JSON", messages=[{"role": "user", "content": inputs.text}]
    )
    return Done(ok=bool(result.data["ok"]))


def test_ctx_llm_never_sends_or_logs_personal_values(caplog):
    recorder = _Recorder()
    configure_llm(lambda: recorder)
    try:
        with caplog.at_level(logging.WARNING, logger="skill_sdk"):
            invoke(ask, {"text": "Кузнецова Мария Ильинична, СНИЛС 112-233-445 95, ИНН 7707083893"})
    finally:
        configure_llm(None)
    [call] = recorder.calls
    sent = call["messages"][0]["content"]
    assert "Кузнецова" not in sent and "112-233-445" not in sent
    assert "7707083893" in sent  # ИНН организации — не персональные данные
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "снилс" in logged and "Кузнецова" not in logged and "112-233-445" not in logged
