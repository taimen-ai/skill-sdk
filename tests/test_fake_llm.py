"""FakeLlm: подделка ctx.llm для тестов скилла — ответы, учёт вызовов и cost."""

from __future__ import annotations

import asyncio

import pydantic
import pytest

from skill_sdk import configure_llm, context
from skill_sdk.testing import FakeLlm, LlmCall, ainvoke, invoke
from tests import sample_skills as s


def test_single_answer_and_cost():
    llm = FakeLlm({"summary": "кратко"}, model="m-1", cost_usd=0.002)
    result = invoke(s.summarize, {"text": "длинный текст"}, llm=llm)
    assert result.outputs == {"summary": "кратко"}
    assert result.cost["llm"]["calls"] == 1
    assert result.cost["llm"]["models"] == ["m-1"]
    assert result.cost["llm"]["totalTokens"] == 15
    [call] = llm.calls
    assert (call.mode, call.schema_name, call.prompt) == ("json", "summary", "длинный текст")


def test_answers_in_order_and_exhaustion():
    llm = FakeLlm([{"summary": "раз"}, {"summary": "два"}])
    assert invoke(s.summarize, {"text": "a"}, llm=llm).outputs == {"summary": "раз"}
    assert invoke(s.summarize, {"text": "b"}, llm=llm).outputs == {"summary": "два"}
    with pytest.raises(AssertionError, match="ответов задано 2"):
        invoke(s.summarize, {"text": "c"}, llm=llm)


def test_answer_as_a_function_of_the_call():
    def answer(call: LlmCall) -> dict[str, str]:
        return {"summary": call.prompt.upper()}

    assert invoke(s.summarize, {"text": "abc"}, llm=FakeLlm(answer)).outputs == {"summary": "ABC"}


def test_answer_is_checked_by_the_response_model():
    with pytest.raises(pydantic.ValidationError):
        invoke(s.summarize, {"text": "x"}, llm=FakeLlm({"other": 1}))


def test_calls_are_recorded_after_the_personal_data_guard():
    llm = FakeLlm({"summary": "ok"})
    invoke(s.summarize, {"text": "пишите на ivan@example.com"}, llm=llm)
    assert "ivan@example.com" not in llm.calls[0].prompt
    assert "[ПДн:email]" in llm.calls[0].prompt


def test_previous_llm_factory_is_restored():
    before = context._llm_factory
    invoke(s.summarize, {"text": "x"}, llm=FakeLlm({"summary": "ok"}))
    assert context._llm_factory is before


@pytest.mark.parametrize("target", [s.summarize_later, s.summarize_sync])
async def test_parallel_invocations_keep_their_own_fakes(target):
    # Скилл уступает цикл (или уходит в поток) до ctx.llm — вызовы действительно
    # перемежаются; глобальная подмена фабрики отдала бы первому чужую подделку.
    installed = FakeLlm({"summary": "по конфигурации"})
    configure_llm(lambda: installed)
    try:
        first, second = FakeLlm({"summary": "первый"}), FakeLlm({"summary": "второй"})
        one, two = await asyncio.gather(
            ainvoke(target, {"text": "a"}, llm=first),
            ainvoke(target, {"text": "b"}, llm=second),
        )
        assert (one.outputs, two.outputs) == ({"summary": "первый"}, {"summary": "второй"})
        assert ([c.prompt for c in first.calls], [c.prompt for c in second.calls]) == (
            ["a"],
            ["b"],
        )
        # настройка инсталляции не тронута
        after = await ainvoke(target, {"text": "c"})
        assert after.outputs == {"summary": "по конфигурации"}
    finally:
        configure_llm(None)


def test_answers_are_copied_deeply():
    answer = {"summary": "ok", "meta": {"tags": ["x"]}}
    llm = FakeLlm(answer)
    result = asyncio.run(
        llm.chat_json_object(system_prompt="s", messages=[{"role": "user", "content": "q"}])
    )
    result.data["meta"]["tags"].append("y")
    assert answer["meta"]["tags"] == ["x"]


def test_call_records_temperature_and_max_tokens():
    llm = FakeLlm({"summary": "ok"})
    asyncio.run(
        llm.chat_json_object(
            system_prompt="s",
            messages=[{"role": "user", "content": "q"}],
            temperature=0.3,
            max_tokens=64,
        )
    )
    assert (llm.calls[0].temperature, llm.calls[0].max_tokens) == (0.3, 64)
