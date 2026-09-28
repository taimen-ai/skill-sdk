"""Провайдер ``claude-code`` для ``ctx.llm`` на поддельном ``claude`` в PATH.

Поддельный CLI — скрипт на Python: пишет в журнал argv, stdin и то, что видел в
окружении, и отвечает по сценарию из ``FAKE_CLAUDE_SCRIPT`` (JSON-список ответов,
по одному на вызов; последний повторяется).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from skill_sdk import SkillError, configure_llm
from skill_sdk.claude_code import ClaudeCodeLlm, extract_json
from skill_sdk.testing import invoke
from tests import sample_skills as s

TOKEN = "sk-ant-oat01-secret-subscription-token"

FAKE_CLAUDE = f"""#!{sys.executable}
import json, os, sys, time
log = os.environ["FAKE_CLAUDE_LOG"]
script = json.loads(os.environ["FAKE_CLAUDE_SCRIPT"])
calls = []
if os.path.exists(log):
    with open(log) as f:
        calls = json.load(f)
calls.append({{
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == {TOKEN!r},
    "apiKey": "ANTHROPIC_API_KEY" in os.environ,
    "cwdEmpty": not os.listdir("."),
}})
with open(log, "w") as f:
    json.dump(calls, f)
step = script[min(len(calls), len(script)) - 1]
if step.get("sleep"):
    time.sleep(step["sleep"])
if "stderr" in step:
    sys.stderr.write(step["stderr"])
if "stdout" in step:
    sys.stdout.write(step["stdout"])
sys.exit(step.get("exit", 0))
"""


def reply(result: str, *, is_error: bool = False, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
        "result": result,
        "total_cost_usd": 0.12,
        "usage": {
            "input_tokens": 100,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 5,
            "output_tokens": 20,
        },
        "modelUsage": {"claude-sonnet-5": {"inputTokens": 100}},
        **extra,
    }
    return {"stdout": json.dumps(body)}


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-must-not-leak")
    monkeypatch.setenv("SKILL_LLM_PROVIDER", "claude-code")
    monkeypatch.setenv("SKILL_LLM_MODELS", "claude-sonnet-5")
    for name in ("SKILL_LLM_BASE_URL", "SKILL_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    configure_llm(None)

    def script(*steps: dict[str, object]) -> None:
        monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", json.dumps(list(steps)))

    def calls() -> list[dict[str, object]]:
        return json.loads(log.read_text()) if log.exists() else []

    script.calls = calls  # type: ignore[attr-defined]
    return script


def test_skill_gets_json_from_claude_by_subscription(fake_claude):
    fake_claude(reply('{"summary": "кратко"}'))
    result = invoke(s.summarize, {"text": "длинный текст"})
    assert result.outputs == {"summary": "кратко"}
    # токены — в cost; условная цена CLI под подпиской расходом не считается
    assert result.cost == {
        "llm": {
            "calls": 1,
            "promptTokens": 115,
            "completionTokens": 20,
            "totalTokens": 135,
            "models": ["claude-sonnet-5"],
        }
    }
    [call] = fake_claude.calls()
    argv = call["argv"]
    assert argv[:3] == ["--print", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    # system prompt скилла и сообщение — на stdin, а не в argv
    assert "Перескажи" in call["stdin"] and "длинный текст" in call["stdin"]
    assert '"summary"' in call["stdin"]  # схема ответа из pydantic-модели
    assert not any("длинный текст" in a or "Перескажи" in a for a in argv)
    # токен подписки — из окружения; ключ API до CLI не доходит; каталог пустой
    assert call["token"] is True and call["apiKey"] is False and call["cwdEmpty"] is True
    assert not any(TOKEN in a for a in argv)


def test_json_in_fence_or_prose_is_extracted():
    assert extract_json('Вот ответ:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Итог: {"a": {"b": 2}} — готово') == {"a": {"b": 2}}


def test_invalid_json_is_retried_once_with_the_error(fake_claude):
    fake_claude(reply("не JSON"), reply('{"summary": "со второй попытки"}'))
    result = invoke(s.summarize, {"text": "x"})
    assert result.outputs == {"summary": "со второй попытки"}
    first, second = fake_claude.calls()
    assert "Исправление" not in first["stdin"]
    assert "Исправление" in second["stdin"]
    assert result.cost["llm"]["promptTokens"] == 230  # обе попытки учтены


def test_invalid_twice_moves_to_the_next_model_then_fails_retryably(fake_claude, monkeypatch):
    monkeypatch.setenv("SKILL_LLM_MODELS", "claude-sonnet-5, sonnet")
    fake_claude(reply('{"wrong": 1}'))
    with pytest.raises(SkillError) as error:
        invoke(s.summarize, {"text": "x"})
    assert error.value.code == "llm_invalid_response"
    assert error.value.retryable is True
    models = [c["argv"][c["argv"].index("--model") + 1] for c in fake_claude.calls()]
    assert models == ["claude-sonnet-5", "claude-sonnet-5", "sonnet", "sonnet"]


@pytest.mark.parametrize(
    "step",
    [
        reply("Claude AI usage limit reached|1790000000", is_error=True),
        reply("API Error", is_error=True, api_error_status=429),
        {"stderr": "5-hour limit reached ∙ resets 3pm", "exit": 1},
    ],
)
def test_subscription_limit_is_retryable_without_trying_other_models(
    fake_claude, monkeypatch, step
):
    monkeypatch.setenv("SKILL_LLM_MODELS", "claude-sonnet-5,sonnet")
    fake_claude(step)
    with pytest.raises(SkillError) as error:
        invoke(s.summarize, {"text": "x"})
    assert error.value.code == "llm_rate_limited"
    assert error.value.retryable is True
    assert len(fake_claude.calls()) == 1


def test_cli_failure_is_retryable_and_never_shows_the_token(fake_claude):
    fake_claude({"stderr": f"auth failed for {TOKEN}", "exit": 1})
    with pytest.raises(SkillError) as error:
        invoke(s.summarize, {"text": "x"})
    assert error.value.code == "llm_failed"
    assert error.value.retryable is True
    assert TOKEN not in error.value.message
    assert "***" in error.value.message


def test_timeout_kills_the_cli(fake_claude, monkeypatch):
    monkeypatch.setenv("SKILL_LLM_TIMEOUT_SECONDS", "0.5")
    fake_claude({"sleep": 5, **reply('{"summary": "поздно"}')})
    with pytest.raises(SkillError) as error:
        invoke(s.summarize, {"text": "x"})
    assert error.value.code == "llm_timeout"
    assert error.value.retryable is True


def test_missing_cli_is_retryable(monkeypatch, tmp_path):
    monkeypatch.setenv("SKILL_LLM_PROVIDER", "claude-code")
    monkeypatch.setenv("SKILL_LLM_CLAUDE_BINARY", "claude-not-installed")
    monkeypatch.setenv("PATH", str(tmp_path))
    configure_llm(None)
    with pytest.raises(SkillError) as error:
        invoke(s.summarize, {"text": "x"})
    assert error.value.code == "llm_unavailable"
    assert error.value.retryable is True


def test_unknown_provider_is_a_configuration_error(monkeypatch):
    monkeypatch.setenv("SKILL_LLM_PROVIDER", "aitunnel")
    configure_llm(None)
    with pytest.raises(SkillError) as error:
        invoke(s.summarize, {"text": "x"})
    assert error.value.code == "llm_not_configured"


async def test_chat_json_object_returns_a_dict(fake_claude):
    fake_claude(reply('{"verdict": "ok"}'))
    llm = ClaudeCodeLlm.from_environment(models=("sonnet",))
    result = await llm.chat_json_object(
        system_prompt="Оцени", messages=[{"role": "user", "content": "текст"}]
    )
    assert result.data == {"verdict": "ok"}
    assert result.model == "claude-sonnet-5"
    assert result.cost_usd is None


async def test_without_models_the_cli_default_is_used(fake_claude):
    class Out(BaseModel):
        n: int

    fake_claude(reply('{"n": 3}'))
    llm = ClaudeCodeLlm.from_environment()
    result = await llm.chat_json(
        system_prompt="Посчитай", messages=[], response_model=Out, schema_name="out"
    )
    assert result.data == Out(n=3)
    [call] = fake_claude.calls()
    assert "--model" not in call["argv"]
